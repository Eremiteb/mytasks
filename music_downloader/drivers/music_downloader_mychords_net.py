import logging
import re
import time
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

logger = logging.LoggerAdapter(logging.getLogger("Engine"), {"site": "mychords_net"})

_TRACK_ID_RE = re.compile(r"/(\d+)-[^/]+\.html?$")


def parse_listing(html, base_url):
    """Возвращает записи списка новинок: ID и адрес страницы трека, исполнитель и название."""
    tracks = []
    for link in BeautifulSoup(html, "html.parser").select("li.b-listing__full__item a.b-listing__full__item__name"):
        href = link.get("href", "")
        track_id = _TRACK_ID_RE.search(href)
        if track_id is None:
            continue
        text = unescape(link.get_text(" ", strip=True))
        artist, separator, title = text.partition(" - ")
        tracks.append({
            "id": track_id[1],
            "artist": artist.strip() if separator else None,
            "title": title.strip() if separator else text,
            "src": urljoin(base_url, href),
        })
    return tracks


def parse_player_url(html, base_url):
    """Находит адрес встроенного плеера на странице трека."""
    player = BeautifulSoup(html, "html.parser").select_one("div.b-words__player[data-src]")
    if player is None:
        return None
    iframe = BeautifulSoup(player["data-src"], "html.parser").find("iframe", src=True)
    return urljoin(base_url, iframe["src"]) if iframe else None


def parse_player_file(html):
    """Извлекает прямую ссылку на аудио из страницы плеера."""
    file_tag = BeautifulSoup(html, "html.parser").select_one("#file[data-mp3]")
    if file_tag is None:
        return None
    download_url = file_tag["data-mp3"].strip()
    if urlsplit(download_url).scheme not in ("http", "https"):
        return None
    return download_url


def get_tracks(session, base_url, category_path):
    """Возвращает список новинок; адрес аудио запрашивается позже, только для новых треков."""
    target_url = urljoin(base_url, category_path)
    try:
        response = session.get(target_url, timeout=(5, 20))
        response.raise_for_status()
        tracks = parse_listing(response.text, base_url)
    except Exception as error:
        logger.error(f"MyChords: не удалось прочитать список {category_path}: {error}")
        return []
    if not tracks:
        logger.warning(f"MyChords: в списке {category_path} не найдено треков")
    else:
        logger.info(f"MyChords: {category_path} — получено треков: {len(tracks)}")
    return tracks


def resolve_track(session, track):
    """Открывает страницу трека и плеер, возвращает ссылку на аудио или None."""
    page_url = track["src"]
    try:
        page = session.get(page_url, timeout=(5, 20))
        page.raise_for_status()
        player_url = parse_player_url(page.text, page_url)
        if not player_url:
            logger.warning(f"MyChords: на странице {page_url} нет плеера")
            return None
        player = session.get(player_url, headers={"Referer": page_url}, timeout=(5, 20))
        player.raise_for_status()
        download_url = parse_player_file(player.text)
        if download_url is None:
            logger.warning(f"MyChords: плеер {player_url} не содержит ссылки на аудио")
            return None
    except Exception as error:
        logger.warning(f"MyChords: не удалось получить аудио для {page_url}: {error}")
        return None
    finally:
        time.sleep(1)
    return {"download_url": download_url, "referer": player_url, "src": page_url}
