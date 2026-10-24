"""Общий поиск MP3 по «исполнитель — название» для драйверов без собственного аудио.

Список источников задаётся в корне `music_downloader.json` ключом `search_sources`
и передаётся движком в `track["_search_sources"]`. Один список обслуживает Shazam,
MyChords и Beu FM; новый источник добавляется записью в конфиг, если его драйвер
предоставляет `search_tracks(session, base_url, search_path)`.
"""
import importlib.util
import logging
import os
import re
import time
import unicodedata
from html import unescape
from urllib.parse import quote, quote_plus, urljoin, urlsplit

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


# Пометки, не меняющие запись: участники `(feat. X)` / `[ft. X]` и ремастеринг.
# `(with X)`, remix, live, версии и миксы остаются различием (другая запись).
_FEAT_RE = re.compile(r"\s*[\(\[](?:feat\.?|ft\.?|featuring)\s[^\)\]]*[\)\]]", re.I)
_REMASTER_RE = re.compile(
    r"\s*[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]|\s+-\s+(?:\d{4}\s+-?\s*)?remaster(?:ed)?(?:\s+\d{4})?$", re.I
)
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\band\b|\bwith\b)\s*", re.I)


def strip_latin_marks(text):
    """Снимает диакритику только с латиницы: «JAŸ-Z» → «JAY-Z», а кириллические «й», «ё» не трогает."""
    result = []
    for char in unicodedata.normalize("NFKD", text or ""):
        if unicodedata.combining(char) and result and result[-1].isascii():
            continue
        result.append(char)
    return unicodedata.normalize("NFC", "".join(result))


def fold_key(value):
    """`match_key` без латинской диакритики (только для мягкого прохода)."""
    return match_key(strip_latin_marks(unescape(value or "")))


def clean_title(title):
    """Название без `(feat. …)` и пометок ремастеринга."""
    return _REMASTER_RE.sub("", _FEAT_RE.sub("", title or "")).strip()


def artist_keys(artist):
    """Ключи отдельных исполнителей: «A, B & C feat. D» → {a, b, c, d}."""
    return {key for part in _ARTIST_SPLIT_RE.split(artist or "") if (key := fold_key(part))}


def relaxed_match(expected_artist, expected_title, artist, title):
    """Мягкое совпадение: то же название без feat/remaster/диакритики и хотя бы один общий исполнитель."""
    expected_key = fold_key(clean_title(expected_title))
    return (
        bool(expected_key)
        and fold_key(clean_title(title)) == expected_key
        and bool(artist_keys(expected_artist) & artist_keys(artist))
    )


def search_queries(artist, title):
    """Полный запрос и запасной «первый исполнитель + чистое название»."""
    queries = [f"{artist} {title}"]
    first_artist = _ARTIST_SPLIT_RE.split(artist)[0].strip()
    fallback = f"{first_artist} {clean_title(title)}"
    if first_artist and fallback != queries[0]:
        queries.append(fallback)
    # Поиск сайтов часто не сопоставляет «Ÿ»/«À» с «Y»/«A»: добавляем вариант без диакритики.
    plain = strip_latin_marks(queries[-1])
    if plain not in queries:
        queries.append(plain)
    return queries


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
    # {query} — для параметра (`q=Кино+Цой`), {query_path} — для сегмента пути (`/search/Кино%20Цой`).
    search_path = source["search_path"].format(query=quote_plus(query), query_path=quote(query, safe=""))
    return (
        driver.search_tracks(session, source["base_url"], search_path),
        urljoin(source["base_url"], search_path),
    )


def _reachable(session, candidate, label, name, log):
    """Проверяет ссылку запросом без чтения тела; у кандидата с `fetch` её уже проверил драйвер."""
    if candidate.get("fetch"):
        return True
    with session.get(
        candidate["download_url"], headers={"Referer": candidate["referer"]}, stream=True, timeout=(5, 15)
    ) as response:
        if response.status_code == 200:
            return True
    log.warning(f"{label}: {name}, ссылка недоступна (HTTP {response.status_code})")
    return False


def _result(candidate):
    result = {
        "download_url": candidate["download_url"],
        "referer": candidate["referer"],
        "src": candidate.get("src") or candidate["search_url"],
    }
    if candidate.get("fetch"):
        # Драйвер сам скачивает файл (yt-dlp): движок вызовет fetch() вместо HTTP-запроса.
        result["fetch"] = candidate["fetch"]
    return result


def find_audio(session, track, label, log=logger, sources=None):
    """Ищет трек в источниках из конфига и возвращает `download_url/referer/src` либо `None`.

    Сначала во всех источниках по порядку ищется точное совпадение `match_key`
    исполнителя и названия (полным запросом, затем «первый исполнитель + название
    без feat»). Если его нет нигде, берётся первое мягкое совпадение
    (`relaxed_match`: без `(feat. …)` и пометок remaster, общий исполнитель).
    Превью не используются; ссылка проверяется запросом без чтения тела.
    """
    artist, title = track.get("artist"), track.get("title")
    expected = (match_key(artist), match_key(title))
    if not all(expected):
        log.warning(f"{label}: нельзя искать трек без исполнителя или названия")
        return None
    relaxed = []
    for source in enabled_sources(sources if sources is not None else track.get("_search_sources")):
        name = source["name"]
        try:
            for query in search_queries(artist, title):
                candidates, search_url = _query_source(session, source, query)
                time.sleep(1)
                for candidate in candidates:
                    download_url = candidate.get("download_url") or ""
                    if urlsplit(download_url).scheme not in ("http", "https"):
                        continue
                    found = {**candidate, "referer": candidate.get("referer") or search_url, "search_url": search_url}
                    if (match_key(candidate.get("artist")), match_key(candidate.get("title"))) == expected:
                        if _reachable(session, found, label, name, log):
                            log.info(f"{label}: {artist} — {title}: найдено совпадение в {name}")
                            return _result(found)
                    elif relaxed_match(artist, title, candidate.get("artist"), candidate.get("title")):
                        relaxed.append((name, found))
        except Exception as error:
            log.warning(f"{label}: поиск {artist} — {title} в {name} не удался: {error}")
    for name, found in relaxed:
        try:
            if _reachable(session, found, label, name, log):
                log.info(
                    f"{label}: {artist} — {title}: найдено в {name} без пометок feat/remaster "
                    f"как {found.get('artist')} — {found.get('title')}"
                )
                return _result(found)
        except Exception as error:
            log.warning(f"{label}: проверка ссылки {name} не удалась: {error}")
    log.warning(f"{label}: {artist} — {title}: совпадение с доступной ссылкой не найдено")
    return None
