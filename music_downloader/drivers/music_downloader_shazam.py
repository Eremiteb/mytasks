import importlib.util
import json
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

# Shazam (Next.js App Router за Fastly) периодически отдаёт по адресу чарта
# HTTP 200 со страницей чужой песни (иной canonical, ~1.8 МБ вместо ~3.7 МБ).
# Дефект держится до истечения TTL узла и виден не со всех маршрутов, причём
# полноценная HTML-страница и RSC-поток того же адреса «ломаются» независимо
# друг от друга. Поэтому каждая попытка сначала читает HTML, а при неудаче —
# RSC-поток (заголовок `RSC: 1`, ответ `text/x-component` с теми же данными в
# полезной нагрузке `self.__next_f`); попытки распределены по минутам и идут с
# новым соединением. Query-параметр-«антикэш» не добавлять: отдельный ключ
# Fastly чаще указывает как раз на «отравленную» копию.
CHART_RETRY_DELAYS = (15, 45, 90)

# Куски RSC-потока Next.js: `self.__next_f.push([1, "<json-строка>"])`.
_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', re.S)
# Блок карточки чарта в RSC: имя трека, имя исполнителя и ссылка на песню идут
# подряд в пропсах плавающего меню SongItem.
_FLIGHT_TRACK_RE = re.compile(
    r'"trackName":"((?:[^"\\]|\\.)*)",'
    r'"artistName":"((?:[^"\\]|\\.)*)"'
    r'(?:,"aria-label":"(?:[^"\\]|\\.)*")?'
    r',"url":"https://www\.shazam\.com/song/(\d+)/[^"]*"'
)


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


def parse_flight_chart(body):
    """Извлекает Top 200 из RSC-потока Next.js.

    Принимает и «сырой» ответ `text/x-component`, и полную HTML-страницу — в
    последней тот же поток лежит в тегах `self.__next_f.push`. JavaScript не
    исполняется: порядок карточек в потоке совпадает с позициями чарта
    (проверено против DOM-разбора), поэтому ранг берётся из порядка следования.
    """
    chunks = _FLIGHT_PUSH_RE.findall(body)
    flight = "".join(json.loads(chunk) for chunk in chunks) if chunks else body

    tracks = []
    seen = set()
    for title_raw, artist_raw, song_id in _FLIGHT_TRACK_RE.findall(flight):
        if song_id in seen:
            continue
        seen.add(song_id)
        tracks.append({
            # Тот же song/ADAM ID, что и у DOM-разбора, — ключи БД совпадают.
            "id": f"adam_{song_id}",
            "rank": len(tracks) + 1,
            "artist": unescape(json.loads(f'"{artist_raw}"')).strip(),
            "title": unescape(json.loads(f'"{title_raw}"')).strip(),
            "src": f"https://www.shazam.com/song/{song_id}/",
        })
    if len(tracks) != 200 or any(not track["artist"] or not track["title"] for track in tracks):
        raise ValueError(f"RSC-поток Shazam: ожидался Top 200 с уникальными ID; получено {len(tracks)}")
    return tracks


# Источники чарта в порядке предпочтения: обычная страница, затем RSC-поток
# того же адреса (отдельный объект кэша, отравляется независимо).
CHART_SOURCES = (
    ("HTML", {}, parse_chart),
    ("RSC", {"RSC": "1"}, parse_flight_chart),
)


def get_tracks(session, base_url, category_path):
    """Возвращает метаданные чарта; поиск MP3 выполняется после дедупликации движком.

    Каждая попытка читает HTML-страницу, а при неудаче — RSC-поток того же
    адреса; попытки повторяются по расписанию ``CHART_RETRY_DELAYS`` с новым
    соединением. ``ERROR`` пишется только после того, как оба источника не
    ответили ни разу, — сама по себе такая запись означает временный сбой
    Shazam/CDN. На поломку разбора указывают ``В карточке чарта...`` /
    ``Ожидался полный Top 200...`` / ``RSC-поток Shazam...`` при живом чарте.
    """
    target_url = urljoin(base_url, category_path)
    last_error = None
    attempt = 0
    for delay in (0, *CHART_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        attempt += 1
        for kind, extra_headers, parser in CHART_SOURCES:
            headers = {**CHART_HEADERS, **extra_headers}
            if attempt > 1:
                # Новое соединение — шанс выйти на другой пограничный узел CDN.
                headers["Connection"] = "close"
            try:
                response = session.get(target_url, headers=headers, timeout=(5, 20))
                response.raise_for_status()
                if response.status_code != 200 or not response.text.strip():
                    raise ValueError(f"пустой ответ: HTTP {response.status_code}")
                tracks = parser(response.text)
            except Exception as error:
                last_error = f"{kind}: {error}"
                continue
            if kind != "HTML":
                logger.info("Shazam: HTML-страница недоступна, чарт получен из RSC-потока")
            logger.info(f"Shazam: получено {len(tracks)} позиций; источники будут проверены перед скачиванием")
            return tracks
        logger.warning(f"Shazam: попытка {attempt} чтения мирового чарта не удалась ({last_error})")
    logger.error(f"Shazam: не удалось прочитать мировой чарт за {attempt} попыток (вероятен временный сбой Shazam/CDN): {last_error}")
    return []


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
