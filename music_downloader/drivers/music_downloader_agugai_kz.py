import logging
import random
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger("Engine")


_MP3_RE = re.compile(r'(https?://[^\s"\']+\.mp3|/uploads/[^\s"\']+\.mp3)', re.IGNORECASE)


def _pick_mp3_url(html: str, soup: BeautifulSoup, base_url: str):
    """
    Пытаемся достать прямую mp3-ссылку максимально "мягко":
    1) <a href="...mp3">
    2) <a>...скачать...</a>
    3) regex по всему html
    """
    # 1) прямые ссылки
    a = soup.select_one('a[href$=".mp3"], a[href*=".mp3?"]')
    if a and a.get("href"):
        return urljoin(base_url, a["href"])

    # 2) кнопка "скачать"
    for a in soup.find_all("a"):
        txt = (a.get_text(" ", strip=True) or "").lower()
        if "скач" in txt and a.get("href"):
            href = a["href"]
            if ".mp3" in href or "/uploads/" in href:
                return urljoin(base_url, href)

    # 3) regex
    m = _MP3_RE.search(html)
    if m:
        return urljoin(base_url, m.group(1))

    return None


def _extract_title_artist(soup: BeautifulSoup):
    """
    Извлекает название трека и имя артиста из HTML страницы agugai.kz.
    Формат возврата: (title, artist)
    """
    title = None
    artist = None
    
    # Список служебных слов, которые нужно игнорировать
    skip_words = {
        "поделиться", "скачать", "слушать", "онлайн", "бесплатно", 
        "download", "listen", "share", "free", "online"
    }

    # Приоритет 1: og:title обычно содержит "название - исполнитель - служебный текст"
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        c = og["content"]
        # Очищаем от служебного текста в конце
        clean = re.sub(r"\s*-\s*(слушать|скачать|бесплатно|онлайн).*$", "", c, flags=re.IGNORECASE)
        if " - " in clean:
            parts = [p.strip() for p in clean.split(" - ")]
            if len(parts) >= 2:
                # Первая часть - название трека, вторая - исполнитель
                title = parts[0]
                artist = parts[1]

    # Приоритет 2: H1 тег (может содержать только название)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True) or None

    # Приоритет 3: Ищем артиста в ссылках, если еще не найден
    if not artist and title:
        # Ищем ссылки на артиста (обычно это ссылки вида /artist/...)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/artist/" in href or "/исполнитель/" in href:
                txt = a.get_text(" ", strip=True)
                if txt and txt.lower() not in skip_words:
                    artist = txt
                    break
    
    # Приоритет 4: Поиск в og:description
    if not artist:
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            desc = og_desc["content"]
            if " - " in desc:
                first_part = desc.split(" - ")[0].strip()
                if (
                    first_part
                    and first_part.lower() not in skip_words
                    and (not title or first_part.lower() != title.lower())
                ):
                    artist = first_part

    # Приоритет 5: Ищем любую ссылку рядом с h1, но фильтруем служебные слова
    if not artist:
        h1 = soup.find("h1")
        if h1 and h1.parent:
            for elem in h1.parent.find_all("a", limit=20):
                txt = elem.get_text(" ", strip=True)
                txt_lower = txt.lower()
                # Фильтруем служебные слова и пустые строки
                if (
                    txt
                    and not txt.startswith("/")
                    and txt_lower not in skip_words
                    and (not title or txt_lower != title.lower())
                    and len(txt) < 100
                ):
                    artist = txt
                    break

    return title, artist


def _norm_track_id(track_url: str, mp3_url: str | None):
    """
    Делаем стабильный ID:
    - основной: slug из /music/<slug>
    - если не получилось — используем последний сегмент mp3
    """
    try:
        path = urlparse(track_url).path.rstrip("/")
        if path.startswith("/music/"):
            slug = path.split("/music/", 1)[1]
            if slug:
                return slug
    except Exception:
        pass

    if mp3_url:
        try:
            p = urlparse(mp3_url).path.rstrip("/")
            last = p.split("/")[-1]
            if last:
                return last
        except Exception:
            pass

    return re.sub(r"[^a-zA-Z0-9_-]+", "_", track_url)[:128]


def get_tracks(session, base_url, category_path):
    """
    category_path ожидается как '/catalog/music' или '/catalog/music?page=2'
    Возвращает список dict:
      {id, artist, title, download_url, referer}
    """
    target_url = urljoin(base_url, category_path)

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
        "Referer": base_url,
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
    }

    try:
        resp = session.get(target_url, timeout=25, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"AGUGAI: не удалось открыть категорию {category_path}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # В списке мы ищем ссылки вида /music/<slug>
    track_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/music/"):
            full = urljoin(base_url, href)
            track_links.append(full)

    # Уникализация, сохраняя порядок
    seen = set()
    uniq_links = []
    for u in track_links:
        if u not in seen:
            seen.add(u)
            uniq_links.append(u)

    tracks = []
    for _i, track_url in enumerate(uniq_links, start=1):
        # маленькая рандом-пауза между карточками
        time.sleep(random.uniform(0.5, 1.3))

        try:
            r = session.get(track_url, timeout=25, headers={**headers, "Referer": target_url})
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"AGUGAI: пропуск трека {track_url}: {e}")
            continue

        tsoup = BeautifulSoup(r.text, "html.parser")
        mp3_url = _pick_mp3_url(r.text, tsoup, base_url)
        if not mp3_url:
            logger.warning(f"AGUGAI: не нашёл mp3 ссылку: {track_url}")
            continue

        title, artist = _extract_title_artist(tsoup)
        t_id = _norm_track_id(track_url, mp3_url)

        tracks.append({
            "id": t_id,
            "artist": artist,
            "title": title,
            "download_url": mp3_url,
            "referer": track_url,
        })

    # финальная пауза (как в других драйверах)
    wait_s = 4 + random.uniform(1.0, 3.0)
    logger.info(f"AGUGAI: категория {category_path} обработана. Пауза {wait_s:.1f} сек.")
    time.sleep(wait_s)

    return tracks
