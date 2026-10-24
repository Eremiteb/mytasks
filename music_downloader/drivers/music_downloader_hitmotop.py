"""Источник поиска MP3 Hitmotop (`rus.hitmotop.com`, переадресует на зеркало `hitmoz`).

Используется только как запись `search_sources`: карточка выдачи
`li[data-musmeta]` несёт JSON с исполнителем, названием и прямой ссылкой на MP3.
"""
import json
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")


def parse_search(html, page_url):
    """Разбирает выдачу поиска; ссылки абсолютные, Referer — итоговый адрес страницы."""
    tracks = []
    for item in BeautifulSoup(html, "html.parser").select("li[data-musmeta]"):
        try:
            meta = json.loads(item.get("data-musmeta") or "")
        except ValueError:
            continue
        if not isinstance(meta, dict) or not meta.get("url"):
            continue
        tracks.append({
            "artist": meta.get("artist"),
            "title": meta.get("title"),
            "download_url": urljoin(page_url, meta["url"]),
            "referer": page_url,
        })
    return tracks


def search_tracks(session, base_url, search_path):
    url = urljoin(base_url, search_path)
    response = session.get(url, timeout=(5, 20))
    response.raise_for_status()
    return parse_search(response.text, response.url if isinstance(response.url, str) else url)
