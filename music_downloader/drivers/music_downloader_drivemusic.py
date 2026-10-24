"""Источник поиска MP3 DriveMusic (`drivemusic.me`).

Используется только как запись `search_sources`: поиск DLE
`/?do=search&subaction=search&story=<запрос>`; карточка `.music-popular__item`
содержит `button[data-url]` (MP3 на `cdn*.drivemusic.me`, ссылка с меткой времени,
поэтому берётся непосредственно перед скачиванием), название
`a.popular-play-author` и исполнителя `.popular-play-composition a`.
"""
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")


def parse_search(html, page_url):
    tracks = []
    for item in BeautifulSoup(html, "html.parser").select(".music-popular__item"):
        button = item.select_one("[data-url]")
        title = item.select_one("a.popular-play-author")
        artist = item.select_one(".popular-play-composition a")
        if button is None or title is None or artist is None:
            continue
        tracks.append({
            "artist": artist.get_text(" ", strip=True),
            "title": title.get_text(" ", strip=True),
            "download_url": urljoin(page_url, button.get("data-url") or ""),
            "referer": page_url,
            "src": urljoin(page_url, title.get("href") or ""),
        })
    return tracks


def search_tracks(session, base_url, search_path):
    url = urljoin(base_url, search_path)
    response = session.get(url, timeout=(5, 20))
    response.raise_for_status()
    return parse_search(response.text, response.url if isinstance(response.url, str) else url)
