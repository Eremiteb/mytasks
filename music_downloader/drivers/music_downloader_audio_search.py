"""Общий поиск MP3 по «исполнитель — название» для драйверов без собственного аудио.

Список источников задаётся в корне `music_downloader.json` ключом `search_sources`
и передаётся движком в `track["_search_sources"]`. Один список обслуживает Shazam,
MyChords и Beu FM; новый источник добавляется записью в конфиг, если его драйвер
предоставляет `search_tracks(session, base_url, search_path)`.
"""
import importlib.util
import logging
import os
import time
import unicodedata
from html import unescape
from urllib.parse import quote_plus, urljoin, urlsplit

logger = logging.getLogger("Engine")

# Запасной список на случай, когда в конфиге нет `search_sources` (прежнее поведение).
DEFAULT_SOURCES = (
    {
        "name": "sefon.pro",
        "driver": "drivers/music_downloader_sefon_pro.py",
        "base_url": "https://sefon.pro",
        "search_path": "/search/?q={query}",
    },
    {
        "name": "lmusic_kz",
        "driver": "drivers/music_downloader_lmusic_kz.py",
        "base_url": "https://lmusic.kz",
        "search_path": "/search?q={query}",
    },
)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DRIVERS = {}


def match_key(value):
    """Игнорирует регистр и пунктуацию, но не удаляет слова remix, live и другие версии."""
    text = unicodedata.normalize("NFKC", unescape(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _load_driver(path):
    """Загружает драйвер источника по пути (каталог загрузчика не входит в sys.path) и кеширует его."""
    full_path = path if os.path.isabs(path) else os.path.join(_BASE_DIR, path)
    if full_path not in _DRIVERS:
        spec = importlib.util.spec_from_file_location(
            "audio_search_" + os.path.splitext(os.path.basename(full_path))[0], full_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Не удалось загрузить драйвер поиска {full_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DRIVERS[full_path] = module
    return _DRIVERS[full_path]


def enabled_sources(sources):
    """Включённые источники в порядке из конфига; None — запасной список."""
    if sources is None:
        sources = DEFAULT_SOURCES
    return [source for source in sources if source.get("enabled", True)]


def _query_source(session, source, query):
    """Возвращает кандидатов источника и адрес поисковой страницы."""
    driver = _load_driver(source["driver"])
    search_path = source["search_path"].format(query=quote_plus(query))
    return (
        driver.search_tracks(session, source["base_url"], search_path),
        urljoin(source["base_url"], search_path),
    )


def find_audio(session, track, label, log=logger, sources=None):
    """Ищет точное совпадение исполнителя и названия в источниках из конфига.

    Возвращает `download_url/referer/src` либо `None`. Превью и неточные совпадения
    не используются; ссылка проверяется запросом без чтения тела.
    """
    artist, title = track.get("artist"), track.get("title")
    expected = (match_key(artist), match_key(title))
    if not all(expected):
        log.warning(f"{label}: нельзя искать трек без исполнителя или названия")
        return None
    for source in enabled_sources(sources if sources is not None else track.get("_search_sources")):
        name = source["name"]
        try:
            candidates, search_url = _query_source(session, source, f"{artist} {title}")
            for candidate in candidates:
                actual = (match_key(candidate.get("artist")), match_key(candidate.get("title")))
                download_url = candidate.get("download_url") or ""
                if actual != expected or urlsplit(download_url).scheme not in ("http", "https"):
                    continue
                referer = candidate.get("referer") or search_url
                # Проверяем доступность ссылки, не скачивая тело повторно.
                with session.get(download_url, headers={"Referer": referer}, stream=True, timeout=(5, 15)) as response:
                    if response.status_code != 200:
                        log.warning(f"{label}: {name}, ссылка недоступна (HTTP {response.status_code})")
                        continue
                log.info(f"{label}: {artist} — {title}: найдено совпадение в {name}")
                return {
                    "download_url": download_url,
                    "referer": referer,
                    "src": candidate.get("src") or search_url,
                }
        except Exception as error:
            log.warning(f"{label}: поиск {artist} — {title} в {name} не удался: {error}")
        finally:
            time.sleep(1)
    log.warning(f"{label}: {artist} — {title}: совпадение с доступной ссылкой не найдено")
    return None
