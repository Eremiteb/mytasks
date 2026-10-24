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


class FileResponse:
    """Готовый файл на диске в виде потокового HTTP-ответа для движка (`fetch()`).

    Поля `status_code`, `headers`, `iter_content` и контекстный менеджер — как у
    `requests`, поэтому лимиты каталогов, очистка тегов и `filehash` работают без
    изменений. `on_close` вызывается при закрытии (например, удалить временный каталог).
    """

    status_code = 200

    def __init__(self, path, on_close=None):
        self._path, self._on_close = path, on_close
        self.headers = {"content-length": str(os.path.getsize(path)), "content-type": "audio/mpeg"}

    def iter_content(self, chunk_size=131072):
        with open(self._path, "rb") as source:
            while chunk := source.read(chunk_size):
                yield chunk

    def close(self):
        if self._on_close is not None:
            self._on_close()
            self._on_close = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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


# Кириллица → латиница для сравнения без учёта письменности (русские и казахские
# буквы). Ключ грубый намеренно: сравниваются и исполнитель, и название сразу.
_CYR_TO_LAT = str.maketrans({
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "і": "i", "к": "k", "қ": "k", "л": "l", "м": "m",
    "н": "n", "ң": "n", "о": "o", "ө": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ұ": "u", "ү": "u", "ф": "f", "х": "h", "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})
# Латинские написания тех же звуков сводятся к одному: q/k, kh/h, j/zh, ı/i.
_LAT_FOLD = (("kh", "h"), ("q", "k"), ("j", "zh"), ("ı", "i"), ("w", "u"))


def fold_key(value):
    """Ключ мягкого прохода без латинской диакритики и без учёта письменности.

    «Мәди Сыздықов» и «Madi Syzdykov» → «madisyzdykov», «аi ómir-ai» → «aiomirai».
    Строгий проход использует `match_key` и этим ключом не ослабляется.
    """
    text = strip_latin_marks(unicodedata.normalize("NFKC", unescape(value or "")).casefold())
    text = text.translate(_CYR_TO_LAT)
    for old, new in _LAT_FOLD:
        text = text.replace(old, new)
    return match_key(strip_latin_marks(text))


