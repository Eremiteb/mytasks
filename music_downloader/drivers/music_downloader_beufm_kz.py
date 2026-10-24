import hashlib
import importlib.util
import logging
import os
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

logger = logging.LoggerAdapter(logging.getLogger("Engine"), {"site": "beufm_kz"})


def _load_driver(name):
    """Загружает соседний драйвер по пути: движок не добавляет каталог загрузчика в sys.path."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить драйвер {name} из {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audio_search = _load_driver("music_downloader_audio_search")

# Имя трека на странице радио заполняет не сервер, а `template/Player.js`: раз в 10 с
# он читает `1.txt` и вставляет содержимое в `<span id="track_org">`. В исходном HTML
# спан пуст, поэтому драйвер сначала смотрит спан, а при пустом значении читает тот же
# `1.txt`, что и сам плеер. Формат строки — «Исполнитель - Название», кириллица
# приходит числовыми HTML-сущностями (`&#1057;`).
NOW_PLAYING_PATH = "1.txt"
NO_CACHE_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


def _site_root(base_url):
    """HTTPS-сертификат выдан только на `beufm.kz`: `https://www.beufm.kz` не проходит проверку."""
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if host == "www.beufm.kz":
        return parts._replace(netloc="beufm.kz").geturl()
    return base_url


def _match_key(value):
    """Игнорирует регистр и пунктуацию, но не удаляет слова remix, live и другие версии."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(char for char in text if char.isalnum())


def parse_now_playing(markup):
    """Возвращает `(исполнитель, название)` из HTML спана или текста `1.txt`; иначе `None`.

    Строка без разделителя « - » — не трек (джингл, реклама, название станции), её не берём.
    BeautifulSoup заодно раскрывает числовые сущности и убирает возможные теги.
    """
    soup = BeautifulSoup(markup or "", "html.parser")
    span = soup.select_one("#track_org")
    text = (span if span is not None else soup).get_text(" ", strip=True)
    artist, separator, title = text.partition(" - ")
    artist, title = artist.strip(), title.strip()
    if not separator or not artist or not title:
        return None
    return artist, title


def _track_id(artist, title):
    """Стабильный ID из нормализованной пары: у радио нет собственных идентификаторов треков."""
    digest = hashlib.sha256(f"{_match_key(artist)}|{_match_key(title)}".encode()).hexdigest()
    return f"fm_{digest[:16]}"


def get_tracks(session, base_url, category_path):
    """Возвращает текущий эфирный трек радио (0 или 1 запись); MP3 ищется позже в `resolve_track`."""
    root = _site_root(base_url)
    page_url = urljoin(root, category_path)
    try:
        page = session.get(page_url, headers=NO_CACHE_HEADERS, timeout=(5, 15))
        page.raise_for_status()
        parsed = parse_now_playing(page.text)
        source = page.url if isinstance(getattr(page, "url", None), str) else page_url
        if parsed is None:
            # Спан пуст — читаем источник, из которого его наполняет Player.js.
            now_url = urljoin(source, NOW_PLAYING_PATH)
            now = session.get(now_url, headers=NO_CACHE_HEADERS, timeout=(5, 15))
            now.raise_for_status()
            parsed = parse_now_playing(now.text)
            source = now_url
    except Exception as error:
        logger.error(f"Beu FM: не удалось прочитать название текущего трека: {error}")
        return []
    if parsed is None:
        logger.info("Beu FM: в эфире нет трека в формате «Исполнитель - Название» (джингл или реклама)")
        return []
    artist, title = parsed
    logger.info(f"Beu FM: в эфире {artist} - {title}")
    return [{"id": _track_id(artist, title), "artist": artist, "title": title, "src": page_url}]


def resolve_track(session, track):
    """Ищет проигрываемую песню в источниках `search_sources` из конфига (точное совпадение)."""
    return audio_search.find_audio(session, track, "Beu FM", logger)
