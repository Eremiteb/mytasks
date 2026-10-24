"""Источник поиска MP3 Muzofond (`muzofond.fm`).

Используется только как запись `search_sources`: `/search/<запрос>` переадресует
на `/music/<запрос>`; карточка `li.item` содержит `li.play[data-url]` (прямая
ссылка на MP3 через `dl*.muzofond.fm`), `span.artist` и `span.track`.
"""
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")


def parse_search(html, page_url):
    tracks = []
    for item in BeautifulSoup(html, "html.parser").select("li.item"):
        play = item.select_one("li.play[data-url]")
        artist = item.select_one(".trackTitle .artist")
        title = item.select_one(".trackTitle .track")
        if play is None or artist is None or title is None:
            continue
        tracks.append({
            "artist": artist.get_text(" ", strip=True),
            "title": title.get_text(" ", strip=True),
            "download_url": urljoin(page_url, play.get("data-url") or ""),
            "referer": page_url,
        })
    return tracks


def search_tracks(session, base_url, search_path):
    url = urljoin(base_url, search_path)
    response = session.get(url, timeout=(5, 20))
    if response.status_code == 404:
        # Пустую выдачу Muzofond отдаёт как 404 — это «не найдено», а не сбой.
        return []
    response.raise_for_status()
    return parse_search(response.text, response.url if isinstance(response.url, str) else url)