# Латиница → кириллица «по-русски» для поисковых запросов (Beu FM пишет казахские
# песни латиницей, а сайты хранят их кириллицей). Точность не нужна: поиск
# сайтов нечёткий, а сверка идёт по `fold_key`.
_LAT_DIGRAPHS = (("shch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
                 ("ya", "я"), ("yu", "ю"), ("yo", "ё"))
_LAT_TO_CYR = str.maketrans({
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и",
    "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
    "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс", "y": "ы", "z": "з",
})
_VOWELS_CYR = "аеёиоуыэюя"


def to_cyrillic(text):
    """«Madi Syzdykov Ai Omir ai» → «Мади Сыздыков Ай Омир ай»; не-латиница не меняется."""
    words = []
    for word in strip_latin_marks(text or "").split(" "):
        lower = word.casefold()
        for old, new in _LAT_DIGRAPHS:
            lower = lower.replace(old, new)
        chars = list(lower.translate(_LAT_TO_CYR))
        # «и»/«ы» после гласной и не перед гласной — это «й»: Ai → Ай, Kairat → Кайрат.
        for index, char in enumerate(chars):
            following = chars[index + 1] if index + 1 < len(chars) else ""
            if char in "иы" and index and chars[index - 1] in _VOWELS_CYR and not (following and following in _VOWELS_CYR):
                chars[index] = "й"
        converted = "".join(chars)
        words.append(converted[:1].upper() + converted[1:] if word[:1].isupper() else converted)
    return " ".join(words)


def to_latin(text):
    """«Сара Амангелді Көздерің» → «Sara Amangeldi Kozderin»; латиница не меняется."""
    words = []
    for word in (text or "").split(" "):
        converted = word.casefold().translate(_CYR_TO_LAT)
        words.append(converted[:1].upper() + converted[1:] if word[:1].isupper() else converted)
    return " ".join(words)


def clean_title(title):
    """Название без `(feat. …)` и пометок ремастеринга."""
    return _REMASTER_RE.sub("", _FEAT_RE.sub("", title or "")).strip()


def artist_keys(artist, alias_groups=()):
    """Ключи исполнителей «A, B & C feat. D» → {a, b, c, d} плюс синонимы из `artist_aliases`."""
    keys = {key for part in _ARTIST_SPLIT_RE.split(artist or "") if (key := fold_key(part))}
    for group in alias_groups or ():
        group_keys = {fold_key(name) for name in group} - {""}
        if keys & group_keys:
            keys |= group_keys
    return keys


def artist_aliases(artist, alias_groups=()):
    """Синонимы исполнителя из конфига (без него самого), в порядке записи."""
    keys = artist_keys(artist)
    aliases = []
    for group in alias_groups or ():
        if keys & {fold_key(name) for name in group}:
            aliases += [name for name in group if fold_key(name) not in keys and name not in aliases]
    return aliases


_WITH_RE = re.compile(r"\s*[\(\[]with\s([^\)\]]*)[\)\]]", re.I)
_GUEST_RE = re.compile(r"[\(\[](?:feat\.?|ft\.?|featuring|with)\s([^\)\]]*)[\)\]]", re.I)


def _with_guests(title):
    """Гости из `(with X)`: это отдельная запись, их нельзя отбрасывать, как `(feat. X)`."""
    return set().union(*(artist_keys(guest) for guest in _WITH_RE.findall(title or "")))


def _people(artist, title, alias_groups=()):
    """Все участники записи: исполнители и гости из `(feat. …)` / `(with …)` в названии."""
    keys = artist_keys(artist, alias_groups)
    for guest in _GUEST_RE.findall(title or ""):
        keys |= artist_keys(guest)
    return keys


def relaxed_match(expected_artist, expected_title, artist, title, alias_groups=()):
    """Мягкое совпадение: то же название без feat/remaster/диакритики/различия письменности
    и хотя бы один общий исполнитель (с учётом синонимов `artist_aliases`).

    `(with X)` — другая запись, чем сольная: гость X должен быть у кандидата среди
    исполнителей или в названии. Так «Tame Impala, JENNIE — Dracula» совпадает с
    «Tame Impala — Dracula (with JENNIE)», а сольная «Tame Impala — Dracula» — нет
    (замер отпечатков 2026-10-24: доля различий 0.30 при 0.0 у одной записи).
    """
    expected_key = fold_key(_WITH_RE.sub("", clean_title(expected_title)))
    if not expected_key or fold_key(_WITH_RE.sub("", clean_title(title))) != expected_key:
        return False
    expected_people = _people(expected_artist, expected_title, alias_groups)
    candidate_people = _people(artist, title)
    return (
        bool(artist_keys(expected_artist, alias_groups) & artist_keys(artist))
        and _with_guests(expected_title) <= candidate_people
        and _with_guests(title) <= expected_people
    )


def search_queries(artist, title, alias_groups=(), translit=False):
    """Полный запрос, запасной «первый исполнитель + чистое название», вариант без диакритики,
    при `translit` — в другой письменности (латиница ↔ кириллица), и по запросу на каждый синоним."""
    queries = [f"{artist} {title}"]
    first_artist = _ARTIST_SPLIT_RE.split(artist)[0].strip()
    fallback = f"{first_artist} {clean_title(title)}"
    if first_artist and fallback != queries[0]:
        queries.append(fallback)
    # Поиск сайтов часто не сопоставляет «Ÿ»/«À» с «Y»/«A»: добавляем вариант без диакритики.
    plain = strip_latin_marks(queries[-1])
    extra = [plain]
    if translit:
        # Казахские песни: латиница ↔ кириллица (Beu FM пишет то так, то так, сайты — по-своему).
        extra.append(to_cyrillic(plain) if plain.isascii() else to_latin(plain))
    extra += [f"{alias} {clean_title(title)}" for alias in artist_aliases(artist, alias_groups)]
    for query in extra:
        if query not in queries:
            queries.append(query)
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
    alias_groups = track.get("_artist_aliases") or ()
    queries = search_queries(artist, title, alias_groups, bool(track.get("search_translit")))
    relaxed = []
    for source in enabled_sources(sources if sources is not None else track.get("_search_sources")):
        name = source["name"]
        try:
            for query in queries:
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
                    elif relaxed_match(artist, title, candidate.get("artist"), candidate.get("title"), alias_groups):
                        relaxed.append((name, found))
        except Exception as error:
            log.warning(f"{label}: поиск {artist} — {title} в {name} не удался: {error}")
    for name, found in relaxed:
        try:
            if _reachable(session, found, label, name, log):
                log.info(
                    f"{label}: {artist} — {title}: мягкое совпадение в {name} "
                    f"как {found.get('artist')} — {found.get('title')}"
                )
                return _result(found)
        except Exception as error:
            log.warning(f"{label}: проверка ссылки {name} не удалась: {error}")
    log.warning(f"{label}: {artist} — {title}: совпадение с доступной ссылкой не найдено")
    return None
