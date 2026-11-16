#!/usr/bin/env python3
"""Лёгкая домашняя библиотека: каталог книг в SQLite (FTS5) и веб-интерфейс в браузере.

Только стандартная библиотека Python, без зависимостей. Режимы:
  scan            — проиндексировать каталог книг (инкрементально по размеру и mtime);
  serve           — запустить веб-интерфейс (по умолчанию);
  fix-encoding    — вылечить кодировку: fb2 с заявленной не UTF-8 при тексте в UTF-8, txt в cp1251 → UTF-8;
  unpack-fb2zip   — извлечь .fb2 из *.fb2.zip рядом с архивом;
  convert         — DOC/DOCX/RTF/ODT/MHT → EPUB (или FB2) через pandoc/LibreOffice;
  --migrate-db    — только миграции схемы БД;
  -n, --dry-run   — ничего не менять: показать план (для scan — что будет добавлено,
                    обновлено и удалено из каталога; для serve — адрес и пути).

Веб-интерфейс и scan файлы книг только читают. Меняют файлы только служебные режимы fix-encoding,
unpack-fb2zip и convert — с обязательным предварительным --dry-run и JSON-отчётом перед изменениями.
Статусы чтения, оценки, избранное, заметки, полки и позиция чтения хранятся в той же БД
по относительному пути книги и переживают пересканирование.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import email
import fcntl
import hmac
import html
import io
import json
import logging
import os
import posixpath
import re
import shutil
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from email import policy as email_policy
from email.utils import parseaddr
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

PROJECT_DIR = Path(__file__).resolve().parent
REPO_DIR = PROJECT_DIR.parent
SERVICE = "books_library"
CONFIG_PATH = PROJECT_DIR / "books_library.json"
LOCK_PATH = PROJECT_DIR / ".books_library.lock"
# Всё хозяйство подпроекта — в его каталоге: БД (db_path по умолчанию), блокировка и JSON-отчёты перед изменениями.
REPORTS_DIR = PROJECT_DIR / "reports"
# Кэш просмотра (восстановимый): страницы DjVu в PNG и EPUB, полученные Calibre из MOBI/AZW/DOC/RTF/CHM и т. п.
CACHE_DIR = PROJECT_DIR / "cache"

LOG = logging.getLogger(SERVICE)

DEFAULT_CONFIG: dict[str, Any] = {
    "books_dir": "/mnt/copy/Books",
    "db_path": "books_library.db",
    "host": "127.0.0.1",
    "port": 8090,
    "scan_workers": 8,
    "reader_max_mb": 64,
    "page_size": 50,
    "convert_timeout": 300,
    "cache_mb": 4096,
    # Карантин служебных режимов: вместо удаления файлы переносятся сюда (<причина>/<путь в каталоге книг>).
    "quarantine_dir": "/mnt/copy/Books-removed",
    "workers": 12,
    "auth": {"user": "", "password": "", "password_env": "BOOKS_LIBRARY_PASSWORD"},
}

# Каталоги, которые не обходятся: служебные Syncthing/ОС и ресурсы сохранённых HTML-страниц.
SKIP_DIRS = {".stfolder", ".stversions", ".sync", ".thumbnails", "graphify-out"}
HTML_SIDECAR_SUFFIXES = ("_files", ".files", " files")
BOOK_EXTENSIONS = {
    "azw", "azw3", "chm", "djvu", "doc", "docx", "epub", "fb2", "mobi", "odt", "pdf", "rtf", "txt", "zip",
}
# Заглушки вместо автора, названия или жанра: такие значения не заменяют данные из пути.
PLACEHOLDERS = {"неизвестный", "неизвестен", "неизвестный автор", "автор неизвестен", "unknown", "unknown author",
                "unrecognised", "unrecognized", "без названия", "untitled"}
# Сколько байт начала fb2 читается при сканировании: описание книги всегда в начале файла.
FB2_HEAD_BYTES = 512 * 1024
# Предел размера одного служебного файла внутри EPUB/ZIP (защита от zip-бомб).
ZIP_MEMBER_MAX = 32 * 1024 * 1024
IMAGE_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp",
}
# Разделы — папки верхнего уровня с «_» (нехудожественное, журналы, сборники, неразобранное);
# прочие папки верхнего уровня — авторы художественной литературы (раздел пустой).
SECTION_NAMES = {"documentation": "Документация", "1tmp": "Временное", "разобрать": "Разобрать"}
FICTION_SECTION = "Художественная литература"
STATUSES = {"": "—", "want": "Хочу прочитать", "reading": "Читаю", "done": "Прочитано", "dropped": "Брошено"}
SORTS = {
    "title": "b.title COLLATE NOCASE, b.id",
    "author": "b.authors COLLATE NOCASE, b.series, b.series_no, b.title COLLATE NOCASE",
    "series": "b.series COLLATE NOCASE, b.series_no, b.title COLLATE NOCASE",
    "added": "b.added DESC, b.id DESC",
    "path": "b.path COLLATE NOCASE",
    "year": "b.year DESC, b.title COLLATE NOCASE",
    "size": "b.size DESC",
}
# Наиболее частые жанры fb2; прочие коды показываются как есть.
GENRES = {
    "sf": "Фантастика", "sf_fantasy": "Фэнтези", "sf_action": "Боевая фантастика", "sf_heroic": "Героическая фантастика",
    "sf_space": "Космическая фантастика", "sf_social": "Социальная фантастика", "sf_history": "Альтернативная история",
    "sf_horror": "Ужасы", "sf_humor": "Юмористическая фантастика", "sf_detective": "Детективная фантастика",
    "sf_cyberpunk": "Киберпанк", "sf_postapocalyptic": "Постапокалипсис", "sf_epic": "Эпическая фантастика",
    "popadanec": "Попаданцы", "sf_litrpg": "ЛитРПГ", "litrpg": "ЛитРПГ", "sf_etc": "Фантастика: прочее",
    "detective": "Детектив", "det_classic": "Классический детектив", "det_police": "Полицейский детектив",
    "det_action": "Боевик", "det_irony": "Иронический детектив", "det_history": "Исторический детектив",
    "det_espionage": "Шпионский детектив", "det_crime": "Криминальный детектив", "thriller": "Триллер",
    "prose_classic": "Классическая проза", "prose_contemporary": "Современная проза", "prose_history": "Историческая проза",
    "prose_rus_classic": "Русская классика", "prose_su_classics": "Советская классика", "prose_military": "Военная проза",
    "love_contemporary": "Современный любовный роман", "love_history": "Исторический любовный роман",
    "love_sf": "Любовное фэнтези", "love_short": "Короткий любовный роман", "love": "Любовный роман",
    "adv_history": "Исторические приключения", "adventure": "Приключения", "adv_animal": "Природа и животные",
    "adv_geo": "Путешествия", "children": "Детское", "child_tale": "Сказки", "child_sf": "Детская фантастика",
    "poetry": "Поэзия", "dramaturgy": "Драматургия", "humor": "Юмор", "humor_prose": "Юмористическая проза",
    "sci_history": "История", "sci_psychology": "Психология", "sci_philosophy": "Философия", "sci_tech": "Техника",
    "comp_programming": "Программирование", "computers": "Компьютеры", "nonfiction": "Документальное",
    "nonf_biography": "Биографии", "nonf_publicism": "Публицистика", "religion": "Религия", "reference": "Справочники",
    "home": "Дом и семья", "home_health": "Здоровье", "home_cooking": "Кулинария", "antique": "Старинное",
}

# Схема БД. Меняется ТОЛЬКО новой миграцией в конце списка (PRAGMA user_version), не правкой прежних.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE books(
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        fmt TEXT NOT NULL,
        title TEXT NOT NULL,
        authors TEXT NOT NULL DEFAULT '',
        series TEXT NOT NULL DEFAULT '',
        series_no INTEGER,
        genres TEXT NOT NULL DEFAULT '',
        lang TEXT NOT NULL DEFAULT '',
        year TEXT NOT NULL DEFAULT '',
        annotation TEXT NOT NULL DEFAULT '',
        cover INTEGER NOT NULL DEFAULT 0,
        added REAL NOT NULL,
        section TEXT NOT NULL DEFAULT '',
        collection TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX books_series ON books(series) WHERE series <> '';
    CREATE INDEX books_section ON books(section, collection);
    CREATE INDEX books_added ON books(added);
    CREATE TABLE book_authors(
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        PRIMARY KEY(book_id, name)
    ) WITHOUT ROWID;
    CREATE INDEX book_authors_name ON book_authors(name);
    CREATE TABLE book_genres(
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        PRIMARY KEY(book_id, name)
    ) WITHOUT ROWID;
    CREATE INDEX book_genres_name ON book_genres(name);
    CREATE VIRTUAL TABLE books_fts USING fts5(
        doc, content='', contentless_delete=1, tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TABLE marks(
        path TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT '',
        rating INTEGER NOT NULL DEFAULT 0,
        favorite INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        progress REAL NOT NULL DEFAULT 0,
        opened REAL,
        updated REAL NOT NULL
    );
    CREATE INDEX marks_opened ON marks(opened) WHERE opened IS NOT NULL;
    CREATE TABLE shelves(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE shelf_items(
        shelf_id INTEGER NOT NULL REFERENCES shelves(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        PRIMARY KEY(shelf_id, path)
    ) WITHOUT ROWID;
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # v2: однокнижные zip прозрачны (формат — книги внутри): все zip перечитываются при следующем scan;
    # результаты режима dedup для вкладки «Дубликаты».
    """
    UPDATE books SET mtime=-1 WHERE lower(path) LIKE '%.zip';
    CREATE TABLE dup_groups(
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL,
        status TEXT NOT NULL,
        created REAL NOT NULL
    );
    CREATE TABLE dup_members(
        group_id INTEGER NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        fmt TEXT NOT NULL,
        size INTEGER NOT NULL,
        words INTEGER NOT NULL,
        coverage REAL,
        role TEXT NOT NULL,
        keeper TEXT NOT NULL DEFAULT '',
        quarantine TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(group_id, path)
    ) WITHOUT ROWID;
    CREATE INDEX dup_members_path ON dup_members(path);
    """,
]
DB_LATEST = len(MIGRATIONS)


class ConfigError(Exception):
    """Неверная или неполная конфигурация."""


class ConfirmRequiredError(Exception):
    """Боевой запуск без ссылки на успешный dry-run с теми же параметрами."""


class ScanBusyError(Exception):
    """Сканирование уже выполняется (CLI, таймер или кнопка в интерфейсе)."""


# ======================================================================
# Конфигурация и журнал
# ======================================================================

def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Читает JSON-конфиг поверх значений по умолчанию и проверяет типы и диапазоны."""
    cfg: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"не читается {path}: {error}") from error
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: ожидается JSON-объект")
        for key, value in data.items():
            if key.startswith("_"):
                continue
            if key not in cfg:
                raise ConfigError(f"{path}: неизвестный ключ {key!r}")
            if isinstance(cfg[key], dict):
                if not isinstance(value, dict):
                    raise ConfigError(f"{path}: {key} должен быть объектом")
                cfg[key].update(value)
            else:
                cfg[key] = value
    for key, low, high in (("port", 1, 65535), ("scan_workers", 1, 64), ("reader_max_mb", 1, 1024), ("page_size", 10, 500),
                            ("convert_timeout", 10, 3600), ("cache_mb", 100, 1_000_000), ("workers", 1, 64)):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or not low <= cfg[key] <= high:
            raise ConfigError(f"{key} должен быть целым от {low} до {high}")
    for key in ("books_dir", "db_path", "host", "quarantine_dir"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise ConfigError(f"{key} должен быть непустой строкой")
    db_path = Path(cfg["db_path"]).expanduser()
    cfg["db_path"] = str(db_path if db_path.is_absolute() else PROJECT_DIR / db_path)
    cfg["books_dir"] = str(Path(cfg["books_dir"]).expanduser())
    cfg["quarantine_dir"] = str(Path(cfg["quarantine_dir"]).expanduser())
    if Path(cfg["quarantine_dir"]).resolve().is_relative_to(Path(cfg["books_dir"]).resolve()):
        raise ConfigError("quarantine_dir не может быть внутри books_dir: карантин не индексируется")
    auth = cfg["auth"]
    if not auth.get("password") and auth.get("password_env"):
        auth["password"] = os.environ.get(str(auth["password_env"]), "")
    if auth.get("password") and not auth.get("user"):
        raise ConfigError("auth.password задан без auth.user")
    if cfg["host"] not in {"127.0.0.1", "::1", "localhost"} and not auth.get("password"):
        LOG.warning("Интерфейс открыт на %s без пароля: задайте auth.user и auth.password", cfg["host"])
    return cfg


class JsonlHandler(logging.Handler):
    """Пишет записи в logs/books_library-<время>.jsonl по общей схеме mytasks (ECS + прежние поля)."""

    def __init__(self, log_dir: Path, keep: int) -> None:
        super().__init__()
        log_dir.mkdir(parents=True, exist_ok=True)
        old = sorted(log_dir.glob(f"{SERVICE}-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[max(keep - 1, 0):]:
            stale.unlink(missing_ok=True)
        self.path = log_dir / f"{SERVICE}-{datetime.now():%Y-%m-%d-%H-%M-%S}.jsonl"
        # Файл создаётся при первой записи: проверки без событий (check_mytasks --status → --migrate-db --dry-run)
        # не плодят пустые журналы, которые ротация оставила бы вместо настоящих.
        self.stream: Any = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname.lower()
            event = getattr(record, "event", "message")
            message = record.getMessage()
            detail = str(getattr(record, "detail", ""))
            line = json.dumps({
                "@timestamp": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                "schema.version": "1.0", "log.level": level, "message": message, "event.action": event,
                "service.name": SERVICE, "script": f"{SERVICE}.py", "event": event, "level": level,
                "msg": message, "detail": detail,
            }, ensure_ascii=False) + "\n"
            if self.stream is None:
                self.stream = self.path.open("a", encoding="utf-8")
            self.stream.write(line)
            self.stream.flush()
        except (OSError, ValueError):
            self.handleError(record)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
        super().close()


def setup_logging() -> None:
    LOG.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(console)
    keep = 10
    template = REPO_DIR / "conf" / "log_template.conf"
    if template.is_file():
        match = re.search(r'^KEEP_LOGS="?(\d+)"?', template.read_text(encoding="utf-8"), re.M)
        keep = int(match.group(1)) if match else keep
    try:
        LOG.addHandler(JsonlHandler(Path(os.environ.get("MYTASKS_LOG_DIR") or REPO_DIR / "logs"), keep))
    except OSError as error:
        LOG.warning("JSONL-журнал недоступен: %s", error)


def say(message: str, event: str, level: int = logging.INFO, detail: str = "") -> None:
    LOG.log(level, message, extra={"event": event, "detail": detail})


# ======================================================================
# База данных
# ======================================================================

def connect(db_path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"{Path(db_path).as_uri()}?mode=ro", uri=True, timeout=10)
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def migrate_database(db_path: str, dry_run: bool = False, out: Callable[[str], None] = print) -> int:
    """Приводит БД к последней схеме; в dry-run только печатает план. Возвращает версию схемы."""
    exists = Path(db_path).is_file()
    if dry_run:
        version = 0
        if exists:
            with closing(connect(db_path, readonly=True)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < DB_LATEST:
            out(f"[dry-run] будет: {'обновить' if exists else 'создать'} БД {db_path} до схемы v{DB_LATEST}")
            say("план миграции", "dry_migrate", detail=f"v{version}->v{DB_LATEST}")
        else:
            out(f"БД {db_path}: уже v{version}, миграции не нужны")  # «уже vN» читает check_mytasks.sh
        return version
    with closing(connect(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > DB_LATEST:
            raise ConfigError(f"схема БД v{version} новее программы (v{DB_LATEST})")
        for number in range(version + 1, DB_LATEST + 1):
            conn.executescript(f"BEGIN;{MIGRATIONS[number - 1]};PRAGMA user_version={number};COMMIT;")
            say(f"БД {db_path}: применена миграция v{number}", "migrate", detail=str(number))
    os.chmod(db_path, 0o600)
    return DB_LATEST


@contextmanager
def scan_lock() -> Iterator[None]:
    """Один процесс сканирования за раз: CLI, таймер и кнопка «Пересканировать» пишут в одну БД."""
    with LOCK_PATH.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ScanBusyError("сканирование уже выполняется") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def fold(text: str) -> str:
    """Нормализация для поиска: регистр, ё→е."""
    return text.casefold().replace("ё", "е")


def fts_query(text: str) -> str:
    """Безопасный запрос FTS5: только словесные токены в кавычках с префиксным поиском (И)."""
    tokens = re.findall(r"\w+", fold(text))[:12]
    return " ".join(f'"{token}"*' for token in tokens)


# ======================================================================
# Метаданные книг
# ======================================================================

def decode_xml(raw: bytes) -> str:
    """Декодирует XML по объявлению encoding (по умолчанию UTF-8), битые байты заменяются."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    match = re.search(rb'encoding\s*=\s*["\']([\w.:-]+)["\']', raw[:200])
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "utf-8"
    # Частая ошибка конвертеров: объявлена windows-1251, а байты — UTF-8. Текст в cp1251 почти никогда
    # не бывает корректным UTF-8, поэтому корректный UTF-8 с не-ASCII байтами считаем UTF-8.
    if codecs.lookup(encoding).name != "utf-8" and re.search(rb"[\x80-\xff]", raw):
        try:
            return raw.decode("utf-8").lstrip("﻿")
        except UnicodeDecodeError as error:
            if error.start >= len(raw) - 4:  # обрезанный посреди символа хвост прочитанного начала файла
                return raw[:error.start].decode("utf-8", errors="replace").lstrip("﻿")
    return raw.decode(encoding, errors="replace").lstrip("﻿")


def decode_text(raw: bytes) -> str:
    """Текст TXT: UTF-8, иначе cp1251 (основная кодировка старых русских книг)."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def strip_tags(fragment: str) -> str:
    text = re.sub(r"</p>|<br\s*/?>|<empty-line\s*/>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def parse_attrs(tag: str) -> dict[str, str]:
    return {k.lower(): html.unescape(v1 or v2) for k, v1, v2 in re.findall(r'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag)}


def xml_tag(name: str, source: str) -> str:
    match = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", source, re.S)
    return strip_tags(match.group(1)) if match else ""


def fb2_meta(raw: bytes) -> dict[str, Any]:
    """Описание книги из title-info fb2 (регулярными выражениями: многие файлы — не валидный XML)."""
    text = decode_xml(raw)
    match = re.search(r"<(?:\w+:)?title-info\b.*?</(?:\w+:)?title-info>", text, re.S)
    if not match:
        return {}
    info = match.group(0)
    authors, nick_only = [], True
    for block in re.findall(r"<(?:\w+:)?author\b[^>]*>(.*?)</(?:\w+:)?author>", info, re.S):
        name = " ".join(part for part in (xml_tag(k, block) for k in ("last-name", "first-name", "middle-name")) if part)
        nick_only = nick_only and not name
        name = name or xml_tag("nickname", block)
        if name and name not in authors and fold(name) not in PLACEHOLDERS:
            authors.append(name)
    genres = list(dict.fromkeys(g for g in (strip_tags(x) for x in re.findall(r"<(?:\w+:)?genre\b[^>]*>(.*?)<", info, re.S))
                                if g and fold(g) not in PLACEHOLDERS))
    title = xml_tag("book-title", info)
    meta: dict[str, Any] = {
        "title": "" if fold(title) in PLACEHOLDERS else title, "authors": authors, "genres": genres, "nick_only": nick_only,
        "lang": xml_tag("lang", info), "annotation": xml_tag("annotation", info),
        "cover": bool(re.search(r"<(?:\w+:)?coverpage\b", info)),
    }
    seq = re.search(r"<(?:\w+:)?sequence\b([^>]*)>", info)
    if seq:
        attrs = parse_attrs(seq.group(1))
        meta["series"] = attrs.get("name", "").strip()
        number = re.match(r"\d+", attrs.get("number", "").strip())
        meta["series_no"] = int(number.group(0)) if number else None
    date = re.search(r"<(?:\w+:)?date\b([^>]*)>(.*?)<", info, re.S)
    if date:
        year = re.search(r"\b(1\d{3}|20\d{2})\b", parse_attrs(date.group(1)).get("value", "") + " " + date.group(2))
        meta["year"] = year.group(1) if year else ""
    return meta


def zip_read(archive: zipfile.ZipFile, name: str, limit: int = ZIP_MEMBER_MAX) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"{name}: {info.file_size} байт больше предела {limit}")
    with archive.open(info) as handle:
        return handle.read(limit + 1)[:limit]


def zip_fb2_name(archive: zipfile.ZipFile) -> str | None:
    names = [n for n in archive.namelist() if n.lower().endswith(".fb2")]
    return names[0] if len(names) == 1 else None


class Epub:
    """Минимальный разбор EPUB: OPF, оглавление (spine) и обложка."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.zip = archive
        container = zip_read(archive, "META-INF/container.xml").decode("utf-8", errors="replace")
        match = re.search(r'full-path\s*=\s*"([^"]+)"', container)
        if not match:
            raise ValueError("нет rootfile в container.xml")
        self.opf_path = unquote(match.group(1))
        self.opf = decode_xml(zip_read(archive, self.opf_path))
        self.base = posixpath.dirname(self.opf_path)
        self.items: dict[str, dict[str, str]] = {}
        for tag in re.findall(r"<(?:\w+:)?item\b([^>]*)>", self.opf):
            attrs = parse_attrs(tag)
            if "id" in attrs and "href" in attrs:
                attrs["path"] = self.resolve(self.base, attrs["href"])
                self.items[attrs["id"]] = attrs
        self.names = set(archive.namelist())

    @staticmethod
    def resolve(base: str, href: str) -> str:
        return posixpath.normpath(posixpath.join(base, unquote(href.split("#", 1)[0]))).lstrip("/")

    def spine(self) -> list[str]:
        paths = []
        for tag in re.findall(r"<(?:\w+:)?itemref\b([^>]*)>", self.opf):
            item = self.items.get(parse_attrs(tag).get("idref", ""))
            if item and item["path"] in self.names:
                paths.append(item["path"])
        return paths

    def cover_path(self) -> str | None:
        for item in self.items.values():
            if "cover-image" in item.get("properties", ""):
                return item["path"]
        for tag in re.findall(r"<(?:\w+:)?meta\b([^>]*)>", self.opf):
            attrs = parse_attrs(tag)
            if attrs.get("name") == "cover" and attrs.get("content") in self.items:
                return self.items[attrs["content"]]["path"]
        for item in self.items.values():
            if "cover" in (item["id"] + item["href"]).lower() and item.get("media-type", "").startswith("image/"):
                return item["path"]
        return None

    def meta(self) -> dict[str, Any]:
        def dc(name: str) -> list[str]:
            return [strip_tags(x) for x in re.findall(rf"<dc:{name}\b[^>]*>(.*?)</dc:{name}>", self.opf, re.S)]

        def real(values: list[str]) -> list[str]:
            return list(dict.fromkeys(v for v in values if v and fold(v) not in PLACEHOLDERS))

        meta: dict[str, Any] = {
            "title": next(iter(real(dc("title"))), ""), "authors": real(dc("creator")), "genres": real(dc("subject")), "lang": next(iter(dc("language")), ""),
            "annotation": next(iter(dc("description")), ""), "cover": self.cover_path() is not None,
        }
        year = re.search(r"\b(1\d{3}|20\d{2})\b", " ".join(dc("date")))
        meta["year"] = year.group(1) if year else ""
        for tag in re.findall(r"<(?:\w+:)?meta\b([^>]*)>", self.opf):
            attrs = parse_attrs(tag)
            if attrs.get("name") == "calibre:series":
                meta["series"] = attrs.get("content", "")
            elif attrs.get("name") == "calibre:series_index":
                number = re.match(r"\d+", attrs.get("content", ""))
                meta["series_no"] = int(number.group(0)) if number else None
        return meta


def split_name(rel: str) -> tuple[str, str]:
    """Имя без расширения и формат по имени файла: name.<ext>.zip — однокнижный архив формата ext."""
    name = PurePosixPath(rel).name
    stem, _, ext = name.rpartition(".")
    if ext.lower() == "zip" and stem:
        inner_stem, _, inner_ext = stem.rpartition(".")
        if inner_stem and inner_ext.lower() in INNER_EXTENSIONS:
            return inner_stem, inner_ext.lower()
    return (stem or name), ext.lower()


# Форматы книги внутри однокнижного zip (zip в zip не бывает книгой).
INNER_EXTENSIONS = BOOK_EXTENSIONS - {"zip"}
# Наибольшая допустимая степень сжатия записи zip: выше — признак zip-бомбы (текст fb2/txt сжимается в 3–15 раз).
ZIP_MAX_RATIO = 200


def single_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    """Единственная запись-книга однокнижного zip или None (многофайловый архив, вложенный zip, странное имя)."""
    files = [i for i in archive.infolist() if not i.is_dir()]
    if len(files) != 1:
        return None
    name = PurePosixPath(files[0].filename.replace("\\", "/")).name
    ext = name.rpartition(".")[2].lower()
    if not name or name.startswith(".") or "." not in name or ext not in INNER_EXTENSIONS:
        return None
    return files[0]


def member_ext(info: zipfile.ZipInfo) -> str:
    return info.filename.rpartition(".")[2].lower()


def read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Запись zip целиком с защитой от zip-бомб: заявленный размер, степень сжатия и фактически прочитанное."""
    if info.file_size > limit:
        raise ValueError(f"{info.filename}: {info.file_size} байт больше предела {limit}")
    if info.file_size > 1024 * 1024 and info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
        raise ValueError(f"{info.filename}: подозрительная степень сжатия (zip-бомба?)")
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{info.filename}: фактический размер больше предела {limit}")
    return data


def is_wrapped(rel: str, fmt: str) -> bool:
    """Однокнижный zip: файл .zip, а формат книги — внутренний (fb2, pdf…); fb2.zip — запись прежней схемы до scan."""
    return rel.lower().endswith(".zip") and fmt not in ("zip", "fb2.zip")


def display_path(rel: str, fmt: str) -> str:
    """Путь для интерфейса: однокнижный zip показывается как name.ext (zip не упоминается)."""
    if not is_wrapped(rel, fmt):
        return rel
    stem = rel[:-4]
    return stem if stem.lower().endswith(f".{fmt}") else f"{stem}.{fmt}"


def section_name(folder: str) -> str:
    name = folder.lstrip("_").strip()
    return SECTION_NAMES.get(name, name[:1].upper() + name[1:])


def path_meta(rel: str) -> dict[str, Any]:
    """Метаданные по пути.

    Папка автора («Фамилия Имя/книга») даёт автора, а «Фамилия - Название» в имени файла — название.
    В разделах (папки «_…») раздел — сама папка, коллекция — её подпапка, автор и название берутся
    из имени файла «Автор - Название» (так названы книги сборников и Trauma), если оно так устроено.
    """
    parts = PurePosixPath(rel).parts
    stem, fmt = split_name(rel)
    title = stem.replace("_", " ").strip() or stem
    meta: dict[str, Any] = {"title": title, "authors": [], "fmt": fmt, "section": "", "collection": ""}
    left, sep, right = title.partition(" - ")
    named = bool(sep and right.strip() and left.strip() and not re.search(r"\d", left) and len(left.split()) <= 4)
    if len(parts) > 1 and parts[0].startswith("_"):
        meta["section"] = section_name(parts[0])
        meta["collection"] = parts[1].lstrip("_").strip() if len(parts) > 2 else ""
        if named:
            meta.update(title=right.strip(), authors=[a.strip() for a in left.split(",") if a.strip()])
    elif len(parts) > 1 and not parts[0].startswith("."):
        meta["authors"] = [parts[0]]
        if named and fold(left.split()[0]) in fold(parts[0]).split():
            meta["title"] = right.strip()
    return meta


def read_book_meta(root: Path, rel: str) -> dict[str, Any]:
    """Метаданные одной книги; при любой ошибке разбора — данные по пути (книга остаётся в каталоге)."""
    meta = path_meta(rel)
    full = root / rel
    try:
        found: dict[str, Any] = {}
        if meta["fmt"] == "fb2" and not rel.lower().endswith(".zip"):
            with full.open("rb") as handle:
                found = fb2_meta(handle.read(FB2_HEAD_BYTES))
        elif meta["fmt"] == "epub" and not rel.lower().endswith(".zip"):
            with zipfile.ZipFile(full) as archive:
                found = Epub(archive).meta()
        elif rel.lower().endswith(".zip"):
            with zipfile.ZipFile(full) as archive:
                inner = single_member(archive)
                meta["fmt"] = member_ext(inner) if inner else "zip"
                if inner and meta["fmt"] == "fb2":
                    if inner.file_size > ZIP_MAX_RATIO * max(inner.compress_size, 1) and inner.file_size > 1024 * 1024:
                        raise ValueError("подозрительная степень сжатия")
                    with archive.open(inner) as handle:
                        found = fb2_meta(handle.read(FB2_HEAD_BYTES))
                elif inner and meta["fmt"] == "epub":
                    data = read_member(archive, inner, 256 * 1024 * 1024)
                    with zipfile.ZipFile(io.BytesIO(data)) as nested:
                        found = Epub(nested).meta()
        # Автор-ник (как у fb2 после pandoc-конвертации) не заменяет автора из имени папки.
        if found.pop("nick_only", False) and meta["authors"]:
            found.pop("authors", None)
        for key, value in found.items():
            if value not in ("", [], None):
                meta[key] = value
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
        meta["error"] = f"{type(error).__name__}: {error}"
    return meta


def iter_book_files(root: Path, errors: list[str]) -> Iterator[tuple[str, int, float]]:
    """Относительный путь, размер и mtime каждого файла книги; HTML и служебные каталоги пропускаются."""
    def onerror(error: OSError) -> None:
        errors.append(f"{error.filename}: {error.strerror}")

    for current, dirs, files in os.walk(root, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not d.endswith(HTML_SIDECAR_SUFFIXES))
        for name in sorted(files):
            if name.rpartition(".")[2].lower() not in BOOK_EXTENSIONS or name.startswith("."):
                continue
            full = os.path.join(current, name)
            try:
                stat = os.stat(full)
            except OSError as error:
                errors.append(f"{full}: {error.strerror}")
                continue
            yield os.path.relpath(full, root).replace(os.sep, "/"), stat.st_size, stat.st_mtime


# ======================================================================
# Сканирование
# ======================================================================

SCAN_STATE: dict[str, Any] = {"running": False, "done": 0, "total": 0, "stage": "", "result": None, "error": ""}


def upsert_book(conn: sqlite3.Connection, rel: str, size: int, mtime: float, meta: dict[str, Any]) -> None:
    authors, genres = meta.get("authors", []), meta.get("genres", [])
    values = (
        size, mtime, meta["fmt"], meta["title"][:1000], "; ".join(authors)[:2000], str(meta.get("series", ""))[:500],
        meta.get("series_no"), "; ".join(genres)[:1000], str(meta.get("lang", ""))[:20], str(meta.get("year", ""))[:4],
        str(meta.get("annotation", ""))[:20000], int(bool(meta.get("cover"))), meta.get("section", ""),
        meta.get("collection", "")[:300],
    )
    row = conn.execute("SELECT id FROM books WHERE path=?", (rel,)).fetchone()
    if row:
        book_id = row[0]
        conn.execute(
            "UPDATE books SET size=?, mtime=?, fmt=?, title=?, authors=?, series=?, series_no=?, genres=?, lang=?,"
            " year=?, annotation=?, cover=?, section=?, collection=? WHERE id=?", (*values, book_id))
        conn.execute("DELETE FROM book_authors WHERE book_id=?", (book_id,))
        conn.execute("DELETE FROM book_genres WHERE book_id=?", (book_id,))
        conn.execute("DELETE FROM books_fts WHERE rowid=?", (book_id,))
    else:
        book_id = conn.execute(
            "INSERT INTO books(size, mtime, fmt, title, authors, series, series_no, genres, lang, year, annotation, cover,"
            " section, collection, path, added) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, rel, time.time())).lastrowid
    conn.executemany("INSERT OR IGNORE INTO book_authors VALUES (?,?)", [(book_id, a[:300]) for a in authors])
    conn.executemany("INSERT OR IGNORE INTO book_genres VALUES (?,?)", [(book_id, g[:100]) for g in genres])
    doc = " ".join([meta["title"], *authors, str(meta.get("series", "")), *genres, *(GENRES.get(g, "") for g in genres),
                    meta.get("section", ""), meta.get("collection", ""), split_name(rel)[0], str(meta.get("annotation", ""))[:4000]])
    conn.execute("INSERT INTO books_fts(rowid, doc) VALUES (?,?)", (book_id, fold(doc)))


def scan(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, int]:
    """Инкрементальное сканирование: новые и изменённые файлы разбираются, исчезнувшие удаляются из каталога."""
    root = Path(cfg["books_dir"])
    if not root.is_dir():
        raise ConfigError(f"каталог книг недоступен: {root}")
    db_path = cfg["db_path"]
    known: dict[str, tuple[int, float]] = {}
    if Path(db_path).is_file():
        with closing(connect(db_path, readonly=dry_run)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
                known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, size, mtime FROM books")}
    SCAN_STATE.update(stage="обход каталога", done=0, total=0)
    errors: list[str] = []
    todo: list[tuple[str, int, float]] = []
    seen: set[str] = set()
    new = 0
    for rel, size, mtime in iter_book_files(root, errors):
        seen.add(rel)
        old = known.get(rel)
        if old is None or old[0] != size or old[1] != mtime:
            todo.append((rel, size, mtime))
            new += old is None
    removed = [p for p in known if p not in seen]
    stats = {"files": len(seen), "new": new, "changed": len(todo) - new, "removed": len(removed), "walk_errors": len(errors)}
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    if dry_run:
        out(f"[dry-run] будет: каталог {root} — книг {stats['files']}, добавить {stats['new']}, "
              f"обновить {stats['changed']}, удалить из каталога {stats['removed']} (файлы книг не трогаются)")
        if not Path(db_path).is_file():
            out(f"[dry-run] будет: создать БД {db_path} (схема v{DB_LATEST})")
        for rel, *_ in todo[:15]:
            out(f"[dry-run] будет: {'обновить' if rel in known else 'добавить'} {rel}")
        for rel in removed[:15]:
            out(f"[dry-run] будет: удалить из каталога {rel}")
        say("план сканирования", "dry_scan", detail=json.dumps(stats, ensure_ascii=False))
        return stats

    migrate_database(db_path, out=out)
    started = last_report = time.monotonic()
    parse_errors = 0
    SCAN_STATE.update(stage="разбор книг", total=len(todo))
    with closing(connect(db_path)) as conn:
        for start in range(0, len(removed), 500):
            chunk = removed[start:start + 500]
            marks = ",".join("?" * len(chunk))
            with conn:
                conn.execute(f"DELETE FROM books_fts WHERE rowid IN (SELECT id FROM books WHERE path IN ({marks}))", chunk)
                conn.execute(f"DELETE FROM books WHERE path IN ({marks})", chunk)
        with ThreadPoolExecutor(max_workers=cfg["scan_workers"]) as pool:
            for start in range(0, len(todo), 500):
                batch = todo[start:start + 500]
                metas = list(pool.map(lambda item: read_book_meta(root, item[0]), batch))
                with conn:
                    for (rel, size, mtime), meta in zip(batch, metas, strict=True):
                        if "error" in meta:
                            parse_errors += 1
                            if parse_errors <= 50:
                                say(f"метаданные по пути: {rel}", "parse_error", logging.WARNING, detail=meta["error"])
                        upsert_book(conn, rel, size, mtime, meta)
                done = start + len(batch)
                SCAN_STATE["done"] = done
                now = time.monotonic()
                if now - last_report >= 30 or done == len(todo):
                    rate = done / max(now - started, 1e-6)
                    eta = (len(todo) - done) / rate if rate else 0
                    say(f"разбор: {done}/{len(todo)} ({done * 100 // max(len(todo), 1)} %), {rate:.0f} книг/с, "
                        f"осталось ~{eta / 60:.1f} мин", "progress")
                    last_report = now
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (datetime.now().isoformat(timespec="seconds"),))
        conn.execute("PRAGMA optimize")
    stats["parse_errors"] = parse_errors
    say(f"сканирование завершено: {json.dumps(stats, ensure_ascii=False)}", "scan_done", detail=json.dumps(stats))
    return stats


DECL_RE = re.compile(rb'(<\?xml[^>]*?encoding\s*=\s*["\'])([\w.:-]+)(["\'])')


def wrong_declaration(raw: bytes) -> str | None:
    """Объявленная кодировка, если она не UTF-8, а байты файла — корректный UTF-8 с не-ASCII текстом."""
    match = DECL_RE.search(raw[:300])
    if not match:
        return None
    declared = match.group(2).decode("ascii")
    try:
        if codecs.lookup(declared).name == "utf-8":
            return None
    except LookupError:
        pass
    if not re.search(rb"[\x80-\xff]", raw):
        return None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return declared


def cp1251_text(raw: bytes) -> bool:
    """TXT в cp1251: не UTF-8, декодируется cp1251 без ошибок, не-ASCII символы — в основном кириллица."""
    if not re.search(rb"[\x80-\xff]", raw) or raw.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    try:
        raw.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("cp1251")
    except UnicodeDecodeError:
        return False
    if "\x00" in text:
        return False
    high = [ch for ch in text if ord(ch) > 127]
    cyrillic = sum(1 for ch in high if "\u0400" <= ch <= "\u04ff")
    return cyrillic >= 0.8 * len(high)


def target_dir(cfg: dict[str, Any], path: str | None) -> Path:
    """Каталог обработки: --path (абсолютный или относительно каталога книг), иначе каталог книг."""
    root = Path(cfg["books_dir"])
    target = root if not path else (Path(path).expanduser() if Path(path).expanduser().is_absolute() else root / path)
    if not target.is_dir():
        raise ConfigError(f"каталог недоступен: {target}")
    resolved = target.resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ConfigError(f"корневой или домашний каталог целиком не обрабатывается: {resolved}")
    return target


def walk_files(target: Path, suffixes: tuple[str, ...], errors: list[str]) -> Iterator[tuple[Path, int]]:
    """Файлы с нужными окончаниями имени (без учёта регистра), служебные каталоги пропускаются."""
    def onerror(error: OSError) -> None:
        errors.append(f"{error.filename}: {error.strerror}")

    for current, dirs, files in os.walk(target, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not d.endswith(HTML_SIDECAR_SUFFIXES))
        for name in sorted(files):
            if name.lower().endswith(suffixes) and not name.startswith("."):
                full = Path(current) / name
                try:
                    yield full, full.stat().st_size
                except OSError as error:
                    errors.append(f"{full}: {error.strerror}")


def write_report(kind: str, data: dict[str, Any]) -> Path:
    """JSON-отчёт перед изменением файлов библиотеки: books_library/reports/<вид>-<время>.json."""
    report_dir = REPORTS_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / f"{kind}-{datetime.now():%Y-%m-%d-%H-%M-%S}.json"
    report.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def atomic_write(full: Path, data: bytes, tag: str) -> None:
    """Запись через временный файл рядом и os.replace с сохранением прав исходного файла."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{full.name[:80]}.", suffix=f".{tag}.tmp", dir=full.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, full.stat().st_mode & 0o777)
        os.replace(tmp, full)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def report_progress(state: dict[str, float], done: int, found: int, what: str) -> None:
    now = time.monotonic()
    if now - state["last"] >= 30:
        say(f"{what}: проверено {done}, найдено {found}, {done / max(now - state['start'], 1e-6):.0f} файлов/с", "progress")
        state["last"] = now


# ----------------------------------------------------------------------
# Распаковка fb2.zip (замена unpack-fb2zip.sh)
# ----------------------------------------------------------------------

def unique_path(directory: Path, name: str, taken: set[Path]) -> Path:
    """Свободное имя в каталоге: name, затем name_1, name_2… (с учётом уже запланированных)."""
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, dot + ext) if stem else (name, "")
    candidate, number = directory / name, 1
    while candidate.exists() or candidate in taken:
        candidate = directory / f"{base}_{number}{suffix}"
        number += 1
    taken.add(candidate)
    return candidate


def unpack_fb2zip(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, max_mb: int = 200,
                  out: Callable[[str], None] = print) -> dict[str, int]:
    """Извлекает .fb2 из *.fb2.zip рядом с архивом (без внутренних путей), архив остаётся на месте.

    Конфликты имён — суффиксы _1, _2…; запись больше max_mb пропускается; CRC проверяет zipfile при чтении.
    """
    target = target_dir(cfg, path)
    limit = max_mb * 1024 * 1024
    errors: list[str] = []
    stats = {"archives": 0, "extracted": 0, "skipped": 0, "failed": 0}
    taken: set[Path] = set()
    for archive_path, _size in walk_files(target, (".fb2.zip",), errors):
        stats["archives"] += 1
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = [i for i in archive.infolist() if i.filename.lower().endswith(".fb2") and not i.is_dir()]
                if not entries:
                    say(f"в архиве нет .fb2: {archive_path}", "no_fb2_inside", detail=str(archive_path))
                for info in entries:
                    name = PurePosixPath(info.filename.replace("\\", "/")).name
                    if not name or name.startswith("."):
                        stats["skipped"] += 1
                        continue
                    if info.file_size > limit:
                        stats["skipped"] += 1
                        say(f"запись больше {max_mb} МБ пропущена: {archive_path}:{info.filename}", "entry_too_large",
                            logging.WARNING, detail=str(archive_path))
                        continue
                    target_file = unique_path(archive_path.parent, name, taken)
                    if dry_run:
                        out(f"[dry-run] будет: извлечь {archive_path} → {target_file}")
                        continue
                    with archive.open(info) as handle:
                        data = handle.read(limit + 1)
                    if not data or len(data) > limit:
                        raise ValueError(f"{info.filename}: пустая или слишком большая запись")
                    fd, tmp_name = tempfile.mkstemp(prefix=".unpack-fb2zip.", dir=archive_path.parent)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    os.chmod(tmp_name, archive_path.stat().st_mode & 0o666)
                    if target_file.exists():  # появился после планирования — новое свободное имя
                        target_file = unique_path(archive_path.parent, name, taken)
                    os.replace(tmp_name, target_file)
                    stats["extracted"] += 1
                    say(f"извлечён {target_file}", "extracted", detail=f"{archive_path} -> {target_file}")
        except (OSError, ValueError, zipfile.BadZipFile, EOFError) as error:
            stats["failed"] += 1
            say(f"ошибка архива {archive_path}: {error}", "extract_failed", logging.ERROR, detail=str(error))
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    if dry_run:
        out(f"[dry-run] будет: архивов {stats['archives']}, пропущено записей {stats['skipped']}, ошибок {stats['failed']}")
    say(f"распаковка: {json.dumps(stats, ensure_ascii=False)}", "dry_unpack_done" if dry_run else "unpack_done", detail=json.dumps(stats))
    return stats


# ----------------------------------------------------------------------
# Конвертация DOC/DOCX/RTF/ODT (замена doc2fb2.sh)
# ----------------------------------------------------------------------

CONVERT_SOURCES = ("doc", "docx", "rtf", "odt", "mht", "mhtml")


def decode_part(raw: bytes, declared: str | None) -> str:
    """Текст части MHT: заявленная кодировка и запасные, побеждает вариант с наименьшим числом замен."""
    best, best_bad = None, None
    for encoding in (declared, "utf-8", "cp1251", "koi8-r", "latin-1"):
        if not encoding:
            continue
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            continue
        bad = text.count("\ufffd")
        if best_bad is None or bad < best_bad:
            best, best_bad = text, bad
            if bad == 0:
                break
    return best if best is not None else raw.decode("utf-8", errors="replace")


def mht_html(path: Path, limit: int) -> tuple[str, str, str]:
    """HTML, название и автор из MHT/MHTML (замена mht_to_fb2.py): text/html, иначе text/plain в <pre>."""
    message = email.message_from_bytes(read_limited(path, limit), policy=email_policy.default)
    html_text = plain = None
    for part in message.walk() if message.is_multipart() else [message]:
        ctype = (part.get_content_type() or "").lower()
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if ctype == "text/html" and html_text is None:
            html_text = decode_part(payload, part.get_content_charset())
        elif ctype == "text/plain" and plain is None:
            plain = decode_part(payload, part.get_content_charset())
    if not html_text and plain:
        html_text = f"<pre>{html.escape(plain)}</pre>"
    if not html_text:
        raise ValueError("в MHT нет HTML")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    title = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", title_match.group(1))).split()) if title_match else ""
    name, address = parseaddr(str(message.get("From") or ""))
    return html_text, title or str(message.get("Subject") or "").strip(), (name or address).strip()


def run_tool(args: list[str], timeout: int, cwd: Path | None = None) -> tuple[bool, str]:
    """Запуск внешней программы без оболочки; возвращает успех и хвост stderr для журнала."""
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"тайм-аут {timeout} с: {args[0]}"
    except OSError as error:
        return False, f"{args[0]}: {error}"
    tail = (done.stderr or b"").decode("utf-8", errors="replace")[-400:]
    return done.returncode == 0, tail


def strip_foreign_images(node: Any, media: Path) -> None:
    """Убирает из pandoc-AST картинки, путь которых ведёт не в каталог извлечённых картинок (чужие файлы, URL)."""
    if isinstance(node, list):
        for item in node:
            strip_foreign_images(item, media)
    elif isinstance(node, dict):
        if node.get("t") == "Image" and isinstance(node.get("c"), list) and len(node["c"]) == 3:
            target = node["c"][2]
            url = str(target[0]) if isinstance(target, list) and target else ""
            path = Path(unquote(url))
            inside = not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) and path.is_absolute() and \
                path.resolve().is_relative_to(media.resolve())
            if not inside:
                node["t"], node["c"] = "Str", ""
                return
        for value in node.values():
            strip_foreign_images(value, media)


def docx_core(path: Path) -> tuple[str, str]:
    """Название и автор из docProps/core.xml документа DOCX."""
    try:
        with zipfile.ZipFile(path) as archive:
            core = zip_read(archive, "docProps/core.xml", 1024 * 1024).decode("utf-8", errors="replace")
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return "", ""
    return xml_tag("title", core), xml_tag("creator", core)


def verify_book(path: Path, fmt: str) -> str | None:
    """Проверка результата: EPUB — ZIP/CRC, mimetype, container.xml и текст глав; FB2 — FictionBook с текстом."""
    try:
        if fmt == "epub":
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    return "ошибка CRC"
                if zip_read(archive, "mimetype").strip() != b"application/epub+zip":
                    return "неверный mimetype"
                epub = Epub(archive)
                text = "".join(strip_tags(decode_xml(zip_read(archive, p))) for p in epub.spine())
        else:
            raw = path.read_bytes()
            if b"<FictionBook" not in raw[:4096]:
                return "нет FictionBook"
            body = decode_xml(raw)
            text = strip_tags(body[body.find("<body"):])
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, EOFError) as error:
        return f"{type(error).__name__}: {error}"
    return None if len(text.strip()) >= 50 else "почти нет текста"


def convert_one(src: Path, out: Path, fmt: str, cfg: dict[str, Any], options: dict[str, Any]) -> tuple[bool, str]:
    """Цепочка как в doc2fb2.sh: docx/odt → pandoc; doc/rtf → LibreOffice → docx → pandoc;
    запасные пути: rtf → pandoc напрямую → unrtf; doc → html/antiword/catdoc/wvText → текст → pandoc.
    mht/mhtml (как mht_to_fb2.py): HTML из MHT → pandoc; название — <title> или тема, автор — From."""
    timeout = cfg["convert_timeout"]
    ext = src.suffix.lower().lstrip(".")
    meta_title, meta_author = docx_core(src) if ext == "docx" else ("", "")
    mht_text = ""
    if ext in {"mht", "mhtml"}:
        mht_text, meta_title, meta_author = mht_html(src, cfg["reader_max_mb"] * 1024 * 1024)
    title = meta_title if options["title_mode"] == "auto" and meta_title else path_meta(src.name)["title"]
    author = options["author"] or meta_author or next(iter(path_meta(str(src.relative_to(Path(cfg["books_dir"])))
                                                                     if src.is_relative_to(Path(cfg["books_dir"])) else src.name)
                                                           ["authors"]), "")
    with tempfile.TemporaryDirectory(prefix="books_library-convert-") as tmp_name:
        tmp = Path(tmp_name)
        produced = tmp / f"book.{fmt}"

        def pandoc(source: Path, from_format: str = "") -> tuple[bool, str]:
            # Шаг 1: недоверенный документ разбирается в песочнице (без сети и чужих файлов) в JSON-AST,
            # картинки извлекаются во временный каталог. Шаг 2 пишет EPUB/FB2 из AST вне песочницы (писателю
            # EPUB нужны его собственные файлы данных), но картинки вне временного каталога из AST убираются.
            ast, media = tmp / "book.json", tmp / "media"
            args = ["pandoc", "--sandbox", f"--extract-media={media}", "-t", "json", "-o", str(ast), str(source)]
            if from_format:
                args[1:1] = ["-f", from_format]
            ok, err = run_tool(args, timeout, cwd=tmp)
            if not ok:
                return ok, err
            try:
                tree = json.loads(ast.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                return False, f"AST: {error}"
            strip_foreign_images(tree, media)
            ast.write_text(json.dumps(tree), encoding="utf-8")
            args = ["pandoc", "-f", "json", "-o", str(produced), f"--resource-path={tmp}", str(ast)]
            if options["title_mode"] != "keep":
                args += ["--metadata", f"title={title}"]
            if author:
                args += ["--metadata", f"author={author}"]
            if options["toc"]:
                args += ["--toc", f"--toc-depth={options['toc_depth']}"]
            return run_tool(args, timeout, cwd=tmp)

        staged = tmp / f"source.{ext}"  # копия с простым именем: внешние программы не видят странных имён
        attempts: list[Callable[[], tuple[bool, str]]] = []
        if ext in {"mht", "mhtml"}:
            staged = tmp / "source.html"
            staged.write_text(mht_text, encoding="utf-8")
            attempts.append(lambda: pandoc(staged, "html"))
        elif ext in {"docx", "odt"}:
            shutil.copyfile(src, staged)
            attempts.append(lambda: pandoc(staged))
        else:
            shutil.copyfile(src, staged)
            office = shutil.which("soffice") or shutil.which("libreoffice")

            def via_office() -> tuple[bool, str]:
                if not office:
                    return False, "нет LibreOffice"
                ok, err = run_tool([office, f"-env:UserInstallation={(tmp / 'lo').as_uri()}", "--headless", "--convert-to",
                                    "docx", "--outdir", str(tmp / "lo-out"), str(staged)], timeout)
                result = tmp / "lo-out" / "source.docx"
                return pandoc(result) if ok and result.is_file() else (False, err or "LibreOffice не создал docx")

            def via_text(tool: list[str], to_file: bool = False) -> tuple[bool, str]:
                if not shutil.which(tool[0]):
                    return False, f"нет {tool[0]}"
                text_path = tmp / "source.txt"
                if to_file:
                    ok, err = run_tool([*tool, str(staged), str(text_path)], timeout)
                else:
                    try:
                        done = subprocess.run([*tool, str(staged)], capture_output=True, timeout=timeout, check=False,
                                              stdin=subprocess.DEVNULL)
                    except (subprocess.TimeoutExpired, OSError) as error:
                        return False, str(error)
                    ok, err = done.returncode == 0, done.stderr.decode("utf-8", errors="replace")[-400:]
                    text_path.write_bytes(done.stdout)
                if not ok or not text_path.is_file() or text_path.stat().st_size == 0:
                    return False, err or f"{tool[0]}: пустой текст"
                text_path.write_text(decode_text(text_path.read_bytes()), encoding="utf-8")
                return pandoc(text_path, "plain")

            attempts.append(via_office)
            if ext == "rtf":
                attempts += [lambda: pandoc(staged, "rtf"), lambda: via_text(["unrtf", "--text", "--nopict", "--quiet"])]
            else:
                if staged.read_bytes()[:512].lstrip().lower().startswith((b"<html", b"<!doctype html")):
                    attempts.insert(0, lambda: pandoc(staged, "html"))
                attempts += [lambda: via_text(["antiword"]), lambda: via_text(["catdoc"]), lambda: via_text(["catdoc", "-b"]),
                             lambda: via_text(["wvText"], to_file=True)]
        errors = []
        for attempt in attempts:
            produced.unlink(missing_ok=True)
            ok, err = attempt()
            if ok and produced.is_file():
                problem = verify_book(produced, fmt)
                if problem is None:
                    fd, tmp_out = tempfile.mkstemp(prefix=".convert.", dir=out.parent)
                    os.close(fd)
                    shutil.copyfile(produced, tmp_out)
                    os.chmod(tmp_out, src.stat().st_mode & 0o666)
                    os.replace(tmp_out, out)
                    return True, ""
                err = f"проверка: {problem}"
            errors.append(err.strip().splitlines()[-1] if err.strip() else "ошибка")
        return False, "; ".join(errors)[-600:]


def convert_documents(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                      out: Callable[[str], None] = print, **options: Any) -> dict[str, int]:
    """Конвертирует DOC/DOCX/RTF/ODT в EPUB (по правилу библиотеки) или FB2 рядом с исходником.

    Существующий результат не перезаписывается без overwrite. Исходник удаляется только с delete_source и только
    после проверки результата (verify_book); перед удалением — JSON-отчёт books_library/reports/convert-<время>.json.
    """
    if not shutil.which("pandoc"):
        raise ConfigError("не найден pandoc (нужен для convert)")
    target = target_dir(cfg, path)
    fmt = options["to"]
    only = CONVERT_SOURCES if options["only"] == "all" else (options["only"],)
    excludes = [x.casefold() for x in options["exclude"]]
    errors: list[str] = []
    plan: list[tuple[Path, Path]] = []
    stats = {"sources": 0, "planned": 0, "skipped_exists": 0, "converted": 0, "failed": 0, "deleted_sources": 0}
    for src, _size in walk_files(target, tuple(f".{x}" for x in only), errors):
        stats["sources"] += 1
        if any(x in src.name.casefold() for x in excludes):
            continue
        result = src.with_suffix(f".{fmt}")
        if result.exists() and not (options["overwrite"] and not (options["skip_newer"] and result.stat().st_mtime >= src.stat().st_mtime)):
            stats["skipped_exists"] += 1
            continue
        plan.append((src, result))
    stats["planned"] = len(plan)
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    if dry_run:
        for src, result in plan[:30]:
            out(f"[dry-run] будет: {src} → {result}{' (исходник удалить после проверки)' if options['delete_source'] else ''}")
        out(f"[dry-run] будет: конвертировать {len(plan)} из {stats['sources']} документов в {fmt.upper()}, "
              f"уже есть {stats['skipped_exists']}")
        say("план конвертации", "dry_convert", detail=json.dumps(stats))
        return stats
    done_list: list[dict[str, Any]] = []
    started = last = time.monotonic()
    for number, (src, result) in enumerate(plan, 1):
        ok, err = convert_one(src, result, fmt, cfg, options)
        if ok:
            stats["converted"] += 1
            done_list.append({"source": str(src), "size": src.stat().st_size, "result": str(result), "result_size": result.stat().st_size})
            say(f"готово: {result}", "converted", detail=str(src))
        else:
            stats["failed"] += 1
            say(f"не конвертирован {src}: {err}", "convert_failed", logging.ERROR, detail=err)
        now = time.monotonic()
        if now - last >= 30 or number == len(plan):
            rate = number / max(now - started, 1e-6)
            say(f"конвертация: {number}/{len(plan)} ({number * 100 // len(plan)} %), осталось ~{(len(plan) - number) / rate / 60:.1f} мин",
                "progress")
            last = now
    if options["delete_source"] and done_list:
        report = write_report("convert", {"target": str(target), "deleted_sources": done_list,
                                          "reason": f"исходник заменён проверенным {fmt.upper()}"})
        for item in done_list:
            if verify_book(Path(item["result"]), fmt) is None:
                Path(item["source"]).unlink(missing_ok=True)
                stats["deleted_sources"] += 1
        say(f"удалено исходников: {stats['deleted_sources']}, отчёт {report}", "sources_deleted", detail=str(report))
    say(f"конвертация: {json.dumps(stats, ensure_ascii=False)}", "convert_done", detail=json.dumps(stats))
    return stats


# ======================================================================
# Ночные режимы: карантин, кодировки, fb2 → epub, дубликаты, упаковка книг в zip
# ======================================================================

# Причины карантина: <quarantine_dir>/<причина>/<путь в каталоге книг>. Карантин не индексируется.
QUARANTINE_REASONS = ("replaced", "duplicate", "edited", "rejected-convert", "rejected-zip", "rejected-encoding",
                      "rejected-name")
# Уже сжатые форматы кладутся в zip без сжатия: deflate их не уменьшит, только потратит процессор.
STORED_EXTENSIONS = {"pdf", "djvu", "chm", "docx", "odt", "mobi", "azw", "azw3", "epub", "mp3", "jpg", "jpeg", "png",
                     "gif", "webp"}
# Имя, недопустимое для NTFS/Windows (Syncthing): запрещённые символы, управляющие, точка или пробел в конце.
NTFS_BAD = re.compile(r'[<>:"\\|?*\x00-\x1f]|[. ]$')
# Значимость форматов для dedup: остаётся самый значимый (epub → fb2 → txt → pdf → djvu → прочие).
FORMAT_RANK = {"epub": 0, "fb2": 1, "txt": 2, "pdf": 3, "djvu": 4}
DEDUP_MIN_WORDS = 200
DEDUP_COVERAGE = 0.97
DEDUP_GROUP_MAX = 16
# «Полу-cp866»: заглавные кириллицы cp866 (0x80–0x9F) прочитаны как cp1251, строчные верны («‘тейнбек „жон»).
SEMI_CP866 = {bytes([b]).decode("cp1251"): bytes([b]).decode("cp866") for b in range(0x80, 0xA0) if b != 0x98}
_SEMI = re.escape("".join(SEMI_CP866))
SEMI_START = re.compile(f"[{_SEMI}](?=[а-яё])")
SEMI_ANY = re.compile(f"(?<=[а-яёА-ЯЁ{_SEMI}])[{_SEMI}]|[{_SEMI}](?=[а-яёА-ЯЁ{_SEMI}])")
UPPER_CYR = re.compile("[А-ЯЁ]")
# Однобуквенные слова (А, В, И, К, О, С, У, Я) в искажённом тексте стоят отдельно, без соседних букв.
SEMI_ALONE = re.compile(r"(?<!\w)[Ђ‚€ЉЋ‘“џ](?!\w)")


def norm_key(text: str) -> str:
    """Ключ сравнения имён: NFKC, регистр, ё→е, только слова из букв и цифр."""
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")))


def fix_semi_cp866(text: str, field: bool = False) -> tuple[str, int]:
    """Восстанавливает заглавные «полу-cp866». Только если настоящих заглавных кириллицы (почти) нет, а
    «заглавная + строчные» встречается: в документе — от 3 раз, в поле метаданных — от 1. Возвращает текст и число замен."""
    hits = len(SEMI_START.findall(text))
    if not hits or (not field and hits < 3) or len(UPPER_CYR.findall(text)) > hits // 50:
        return text, 0
    fixed, count = SEMI_ANY.subn(lambda m: SEMI_CP866[m.group(0)], text)
    fixed, alone = SEMI_ALONE.subn(lambda m: SEMI_CP866[m.group(0)], fixed)
    return fixed, count + alone


def fix_fb2_text(text: str) -> tuple[str, int]:
    """«Полу-cp866» во всём fb2, иначе — в отдельных полях описания (title-info и т. п.)."""
    fixed, count = fix_semi_cp866(text)
    if count:
        return fixed, count
    match = re.search(r"<(?:\w+:)?description\b.*?</(?:\w+:)?description>", text, re.S)
    if not match:
        return text, 0
    total = 0

    def field(m: re.Match[str]) -> str:
        nonlocal total
        value, n = fix_semi_cp866(m.group(1), field=True)
        total += n
        return f">{value}<"

    description = re.sub(r">([^<>]+)<", field, match.group(0))
    return (text[:match.start()] + description + text[match.end():], total) if total else (text, 0)


def detect_txt(raw: bytes) -> tuple[str | None, str]:
    """Текст и кодировка TXT: UTF-8/UTF-16 с BOM, UTF-8, затем cp1251/KOI8-R/cp866 по доле строчных среди кириллических
    букв (≥ 0,8 — у верной кодировки ~0,95, у неверной заглавные и строчные перепутаны), затем cp1252. None — не распознан."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return None, "utf-16?"
    if b"\x00" in raw:
        return None, "binary"
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    best: tuple[float, str, str] | None = None
    for encoding, min_letters in (("cp1251", 0.7), ("koi8_r", 0.7), ("cp866", 0.3)):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        high = [ch for ch in text if ord(ch) > 127]
        letters = [ch for ch in high if ch.isalpha()]
        cyr = [ch for ch in letters if "\u0400" <= ch <= "\u04ff"]
        if not cyr or len(letters) < min_letters * len(high) or len(cyr) < 0.8 * len(letters):
            continue
        share = sum(ch.islower() for ch in cyr) / len(cyr)
        if share >= 0.8 and (best is None or share > best[0]):
            best = (share, text, encoding)
    if best:
        return best[1], best[2]
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError:
        return None, "unknown"
    high = [ch for ch in text if ord(ch) > 127]
    if sum(ch.isalpha() or ch in "’‘“”–—…«»•©°" for ch in high) >= 0.8 * len(high):
        return text, "cp1252"
    return None, "unknown"


def normalize_txt(raw: bytes) -> tuple[bytes, str, int]:
    """TXT → UTF-8 без BOM, переводы строк LF, исправленная «полу-cp866». ValueError — кодировка не распознана."""
    text, encoding = detect_txt(raw)
    if text is None:
        raise ValueError(f"кодировка не распознана ({encoding})")
    text, semi = fix_semi_cp866(text)
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"), encoding, semi


def normalize_fb2(raw: bytes) -> tuple[bytes, list[str], int]:
    """fb2: UTF-8 при заявленной иной кодировке; «полу-cp866» в тексте и описании. Данные, что сделано, число замен."""
    done: list[str] = []
    declared = wrong_declaration(raw)
    if declared:
        raw = DECL_RE.sub(rb"\g<1>utf-8\g<3>", raw, count=1)
        done.append(f"encoding {declared}→utf-8")
    match = DECL_RE.search(raw[:300])
    encoding = match.group(2).decode("ascii") if match else "utf-8"
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return raw, done, 0  # битые байты: не перекодируем, чтобы не потерять их
    fixed, semi = fix_fb2_text(text)
    if semi:
        try:
            raw = fixed.encode(encoding)
        except UnicodeEncodeError:
            raw = DECL_RE.sub(rb"\g<1>utf-8\g<3>", fixed.encode("utf-8"), count=1)
        done.append(f"полу-cp866: {semi}")
    return raw, done, semi


FB2_NS = b"http://www.gribuser.ru/xml/fictionbook/2.0"


def ensure_fb2_namespace(raw: bytes) -> bytes:
    """Без пространства имён FictionBook (и xlink у l:href) Calibre собирает почти пустой epub: добавить их в корень."""
    match = re.search(rb"<FictionBook\b[^>]*>", raw[:8192])
    if not match:
        return raw
    tag = match.group(0)
    extra = b""
    if not re.search(rb"\sxmlns\s*=", tag):
        extra += b' xmlns="' + FB2_NS + b'"'
    for prefix in set(re.findall(rb"<[^>]*\s(\w+):href\s*=", raw[:1024 * 1024])):
        if not re.search(rb"\sxmlns:" + prefix + rb"\s*=", tag):
            extra += b" xmlns:" + prefix + b'="http://www.w3.org/1999/xlink"'
    return raw if not extra else raw[:match.start()] + tag[:12] + extra + tag[12:] + raw[match.end():]


def count_words(text: str) -> int:
    return len(re.findall(r"[^\W_]+", text))


def epub_text_loss(fb2_raw: bytes, epub: Path) -> str | None:
    """Проверка, что текст не потерян: в epub не меньше 80 % слов тела fb2 (без бинарных вложений)."""
    text = decode_xml(fb2_raw)
    start, end = text.find("<body"), text.find("<binary")
    fb2_words = count_words(re.sub(r"<[^>]*>", " ", text[max(start, 0):end if end > start else None]))
    with zipfile.ZipFile(epub) as archive:
        epub_words = count_words(" ".join(re.sub(r"<[^>]*>", " ", decode_xml(zip_read(archive, p))) for p in Epub(archive).spine()))
    if fb2_words >= 50 and epub_words < 0.8 * fb2_words:
        return f"в epub {epub_words} слов из {fb2_words} в fb2"
    return None


def calibre_env(tmp: Path) -> dict[str, str]:
    """Окружение Calibre во временном каталоге: в песочнице службы домашний каталог только для чтения."""
    for sub in ("config", "cache", "temp", "xdg"):
        (tmp / sub).mkdir(exist_ok=True)
    return {**os.environ, "HOME": str(tmp), "CALIBRE_CONFIG_DIRECTORY": str(tmp / "config"),
            "CALIBRE_CACHE_DIRECTORY": str(tmp / "cache"), "CALIBRE_TEMP_DIR": str(tmp / "temp"),
            "XDG_CACHE_HOME": str(tmp / "xdg"), "QT_QPA_PLATFORM": "offscreen"}


def relink(db_path: str, pairs: list[tuple[str, str]]) -> int:
    """Переносит отметки и полки со старого пути книги на новый (упаковка, замена, переименование, дубликат)."""
    if not pairs or not Path(db_path).is_file():
        return 0
    with closing(connect(db_path)) as conn, conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            return 0
        for old, new in pairs:
            conn.execute("UPDATE OR IGNORE marks SET path=? WHERE path=?", (new, old))
            conn.execute("UPDATE OR IGNORE shelf_items SET path=? WHERE path=?", (new, old))
            conn.execute("DELETE FROM shelf_items WHERE path=?", (old,))
    return len(pairs)


def zip_date(mtime: float) -> tuple[int, int, int, int, int, int]:
    return max(datetime.fromtimestamp(mtime).timetuple()[:6], (1980, 1, 1, 0, 0, 0))


def name_ok(name: str) -> bool:
    return len(name.encode("utf-8")) <= 255 and not NTFS_BAD.search(name)


def parallel(items: Iterator[Any], fn: Callable[[Any], None], workers: int, run: NightRun) -> None:
    """fn для каждого элемента в workers потоках (в очереди не больше workers*4). Сбой одного файла режим не останавливает."""
    def guarded(item: Any) -> None:
        try:
            fn(item)
        except Exception as error:  # noqa: BLE001 — правило: режим никогда не останавливается из-за одного файла
            what = item[0] if isinstance(item, tuple) else item
            run.record("failed", str(what)[:500], reason=f"{type(error).__name__}: {error}")
        run.tick()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: set[Any] = set()
        for item in items:
            pending.add(pool.submit(guarded, item))
            if len(pending) >= workers * 4:
                _done, pending = wait(pending, return_when=FIRST_COMPLETED)
        wait(pending)


class NightRun:
    """Общее хозяйство изменяющего режима: карантин, журнал действий (JSONL по ходу работы — переживает обрыв),
    прогресс раз в 30 с, итоговый JSON-отчёт и перенос отметок/полок на новые пути."""

    def __init__(self, cfg: dict[str, Any], kind: str, dry_run: bool, path: str | None, out: Callable[[str], None]) -> None:
        self.cfg, self.kind, self.dry_run, self.out = cfg, kind, dry_run, out
        self.root = Path(cfg["books_dir"])
        self.target = target_dir(cfg, path)
        self.qroot = Path(cfg["quarantine_dir"])
        self.limit = cfg["reader_max_mb"] * 1024 * 1024
        self.lock = threading.Lock()
        self.stats: dict[str, int] = {"checked": 0}
        self.relinks: list[tuple[str, str]] = []
        self.started = self.last = time.monotonic()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now():%Y-%m-%d-%H-%M-%S}"
        self.journal_path = REPORTS_DIR / f"{kind}{'-dry-run' if dry_run else ''}-{stamp}.jsonl"
        self.journal = self.journal_path.open("a", encoding="utf-8")
        say(f"{kind}{' (dry-run)' if dry_run else ''}: {self.target}, журнал {self.journal_path}", "night_start")

    def rel(self, full: Path) -> str:
        for base in (self.root, self.target):
            try:
                return full.relative_to(base).as_posix()
            except ValueError:
                continue
        return full.name

    def record(self, action: str, path: Path | str, **extra: Any) -> None:
        line = json.dumps({"action": action, "path": str(path), **extra}, ensure_ascii=False)
        with self.lock:
            self.stats[action] = self.stats.get(action, 0) + 1
            self.journal.write(line + "\n")
            self.journal.flush()
            show = self.dry_run and self.stats[action] <= 20
        if show:
            self.out(f"[dry-run] будет: {action} {path}{' → ' + str(extra['to']) if 'to' in extra else ''}")
        if action.startswith("rejected") or action == "failed":
            say(f"{self.kind}: {action} {path}", action.replace("-", "_"), logging.WARNING, detail=str(extra.get("reason", "")))

    def count(self, key: str) -> None:
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def tick(self) -> None:
        with self.lock:
            self.stats["checked"] += 1
            now = time.monotonic()
            if now - self.last < 30:
                return
            self.last = now
            stats = dict(self.stats)
        done = sum(v for k, v in stats.items() if k not in ("checked", "ok", "skipped"))
        say(f"{self.kind}: проверено {stats['checked']}, сделано {done}, {stats['checked'] / max(now - self.started, 1e-6):.1f} "
            f"файлов/с · {json.dumps(stats, ensure_ascii=False)}", "progress")

    def quarantine(self, full: Path, folder: str, **extra: Any) -> Path:
        """Переносит файл в карантин <folder>/; в dry-run только запись плана. Имя занято — суффикс _1, _2…"""
        if folder not in QUARANTINE_REASONS:
            raise ValueError(f"неизвестная причина карантина {folder}")
        dest = self.qroot / folder / self.rel(full)
        if not self.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                if dest.exists():
                    dest = unique_path(dest.parent, dest.name, set())
                shutil.move(full, dest)  # одна файловая система — это rename
        self.record(folder, full, to=str(dest), **extra)
        return dest

    def backup(self, full: Path, folder: str = "replaced") -> Path:
        """Копия прежней версии файла в карантин перед его заменой (сам файл остаётся на месте)."""
        dest = self.qroot / folder / self.rel(full)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if dest.exists():
                dest = unique_path(dest.parent, dest.name, set())
            shutil.copy2(full, dest)
        return dest

    def moved(self, old: Path, new: Path) -> None:
        with self.lock:
            self.relinks.append((self.rel(old), self.rel(new)))

    def finish(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        self.journal.close()
        if not self.dry_run:
            self.stats["relinked"] = relink(self.cfg["db_path"], self.relinks)
        report = write_report(f"{self.kind}{'-dry-run' if self.dry_run else ''}",
                              {"target": str(self.target), "dry_run": self.dry_run, "stats": self.stats,
                               "journal": str(self.journal_path), **(extra or {})})
        say(f"{self.kind}{' (dry-run)' if self.dry_run else ''} завершён: {json.dumps(self.stats, ensure_ascii=False)}, "
            f"отчёт {report}", "night_done", detail=json.dumps(self.stats))
        if self.dry_run:
            self.out(f"[dry-run] итог {self.kind}: {json.dumps(self.stats, ensure_ascii=False)}; журнал {self.journal_path}")
        return {**self.stats, "report": str(report)}


def walk_dirs(target: Path, errors: list[str]) -> Iterator[tuple[Path, list[str]]]:
    """Каталоги и имена файлов в них (служебные каталоги и ресурсы HTML пропускаются)."""
    def onerror(error: OSError) -> None:
        errors.append(f"{error.filename}: {error.strerror}")

    for current, dirs, files in os.walk(target, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not d.endswith(HTML_SIDECAR_SUFFIXES))
        yield Path(current), sorted(f for f in files if not f.startswith("."))


def write_zip(tmp: Path, name: str, mtime: float, source: Any, size: int, ext: str) -> int:
    """Однокнижный zip из потока source; возвращает CRC32 прочитанных данных (для сверки с записанным)."""
    info = zipfile.ZipInfo(name, date_time=zip_date(mtime))
    info.compress_type = zipfile.ZIP_STORED if ext in STORED_EXTENSIONS else zipfile.ZIP_DEFLATED
    info.file_size = size  # по нему zipfile заранее решает, нужен ли ZIP64
    info.external_attr = 0o644 << 16
    crc = 0
    with zipfile.ZipFile(tmp, "w", compresslevel=6) as archive, archive.open(info, "w") as sink:
        while chunk := source.read(4 * 1024 * 1024):
            crc = zlib.crc32(chunk, crc)
            sink.write(chunk)
    return crc


def verify_zip(path: Path, name: str, size: int, crc: int) -> str | None:
    """Проверка упакованного: одна запись с тем же именем, размер и CRC как у исходника, testzip без ошибок."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != 1 or infos[0].filename != name:
                return "не одна запись или чужое имя"
            if infos[0].file_size != size or infos[0].CRC != crc:
                return "размер или CRC не совпадают с исходником"
            if archive.testzip() is not None:
                return "ошибка CRC при чтении"
    except (OSError, zipfile.BadZipFile, EOFError) as error:
        return f"{type(error).__name__}: {error}"
    return None


def replace_zip_member(run: NightRun, full: Path, info: zipfile.ZipInfo, data: bytes) -> None:
    """Перепаковка однокнижного zip с новым содержимым книги: проверка, копия прежнего в карантин, атомарная замена."""
    fd, tmp_name = tempfile.mkstemp(prefix=".repack.", suffix=".tmp", dir=full.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        name = info.filename
        crc = write_zip(tmp, name, full.stat().st_mtime, io.BytesIO(data), len(data), member_ext(info))
        problem = verify_zip(tmp, name, len(data), crc)
        if problem:
            raise ValueError(f"перепаковка: {problem}")
        os.chmod(tmp, full.stat().st_mode & 0o666)
        run.backup(full)
        os.replace(tmp, full)
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# fix-encoding: fb2 (encoding, «полу-cp866»), txt (cp1251/KOI8-R/cp866 → UTF-8, CRLF → LF), то же внутри однокнижных zip
# ----------------------------------------------------------------------

def fix_encoding(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, only: str = "all",
                 out: Callable[[str], None] = print) -> dict[str, Any]:
    """Лечит кодировку книг; файлы правятся на месте атомарно. Исправление «полу-cp866» сохраняет прежнюю версию
    в карантин replaced/, перепакованный zip — тоже. Нераспознанная кодировка txt — в карантин rejected-encoding/."""
    run = NightRun(cfg, "fix-encoding", dry_run, path, out)
    kinds = {"all": ("fb2", "txt"), "fb2": ("fb2",), "txt": ("txt",)}[only]
    suffixes = (*(f".{k}" for k in kinds), ".zip")

    def transform(kind: str, raw: bytes) -> tuple[bytes, str, int]:
        if kind == "fb2":
            data, done, semi = normalize_fb2(raw)
            return data, ", ".join(done), semi
        data, encoding, semi = normalize_txt(raw)
        return data, f"{encoding}→utf-8+LF" + (f", полу-cp866: {semi}" if semi else ""), semi

    def one(item: tuple[Path, int]) -> None:
        full, size = item
        kind = full.name.rpartition(".")[2].lower()
        if kind == "zip":
            try:
                with zipfile.ZipFile(full) as archive:
                    info = single_member(archive)
                    if info is None or member_ext(info) not in kinds:
                        return
                    raw = read_member(archive, info, run.limit)
            except (ValueError, zipfile.BadZipFile, EOFError) as error:
                run.record("skipped", full, reason=str(error))  # битые zip разбирает zip-books
                return
            try:
                data, what, _semi = transform(member_ext(info), raw)
            except ValueError as error:
                run.quarantine(full, "rejected-encoding", reason=str(error))
                return
            if data != raw:
                run.record("fixed-zip", full, what=what)
                if not dry_run:
                    replace_zip_member(run, full, info, data)
            return
        if size > run.limit:
            run.record("skipped", full, reason="больше reader_max_mb")
            return
        if kind == "fb2":
            with full.open("rb") as handle:
                head = handle.read(FB2_HEAD_BYTES)
            # Полностью читается только fb2 с признаками ошибки в начале: «полу-cp866» или UTF-8 при иной заявленной.
            match = DECL_RE.search(head[:300])
            try:
                declared_utf8 = not match or codecs.lookup(match.group(2).decode("ascii")).name == "utf-8"
            except LookupError:
                declared_utf8 = False
            suspicious = bool(SEMI_START.search(decode_xml(head)))
            if not declared_utf8 and not suspicious:
                try:
                    head.decode("utf-8")
                    suspicious = True
                except UnicodeDecodeError as error:
                    suspicious = error.start >= len(head) - 4  # UTF-8, обрезанный посреди символа
            if not suspicious:
                return
        raw = full.read_bytes()
        try:
            data, what, semi = transform(kind, raw)
        except ValueError as error:
            run.quarantine(full, "rejected-encoding", reason=str(error))
            return
        if data == raw:
            return
        run.record("fixed", full, what=what)
        if not dry_run:
            if semi:
                run.backup(full)
            atomic_write(full, data, "fix-encoding")

    errors: list[str] = []
    parallel(walk_files(run.target, suffixes, errors), one, cfg["workers"], run)
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    return run.finish({"walk_errors": errors[:200]})


# ----------------------------------------------------------------------
# convert fb2 → epub (Calibre), в т. ч. fb2 внутри однокнижного zip
# ----------------------------------------------------------------------

def convert_fb2(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
                out: Callable[[str], None] = print) -> dict[str, Any]:
    """fb2 и однокнижный zip с fb2 → name.epub рядом (имя как у fb2), проверка verify_book, исходник — в карантин
    replaced/. Рядом уже есть epub с тем же нормализованным именем — исходник сразу в replaced/ без конвертации.
    Сбой Calibre или проверки — исходник в rejected-convert/."""
    if not dry_run and not shutil.which("ebook-convert"):
        raise ConfigError("не найден ebook-convert (Calibre)")
    run = NightRun(cfg, "convert-fb2", dry_run, path, out)
    timeout = cfg["convert_timeout"]

    def tasks() -> Iterator[tuple[Path, dict[str, str]]]:
        for folder, names in walk_dirs(run.target, errors):
            epubs = {norm_key(n[:-5]): n for n in names if n.lower().endswith(".epub")}
            for name in names:
                if name.lower().endswith((".fb2", ".zip")):
                    yield folder / name, epubs

    def one(item: tuple[Path, dict[str, str]]) -> None:
        src, epubs = item
        data: bytes | None = None
        if src.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(src) as archive:
                    info = single_member(archive)
                    if info is None or member_ext(info) != "fb2":
                        return
                    data = read_member(archive, info, 256 * 1024 * 1024)
            except (ValueError, zipfile.BadZipFile, EOFError) as error:
                run.record("skipped", src, reason=str(error))
                return
        stem = split_name(src.name)[0]
        result = src.parent / f"{stem}.epub"
        existing = epubs.get(norm_key(stem))
        if existing or result.exists():
            keeper = src.parent / (existing or result.name)
            run.quarantine(src, "replaced", reason="рядом уже есть epub", keeper=str(keeper))
            run.moved(src, keeper)
            return
        if not name_ok(result.name):
            run.quarantine(src, "rejected-name", reason="имя epub недопустимо")
            return
        if dry_run:
            run.record("convert", src, to=str(result))
            return
        with tempfile.TemporaryDirectory(prefix="books_library-fb2-") as tmp_name:
            tmp = Path(tmp_name)
            staged, produced = tmp / "book.fb2", tmp / "book.epub"
            if data is None:
                if src.stat().st_size > 256 * 1024 * 1024:
                    run.record("skipped", src, reason="fb2 больше 256 МБ")
                    return
                data = src.read_bytes()
            data = ensure_fb2_namespace(data)
            staged.write_bytes(data)
            try:
                done = subprocess.run(["ebook-convert", str(staged), str(produced)], capture_output=True, env=calibre_env(tmp),
                                      cwd=tmp, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
                problem = None if done.returncode == 0 and produced.is_file() else \
                    f"ebook-convert: {done.stderr.decode('utf-8', errors='replace')[-300:] or done.returncode}"
            except subprocess.TimeoutExpired:
                problem = f"ebook-convert: тайм-аут {timeout} с"
            try:
                problem = problem or verify_book(produced, "epub") or epub_text_loss(data, produced)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
                problem = f"проверка epub: {type(error).__name__}: {error}"
            if problem:
                run.quarantine(src, "rejected-convert", reason=problem)
                return
            fd, tmp_out = tempfile.mkstemp(prefix=".convert.", suffix=".tmp", dir=src.parent)
            os.close(fd)
            try:
                shutil.copyfile(produced, tmp_out)
                os.chmod(tmp_out, 0o644)
                if result.exists():  # появился, пока конвертировали
                    raise FileExistsError(str(result))
                os.replace(tmp_out, result)
            finally:
                Path(tmp_out).unlink(missing_ok=True)
        run.quarantine(src, "replaced", reason="заменён проверенным epub", keeper=str(result))
        run.moved(src, result)

    errors: list[str] = []
    parallel(tasks(), one, workers or cfg["workers"], run)
    return run.finish({"walk_errors": errors[:200]})


# ----------------------------------------------------------------------
# dedup: дубликаты по значимости формата с подтверждением по тексту (шинглы по 5 слов)
# ----------------------------------------------------------------------

def book_text(cfg: dict[str, Any], full: Path, fmt: str, tmp: Path) -> str:
    """Текст книги для сравнения: fb2/epub/txt — сами, pdf — pdftotext, djvu — djvutxt, прочее — ebook-convert в txt."""
    limit = cfg["reader_max_mb"] * 1024 * 1024
    timeout = cfg["convert_timeout"]
    if full.name.lower().endswith(".zip"):
        with zipfile.ZipFile(full) as archive:
            info = single_member(archive)
            if info is None:
                return ""
            staged = tmp / f"inner.{member_ext(info)}"
            staged.write_bytes(read_member(archive, info, limit))
        return book_text(cfg, staged, member_ext(info), tmp)
    if fmt == "fb2":
        text = decode_xml(read_limited(full, limit))
        start, end = text.find("<body"), text.find("<binary")
        return re.sub(r"<[^>]*>", " ", text[max(start, 0):end if end > start else None])
    if fmt == "epub":
        with zipfile.ZipFile(full) as archive:
            epub = Epub(archive)
            return " ".join(re.sub(r"<[^>]*>", " ", decode_xml(zip_read(archive, p))) for p in epub.spine())
    if fmt == "txt":
        return decode_text(read_limited(full, limit))
    if fmt in ("pdf", "djvu"):
        args = ["pdftotext", "-q", "-enc", "UTF-8", str(full), "-"] if fmt == "pdf" else ["djvutxt", str(full)]
        done = subprocess.run(args, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        return done.stdout[:limit].decode("utf-8", errors="replace")
    if fmt in CALIBRE_FORMATS or fmt in ("azw", "azw3", "mobi"):
        staged, result = tmp / f"book.{fmt}", tmp / "book.txt"
        shutil.copyfile(full, staged)
        subprocess.run(["ebook-convert", str(staged), str(result)], capture_output=True, env=calibre_env(tmp), cwd=tmp,
                       timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        return decode_text(result.read_bytes()[:limit]) if result.is_file() else ""
    return ""


def shingles(text: str) -> tuple[int, set[int], bool]:
    """Число слов и хэши шинглов по 5 слов. У длинных текстов — выборка хэшей с остатком 0 по модулю 4: фильтр один
    для всех файлов, поэтому доля «шинглы младшего в старшем» оценивается без смещения, а памяти вчетверо меньше."""
    words = re.findall(r"[^\W_]+", fold(text))
    hashes = {hash(tuple(words[i:i + 5])) for i in range(max(len(words) - 4, 0))}
    sampled = len(words) > 20000
    if sampled:
        hashes = {h for h in hashes if h % 4 == 0}
    return len(words), hashes, sampled


def dedup(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
          out: Callable[[str], None] = print) -> dict[str, Any]:
    """Группы-кандидаты: автор+название из метаданных (по всей библиотеке) и имя файла в той же папке. В группе
    книги упорядочены по значимости формата (epub → fb2 → txt → pdf → djvu → прочие), затем по объёму текста и
    размеру. Книга — дубль (карантин duplicate/) только если ≥ 97 % её шинглов есть у оставляемой и в ней ≥ 200 слов;
    уникальный текст в любом формате остаётся, такие группы — «похожие» для ручного решения. Итог — в dup_groups."""
    run = NightRun(cfg, "dedup", dry_run, path, out)
    root = run.root
    if not run.target.resolve().is_relative_to(root.resolve()):
        raise ConfigError("dedup: --path должен быть внутри каталога книг")
    errors: list[str] = []
    known: dict[str, sqlite3.Row] = {}
    if Path(cfg["db_path"]).is_file():
        with closing(connect(cfg["db_path"], readonly=True)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
                known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime, fmt, title, authors FROM books")}
    files = [(rel, size, mtime) for rel, size, mtime in iter_book_files(run.target, errors)]
    prefix = run.rel(run.target)
    prefix = "" if prefix in ("", ".") else prefix + "/"
    files = [(prefix + rel, size, mtime) for rel, size, mtime in files]

    def meta_of(item: tuple[str, int, float]) -> dict[str, Any]:
        rel, size, mtime = item
        row = known.get(rel)
        if row and row["size"] == size and row["mtime"] == mtime and row["fmt"] != "fb2.zip":
            return {"fmt": row["fmt"], "title": row["title"], "authors": [a for a in row["authors"].split("; ") if a]}
        return read_book_meta(root, rel)

    with ThreadPoolExecutor(max_workers=workers or cfg["workers"]) as pool:
        metas = list(pool.map(meta_of, files, chunksize=64))
    say(f"dedup: метаданные {len(files)} файлов собраны", "progress")
    parent = list(range(len(files)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict[str, int] = {}
    for index, ((rel, _size, _mtime), meta) in enumerate(zip(files, metas, strict=True)):
        if meta["fmt"] == "zip":
            continue  # многофайловый архив — не книга для сравнения
        keys = [f"n:{PurePosixPath(rel).parent}/{norm_key(split_name(rel)[0])}"]
        title = norm_key(meta.get("title", ""))
        authors = " ".join(sorted(norm_key(" ".join(meta.get("authors", []))).split()))
        if authors and re.search(r"[^\W\d_]", title):
            keys.append(f"m:{authors}|{title}")
        for key in keys:
            if key in first:
                parent[find(index)] = find(first[key])
            else:
                first[key] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(files)):
        if metas[index]["fmt"] != "zip":
            groups.setdefault(find(index), []).append(index)
    candidates = [g for g in groups.values() if len(g) > 1]
    say(f"dedup: групп-кандидатов {len(candidates)}", "progress")
    results: list[dict[str, Any]] = []

    def one(group: list[int]) -> None:
        if len(group) > DEDUP_GROUP_MAX:
            run.record("group-too-large", files[group[0]][0], members=len(group))
            return
        members = []
        with tempfile.TemporaryDirectory(prefix="books_library-dedup-") as tmp_name:
            for index in group:
                rel, size, _mtime = files[index]
                fmt = metas[index]["fmt"]
                try:
                    words, shingle_set, sampled = shingles(book_text(cfg, root / rel, fmt, Path(tmp_name)))
                except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError, subprocess.TimeoutExpired) as error:
                    words, shingle_set, sampled = 0, set(), False
                    run.record("no-text", rel, reason=f"{type(error).__name__}: {error}")
                members.append({"path": rel, "fmt": fmt, "size": size, "words": words, "sh": shingle_set, "sampled": sampled})
        members.sort(key=lambda m: (FORMAT_RANK.get(m["fmt"], 5), -m["words"], -m["size"]))
        kept: list[dict[str, Any]] = []
        for member in members:
            best, keeper = 0.0, None
            for other in kept:
                mine = member["sh"]
                if other["sampled"] and not member["sampled"]:  # сравнивать с выборкой — по той же выборке
                    mine = {h for h in mine if h % 4 == 0}
                if mine and other["sh"]:
                    coverage = len(mine & other["sh"]) / len(mine)
                    if coverage > best:
                        best, keeper = coverage, other
            member["coverage"] = round(best, 4) if kept else None
            if keeper is not None and member["words"] >= DEDUP_MIN_WORDS and best >= DEDUP_COVERAGE:
                member.update(role="duplicate", keeper=keeper["path"])
                dest = run.quarantine(root / member["path"], "duplicate", keeper=keeper["path"], coverage=member["coverage"])
                member["quarantine"] = str(dest)
                run.moved(root / member["path"], root / keeper["path"])
            else:
                member.update(role="kept", keeper="", quarantine="")
                kept.append(member)
        for member in members:
            member.pop("sh")
            member.pop("sampled")
        status = "similar" if len(kept) > 1 else "auto"
        if status == "auto" and len(kept) == len(members):
            return
        with run.lock:
            results.append({"key": min(files[i][0] for i in group), "status": status, "members": members})

    parallel(iter(candidates), one, workers or cfg["workers"], run)
    if not dry_run and results:
        migrate_database(cfg["db_path"], out=out)
        with closing(connect(cfg["db_path"])) as conn, conn:
            resolved = {r[0] for r in conn.execute("SELECT key FROM dup_groups WHERE status='resolved'")}
            conn.execute("DELETE FROM dup_groups WHERE status <> 'resolved'")
            for group in results:
                if group["status"] == "similar" and group["key"] in resolved:
                    continue
                gid = conn.execute("INSERT INTO dup_groups(key, status, created) VALUES (?,?,?)",
                                   (group["key"], group["status"], time.time())).lastrowid
                conn.executemany("INSERT OR IGNORE INTO dup_members VALUES (?,?,?,?,?,?,?,?,?)",
                                 [(gid, m["path"], m["fmt"], m["size"], m["words"], m["coverage"], m["role"], m["keeper"],
                                   m["quarantine"]) for m in group["members"]])
    run.stats["groups"] = len(results)
    run.stats["similar_groups"] = sum(g["status"] == "similar" for g in results)
    return run.finish({"walk_errors": errors[:200], "groups": results})


# ----------------------------------------------------------------------
# zip-books: каждая книга (кроме epub и zip) → name.ext.zip; однокнижные zip — к виду name.ext.zip
# ----------------------------------------------------------------------

def zip_books(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
              out: Callable[[str], None] = print) -> dict[str, Any]:
    """Упаковка: уже сжатые форматы — ZIP_STORED, прочие — deflate 6. Проверка: одна запись, размер и CRC как у
    исходника, testzip; только после неё исходник удаляется (содержимое в zip побайтно — это не карантин).
    Многофайловые zip не трогаются; битые zip — в rejected-zip/, недопустимые для NTFS или длинные имена — в rejected-name/."""
    run = NightRun(cfg, "zip-books", dry_run, path, out)
    suffixes = tuple(f".{x}" for x in BOOK_EXTENSIONS if x != "epub")

    def old_zip(src: Path) -> None:
        try:
            with zipfile.ZipFile(src) as archive:
                info = single_member(archive)
                if info is None:
                    return  # многофайловый архив — не трогаем
                if archive.testzip() is not None:
                    raise zipfile.BadZipFile("ошибка CRC")
        except (zipfile.BadZipFile, EOFError, ValueError) as error:
            run.quarantine(src, "rejected-zip", reason=f"{type(error).__name__}: {error}")
            return
        wanted = f"{split_name(src.name)[0]}.{member_ext(info)}.zip"
        if src.name == wanted:
            run.count("ok")
            return
        if not name_ok(wanted):
            run.quarantine(src, "rejected-name", reason=f"имя {wanted} недопустимо")
            return
        dest = src.parent / wanted
        if dest.exists():
            run.record("conflict", src, to=str(dest))
            return
        run.record("renamed", src, to=str(dest))
        if not dry_run:
            os.rename(src, dest)
            run.moved(src, dest)

    def one(item: tuple[Path, int]) -> None:
        src, size = item
        ext = src.name.rpartition(".")[2].lower()
        if ext == "zip":
            old_zip(src)
            return
        dest = src.parent / f"{src.name}.zip"
        if not name_ok(dest.name):
            run.quarantine(src, "rejected-name", reason="имя zip длиннее 255 байт или недопустимо для NTFS")
            return
        if dest.exists():
            run.quarantine(src, "rejected-zip", reason=f"имя занято: {dest.name}")
            return
        if dry_run:
            run.record("zipped", src, to=str(dest))
            return
        stat = src.stat()
        fd, tmp_name = tempfile.mkstemp(prefix=".zip-books.", suffix=".tmp", dir=src.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with src.open("rb") as source:
                crc = write_zip(tmp, src.name, stat.st_mtime, source, stat.st_size, ext)
            problem = verify_zip(tmp, src.name, stat.st_size, crc)
            now = src.stat()
            if problem or (now.st_size, now.st_mtime) != (stat.st_size, stat.st_mtime):
                raise ValueError(problem or "исходник изменился во время упаковки")
            os.chmod(tmp, stat.st_mode & 0o666)
            os.utime(tmp, (stat.st_atime, stat.st_mtime))
            if dest.exists():
                raise FileExistsError(str(dest))
            os.replace(tmp, dest)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            tmp.unlink(missing_ok=True)
            run.quarantine(src, "rejected-zip", reason=f"{type(error).__name__}: {error}")
            return
        src.unlink()
        run.record("zipped", src, to=str(dest), size=stat.st_size, zip_size=dest.stat().st_size)
        run.moved(src, dest)

    errors: list[str] = []
    parallel(walk_files(run.target, suffixes, errors), one, workers or cfg["workers"], run)
    return run.finish({"walk_errors": errors[:200]})


FILE_CHANGING = ("fix-encoding", "unpack-fb2zip", "convert", "dedup", "zip-books")
COMMANDS = ("scan", "migrate", *FILE_CHANGING)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_OUTPUT_LINES = 2000


def command_params(command: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Проверяет и нормализует параметры режима (общие для CLI и API). Неизвестные ключи — ошибка."""
    if command not in COMMANDS:
        raise ConfigError(f"неизвестная команда {command!r}; допустимо: {', '.join(COMMANDS)}")
    allowed = {
        "scan": set(), "migrate": set(), "fix-encoding": {"path", "only"}, "unpack-fb2zip": {"path", "max_mb"},
        "convert": {"path", "only", "to", "overwrite", "skip_newer", "exclude", "author", "title_mode", "toc", "toc_depth",
                    "delete_source", "workers"},
        "dedup": {"path", "workers"}, "zip-books": {"path", "workers"},
    }[command]
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{command}: неизвестные параметры {', '.join(sorted(unknown))}")
    params: dict[str, Any] = {}
    if "path" in allowed:
        path = raw.get("path")
        if path is not None and (not isinstance(path, str) or not path or "\x00" in path):
            raise ConfigError("path должен быть непустой строкой")
        params["path"] = path
    if "only" in allowed:
        valid = ("all", "fb2", "txt") if command == "fix-encoding" else ("all", "fb2", *CONVERT_SOURCES)
        params["only"] = raw.get("only", "all")
        if params["only"] not in valid:
            raise ConfigError(f"only: допустимо {', '.join(valid)}")
    if "workers" in allowed:
        params["workers"] = raw.get("workers")
        if params["workers"] is not None and (not isinstance(params["workers"], int) or isinstance(params["workers"], bool)
                                              or not 1 <= params["workers"] <= 64):
            raise ConfigError("workers: целое 1..64")
    if command == "unpack-fb2zip":
        params["max_mb"] = raw.get("max_mb", 200)
        if not isinstance(params["max_mb"], int) or isinstance(params["max_mb"], bool) or not 1 <= params["max_mb"] <= 4096:
            raise ConfigError("max_mb: целое 1..4096")
    if command == "convert":
        params.update(to=raw.get("to", "epub"), title_mode=raw.get("title_mode", "auto"), toc_depth=raw.get("toc_depth", 3),
                      author=raw.get("author", ""), exclude=raw.get("exclude", []))
        for key in ("overwrite", "skip_newer", "delete_source"):
            params[key] = raw.get(key, False)
        params["toc"] = raw.get("toc", True)
        if params["to"] not in ("epub", "fb2") or params["title_mode"] not in ("auto", "filename", "keep"):
            raise ConfigError("to: epub|fb2; title_mode: auto|filename|keep")
        if not isinstance(params["toc_depth"], int) or isinstance(params["toc_depth"], bool) or not 1 <= params["toc_depth"] <= 6:
            raise ConfigError("toc_depth: целое 1..6")
        if not all(isinstance(params[k], bool) for k in ("overwrite", "skip_newer", "delete_source", "toc")):
            raise ConfigError("overwrite, skip_newer, delete_source, toc — логические значения")
        if not isinstance(params["author"], str) or not isinstance(params["exclude"], list) or \
                not all(isinstance(x, str) for x in params["exclude"]):
            raise ConfigError("author — строка, exclude — список строк")
    return params


def run_command(cfg: dict[str, Any], command: str, dry_run: bool, params: dict[str, Any],
                out: Callable[[str], None] = print) -> Any:
    """Единая точка запуска режимов для CLI и API. Изменяющий запуск берёт общую блокировку (код 3 — занято)."""
    def action() -> Any:
        if command == "scan":
            return scan(cfg, dry_run, out=out)
        if command == "migrate":
            return {"schema": migrate_database(cfg["db_path"], dry_run, out=out)}
        if command == "fix-encoding":
            return fix_encoding(cfg, dry_run, params["path"], params["only"], out=out)
        if command == "unpack-fb2zip":
            return unpack_fb2zip(cfg, dry_run, params["path"], params["max_mb"], out=out)
        if command == "dedup":
            return dedup(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "zip-books":
            return zip_books(cfg, dry_run, params["path"], params["workers"], out=out)
        if params["only"] == "fb2":
            return convert_fb2(cfg, dry_run, params["path"], params["workers"], out=out)
        return convert_documents(cfg, dry_run, params["path"], out=out,
                                 **{k: v for k, v in params.items() if k not in ("path", "workers")})

    if dry_run or command == "migrate":
        return action()
    with scan_lock():
        return action()


def job_view(job: dict[str, Any], full: bool = True) -> dict[str, Any]:
    keys = ("id", "command", "params", "dry_run", "confirm", "state", "created", "started", "finished", "result", "error")
    view = {k: job.get(k) for k in keys}
    view["output"] = job["output"] if full else job["output"][-5:]
    return view


def start_job(cfg: dict[str, Any], command: str, dry_run: bool, raw: dict[str, Any], confirm: str = "") -> dict[str, Any]:
    """Фоновое задание. Боевой запуск режима, меняющего файлы книг, требует confirm — id успешного dry-run
    того же режима с теми же параметрами (правило проекта: сначала dry-run, затем боевой запуск)."""
    params = command_params(command, raw)
    if command in FILE_CHANGING and not dry_run:
        with JOBS_LOCK:
            proof = JOBS.get(confirm)
        if not proof or not proof["dry_run"] or proof["state"] != "done" or proof["command"] != command \
                or proof["params"] != params:
            raise ConfirmRequiredError("боевой запуск требует confirm — id успешного dry-run с теми же параметрами")
    job: dict[str, Any] = {"id": secrets.token_hex(8), "command": command, "params": params, "dry_run": dry_run,
                           "confirm": confirm or None, "state": "queued", "created": datetime.now().isoformat(timespec="seconds"),
                           "started": None, "finished": None, "result": None, "error": None, "output": []}

    def out(line: str) -> None:
        if len(job["output"]) < JOB_OUTPUT_LINES:
            job["output"].append(str(line))

    def run() -> None:
        job.update(state="running", started=datetime.now().isoformat(timespec="seconds"))
        try:
            job["result"] = run_command(cfg, command, dry_run, params, out)
            job["state"] = "done"
        except (ScanBusyError, ConfigError, OSError, ValueError, sqlite3.Error) as error:
            job.update(state="failed", error=str(error))
            say(f"задание {command} не выполнено: {error}", "job_failed", logging.ERROR, detail=job["id"])
        finally:
            job["finished"] = datetime.now().isoformat(timespec="seconds")

    with JOBS_LOCK:
        JOBS[job["id"]] = job
        for stale in sorted(JOBS.values(), key=lambda j: j["created"])[:-100]:
            if stale["state"] in ("done", "failed"):
                JOBS.pop(stale["id"], None)
    say(f"задание {command}{' (dry-run)' if dry_run else ''}: {job['id']}", "job_start", detail=json.dumps(params, ensure_ascii=False))
    threading.Thread(target=run, name=f"job-{command}", daemon=True).start()
    return job


# ======================================================================
# Чтение книг
# ======================================================================

class Sanitizer(HTMLParser):
    """Превращает XHTML/fb2 в безопасный HTML: только разрешённые теги без атрибутов, картинки — через сервер."""

    ALLOWED = {
        "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "em", "strong", "b", "i", "u", "s", "sub", "sup",
        "blockquote", "pre", "code", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "td", "th",
        "div", "span", "section", "small", "img",
    }
    VOID = {"br", "hr", "img"}
    DROP = {"script", "style", "head", "noscript", "iframe", "object", "embed", "binary", "template"}
    FB2 = {
        "title": "h3", "subtitle": "h4", "emphasis": "em", "strikethrough": "s", "epigraph": "blockquote",
        "cite": "blockquote", "poem": "div", "stanza": "div", "v": "p", "text-author": "p", "empty-line": "br",
        "image": "img", "body": "div", "annotation": "blockquote", "description": "head",
    }

    def __init__(self, image_url: Callable[[str], str | None], fb2: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url = image_url
        self.tag_map = self.FB2 if fb2 else {"image": "img"}
        self.out: list[str] = []
        self.skip = 0

    def _name(self, tag: str) -> str:
        tag = tag.removeprefix("x-")
        tag = tag.rpartition(":")[2] if ":" in tag else tag
        return self.tag_map.get(tag, tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = self._name(tag)
        if name in self.DROP:
            self.skip += 1
            return
        if self.skip or name not in self.ALLOWED:
            return
        if name == "img":
            values = dict(attrs)
            src = next((values[k] for k in ("src", "l:href", "xlink:href", "href") if values.get(k)), None)
            url = self.image_url(src) if src else None
            if url:
                self.out.append(f'<img src="{html.escape(url)}" alt="" loading="lazy">')
            return
        self.out.append(f"<{name}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = self._name(tag)
        if name in self.DROP or self.skip or name not in self.ALLOWED:
            return
        self.handle_starttag(tag, attrs)
        if name not in self.VOID:
            self.out.append(f"</{name}>")

    def handle_endtag(self, tag: str) -> None:
        name = self._name(tag)
        if name in self.DROP:
            self.skip = max(self.skip - 1, 0)
        elif not self.skip and name in self.ALLOWED and name not in self.VOID:
            self.out.append(f"</{name}>")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        self.close()
        return "".join(self.out)


def sanitize(markup: str, image_url: Callable[[str], str | None], fb2: bool = False) -> str:
    cleaner = Sanitizer(image_url, fb2)
    # Префикс «x-» у всех тегов: иначе HTMLParser разбирает содержимое title/textarea/script/style как
    # сырой текст (теги fb2 внутри <title> вышли бы наружу текстом, а <script/> поглотил бы главу).
    cleaner.feed(re.sub(r"<(/?)(?=[A-Za-z])", r"<\1x-", markup))
    return cleaner.result()


def read_limited(path: Path, limit: int) -> bytes:
    if path.stat().st_size > limit:
        raise ValueError("файл больше предела reader_max_mb")
    return path.read_bytes()


def fb2_bytes(path: Path, fmt: str, limit: int) -> bytes:
    if fmt != "fb2.zip":
        return read_limited(path, limit)
    with zipfile.ZipFile(path) as archive:
        inner = zip_fb2_name(archive)
        if not inner:
            raise ValueError("в архиве нет единственного fb2")
        return zip_read(archive, inner, limit)


def fb2_binary(raw: bytes, binary_id: str) -> tuple[str, bytes] | None:
    pattern = rb'<binary\b([^>]*\bid=["\']' + re.escape(binary_id.encode("utf-8")) + rb'["\'][^>]*)>(.*?)</binary>'
    match = re.search(pattern, raw, re.S)
    if not match:
        return None
    ctype = parse_attrs(match.group(1).decode("utf-8", errors="replace")).get("content-type", "")
    try:
        data = base64.b64decode(re.sub(rb"\s+", b"", match.group(2)))
    except (binascii.Error, ValueError):
        return None
    return ctype, data


def safe_image_type(ctype: str, name: str = "") -> str | None:
    """Только растровые картинки: SVG и прочее с того же источника могли бы исполнить скрипт."""
    if ctype in IMAGE_TYPES.values():
        return ctype
    return IMAGE_TYPES.get(name.rpartition(".")[2].lower())


# ======================================================================
# Просмотр прочих форматов: серверные инструменты дают браузеру понятный вид
# ======================================================================

# Calibre (ebook-convert) переводит их в EPUB, дальше работает обычная читалка EPUB.
CALIBRE_FORMATS = {"mobi", "azw", "azw3", "doc", "docx", "rtf", "odt", "chm"}
# Постраничные форматы: позиция чтения — номер главы/страницы плюс доля прокрутки.
PAGED_FORMATS = {"epub", "djvu", *CALIBRE_FORMATS}
# Текстовые файлы внутри ZIP, которые показываются как текст.
TEXT_EXTENSIONS = {"txt", "nfo", "diz", "ini", "log", "md", "csv", "xml", "json", "pas", "c", "cpp", "h", "hpp", "cs",
                   "java", "py", "js", "bas", "asm", "bat", "sql"}
DJVU_PAGE_WIDTH = 1400
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def cache_lock(key: str) -> threading.Lock:
    """Одна конвертация на ключ: параллельные запросы той же книги ждут готовый результат."""
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def cache_key(book: sqlite3.Row) -> str:
    """Ключ кэша меняется вместе с файлом (размер и mtime): устаревший результат не используется."""
    return f"{book['id']}-{book['size']}-{int(book['mtime'])}"


def cache_store(path: Path, data: bytes, limit_mb: int) -> None:
    """Атомарная запись в кэш и удаление самых старых файлов сверх cache_mb."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".cache.", dir=CACHE_DIR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp_name, path)
    cache_prune(limit_mb)


def cache_prune(limit_mb: int) -> None:
    files = []
    for item in CACHE_DIR.glob("*"):
        if item.is_file() and not item.name.startswith("."):
            stat = item.stat()
            files.append((stat.st_atime, stat.st_size, item))
    total = sum(size for _, size, _ in files)
    for _, size, item in sorted(files):
        if total <= limit_mb * 1024 * 1024:
            break
        item.unlink(missing_ok=True)
        total -= size


def pnm_to_png(data: bytes) -> bytes:
    """PNM (P4/P5/P6, вывод ddjvu) → PNG средствами zlib: браузеры PNM не показывают."""
    tokens: list[bytes] = []
    pos = 0
    needed = 3
    while len(tokens) < needed + 1:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        tokens.append(data[start:pos])
        if tokens[0] == b"P4":
            needed = 2
    pos += 1  # один пробельный символ после заголовка
    magic, width, height = tokens[0], int(tokens[1]), int(tokens[2])
    if magic == b"P4":
        row_len, depth, color = (width + 7) // 8, 1, 0
        pixels = bytes(255 - b for b in data[pos:pos + row_len * height])  # в PNM 1 — чёрный, в PNG 0 — чёрный
    elif magic in (b"P5", b"P6") and int(tokens[3]) <= 255:
        channels = 1 if magic == b"P5" else 3
        row_len, depth, color = width * channels, 8, 0 if channels == 1 else 2
        pixels = data[pos:pos + row_len * height]
    else:
        raise ValueError(f"неподдерживаемый PNM {magic!r}")
    if len(pixels) < row_len * height:
        raise ValueError("обрезанный PNM")
    raw = b"".join(b"\x00" + pixels[i * row_len:(i + 1) * row_len] for i in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([depth, color, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def djvu_page_count(path: Path, timeout: int) -> int:
    done = subprocess.run(["djvused", "-e", "n", str(path)], capture_output=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL)
    count = done.stdout.decode("ascii", errors="replace").strip()
    if done.returncode != 0 or not count.isdigit() or int(count) < 1:
        raise ValueError(f"djvused: {done.stderr.decode('utf-8', errors='replace')[-200:] or 'нет страниц'}")
    return int(count)


def djvu_page_png(cfg: dict[str, Any], book: sqlite3.Row, path: Path, page: int) -> bytes:
    """Страница DjVu картинкой (ddjvu → PNM → PNG) с кэшированием."""
    target = CACHE_DIR / f"djvu-{cache_key(book)}-p{page}-w{DJVU_PAGE_WIDTH}.png"
    with cache_lock(target.name):
        if target.is_file():
            os.utime(target)
            return target.read_bytes()
        done = subprocess.run(["ddjvu", "-format=pnm", f"-page={page}", f"-size={DJVU_PAGE_WIDTH}x{DJVU_PAGE_WIDTH * 3}",
                               str(path), "-"], capture_output=True, timeout=cfg["convert_timeout"], check=False,
                              stdin=subprocess.DEVNULL)
        if done.returncode != 0 or not done.stdout:
            raise ValueError(f"ddjvu: {done.stderr.decode('utf-8', errors='replace')[-200:]}")
        png = pnm_to_png(done.stdout)
        cache_store(target, png, cfg["cache_mb"])
        return png


def djvu_page_text(path: Path, page: int, timeout: int) -> str:
    done = subprocess.run(["djvutxt", f"--page={page}", str(path)], capture_output=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL)
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else ""


def unwrap_book(cfg: dict[str, Any], book: sqlite3.Row, raw: Path) -> Path:
    """Книга однокнижного zip для просмотра: распаковка в cache/ (пределы reader_max_mb и cache_mb, защита от бомб).

    Имя файла в кэше — из id/размера/mtime и формата из БД (только известные расширения), не из архива.
    """
    if book["fmt"] not in INNER_EXTENSIONS:
        raise ValueError(f"неизвестный формат {book['fmt']!r}")
    target = CACHE_DIR / f"unzip-{cache_key(book)}.{book['fmt']}"
    with cache_lock(target.name):
        if target.is_file():
            os.utime(target)
            return target
        with zipfile.ZipFile(raw) as archive:
            inner = single_member(archive)
            if inner is None or member_ext(inner) != book["fmt"]:
                raise ValueError("архив изменился после сканирования")
            data = read_member(archive, inner, cfg["reader_max_mb"] * 1024 * 1024)
        cache_store(target, data, cfg["cache_mb"])
        return target


def calibre_epub(cfg: dict[str, Any], book: sqlite3.Row, path: Path, create: bool = True) -> Path | None:
    """EPUB для просмотра MOBI/AZW/AZW3/DOC/DOCX/RTF/ODT/CHM: ebook-convert (Calibre) один раз, затем из кэша.

    Calibre работает во временном каталоге со своими каталогами настроек и кэша: в песочнице службы домашний
    каталог только для чтения. Результат проверяется так же, как у convert (verify_book).
    """
    target = CACHE_DIR / f"calibre-{cache_key(book)}.epub"
    with cache_lock(target.name):
        if target.is_file():
            os.utime(target)
            return target
        if not create:
            return None
        if not shutil.which("ebook-convert"):
            raise ValueError("не найден ebook-convert (Calibre)")
        with tempfile.TemporaryDirectory(prefix="books_library-view-") as tmp_name:
            tmp = Path(tmp_name)
            staged = tmp / f"book.{book['fmt']}"
            shutil.copyfile(path, staged)
            env = {**os.environ, "HOME": str(tmp), "CALIBRE_CONFIG_DIRECTORY": str(tmp / "config"),
                   "CALIBRE_CACHE_DIRECTORY": str(tmp / "cache"), "CALIBRE_TEMP_DIR": str(tmp / "temp"),
                   "XDG_CACHE_HOME": str(tmp / "xdg"), "QT_QPA_PLATFORM": "offscreen"}
            for sub in ("config", "cache", "temp", "xdg"):
                (tmp / sub).mkdir()
            result = tmp / "book.epub"
            try:
                done = subprocess.run(["ebook-convert", str(staged), str(result)], capture_output=True, env=env, cwd=tmp,
                                      timeout=cfg["convert_timeout"], check=False, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as error:
                raise ValueError(f"ebook-convert: тайм-аут {cfg['convert_timeout']} с") from error
            if done.returncode != 0 or not result.is_file():
                raise ValueError(f"ebook-convert: {done.stderr.decode('utf-8', errors='replace')[-300:] or done.returncode}")
            problem = verify_book(result, "epub")
            if problem:
                raise ValueError(f"ebook-convert: {problem}")
            cache_store(target, result.read_bytes(), cfg["cache_mb"])
        return target


# ======================================================================
# Веб-интерфейс
# ======================================================================

CSS = """
:root{--bg:#fbfaf7;--fg:#22211f;--mut:#6f6b63;--line:#e3dfd6;--acc:#2f5d8a;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#1b1c1e;--fg:#e6e3dc;--mut:#9a958b;--line:#34363a;--acc:#8db7e3;--card:#232427}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;flex-wrap:wrap;gap:.3rem 1rem;align-items:center;padding:.6rem 1rem;border-bottom:1px solid var(--line)}
header b{font-size:1.1rem}main{max-width:72rem;margin:0 auto;padding:1rem}
form.s{display:flex;flex-wrap:wrap;gap:.4rem;margin:.2rem 0 1rem}input,select,textarea,button{font:inherit;color:inherit;
background:var(--card);border:1px solid var(--line);border-radius:6px;padding:.3rem .5rem}input[name=q]{flex:1 1 16rem}
button{cursor:pointer}.list{list-style:none;padding:0;margin:0}.list li{display:flex;gap:.8rem;padding:.5rem 0;
border-bottom:1px solid var(--line)}.cv{width:48px;min-width:48px;height:68px;object-fit:cover;border-radius:3px;background:var(--line)}
.mut{color:var(--mut);font-size:.9em}.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 .35rem;
font-size:.8em;color:var(--mut);margin-right:.2rem}.cols{columns:18rem;column-gap:2rem}.cols a{display:block}
.book{display:flex;flex-wrap:wrap;gap:1.5rem}.book img{max-width:220px;border-radius:4px}.book .info{flex:1 1 20rem}
.pager{margin:1rem 0;display:flex;gap:.6rem;flex-wrap:wrap}table{border-collapse:collapse}td,th{padding:.2rem .8rem .2rem 0;text-align:left}
.letters a{display:inline-block;min-width:1.6rem}.reader{max-width:42rem;margin:0 auto;padding:1rem;font:1.15rem/1.65 Georgia,serif}
.reader img{max-width:100%}.reader h3,.reader h4{text-align:center}.bar{position:sticky;top:0;background:var(--bg);padding:.4rem 0;
border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:.8rem;font:14px system-ui,sans-serif}
"""

APP_JS = """
(function(){var r=document.querySelector('.reader');if(!r)return;var id=r.dataset.id,ch=r.dataset.ch||'';
var size=parseFloat(localStorage.getItem('fs')||'1.15');r.style.fontSize=size+'rem';
document.querySelectorAll('[data-fs]').forEach(function(b){b.onclick=function(){size=Math.max(.8,Math.min(2,size+parseFloat(b.dataset.fs)));
r.style.fontSize=size+'rem';localStorage.setItem('fs',size);return false}});
var p=parseFloat(r.dataset.progress||'0');if(p>0)window.scrollTo(0,p*(document.documentElement.scrollHeight-innerHeight));
var t=null;addEventListener('scroll',function(){clearTimeout(t);t=setTimeout(function(){var h=document.documentElement.scrollHeight-innerHeight;
var v=h>0?scrollY/h:0;fetch('/api/progress',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'id='+id+'&ch='+ch+'&p='+v.toFixed(4)})},800)})})();
"""

NAV = [("/", "Поиск"), ("/sections", "Разделы"), ("/authors", "Авторы"), ("/series", "Серии"), ("/genres", "Жанры"), ("/browse", "Папки"),
       ("/shelves", "Полки"), ("/recent", "Недавние"), ("/stats", "Статистика")]


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def genre_name(code: str) -> str:
    return GENRES.get(code, code)


def save_mark(conn: sqlite3.Connection, path: str, fields: dict[str, Any]) -> None:
    """Частичное обновление отметок книги (статус, оценка, избранное, заметка); проверка значений — ValueError."""
    clean: dict[str, Any] = {}
    if "status" in fields:
        if fields["status"] not in STATUSES:
            raise ValueError(f"status: допустимо {', '.join(repr(k) for k in STATUSES)}")
        clean["status"] = fields["status"]
    if "rating" in fields:
        if not isinstance(fields["rating"], int) or isinstance(fields["rating"], bool) or not 0 <= fields["rating"] <= 5:
            raise ValueError("rating: целое 0..5")
        clean["rating"] = fields["rating"]
    if "favorite" in fields:
        if not isinstance(fields["favorite"], bool):
            raise ValueError("favorite: true или false")
        clean["favorite"] = int(fields["favorite"])
    if "note" in fields:
        if not isinstance(fields["note"], str):
            raise ValueError("note: строка")
        clean["note"] = fields["note"][:10000]
    conn.execute("INSERT OR IGNORE INTO marks(path, updated) VALUES (?, ?)", (path, time.time()))
    for key, value in clean.items():  # имена колонок — только из фиксированного набора выше
        conn.execute(f"UPDATE marks SET {key}=?, updated=? WHERE path=?", (value, time.time(), path))


def create_shelf(conn: sqlite3.Connection, name: str) -> int:
    name = name.strip()[:100]
    if not name:
        raise ValueError("пустое имя полки")
    conn.execute("INSERT OR IGNORE INTO shelves(name) VALUES (?)", (name,))
    return conn.execute("SELECT id FROM shelves WHERE name=?", (name,)).fetchone()[0]


def set_shelves(conn: sqlite3.Connection, path: str, shelf_ids: list[int]) -> None:
    conn.execute("DELETE FROM shelf_items WHERE path=?", (path,))
    conn.executemany("INSERT OR IGNORE INTO shelf_items SELECT id, ? FROM shelves WHERE id=?", [(path, sid) for sid in shelf_ids])


def book_json(row: sqlite3.Row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k not in ("mtime",)}  # noqa: SIM118 — у sqlite3.Row нет __iter__ по ключам
    data["path"] = display_path(row["path"], row["fmt"])
    data["authors"] = [a for a in row["authors"].split("; ") if a]
    data["genres"] = [g for g in row["genres"].split("; ") if g]
    data["cover"] = bool(row["cover"])
    data["links"] = {"page": f"/book/{row['id']}", "file": f"/file/{row['id']}", "read": f"/read/{row['id']}",
                     "cover": f"/cover/{row['id']}" if row["cover"] else None}
    if is_wrapped(row["path"], row["fmt"]):
        data["links"]["raw"] = f"/api/books/{row['id']}/file"  # исходный однокнижный архив — только через API
    return data


API_HELP = {
    "GET /api/books": "поиск: q, author, series, genre, section, collection, fmt, lang, status, shelf, fav=1, dir, sort, page, "
                      "per_page (≤500)",
    "GET /api/books/<id>": "книга с отметками и полками",
    "POST /api/books/<id>/mark": "{status?, rating?, favorite?, note?} — частичное обновление",
    "POST /api/books/<id>/shelves": "{shelf_ids: [..]} — заменить полки книги",
    "GET /api/authors | /api/series": "q (подстрока) или l (первая буква); не больше 1000",
    "GET /api/genres | /api/sections | /api/shelves | /api/recent | /api/stats": "справочники и статистика",
    "POST /api/shelves": "{name} — создать полку",
    "POST /api/shelves/<id>/delete": "удалить полку (книги не трогаются)",
    "GET /api/jobs, GET /api/jobs/<id>": "фоновые задания: состояние, вывод плана, результат",
    "POST /api/jobs": "{command: scan|migrate|fix-encoding|unpack-fb2zip|convert, dry_run: true (по умолчанию), "
                      "confirm: <id успешного dry-run> — обязателен для боевого fix-encoding/unpack-fb2zip/convert, "
                      "параметры режима: path, only, max_mb, to, overwrite, skip_newer, exclude, author, title_mode, toc, "
                      "toc_depth, delete_source}",
    "POST /api/progress": "позиция чтения (форма id, ch, p) — используется читалкой",
}


class Handler(BaseHTTPRequestHandler):
    """HTTP-обработчик. Все данные выдаются по id из БД: путь файла из запроса не принимается."""

    cfg: dict[str, Any] = {}
    server_version = "books_library"
    sys_version = ""

    # ---- служебное -------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — сигнатура базового класса
        LOG.debug(format, *args)

    def db(self) -> sqlite3.Connection:
        return connect(self.cfg["db_path"])

    def authorized(self) -> bool:
        auth = self.cfg["auth"]
        if not auth.get("password"):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                return False
            return hmac.compare_digest(user, auth["user"]) & hmac.compare_digest(password, auth["password"])
        return False

    def same_origin(self) -> bool:
        """Защита POST от CSRF: Origin/Referer должен совпадать с Host."""
        source = self.headers.get("Origin") or self.headers.get("Referer") or ""
        return bool(source) and urlsplit(source).netloc == self.headers.get("Host", "")

    def send_body(self, body: bytes, ctype: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline';"
                             " script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def page(self, title: str, body: str, status: int = 200) -> None:
        nav = " ".join(f'<a href="{href}">{label}</a>' for href, label in NAV)
        doc = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
               f'initial-scale=1"><title>{e(title)} — Библиотека</title><style>{CSS}</style></head><body><header>'
               f'<b><a href="/">Библиотека</a></b>{nav}</header><main>{body}</main><script src="/app.js"></script></body></html>')
        self.send_body(doc.encode("utf-8"), "text/html; charset=utf-8", status)

    def error_page(self, status: int, message: str) -> None:
        self.page("Ошибка", f"<h2>{e(message)}</h2>", status)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8", status, headers={"Cache-Control": "no-store"})

    def book_or_404(self, conn: sqlite3.Connection, raw_id: str) -> sqlite3.Row | None:
        if not raw_id.isdigit():
            return None
        return conn.execute("SELECT * FROM books WHERE id=?", (int(raw_id),)).fetchone()

    def raw_path(self, book: sqlite3.Row) -> Path | None:
        """Полный путь файла книги с проверкой, что он внутри каталога книг (защита от обхода каталогов)."""
        root = Path(self.cfg["books_dir"]).resolve()
        full = (root / book["path"]).resolve()
        return full if full.is_relative_to(root) and full.is_file() else None

    def book_path(self, book: sqlite3.Row) -> Path | None:
        """Файл книги для просмотра: у однокнижного zip — распакованная копия в кэше."""
        full = self.raw_path(book)
        if full is None or not is_wrapped(book["path"], book["fmt"]):
            return full
        try:
            return unwrap_book(self.cfg, book, full)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
            say(f"не распакован {book['path']}", "unwrap_error", logging.WARNING, detail=str(error))
            return None

    # ---- маршрутизация ---------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802 — имя задано http.server
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        self.dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.dispatch("POST")

    def dispatch(self, method: str) -> None:
        if not self.authorized():
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="books", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # http.server декодирует строку запроса как latin-1: сырые UTF-8 байты (curl без процент-кодирования)
        # возвращаются в исходный вид, процент-кодированные URL браузера это не меняет.
        try:
            raw_path = self.path.encode("latin-1").decode("utf-8")
        except UnicodeError:
            raw_path = self.path
        url = urlsplit(raw_path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        self.form: dict[str, list[str]] = {}
        self.json_body: dict[str, Any] = {}
        is_json = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json"
        if method == "POST":
            if not self.same_origin() and not (is_json and url.path.startswith("/api/")):
                self.error_page(HTTPStatus.FORBIDDEN, "Запрос с чужой страницы отклонён")
                return
            raw_length = self.headers.get("Content-Length") or "0"
            length = int(raw_length) if raw_length.isdigit() else -1
            if not 0 <= length <= 100_000:
                self.error_page(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Слишком большой запрос")
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            if is_json:
                try:
                    parsed = json.loads(body or "{}")
                except ValueError:
                    parsed = None
                if not isinstance(parsed, dict):
                    self.send_json({"error": "тело запроса — JSON-объект"}, HTTPStatus.BAD_REQUEST)
                    return
                self.json_body = parsed
            else:
                self.form = parse_qs(body, keep_blank_values=True)
        parts = [unquote(p) for p in url.path.split("/") if p]
        route = (method, parts[0] if parts else "")
        handlers: dict[tuple[str, str], Callable[[list[str]], None]] = {
            ("GET", ""): self.view_search, ("GET", "book"): self.view_book, ("GET", "read"): self.view_read,
            ("GET", "file"): self.view_file, ("GET", "cover"): self.view_cover, ("GET", "res"): self.view_res, ("GET", "page"): self.view_djvu_page,
            ("GET", "zip"): self.view_zip,
            ("GET", "authors"): self.view_authors, ("GET", "sections"): self.view_sections, ("GET", "series"): self.view_series, ("GET", "genres"): self.view_genres,
            ("GET", "browse"): self.view_browse, ("GET", "shelves"): self.view_shelves, ("GET", "recent"): self.view_recent,
            ("GET", "stats"): self.view_stats, ("GET", "opds"): self.view_opds, ("GET", "app.js"): self.view_js,
            ("POST", "book"): self.post_book, ("POST", "shelves"): self.post_shelves, ("POST", "scan"): self.post_scan,
            ("GET", "api"): self.api_get, ("POST", "api"): self.api_post,
        }
        handler = handlers.get(route)
        if handler is None:
            self.error_page(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        try:
            handler(parts[1:])
        except sqlite3.OperationalError as error:
            say(f"ошибка БД: {error}", "db_error", logging.ERROR)
            if route[1] == "api":
                self.send_json({"error": "база недоступна или не создана"}, HTTPStatus.SERVICE_UNAVAILABLE)
            else:
                self.error_page(HTTPStatus.SERVICE_UNAVAILABLE, "База недоступна или не создана: запустите сканирование")
        except (ConnectionError, TimeoutError):
            pass

    def view_js(self, _parts: list[str]) -> None:
        self.send_body(APP_JS.encode("utf-8"), "text/javascript; charset=utf-8", headers={"Cache-Control": "max-age=3600"})

    # ---- поиск и списки --------------------------------------------
    def search_rows(self, conn: sqlite3.Connection, params: dict[str, str], limit: int, offset: int) -> tuple[int, list[sqlite3.Row]]:
        joins, where, args = ["LEFT JOIN marks m ON m.path=b.path"], [], []
        order = SORTS.get(params.get("sort", ""), "")
        match = fts_query(params.get("q", ""))
        if match:
            joins.insert(0, "JOIN (SELECT rowid AS rid, rank FROM books_fts WHERE books_fts MATCH ?) f ON f.rid=b.id")
            args.append(match)
            order = order or "f.rank"
        filters = {
            "author": "b.id IN (SELECT book_id FROM book_authors WHERE name=?)",
            "genre": "b.id IN (SELECT book_id FROM book_genres WHERE name=?)",
            "series": "b.series=?", "fmt": "b.fmt=?", "section": "b.section=?", "collection": "b.collection=?", "lang": "b.lang=?", "status": "m.status=?",
            "shelf": "b.path IN (SELECT path FROM shelf_items WHERE shelf_id=?)",
        }
        for key, clause in filters.items():
            if params.get(key):
                where.append(clause)
                args.append(params[key])
        if params.get("fav"):
            where.append("m.favorite=1")
        if params.get("dir"):
            where.append("b.path LIKE ? ESCAPE '\\'")
            args.append(re.sub(r"([\\%_])", r"\\\1", params["dir"].strip("/")) + "/%")
        sql = f"FROM books b {' '.join(joins)} {'WHERE ' + ' AND '.join(where) if where else ''}"
        total = conn.execute(f"SELECT count(*) {sql}", args).fetchone()[0]
        rows = conn.execute(f"SELECT b.*, m.status, m.rating, m.favorite {sql} ORDER BY {order or SORTS['title']} LIMIT ? OFFSET ?",
                            [*args, limit, offset]).fetchall()
        return total, rows

    def book_list(self, rows: list[sqlite3.Row]) -> str:
        items = []
        for row in rows:
            cover = (f'<img class="cv" src="/cover/{row["id"]}" alt="" loading="lazy">' if row["cover"]
                     else '<span class="cv"></span>')
            authors = ", ".join(f'<a href="/?author={quote(a)}">{e(a)}</a>' for a in row["authors"].split("; ") if a)
            series = (f' · <a href="/?series={quote(row["series"])}&sort=series">{e(row["series"])}'
                      f'{" #" + str(row["series_no"]) if row["series_no"] else ""}</a>') if row["series"] else ""
            marks = (" ★" if row["favorite"] else "") + (f' <span class="tag">{e(STATUSES.get(row["status"], ""))}</span>'
                                                         if row["status"] else "")
            items.append(f'<li>{cover}<div><a href="/book/{row["id"]}"><b>{e(row["title"])}</b></a>{marks}<br>'
                         f'{authors}{series}<br><span class="tag">{e(row["fmt"])}</span>{self.section_tag(row)}<span class="mut">'
                         f'{e(row["year"])} {human_size(row["size"])}</span></div></li>')
        return f'<ul class="list">{"".join(items)}</ul>' if items else "<p>Ничего не найдено.</p>"

    def pager(self, total: int, page: int, size: int, params: dict[str, str]) -> str:
        pages = max((total + size - 1) // size, 1)
        links = []
        for number, label in ((page - 1, "← назад"), (page + 1, "вперёд →")):
            if 1 <= number <= pages:
                links.append(f'<a href="?{e(urlencode({**params, "page": number}))}">{label}</a>')
        return f'<div class="pager"><span class="mut">Найдено {total}, стр. {page} из {pages}</span>{" ".join(links)}</div>'

    def view_search(self, _parts: list[str]) -> None:
        params = {k: v for k, v in self.query.items() if k != "page" and v}
        page = int(self.query["page"]) if self.query.get("page", "").isdigit() else 1
        page = max(page, 1)
        size = self.cfg["page_size"]
        with closing(self.db()) as conn:
            total, rows = self.search_rows(conn, params, size, (page - 1) * size)
            fmts = [r[0] for r in conn.execute("SELECT DISTINCT fmt FROM books ORDER BY fmt")]
            sections = [r[0] for r in conn.execute("SELECT DISTINCT section FROM books ORDER BY section")]
            shelves = conn.execute("SELECT id, name FROM shelves ORDER BY name").fetchall()

        def options(name: str, values: list[tuple[str, str]], empty: str) -> str:
            opts = "".join(f'<option value="{e(v)}"{" selected" if params.get(name) == v else ""}>{e(t)}</option>' for v, t in values)
            return f'<select name="{name}"><option value="">{empty}</option>{opts}</select>'

        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(params[k])}">'
                         for k in ("author", "series", "genre", "dir", "lang", "collection") if params.get(k))
        sort_names = {"title": "по названию", "author": "по автору", "series": "по серии", "added": "новые",
                      "year": "по году", "size": "по размеру", "path": "по папкам (выпуски)"}
        form = (f'<form class="s" action="/">{hidden}<input name="q" value="{e(params.get("q", ""))}" placeholder='
                f'"Название, автор, серия, жанр, аннотация" autofocus>'
                f'{options("section", [(x, x or FICTION_SECTION) for x in sections], "все разделы")}'
                f'{options("fmt", [(f, f) for f in fmts], "все форматы")}'
                f'{options("status", [(k, v) for k, v in STATUSES.items() if k], "любой статус")}'
                f'{options("shelf", [(str(s["id"]), s["name"]) for s in shelves], "все полки")}'
                f'{options("sort", list(sort_names.items()), "по релевантности")}'
                f'<label><input type="checkbox" name="fav" value="1"{" checked" if params.get("fav") else ""}> ★</label>'
                f'<button>Найти</button></form>')
        active = " ".join(f'<span class="tag">{e(k)}: {e(genre_name(v) if k == "genre" else v)}</span>'
                          for k, v in params.items() if k in {"author", "series", "genre", "dir", "lang", "collection"})
        if active:
            active += ' <a href="/">сбросить</a>'
        self.page(params.get("q") or "Поиск", form + active + self.pager(total, page, size, params) + self.book_list(rows)
                  + self.pager(total, page, size, params))

    def grouped(self, title: str, sql: str, link: Callable[[str], str], label: Callable[[str], str] = str) -> None:
        """Списки авторов и серий: по первой букве и по подстроке."""
        letter, needle = self.query.get("l", ""), self.query.get("q", "").strip()
        with closing(self.db()) as conn:
            letters = [r[0] for r in conn.execute(f"SELECT DISTINCT substr(k, 1, 1) FROM ({sql}) WHERE k <> '' ORDER BY 1")]
            if needle:
                rows = conn.execute(f"SELECT k, c FROM ({sql}) WHERE k LIKE ? ORDER BY k LIMIT 1000", (f"%{needle}%",)).fetchall()
            else:
                letter = letter or (letters[0] if letters else "")
                rows = conn.execute(f"SELECT k, c FROM ({sql}) WHERE substr(k, 1, 1)=? ORDER BY k", (letter,)).fetchall()
        bar = " ".join(f'<a href="?l={quote(x)}">{"<b>" + e(x) + "</b>" if x == letter and not needle else e(x)}</a>' for x in letters)
        items = "".join(f'<a href="{link(r[0])}">{e(label(r[0]))} <span class="mut">{r[1]}</span></a>' for r in rows)
        self.page(title, f'<h2>{title}</h2><form class="s"><input name="q" value="{e(needle)}" placeholder="Найти">'
                         f'<button>Найти</button></form><p class="letters">{bar}</p><div class="cols">{items}</div>')

    def view_authors(self, _parts: list[str]) -> None:
        self.grouped("Авторы", "SELECT name AS k, count(*) AS c FROM book_authors GROUP BY name", lambda a: f"/?author={quote(a)}")

    def view_series(self, _parts: list[str]) -> None:
        self.grouped("Серии", "SELECT series AS k, count(*) AS c FROM books WHERE series <> '' GROUP BY series",
                     lambda s: f"/?series={quote(s)}&sort=series")

    @staticmethod
    def section_tag(row: sqlite3.Row) -> str:
        if not row["section"]:
            return ""
        link = f'/?section={quote(row["section"])}'
        tag = f'<a class="tag" href="{link}">{e(row["section"])}</a>'
        if row["collection"]:
            tag += f'<a class="tag" href="{link}&collection={quote(row["collection"])}&sort=path">{e(row["collection"])}</a>'
        return tag

    def view_sections(self, _parts: list[str]) -> None:
        """Разделы (папки «_…») с коллекциями: журналы, сборники, документация, неразобранное."""
        with closing(self.db()) as conn:
            rows = conn.execute("SELECT section, collection, count(*) FROM books GROUP BY section, collection "
                                "ORDER BY section = '', section, collection").fetchall()
        blocks: dict[str, list[tuple[str, int]]] = {}
        for section, collection, count in rows:
            blocks.setdefault(section, []).append((collection, count))
        html_parts = []
        for section, items in blocks.items():
            total = sum(c for _, c in items)
            head = (f'<h3><a href="/?section={quote(section)}">{e(section or FICTION_SECTION)}</a> '
                    f'<span class="mut">{total}</span></h3>')
            if section == "":
                html_parts.append(head + '<p><a href="/authors">по авторам</a></p>')
                continue
            links = "".join(f'<a href="/?section={quote(section)}&collection={quote(c)}&sort=path">{e(c)} '
                            f'<span class="mut">{n}</span></a>' for c, n in sorted(items, key=lambda x: -x[1]) if c)
            html_parts.append(f'{head}<div class="cols">{links}</div>')
        self.page("Разделы", "<h2>Разделы</h2>" + ("".join(html_parts) or "<p>Каталог пуст.</p>"))

    def view_genres(self, _parts: list[str]) -> None:
        with closing(self.db()) as conn:
            rows = conn.execute("SELECT name, count(*) FROM book_genres GROUP BY name ORDER BY count(*) DESC").fetchall()
        items = "".join(f'<a href="/?genre={quote(r[0])}">{e(genre_name(r[0]))} <span class="mut">{r[1]}</span></a>' for r in rows)
        self.page("Жанры", f'<h2>Жанры</h2><div class="cols">{items or "Жанров пока нет."}</div>')

    def view_browse(self, _parts: list[str]) -> None:
        """Навигация по папкам библиотеки (только чтение, путь проверяется внутри каталога книг)."""
        root = Path(self.cfg["books_dir"]).resolve()
        rel = self.query.get("dir", "").strip("/")
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_dir():
            self.error_page(HTTPStatus.NOT_FOUND, "Папка не найдена")
            return
        try:
            entries = sorted(os.scandir(target), key=lambda x: x.name.casefold())
        except OSError as error:
            self.error_page(HTTPStatus.SERVICE_UNAVAILABLE, f"Папка не читается: {error.strerror}")
            return
        dirs = [x.name for x in entries if x.is_dir(follow_symlinks=False) and x.name not in SKIP_DIRS
                and not x.name.startswith(".") and not x.name.endswith(HTML_SIDECAR_SUFFIXES)]
        base = target.relative_to(root).as_posix()
        base = "" if base == "." else base
        files = [f"{base}/{x.name}" if base else x.name for x in entries if x.is_file(follow_symlinks=False)]
        with closing(self.db()) as conn:
            ids = {}
            for start in range(0, len(files), 500):
                chunk = files[start:start + 500]
                ids.update((r[0], (r[1], r[2])) for r in
                           conn.execute(f"SELECT path, id, fmt FROM books WHERE path IN ({','.join('?' * len(chunk))})", chunk))
        crumbs, acc = ['<a href="/browse">Книги</a>'], []
        for piece in PurePosixPath(base).parts:
            acc.append(piece)
            crumbs.append(f'<a href="/browse?dir={quote("/".join(acc))}">{e(piece)}</a>')
        dir_links = "".join(f'<a href="/browse?dir={quote(f"{base}/{d}" if base else d)}">📁 {e(d)}</a>' for d in dirs)
        file_links = "".join(f'<a href="/book/{ids[f][0]}">{e(PurePosixPath(display_path(f, ids[f][1])).name)}</a>'
                             for f in files if f in ids)
        more = f' · <a href="/?dir={quote(base)}">книги папки поиском</a>' if base else ""
        self.page(base or "Папки", f'<h2>{" / ".join(crumbs)}</h2><p class="mut">Папок {len(dirs)}, книг {len(ids)}{more}</p>'
                                   f'<div class="cols">{dir_links}{file_links}</div>')

    def view_recent(self, _parts: list[str]) -> None:
        with closing(self.db()) as conn:
            rows = conn.execute("SELECT b.*, m.status, m.rating, m.favorite FROM marks m JOIN books b ON b.path=m.path "
                                "WHERE m.opened IS NOT NULL ORDER BY m.opened DESC LIMIT 100").fetchall()
        self.page("Недавние", "<h2>Недавно открытые</h2>" + self.book_list(rows))

    def view_stats(self, _parts: list[str]) -> None:
        with closing(self.db()) as conn:
            total = conn.execute("SELECT count(*), coalesce(sum(size), 0) FROM books").fetchone()
            fmts = conn.execute("SELECT fmt, count(*), sum(size) FROM books GROUP BY fmt ORDER BY 2 DESC").fetchall()
            statuses = conn.execute("SELECT status, count(*) FROM marks WHERE status <> '' GROUP BY status").fetchall()
            authors = conn.execute("SELECT count(DISTINCT name) FROM book_authors").fetchone()[0]
            last = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        rows = "".join(f"<tr><td>{e(r[0])}</td><td>{r[1]}</td><td>{human_size(r[2] or 0)}</td></tr>" for r in fmts)
        marks = "".join(f"<tr><td>{e(STATUSES.get(r[0], r[0]))}</td><td><a href='/?status={e(r[0])}'>{r[1]}</a></td></tr>"
                        for r in statuses)
        with JOBS_LOCK:
            jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)[:10]
        running = any(j["command"] == "scan" and j["state"] in ("queued", "running") for j in jobs)
        progress = f"<p>Идёт сканирование: {e(SCAN_STATE['stage'])} {SCAN_STATE['done']}/{SCAN_STATE['total']}. " \
                   f"Обновите страницу.</p>" if running else ""
        job_rows = "".join(f"<tr><td>{e(j['created'])}</td><td><a href='/api/jobs/{e(j['id'])}'>{e(j['command'])}</a>"
                           f"{' (dry-run)' if j['dry_run'] else ''}</td><td>{e(j['state'])}</td>"
                           f"<td class='mut'>{e(json.dumps(j['result'] or j['error'], ensure_ascii=False))}</td></tr>" for j in jobs)
        scan_info = (f'{progress}<form method="post" action="/scan"><button{" disabled" if running else ""}>Пересканировать '
                     f'библиотеку</button></form>' + (f"<h3>Задания</h3><table>{job_rows}</table>" if job_rows else ""))
        self.page("Статистика", f"<h2>Статистика</h2><p>Книг: <b>{total[0]}</b> ({human_size(total[1])}), авторов: {authors}. "
                                f"Каталог: {e(self.cfg['books_dir'])}. Последнее сканирование: {e(last[0] if last else 'не было')}.</p>"
                                f"<table><tr><th>Формат</th><th>Книг</th><th>Объём</th></tr>{rows}</table>"
                                f"<h3>Чтение</h3><table>{marks or '<tr><td>отметок пока нет</td></tr>'}</table>"
                                f"<h3>Каталог</h3>{scan_info}<p class='mut'>OPDS для читалок: /opds · JSON API: <a href='/api'>/api</a></p>")

    # ---- книга -----------------------------------------------------
    def view_book(self, parts: list[str]) -> None:
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
            if not book:
                self.error_page(HTTPStatus.NOT_FOUND, "Книга не найдена")
                return
            mark = conn.execute("SELECT * FROM marks WHERE path=?", (book["path"],)).fetchone()
            shelves = conn.execute("SELECT s.id, s.name, i.path IS NOT NULL AS has FROM shelves s LEFT JOIN shelf_items i "
                                   "ON i.shelf_id=s.id AND i.path=? ORDER BY s.name", (book["path"],)).fetchall()
            same = []
            if book["series"]:
                same = conn.execute("SELECT b.*, m.status, m.rating, m.favorite FROM books b LEFT JOIN marks m ON m.path=b.path "
                                    "WHERE b.series=? AND b.id<>? ORDER BY b.series_no, b.title LIMIT 50",
                                    (book["series"], book["id"])).fetchall()
        bid = book["id"]
        authors = ", ".join(f'<a href="/?author={quote(a)}">{e(a)}</a>' for a in book["authors"].split("; ") if a) or "—"
        genres = " ".join(f'<a class="tag" href="/?genre={quote(g)}">{e(genre_name(g))}</a>' for g in book["genres"].split("; ") if g)
        series = (f'<p>Серия: <a href="/?series={quote(book["series"])}&sort=series">{e(book["series"])}</a>'
                  f'{" #" + str(book["series_no"]) if book["series_no"] else ""}</p>') if book["series"] else ""
        if book["fmt"] == "pdf":
            actions = [f'<a href="/file/{bid}?inline=1"><b>Открыть PDF</b></a>']
        elif book["fmt"] == "zip":
            actions = [f'<a href="/read/{bid}"><b>Содержимое архива</b></a>']
        else:
            actions = [f'<a href="/read/{bid}"><b>Читать</b></a>']
        actions.append(f'<a href="/file/{bid}">Скачать ({human_size(book["size"])})</a>')
        folder = str(PurePosixPath(book["path"]).parent)
        actions.append(f'<a href="/browse?dir={quote(folder if folder != "." else "")}">Папка</a>')
        status = mark["status"] if mark else ""
        rating = mark["rating"] if mark else 0
        status_opts = "".join(f'<option value="{k}"{" selected" if k == status else ""}>{v}</option>' for k, v in STATUSES.items())
        rating_opts = "".join(f'<option value="{n}"{" selected" if n == rating else ""}>{"★" * n or "без оценки"}</option>'
                              for n in range(6))
        shelf_boxes = "".join(f'<label><input type="checkbox" name="shelf" value="{s["id"]}"{" checked" if s["has"] else ""}> '
                              f'{e(s["name"])}</label> ' for s in shelves)
        progress = ""
        if mark and mark["progress"]:
            progress = (f' · {"страница" if book["fmt"] == "djvu" else "глава"} {int(mark["progress"]) + 1}'
                        if book["fmt"] in PAGED_FORMATS
                        else f' · прочитано {mark["progress"] * 100:.0f} %')
        cover = f'<img src="/cover/{bid}" alt="Обложка">' if book["cover"] else ""
        annotation = "".join(f"<p>{e(p)}</p>" for p in book["annotation"].split("\n") if p)
        body = (f'<div class="book">{cover}<div class="info"><h2>{e(book["title"])}</h2><p>{authors}</p>{series}<p>{genres}</p>'
                f'<p class="mut">{e(book["fmt"])} · {e(book["lang"])} · {e(book["year"])}{progress}<br>'
                f'{e(display_path(book["path"], book["fmt"]))}</p>'
                f'<p>{" · ".join(actions)}</p>{annotation}'
                f'<form method="post" action="/book/{bid}/mark"><p><select name="status">{status_opts}</select> '
                f'<select name="rating">{rating_opts}</select> <label><input type="checkbox" name="favorite" value="1"'
                f'{" checked" if mark and mark["favorite"] else ""}> ★ избранное</label></p>'
                f'<p><textarea name="note" rows="3" cols="50" placeholder="Заметка">{e(mark["note"] if mark else "")}</textarea></p>'
                f'<p>Полки: {shelf_boxes or "<span class=mut>нет полок</span>"} <input name="new_shelf" placeholder="новая полка">'
                f'</p><button>Сохранить</button></form></div></div>')
        if same:
            body += "<h3>Другие книги серии</h3>" + self.book_list(same)
        self.page(book["title"], body)

    def post_book(self, parts: list[str]) -> None:
        if len(parts) != 2 or parts[1] != "mark":
            self.error_page(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        form = {k: v[-1] for k, v in self.form.items()}
        status = form.get("status", "")
        rating = int(form["rating"]) if form.get("rating", "").isdigit() else 0
        if status not in STATUSES or not 0 <= rating <= 5:
            self.error_page(HTTPStatus.BAD_REQUEST, "Неверные данные формы")
            return
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0])
            if not book:
                self.error_page(HTTPStatus.NOT_FOUND, "Книга не найдена")
                return
            shelf_ids = [int(x) for x in self.form.get("shelf", []) if x.isdigit()]
            with conn:
                save_mark(conn, book["path"], {"status": status, "rating": rating, "favorite": form.get("favorite") == "1",
                                               "note": form.get("note", "")})
                new_shelf = form.get("new_shelf", "").strip()[:100]
                if new_shelf:
                    shelf_ids.append(create_shelf(conn, new_shelf))
                set_shelves(conn, book["path"], shelf_ids)
        self.redirect(f"/book/{book['id']}")

    def view_shelves(self, _parts: list[str]) -> None:
        with closing(self.db()) as conn:
            shelves = conn.execute("SELECT s.id, s.name, count(i.path) FROM shelves s LEFT JOIN shelf_items i ON i.shelf_id=s.id "
                                   "GROUP BY s.id ORDER BY s.name").fetchall()
            favorites = conn.execute("SELECT count(*) FROM marks WHERE favorite=1").fetchone()[0]
        rows = "".join(f'<tr><td><a href="/?shelf={s[0]}">{e(s[1])}</a></td><td>{s[2]}</td><td><form method="post" '
                       f'action="/shelves"><input type="hidden" name="delete" value="{s[0]}"><button>удалить</button></form>'
                       f'</td></tr>' for s in shelves)
        self.page("Полки", f'<h2>Полки</h2><p><a href="/?fav=1">★ Избранное</a> ({favorites})</p><table>{rows}</table>'
                           f'<form class="s" method="post" action="/shelves"><input name="name" placeholder="Новая полка" '
                           f'required maxlength="100"><button>Создать</button></form><p class="mut">Удаление полки не трогает книги.</p>')

    def post_shelves(self, _parts: list[str]) -> None:
        form = {k: v[-1] for k, v in self.form.items()}
        with closing(self.db()) as conn, conn:
            if form.get("delete", "").isdigit():
                conn.execute("DELETE FROM shelves WHERE id=?", (int(form["delete"]),))
            elif form.get("name", "").strip():
                create_shelf(conn, form["name"].strip()[:100])
        self.redirect("/shelves")

    def post_scan(self, _parts: list[str]) -> None:
        try:
            start_job(self.cfg, "scan", False, {})
        except (ConfigError, ConfirmRequiredError) as error:
            say(f"сканирование не запущено: {error}", "scan_failed", logging.ERROR)
        self.redirect("/stats")

    def post_progress(self, parts: list[str]) -> None:
        form = {k: v[-1] for k, v in self.form.items()}
        try:
            progress = min(max(float(form.get("p", "0")), 0.0), 1.0)
        except ValueError:
            progress = 0.0
        if not form.get("id", "").isdigit():
            self.send_body(b"", "text/plain", HTTPStatus.BAD_REQUEST)
            return
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, form["id"])
            if book:
                chapter = int(form["ch"]) if form.get("ch", "").isdigit() else 0
                with conn:
                    conn.execute("INSERT INTO marks(path, progress, updated) VALUES (?,?,?) ON CONFLICT(path) DO UPDATE SET "
                                 "progress=excluded.progress, updated=excluded.updated",
                                 (book["path"], chapter + progress if book["fmt"] in PAGED_FORMATS else progress, time.time()))
        self.send_body(b"ok", "text/plain")

    # ---- JSON API ---------------------------------------------------
    def api_get(self, parts: list[str]) -> None:
        name = parts[0] if parts else ""
        if name == "books" and len(parts) == 3 and parts[2] == "file":
            self.view_file([parts[1]], raw=True)
            return
        if not name:
            self.send_json({"endpoints": API_HELP})
            return
        if name == "jobs":
            with JOBS_LOCK:
                if len(parts) > 1:
                    job = JOBS.get(parts[1])
                    self.send_json(job_view(job) if job else {"error": "задание не найдено"},
                                   HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
                else:
                    self.send_json([job_view(j, full=False) for j in sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)])
            return
        with closing(self.db()) as conn:
            if name == "books" and len(parts) > 1:
                book = self.book_or_404(conn, parts[1])
                if not book:
                    self.send_json({"error": "книга не найдена"}, HTTPStatus.NOT_FOUND)
                    return
                mark = conn.execute("SELECT status, rating, favorite, note, progress, opened FROM marks WHERE path=?",
                                    (book["path"],)).fetchone()
                shelves = conn.execute("SELECT s.id, s.name FROM shelves s JOIN shelf_items i ON i.shelf_id=s.id WHERE i.path=?",
                                       (book["path"],)).fetchall()
                self.send_json({**book_json(book), "mark": dict(mark) if mark else None, "shelves": [dict(x) for x in shelves]})
            elif name == "books":
                params = {k: v for k, v in self.query.items() if k not in ("page", "per_page") and v}
                page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
                size = min(max(int(self.query["per_page"]), 1), 500) if self.query.get("per_page", "").isdigit() \
                    else self.cfg["page_size"]
                total, rows = self.search_rows(conn, params, size, (page - 1) * size)
                self.send_json({"total": total, "page": page, "per_page": size, "items": [book_json(r) for r in rows]})
            elif name in ("authors", "series"):
                sql = ("SELECT name AS k, count(*) AS c FROM book_authors GROUP BY name" if name == "authors" else
                       "SELECT series AS k, count(*) AS c FROM books WHERE series <> '' GROUP BY series")
                needle, letter = self.query.get("q", ""), self.query.get("l", "")
                if needle:
                    rows = conn.execute(f"SELECT k, c FROM ({sql}) WHERE k LIKE ? ORDER BY k LIMIT 1000", (f"%{needle}%",))
                elif letter:
                    rows = conn.execute(f"SELECT k, c FROM ({sql}) WHERE substr(k, 1, 1)=? ORDER BY k LIMIT 1000", (letter,))
                else:
                    rows = conn.execute(f"SELECT k, c FROM ({sql}) ORDER BY c DESC LIMIT 1000")
                self.send_json([{"name": r[0], "count": r[1]} for r in rows])
            elif name == "genres":
                rows = conn.execute("SELECT name, count(*) FROM book_genres GROUP BY name ORDER BY count(*) DESC")
                self.send_json([{"code": r[0], "name": genre_name(r[0]), "count": r[1]} for r in rows])
            elif name == "sections":
                rows = conn.execute("SELECT section, collection, count(*) FROM books GROUP BY section, collection ORDER BY 1, 2")
                self.send_json([{"section": r[0] or FICTION_SECTION, "section_key": r[0], "collection": r[1], "count": r[2]}
                                for r in rows])
            elif name == "shelves":
                rows = conn.execute("SELECT s.id, s.name, count(i.path) AS count FROM shelves s LEFT JOIN shelf_items i "
                                    "ON i.shelf_id=s.id GROUP BY s.id ORDER BY s.name")
                self.send_json([dict(r) for r in rows])
            elif name == "recent":
                rows = conn.execute("SELECT b.* FROM marks m JOIN books b ON b.path=m.path WHERE m.opened IS NOT NULL "
                                    "ORDER BY m.opened DESC LIMIT 100")
                self.send_json([book_json(r) for r in rows])
            elif name == "stats":
                total = conn.execute("SELECT count(*), coalesce(sum(size), 0) FROM books").fetchone()
                fmts = conn.execute("SELECT fmt, count(*), sum(size) FROM books GROUP BY fmt ORDER BY 2 DESC").fetchall()
                statuses = conn.execute("SELECT status, count(*) FROM marks WHERE status <> '' GROUP BY status").fetchall()
                last = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
                self.send_json({"books": total[0], "bytes": total[1], "last_scan": last[0] if last else None,
                                "formats": {r[0]: {"books": r[1], "bytes": r[2]} for r in fmts},
                                "statuses": {r[0]: r[1] for r in statuses}, "scan": SCAN_STATE})
            else:
                self.send_json({"error": "неизвестный ресурс", "endpoints": API_HELP}, HTTPStatus.NOT_FOUND)

    def api_post(self, parts: list[str]) -> None:
        name = parts[0] if parts else ""
        if name == "progress":
            self.post_progress(parts)
            return
        body = self.json_body
        try:
            if name == "jobs" and len(parts) == 1:
                command = body.get("command", "")
                dry_run = body.get("dry_run", True)
                confirm = body.get("confirm", "") or ""
                if not isinstance(command, str) or not isinstance(dry_run, bool) or not isinstance(confirm, str):
                    raise ValueError("command — строка, dry_run — true/false, confirm — строка")
                raw = {k: v for k, v in body.items() if k not in ("command", "dry_run", "confirm")}
                job = start_job(self.cfg, command, dry_run, raw, confirm)
                self.send_json(job_view(job), HTTPStatus.ACCEPTED)
                return
            with closing(self.db()) as conn:
                if name == "books" and len(parts) == 3 and parts[2] in ("mark", "shelves"):
                    book = self.book_or_404(conn, parts[1])
                    if not book:
                        self.send_json({"error": "книга не найдена"}, HTTPStatus.NOT_FOUND)
                        return
                    with conn:
                        if parts[2] == "mark":
                            unknown = set(body) - {"status", "rating", "favorite", "note"}
                            if unknown:
                                raise ValueError(f"неизвестные поля: {', '.join(sorted(unknown))}")
                            save_mark(conn, book["path"], body)
                        else:
                            ids = body.get("shelf_ids")
                            if not isinstance(ids, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in ids):
                                raise ValueError("shelf_ids: список целых")
                            set_shelves(conn, book["path"], ids)
                    self.send_json({"ok": True, "id": book["id"]})
                elif name == "shelves" and len(parts) == 1:
                    if not isinstance(body.get("name"), str):
                        raise ValueError("name: строка")
                    with conn:
                        shelf_id = create_shelf(conn, body["name"])
                    self.send_json({"id": shelf_id, "name": body["name"].strip()[:100]}, HTTPStatus.CREATED)
                elif name == "shelves" and len(parts) == 3 and parts[1].isdigit() and parts[2] == "delete":
                    with conn:
                        deleted = conn.execute("DELETE FROM shelves WHERE id=?", (int(parts[1]),)).rowcount
                    self.send_json({"deleted": deleted})
                else:
                    self.send_json({"error": "неизвестный ресурс", "endpoints": API_HELP}, HTTPStatus.NOT_FOUND)
        except (ValueError, ConfigError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except ConfirmRequiredError as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)

    # ---- файлы, обложки, чтение -----------------------------------
    def view_file(self, parts: list[str], raw: bool = False) -> None:
        """Файл книги. Однокнижный zip отдаётся распакованным потоком (name.ext); raw — исходный файл (только API)."""
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
        full = self.raw_path(book) if book else None
        if not full:
            self.error_page(HTTPStatus.NOT_FOUND, "Файл не найден")
            return
        inline = self.query.get("inline") == "1" and book["fmt"] == "pdf"
        ctype = "application/pdf" if inline else "application/octet-stream"
        disposition = "inline" if inline else "attachment"
        with ExitStack() as stack:
            if is_wrapped(book["path"], book["fmt"]) and not raw:
                try:
                    archive = stack.enter_context(zipfile.ZipFile(full))
                    inner = single_member(archive)
                    if inner is None or member_ext(inner) != book["fmt"] or \
                            (inner.file_size > 1024 * 1024 and inner.file_size > ZIP_MAX_RATIO * max(inner.compress_size, 1)):
                        raise ValueError("архив изменился или подозрителен")
                    size, source = inner.file_size, stack.enter_context(archive.open(inner))
                except (OSError, ValueError, zipfile.BadZipFile, EOFError) as error:
                    self.error_page(HTTPStatus.UNPROCESSABLE_ENTITY, f"Архив не читается: {error}")
                    return
                name = PurePosixPath(display_path(book["path"], book["fmt"])).name
            else:
                size, source, name = full.stat().st_size, stack.enter_context(full.open("rb")), full.name
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quote(name)}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)
            except (zipfile.BadZipFile, EOFError) as error:  # заголовки уже отправлены: только журнал и обрыв
                say(f"обрыв выдачи {book['path']}", "stream_error", logging.WARNING, detail=str(error))
                self.close_connection = True

    def send_image(self, found: tuple[str, bytes] | None) -> None:
        ctype = safe_image_type(*found[:1]) if found else None
        if not found or not ctype:
            self.send_body(b"", "text/plain", HTTPStatus.NOT_FOUND)
            return
        self.send_body(found[1], ctype, headers={"Cache-Control": "max-age=86400"})

    def book_image(self, book: sqlite3.Row, ref: str | None) -> tuple[str, bytes] | None:
        """Картинка книги: ref — id бинарника fb2 или путь внутри EPUB; None — обложка."""
        full = self.book_path(book)
        if not full:
            return None
        limit = self.cfg["reader_max_mb"] * 1024 * 1024
        try:
            if book["fmt"] in {"fb2", "fb2.zip"}:
                raw = fb2_bytes(full, book["fmt"], limit)
                if ref is None:
                    head = re.search(rb"<(?:\w+:)?coverpage\b.*?href=[\"']#?([^\"']+)", raw[:FB2_HEAD_BYTES], re.S)
                    ref = head.group(1).decode("utf-8", errors="replace") if head else None
                return fb2_binary(raw, ref) if ref else None
            if book["fmt"] in CALIBRE_FORMATS:
                converted = calibre_epub(self.cfg, book, full, create=False)  # обложку ради списка не конвертируем
                if not converted:
                    return None
                full = converted
            if book["fmt"] == "epub" or book["fmt"] in CALIBRE_FORMATS:
                with zipfile.ZipFile(full) as archive:
                    epub = Epub(archive)
                    path = epub.cover_path() if ref is None else ref
                    if path and path in epub.names and (ctype := safe_image_type("", path)):
                        return ctype, zip_read(archive, path)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
            say(f"картинка не извлечена: {book['path']}", "image_error", logging.WARNING, detail=str(error))
        return None

    def view_cover(self, parts: list[str]) -> None:
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
        self.send_image(self.book_image(book, None) if book else None)

    def view_res(self, parts: list[str]) -> None:
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
        ref = self.query.get("p", "")
        self.send_image(self.book_image(book, ref) if book and ref else None)

    def view_read(self, parts: list[str]) -> None:
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
            if book:
                with conn:
                    conn.execute("INSERT INTO marks(path, opened, updated, status) VALUES (?,?,?, 'reading') ON CONFLICT(path) "
                                 "DO UPDATE SET opened=excluded.opened, status=CASE WHEN status IN ('', 'want') THEN 'reading' "
                                 "ELSE status END", (book["path"], time.time(), time.time()))
                mark = conn.execute("SELECT progress FROM marks WHERE path=?", (book["path"],)).fetchone()
        full = self.book_path(book) if book else None
        if not full:
            self.error_page(HTTPStatus.NOT_FOUND, "Книга не найдена")
            return
        bid, fmt = book["id"], book["fmt"]
        limit = self.cfg["reader_max_mb"] * 1024 * 1024
        saved = mark["progress"] if mark else 0.0
        chapter_nav, chapter, progress = "", "", saved
        try:
            if fmt in {"fb2", "fb2.zip"}:
                text = decode_xml(fb2_bytes(full, fmt, limit))
                start = text.find("<body")
                if start < 0:
                    raise ValueError("в fb2 нет <body>")
                end = text.find("<binary", start)
                content = sanitize(text[start:end if end > 0 else None], lambda ref: f"/res/{bid}?p={quote(ref.lstrip('#'))}", fb2=True)
            elif fmt == "txt":
                text = decode_text(read_limited(full, limit))
                blocks = re.split(r"\n\s*\n", text) if text.count("\n\n") > text.count("\n") / 10 else text.splitlines()
                content = "".join(f"<p>{e(b.strip())}</p>" for b in blocks if b.strip())
            elif fmt == "djvu":
                pages = djvu_page_count(full, self.cfg["convert_timeout"])
                if self.query.get("page", "").isdigit():
                    index, progress = int(self.query["page"]) - 1, 0.0
                elif self.query.get("ch", "").isdigit():
                    index, progress = int(self.query["ch"]), 0.0
                else:
                    index, progress = int(saved), saved - int(saved)
                index = min(max(index, 0), pages - 1)
                text = djvu_page_text(full, index + 1, self.cfg["convert_timeout"]).strip()
                text_block = f"<details><summary>Текст страницы</summary><pre>{e(text)}</pre></details>" if text else ""
                content = f'<p><img src="/page/{bid}/{index + 1}" alt="Страница {index + 1}"></p>{text_block}'
                chapter = str(index)
                prev_link = f'<a href="/read/{bid}?ch={index - 1}">← стр.</a>' if index > 0 else ""
                next_link = f'<a href="/read/{bid}?ch={index + 1}">стр. →</a>' if index < pages - 1 else ""
                jump = (f'<form class="s" action="/read/{bid}" style="display:inline"><input name="page" type="number" min="1" '
                        f'max="{pages}" value="{index + 1}" style="width:5em"><button>перейти</button></form>')
                chapter_nav = f"{prev_link} <span class='mut'>{index + 1}/{pages}</span> {next_link} {jump}"
            elif fmt == "zip":
                self.redirect(f"/zip/{bid}")
                return
            elif fmt == "epub" or fmt in CALIBRE_FORMATS:
                if fmt in CALIBRE_FORMATS:
                    converted = calibre_epub(self.cfg, book, full)
                    if converted is None:
                        raise ValueError("EPUB для просмотра не создан")
                    full = converted
                with zipfile.ZipFile(full) as archive:
                    epub = Epub(archive)
                    spine = epub.spine()
                    if not spine:
                        raise ValueError("пустое оглавление EPUB")
                    if self.query.get("ch", "").isdigit():
                        index, progress = int(self.query["ch"]), 0.0
                    else:
                        index, progress = int(saved), saved - int(saved)
                    index = min(max(index, 0), len(spine) - 1)
                    base = posixpath.dirname(spine[index])

                    def image(src: str, base: str = base) -> str | None:
                        path = Epub.resolve(base, src)
                        return f"/res/{bid}?p={quote(path)}" if path in epub.names else None

                    content = sanitize(decode_xml(zip_read(archive, spine[index])), image)
                chapter = str(index)
                prev_link = f'<a href="/read/{bid}?ch={index - 1}">← глава</a>' if index > 0 else ""
                next_link = f'<a href="/read/{bid}?ch={index + 1}">глава →</a>' if index < len(spine) - 1 else ""
                chapter_nav = f"{prev_link} <span class='mut'>{index + 1}/{len(spine)}</span> {next_link}"
            else:
                self.redirect(f"/file/{bid}?inline=1" if fmt == "pdf" else f"/book/{bid}")
                return
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError, subprocess.TimeoutExpired) as error:
            self.error_page(HTTPStatus.UNPROCESSABLE_ENTITY, f"Книгу не удалось открыть в браузере: {error}. Скачайте файл.")
            return
        bar = (f'<div class="bar"><a href="/book/{bid}">← {e(book["title"])}</a> {chapter_nav} '
               f'<a href="#" data-fs="-0.1">A−</a> <a href="#" data-fs="0.1">A+</a></div>')
        body = (f'{bar}<article class="reader" data-id="{bid}" data-ch="{e(chapter)}" data-progress="{progress:.4f}">'
                f'{content}</article>{bar if chapter_nav else ""}')
        self.page(book["title"], body)

    def view_djvu_page(self, parts: list[str]) -> None:
        """Картинка страницы DjVu: /page/<id>/<номер с 1>."""
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
        full = self.book_path(book) if book and book["fmt"] == "djvu" else None
        if not full or len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 1:
            self.send_body(b"", "text/plain", HTTPStatus.NOT_FOUND)
            return
        try:
            png = djvu_page_png(self.cfg, book, full, int(parts[1]))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            say(f"страница DjVu не получена: {book['path']}", "djvu_error", logging.WARNING, detail=str(error))
            self.send_body(b"", "text/plain", HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        self.send_body(png, "image/png", headers={"Cache-Control": "max-age=86400"})

    def view_zip(self, parts: list[str]) -> None:
        """Содержимое ZIP: список файлов (/zip/<id>) и просмотр файла из архива (/zip/<id>?e=<имя>)."""
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if parts else "")
        full = self.book_path(book) if book and book["fmt"] in {"zip", "fb2.zip"} else None
        if not full:
            self.error_page(HTTPStatus.NOT_FOUND, "Архив не найден")
            return
        bid, entry = book["id"], self.query.get("e", "")
        limit = self.cfg["reader_max_mb"] * 1024 * 1024
        try:
            with zipfile.ZipFile(full) as archive:
                infos = [i for i in archive.infolist() if not i.is_dir()]
                if not entry:
                    rows = "".join(
                        f'<tr><td><a href="/zip/{bid}?e={quote(i.filename)}">{e(i.filename)}</a></td>'
                        f'<td class="mut">{human_size(i.file_size)}</td><td><a href="/zip/{bid}?e={quote(i.filename)}&dl=1">'
                        f'скачать</a></td></tr>' for i in infos[:3000])
                    more = f"<p class='mut'>Показаны первые 3000 из {len(infos)}.</p>" if len(infos) > 3000 else ""
                    self.page(book["title"], f'<p><a href="/book/{bid}">← {e(book["title"])}</a></p><h2>Содержимое архива '
                                             f'({len(infos)})</h2>{more}<div style="overflow-x:auto"><table>{rows}</table></div>')
                    return
                info = next((i for i in infos if i.filename == entry), None)
                if info is None:
                    self.error_page(HTTPStatus.NOT_FOUND, "Файла нет в архиве")
                    return
                data = zip_read(archive, info.filename, limit)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
            self.error_page(HTTPStatus.UNPROCESSABLE_ENTITY, f"Архив не читается: {error}")
            return
        name = PurePosixPath(info.filename.replace("\\", "/")).name or "file"
        ext = name.rpartition(".")[2].lower()
        back = f'<div class="bar"><a href="/zip/{bid}">← содержимое архива</a> <span class="mut">{e(info.filename)}</span></div>'
        if self.query.get("dl") == "1":
            self.send_body(data, "application/octet-stream",
                           headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        elif (ctype := safe_image_type("", name)) is not None:
            self.send_body(data, ctype, headers={"Cache-Control": "max-age=86400"})
        elif ext == "pdf":
            self.send_body(data, "application/pdf", headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})
        elif ext == "fb2":
            text = decode_xml(data)
            start = max(text.find("<body"), 0)
            end = text.find("<binary", start)
            body = sanitize(text[start:end if end > 0 else None], lambda _ref: None, fb2=True)
            self.page(name, f'{back}<article class="reader">{body}</article>')
        elif ext in {"htm", "html", "xhtml"}:
            self.page(name, f'{back}<article class="reader">{sanitize(decode_part(data, None), lambda _ref: None)}</article>')
        elif ext in TEXT_EXTENSIONS or b"\x00" not in data[:4096]:
            self.page(name, f'{back}<pre class="reader" style="white-space:pre-wrap">{e(decode_text(data))}</pre>')
        else:
            self.send_body(data, "application/octet-stream",
                           headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    # ---- OPDS для читалок -----------------------------------------
    def view_opds(self, _parts: list[str]) -> None:
        """OPDS 1.2: поиск (?q=) и новые поступления; ссылки на скачивание и обложки."""
        host = self.headers.get("Host", "")
        query = self.query.get("q", "")
        with closing(self.db()) as conn:
            _total, rows = self.search_rows(conn, {"q": query, "sort": "" if query else "added"}, 100, 0)
        updated = datetime.now(UTC).isoformat(timespec="seconds")
        entries = []
        for row in rows:
            authors = "".join(f"<author><name>{e(a)}</name></author>" for a in row["authors"].split("; ") if a)
            cover = (f'<link rel="http://opds-spec.org/image" href="/cover/{row["id"]}" type="image/jpeg"/>' if row["cover"] else "")
            entries.append(f'<entry><title>{e(row["title"])}</title><id>urn:books:{row["id"]}</id><updated>{updated}</updated>'
                           f'{authors}<content type="text">{e(row["annotation"][:1000])}</content>{cover}'
                           f'<link rel="http://opds-spec.org/acquisition" href="/file/{row["id"]}" '
                           f'type="application/octet-stream"/></entry>')
        feed = (f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><id>urn:books:{e(host)}</id>'
                f'<title>Библиотека{": " + e(query) if query else ": новые"}</title><updated>{updated}</updated>'
                f'<link rel="search" type="application/atom+xml" href="/opds?q={{searchTerms}}"/>'
                f'<link rel="start" type="application/atom+xml;profile=opds-catalog" href="/opds"/>{"".join(entries)}</feed>')
        self.send_body(feed.encode("utf-8"), "application/atom+xml;profile=opds-catalog; charset=utf-8")


def serve(cfg: dict[str, Any], dry_run: bool = False) -> None:
    url = f"http://{cfg['host']}:{cfg['port']}/"
    if dry_run:
        print(f"[dry-run] будет: открыть веб-интерфейс {url}; каталог книг {cfg['books_dir']} (только чтение); БД {cfg['db_path']}")
        migrate_database(cfg["db_path"], dry_run=True)
        say("план запуска сервера", "dry_serve", detail=url)
        return
    if not Path(cfg["books_dir"]).is_dir():
        say(f"каталог книг сейчас недоступен: {cfg['books_dir']}", "books_dir_missing", logging.WARNING)
    migrate_database(cfg["db_path"])
    Handler.cfg = cfg
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    server.daemon_threads = True
    say(f"веб-интерфейс: {url}", "serve", detail=url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        say("веб-интерфейс остановлен", "serve_stop")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Лёгкая веб-библиотека книг: каталог в SQLite и интерфейс в браузере.")
    parser.add_argument("command", nargs="?", choices=("serve", "scan", *FILE_CHANGING), default="serve",
                        help="serve (по умолчанию), scan, fix-encoding, unpack-fb2zip, convert, dedup или zip-books")
    parser.add_argument("-p", "--path", help="fix-encoding/unpack-fb2zip/convert: каталог (по умолчанию каталог книг)")
    parser.add_argument("--only", default="all",
                        help="fix-encoding: all|fb2|txt; convert: all|fb2|doc|docx|rtf|odt|mht|mhtml (fb2 — в epub через Calibre)")
    parser.add_argument("--workers", type=int, help="convert --only fb2, dedup, zip-books: потоков (по умолчанию workers из конфига)")
    parser.add_argument("--max-mb", type=int, default=200, help="unpack-fb2zip: предел размера записи, МБ")
    parser.add_argument("--to", choices=("epub", "fb2"), default="epub", help="convert: формат результата (по умолчанию epub)")
    parser.add_argument("--overwrite", action="store_true", help="convert: перезаписывать существующий результат")
    parser.add_argument("--skip-newer", action="store_true", help="convert: с --overwrite не трогать результат новее исходника")
    parser.add_argument("--exclude", action="append", default=[], help="convert: пропускать имена с этой подстрокой")
    parser.add_argument("--author", default="", help="convert: автор для всех документов")
    parser.add_argument("--title-mode", choices=("auto", "filename", "keep"), default="auto", help="convert: откуда название")
    parser.add_argument("--no-toc", action="store_true", help="convert: без оглавления")
    parser.add_argument("--toc-depth", type=int, default=3, help="convert: глубина оглавления")
    parser.add_argument("--delete-source", action="store_true",
                        help="convert: удалить исходник после проверки результата (с JSON-отчётом)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="ничего не менять, показать план")
    parser.add_argument("--migrate-db", action="store_true", help="только миграции схемы БД")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="путь к books_library.json")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        cfg = load_config(args.config)
        if args.migrate_db or args.command != "serve":
            command = "migrate" if args.migrate_db else args.command
            raw: dict[str, Any] = {}
            if command in FILE_CHANGING:
                raw = {"path": args.path, "only": args.only}
            if command == "unpack-fb2zip":
                raw = {"path": args.path, "max_mb": args.max_mb}
            elif command in ("dedup", "zip-books"):
                raw = {"path": args.path, "workers": args.workers}
            elif command == "convert" and args.only == "fb2":
                raw.update(workers=args.workers)
            elif command == "convert":
                raw.update(to=args.to, overwrite=args.overwrite, skip_newer=args.skip_newer, exclude=args.exclude,
                           author=args.author, title_mode=args.title_mode, toc=not args.no_toc, toc_depth=args.toc_depth,
                           delete_source=args.delete_source)
            run_command(cfg, command, args.dry_run, command_params(command, raw))
        else:
            serve(cfg, dry_run=args.dry_run)
    except ConfigError as error:
        say(f"ошибка конфигурации: {error}", "config_error", logging.ERROR)
        return 2
    except ScanBusyError as error:
        say(str(error), "already_running", logging.ERROR)
        return 3
    except (OSError, sqlite3.Error) as error:
        say(f"ошибка: {error}", "failed", logging.ERROR)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
