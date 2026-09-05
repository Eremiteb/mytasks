import importlib.util
import logging
import os
import re
import time
import unicodedata
from html import unescape
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

logger = logging.LoggerAdapter(logging.getLogger("Engine"), {"site": "shazam_com"})


def _load_driver(name):
    """Загружает соседний драйвер по пути: движок не добавляет каталог загрузчика в sys.path."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sefon = _load_driver("music_downloader_sefon_pro")
lmusic = _load_driver("music_downloader_lmusic_kz")

CHART_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux i686; rv:60.9) "
        "Gecko/20100101 Goanna/4.1 Firefox/60.9 PaleMoon/28.3.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}


def parse_chart(html):
    """Извлекает полный мировой Top 200, не смешивая его с рекомендациями."""
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = urlsplit(canonical.get("href", "") if canonical else "")
    if (
        canonical_url.hostname not in ("shazam.com", "www.shazam.com")
        or not canonical_url.path.rstrip("/").endswith("/charts/top-200/world")
    ):
        raise ValueError("Shazam вернул не страницу мирового Top 200")

    # React присылает часть текста отдельными HTML-вставками. JavaScript не выполняем.
    elements = {element["id"]: element for element in soup.find_all(id=True)}
    for script in soup.find_all("script"):
        for source_id, target_id in re.findall(r'\$RS\("([^"]+)",\s*"([^"]+)"\)', script.get_text()):
            source, target = elements.get(source_id), elements.get(target_id)
            if source is None or target is None:
                raise ValueError("Неполная серверная HTML-вставка Shazam")
            target.replace_with(*list(source.contents))
            source.decompose()

    tracks = []
    for card in soup.select('[data-test-id="songItem"]'):
        title = next((
            tag for tag in card.select('[data-test-id="charts_userevent_list_songTitle"]')
            if tag.get_text(" ", strip=True)
        ), None)
        artist = card.select_one('[data-test-id="charts_userevent_list_artistName"]')
        rank = card.select_one('[class*="SongItem-module_rankingNumber"]')
        if title is None or artist is None or rank is None:
            raise ValueError("В карточке чарта нет названия, исполнителя или позиции")
        song_id = re.search(r"/song/(\d+)(?:/|$)", title.get("href", ""))
        if song_id is None:
            raise ValueError("В карточке чарта отсутствует идентификатор песни")
        tracks.append({
            # Современный song/ADAM ID из ссылки Shazam, а не ID найденного MP3.
            "id": f"adam_{song_id[1]}",
            "rank": int(rank.get_text(strip=True)),
            "artist": unescape(artist.get_text(" ", strip=True)),
            "title": unescape(title.get_text(" ", strip=True)),
            "src": urljoin("https://www.shazam.com", title["href"]),
        })
    if (
        len(tracks) != 200
        or [track["rank"] for track in tracks] != list(range(1, 201))
        or len({track["id"] for track in tracks}) != 200
        or any(not track["artist"] or not track["title"] for track in tracks)
    ):
        raise ValueError(f"Ожидался полный Top 200 с уникальными позициями и ID; получено {len(tracks)} карточек")
    return tracks


def get_tracks(session, base_url, category_path):
    """Возвращает метаданные чарта; поиск MP3 выполняется после дедупликации движком."""
    target_url = urljoin(base_url, category_path)
    try:
        response = session.get(target_url, headers=CHART_HEADERS, timeout=(5, 20))
        response.raise_for_status()
        if response.status_code != 200 or not response.text.strip():
            raise ValueError(f"Пустой или неожиданный ответ Shazam: HTTP {response.status_code}")
        tracks = parse_chart(response.text)
    except Exception as error:
        logger.error(f"Shazam: не удалось прочитать мировой чарт: {error}")
        return []
    logger.info(f"Shazam: получено {len(tracks)} позиций; источники будут проверены перед скачиванием")
    return tracks


def _match_key(value):
    """Игнорирует регистр и пунктуацию, но не удаляет слова remix, live и другие версии."""
    text = unicodedata.normalize("NFKC", unescape(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _search_sefon(session, query):
    path = "/search/?" + urlencode({"q": query})
    return sefon.get_tracks(session, "https://sefon.pro", path), urljoin("https://sefon.pro", path)


def _search_lmusic(session, query):
    url = "https://lmusic.kz/search?" + urlencode({"q": query})
    response = session.get(url, timeout=(5, 15))
    response.raise_for_status()
    return lmusic.parse_tracks(response.text, "https://lmusic.kz", url), url


def resolve_track(session, track):
    """Ищет совпадение исполнителя и названия в публичных источниках MP3."""
    artist, title = track["artist"], track["title"]
    expected = (_match_key(artist), _match_key(title))
    if not all(expected):
        logger.warning("Shazam: нельзя искать трек без исполнителя или названия")
        return None
    for source, search in (("sefon.pro", _search_sefon), ("lmusic_kz", _search_lmusic)):
        try:
            candidates, search_url = search(session, f"{artist} {title}")
            for candidate in candidates:
                actual = (_match_key(candidate.get("artist")), _match_key(candidate.get("title")))
                download_url = candidate.get("download_url") or ""
                if actual != expected or urlsplit(download_url).scheme not in ("http", "https"):
                    continue
                referer = candidate.get("referer") or search_url
                # Проверяем доступность ссылки, не скачивая тело повторно.
                with session.get(download_url, headers={"Referer": referer}, stream=True, timeout=(5, 15)) as response:
                    if response.status_code != 200:
                        logger.warning(f"Shazam: {source}, ссылка недоступна (HTTP {response.status_code})")
                        continue
                logger.info(f"Shazam: {artist} — {title}: найдено совпадение в {source}")
                return {
                    "download_url": download_url,
                    "referer": referer,
                    "src": candidate.get("src") or search_url,
                }
        except Exception as error:
            logger.warning(f"Shazam: поиск {artist} — {title} в {source} не удался: {error}")
        finally:
            time.sleep(1)
    logger.warning(f"Shazam: {artist} — {title}: совпадение с доступной ссылкой не найдено")
    return None
