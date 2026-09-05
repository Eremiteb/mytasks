import json
import logging
import random
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")

# допускаем варианты:
# /song/slug/232692
# song/slug/232692
# https://lmu.kz/song/slug/232692
_SONG_RE = re.compile(r"(?:^|/)(song)/[^/]+/(\d+)/?$", re.IGNORECASE)


def _norm_href(href: str, base_url: str) -> str:
    """Нормализует href к пути вида /song/... или /..."""
    href = (href or "").strip()
    if not href:
        return ""
    # абсолютный URL -> берём path+query? (query не нужен)
    if href.startswith("http://") or href.startswith("https://"):
        try:
            u = urlparse(href)
            return u.path or ""
        except Exception:
            return href
    # относительный без ведущего /
    if not href.startswith("/"):
        href = "/" + href
    return href


def _pick_artist_from_row(a_title, session=None, base_url=None, href=None):
    """
    В списке категории могут быть разные форматы.
    Пробуем несколько способов найти артиста:
    1. Ближайший следующий <a> в том же контейнере (не title)
    2. Родительский контейнер - ищем другие <a> теги
    3. Если есть доступ к странице трека - парсим оттуда
    """
    title = (a_title.get_text(" ", strip=True) or "").strip()
    
    # Способ 1: ближайший следующий <a> в том же контейнере
    parent = a_title.parent
    if parent:
        for nxt in parent.find_all("a", href=True):
            if nxt == a_title:
                continue
            txt = (nxt.get_text(" ", strip=True) or "").strip()
            if txt and txt != title and len(txt) <= 120 and "скач" not in txt.lower() and "слуш" not in txt.lower():
                return txt
    
    # Способ 2: ищем в соседних элементах
    for nxt in a_title.find_all_next("a", href=True, limit=5):
        txt = (nxt.get_text(" ", strip=True) or "").strip()
        if txt and txt != title and len(txt) <= 120 and "скач" not in txt.lower() and "слуш" not in txt.lower():
            return txt
    
    # Способ 3: если есть доступ к странице трека, парсим оттуда
    if session and base_url and href:
        try:
            from urllib.parse import urljoin
            track_url = urljoin(base_url, href)
            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": base_url,
            }
            resp = session.get(track_url, timeout=15, headers=headers)
            if resp.status_code == 200:
                track_soup = BeautifulSoup(resp.text, "html.parser")
                # Ищем в og:title информацию
                og = track_soup.find("meta", attrs={"property": "og:title"})
                if og and og.get("content"):
                    c = og["content"]
                    # Формат может быть: "title - artist - ..." или "artist - title - ..."
                    parts = [p.strip(" \t\n\r-–—") for p in c.split("-")]
                    parts = [p for p in parts if p and "скач" not in p.lower() and "слуш" not in p.lower()]
                    if len(parts) >= 2:
                        # Ищем которая часть это артист (обычно вторая)
                        return parts[1]
        except Exception:
            pass
    
    return None


