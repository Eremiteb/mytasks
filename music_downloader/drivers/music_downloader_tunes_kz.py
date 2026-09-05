import logging
import random
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")

_TRACK_RE = re.compile(r"^/(\d+)-[^/]+\.html?$", re.IGNORECASE)
_MP3_RE = re.compile(r"\.mp3($|\?)", re.IGNORECASE)


def _clean_title_tail(text: str | None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"\s+скачать\b.*$", "", text, flags=re.IGNORECASE).strip()
    return cleaned


def _attr_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value)
    return ""


def _norm_href(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        try:
            u = urlparse(href)
            return u.path or ""
        except Exception:
            return href
    if not href.startswith("/"):
        href = "/" + href
    return href


def _split_artist_title(text: str):
    t = (text or "").strip()

    # распространённые разделители
    for sep in (" - ", " — ", " – "):
        if sep in t:
            a, b = t.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a and b:
                # Удаляем дублирование имени исполнителя из заголовка
                if b.endswith(" " + a):
                    b = b[:-(len(a) + 1)].strip()
                return a, b
    return None, t or None


def _should_prefer_page_pair(list_artist, list_title, page_artist, page_title) -> bool:
    """
    Решаем, стоит ли заменить пару artist/title из списка
    на пару из страницы трека.
    """
    if not (page_artist and page_title):
        return False

    if not (list_artist and list_title):
        return True

    la = list_artist.strip().lower()
    lt = list_title.strip().lower()
    pa = page_artist.strip().lower()
    pt = page_title.strip().lower()

    if not (la and lt and pa and pt):
        return True

    # Явные мусорные хвосты в тексте списка.
    if "скач" in la or "скач" in lt:
        return True

    # Частый сбой: в title из списка попадает имя артиста.
    if pa in lt:
        return True

    # Если "artist" из списка выглядит как фрагмент названия,
    # а страница даёт полноценную пару — доверяем странице.
    return bool(la in pt and pa not in lt)


def _extract_page_artist_title(soup: BeautifulSoup):
    """
    Пытаемся добыть артиста/название с самой страницы трека.
    Приоритет:
      1) meta og:title / twitter:title (часто "Artist - Title")
      2) <h1> и соседние блоки
      3) title страницы
    """
    def get_meta(prop_or_name):
        def _normalize_meta_content(value):
            if isinstance(value, str):
                cleaned = value.strip()
                return cleaned or None
            if isinstance(value, (list, tuple)):
                cleaned = " ".join(str(part) for part in value).strip()
                return cleaned or None
            return None

        # <meta property="og:title" content="...">
        tag = soup.find("meta", attrs={"property": prop_or_name})
        if tag:
            content = _normalize_meta_content(tag.get("content"))
            if content:
                return content
        tag = soup.find("meta", attrs={"name": prop_or_name})
        if tag:
            content = _normalize_meta_content(tag.get("content"))
            if content:
                return content
        return None

    meta = get_meta("og:title") or get_meta("twitter:title")
    if meta:
        a, t = _split_artist_title(meta)
        if a and t:
            return a, t
        if t:
            return None, t

    h1 = soup.find("h1")
    if h1:
        txt = (h1.get_text(" ", strip=True) or "").strip()
        if txt:
            a, t = _split_artist_title(txt)
            if a and t:
                return a, t
            return None, txt

    title = soup.title.get_text(" ", strip=True).strip() if soup.title else None
    if title:
        # часто title содержит лишнее "скачать" / домен — режем по "|"
        title = title.split("|")[0].strip()
        a, t = _split_artist_title(title)
        if a and t:
            return a, t
        return None, title

    return None, None


def _extract_download_link(session, base_url, track_url):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": track_url,
    }

    try:
        r = session.get(track_url, timeout=25, headers=headers)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"TUNES.KZ: failed to open track page {track_url}: {e}")
        return None, None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # 1) кнопка/ссылка скачать
    for a in soup.find_all("a", href=True):
        txt = (a.get_text(" ", strip=True) or "").lower()
        href = _attr_to_text(a.get("href")).strip()
        if not href:
            continue
        if "скач" in txt:
            return urljoin(base_url, href), soup, r.text

    # 2) прямой mp3
    for a in soup.find_all("a", href=True):
        href = _attr_to_text(a.get("href")).strip()
        if not href:
            continue
        if _MP3_RE.search(href):
            return urljoin(base_url, href), soup, r.text

    return None, soup, r.text


def get_tracks(session, base_url, category_path):
    target_url = urljoin(base_url, category_path)

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": base_url,
    }

    try:
        resp = session.get(target_url, timeout=30, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"TUNES.KZ: failed to fetch category {category_path}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    tracks = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = _norm_href(_attr_to_text(a.get("href")))
        m = _TRACK_RE.match(href)
        if not m:
            continue

        track_id = m.group(1)
        if track_id in seen:
            continue
        seen.add(track_id)

        track_url = urljoin(base_url, href)
        list_text = (a.get_text(" ", strip=True) or "").strip()

        artist, title = _split_artist_title(list_text)
        download_url, page_soup, _ = _extract_download_link(session, base_url, track_url)
        if not download_url:
            continue

        p_artist, p_title = _extract_page_artist_title(page_soup) if page_soup else (None, None)

        # Если в списке не было "Artist - Title", берём с карточки трека
        if (not artist) or (not title):
            artist = artist or p_artist
            title = title or p_title or list_text

        # Если разбор строки списка подозрительный — приоритет у карточки трека.
        if _should_prefer_page_pair(artist, title, p_artist, p_title):
            artist = p_artist
            title = p_title

        # Если в артист попал номер трека (например, "8"), берём пару со страницы
        if artist and artist.isdigit() and p_artist and p_title:
            artist = p_artist
            title = p_title

        title = _clean_title_tail(title)

        # Удаляем дублирование имени исполнителя в конце названия
        if title and artist and title.endswith(" " + artist):
            title = title[:-(len(artist) + 1)].strip()

        tracks.append({
            "id": track_id,
            "artist": artist,
            "title": title,
            "download_url": download_url,
            "referer": track_url,
            "src": track_url,
        })

        time.sleep(random.uniform(1.0, 2.0))

    logger.info(f"TUNES.KZ: {category_path} -> {len(tracks)} tracks")
    return tracks