def _extract_title_artist_from_track_page(session, base_url, href, list_title=None):
    """
    Пытаемся аккуратно определить title/artist на странице трека.
    list_title помогает выбрать правильный порядок в og:title.
    """
    if not (session and base_url and href):
        return None, None

    try:
        track_url = urljoin(base_url, href)
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": base_url,
        }
        resp = session.get(track_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            return None, None
    except Exception:
        return None, None

    soup = BeautifulSoup(resp.text or "", "html.parser")

    title = None
    artist = None

    def _clean_text(txt: str | None) -> str | None:
        if not txt:
            return None
        return (txt or "").strip(" \t\n\r-–—") or None

    # JSON-LD
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = s.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            if not title:
                title = _clean_text(obj.get("name"))
            if not artist:
                by_artist = obj.get("byArtist") or obj.get("artist")
                if isinstance(by_artist, dict):
                    artist = _clean_text(by_artist.get("name"))
                elif isinstance(by_artist, list):
                    for a in by_artist:
                        if isinstance(a, dict) and a.get("name"):
                            artist = _clean_text(a.get("name"))
                            break
                        if isinstance(a, str):
                            artist = _clean_text(a)
                            break
                elif isinstance(by_artist, str):
                    artist = _clean_text(by_artist)

    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        c = og["content"]
        parts = [p.strip(" \t\n\r-–—") for p in c.split("-")]
        parts = [p for p in parts if p and "скач" not in p.lower() and "слуш" not in p.lower()]

        print(f"[DEBUG-LMU] og:title: {c}", file=__import__('sys').stderr)
        print(f"[DEBUG-LMU] parts: {parts}", file=__import__('sys').stderr)
        print(f"[DEBUG-LMU] list_title: {list_title}", file=__import__('sys').stderr)

        if list_title:
            lt = list_title.strip().lower()
            matched = None
            matched_idx = -1
            
            # Ищем совпадение list_title в parts
            for i, p in enumerate(parts):
                if p.lower() == lt or lt in p.lower() or p.lower() in lt:
                    matched = p
                    matched_idx = i
                    print(f"[DEBUG-LMU] Found match at idx {i}: {p}", file=__import__('sys').stderr)
                    break
            
            if matched:
                # Если найдён title в parts, остальное - artist
                # Обычно формат: "title - artist" или "artist - title"
                # Если matched (из list_title) на позиции 0, то это title, следующее - artist
                # Если matched на другой позиции, то последний элемент - artist
                if matched_idx == 0 and len(parts) > 1:
                    title = matched
                    artist = parts[-1]  # Берём последний элемент как артист
                    print(f"[DEBUG-LMU] Match at idx 0: title={title}, artist={artist}", file=__import__('sys').stderr)
                elif matched_idx > 0:
                    title = matched
                    # Артист - это первый элемент или элемент перед title
                    artist = parts[0]
                    print(f"[DEBUG-LMU] Match at idx {matched_idx}: title={title}, artist={artist}", file=__import__('sys').stderr)
                else:
                    title = matched

        if not title and len(parts) >= 2:
            # Фолбэк: предполагаем формат "title - artist".
            title = parts[0]
            artist = parts[1]
            print(f"[DEBUG-LMU] Fallback: title={title}, artist={artist}", file=__import__('sys').stderr)

    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True) or None

    if not artist:
        for prop in [
            "music:musician",
            "music:creator",
            "music:composer",
            "music:artist",
            "og:music:artist",
        ]:
            meta = soup.find("meta", attrs={"property": prop})
            if meta and meta.get("content"):
                artist = _clean_text(meta.get("content"))
                if artist:
                    break

    if not artist:
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            desc = og_desc["content"]
            if " - " in desc:
                artist = desc.split(" - ")[0].strip() or None

    if not artist:
        def _cls_has_artist(tag):
            cls = tag.get("class") or []
            return any(
                re.search(r"artist|исполн|singer|performer|author", c, re.IGNORECASE)
                for c in cls
            )

        el = soup.find(lambda t: t.name in ("a", "div", "span") and _cls_has_artist(t))
        if el:
            txt = el.get_text(" ", strip=True)
            if txt:
                artist = txt

    if not artist:
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if any(k in href for k in ["/artist/", "/singer/", "/performer/"]):
                txt = a.get_text(" ", strip=True)
                if txt:
                    artist = txt
                    break

    if list_title and " - " in list_title:
        left, right = [p.strip() for p in list_title.split(" - ", 1)]
        if artist:
            if artist.lower() == left.lower() and not title:
                title = right
            elif artist.lower() == right.lower() and not title:
                title = left
        elif not title:
            # Если нет явного артиста, сохраняем правую часть как title.
            title = right

    return title, artist


def get_tracks(session, base_url, category_path):
    """
    Драйвер для https://lmu.kz (категория /sections/kazakhstan).

    Парсим страницу категории и собираем ссылки на треки:
      /song/<slug>/<id>
    Ссылка скачивания формируется как:
      /download/<id>
    (на странице трека есть кнопка "Скачать").  # см. https://lmu.kz/song/turar-kobelek/232692
    """
    target_url = urljoin(base_url, category_path)
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": base_url,
    }

    try:
        resp = session.get(target_url, timeout=30, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"LMU.KZ: failed to fetch category {category_path}: {e}")
        return []

    html = resp.text or ""
    soup = BeautifulSoup(html, "html.parser")

    anchors = soup.find_all("a", href=True)
    if not anchors:
        logger.warning(f"LMU.KZ: no <a href> tags found at {category_path} (len(html)={len(html)})")
        return []

    seen_ids = set()
    tracks = []

    for a in anchors:
        raw_href = a.get("href", "")
        path = _norm_href(raw_href, base_url)
        m = _SONG_RE.search(path)
        if not m:
            continue
        t_id = m.group(2)
        if t_id in seen_ids:
            continue
        seen_ids.add(t_id)

        title = (a.get_text(" ", strip=True) or "").strip()
        artist = _pick_artist_from_row(a, session=session, base_url=base_url, href=raw_href)

        page_title, page_artist = _extract_title_artist_from_track_page(
            session, base_url, raw_href, list_title=title
        )
        if page_title:
            title = page_title
        if page_artist:
            artist = page_artist
        download_url = urljoin(base_url, f"/download/{t_id}")
        tracks.append({
            "id": t_id,
            "artist": artist,
            "title": title,
            "download_url": download_url,
            "referer": target_url,
        })

    # если вдруг структура изменилась — дадим полезную диагностику
    if not tracks:
        sample = []
        for a in anchors[:50]:
            sample.append(a.get("href", ""))
        logger.warning(
            f"LMU.KZ: 0 tracks on {category_path}. "
            f"Anchors={len(anchors)}. Sample hrefs: {sample[:15]}"
        )

    wait = random.uniform(2.0, 5.0)
    logger.info(f"LMU.KZ: {category_path} -> {len(tracks)} tracks. Pause {wait:.1f}s")
    time.sleep(wait)
    return tracks
