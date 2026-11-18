#!/usr/bin/env python3
"""Лёгкая домашняя библиотека: каталог книг в SQLite (FTS5) и веб-интерфейс в браузере.

Только стандартная библиотека Python, без зависимостей. Режимы:
  scan            — проиндексировать каталог книг (инкрементально по размеру и mtime);
  serve           — запустить веб-интерфейс (по умолчанию);
  fix-encoding    — вылечить кодировку: fb2 с заявленной не UTF-8 при тексте в UTF-8, txt в cp1251 → UTF-8;
  unpack-fb2zip   — извлечь .fb2 из *.fb2.zip рядом с архивом;
  convert         — DOC/DOCX/RTF/ODT/MHT → EPUB (или FB2) через pandoc/LibreOffice;
  reconvert-fb2   — calibre-epub из fb2 пересобрать собственным конвертером fb2 → EPUB 3 (с --dry-run и --sample);
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
import hashlib
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
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
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
    # Кэш извлечённого текста для dedup (восстановимый, на быстром диске): пустая строка в text_cache_dir — кэш выключен;
    # text_cache_mb — предел сжатых данных, МБ (0 — кэш выключен). Каталог создаёт администратор, движок его не создаёт.
    "text_cache_dir": "/var/cache/books_library",
    "text_cache_mb": 20000,
    # Карантин служебных режимов: вместо удаления файлы переносятся сюда (<причина>/<путь в каталоге книг>).
    "quarantine_dir": "/mnt/copy/Books-removed",
    "workers": 12,
    # Потоки unzip-books: распаковка пишет на диск, а HDD SMR при параллельной записи сильно замедляется.
    "unzip_workers": 2,
    "auth":{"user": "", "password": "", "password_env": "BOOKS_LIBRARY_PASSWORD"},
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
    # v3: правки метаданных из веба и API: для форматов без записи в файл (pdf, djvu, txt…) — в БД;
    # журнал правок с путём копии прежней версии файла в карантине.
    """
    CREATE TABLE meta_overrides(path TEXT PRIMARY KEY, title TEXT, authors TEXT, series TEXT, series_no INTEGER, genres TEXT, year TEXT, lang TEXT, annotation TEXT, updated REAL NOT NULL);
    CREATE TABLE edit_log(id INTEGER PRIMARY KEY, path TEXT NOT NULL, new_path TEXT NOT NULL DEFAULT '', fields TEXT NOT NULL, backup TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
    """,
    # v4: сборники книги. folder — книга лежит в папке сборника; dedup — копия из сборника ушла в карантин как дубль,
    # принадлежность перешла к оставленной книге (keeper). VIEW collections — сборники со счётчиками книг.
    """
    CREATE TABLE book_collections(
        path TEXT NOT NULL,          -- путь книги в библиотеке (как books.path)
        section TEXT NOT NULL,
        collection TEXT NOT NULL,
        source TEXT NOT NULL,        -- 'folder' (лежит в папке сборника) или 'dedup' (копия из сборника ушла в карантин как дубль)
        orig_path TEXT NOT NULL,     -- путь файла в сборнике (для folder = path; для dedup = путь удалённой копии)
        PRIMARY KEY(path, section, collection, orig_path)
    ) WITHOUT ROWID;
    CREATE INDEX book_collections_coll ON book_collections(section, collection);
    CREATE VIEW collections AS
        SELECT bc.section, bc.collection, count(DISTINCT bc.path) AS books
        FROM book_collections bc JOIN books b ON b.path = bc.path
        GROUP BY bc.section, bc.collection;
    -- keeper, переименованные позже упаковкой zip-books (name.ext -> name.ext.zip):
    UPDATE dup_members SET keeper = keeper || '.zip'
        WHERE keeper <> '' AND keeper NOT IN (SELECT path FROM books) AND keeper || '.zip' IN (SELECT path FROM books);
    UPDATE OR IGNORE dup_members SET path = path || '.zip'
        WHERE role = 'kept' AND path NOT IN (SELECT path FROM books) AND path || '.zip' IN (SELECT path FROM books);
    """,
    # v5: варианты написания одного автора (папки «Ivanov Ivan» и «Иванов Иван»). Решения пользователя постоянны.
    """
    CREATE TABLE author_variants(
        a TEXT NOT NULL,                          -- папка верхнего уровня, a < b
        b TEXT NOT NULL,
        tier TEXT NOT NULL,                       -- empty | common | manual
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | merged | different
        target TEXT NOT NULL DEFAULT '',          -- папка, в которую склеено (для merged)
        updated REAL NOT NULL,
        PRIMARY KEY(a, b)
    ) WITHOUT ROWID;
    """,
]
DB_LATEST = len(MIGRATIONS)
# Первая схема с book_collections и VIEW collections: новые запросы к ним — только при user_version >= этого значения.
SCHEMA_COLLECTIONS = 4


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
                            ("convert_timeout", 10, 3600), ("cache_mb", 100, 1_000_000), ("workers", 1, 64),
                            ("unzip_workers", 1, 64), ("text_cache_mb", 0, 10_000_000)):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or not low <= cfg[key] <= high:
            raise ConfigError(f"{key} должен быть целым от {low} до {high}")
    for key in ("books_dir", "db_path", "host", "quarantine_dir"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise ConfigError(f"{key} должен быть непустой строкой")
    if not isinstance(cfg["text_cache_dir"], str):
        raise ConfigError("text_cache_dir должен быть строкой (пустая — кэш выключен)")
    if cfg["text_cache_dir"]:
        text_dir = Path(cfg["text_cache_dir"]).expanduser()
        cfg["text_cache_dir"] = str(text_dir if text_dir.is_absolute() else PROJECT_DIR / text_dir)
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


def path_collection(rel: str) -> tuple[str, str]:
    """Раздел и сборник по пути: «_Раздел/_Сборник/книга» → (раздел, сборник); сборника нет — сборник ''."""
    parts = PurePosixPath(rel).parts
    if len(parts) > 1 and parts[0].startswith("_"):
        return section_name(parts[0]), (parts[1].lstrip("_").strip() if len(parts) > 2 else "")
    return "", ""


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
        meta["section"], meta["collection"] = path_collection(rel)
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


EDIT_KEYS = ("title", "authors", "series", "series_no", "genres", "year", "lang", "annotation")


def load_overrides(db_path: str) -> dict[str, sqlite3.Row]:
    """Правки метаданных из meta_overrides по пути книги; до схемы v3 таблицы нет — пусто."""
    with closing(connect(db_path)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            return {}
        return {row["path"]: row for row in conn.execute("SELECT * FROM meta_overrides")}


def apply_override(meta: dict[str, Any], row: sqlite3.Row) -> None:
    """Поверх разбора — непустые поля правки: метаданные книг без записи в файл живут в БД и не затираются сканом."""
    for key in EDIT_KEYS:
        value = row[key]
        if value is None or value == "":
            continue
        meta[key] = [v for v in value.split("; ") if v] if key in ("authors", "genres") else value


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
    overrides = load_overrides(db_path)
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
                        if rel in overrides:
                            apply_override(meta, overrides[rel])
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
            if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_COLLECTIONS:
                report_collections(rebuild_collections(conn), out)
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
                      "rejected-name", "unzipped", "replaced-calibre")
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
DEDUP_SIMILAR = 0.85  # dedup-folders: доля шинглов от оставленных, с которой книга «похожая» (решает человек)
DEDUP_FOLDER_MAX = 3000  # dedup-folders: книг в папке автора больше — папка пропускается (folder-too-large)
FOLDER_SAMPLE = 4  # dedup-folders: единая выборка шинглов (остаток по модулю 4) для всех книг папки
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


# управляющие байты, недопустимые в тексте (кроме \t \n \f \r)
TXT_CONTROL = bytes(b for b in range(32) if b not in (9, 10, 12, 13))


def detect_txt(raw: bytes) -> tuple[str | None, str]:
    """Текст и кодировка TXT: UTF-8/UTF-16 с BOM, UTF-8, затем cp1251/KOI8-R/cp866 по доле строчных среди кириллических
    букв (≥ 0,8 — у верной кодировки ~0,95, у неверной заглавные и строчные перепутаны), затем cp1252. None — не распознан.
    Бинарным считается файл, где управляющих байт больше 1 %; одиночные управляющие байты выбрасываются перед декодированием."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            return None, "utf-16?"
    controls = len(raw) - len(raw.translate(None, TXT_CONTROL))
    if controls > len(raw) * 0.01:
        return None, "binary"
    raw = raw.translate(None, TXT_CONTROL)
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


EPUB_HEAD = re.compile(r"<head[\s>].*?</head>", re.S | re.I)


def markup_plain(markup: str) -> str:
    """Текст разметки без тегов. Ссылки &#1090; и мягкие переносы раскрываются/убираются, иначе каждая буква-код — отдельное «слово»."""
    return html.unescape(re.sub(r"<[^>]*>", " ", markup)).replace("\xad", "")


def epub_word_count(epub: Path) -> int:
    """Слов в тексте глав epub (spine) без разметки и без <head>: <title> и CSS в каждом файле — не текст книги."""
    with zipfile.ZipFile(epub) as archive:
        return count_words(" ".join(markup_plain(EPUB_HEAD.sub(" ", decode_xml(zip_read(archive, p))))
                                    for p in Epub(archive).spine()))


def epub_text_loss(fb2_raw: bytes, epub: Path) -> str | None:
    """Проверка, что текст не потерян: в epub не меньше 80 % слов тела fb2 (без бинарных вложений)."""
    text = decode_xml(fb2_raw)
    start, end = text.find("<body"), text.find("<binary")
    fb2_words = count_words(markup_plain(text[max(start, 0):end if end > start else None]))
    epub_words = epub_word_count(epub)
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
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            return 0
        for old, new in pairs:
            conn.execute("UPDATE OR IGNORE marks SET path=? WHERE path=?", (new, old))
            conn.execute("UPDATE OR IGNORE shelf_items SET path=? WHERE path=?", (new, old))
            conn.execute("DELETE FROM shelf_items WHERE path=?", (old,))
            if version >= SCHEMA_COLLECTIONS:
                conn.execute("UPDATE OR IGNORE book_collections SET path=? WHERE path=?", (new, old))
                conn.execute("DELETE FROM book_collections WHERE path=?", (old,))
                conn.execute("UPDATE dup_members SET keeper=? WHERE keeper=?", (new, old))
                conn.execute("UPDATE OR IGNORE dup_members SET path=? WHERE path=? AND role='kept'", (new, old))
    return len(pairs)


def rebuild_collections(conn: sqlite3.Connection) -> dict[str, int]:
    """Дополняет book_collections (схема v4) в транзакции вызывающего: строки не удаляются и не переписываются —
    принадлежность книги к сборнику переживает перенос файла из папки сборника (его путь переносит relink).
    folder — книги из папок сборников; dedup — копии из сборников, ушедшие в карантин как дубли: принадлежность
    переходит к оставленной книге (keeper), с учётом переименования keeper в name.ext.zip. Без книги — keeper не
    найден ни как путь, ни как .zip."""
    counts = {"folder": conn.execute("INSERT OR IGNORE INTO book_collections SELECT path, section, collection, 'folder', path "
                                     "FROM books WHERE collection <> ''").rowcount, "dedup": 0, "unresolved": 0}
    known = {row[0] for row in conn.execute("SELECT path FROM books")}
    for path, keeper in conn.execute("SELECT path, keeper FROM dup_members WHERE role='duplicate' AND keeper <> ''").fetchall():
        section, collection = path_collection(path)
        if not collection:
            continue
        if keeper in known:
            target = keeper
        elif keeper + ".zip" in known:
            target = keeper + ".zip"
        else:
            counts["unresolved"] += 1
            continue
        counts["dedup"] += conn.execute("INSERT OR IGNORE INTO book_collections VALUES (?,?,?,'dedup',?)",
                                        (target, section, collection, path)).rowcount
    return counts


def report_collections(counts: dict[str, int], out: Callable[[str], None]) -> None:
    """Итог пересборки сборников: строка в вывод и событие журнала."""
    line = f"сборники: folder {counts['folder']}, dedup {counts['dedup']}, без книги {counts['unresolved']}"
    out(line)
    say(line, "collections", detail=json.dumps(counts, ensure_ascii=False))


DUP_STATUS_NAMES = {"similar": "Похожие (ручное решение)", "auto": "Удалены автоматически", "resolved": "Решённые"}
DUP_PAGE_SIZE = 50


def dup_groups_page(conn: sqlite3.Connection, status: str, page: int, size: int,
                    statuses: tuple[str, ...] | None = None) -> tuple[int, list[dict[str, Any]]]:
    """Группы дублей с участниками (веб и API): статус status или любой из statuses. Путь карантина наружу не отдаётся вызывающими."""
    wanted = statuses or (status,)
    marks = ",".join("?" * len(wanted))  # только знаки «?», значения передаются параметрами
    total = conn.execute(f"SELECT count(*) FROM dup_groups WHERE status IN ({marks})", wanted).fetchone()[0]
    groups: list[dict[str, Any]] = []
    for group in conn.execute(f"SELECT id, key, status, created FROM dup_groups WHERE status IN ({marks}) ORDER BY id LIMIT ? OFFSET ?",
                              (*wanted, size, (page - 1) * size)).fetchall():
        members = []
        for member in conn.execute("SELECT path, fmt, size, words, coverage, role, quarantine FROM dup_members "
                                   "WHERE group_id=? ORDER BY role DESC, path", (group["id"],)).fetchall():
            book = conn.execute("SELECT id FROM books WHERE path=?", (member["path"],)).fetchone()
            members.append({**dict(member), "book_id": book[0] if book else None})
        groups.append({**dict(group), "members": members})
    return total, groups


def dup_group_json(group: dict[str, Any]) -> dict[str, Any]:
    return {"id": group["id"], "key": group["key"], "status": group["status"], "created": group["created"],
            "members": [{"path": display_path(m["path"], m["fmt"]), "key_path": m["path"], "fmt": m["fmt"], "size": m["size"],
                         "words": m["words"], "coverage": m["coverage"], "role": m["role"], "book_id": m["book_id"],
                         "in_quarantine": bool(m["quarantine"])} for m in group["members"]]}


def dup_action(cfg: dict[str, Any], group_id: int, action: str, path: str = "") -> str:
    """Решение по группе дублей: общая логика веба и API. Возвращает сообщение; ошибки для пользователя — ValueError."""
    root = Path(cfg["books_dir"]).resolve()
    if action == "keep-both":
        with closing(connect(cfg["db_path"])) as conn, conn:
            if conn.execute("UPDATE dup_groups SET status='resolved' WHERE id=?", (group_id,)).rowcount == 0:
                raise ValueError("группа дублей не найдена")
        say(f"группа {group_id}: оставлены все файлы", "dup_action", detail=f"keep-both group={group_id}")
        return "Все файлы группы оставлены, группа помечена решённой."
    if action == "resolve":
        with closing(connect(cfg["db_path"])) as conn, conn:
            if conn.execute("UPDATE dup_groups SET status='resolved' WHERE id=?", (group_id,)).rowcount == 0:
                raise ValueError("группа дублей не найдена")
        say(f"группа {group_id}: помечена решённой", "dup_action", detail=f"resolve group={group_id}")
        return "Группа помечена решённой, файлы не менялись."
    if action == "quarantine":
        with closing(connect(cfg["db_path"])) as conn:
            kept = [r[0] for r in conn.execute("SELECT path FROM dup_members WHERE group_id=? AND role='kept' ORDER BY path",
                                               (group_id,))]
        if path not in kept:
            raise ValueError("файл не является оставленным участником этой группы")
        others = [p for p in kept if p != path]
        if not others:
            raise ValueError("единственную оставленную книгу группы нельзя отправить в карантин")
        keeper = others[0]
        with scan_lock():
            full = (root / path).resolve()
            if not full.is_relative_to(root) or not full.is_file():
                raise ValueError("файл не найден в каталоге книг")
            dest = Path(cfg["quarantine_dir"]) / "duplicate" / path
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest = unique_path(dest.parent, dest.name, set())
                shutil.move(full, dest)
            except OSError as error:
                raise ValueError(f"не удалось переместить файл в карантин: {error.strerror}") from error
            with closing(connect(cfg["db_path"])) as conn, conn:
                conn.execute("UPDATE dup_members SET role='duplicate', quarantine=?, keeper=? WHERE group_id=? AND path=?",
                             (str(dest), keeper, group_id, path))
                conn.execute("DELETE FROM books_fts WHERE rowid IN (SELECT id FROM books WHERE path=?)", (path,))
                conn.execute("DELETE FROM books WHERE path=?", (path,))
            relink(cfg["db_path"], [(path, keeper)])
        say(f"группа {group_id}: в карантин {path}", "dup_action", detail=f"quarantine group={group_id} keeper={keeper}")
        return f"«{path}» перенесён в карантин, оставлен «{keeper}»."
    if action == "restore":
        with closing(connect(cfg["db_path"])) as conn:
            row = conn.execute("SELECT quarantine FROM dup_members WHERE group_id=? AND path=? AND role='duplicate' "
                               "AND quarantine<>''", (group_id, path)).fetchone()
        if row is None:
            raise ValueError("файл не числится в карантине этой группы")
        with scan_lock():
            source = Path(row[0]).resolve()
            if not source.is_relative_to(Path(cfg["quarantine_dir"]).resolve()) or not source.is_file():
                raise ValueError("файл в карантине не найден")
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise ValueError("путь вне каталога книг")
            if target.exists():
                raise ValueError("на месте уже есть файл, восстановление отменено")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(source, target)
            except OSError as error:
                raise ValueError(f"не удалось восстановить файл: {error.strerror}") from error
            stat = target.stat()
            meta = read_book_meta(root, path)
            with closing(connect(cfg["db_path"])) as conn, conn:
                conn.execute("UPDATE dup_members SET role='kept', quarantine='', keeper='' WHERE group_id=? AND path=?",
                             (group_id, path))
                kept_count = conn.execute("SELECT count(*) FROM dup_members WHERE group_id=? AND role='kept'",
                                          (group_id,)).fetchone()[0]
                if kept_count > 1:
                    conn.execute("UPDATE dup_groups SET status='similar' WHERE id=?", (group_id,))
                upsert_book(conn, path, stat.st_size, stat.st_mtime, meta)
        say(f"группа {group_id}: восстановлен {path}", "dup_action", detail=f"restore group={group_id}")
        return f"«{path}» восстановлен в каталог."
    raise ValueError("неизвестное действие")


# ----------------------------------------------------------------------
# Правка метаданных: проверка полей, запись в файл (epub, fb2), каталог, переименование, журнал
# ----------------------------------------------------------------------

EDIT_LANG_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")
EDIT_YEAR_RE = re.compile(r"[0-9]{4}")
EDIT_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EDIT_NAME_MAX = 240  # байт имени с расширением; запас до лимита файловой системы 255
EDIT_MEMBER_MAX = 256 * 1024 * 1024
OPF_METADATA_RE = re.compile(r"(<((?:[\w.-]+:)?)metadata(?=[\s>])[^>]*>)(.*?)(</\2metadata\s*>)", re.S)
FB2_TITLE_INFO_RE = re.compile(r"(<((?:[\w.-]+:)?)title-info(?=[\s>])[^>]*>)(.*?)(</\2title-info\s*>)", re.S)


def _edit_text(value: Any, field: str, low: int, high: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}: ожидается строка")
    text = value.replace("\r\n", "\n").replace("\r", "\n") if multiline else value
    text = text.strip()
    if any(ord(ch) < 32 and not (multiline and ch == "\n") for ch in text):
        raise ValueError(f"{field}: недопустимые управляющие символы")
    if not low <= len(text) <= high:
        raise ValueError(f"{field}: длина от {low} до {high} символов")
    return text


def _edit_names(value: Any, field: str, min_items: int, max_items: int, high: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field}: ожидается список строк")
    items = [_edit_text(item, field, 1, high) for item in value]
    if not min_items <= len(items) <= max_items:
        raise ValueError(f"{field}: от {min_items} до {max_items} значений")
    return items


def validate_edit(raw: dict[str, Any]) -> dict[str, Any]:
    """Проверка полей правки метаданных; ошибки — ValueError с текстом для пользователя."""
    unknown = sorted(set(raw) - set(EDIT_KEYS))
    if unknown:
        raise ValueError(f"неизвестные поля: {', '.join(unknown)}")
    if "title" not in raw or "authors" not in raw:
        raise ValueError("обязательны поля title и authors")
    raw_no = raw.get("series_no")
    if isinstance(raw_no, str):
        raw_no = raw_no.strip()
    if raw_no in (None, ""):
        number = None
    elif isinstance(raw_no, bool) or not (isinstance(raw_no, int) or (isinstance(raw_no, str) and raw_no.isascii() and raw_no.isdigit())):
        raise ValueError("series_no: только цифры или пусто")
    else:
        number = int(raw_no)
        if not 0 <= number <= 100000:
            raise ValueError("series_no: от 0 до 100000")
    year = _edit_text(raw.get("year", ""), "year", 0, 4)
    if year and not EDIT_YEAR_RE.fullmatch(year):
        raise ValueError("year: пусто или четыре цифры")
    lang = _edit_text(raw.get("lang", ""), "lang", 0, 20)
    if lang and not EDIT_LANG_RE.fullmatch(lang):
        raise ValueError("lang: код языка вида ru или en-US")
    return {
        "title": _edit_text(raw["title"], "title", 1, 500),
        "authors": _edit_names(raw["authors"], "authors", 1, 20, 200),
        "series": _edit_text(raw.get("series", ""), "series", 0, 300),
        "series_no": number,
        "genres": _edit_names(raw.get("genres", []), "genres", 0, 20, 100),
        "year": year,
        "lang": lang,
        "annotation": _edit_text(raw.get("annotation", ""), "annotation", 0, 20000, multiline=True),
    }


def _xml_text(value: str) -> str:
    return html.escape(value, quote=True)


def _drop_element(block: str, name: str, prefix: str = "") -> str:
    """Удаляет элемент name (префикс — регулярка) вместе с содержимым либо самозакрывающийся."""
    tag = rf"{prefix}{re.escape(name)}"
    return re.sub(rf"<{tag}(?=[\s/>])[^>]*?(?:/>|>.*?</{tag}\s*>)", "", block, flags=re.S)


def opf_with_meta(opf: str, fields: dict[str, Any]) -> str:
    """OPF с новыми dc-полями и серией calibre; прежние dc:title/creator/subject/date/language/description заменяются."""
    match = OPF_METADATA_RE.search(opf)
    if not match:
        raise ValueError("в OPF нет блока metadata")
    inner = match.group(3)
    for name in ("title", "creator", "subject", "date", "language", "description"):
        inner = _drop_element(inner, name, "dc:")
    inner = re.sub(r'<(?:[\w.-]+:)?meta\b[^>]*?\bname\s*=\s*["\']calibre:series(?:_index)?["\'][^>]*>', "", inner)
    role = ' opf:role="aut"' if re.search(r"xmlns:opf\s*=", opf) else ""  # атрибут только при объявленном префиксе
    lines = [f"<dc:title>{_xml_text(fields['title'])}</dc:title>"]
    lines += [f"<dc:creator{role}>{_xml_text(author)}</dc:creator>" for author in fields["authors"]]
    lines += [f"<dc:subject>{_xml_text(genre)}</dc:subject>" for genre in fields["genres"]]
    if fields["year"]:
        lines.append(f"<dc:date>{_xml_text(fields['year'])}</dc:date>")
    if fields["lang"]:
        lines.append(f"<dc:language>{_xml_text(fields['lang'])}</dc:language>")
    if fields["annotation"]:
        lines.append(f"<dc:description>{_xml_text(fields['annotation'])}</dc:description>")
    if fields["series"]:
        lines.append(f'<meta name="calibre:series" content="{_xml_text(fields["series"])}"/>')
        if fields["series_no"] is not None:
            lines.append(f'<meta name="calibre:series_index" content="{fields["series_no"]}"/>')
    new_inner = inner.rstrip() + "".join(f"\n    {line}" for line in lines) + "\n  "
    return opf[:match.start(3)] + new_inner + opf[match.end(3):]


def epub_with_meta(data: bytes, fields: dict[str, Any]) -> bytes:
    """EPUB с обновлённым OPF: mimetype первым без сжатия, остальные записи переносятся как были."""
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        epub = Epub(source)
        new_opf = opf_with_meta(epub.opf, fields).encode("utf-8")
        infos = sorted(source.infolist(), key=lambda item: item.filename != "mimetype")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as target:
            for info in infos:
                payload = new_opf if info.filename == epub.opf_path else read_member(source, info, EDIT_MEMBER_MAX)
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
                entry.external_attr = info.external_attr
                target.writestr(entry, payload)
    return out.getvalue()


def _fb2_author(name: str) -> str:
    """«Фамилия Имя Отчество» → <author>: первое слово — фамилия, второе — имя, остальное — отчество."""
    parts = name.split()
    tags = (("last-name", parts[0]), ("first-name", parts[1] if len(parts) > 1 else ""),
            ("middle-name", " ".join(parts[2:])))
    return "<author>" + "".join(f"<{tag}>{_xml_text(value)}</{tag}>" for tag, value in tags if value) + "</author>"


def fb2_with_meta(raw: bytes, fields: dict[str, Any]) -> bytes:
    """FB2 с новым title-info; кодировка объявления и остальной текст сохраняются."""
    match = DECL_RE.search(raw[:300])
    encoding = match.group(2).decode("ascii") if match else "utf-8"
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as error:
        raise ValueError("fb2 с битыми байтами — правка только в БД невозможна, исправьте кодировку") from error
    found = FB2_TITLE_INFO_RE.search(text)
    if not found:
        raise ValueError("в fb2 нет title-info")
    body = found.group(3)
    for name in ("genre", "author", "book-title", "annotation", "date", "lang", "sequence"):
        body = _drop_element(body, name, r"(?:[\w.-]+:)?")
    head = [f"<genre>{_xml_text(genre)}</genre>" for genre in fields["genres"]]
    head += [_fb2_author(author) for author in fields["authors"]]
    head.append(f"<book-title>{_xml_text(fields['title'])}</book-title>")
    if fields["annotation"]:
        paragraphs = "".join(f"<p>{_xml_text(line)}</p>" for line in fields["annotation"].split("\n") if line.strip())
        head.append(f"<annotation>{paragraphs}</annotation>")
    tail = []
    if fields["year"]:
        tail.append(f"<date>{_xml_text(fields['year'])}</date>")
    if fields["lang"]:
        tail.append(f"<lang>{_xml_text(fields['lang'])}</lang>")
    if fields["series"]:
        number = f' number="{fields["series_no"]}"' if fields["series_no"] is not None else ""
        tail.append(f'<sequence name="{_xml_text(fields["series"])}"{number}/>')
    new_text = (text[:found.start()] + found.group(1) + "".join(head) + body + "".join(tail)
                + found.group(4) + text[found.end():])
    try:
        return new_text.encode(encoding)
    except UnicodeEncodeError:  # символы не входят в объявленную кодировку — сохраняем в UTF-8 с новым объявлением
        converted = new_text.encode("utf-8")
        return DECL_RE.sub(lambda m: m.group(1) + b"utf-8" + m.group(3), converted, count=1)


def edited_name(fields: dict[str, Any], ext: str) -> str:
    """«Автор - Название» + исходное расширение; запрещённые в Windows символы заменяются, название обрезается."""
    authors = EDIT_BAD_CHARS.sub("_", ", ".join(fields["authors"]))
    title = EDIT_BAD_CHARS.sub("_", fields["title"])
    while title and len(f"{authors} - {title}{ext}".encode("utf-8")) > EDIT_NAME_MAX:
        title = title[:-1]
    base = f"{authors} - {title}" if title else authors
    return base.strip(" .") + ext


def edited_target(root: Path, full: Path, old_rel: str, fields: dict[str, Any], ext: str) -> Path:
    """Новое место файла: в разделе («_…») — та же папка, иначе — папка первого автора."""
    if PurePosixPath(old_rel).parts[0].startswith("_"):
        folder = PurePosixPath(old_rel).parent
    else:
        name = EDIT_BAD_CHARS.sub("_", fields["authors"][0]).strip(" ._")
        if not name:
            raise ValueError("по имени автора нельзя составить имя папки")
        folder = PurePosixPath(name)
    target = (root / folder / edited_name(fields, ext)).resolve()
    if not target.is_relative_to(root) or not all(name_ok(part) for part in target.relative_to(root).parts):
        raise ValueError("новое имя недопустимо для Windows или слишком длинное")
    if target != full and target.exists():
        raise ValueError("файл с таким именем уже есть")
    return target


def _rewrite_book_file(cfg: dict[str, Any], full: Path, old_rel: str, fmt: str, wrapped: bool,
                       fields: dict[str, Any]) -> str:
    """Записывает метаданные в epub или fb2: проверка новой версии, копия прежней в карантин edited/, атомарная замена."""
    limit = cfg["reader_max_mb"] * 1024 * 1024
    member = ""
    try:
        if wrapped:
            with zipfile.ZipFile(full) as archive:
                inner = single_member(archive)
                if inner is None or member_ext(inner) != fmt:
                    raise ValueError("однокнижный архив не похож на книгу этого формата")
                member = inner.filename
                data = read_member(archive, inner, limit)
        else:
            if full.stat().st_size > limit:
                raise ValueError(f"файл больше предела reader_max_mb ({cfg['reader_max_mb']} МБ)")
            data = full.read_bytes()
        new = epub_with_meta(data, fields) if fmt == "epub" else fb2_with_meta(data, fields)
    except (OSError, KeyError, zipfile.BadZipFile, EOFError) as error:
        raise ValueError(f"не удалось прочитать книгу для правки: {error}") from error
    if fmt == "epub":
        with tempfile.TemporaryDirectory(prefix="books-edit-") as tmp_dir:
            probe = Path(tmp_dir) / "check.epub"
            probe.write_bytes(new)
            problem = verify_book(probe, "epub")
    else:
        problem = None if fb2_meta(new).get("title") == " ".join(fields["title"].split()) else "название в файле не совпало"
    if problem:
        raise ValueError(f"проверка не прошла: {problem}")
    dest = Path(cfg["quarantine_dir"]) / "edited" / old_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = unique_path(dest.parent, dest.name, set())
    shutil.copy2(full, dest)
    if wrapped:
        fd, tmp_name = tempfile.mkstemp(prefix=".edit.", suffix=".tmp", dir=full.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            crc = write_zip(tmp, member, full.stat().st_mtime, io.BytesIO(new), len(new), fmt)
            problem = verify_zip(tmp, member, len(new), crc)
            if problem:
                raise ValueError(f"перепаковка не прошла проверку: {problem}")
            os.chmod(tmp, full.stat().st_mode & 0o666)
            os.replace(tmp, full)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        atomic_write(full, new, "edit")
    return str(dest)


def edit_book(cfg: dict[str, Any], book_id: int, raw: dict[str, Any], rename: bool = False) -> str:
    """Правка метаданных книги: общая логика веба и API.

    epub и fb2 переписываются в файле (прежняя версия — в карантин edited/); остальные форматы сохраняют правку
    в БД (meta_overrides). rename переименовывает файл в «Автор - Название». Ошибки для пользователя — ValueError.
    """
    fields = validate_edit(raw)
    root = Path(cfg["books_dir"]).resolve()
    with scan_lock():
        migrate_database(cfg["db_path"])
        with closing(connect(cfg["db_path"])) as conn:
            book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if book is None:
            raise ValueError("книга не найдена")
        old_rel, fmt = book["path"], book["fmt"]
        full = (root / old_rel).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            raise ValueError("файл книги не найден в каталоге книг")
        wrapped = is_wrapped(old_rel, fmt)
        original = PurePosixPath(old_rel).name
        ext = original[-(len(fmt) + 5):] if wrapped else "." + original.rpartition(".")[2]
        target = edited_target(root, full, old_rel, fields, ext) if rename else full
        rewrite = fmt in ("epub", "fb2")
        backup = _rewrite_book_file(cfg, full, old_rel, fmt, wrapped, fields) if rewrite else ""
        if target != full:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(full, target)
            except OSError as error:
                raise ValueError(f"не удалось переименовать файл: {error.strerror}") from error
        new_rel = target.relative_to(root).as_posix()
        with closing(connect(cfg["db_path"])) as conn, conn:
            if new_rel != old_rel:
                conn.execute("UPDATE books SET path=? WHERE id=?", (new_rel, book_id))
                conn.execute("UPDATE meta_overrides SET path=? WHERE path=?", (new_rel, old_rel))
            if not rewrite:
                conn.execute(
                    "INSERT OR REPLACE INTO meta_overrides(path, title, authors, series, series_no, genres, year, lang,"
                    " annotation, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_rel, fields["title"], "; ".join(fields["authors"]), fields["series"], fields["series_no"],
                     "; ".join(fields["genres"]), fields["year"], fields["lang"], fields["annotation"], time.time()))
            stat = target.stat()
            meta = read_book_meta(root, new_rel)
            meta.update({key: fields[key] for key in EDIT_KEYS})
            upsert_book(conn, new_rel, stat.st_size, stat.st_mtime, meta)
            conn.execute("INSERT INTO edit_log(path, new_path, fields, backup, created) VALUES (?,?,?,?,?)",
                         (old_rel, new_rel if new_rel != old_rel else "", json.dumps(fields, ensure_ascii=False), backup,
                          time.time()))
        if new_rel != old_rel:
            relink(cfg["db_path"], [(old_rel, new_rel)])
    renamed = new_rel != old_rel
    say(f"книга {book_id}: правка метаданных", "book_edit", detail=json.dumps(
        {"book_id": book_id, "path": new_rel, "renamed": renamed, "backup": backup, "fields": fields}, ensure_ascii=False))
    where = "записаны в файл (прежняя версия — в карантине edited/)" if rewrite else \
        "сохранены в каталоге (формат не поддерживает запись в файл)"
    return f"Метаданные {where}." + (f" Файл переименован: «{new_rel}»." if renamed else "")


# ======================================================================
# Проверка метаданных и переименование автора
# ======================================================================

FIX_REASON_TEXT = {
    "semi": "искажённые заглавные (полу-cp866)",
    "garbage": "мусорные символы",
    "title": "пустое или числовое название",
    "author": "нет автора",
}
FIX_COLUMNS = "id, path, fmt, title, authors, series, series_no, genres, year, lang, annotation"
FIX_SQL = f"SELECT {FIX_COLUMNS} FROM books ORDER BY id"
FIX_PAGE_SIZE = 100
RENAME_LIMIT = 2000
CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
CYRILLIC = re.compile("[а-яёА-ЯЁ]")
LETTER = re.compile(r"[^\W\d_]")
# Символы cp1252 с кодами 0x80–0x9F (€ ‚ „ … ‘ ’ “ ” и др.): продолжение UTF-8 при чтении как cp1252.
CP1252_HIGH = "".join(ch for ch in (bytes([b]).decode("cp1252", errors="replace") for b in range(0x80, 0xA0))
                      if ch != "\ufffd")
# Кириллица в UTF-8 — два байта: лидирующий 0xD0 или 0xD1 (Ð, Ñ) и продолжение 0x80–0xBF. При чтении как cp1252
# или latin-1 продолжение — это U+0080–U+00BF либо символ из CP1252_HIGH.
MOJIBAKE = re.compile(f"[ÐÑ][\x80-\xbf{re.escape(CP1252_HIGH)}]")


def _book_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Поля книги в виде аргумента edit_book — значения из БД как есть."""
    return {
        "title": row["title"] or "",
        "authors": [name for name in str(row["authors"] or "").split("; ") if name],
        "series": row["series"] or "",
        "series_no": row["series_no"],
        "genres": [genre for genre in str(row["genres"] or "").split("; ") if genre],
        "year": row["year"] or "",
        "lang": row["lang"] or "",
        "annotation": row["annotation"] or "",
    }


def _is_garbage(value: str) -> bool:
    return "\ufffd" in value or CONTROL_CHARS.search(value) is not None or MOJIBAKE.search(value) is not None


def _repair_mojibake(value: str) -> str | None:
    """UTF-8, прочитанный как cp1252 (или latin-1), — обратно в кириллицу. Принимается, если нет символа U+FFFD, управляющих
    символов и есть кириллица; иначе None."""
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = value.encode(codec).decode("utf-8")
        except UnicodeError:
            continue
        if "\ufffd" not in fixed and CONTROL_CHARS.search(fixed) is None and CYRILLIC.search(fixed):
            return fixed
    return None


def suspicious_fields(row: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Причины проверки метаданных книги и поля для edit_book. Пустой словарь — предложения нет."""
    base = _book_fields(row)
    reasons: list[str] = []
    title, title_hits = fix_semi_cp866(base["title"], field=True)
    authors_fixed = [fix_semi_cp866(name, field=True) for name in base["authors"]]
    authors = [name for name, _ in authors_fixed]
    if title_hits or any(hits for _, hits in authors_fixed):
        reasons.append(FIX_REASON_TEXT["semi"])
    candidate_title, candidate_authors = title, authors
    garbage = [value for value in (title, *authors) if _is_garbage(value)]
    if garbage:
        reasons.append(FIX_REASON_TEXT["garbage"])
        repaired = {value: fixed for value in garbage if (fixed := _repair_mojibake(value)) is not None}
        if all(value in repaired for value in garbage):
            candidate_title = repaired.get(title, title)
            candidate_authors = [repaired.get(name, name) for name in authors]
    stripped = title.strip()
    if not stripped or LETTER.search(stripped) is None or stripped.casefold() in PLACEHOLDERS:
        reasons.append(FIX_REASON_TEXT["title"])
    if not authors:
        reasons.append(FIX_REASON_TEXT["author"])
    suggestion: dict[str, Any] = {}
    if candidate_title != base["title"] or candidate_authors != base["authors"]:
        suggestion = {**base, "title": candidate_title, "authors": candidate_authors}
        try:
            validate_edit(suggestion)  # предложение должно проходить ту же проверку, что и правка
        except ValueError:
            suggestion = {}
    return reasons, suggestion


def fix_scan(conn: sqlite3.Connection, reason: str, page: int, size: int
             ) -> tuple[dict[str, int], int, list[tuple[sqlite3.Row, list[str], dict[str, Any]]]]:
    """Один проход по книгам курсором: счётчики причин (ключ «all» — все проблемные), число книг под фильтром
    и книги нужной страницы."""
    counts = {"all": 0, **{code: 0 for code in FIX_REASON_TEXT}}
    total = 0
    first = (page - 1) * size
    items: list[tuple[sqlite3.Row, list[str], dict[str, Any]]] = []
    for row in conn.execute(FIX_SQL):
        reasons, suggestion = suspicious_fields(row)
        if not reasons:
            continue
        codes = [code for code, text in FIX_REASON_TEXT.items() if text in reasons]
        counts["all"] += 1
        for code in codes:
            counts[code] += 1
        if reason and reason not in codes:
            continue
        if first <= total < first + size:
            items.append((row, reasons, suggestion))
        total += 1
    return counts, total, items


def rename_author(cfg: dict[str, Any], old: str, new: str) -> str:
    """Переименование автора (слияние с уже существующим) во всех его книгах через edit_book.

    Ошибки отдельных книг собираются; ScanBusyError прерывает весь проход. Сам scan_lock здесь не берётся:
    его берёт edit_book для каждой книги, иначе была бы взаимоблокировка."""
    old, new = old.strip(), new.strip()
    for field, value in (("old", old), ("new", new)):
        if not 1 <= len(value) <= 200 or CONTROL_CHARS.search(value):
            raise ValueError(f"{field}: от 1 до 200 символов без управляющих")
    if old == new:
        raise ValueError("новое имя совпадает со старым")
    with closing(connect(cfg["db_path"])) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT b.id FROM books b JOIN book_authors a ON a.book_id=b.id WHERE a.name=? LIMIT ?",
            (old, RENAME_LIMIT + 1)).fetchall()]
        if len(ids) > RENAME_LIMIT:
            raise ValueError(f"у автора больше {RENAME_LIMIT} книг: переименование не выполнено")
        rows = [conn.execute(f"SELECT {FIX_COLUMNS} FROM books WHERE id=?", (book_id,)).fetchone() for book_id in ids]
    done, errors = 0, []
    for row in rows:
        if row is None:
            continue
        authors = list(dict.fromkeys(new if name == old else name for name in _book_fields(row)["authors"]))
        try:
            edit_book(cfg, row["id"], {**_book_fields(row), "authors": authors}, rename=False)
        except ValueError as error:
            errors.append(f"книга {row['id']}: {error}")
        else:
            done += 1
    return f"переименовано {done}, ошибок {len(errors)}" + (f": {'; '.join(errors[:5])}" if errors else "")


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
            # накопительно: unzip-books переносит отметки пачками во время прогона, хвост — здесь
            self.stats["relinked"] = self.stats.get("relinked", 0) + relink(self.cfg["db_path"], self.relinks)
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
# fb2 → EPUB 3 собственным конвертером (только stdlib): сноски во всплывающих окнах, серии, обложка
# ----------------------------------------------------------------------

# Раздел крупнее этого (символов разметки) делится по вложенным разделам: читалки плохо листают огромные файлы.
FB2_CHAPTER_MAX = 300_000
# DOCTYPE с ENTITY в начале файла отвергается: внутренние сущности fb2 не нужны, а XXE и «взрыв» сущностей — риск.
FB2_ENTITY_HEAD = 4096
EPUB_NOTES_FILE = "notes.xhtml"
EPUB_IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
# Строчные элементы fb2 и их XHTML-эквиваленты; прочие неизвестные теги разворачиваются, текст сохраняется.
INLINE_WRAP = {"emphasis": "em", "strong": "strong", "strikethrough": "s", "sub": "sub", "sup": "sup", "code": "code",
               "style": "span"}
EPUB_CONTAINER = ('<?xml version="1.0" encoding="utf-8"?>\n'
                  '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                  '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                  "</rootfiles></container>\n")
# Сдержанный стиль: размеры и шрифты задаёт читалка (только em и проценты), хрупкого позиционирования нет.
EPUB_CSS = """@charset "UTF-8";
p{margin:0;text-indent:1.5em;text-align:justify;hyphens:auto;-epub-hyphens:auto;-webkit-hyphens:auto}
h1,h2,h3,h4,h5,h6{text-align:center;text-indent:0;hyphens:none;-epub-hyphens:none;-webkit-hyphens:none;
margin:1.2em 0 .6em;font-weight:bold;page-break-after:avoid;break-after:avoid}
h1{page-break-before:always;break-before:page}
p.subtitle{text-align:center;text-indent:0;font-weight:bold}
p.empty-line{text-indent:0;margin:0}
blockquote.epigraph{margin:1em 0 1em 40%;font-style:italic}
blockquote.cite{margin:1em 2em;font-style:italic}
p.text-author,p.date{text-align:right;text-indent:0;font-style:italic}
div.poem{margin:1em 0 1em 3em}
div.stanza{margin:.8em 0}
div.poem-title{text-align:left;text-indent:0;font-weight:bold;margin:.5em 0}
p.v{text-indent:0;margin:0}
div.annotation{font-size:.95em;margin:1em 0}
div.annotation p{text-indent:0}
aside.footnote{font-size:.9em;margin:.6em 0}
aside.footnote p{text-indent:0}
p.footnote-title{font-weight:bold;text-indent:0}
p.backlink{text-indent:0;text-align:right}
.noteref{vertical-align:super;font-size:.75em;line-height:0;text-decoration:none}
div.image{text-align:center;text-indent:0;margin:1em 0}
img{max-width:100%}
.cover-wrap{text-align:center}
img.cover-img{display:block;margin:0 auto;max-width:100%;max-height:100vh;height:auto}
table{border-collapse:collapse;margin:1em auto}
td,th{padding:.2em .4em;vertical-align:top}
code{font-family:monospace}
"""


def _local(tag: str) -> str:
    """Имя тега или атрибута без пространства имён."""
    return tag.rpartition("}")[2]


def _attr(elem: ET.Element | None, name: str) -> str:
    """Значение атрибута по локальному имени (l:href, xml:lang и обычные атрибуты); пусто, если нет."""
    if elem is None:
        return ""
    for key, value in elem.attrib.items():
        if _local(key) == name:
            return value
    return ""


def _kids(elem: ET.Element | None, name: str) -> list[ET.Element]:
    """Дочерние элементы с таким локальным именем."""
    return [] if elem is None else [child for child in elem if _local(child.tag) == name]


def _first(elem: ET.Element | None, name: str) -> ET.Element | None:
    """Первый дочерний элемент с таким именем или None."""
    found = _kids(elem, name)
    return found[0] if found else None


def _text(elem: ET.Element | None) -> str:
    """Весь текст элемента одной строкой (пробелы схлопнуты) — для подписей, заголовков и описания."""
    return "" if elem is None else " ".join("".join(elem.itertext()).split())


def _valid_url(href: str) -> bool:
    """Внешняя ссылка годится для EPUB, только если у неё есть хост и нет пробелов (иначе epubcheck: RSC-020)."""
    try:
        return bool(urlsplit(href).hostname) and not any(ch.isspace() for ch in href)
    except ValueError:
        return False


def _esc(value: str) -> str:
    """Экранирование для текста и атрибутов XML/XHTML."""
    return html.escape(value, quote=True)


def _year(text: str) -> str:
    match = re.search(r"\b(1\d{3}|20\d{2})\b", text)
    return match.group(1) if match else ""


def _image_ext(data: bytes) -> str | None:
    """Формат растра по сигнатуре (атрибут content-type не доверяем). SVG не принимается: в нём бывают скрипты."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _person(elem: ET.Element) -> tuple[str, str]:
    """Автор или переводчик: имя для dc:creator («Имя Отчество Фамилия») и сортировка («Фамилия, Имя Отчество»)."""
    first, middle, last = (_text(_first(elem, key)) for key in ("first-name", "middle-name", "last-name"))
    name = " ".join(part for part in (first, middle, last) if part) or _text(_first(elem, "nickname"))
    given = " ".join(part for part in (first, middle) if part)
    filed = ", ".join(part for part in (last, given) if part) or name
    return name, filed


FB2_NOTE_BODIES = {"notes", "comments", "footnotes", "примечания", "сноски", "комментарии"}
XML_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
XML_AMP = re.compile(r"&(#\d+;|#x[0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]*;)?")
XML_ENTITIES = {"amp;", "lt;", "gt;", "quot;", "apos;"}


def _repair_fb2(text: str) -> str:
    """Чинит текст fb2 для второй попытки разбора: убирает запрещённые в XML символы, лишнее перед <?xml,
    голый «&» экранирует, HTML-сущности (&nbsp; и т. п.) раскрывает. Разметку не трогает."""
    def amp(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref is None:
            return "&amp;"
        if ref.startswith("#") or ref in XML_ENTITIES:
            return match.group(0)
        value = html.unescape("&" + ref)
        return _esc(value) if value != "&" + ref else "&amp;" + ref
    return XML_AMP.sub(amp, XML_BAD_CHARS.sub("", text.lstrip("\ufeff \t\r\n")))


def _parse_fb2(data: bytes) -> ET.Element:
    """Разбирает fb2 без внешних сущностей. Неразборчивый XML и DOCTYPE с ENTITY — ValueError (вызывающий откатится)."""
    text = decode_xml(data)
    if re.search(r"<!ENTITY", text[:FB2_ENTITY_HEAD], re.IGNORECASE):
        raise ValueError("DOCTYPE с ENTITY отклонён")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        try:  # частые огрехи fb2, которые терпит Calibre: управляющие символы, голый «&», HTML-сущности
            root = ET.fromstring(_repair_fb2(text))
        except ET.ParseError:
            raise ValueError(f"fb2 не разбирается: {error}") from error
    if _local(root.tag) != "FictionBook":
        raise ValueError("корень не FictionBook")
    return root


class _Pass:
    """Итоги одного прохода разметки: ссылки на сноски в порядке документа, файлы якорей, первые подписи ссылок."""

    def __init__(self) -> None:
        self.refs: list[str] = []
        self.anchors: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self.labels: dict[str, str] = {}


class _Unit:
    """Файл главы: узел (раздел или хвост тела), глубина заголовков и разделы, вынесенные в свои файлы."""

    def __init__(self, nodes: list[ET.Element], skip: set[ET.Element], depth: int) -> None:
        self.nodes = nodes
        self.skip = skip
        self.depth = depth
        self.name = ""


class _Renderer:
    """XHTML из узлов fb2 для одного файла. Ссылки на сноски и разделы берутся из книги (homes, locs) после проходов."""

    def __init__(self, book: _Fb2Book, state: _Pass, file: str, skip: set[ET.Element] | None = None) -> None:
        self.book = book
        self.state = state
        self.file = file
        self.skip = skip or set()

    def ident(self, node: ET.Element) -> str:
        """Атрибут id, если у элемента есть якорь книги; запоминает, в каком файле он выведен."""
        anchor = self.book.anchor.get(node)
        if not anchor:
            return ""
        self.state.anchors[anchor] = self.file
        return f' id="{_esc(anchor)}"'

    def block(self, node: ET.Element, depth: int, out: list[str]) -> None:
        """Блочный элемент. depth — уровень заголовков (глубина раздела)."""
        tag = _local(node.tag)
        if tag == "section":
            if node in self.skip:
                return
            out.append(f"<section{self.ident(node)}>")
            self.blocks(node, depth, out)
            out.append("</section>")
        elif tag == "title":
            level = min(depth, 6)
            out.append(f"<h{level}{self.ident(node)}>{self.lines(node)}</h{level}>")
        elif tag == "p":
            out.append(f"<p{self.ident(node)}>{self.inline(node)}</p>")
        elif tag == "subtitle":
            out.append(f'<p class="subtitle"{self.ident(node)}>{self.inline(node)}</p>')
        elif tag == "empty-line":
            out.append('<p class="empty-line">&#160;</p>')
        elif tag == "text-author":
            out.append(f'<p class="text-author"{self.ident(node)}>{self.inline(node)}</p>')
        elif tag == "date":
            out.append(f'<p class="date"{self.ident(node)}>{self.inline(node)}</p>')
        elif tag == "v":
            out.append(f'<p class="v"{self.ident(node)}>{self.inline(node)}</p>')
        elif tag in ("epigraph", "cite"):
            out.append(f'<blockquote class="{tag}"{self.ident(node)}>')
            self.blocks(node, depth, out)
            out.append("</blockquote>")
        elif tag in ("poem", "stanza"):
            out.append(f'<div class="{tag}"{self.ident(node)}>')
            self.blocks(node, depth, out, title_class="poem-title")
            out.append("</div>")
        elif tag == "annotation":
            out.append(f"<div class=\"annotation\"{self.ident(node)}>")
            self.blocks(node, depth, out)
            out.append("</div>")
        elif tag == "table":
            self.table(node, out)
        elif tag == "image":
            picture = self.image_tag(node)
            if picture:
                out.append(f'<div class="image">{picture}</div>')
        elif tag == "a" or tag in INLINE_WRAP:
            out.append(f"<p>{self.inline_node(node)}</p>")
        else:
            self.blocks(node, depth, out)

    def blocks(self, parent: ET.Element, depth: int, out: list[str], title_class: str = "",
               skip_title: bool = False) -> None:
        """Дочерние блоки родителя; текст между блоками (не в абзацах) оборачивается в <p>."""
        if parent.text and parent.text.strip():
            out.append(f"<p>{_esc(parent.text.strip())}</p>")
        for child in parent:
            tag = _local(child.tag)
            if tag == "title" and (skip_title or title_class):
                if title_class:
                    out.append(f'<div class="{title_class}">{self.lines(child)}</div>')
            else:
                self.block(child, depth + 1 if tag == "section" else depth, out)
            if child.tail and child.tail.strip():
                out.append(f"<p>{_esc(child.tail.strip())}</p>")

    def lines(self, node: ET.Element) -> str:
        """Заголовок: строки из <p> через <br/>, иначе весь текст."""
        paragraphs = _kids(node, "p")
        if not paragraphs:
            return self.inline(node)
        return "<br/>".join(self.inline(paragraph) for paragraph in paragraphs)

    def inline(self, node: ET.Element) -> str:
        """Строчное содержимое элемента с его текстом и хвостами дочерних элементов."""
        parts = [_esc(node.text or "")]
        for child in node:
            parts.append(self.inline_node(child))
            parts.append(_esc(child.tail or ""))
        return "".join(parts)

    def inline_node(self, node: ET.Element) -> str:
        tag = _local(node.tag)
        if tag == "a":
            return self.link(node)
        if tag == "image":
            return self.image_tag(node)
        wrap = INLINE_WRAP.get(tag)
        if wrap:
            return f"<{wrap}>{self.inline(node)}</{wrap}>"
        return self.inline(node)

    def link(self, node: ET.Element) -> str:
        """Ссылка: сноска — всплывающая ссылка; внутренняя — на файл цели; http(s) — как есть; прочее — только текст."""
        href = _attr(node, "href").strip()
        if href.startswith("#"):
            target = href[1:]
            if target in self.book.notes:
                return self.noteref(node, target)
            loc = self.book.locs.get(target)
            if loc is not None:
                prefix = "" if loc == self.file else loc
                return f'<a href="{_esc(prefix + "#" + target)}">{self.inline(node)}</a>'
            return self.inline(node)
        if href.lower().startswith(("http://", "https://")) and _valid_url(href):
            return f'<a href="{_esc(href)}">{self.inline(node)}</a>'
        return self.inline(node)

    def noteref(self, node: ET.Element, note: str) -> str:
        """Ссылка на сноску; первая ссылка на сноску получает id ref-<id>, повторные — ref-<id>-<номер>."""
        state = self.state
        state.refs.append(note)
        count = state.counts.get(note, 0) + 1
        state.counts[note] = count
        state.labels.setdefault(note, _text(node))
        ref_id = f"ref-{note}" if count == 1 else f"ref-{note}-{count}"
        home = self.book.homes.get(note, self.file)
        prefix = "" if home == self.file else home
        text = self.inline(node) or "*"
        return (f'<a epub:type="noteref" role="doc-noteref" class="noteref" href="{_esc(prefix + "#" + note)}" '
                f'id="{_esc(ref_id)}"><sup>{text}</sup></a>')

    def aside(self, note: str, out: list[str]) -> None:
        """Сноска целиком: <aside epub:type="footnote"> с текстом и обратной ссылкой; заголовок, совпадающий со ссылкой, опускается."""
        section = self.book.notes[note]
        self.state.anchors[note] = self.file
        out.append(f'<aside epub:type="footnote" role="doc-footnote" id="{_esc(note)}" class="footnote">')
        label = self.state.labels.get(note)
        for child in _kids(section, "title"):
            if label is None or _text(child) != label:
                out.append(f'<p class="footnote-title">{self.lines(child)}</p>')
        self.blocks(section, 2, out, skip_title=True)
        if note in self.book.referenced:
            home = self.book.homes.get(note, self.file)
            prefix = "" if home == self.file else home
            out.append(f'<p class="backlink"><a href="{_esc(prefix + "#ref-" + note)}" role="doc-backlink">↩</a></p>')
        out.append("</aside>")

    def table(self, node: ET.Element, out: list[str]) -> None:
        out.append(f"<table{self.ident(node)}>")
        for row in _kids(node, "tr"):
            out.append(f"<tr{self.ident(row)}>")
            for cell in row:
                tag = _local(cell.tag)
                if tag not in ("td", "th"):
                    continue
                attrs = [f' {name}="{value}"' for name in ("colspan", "rowspan")
                         if (value := _attr(cell, name)).isdigit()]
                align = _attr(cell, "align")
                if align in ("left", "right", "center", "justify"):
                    attrs.append(f' align="{align}"')
                paras = [child for child in cell if _local(child.tag) == "p"]
                # Нестандартные <p> внутри ячейки (листинги кода) — строки через <br/>, иначе слова склеиваются
                body = "<br/>".join(self.inline(child) for child in paras) if paras else self.inline(cell)
                out.append(f"<{tag}{self.ident(cell)}{''.join(attrs)}>{body}</{tag}>")
            out.append("</tr>")
        out.append("</table>")

    def image_tag(self, node: ET.Element) -> str:
        """<img> для картинки из <binary>; битая или отсутствующая картинка заменяется своим alt-текстом."""
        name = self.book.image_name(_attr(node, "href"))
        alt = _attr(node, "alt")
        if name is None:
            return f'<span class="image-missing">{_esc(alt)}</span>' if alt else ""
        return f'<img{self.ident(node)} src="../img/{name}" alt="{_esc(alt)}"/>'


class _Fb2Book:
    """Книга fb2: описание, картинки, тела и план файлов EPUB (главы, сноски, вынесенные разделы)."""

    def __init__(self, root: ET.Element) -> None:
        self.root = root
        desc = _first(root, "description")
        self.ti = _first(desc, "title-info")
        publish = _first(desc, "publish-info")
        self.doc_id = _text(_first(_first(desc, "document-info"), "id"))
        ti = self.ti
        self.title = _text(_first(ti, "book-title")) or "Без названия"
        self.authors = [person for person in (_person(a) for a in _kids(ti, "author")) if person[0]]
        self.translators = [person for person in (_person(a) for a in _kids(ti, "translator")) if person[0]]
        self.genres = [genre for genre in (_text(g) for g in _kids(ti, "genre")) if genre]
        self.lang = _text(_first(ti, "lang")) or "ru"
        annotation = _first(ti, "annotation")
        self.annotation = [text for text in (_text(p) for p in annotation.iter() if _local(p.tag) == "p") if text] \
            if annotation is not None else []
        if annotation is not None and not self.annotation and _text(annotation):
            self.annotation = [_text(annotation)]
        date_el = _first(ti, "date")
        self.year = _year(f"{_attr(date_el, 'value')} {_text(date_el)}") if date_el is not None else ""
        self.year = self.year or _year(_text(_first(publish, "year")))
        self.publisher = _text(_first(publish, "publisher"))
        self.isbn = re.sub(r"[\s-]", "", _text(_first(publish, "isbn")))
        seq = _first(ti, "sequence")
        self.series = _attr(seq, "name").strip()
        number = re.match(r"\d+", _attr(seq, "number").strip())
        self.series_no = number.group(0) if number else ""
        self.images: dict[str, str] = {}
        self.image_data: list[tuple[str, bytes]] = []
        self._load_images()
        cover_img = _first(_first(ti, "coverpage"), "image")
        self.cover_name = self.image_name(_attr(cover_img, "href"))
        bodies = _kids(root, "body")
        self.main = [body for body in bodies if not _attr(body, "name")]
        if not self.main and bodies and _attr(bodies[0], "name").strip().lower() not in FB2_NOTE_BODIES:
            self.main = [bodies[0]]  # первое тело с name="<название книги>" — основное (частый вариант fb2)
        if not self.main:
            raise ValueError("нет основного тела книги")
        self.anchor: dict[ET.Element, str] = {}
        self._assign_anchors()
        self.notes: dict[str, ET.Element] = {}
        note_nodes: set[ET.Element] = set()
        for body in bodies:
            if body in self.main:
                continue
            for child in body:
                anchor = self.anchor.get(child)
                if _local(child.tag) == "section" and anchor:
                    self.notes[anchor] = child
                    note_nodes.add(child)
        self.rest = [child for body in bodies if body not in self.main for child in body if child not in note_nodes]
        self.referenced: set[str] = set()
        self.homes: dict[str, str] = {}
        self.locs: dict[str, str] = {}

    def _load_images(self) -> None:
        """Картинки из <binary>: base64 и сигнатура; неизвестное и битое пропускается."""
        for node in _kids(self.root, "binary"):
            ident = node.get("id") or ""
            try:
                data = base64.b64decode("".join((node.text or "").split()), validate=True)
            except (binascii.Error, ValueError):
                continue
            ext = _image_ext(data)
            if not ident or ext is None or ident in self.images:
                continue
            name = f"{len(self.image_data) + 1:03d}.{ext}"
            self.images[ident] = name
            self.image_data.append((name, data))

    def image_name(self, href: str) -> str | None:
        """Имя файла картинки по ссылке #id или None (внешние и неизвестные ссылки)."""
        href = href.strip()
        return self.images.get(href[1:]) if href.startswith("#") else None

    def _assign_anchors(self) -> None:
        """Якоря: id элементов (первое вхождение, без повторов); у разделов без id — сгенерированный sN."""
        seen: set[str] = set()
        for node in self.root.iter():
            ident = node.get("id")
            if ident and ident not in seen:
                seen.add(ident)
                self.anchor[node] = ident
        counter = 0
        for node in self.root.iter():
            if _local(node.tag) == "section" and node not in self.anchor:
                while True:
                    counter += 1
                    candidate = f"s{counter}"
                    if candidate not in seen:
                        break
                seen.add(candidate)
                self.anchor[node] = candidate

    def _plan_section(self, node: ET.Element, depth: int, units: list[_Unit]) -> None:
        """Раздел — файл целиком; крупнее FB2_CHAPTER_MAX с вложенными разделами — голова файлом и каждый вложенный раздел своим."""
        children = _kids(node, "section")
        if not children or len(ET.tostring(node, encoding="unicode")) <= FB2_CHAPTER_MAX:
            units.append(_Unit([node], set(), depth))
            return
        units.append(_Unit([node], set(children), depth))
        for child in children:
            self._plan_section(child, depth + 1, units)

    def _plan(self) -> list[_Unit]:
        units: list[_Unit] = []
        for body in self.main:
            pending: list[ET.Element] = []
            for node in body:
                if _local(node.tag) == "section":
                    if pending:
                        units.append(_Unit(pending, set(), 1))
                        pending = []
                    self._plan_section(node, 1, units)
                else:
                    pending.append(node)
            if pending:
                units.append(_Unit(pending, set(), 1))
        for index, unit in enumerate(units, 1):
            unit.name = f"ch{index:03d}.xhtml"
        return units

    def _unit_title(self, unit: _Unit) -> str:
        first = unit.nodes[0]
        if _local(first.tag) == "section":
            return _text(_first(first, "title")) or "Раздел"
        return _text(_first(first, "title")) or self.title

    def _home(self, file_refs: list[tuple[str, list[str]]],
              note_refs: dict[str, list[str]]) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Файл, где выводится каждая сноска: первая ссылка в порядке документа (тело, затем сноски на этом месте).

        Сноски, на которые нет ссылок, и разделы служебных тел — в notes.xhtml (он последний).
        """
        homes: dict[str, str] = {}
        placed: dict[str, list[str]] = {}
        for name, refs in file_refs:
            placed[name] = []
            queue = deque(refs)
            while queue:
                note = queue.popleft()
                if note in self.notes and note not in homes:
                    homes[note] = name
                    placed[name].append(note)
                    queue.extend(note_refs.get(note, []))
        placed.setdefault(EPUB_NOTES_FILE, [])
        for note in self.notes:
            if note not in homes:
                homes[note] = EPUB_NOTES_FILE
                placed[EPUB_NOTES_FILE].append(note)
        return homes, placed

    def build(self) -> tuple[list[tuple[str, str, str]], dict[str, list[str]]]:
        """Тексты файлов: [(имя, заголовок, XHTML)] в порядке spine без обложки и страницы книги."""
        units = self._plan()
        anchors: dict[str, str] = {}
        file_refs: list[tuple[str, list[str]]] = []
        for unit in units:  # проход 1: ссылки и якоря глав
            state = _Pass()
            renderer = _Renderer(self, state, unit.name, unit.skip)
            for node in unit.nodes:
                renderer.block(node, unit.depth, [])
            anchors.update(state.anchors)
            file_refs.append((unit.name, state.refs))
        state = _Pass()
        renderer = _Renderer(self, state, EPUB_NOTES_FILE)
        for node in self.rest:
            renderer.block(node, 1, [])
        anchors.update(state.anchors)
        file_refs.append((EPUB_NOTES_FILE, state.refs))
        note_refs: dict[str, list[str]] = {}  # проход 2: содержимое каждой сноски
        note_anchors: dict[str, dict[str, str]] = {}
        for note in self.notes:
            state = _Pass()
            _Renderer(self, state, "").aside(note, [])
            note_refs[note] = state.refs
            note_anchors[note] = state.anchors
        self.homes, placed = self._home(file_refs, note_refs)
        self.referenced = {note for _name, refs in file_refs for note in refs}
        self.referenced.update(note for refs in note_refs.values() for note in refs)
        self.locs = anchors
        for note, found in note_anchors.items():
            for anchor in found:
                self.locs[anchor] = self.homes[note]
            self.locs[note] = self.homes[note]
        final = _Pass()  # проход 3: окончательная разметка с известными файлами сносок и якорей
        texts: list[tuple[str, str, str]] = []
        for unit in units:
            renderer = _Renderer(self, final, unit.name, unit.skip)
            out: list[str] = []
            for node in unit.nodes:
                renderer.block(node, unit.depth, out)
            for note in placed.get(unit.name, []):
                renderer.aside(note, out)
            texts.append((unit.name, self._unit_title(unit), "".join(out)))
        renderer = _Renderer(self, final, EPUB_NOTES_FILE)
        out = []
        for node in self.rest:
            renderer.block(node, 1, out)
        for note in placed.get(EPUB_NOTES_FILE, []):
            renderer.block(self.notes[note], 1, out)
        if out:
            texts.append((EPUB_NOTES_FILE, "Примечания", "".join(out)))
        return texts, placed

    def nav_items(self, container: ET.Element) -> list[tuple[str, str, list[Any]]]:
        """Оглавление: (название, href относительно OEBPS, вложенные пункты) по разделам контейнера."""
        items: list[tuple[str, str, list[Any]]] = []
        for node in _kids(container, "section"):
            anchor = self.anchor.get(node, "")
            loc = self.locs.get(anchor)
            if loc is None:
                continue
            items.append((_text(_first(node, "title")) or "Раздел", f"text/{loc}#{anchor}", self.nav_items(node)))
        return items

    def title_body(self) -> str:
        """Страница книги: название, авторы, перевод, серия, аннотация."""
        parts = [f"<h1>{_esc(self.title)}</h1>"]
        parts += [f'<p class="author">{_esc(name)}</p>' for name, _filed in self.authors]
        parts += [f'<p class="translator">Перевод: {_esc(name)}</p>' for name, _filed in self.translators]
        if self.series:
            number = f", № {self.series_no}" if self.series_no else ""
            parts.append(f'<p class="series">Серия: {_esc(self.series)}{_esc(number)}</p>')
        if self.annotation:
            parts.append('<div class="annotation">' + "".join(f"<p>{_esc(p)}</p>" for p in self.annotation) + "</div>")
        return "".join(parts)


def _page(title: str, lang: str, body: str, css: str = "../css/book.css", body_class: str = "") -> str:
    """Документ XHTML: DOCTYPE, пространство epub, lang и xml:lang, заголовок и ссылка на таблицу стилей."""
    attrs = f' class="{_esc(body_class)}"' if body_class else ""
    code = _esc(lang)
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            f'lang="{code}" xml:lang="{code}"><head><meta charset="utf-8"/><title>{_esc(title)}</title>'
            f'<link rel="stylesheet" type="text/css" href="{css}"/></head><body{attrs}>{body}</body></html>\n')


def _nav_list(items: list[tuple[str, str, list[Any]]]) -> str:
    if not items:
        return ""
    return "<ol>" + "".join(f'<li><a href="{_esc(href)}">{_esc(title)}</a>{_nav_list(children)}</li>'
                            for title, href, children in items) + "</ol>"


def _ncx_points(items: list[tuple[str, str, list[Any]]], counter: list[int]) -> str:
    parts = []
    for title, href, children in items:
        counter[0] += 1
        number = counter[0]
        inner = _ncx_points(children, counter)
        parts.append(f'<navPoint id="np{number}" playOrder="{number}"><navLabel><text>{_esc(title)}</text></navLabel>'
                     f'<content src="{_esc(href)}"/>{inner}</navPoint>')
    return "".join(parts)


def _nav_depth(items: list[tuple[str, str, list[Any]]]) -> int:
    return max((1 + _nav_depth(children) for _title, _href, children in items), default=0)


def fb2_to_epub3(data: bytes, out: Path) -> None:
    """Пишет EPUB 3 из fb2 (данные уже прошли ensure_fb2_namespace). Неразборчивый fb2 — ValueError: откат на Calibre.

    Главы — файлы text/chNNN.xhtml; сноски — всплывающие <aside epub:type="footnote"> в файле первой ссылки и
    обычный текст в notes.xhtml; NCX для читалок EPUB 2. Текст не меняется, только экранируется.
    """
    book = _Fb2Book(_parse_fb2(data))
    texts, _placed = book.build()
    lang = book.lang
    pages: list[tuple[str, str, str]] = []  # (путь в OEBPS, идентификатор в манифесте, XHTML)
    if book.cover_name:
        cover_body = f'<div class="cover-wrap"><img src="../img/{book.cover_name}" alt="Обложка" class="cover-img"/></div>'
        pages.append(("text/cover.xhtml", "cover", _page("Обложка", lang, cover_body, body_class="cover")))
    pages.append(("text/title.xhtml", "title-page", _page(book.title, lang, book.title_body())))
    for name, title, body in texts:
        pages.append((f"text/{name}", name.rpartition(".")[0], _page(title, lang, body)))
    top = [item for body in book.main for item in book.nav_items(body)]
    chapters = [(name, title) for name, title, _body in texts if name.startswith("ch")]
    if not top and chapters:
        top = [(chapters[0][1], f"text/{chapters[0][0]}", [])]
    if any(name == EPUB_NOTES_FILE for name, _title, _body in texts):
        top.append(("Примечания", f"text/{EPUB_NOTES_FILE}", []))
    landmarks = []
    if book.cover_name:
        landmarks.append('<li><a epub:type="cover" href="text/cover.xhtml">Обложка</a></li>')
    if chapters:
        landmarks.append(f'<li><a epub:type="bodymatter" href="text/{chapters[0][0]}">{_esc(book.title)}</a></li>')
    nav_body = (f'<nav epub:type="toc" id="toc"><h1>Оглавление</h1>{_nav_list(top)}</nav>'
                f'<nav epub:type="landmarks" id="landmarks" hidden=""><ol>{"".join(landmarks)}</ol></nav>')
    nav_page = _page("Оглавление", lang, nav_body, css="css/book.css")
    counter = [0]
    ncx_points = _ncx_points(top, counter)
    ident = book.doc_id or "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                       hashlib.sha1(data, usedforsecurity=False).hexdigest()))
    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<head><meta name="dtb:uid" content="{_esc(ident)}"/><meta name="dtb:depth" content="{_nav_depth(top)}"/>'
           '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>'
           f"<docTitle><text>{_esc(book.title)}</text></docTitle><navMap>{ncx_points}</navMap></ncx>\n")
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="css/book.css" media-type="text/css"/>']
    spine = []
    for path, item_id, _xhtml in pages:
        manifest.append(f'<item id="{item_id}" href="{path}" media-type="application/xhtml+xml"/>')
        linear = ' linear="yes"' if item_id == "cover" else ""
        spine.append(f'<itemref idref="{item_id}"{linear}/>')
    for name, _payload in book.image_data:
        media = EPUB_IMAGE_TYPES[name.rpartition(".")[2]]
        if name == book.cover_name:
            manifest.append(f'<item id="cover-image" href="img/{name}" media-type="{media}" properties="cover-image"/>')
        else:
            item_id = "img-" + name.rpartition(".")[0]
            manifest.append(f'<item id="{item_id}" href="img/{name}" media-type="{media}"/>')
    creators, translators = [], []
    for number, (name, filed) in enumerate(book.authors, 1):
        creators.append(f'<dc:creator id="creator{number}">{_esc(name)}</dc:creator>'
                        f'<meta refines="#creator{number}" property="file-as">{_esc(filed)}</meta>'
                        f'<meta refines="#creator{number}" property="role" scheme="marc:relators">aut</meta>')
    for number, (name, filed) in enumerate(book.translators, 1):
        translators.append(f'<dc:contributor id="translator{number}">{_esc(name)}</dc:contributor>'
                           f'<meta refines="#translator{number}" property="file-as">{_esc(filed)}</meta>'
                           f'<meta refines="#translator{number}" property="role" scheme="marc:relators">trl</meta>')
    meta = [f'<dc:identifier id="pub-id">{_esc(ident)}</dc:identifier>']
    if book.isbn:
        meta.append(f"<dc:identifier>urn:isbn:{_esc(book.isbn)}</dc:identifier>")
    meta.append(f'<dc:title id="book-title">{_esc(book.title)}</dc:title>')
    meta.append(f"<dc:language>{_esc(lang)}</dc:language>")
    meta += creators + translators
    meta += [f"<dc:subject>{_esc(genre_name(genre))}</dc:subject>" for genre in book.genres]
    if book.publisher:
        meta.append(f"<dc:publisher>{_esc(book.publisher)}</dc:publisher>")
    if book.year:
        meta.append(f"<dc:date>{_esc(book.year)}</dc:date>")
    if book.annotation:
        meta.append(f"<dc:description>{_esc(chr(10).join(book.annotation))}</dc:description>")
    if book.series:
        meta.append(f'<meta property="belongs-to-collection" id="c1">{_esc(book.series)}</meta>')
        meta.append('<meta refines="#c1" property="collection-type">series</meta>')
        meta.append(f'<meta name="calibre:series" content="{_esc(book.series)}"/>')
        if book.series_no:
            meta.append(f'<meta refines="#c1" property="group-position">{_esc(book.series_no)}</meta>')
            meta.append(f'<meta name="calibre:series_index" content="{_esc(book.series_no)}"/>')
    meta.append(f'<meta property="dcterms:modified">{modified}</meta>')
    if book.cover_name:
        meta.append('<meta name="cover" content="cover-image"/>')
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" xml:lang="{_esc(lang)}">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/">'
           + "".join(meta) + "</metadata><manifest>" + "".join(manifest) + "</manifest>"
           '<spine toc="ncx">' + "".join(spine) + "</spine></package>\n")
    files: list[tuple[str, bytes]] = [("META-INF/container.xml", EPUB_CONTAINER.encode("utf-8")),
                                      ("OEBPS/content.opf", opf.encode("utf-8")),
                                      ("OEBPS/nav.xhtml", nav_page.encode("utf-8")),
                                      ("OEBPS/toc.ncx", ncx.encode("utf-8")),
                                      ("OEBPS/css/book.css", EPUB_CSS.encode("utf-8"))]
    files += [(f"OEBPS/{path}", xhtml.encode("utf-8")) for path, _item, xhtml in pages]
    files += [(f"OEBPS/img/{name}", payload) for name, payload in book.image_data]
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, payload in files:
            archive.writestr(name, payload)


def fb2_own_epub(data: bytes, produced: Path) -> str | None:
    """Собственный конвертер и его проверки: None — epub годен, иначе причина (откат на Calibre)."""
    try:
        fb2_to_epub3(data, produced)
        return verify_book(produced, "epub") or epub_text_loss(data, produced)
    except ValueError as error:
        return f"fb2epub3: {error}"
    except (OSError, KeyError, zipfile.BadZipFile, EOFError) as error:
        return f"fb2epub3: {type(error).__name__}: {error}"


def calibre_fb2_problem(data: bytes, staged: Path, produced: Path, tmp: Path, timeout: int) -> str | None:
    """Calibre (ebook-convert) для fb2 и проверка результата; None — epub годен, иначе причина."""
    if not shutil.which("ebook-convert"):
        return "ebook-convert: не найден (Calibre)"
    try:
        done = subprocess.run(["ebook-convert", str(staged), str(produced)], capture_output=True, env=calibre_env(tmp),
                              cwd=tmp, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
        if done.returncode != 0 and b"SplitError" in done.stderr:  # одна огромная глава: без нарезки на файлы
            done = subprocess.run(["ebook-convert", str(staged), str(produced), "--flow-size", "0"],
                                  capture_output=True, env=calibre_env(tmp), cwd=tmp, timeout=timeout,
                                  check=False, stdin=subprocess.DEVNULL)
        problem = None if done.returncode == 0 and produced.is_file() else \
            f"ebook-convert: {done.stderr.decode('utf-8', errors='replace')[-300:] or done.returncode}"
    except subprocess.TimeoutExpired:
        return f"ebook-convert: тайм-аут {timeout} с"
    try:
        return problem or verify_book(produced, "epub") or epub_text_loss(data, produced)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
        return f"проверка epub: {type(error).__name__}: {error}"


# ----------------------------------------------------------------------
# convert fb2 → epub (Calibre), в т. ч. fb2 внутри однокнижного zip
# ----------------------------------------------------------------------

def convert_fb2(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
                out: Callable[[str], None] = print) -> dict[str, Any]:
    """fb2 и однокнижный zip с fb2 → name.epub рядом (имя как у fb2), проверка verify_book, исходник — в карантин
    replaced/. Рядом уже есть epub с тем же нормализованным именем — исходник сразу в replaced/ без конвертации.
    Сначала собственный конвертер fb2_to_epub3; при его ошибке или провале проверок — Calibre. Поле engine в журнале:
    fb2epub3 или calibre. Сбой обоих или проверки — исходник в rejected-convert/."""
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
            engine = "fb2epub3"
            problem = fb2_own_epub(data, produced)
            if problem:
                produced.unlink(missing_ok=True)
                engine = "calibre"
                problem = calibre_fb2_problem(data, staged, produced, tmp, timeout)
            if problem:
                run.quarantine(src, "rejected-convert", reason=problem, engine=engine)
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
        run.quarantine(src, "replaced", reason="заменён проверенным epub", keeper=str(result), engine=engine)
        run.moved(src, result)

    errors: list[str] = []
    parallel(tasks(), one, workers or cfg["workers"], run)
    return run.finish({"walk_errors": errors[:200]})


# ----------------------------------------------------------------------
# reconvert-fb2: epub, которые ночью собрал Calibre из fb2, пересобираются собственным конвертером
# ----------------------------------------------------------------------

# Окно даты calibre:timestamp (локальная дата): ночные прогоны 16–18 ноября 2026 года.
RECONVERT_FROM = date(2026, 11, 16)
RECONVERT_TO = date(2026, 11, 18)
# Новый epub не должен терять больше 2 % слов относительно calibre-epub.
RECONVERT_WORDS_RATIO = 0.98
# Для сопоставления с fb2 читается только описание: первые 64 КБ файла.
FB2_SOURCE_HEAD = 64 * 1024
SOURCE_LIMIT = 256 * 1024 * 1024


def epub_opf_facts(epub: Path) -> dict[str, Any]:
    """Признаки из OPF epub (читается только он): метка calibre в dc:contributor opf:role="bkp", дата
    calibre:timestamp, название и первый автор."""
    with zipfile.ZipFile(epub) as archive:
        opf = Epub(archive).opf
    calibre = any(re.search(r'\bopf:role\s*=\s*"bkp"', attrs) and strip_tags(body).startswith("calibre")
                  for attrs, body in re.findall(r"<dc:contributor\b([^>]*)>(.*?)</dc:contributor>", opf, re.S))
    stamp = None
    for tag in re.findall(r"<(?:\w+:)?meta\b([^>]*)>", opf):
        attrs = parse_attrs(tag)
        if attrs.get("name") == "calibre:timestamp":
            try:
                stamp = datetime.fromisoformat(attrs.get("content", "")).astimezone().date()
            except ValueError:
                stamp = None
    return {"calibre": calibre, "date": stamp, "title": xml_tag("title", opf), "creator": xml_tag("creator", opf)}


def fb2_source_head(path: Path) -> bytes:
    """Начало fb2 — из файла или из единственной записи однокнижного zip."""
    if path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            info = single_member(archive)
            if info is None or member_ext(info) != "fb2":
                raise ValueError("zip без единственного fb2")
            with archive.open(info) as handle:
                return handle.read(FB2_SOURCE_HEAD)
    with path.open("rb") as handle:
        return handle.read(FB2_SOURCE_HEAD)


def fb2_match_key(head: bytes) -> tuple[str, str] | None:
    """Ключ книги из описания fb2: (norm_key названия, norm_key фамилии первого автора) или None."""
    text = decode_xml(head)
    info = re.search(r"<(?:\w+:)?title-info\b.*", text, re.S)
    if info is None:
        return None
    title = norm_key(xml_tag("book-title", info.group(0)))
    first = re.search(r"<(?:\w+:)?author\b[^>]*>(.*?)</(?:\w+:)?author>", info.group(0), re.S)
    if first is None or not title:
        return None
    surname = norm_key(xml_tag("last-name", first.group(1)) or xml_tag("nickname", first.group(1)))
    return (title, surname) if surname else None


def fb2_source_index(folder: Path, titles: set[str], workers: int, errors: list[str]) -> dict[str, list[tuple[str, str, Path]]]:
    """Индекс fb2 карантина replaced/ по названию: [(фамилия, стем имени файла, путь)]. Читается только описание."""
    index: dict[str, list[tuple[str, str, Path]]] = {}
    lock = threading.Lock()

    def one(item: tuple[Path, int]) -> None:
        path = item[0]
        try:
            key = fb2_match_key(fb2_source_head(path))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
            errors.append(f"{path}: {type(error).__name__}: {error}")
            return
        if key is None or key[0] not in titles:
            return
        with lock:
            index.setdefault(key[0], []).append((key[1], split_name(path.name)[0], path))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, walk_files(folder, (".fb2", ".fb2.zip"), errors)))
    return index


def _strip_number(stem: str) -> str:
    """Стем без суффикса _N, который дают имена копий (name_1.epub)."""
    return re.sub(r"_\d+$", "", stem)


def reconvert_match(title: str, creator: str, stem: str,
                    index: dict[str, list[tuple[str, str, Path]]]) -> tuple[Path | None, str]:
    """Один fb2 для calibre-epub: по названию и фамилии; при нескольких — тот, чей стем совпадает. (путь, '') или (None, причина)."""
    names = f" {creator} "
    pool = [entry for entry in index.get(title, []) if f" {entry[0]} " in names]
    if not pool:
        return None, "нет fb2 с таким названием и автором"
    if len(pool) == 1:
        return pool[0][2], ""
    want = norm_key(_strip_number(stem))
    exact = [path for _surname, fb_stem, path in pool if norm_key(_strip_number(fb_stem)) == want]
    if len(exact) == 1:
        return exact[0], ""
    return None, f"неоднозначно: {len(pool)} кандидатов"


def source_bytes(path: Path) -> bytes:
    """Полный fb2 (или единственная запись однокнижного zip) с пределом размера."""
    return fb2_bytes(path, "fb2.zip" if path.name.lower().endswith(".zip") else "fb2", SOURCE_LIMIT)


def epubcheck_problem(path: Path) -> str | None:
    """Выборочная проверка epubcheck (опция --epubcheck): None — ошибок ERROR/FATAL нет, иначе причина."""
    tool = shutil.which("epubcheck")
    if tool is None:
        raise ConfigError("epubcheck не найден")
    done = subprocess.run([tool, str(path)], capture_output=True, text=True, timeout=600, check=False,
                          stdin=subprocess.DEVNULL)
    lines = [line for line in (done.stdout + done.stderr).splitlines() if "ERROR(" in line or "FATAL(" in line]
    if lines or done.returncode != 0:
        return f"epubcheck: {lines[0] if lines else done.returncode}"
    return None


def reconvert_build(full: Path, source: Path, work: Path, check: bool) -> tuple[Path | None, str]:
    """Собирает epub собственным конвертером во временный файл в work и проверяет его.
    Возвращает (путь к готовому epub, '') или (None, причина): старый calibre-epub остаётся."""
    produced = Path(_temp_name(work))
    try:
        data = ensure_fb2_namespace(source_bytes(source))
        try:
            fb2_to_epub3(data, produced)
        except ValueError as error:
            return _discard(produced, f"fb2epub3: {error}")
        problem = verify_book(produced, "epub") or epub_text_loss(data, produced)
        if problem is None:
            new_words, old_words = epub_word_count(produced), epub_word_count(full)
            if new_words < RECONVERT_WORDS_RATIO * old_words:
                problem = f"слов {new_words} из {old_words} у calibre-epub"
        if problem is None and check:
            problem = epubcheck_problem(produced)
        if problem is not None:
            return _discard(produced, problem)
        return produced, ""
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
        return _discard(produced, f"проверка: {type(error).__name__}: {error}")


def _temp_name(directory: Path) -> str:
    fd, name = tempfile.mkstemp(prefix="reconvert-", suffix=".epub", dir=directory)
    os.close(fd)
    return name


def _discard(produced: Path, reason: str) -> tuple[None, str]:
    produced.unlink(missing_ok=True)
    return None, reason


def swap_epub(run: NightRun, full: Path, produced: Path) -> None:
    """Атомарная замена: новый epub копируется рядом с книгой, старый уходит в replaced-calibre/, затем os.replace.
    Путь книги не меняется, relink не нужен."""
    staged = Path(_temp_name(full.parent))
    try:
        shutil.copyfile(produced, staged)
        os.chmod(staged, 0o644)
        old = run.quarantine(full, "replaced-calibre", reason="заменён epub собственного конвертера", engine="fb2epub3")
        try:
            os.replace(staged, full)
        except OSError:
            shutil.move(old, full)
            raise
    finally:
        staged.unlink(missing_ok=True)


def reconvert_sample_pick(matches: list[tuple[dict[str, Any], Path]], count: int) -> list[tuple[dict[str, Any], Path]]:
    """Выборка для проверки глазами: сначала книги со сносками (type="note" в fb2), затем прочие."""
    with_notes: list[tuple[dict[str, Any], Path]] = []
    plain: list[tuple[dict[str, Any], Path]] = []
    for item in matches[:3000]:
        if len(with_notes) >= count:
            break
        raw = source_bytes(item[1])
        (with_notes if b'type="note"' in raw or b"type='note'" in raw else plain).append(item)
    return (with_notes + plain)[:count]


def reconvert_fb2(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
                  limit: int = 0, sample: int = 0, out_dir: str | None = None, epubcheck: bool = False,
                  out: Callable[[str], None] = print) -> dict[str, Any]:
    """Пересобирает calibre-epub из fb2 собственным конвертером. Выбор: fmt='epub' в БД и calibre:timestamp в окне дат;
    fb2 ищется в карантине replaced/. Ровно один кандидат — он; иначе — по стему имени; иначе unmatched.

    dry-run — только сопоставление. --sample N --out DIR — N книг в DIR без замены в библиотеке.
    Боевой запуск: сборка в text_cache_dir/reconvert, проверки, затем замена под общей блокировкой.
    """
    db_path = Path(cfg["db_path"])
    if not db_path.is_file():
        raise ConfigError(f"нет базы данных {db_path}")
    if epubcheck and not shutil.which("epubcheck"):
        raise ConfigError("epubcheck не найден: опция --epubcheck недоступна")
    run = NightRun(cfg, "reconvert-fb2", dry_run, path, out)
    try:
        prefix = "" if run.target == run.root else run.target.relative_to(run.root).as_posix() + "/"
    except ValueError as error:
        raise ConfigError("reconvert-fb2: путь должен лежать внутри каталога книг") from error
    with closing(connect(cfg["db_path"], readonly=True)) as conn:
        rows = sorted(row[0] for row in conn.execute("SELECT path FROM books WHERE fmt = 'epub'") if row[0].startswith(prefix))
    workers = workers or cfg["workers"]
    candidates: list[dict[str, Any]] = []
    lock = threading.Lock()

    def scan(rel: str) -> None:
        full = run.root / rel
        facts = epub_opf_facts(full)
        if not facts["calibre"]:
            run.count("not-calibre")
            return
        stamp = facts["date"]
        if stamp is None or not RECONVERT_FROM <= stamp <= RECONVERT_TO:
            run.count("out-of-window")
            return
        with lock:
            candidates.append({"rel": rel, "full": full, "title": norm_key(facts["title"]),
                               "creator": norm_key(facts["creator"]), "stem": split_name(rel)[0]})

    parallel(iter(rows), scan, workers, run)
    errors: list[str] = []
    index = fb2_source_index(run.qroot / "replaced", {c["title"] for c in candidates}, workers, errors) if candidates else {}
    matches: list[tuple[dict[str, Any], Path]] = []
    for cand in sorted(candidates, key=lambda c: c["rel"]):
        source, reason = reconvert_match(cand["title"], cand["creator"], cand["stem"], index)
        if source is None:
            run.record("unmatched", cand["full"], reason=reason)
            if reason.startswith("неоднозначно"):
                run.count("ambiguous")
            continue
        run.record("matched", cand["full"], source=str(source))
        matches.append((cand, source))
    if limit:
        matches = matches[:limit]
    if dry_run:
        return run.finish({"walk_errors": errors[:200]})
    work = Path(cfg["text_cache_dir"]) / "reconvert" if cfg["text_cache_dir"] else None
    if work is None:
        raise ConfigError("reconvert-fb2 нужен text_cache_dir: сборка идёт на быстром диске")
    work.mkdir(parents=True, exist_ok=True)
    if sample:
        if not out_dir:
            raise ConfigError("--sample требует --out")
        target = Path(out_dir).expanduser()
        if target.resolve().is_relative_to(run.root.resolve()):
            raise ConfigError("--out не должен лежать в каталоге книг: выборка не индексируется")
        target.mkdir(parents=True, exist_ok=True)
        chosen = reconvert_sample_pick(matches, sample)

        def sample_one(item: tuple[dict[str, Any], Path]) -> None:
            cand, source = item
            produced, problem = reconvert_build(cand["full"], source, work, epubcheck)
            if produced is None:
                run.record("sample-rejected", cand["full"], reason=problem)
                return
            dest = unique_path(target, f"{cand['stem']}.epub", set())
            shutil.move(produced, dest)
            run.record("sample", cand["full"], to=str(dest), source=str(source), notes=b'type="note"' in source_bytes(source),
                       old_size=cand["full"].stat().st_size, new_size=dest.stat().st_size)

        parallel(iter(chosen), sample_one, workers, run)
        return run.finish({"sample_dir": str(target), "walk_errors": errors[:200]})

    def replace_one(item: tuple[dict[str, Any], Path]) -> None:
        cand, source = item
        produced, problem = reconvert_build(cand["full"], source, work, epubcheck)
        if produced is None:
            run.record("kept-calibre", cand["full"], reason=problem, source=str(source))
            return
        try:
            swap_epub(run, cand["full"], produced)
        finally:
            produced.unlink(missing_ok=True)

    parallel(iter(matches), replace_one, workers, run)
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


# Кэш текста для dedup: восстановимые данные в text_cache_dir (по умолчанию /var/cache/books_library). Хэши шинглов
# зависят от PYTHONHASHSEED и не кэшируются: кэшируется только извлечённый текст.
TEXT_CACHE_NAME = "text_cache.db"
TEXT_CACHE_MTIME_TOLERANCE = 1e-3


class TextCache:
    """Текст книги по ключу: путь относительно books_dir, размер, mtime и формат. Если файл переименован или перенесён
    без изменения содержимого, запись находится по размеру, mtime и формату и получает новый путь. Ошибки SQLite не
    останавливают dedup: промах кэша — обычная работа. Соединение общее для потоков, все обращения — под замком."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.lock = threading.Lock()

    @staticmethod
    def open(cfg: dict[str, Any]) -> TextCache | None:
        """None — кэш выключен (каталог не задан, text_cache_mb = 0, каталога нет или он не доступен для записи)."""
        directory = cfg["text_cache_dir"]
        if not directory or cfg["text_cache_mb"] == 0:
            return None
        path = Path(directory)
        # Каталог создаёт администратор (права 0700): движок его не создаёт.
        if not path.is_dir() or not os.access(path, os.W_OK):
            say(f"кэш текста выключен: {path} не существует или нет прав на запись", "text_cache_off", logging.WARNING)
            return None
        try:
            # isolation_level=None: каждая команда — отдельная транзакция, явные BEGIN/COMMIT не нужны.
            conn = sqlite3.connect(path / TEXT_CACHE_NAME, timeout=10, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS texts (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
                         "mtime REAL NOT NULL, fmt TEXT NOT NULL, data BLOB NOT NULL, used REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS texts_size_mtime_fmt ON texts (size, mtime, fmt)")
        except sqlite3.Error as error:
            say(f"кэш текста выключен: {path}: {error}", "text_cache_off", logging.WARNING)
            return None
        return TextCache(conn)

    def get(self, rel: str, size: int, mtime: float, fmt: str) -> str | None:
        """Текст из кэша или None. Запись без совпадения по пути берётся, только если её единственная по размеру,
        mtime и формату (два кандидата — промах: не угадываем, какой файл был переименован)."""
        window = (mtime - TEXT_CACHE_MTIME_TOLERANCE, mtime + TEXT_CACHE_MTIME_TOLERANCE)
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT rowid, data FROM texts WHERE path = ? AND size = ? AND mtime BETWEEN ? AND ? AND fmt = ?",
                    (rel, size, *window, fmt)).fetchone()
                if row is None:
                    found = self.conn.execute(
                        "SELECT rowid, data FROM texts WHERE size = ? AND mtime BETWEEN ? AND ? AND fmt = ?",
                        (size, *window, fmt)).fetchall()
                    if len(found) != 1:
                        return None
                    row = found[0]
                try:
                    text = zlib.decompress(row[1]).decode("utf-8")
                except (zlib.error, UnicodeDecodeError):
                    self.conn.execute("DELETE FROM texts WHERE rowid = ?", (row[0],))
                    return None
                self.conn.execute("UPDATE OR REPLACE texts SET path = ?, used = ? WHERE rowid = ?", (rel, time.time(), row[0]))
                return text
        except sqlite3.Error as error:
            say(f"кэш текста: чтение не удалось: {error}", "text_cache_error", logging.WARNING)
            return None

    def put(self, rel: str, size: int, mtime: float, fmt: str, text: str) -> None:
        """Сохраняет текст, в том числе пустой: «текста нет» у скана — дорогой результат, который тоже нужно помнить."""
        data = zlib.compress(text.encode("utf-8", errors="replace"), 1)
        try:
            with self.lock:
                self.conn.execute("INSERT OR REPLACE INTO texts (path, size, mtime, fmt, data, used) VALUES (?, ?, ?, ?, ?, ?)",
                                  (rel, size, mtime, fmt, data, time.time()))
        except sqlite3.Error as error:
            say(f"кэш текста: запись не удалась: {error}", "text_cache_error", logging.WARNING)

    def prune(self, limit_mb: int) -> None:
        """Сверх предела удаляет самые давно использованные записи. VACUUM не делается: освобождённые страницы
        переиспользуются самой базой; WAL усекается чекпоинтом."""
        limit = limit_mb * 1024 * 1024
        try:
            with self.lock:
                total = self.conn.execute("SELECT COALESCE(SUM(length(data)), 0) FROM texts").fetchone()[0]
                if total > limit:
                    for rowid, size in self.conn.execute(
                            "SELECT rowid, length(data) FROM texts ORDER BY used, rowid").fetchall():
                        if total <= limit:
                            break
                        self.conn.execute("DELETE FROM texts WHERE rowid = ?", (rowid,))
                        total -= size
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as error:
            say(f"кэш текста: очистка не удалась: {error}", "text_cache_error", logging.WARNING)

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def shingles(text: str) -> tuple[int, set[int], bool]:
    """Число слов и хэши шинглов по 5 слов. У длинных текстов — выборка хэшей с остатком 0 по модулю 4: фильтр один
    для всех файлов, поэтому доля «шинглы младшего в старшем» оценивается без смещения, а памяти вчетверо меньше."""
    words = re.findall(r"[^\W_]+", fold(text))
    hashes = {hash(tuple(words[i:i + 5])) for i in range(max(len(words) - 4, 0))}
    sampled = len(words) > 20000
    if sampled:
        hashes = {h for h in hashes if h % 4 == 0}
    return len(words), hashes, sampled


def is_author_folder(folder: str, authors: list[str]) -> bool:
    """Папка верхнего уровня — папка одного из авторов книги: ключи равны или одно ФИО ⊆/⊇ другому (≥ 2 общих слова)."""
    folder_key = author_key(folder)
    if folder_key is None:
        return False
    for author in authors:
        key = author_key(author)
        if key is not None and (folder_key == key or (len(folder_key & key) >= 2 and (folder_key <= key or folder_key >= key))):
            return True
    return False


def coauthor_copy(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Копии одной книги в папках разных авторов книги (книга нескольких авторов) — соавторские, а не дубли:
    у каждой копии папка верхнего уровня — папка одного из авторов из метаданных. Внутри одной папки — не соавторские."""
    top_first, top_second = first["path"].split("/", 1)[0], second["path"].split("/", 1)[0]
    authors = list(first["authors"]) + list(second["authors"])
    return (top_first != top_second and is_author_folder(top_first, authors) and is_author_folder(top_second, authors))


def known_books(cfg: dict[str, Any]) -> dict[str, sqlite3.Row]:
    """Строки библиотеки по пути для dedup и dedup-folders: метаданные не разбираются заново, если файл не менялся."""
    known: dict[str, sqlite3.Row] = {}
    if Path(cfg["db_path"]).is_file():
        with closing(connect(cfg["db_path"], readonly=True)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
                known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime, fmt, title, authors FROM books")}
    return known


def item_meta(root: Path, known: dict[str, sqlite3.Row], item: tuple[str, int, float]) -> dict[str, Any]:
    """Метаданные файла для сравнения: из библиотеки, если размер и mtime совпадают, иначе — разбор файла."""
    rel, size, mtime = item
    row = known.get(rel)
    if row and row["size"] == size and row["mtime"] == mtime and row["fmt"] != "fb2.zip":
        return {"fmt": row["fmt"], "title": row["title"], "authors": [a for a in row["authors"].split("; ") if a]}
    return read_book_meta(root, rel)


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
    known = known_books(cfg)
    files = [(rel, size, mtime) for rel, size, mtime in iter_book_files(run.target, errors)]
    prefix = run.rel(run.target)
    prefix = "" if prefix in ("", ".") else prefix + "/"
    files = [(prefix + rel, size, mtime) for rel, size, mtime in files]

    with ThreadPoolExecutor(max_workers=workers or cfg["workers"]) as pool:
        metas = list(pool.map(lambda item: item_meta(root, known, item), files, chunksize=64))
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
        # суффикс _1, _2… от unique_path (конфликт имён при переносе) не мешает найти кандидата; решает сравнение текста
        keys = [f"n:{PurePosixPath(rel).parent}/{norm_key(re.sub(r'_\d+$', '', split_name(rel)[0]))}"]
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
    # Кэш открывается один раз на прогон и не меняет библиотеку, поэтому работает и в dry-run.
    cache = TextCache.open(cfg)

    def one(group: list[int]) -> None:
        if len(group) > DEDUP_GROUP_MAX:
            run.record("group-too-large", files[group[0]][0], members=len(group))
            return
        members = []
        with tempfile.TemporaryDirectory(prefix="books_library-dedup-") as tmp_name:
            for index in group:
                rel, size, mtime = files[index]
                fmt = metas[index]["fmt"]
                try:
                    text = cache.get(rel, size, mtime, fmt) if cache is not None else None
                    if text is None:
                        text = book_text(cfg, root / rel, fmt, Path(tmp_name))
                        if cache is not None:
                            cache.put(rel, size, mtime, fmt, text)
                            run.count("text_cache_miss")
                    else:
                        run.count("text_cache_hit")
                    words, shingle_set, sampled = shingles(text)
                except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError, subprocess.TimeoutExpired) as error:
                    words, shingle_set, sampled = 0, set(), False
                    run.record("no-text", rel, reason=f"{type(error).__name__}: {error}")
                members.append({"path": rel, "fmt": fmt, "size": size, "words": words, "sh": shingle_set, "sampled": sampled,
                                "authors": metas[index]["authors"]})
        members.sort(key=lambda m: (FORMAT_RANK.get(m["fmt"], 5), -m["words"], -m["size"]))
        # копии одной книги в папках её соавторов — не дубли: в группу не входят (счётчик coauthor-copy)
        unique_members: list[dict[str, Any]] = []
        for member in members:
            if any(coauthor_copy(member, other) for other in unique_members):
                run.count("coauthor-copy")
                continue
            unique_members.append(member)
        members = unique_members
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

    try:
        parallel(iter(candidates), one, workers or cfg["workers"], run)
    finally:
        if cache is not None:
            cache.prune(cfg["text_cache_mb"])
            cache.close()
            say(f"dedup: кэш текста: попаданий {run.stats.get('text_cache_hit', 0)}, "
                f"промахов {run.stats.get('text_cache_miss', 0)}", "progress")
    if not dry_run and results:
        migrate_database(cfg["db_path"], out=out)
        with closing(connect(cfg["db_path"])) as conn, conn:
            resolved = {r[0] for r in conn.execute("SELECT key FROM dup_groups WHERE status='resolved'")}
            # заменяются только «похожие»: «удалены автоматически» — история прошлых прогонов (кнопка «Восстановить»)
            # группы dedup-folders (key folder:…) заменяет только свой режим; обычный dedup их не удаляет
            conn.execute("DELETE FROM dup_groups WHERE status = 'similar' AND key NOT LIKE 'folder:%'")
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
    result = run.finish({"walk_errors": errors[:200], "groups": results})
    # После finish: relink уже перенёс пути дублей на оставленные книги; пересборка приводит таблицу к итоговому виду.
    if not dry_run and Path(cfg["db_path"]).is_file():
        with closing(connect(cfg["db_path"])) as conn, conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_COLLECTIONS:
                report_collections(rebuild_collections(conn), out)
    return result


# ----------------------------------------------------------------------
# dedup-folders: дубли внутри папки автора (папка верхнего уровня «Фамилия Имя Отчество»), каждая папка независимо.
# Правила: дубль — книга, почти полностью (≥ 97 % шинглов) уже есть у оставленной, с ≥ 200 словами; похожая — 85–97 %.


def free_name(directory: Path, name: str, leaving: set[Path], taken: set[Path]) -> Path:
    """Свободное имя рядом: name, затем name_1… . Файл, уходящий в карантин (leaving), занятым не считается: в dry-run
    дубль ещё на месте, и план совпадает с боевым запуском. taken — имена, уже запланированные в этом прогоне."""
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, dot + ext) if stem else (name, "")
    candidate, number = directory / name, 1
    while (candidate.exists() and candidate not in leaving) or candidate in taken:
        candidate = directory / f"{base}_{number}{suffix}"
        number += 1
    taken.add(candidate)
    return candidate


def group_member(item: dict[str, Any], role: str, keeper: str = "", quarantine: str = "",
                 coverage: float | None = None) -> dict[str, Any]:
    """Участник группы для dup_members; путь — итоговый (после переименования оставленного файла)."""
    return {"path": item["final"], "fmt": item["fmt"], "size": item["size"], "words": item["words"],
            "coverage": coverage, "role": role, "keeper": keeper, "quarantine": quarantine}


def text_item(cfg: dict[str, Any], root: Path, cache: TextCache | None, known: dict[str, sqlite3.Row], run: NightRun,
              item: tuple[str, int, float]) -> dict[str, Any] | None:
    """Текст и шинглы одного файла папки (вызывается параллельно). Многофайловый zip — None: это не книга."""
    rel, size, mtime = item
    fmt = split_name(rel)[1]
    words, shingle_set, reason = 0, set(), ""
    try:
        fmt = item_meta(root, known, item)["fmt"]
        if fmt == "zip":
            return None
        text = cache.get(rel, size, mtime, fmt) if cache is not None else None
        if text is None:
            with tempfile.TemporaryDirectory(prefix="books_library-dedup-folders-") as tmp_name:
                text = book_text(cfg, root / rel, fmt, Path(tmp_name))
            if cache is not None:
                cache.put(rel, size, mtime, fmt, text)
                run.count("text_cache_miss")
        else:
            run.count("text_cache_hit")
        words, hashes, _sampled = shingles(text)
        shingle_set = {h for h in hashes if h % FOLDER_SAMPLE == 0}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError, subprocess.TimeoutExpired) as error:
        reason = f"{type(error).__name__}: {error}"
    except Exception as error:  # noqa: BLE001 — правило: режим не останавливается из-за одного файла
        run.record("failed", rel, reason=f"{type(error).__name__}: {error}")
    run.tick()
    return {"path": rel, "fmt": fmt, "size": size, "words": words, "s": shingle_set, "reason": reason,
            "role": "kept", "coverage": None, "keeper": "", "quarantine": ""}


def mark_roles(texts: list[dict[str, Any]]) -> None:
    """Жадный проход по значимости (формат → кириллическое имя → больше слов → путь). Книга — дубль, если ≥ 97 % её
    шинглов уже есть у оставленных и похожих и в ней ≥ 200 слов; похожая — от 85 % до 97 %; иначе оставлена. Без текста
    оставлена и в сравнении не участвует. У дубля и похожего keeper — оставленный с наибольшей долей общих шинглов."""
    texts.sort(key=lambda t: (FORMAT_RANK.get(t["fmt"], 5), not CYRILLIC.search(split_name(t["path"])[0]),
                              -t["words"], t["path"]))
    union: set[int] = set()
    kept: list[dict[str, Any]] = []
    for item in texts:
        s = item["s"]
        if not s:
            kept.append(item)
            continue
        cov = len(s & union) / len(s)
        item["coverage"] = round(cov, 4)
        if cov >= DEDUP_COVERAGE and item["words"] >= DEDUP_MIN_WORDS:
            role = "duplicate"
        elif DEDUP_SIMILAR <= cov < DEDUP_COVERAGE:
            role = "similar"
        else:
            role = "kept"
        # пересечения считаются только для дубля и похожего: для оставленных это квадратичная работа по папке
        scored = [(len(s & k["s"]), k) for k in kept if k["s"]] if role != "kept" else []
        if not scored:
            kept.append(item)
            union |= s
            continue
        item.update(role=role, keeper=max(scored, key=lambda pair: pair[0])[1]["path"])
        if role == "similar":
            union |= s


def keeper_renames(texts: list[dict[str, Any]]) -> dict[str, str]:
    """Оставленный файл с транслитным именем получает кириллическое имя своего дубля (у которого больше всего слов):
    stem дубля + расширение оставленного, вместе с .zip однокнижного архива. Возвращает: старый путь → новое имя."""
    renames: dict[str, str] = {}
    for keeper in texts:
        if keeper["role"] != "kept" or CYRILLIC.search(split_name(keeper["path"])[0]):
            continue
        copies = [t for t in texts if t["role"] == "duplicate" and t["keeper"] == keeper["path"]
                  and CYRILLIC.search(split_name(t["path"])[0])]
        if not copies:
            continue
        best = min(copies, key=lambda t: (-t["words"], t["path"]))
        name = PurePosixPath(keeper["path"]).name
        stem = split_name(keeper["path"])[0]
        new_name = split_name(best["path"])[0] + name[len(stem):]
        if name_ok(new_name):
            renames[keeper["path"]] = new_name
    return renames


def dedup_folder(run: NightRun, folder_rel: str, texts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Решения по одной папке автора: дубли — в карантин, оставленный с транслитным именем — переименовывается.
    Возвращает группы для dup_groups и пары переноса путей (старый → новый) для relink."""
    mark_roles(texts)
    for item in texts:
        if not item["s"]:
            run.record("no-text", item["path"], reason=item["reason"] or "текста нет")
        elif item["role"] == "similar":
            run.record("similar", item["path"], keeper=item["keeper"], coverage=item["coverage"])
    for item in texts:
        if item["role"] != "duplicate":
            continue
        try:
            dest = run.quarantine(run.root / item["path"], "duplicate", keeper=item["keeper"], coverage=item["coverage"])
        except OSError as error:
            run.record("failed", item["path"], reason=f"карантин: {error}")
            item["role"] = "kept"  # файл остался на месте: его связи и группа не меняются
            continue
        item["quarantine"] = str(dest)
    leaving = {run.root / t["path"] for t in texts if t["role"] == "duplicate"}
    taken: set[Path] = set()
    moves: dict[str, str] = {}
    for old, new_name in keeper_renames(texts).items():
        full = run.root / old
        target = free_name(full.parent, new_name, leaving, taken)
        try:
            if not run.dry_run:
                os.rename(full, target)
        except OSError as error:
            run.record("failed", old, reason=f"переименование: {error}")
            continue
        run.record("renamed", old, to=str(target))
        moves[old] = run.rel(target)
    for item in texts:
        item["final"] = moves.get(item["path"], item["path"])
        item["keeper_final"] = moves.get(item["keeper"], item["keeper"])
    pairs = [(item["path"], item["keeper_final"]) for item in texts if item["role"] == "duplicate"]
    pairs += list(moves.items())
    groups: list[dict[str, Any]] = []
    if any(t["role"] == "duplicate" for t in texts):
        members = []
        for t in texts:
            if t["role"] == "duplicate":
                members.append(group_member(t, "duplicate", t["keeper_final"], t["quarantine"], t["coverage"]))
            elif t["role"] == "kept":
                members.append(group_member(t, "kept"))
        groups.append({"key": f"folder:{folder_rel}", "status": "auto", "members": members})
    by_path = {t["path"]: t for t in texts if t["role"] == "kept"}
    for t in texts:
        if t["role"] == "similar":
            groups.append({"key": f"folder:{t['path']}", "status": "similar",
                           "members": [group_member(t, "kept", coverage=t["coverage"]),
                                       group_member(by_path[t["keeper"]], "kept")]})
    return groups, pairs


def dedup_folders(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
                  out: Callable[[str], None] = print) -> dict[str, Any]:
    """Дубли внутри каждой папки автора: папки верхнего уровня книг независимо, «_…» и «.…» пропускаются; с --path —
    только указанная папка. Правила — в mark_roles и dedup_folder. Дубли уходят в карантин duplicate/, связи переносятся
    на оставленную книгу. Группы — в dup_groups: auto по папке с дублями, similar по каждой похожей книге. Папка больше
    DEDUP_FOLDER_MAX книг пропускается. Группы обычного dedup не трогаются."""
    run = NightRun(cfg, "dedup-folders", dry_run, path, out)
    root = run.root
    if not run.target.resolve().is_relative_to(root.resolve()):
        raise ConfigError("dedup-folders: --path должен быть внутри каталога книг")
    if run.target.resolve() == root.resolve():
        with os.scandir(root) as entries:
            names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False)
                           and not e.name.startswith(("_", ".")) and e.name not in SKIP_DIRS)
        folders = [root / name for name in names]
    else:
        if run.target.name.startswith(("_", ".")):
            raise ConfigError("dedup-folders: --path — папка автора, а не раздел «_…» или служебная папка")
        folders = [run.target]
    known = known_books(cfg)
    cache = TextCache.open(cfg)  # один раз на прогон; кэш восстановим и работает в dry-run, как у dedup
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    folders_done = files_total = 0
    try:
        with ThreadPoolExecutor(max_workers=workers or cfg["workers"]) as pool:
            for folder in folders:
                folder_rel = run.rel(folder)
                books = [(f"{folder_rel}/{rel}", size, mtime) for rel, size, mtime in iter_book_files(folder, errors)]
                if not books:
                    continue
                if len(books) > DEDUP_FOLDER_MAX:
                    run.record("folder-too-large", folder_rel, files=len(books))
                    continue
                texts = [t for t in pool.map(lambda item: text_item(cfg, root, cache, known, run, item), books)
                         if t is not None]
                folders_done += 1
                files_total += len(books)
                say(f"dedup-folders: {folder_rel}: книг {len(texts)}", "progress")
                groups, pairs = dedup_folder(run, folder_rel, texts)
                if pairs and not dry_run:
                    run.stats["relinked"] = run.stats.get("relinked", 0) + relink(cfg["db_path"], pairs)
                results.extend(groups)
    finally:
        if cache is not None:
            cache.prune(cfg["text_cache_mb"])
            cache.close()
    if not dry_run:
        migrate_database(cfg["db_path"], out=out)
        with closing(connect(cfg["db_path"])) as conn, conn:
            resolved = {r[0] for r in conn.execute("SELECT key FROM dup_groups WHERE status='resolved'")}
            # заменяются только similar-группы папок; auto-группы папок и группы обычного dedup — история, не трогаются
            conn.execute("DELETE FROM dup_groups WHERE status = 'similar' AND key LIKE 'folder:%'")
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
    result = run.finish({"walk_errors": errors[:200], "groups": results})
    if not dry_run and Path(cfg["db_path"]).is_file():
        with closing(connect(cfg["db_path"])) as conn, conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_COLLECTIONS:
                report_collections(rebuild_collections(conn), out)
    s = run.stats
    out(f"dedup-folders: папок {folders_done}, файлов {files_total}, дублей {s.get('duplicate', 0)}, "
        f"похожих {s.get('similar', 0)}, без текста {s.get('no-text', 0)}, слишком больших папок {s.get('folder-too-large', 0)}, "
        f"переименовано {s.get('renamed', 0)}; кэш текста: попаданий {s.get('text_cache_hit', 0)}, "
        f"промахов {s.get('text_cache_miss', 0)}")
    out("подсказка: запустите scan — каталог книг обновит пути перенесённых и переименованных файлов")
    return result


# ----------------------------------------------------------------------
# zip-books: каждая книга (кроме epub и zip) → name.ext.zip; однокнижные zip — к виду name.ext.zip
# ----------------------------------------------------------------------

def zip_books(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
              out: Callable[[str], None] = print) -> dict[str, Any]:
    """Упаковка: уже сжатые форматы — ZIP_STORED, прочие — deflate 6; сначала пакуются прочие, затем сжатые. Проверка: одна запись, размер и CRC как у
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

    def stored(item: tuple[Path, int]) -> bool:
        return item[0].name.rpartition(".")[2].lower() in STORED_EXTENSIONS

    errors: list[str] = []
    for second in (False, True):  # сначала текстовые форматы, затем уже сжатые (pdf, djvu…)
        parallel((i for i in walk_files(run.target, suffixes, errors) if stored(i) == second), one, workers or cfg["workers"], run)
    return run.finish({"walk_errors": errors[:200]})


# ----------------------------------------------------------------------
# unzip-books: однокнижный name.ext.zip → name.ext (обратный zip-books; решение 17.11.2026: книги хранятся распакованными)
# ----------------------------------------------------------------------

UNZIP_CHUNK = 4 * 1024 * 1024  # блок потоковой распаковки и хеширования
UNZIP_RATIO_MIN_SIZE = 1024 * 1024  # степень сжатия проверяется для записей больше 1 МБ (как в read_member)
UNZIP_BAD_NAME = re.compile(r"[/\\\x00]")  # разделители путей и NUL в имени распакованного файла


def unzip_name(archive: Path, info: zipfile.ZipInfo) -> str:
    """Имя книги: name.ext.zip → name.ext, если ext совпадает с форматом записи; иначе — basename имени записи."""
    stem = archive.name[:-len(".zip")]
    inner_stem, dot, inner_ext = stem.rpartition(".")
    if dot and inner_stem and inner_ext.lower() == member_ext(info):
        return stem
    return PurePosixPath(info.filename.replace("\\", "/")).name


def unzip_name_ok(name: str) -> bool:
    """Имя без путей (косая и обратная черта, NUL), не «.» и не «..», допустимое для NTFS/Syncthing. «..» внутри имени
    («Смолски С..djvu») — не обход пути: имя берётся basename'ом записи. Иначе архив уходит в rejected-name."""
    return name not in ("", ".", "..") and not UNZIP_BAD_NAME.search(name) and name_ok(name)


def same_content(archive: Path, info: zipfile.ZipInfo, dest: Path) -> bool:
    """Двойник: тот же размер и SHA-256 у распакованного потока записи и у существующего файла."""
    if not dest.is_file() or dest.stat().st_size != info.file_size:
        return False
    member_hash, file_hash = hashlib.sha256(), hashlib.sha256()
    with zipfile.ZipFile(archive) as zf, zf.open(info) as source:
        while chunk := source.read(UNZIP_CHUNK):  # zipfile проверяет CRC записи по её концу
            member_hash.update(chunk)
    with dest.open("rb") as handle:
        while chunk := handle.read(UNZIP_CHUNK):
            file_hash.update(chunk)
    return member_hash.digest() == file_hash.digest()


def unzip_extract(run: NightRun, archive: Path, info: zipfile.ZipInfo, name: str) -> Path:
    """Потоковая распаковка записи во временный файл рядом с архивом: размер сверяется, CRC — при чтении zipfile.
    Свободное имя (name, затем name_1…) выбирается под блокировкой прогона. При любой ошибке временный файл удаляется."""
    st = archive.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=".unzip-books.", suffix=".tmp", dir=archive.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(archive) as zf, zf.open(info) as source, tmp.open("wb") as sink:
            shutil.copyfileobj(source, sink, UNZIP_CHUNK)
        if tmp.stat().st_size != info.file_size:
            raise ValueError(f"распаковано {tmp.stat().st_size} байт вместо {info.file_size}")
        os.chmod(tmp, st.st_mode & 0o666)
        os.utime(tmp, (st.st_atime, st.st_mtime))  # дата книги — дата архива (zip-books записывал её в архив)
        with run.lock:
            target = unique_path(archive.parent, name, set())
            os.replace(tmp, target)
        return target
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def unzip_books(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
                out: Callable[[str], None] = print) -> dict[str, Any]:
    """Распаковка однокнижных zip: name.ext.zip → name.ext рядом. Архив уходит в карантин unzipped/ (не удаляется).
    Двойник (тот же размер и SHA-256) — только архив в карантин. Многофайловые zip не трогаются; битые и с
    подозрительной степенью сжатия — в rejected-zip/, недопустимые имена — в rejected-name/."""
    run = NightRun(cfg, "unzip-books", dry_run, path, out)

    def flush_relinks() -> None:
        """Перенос отметок пачками по RELINK_BATCH: при обрыве прогона расхождение с базой не больше пачки."""
        with run.lock:
            if len(run.relinks) < RELINK_BATCH:
                return
            pairs, run.relinks = run.relinks, []
        moved = relink(cfg["db_path"], pairs)
        with run.lock:
            run.stats["relinked"] = run.stats.get("relinked", 0) + moved

    def one(item: tuple[Path, int]) -> None:
        archive, _size = item
        try:
            with zipfile.ZipFile(archive) as zf:
                info = single_member(zf)
        except (zipfile.BadZipFile, EOFError, OSError, ValueError) as error:
            run.quarantine(archive, "rejected-zip", reason=f"{type(error).__name__}: {error}")
            return
        if info is None:
            run.count("multi")  # многофайловый или вложенный архив — не трогаем
            return
        if info.file_size > UNZIP_RATIO_MIN_SIZE and info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
            run.quarantine(archive, "rejected-zip", reason="подозрительная степень сжатия (zip-бомба?)")
            return
        name = unzip_name(archive, info)
        if not unzip_name_ok(name):
            run.quarantine(archive, "rejected-name", reason=f"имя {name!r} недопустимо")
            return
        dest = archive.parent / name
        if dest.exists() and same_content(archive, info, dest):
            run.record("twin", archive, to=str(dest))
            target = dest
        else:
            target = unique_path(archive.parent, name, set()) if dry_run else unzip_extract(run, archive, info, name)
            if target.name != name:
                run.count("conflict")  # имя занято другим содержимым: книга — с суффиксом _N
            run.record("extracted", archive, to=str(target))
        run.quarantine(archive, "unzipped", book=str(target))
        if not dry_run:
            run.moved(archive, target)
            flush_relinks()

    errors: list[str] = []
    parallel(walk_files(run.target, (".zip",), errors), one, workers or cfg["unzip_workers"], run)
    stats = run.stats
    line = (f"unzip-books: {'будет ' if dry_run else ''}распаковано {stats.get('extracted', 0)}, "
            f"двойников {stats.get('twin', 0)}, конфликтов имён {stats.get('conflict', 0)}, "
            f"многофайловых не тронуто {stats.get('multi', 0)}, в rejected-zip {stats.get('rejected-zip', 0)}, "
            f"ошибок {stats.get('failed', 0)}")
    out(line)
    out("дальше: запустите scan — каталог книг обновит пути; архивы остаются в карантине unzipped/")
    return run.finish({"summary": line, "walk_errors": errors[:200]})


# ----------------------------------------------------------------------
# unpack-collections и sort-underscore: книги из раздела сборников / служебных папок «_» — в папки авторов (только CLI; в веб и API не выведены)
# ----------------------------------------------------------------------

CLI_COMMANDS = ("unpack-collections", "merge-authors", "sort-underscore")
COLLECTIONS_SECTION = "Сборники"  # section_name папки «_…» каталога книг: раздел сборников
RELINK_BATCH = 1000  # пар переноса на один relink: при сбое связи и файлы расходятся не больше чем на пачку
FOLDER_NAME_MAX = 120
FOLDER_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
AUTHOR_STOP = frozenset({"unknown", "неизвестен", "неизвестный", "неизвестный автор", "автор неизвестен",
                         "без автора", "автор неустановлен", "неустановленный автор",
                         "esimych", "anonymous", "аноним"})  # заглушки вместо автора (уже нормализованные)
# слово в имени — это составитель, коллектив или жанр/название, а не автор: книга остаётся в сборнике
AUTHOR_COLLECTIVE = frozenset({"коллектив", "составитель", "составители", "авторов", "сборник", "антология", "редактор",
                               "сборники", "эпос", "сказания", "фольклор", "литература", "литературы", "тексты",
                               "саги", "газета", "журнал", "издательство", "издательства"})
# частицы фамилий, могут быть со строчной буквы: «де Сервантес», «фон Миллер», «van Gogh»
AUTHOR_PARTICLES = frozenset({"де", "фон", "ван", "да", "ди", "дю", "дель", "ла", "ле", "на", "эль", "аль", "ибн", "бен",
                              "аб", "ап", "ал", "ад", "оглы",
                              "van", "von", "de", "du", "da", "di", "del", "la", "le", "el", "al", "bin", "ibn"})
PERSON_APOSTROPHES = ("'", "’")
PERSON_PREFIX_LETTERS = frozenset({"д", "л", "о", "o", "d", "l"})  # «д'Артаньян», «О'Нил»


def author_norm(text: str) -> str:
    """Сравнение имён: casefold, ё→е, только буквы, цифры, пробелы и дефис; пустые токены отброшены."""
    kept = "".join(ch for ch in text.casefold().replace("ё", "е") if ch.isalnum() or ch.isspace() or ch == "-")
    return " ".join(token for token in kept.split() if any(ch.isalnum() for ch in token))


def author_key(name: str) -> frozenset[str] | None:
    """Ключ автора или папки — набор слов. None — не имя автора: пусто, цифры, больше 5 слов, одно слово
    короче 2 символов или заглушка из AUTHOR_STOP."""
    norm = author_norm(name)
    words = norm.split()
    if not words or any(ch.isdigit() for ch in norm) or len(words) > 5 or norm in AUTHOR_STOP \
            or AUTHOR_COLLECTIVE.intersection(words):
        return None
    if len(words) == 1 and len(words[0]) < 2:
        return None
    return frozenset(words)


def person_name(name: str) -> bool:
    """Похоже на имя человека, а не на название или жанр: каждое слово, кроме частиц AUTHOR_PARTICLES и приставок
    «д'»/«л'»/«o'», начинается с заглавной буквы; слово целиком заглавными — годно при любой длине; в смешанном
    регистре заглавных после первой буквы не больше половины (КОИ8-кракозябры вида «вБИТПЫЙО» отвергаются).
    Проверяется часть слова до первого дефиса. Слова без букв (инициалы с точкой и т. п.) пропускаются.
    Инициалы («А.», «Р») — годны. Нет ни одного проверенного слова — False."""
    checked = False
    for word in name.split():
        part = word.split("-", 1)[0]
        if len(part) > 2 and part[1] in PERSON_APOSTROPHES and part[0].casefold() in PERSON_PREFIX_LETTERS:
            part = part[2:]
        if part.casefold() in AUTHOR_PARTICLES:
            continue
        letters = [ch for ch in part if ch.isalpha()]
        if not letters:
            continue
        if not letters[0].isupper():
            return False
        if len(letters) > 1 and len(set(ch.casefold() for ch in letters)) == 1:
            return False  # «ЧЧЧЧ»: одна буква повтором — заглушка, не имя
        checked = True
        if part.isupper():
            continue
        rest = letters[1:]
        if sum(ch.isupper() for ch in rest) * 2 > len(rest):
            return False
    return checked


def folder_name(name: str) -> str:
    """Имя папки для Windows и Syncthing: без запрещённых и управляющих символов, без пробелов и точек по краям,
    не длиннее FOLDER_NAME_MAX. Пустой результат — имени нет."""
    return FOLDER_BAD_CHARS.sub("", name).strip(" .")[:FOLDER_NAME_MAX].strip(" .")


def collections_section(root: Path) -> Path:
    """Папка раздела сборников в каталоге книг: section_name папки «_…» равен COLLECTIONS_SECTION."""
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name.startswith("_") and section_name(entry.name) == COLLECTIONS_SECTION:
            return entry
    raise ConfigError(f"раздел сборников «{COLLECTIONS_SECTION}» не найден в {root}")


def author_index(root: Path) -> dict[frozenset[str], str]:
    """Папки авторов верхнего уровня: {ключ: имя}. Папки «_…» и «.…» не входят; при совпадении ключей — первая по сортировке."""
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    index: dict[frozenset[str], str] = {}
    for name in names:
        key = None if name.startswith(("_", ".")) else author_key(name)
        if key is not None:
            index.setdefault(key, name)
    return index


def pick_folder(author: str, key: frozenset[str], parent: str, index: dict[frozenset[str], str]) -> tuple[str, str]:
    """Папка автора и способ выбора: exact — точный ключ; parent — папка, где лежит файл, названа как автор;
    subset — ровно одна папка с пересечением ≥ 2 слов и ключом ⊆ или ⊇; иначе new — «Фамилия Имя Отчество»
    (последнее слово автора первым). Пустое имя у new — имени нет.
    not-person — метаданные не похожи на имя человека (название, жанр, кракозябры): имени нет, папку не создавать."""
    if key in index:
        return index[key], "exact"
    parent_key = author_key(parent) if parent else None
    if parent_key is not None and folder_name(parent) and (
            parent_key == key or (len(parent_key & key) >= 2 and (parent_key <= key or parent_key >= key))):
        return folder_name(parent), "parent"  # в т. ч. «Фамилия Имя» у автора с отчеством
    close = [name for known, name in index.items() if len(known & key) >= 2 and (known <= key or known >= key)]
    if len(close) == 1:
        return close[0], "subset"
    if not person_name(author):
        return "", "not-person"
    words = author.split()
    return folder_name(" ".join([words[-1], *words[:-1]])), "new"


def remove_empty_dirs(section: Path, gone: set[str], dry_run: bool) -> int:
    """Пустые папки раздела снизу вверх: только os.rmdir (без rmtree); сам раздел не трогается. gone — файлы,
    которые перенесёт прогон: в dry-run они ещё на диске, но папка с ними будет пустой."""
    removable: set[str] = set()
    for current, dirs, files in os.walk(section, topdown=False):
        if current == str(section):
            continue
        if any(os.path.join(current, name) not in gone for name in files) or \
                any(os.path.join(current, name) not in removable for name in dirs):
            continue
        if not dry_run:
            try:
                os.rmdir(current)
            except OSError:
                continue
        removable.add(current)
    return len(removable)


def journal_row(run: NightRun, row: dict[str, Any]) -> None:
    """Строка журнала прогона: по полям from/to перенос откатывается; пишется по ходу, с flush."""
    with run.lock:
        run.journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        run.journal.flush()


SORT_SOURCES = ("meta", "folder", "filename", "text")  # sort-underscore: источники автора по убыванию надёжности
EXAMPLES_PER_SOURCE = 30  # примеров каждого источника в итоге dry-run и в отчёте
TEXT_HEAD_CHARS = 2048  # начало текста для поиска автора без метаданных (около 2 КБ)
FILENAME_SPLIT = re.compile(r"^(.+?)(?: - |_-_|\.-\.)(.+)$")  # «Автор - Название», «Автор_-_Название», «Автор.-.Название»
TEXT_NAME_LINE = re.compile(r"^\s*([А-ЯЁ][а-яё]+(?:[ \t]+[А-ЯЁ][а-яё]+){1,2})\.[ \t]+\S")  # «Имя Фамилия. Название»
TEXT_CAPS_LINE = re.compile(r"^\s*([А-ЯЁ]{2,}(?:[ \t]+[А-ЯЁ]{2,}){1,2})\s*$")  # «ИМЯ ФАМИЛИЯ» + название следующей строкой
# «Имя Фамилия» или «Имя Фамилия.» отдельной строкой в первых TEXT_ALONE_LINES строках + название следующей строкой
TEXT_NAME_ALONE = re.compile(r"^\s*([А-ЯЁ][а-яё]+(?:[ \t]+[А-ЯЁ][а-яё]+){1,2})\.?\s*$")
TEXT_ALONE_LINES = 5


def is_initials(name: str) -> bool:
    """«Фамилия И.О.» или «Фамилия И»: после первого слова — только однобуквенные инициалы."""
    tokens = [token for token in re.split(r"[\s.]+", name) if token]
    return len(tokens) >= 2 and all(len(token) == 1 for token in tokens[1:])


def existing_folder(key: frozenset[str], index: dict[frozenset[str], str]) -> str:
    """Папка автора, которая уже лежит в корне: точный ключ или единственная с пересечением ≥ 2 слов и ключом ⊆/⊇.
    Иначе ''. Новые папки не создаются."""
    if key in index:
        return index[key]
    close = [name for known, name in index.items() if len(known & key) >= 2 and (known <= key or known >= key)]
    return close[0] if len(close) == 1 else ""


def initials_folder(name: str, index: dict[frozenset[str], str]) -> str:
    """«Фамилия И.О.» → единственная существующая папка с той же фамилией и начальными буквами имени и отчества."""
    tokens = [token for token in re.split(r"[\s.]+", name) if token]
    surname = author_norm(tokens[0])
    initials = [author_norm(token) for token in tokens[1:]]
    found: set[str] = set()
    for folder in index.values():
        words = author_words(folder)
        if words[:1] == [surname] and len(words) > len(initials) and \
                all(word.startswith(initial) for word, initial in zip(words[1:], initials)):
            found.add(folder)
    return found.pop() if len(found) == 1 else ""


def folder_for_name(name: str, index: dict[frozenset[str], str]) -> str:
    """Существующая папка автора для имени из пути или файла: инициалы — по буквам, полное имя — по ключу."""
    if is_initials(name):
        return initials_folder(name, index)
    key = author_key(name)
    return existing_folder(key, index) if key is not None else ""


def filename_author(stem: str) -> str:
    """Автор из имени файла «Фамилия И.О. - Название», «Фамилия_-_Название», «Фамилия.-.Название»; иначе ''."""
    match = FILENAME_SPLIT.match(stem)
    if match is None:
        return ""
    left = match.group(1).replace("_", " ").strip()
    if not match.group(2).strip() or re.search(r"\d", left) or len(left.split()) > 4:
        return ""
    return left


def text_author(head: str, index: dict[frozenset[str], str]) -> tuple[str, str]:
    """Автор из начала текста: «Имя Фамилия. Название», «ИМЯ ФАМИЛИЯ» или «Имя Фамилия[.]» в первых TEXT_ALONE_LINES
    строках (название — следующей строкой).
    Возвращает (папка, имя) первого кандидата, совпавшего с существующей папкой; иначе ('', '')."""
    lines = [line for line in head.splitlines() if line.strip()][:40]
    for position, line in enumerate(lines):
        candidate = TEXT_NAME_LINE.match(line)
        if candidate is None and position + 1 < len(lines):
            candidate = TEXT_CAPS_LINE.match(line) or (
                TEXT_NAME_ALONE.match(line) if position < TEXT_ALONE_LINES else None)
        if candidate is None:
            continue
        folder = folder_for_name(candidate.group(1), index)
        if folder:
            return folder, candidate.group(1)
    return "", ""


def text_head(cfg: dict[str, Any], full: Path, fmt: str) -> str:
    """Начало текста книги (TEXT_HEAD_CHARS символов); однокнижный zip читается через book_text."""
    with tempfile.TemporaryDirectory(prefix="books_library-sort-underscore-") as tmp_name:
        return book_text(cfg, full, fmt, Path(tmp_name))[:TEXT_HEAD_CHARS]


def unpack_collections(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                       out: Callable[[str], None] = print, *, kind: str = "unpack-collections",
                       roots: list[Path] | None = None, sources: tuple[str, ...] = ("meta",),
                       limit: int | None = None) -> dict[str, Any]:
    """Книги раздела «Сборники» — в папки авторов по первому автору (см. pick_folder). Файлы, которых нет в books,
    не трогаются; книги без автора остаются на месте. Перенос — os.rename в пределах тома (без копирования); связи
    переносятся relink пачками по RELINK_BATCH во время прогона. Боевой запуск сначала пересобирает сборники,
    чтобы folder-строки существовали до переноса. Пустые папки удаляются снизу вверх.
    roots и sources — режим sort-underscore: корни (служебные папки «_…») и источники автора из SORT_SOURCES.
    Источники, кроме meta, принимаются только при совпадении с уже существующей папкой автора (новые не создаются)."""
    root = Path(cfg["books_dir"])
    if roots is None:
        section = collections_section(root)
        scope = target_dir(cfg, path) if path else section
        if not scope.resolve().is_relative_to(section.resolve()):
            raise ConfigError("unpack-collections: --path должен быть внутри раздела сборников")
        scopes, trees = [scope], [section]
    else:
        scopes, trees = roots, roots
    db_path = cfg["db_path"]
    if not Path(db_path).is_file():
        raise ConfigError(f"база не создана: {db_path} (сначала scan)")
    with closing(connect(db_path, readonly=True)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_COLLECTIONS:
            raise ConfigError(f"{kind}: нужна схема v{SCHEMA_COLLECTIONS} (сначала scan)")
    run = NightRun(cfg, kind, dry_run, path, out)
    if not dry_run:
        with closing(connect(db_path)) as conn, conn:
            report_collections(rebuild_collections(conn), out)
    with closing(connect(db_path, readonly=True)) as conn:
        books = {row["path"]: row for row in conn.execute("SELECT path, size, authors, fmt FROM books")}
    prefixes = [scope.relative_to(root).as_posix() + "/" for scope in scopes]
    candidates = sorted(rel for rel in books if any(rel.startswith(prefix) for prefix in prefixes))
    if limit is not None:
        candidates = candidates[:limit]
    errors: list[str] = []
    for scope, prefix in zip(scopes, prefixes):
        for rel, _size, _mtime in iter_book_files(scope, errors):
            if prefix + rel not in books:
                run.count("not_book")
    pre_dirs = {entry.name for entry in root.iterdir() if entry.is_dir()}
    index = author_index(root)
    created: set[str] = set()
    taken: set[Path] = set()
    planned_gone: set[str] = set()
    pending: list[tuple[str, str]] = []
    breakdown: dict[str, dict[str, int]] = {}  # раздел → источник → число переносов
    examples: dict[str, list[str]] = {source: [] for source in sources}

    def folder_of(author: str) -> str:
        """Папка автора по тем же правилам, что у первого автора книги; новая папка попадает в индекс."""
        key = author_key(author)
        folder = pick_folder(author, key, "", index)[0] if key is not None else ""
        if folder and author_key(folder) is not None:
            index.setdefault(author_key(folder), folder)
        return folder

    def note_moved(rel_src: str, dst_rel: str, label: str, source: str, who: str) -> None:
        """Счётчики по разделу и источнику; примеры — по EXAMPLES_PER_SOURCE на источник."""
        run.count("moved")
        run.count(f"source_{source}")
        counts = breakdown.setdefault(label, {})
        counts[source] = counts.get(source, 0) + 1
        if len(examples[source]) < EXAMPLES_PER_SOURCE:
            examples[source].append(f"{rel_src} → {dst_rel} [{who}]")

    for rel in candidates:
        src = root / rel
        top = root / PurePosixPath(rel).parts[0]
        label = section_name(top.name)
        authors = [name.strip() for name in books[rel]["authors"].split("; ") if name.strip()]
        author = books[rel]["authors"].split("; ")[0].strip()
        key = author_key(author) if author else None
        parent = "" if src.parent == top or src.parent.name.startswith(("_", ".")) else src.parent.name
        folder, match, source, found_by, name, failure = "", "", "", "", "", ""
        if "meta" in sources and key is not None:
            if is_initials(author):  # «Фамилия И.О.»: только существующая папка, новую «И.О. Фамилия» не создавать
                folder, match = initials_folder(author, index), "initials"
            else:
                folder, match = pick_folder(author, key, parent, index)
                # sort-underscore: новая папка — только по встроенным метаданным fb2/epub; автор pdf/djvu/txt
                # из имени файла («Элитариум - …») принимается лишь для уже существующей папки
                if kind == "sort-underscore" and match == "new" and books[rel]["fmt"] not in ("fb2", "epub"):
                    folder, match = "", ""
            if folder:
                source, found_by = "meta", author
        if not folder and "folder" in sources and parent:
            folder = folder_for_name(parent, index)
            if folder:
                source, found_by, match = "folder", parent, "existing"
        if not folder and "filename" in sources:
            name = filename_author(split_name(rel)[0])
            folder = folder_for_name(name, index) if name else ""
            if folder:
                source, found_by, match = "filename", name, "existing"
        if not folder and "text" in sources and books[rel]["fmt"] in ("txt", "fb2"):
            try:
                folder, name = text_author(text_head(cfg, src, books[rel]["fmt"]), index)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError, subprocess.TimeoutExpired) as error:
                failure = f"{type(error).__name__}: {error}"
            if folder:
                source, found_by, match = "text", name, "existing"
        if not folder:
            run.count("no_author")
            journal_row(run, {"from": rel, "path": rel, "to": "", "author": author, "folder": "", "match": "",
                              "source": "", "status": "no-author", "error": failure})
            continue
        co_authors = authors[1:] if source == "meta" else []  # книга нескольких авторов: у каждого следующего — копия
        fkey = author_key(folder)
        if fkey is not None:
            index.setdefault(fkey, folder)
        if folder in pre_dirs:
            run.count("into_existing")
        elif folder not in created:
            created.add(folder)
            run.count("new_folders")
        directory = root / folder
        dest = unique_path(directory, src.name, taken)
        if dest.name != src.name:
            run.count("conflicts")
        dst_rel = dest.relative_to(root).as_posix()
        base = {"from": rel, "path": rel, "to": dst_rel, "author": found_by or author, "folder": folder,
                "match": match, "source": source}
        if dry_run:
            planned_gone.add(str(src))
            note_moved(rel, dst_rel, label, source, found_by or author)
            journal_row(run, {**base, "status": "dry", "error": ""})
            if co_authors:
                copy_coauthors(run, root, dest, co_authors, folder_of, taken, dry_run=True, source=source)
            continue
        try:
            size = src.stat().st_size
            directory.mkdir(parents=True, exist_ok=True)
            os.rename(src, dest)
        except OSError as error:  # в т. ч. EXDEV: другой том — не копируем, а пишем ошибку
            run.count("errors")
            journal_row(run, {**base, "status": "error", "error": str(error)})
            continue
        pending.append((rel, dst_rel))  # файл уже на новом месте: связи переносятся при любом исходе проверки
        if len(pending) >= RELINK_BATCH:
            relink(db_path, pending)
            pending.clear()
        try:
            intact = dest.is_file() and dest.stat().st_size == size
        except OSError:
            intact = False
        if intact:
            note_moved(rel, dst_rel, label, source, found_by or author)
            journal_row(run, {**base, "status": "moved", "error": ""})
            if co_authors:
                copy_coauthors(run, root, dest, co_authors, folder_of, taken, dry_run=False, source=source)
        else:
            run.count("errors")
            journal_row(run, {**base, "status": "error", "error": "размер после переноса не совпал"})
    if pending:
        relink(db_path, pending)
    removed = 0
    for tree in trees:
        removed += remove_empty_dirs(tree, planned_gone, dry_run)
    run.stats["removed_dirs"] = removed
    s = run.stats
    done = "будет" if dry_run else ""
    line = (f"{kind}: {done + ' ' if done else ''}перенесено {s.get('moved', 0)} "
            f"(в существующие папки {s.get('into_existing', 0)}, новых папок {s.get('new_folders', 0)}), "
            f"нет автора {s.get('no_author', 0)}, не книги {s.get('not_book', 0)}, копий соавторам {s.get('copied', 0)}, "
            f"ошибки {s.get('errors', 0)}, "
            f"конфликтов имён {s.get('conflicts', 0)}, {'будет удалено' if dry_run else 'удалено'} пустых папок "
            f"{s['removed_dirs']}")
    if len(sources) > 1:
        line += ", по источникам: " + ", ".join(f"{source} {s.get('source_' + source, 0)}" for source in sources)
    out(line)
    say(line, kind.replace("-", "_"), detail=json.dumps({k: v for k, v in s.items() if k != "checked"}, ensure_ascii=False))
    if dry_run and len(sources) > 1:
        for label in sorted(breakdown):
            out(f"[dry-run] раздел «{label}»: " + ", ".join(
                f"{source} {count}" for source, count in sorted(breakdown[label].items())))
        for source in sources:
            out(f"[dry-run] примеры «{source}» ({len(examples[source])}):")
            for example in examples[source]:
                out(f"  {example}")
    if not dry_run:
        out("дальше: запустите scan — каталог books обновит пути книг; перенос откатывается по журналу (from/to)")
    return run.finish({"summary": line, "by_section": breakdown, "examples": examples})


def sort_underscore(cfg: dict[str, Any], dry_run: bool = False, limit: int | None = None,
                    out: Callable[[str], None] = print) -> dict[str, Any]:
    """Программа максимум: книги из всех служебных папок «_…» каталога книг — в папки авторов, у которых автор
    определён (SORT_SOURCES); без автора остаются на месте. Журнал reports/sort-underscore-*.jsonl: path, to, source,
    author; копии соавторам — отдельные строки status copy с путём копии. Откат — по журналу (from/to)."""
    root = Path(cfg["books_dir"])
    roots = sorted(entry for entry in root.iterdir() if entry.is_dir(follow_symlinks=False) and entry.name.startswith("_"))
    if not roots:
        raise ConfigError(f"служебных папок «_…» нет в {root}")
    return unpack_collections(cfg, dry_run, None, out, kind="sort-underscore", roots=roots, sources=SORT_SOURCES,
                              limit=limit)


# ----------------------------------------------------------------------
# merge-authors: «Фамилия Имя» + «Фамилия Имя Отчество» → одна папка «Фамилия Имя Отчество» (только CLI)
# ----------------------------------------------------------------------



def author_words(name: str) -> list[str]:
    """Слова имени в нормализованном виде; порядок сохраняется — он важен для сравнения папок."""
    return author_norm(name).split()


def full_candidates(short: str, names: list[str]) -> list[str]:
    """Папки «Фамилия Имя Отчество» для короткой «Фамилия Имя»: первые два слова совпадают по порядку,
    третье — не инициал (длина ≥ 3)."""
    short_words = author_words(short)
    found = []
    for name in names:
        words = author_words(name)
        if len(words) == 3 and words[:2] == short_words and len(words[2]) >= 3:
            found.append(name)
    return sorted(found)


def patronymic_full(author: str, short_words: list[str]) -> str | None:
    """Полное имя «Фамилия Имя Отчество» по автору книги: три слова, среди них оба слова короткой папки. Порядок —
    как в метаданных (фамилия первая) либо «последнее слово первым» (Иван Петрович Сидоров → Сидоров Иван Петрович).
    Результат должен начинаться с слов короткой папки и иметь отчество длиной ≥ 3; иначе None."""
    raw = author.split()
    words = author_words(author)
    if len(raw) != 3 or len(words) != 3 or not set(short_words) <= set(words):
        return None
    for ordered in (raw, [raw[-1], *raw[:-1]]):
        name = folder_name(" ".join(ordered))
        full_words = author_words(name)
        if len(full_words) == 3 and full_words[:2] == short_words and len(full_words[2]) >= 3:
            return name
    return None


def books_under(db_path: str, folder: str) -> list[sqlite3.Row]:
    """Строки books внутри папки верхнего уровня; LIKE с экранированием % и _ (параметризованно)."""
    if not Path(db_path).is_file():
        return []
    pattern = re.sub(r"([\\%_])", r"\\\1", folder) + "/%"
    with closing(connect(db_path, readonly=True)) as conn:
        return conn.execute("SELECT path, authors FROM books WHERE path LIKE ? ESCAPE '\\'", (pattern,)).fetchall()


def flush_relinks(db_path: str, pending: list[tuple[str, str]], force: bool = False) -> None:
    """Связи переносятся пачками по RELINK_BATCH во время прогона, хвост — при force."""
    if pending and (force or len(pending) >= RELINK_BATCH):
        relink(db_path, pending)
        pending.clear()


def merge_folder(run: NightRun, db_path: str, root: Path, short: str, full: str, candidates: list[str],
                 known: set[str], taken: set[Path], pending: list[tuple[str, str]]) -> None:
    """Склейка: файлы и подпапки «короткой» папки — в «полную» (с тем же относительным путём, при конфликте имени —
    unique_path); затем пустые папки короткой удаляются снизу вверх. Остатки не удаляются, а записываются ошибкой."""
    dry_run = run.dry_run
    src_top, dst_top = root / short, root / full
    moved = 0
    for current, dirs, files in os.walk(src_top):
        target = dst_top / Path(current).relative_to(src_top)
        if not dry_run and not target.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                run.count("errors")
                journal_row(run, {"action": "error", "short": short, "full": full, "from": current, "error": str(error)})
                dirs[:] = []
                continue
        for name in files:
            src = Path(current) / name
            dest = unique_path(target, name, taken)
            if dest.name != name:
                run.count("conflicts")
            src_rel = src.relative_to(root).as_posix()
            dst_rel = dest.relative_to(root).as_posix()
            if dry_run:
                run.count("moved")
                moved += 1
                journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "dry"})
                continue
            try:
                os.rename(src, dest)
            except OSError as error:  # в т. ч. EXDEV: копирования нет, файл остаётся на месте
                run.count("errors")
                journal_row(run, {"action": "error", "short": short, "full": full, "from": src_rel, "error": str(error)})
                continue
            run.count("moved")
            moved += 1
            journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel})
            if src_rel in known:
                pending.append((src_rel, dst_rel))
                flush_relinks(db_path, pending)
    status = "dry" if dry_run else "done"
    if not dry_run:
        for current, _dirs, _files in os.walk(src_top, topdown=False):
            try:
                os.rmdir(current)
            except OSError:
                pass
        if src_top.exists():
            run.count("errors")
            status = "partial"
            journal_row(run, {"action": "error", "short": short, "full": full,
                              "error": "папка не пуста после переноса — не удалена"})
        else:
            run.count("merged")
    else:
        run.count("merged")
    journal_row(run, {"action": "merge", "short": short, "full": full, "candidates": candidates, "moved": moved,
                      "status": status, "error": ""})


def merge_authors(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Правило: «Фамилия Имя» и «Фамилия Имя Отчество» в одном каталоге — всё из короткой в полную; если полной нет,
    отчество берётся из метаданных книг короткой папки и папка переименовывается. Неоднозначное (несколько полных
    или несколько отчеств) не трогается. Связи переносятся relink по ходу прогона; журнал — построчно (откат)."""
    root = Path(cfg["books_dir"])
    db_path = cfg["db_path"]
    run = NightRun(cfg, "merge-authors", dry_run, None, out)
    with os.scandir(root) as entries:
        names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith(("_", ".")))
    pending: list[tuple[str, str]] = []
    taken: set[Path] = set()
    for short in [n for n in names if len(author_words(n)) == 2]:
        if not (root / short).is_dir():
            continue
        candidates = full_candidates(short, names)
        if len(candidates) > 1:
            run.count("ambiguous")
            journal_row(run, {"action": "ambiguous", "short": short, "full": "", "candidates": candidates, "moved": 0,
                              "status": "skip", "error": ""})
            continue
        rows = books_under(db_path, short)
        if candidates:
            full, action = candidates[0], "merge"
        else:
            fulls: dict[str, str] = {}
            for row in rows:
                for author in row["authors"].split("; "):
                    name = patronymic_full(author.strip(), author_words(short))
                    if name:
                        fulls.setdefault(author_norm(name), name)
            if not fulls:
                run.count("no_patronymic")  # только счётчик: журнал не раздувается
                continue
            if len(fulls) > 1:
                run.count("ambiguous")
                journal_row(run, {"action": "ambiguous", "short": short, "full": "", "candidates": sorted(fulls.values()),
                                  "moved": 0, "status": "skip", "error": ""})
                continue
            full = next(iter(fulls.values()))
            action = "merge" if (root / full).is_dir() else "rename"
            candidates = [full]
        known = {row["path"] for row in rows}
        if action == "merge":
            merge_folder(run, db_path, root, short, full, candidates, known, taken, pending)
            continue
        if dry_run:
            run.count("renamed")
            journal_row(run, {"action": "rename", "short": short, "full": full, "candidates": candidates, "moved": 0,
                              "status": "dry", "error": ""})
            continue
        try:
            os.rename(root / short, root / full)
        except OSError as error:
            run.count("errors")
            journal_row(run, {"action": "error", "short": short, "full": full, "error": str(error)})
            continue
        run.count("renamed")
        pending.extend((path, full + path[len(short):]) for path, _authors in rows)
        flush_relinks(db_path, pending)
        journal_row(run, {"action": "rename", "short": short, "full": full, "candidates": candidates, "moved": 0,
                          "status": "done", "error": ""})
    flush_relinks(db_path, pending, force=True)
    s = run.stats
    prefix = "будет " if dry_run else ""
    line = (f"merge-authors: {prefix}склеено папок {s.get('merged', 0)}, {prefix}переименовано {s.get('renamed', 0)}, "
            f"{prefix}перенесено файлов {s.get('moved', 0)}, конфликтов имён {s.get('conflicts', 0)}, "
            f"неоднозначных {s.get('ambiguous', 0)}, ошибок {s.get('errors', 0)}")
    out(line)
    say(line, "merge_authors", detail=json.dumps({k: v for k, v in s.items() if k != "checked"}, ensure_ascii=False))
    if not dry_run:
        out("дальше: запустите scan — каталог books обновит пути книг; откат — по журналу (move, rename)")
    return run.finish({"summary": line})


# ----------------------------------------------------------------------
# author-variants: одна папка автора под разным написанием (Ivanov Ivan и Иванов Иван)
# ----------------------------------------------------------------------

VARIANT_SCHEMA = 5  # первая схема с author_variants
VARIANT_TIER_NAMES = {"empty": "пустая папка", "common": "общие книги", "manual": "вручную"}
VARIANT_STATUS_NAMES = {"pending": "ожидает решения", "merged": "склеено", "different": "разные авторы"}
VARIANT_PAGE_SIZE = 50
VARIANT_TRANSLIT = str.maketrans({
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з", "i": "и", "y": "ы", "k": "к", "l": "л",
    "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х", "c": "к",
    "j": "дж", "w": "в", "x": "кс", "q": "к"})


def variant_norm(word: str) -> str:
    """Фонетическая нормализация фамилии для поиска кандидатов: транслит латиницы в кириллицу, ё/е, й/и, двойные буквы,
    гласные-близнецы. Только подсказка: склеивает всегда пользователь или режим по явному правилу."""
    word = word.casefold().replace("ё", "е").replace("э", "е").replace("й", "и").replace("ъ", "").replace("ь", "")
    for latin, cyr in (("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("yu", "ю"),
                       ("ya", "я")):
        word = word.replace(latin, cyr)
    word = re.sub(r"(.)\1", r"\1", word.translate(VARIANT_TRANSLIT))
    return word.replace("ю", "у").replace("я", "а").replace("ы", "и")


def folder_titles(db_path: str, folders: set[str]) -> dict[str, set[str]]:
    """Нормализованные названия книг по папке верхнего уровня (из books). Без базы или без схемы — пусто."""
    titles: dict[str, set[str]] = {}
    if not Path(db_path).is_file():
        return titles
    with closing(connect(db_path, readonly=True)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            return titles
        for row in conn.execute("SELECT path, title FROM books"):
            folder = row["path"].split("/", 1)[0]
            name = author_norm(row["title"] or "")
            if folder in folders and name:
                titles.setdefault(folder, set()).add(name)
    return titles


def folder_book_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Число книг по папке верхнего уровня (для вкладки «Авторы»)."""
    counts: dict[str, int] = {}
    for (path,) in conn.execute("SELECT path FROM books"):
        folder = path.split("/", 1)[0]
        counts[folder] = counts.get(folder, 0) + 1
    return counts


def variant_candidates(root: Path, db_path: str) -> list[dict[str, Any]]:
    """Пары папок верхнего уровня, которые могут быть одним автором: одинаковое второе слово по первым трём буквам и
    близкая фамилия (variant_norm, отношение ≥ 0.85). Пары, различающиеся только отчеством при равных первых двух
    словах, — дело merge-authors и сюда не попадают. tier: empty — в одной из папок нет файлов; common — у пары есть
    общее название книги; manual — иначе."""
    with os.scandir(root) as entries:
        names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith(("_", ".")))
    keys: dict[str, tuple[str, str]] = {}
    for name in names:
        words = name.split()
        if len(words) >= 2:
            keys[name] = (variant_norm(words[0]), variant_norm(words[1])[:3])
    by_initial: dict[str, list[str]] = {}
    for name, (_surname, initial) in keys.items():
        by_initial.setdefault(initial, []).append(name)
    pairs: set[tuple[str, str]] = set()
    for group in by_initial.values():
        for index, a in enumerate(group):
            for b in group[index + 1:]:
                sa, sb = keys[a][0], keys[b][0]
                if not (sa == sb or (len(sa) > 3 and SequenceMatcher(None, sa, sb).ratio() >= 0.85)):
                    continue
                if a.split()[:2] == b.split()[:2]:
                    continue  # отличие только отчеством — merge-authors
                pairs.add((a, b) if a < b else (b, a))
    involved = {name for pair in pairs for name in pair}
    files = {name: sum(len(found) for _d, _s, found in os.walk(root / name)) for name in involved}
    titles = folder_titles(db_path, involved)
    result: list[dict[str, Any]] = []
    for a, b in sorted(pairs):
        common = any(x == y or (min(len(x), len(y)) > 8 and (x in y or y in x))
                     for x in titles.get(a, set()) for y in titles.get(b, set()))
        tier = "empty" if not files[a] or not files[b] else "common" if common else "manual"
        result.append({"a": a, "b": b, "tier": tier, "files": {a: files[a], b: files[b]}})
    return result


def variant_statuses(db_path: str) -> dict[tuple[str, str], str]:
    """Статусы известных пар (pending | merged | different); без базы или схемы v5 — пусто."""
    if not Path(db_path).is_file():
        return {}
    with closing(connect(db_path, readonly=True)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < VARIANT_SCHEMA:
            return {}
        return {(row["a"], row["b"]): row["status"] for row in conn.execute("SELECT a, b, status FROM author_variants")}


def variants_pending(conn: sqlite3.Connection) -> int:
    """Пар, ожидающих решения; до схемы v5 таблицы нет — 0."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < VARIANT_SCHEMA:
        return 0
    return conn.execute("SELECT count(*) FROM author_variants WHERE status='pending'").fetchone()[0]


def store_variants(db_path: str, candidates: list[dict[str, Any]]) -> None:
    """Пары — в author_variants: новые получают pending, у pending обновляется tier, pending, которых больше нет,
    удаляются. merged и different не перезаписываются: решения пользователя постоянны."""
    keys = {(c["a"], c["b"]) for c in candidates}
    now = time.time()
    with closing(connect(db_path)) as conn, conn:
        stale = [(row["a"], row["b"]) for row in conn.execute(
            "SELECT a, b FROM author_variants WHERE status='pending'").fetchall() if (row["a"], row["b"]) not in keys]
        conn.executemany("DELETE FROM author_variants WHERE a=? AND b=? AND status='pending'", stale)
        conn.executemany("INSERT INTO author_variants(a, b, tier, status, target, updated) VALUES (?,?,?,'pending','',?) "
                         "ON CONFLICT(a, b) DO UPDATE SET tier=excluded.tier, updated=excluded.updated "
                         "WHERE author_variants.status='pending'",
                         [(c["a"], c["b"], c["tier"], now) for c in candidates])


def set_variant(db_path: str, a: str, b: str, status: str, target: str = "") -> None:
    """Статус пары и папка, в которую склеено (для merged)."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute("UPDATE author_variants SET status=?, target=?, updated=? WHERE a=? AND b=?",
                     (status, target, time.time(), a, b))


def variant_target(a: str, b: str, files: dict[str, int]) -> tuple[str, str]:
    """(источник, цель) для склейки: цель — папка с полным ФИО (больше слов), затем с большим числом файлов, затем по имени."""
    source, target = sorted((a, b), key=lambda n: (-len(author_words(n)), -files[n], n))
    return source, target


COAUTHOR_SEPARATOR = re.compile(r",\s*|\s+и\s+|\s*&\s*")  # «Громыко Ольга, Уланов Андрей», «… и …», «…&…»


def coauthor_parts(name: str) -> list[str]:
    """Соавторы по имени папки: меньше двух частей — не соавторская запись (пустой список)."""
    parts = [part.strip() for part in COAUTHOR_SEPARATOR.split(name) if part.strip()]
    return parts if len(parts) >= 2 else []


def copy_coauthors(run: NightRun, root: Path, dest: Path, authors: list[str], folder_for: Callable[[str], str],
                   taken: set[Path], dry_run: bool, source: str = "") -> None:
    """Книга нескольких авторов: файл уже у первого автора, у каждого следующего — настоящая копия под тем же именем в его
    папке (folder_for — та же логика выбора папки, что у первого). Копия пишется во временный файл в той же папке, размер
    сверяется, затем os.replace на свободное имя. Сбой — временный файл удаляется, ошибка в журнале: книга остаётся
    только у первого автора."""
    origin = dest.relative_to(root).as_posix()
    for author in authors:
        folder = folder_for(author)
        if not folder:
            run.count("errors")
            journal_row(run, {"from": origin, "path": origin, "to": "", "author": author, "source": source,
                              "status": "no-folder", "error": ""})
            continue
        target = unique_path(root / folder, dest.name, taken)  # имя резервируется сразу
        copy_rel = target.relative_to(root).as_posix()
        base = {"from": origin, "path": copy_rel, "to": copy_rel, "author": author, "folder": folder,
                "source": source}
        if dry_run:
            run.count("copied")
            journal_row(run, {**base, "status": "dry-copy", "error": ""})
            continue
        tmp: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".coauthor.", suffix=".tmp", dir=target.parent)
            os.close(fd)
            tmp = Path(tmp_name)
            shutil.copyfile(dest, tmp)
            shutil.copystat(dest, tmp)
            if tmp.stat().st_size != dest.stat().st_size:
                raise ValueError("копия не совпала по размеру")
            os.replace(tmp, target)
            tmp = None
        except (OSError, ValueError) as error:
            run.count("errors")
            journal_row(run, {**base, "status": "error", "error": f"копия: {error}"})
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            continue
        run.count("copied")
        journal_row(run, {**base, "status": "copy", "error": ""})


def distribute_coauthor(run: NightRun, root: Path, name: str, taken: set[Path], pending: list[tuple[str, str]]) -> bool:
    """Соавторская папка «А, Б»: её файлы — в папку первого автора (существующую по ключу или новую с именем части),
    остальным — настоящие копии (copy_coauthors). Пустую папку удаляет; подпапки или сбой — папка остаётся. Переносы
    пишутся в pending для relink. True — папка разобрана и удалена."""
    folder = root / name
    parts = coauthor_parts(name)
    index = author_index(root)

    def folder_for(author: str) -> str:
        key = author_key(author)
        if key is None:
            return ""
        found = index.get(key) or folder_name(author)
        index.setdefault(key, found)
        return found

    try:
        entries = sorted(folder.iterdir())
    except OSError as error:
        run.count("errors")
        journal_row(run, {"action": "error", "folder": name, "error": str(error)})
        return False
    if any(entry.is_dir() for entry in entries):
        run.count("errors")
        journal_row(run, {"action": "error", "folder": name, "error": "есть подпапки: соавторская папка не тронута"})
        return False
    for src in entries:
        first = folder_for(parts[0])
        if not first:
            run.count("errors")
            journal_row(run, {"action": "error", "folder": name, "error": f"автор без папки: {parts[0]}"})
            return False
        dest = unique_path(root / first, src.name, taken)
        source = src.relative_to(root).as_posix()
        try:
            (root / first).mkdir(parents=True, exist_ok=True)
            os.rename(src, dest)
        except OSError as error:
            run.count("errors")
            journal_row(run, {"from": source, "to": "", "status": "error", "error": str(error)})
            continue
        run.count("moved")
        pending.append((source, dest.relative_to(root).as_posix()))
        journal_row(run, {"from": source, "to": dest.relative_to(root).as_posix(), "status": "move", "error": ""})
        copy_coauthors(run, root, dest, parts[1:], folder_for, taken, dry_run=False)
    try:
        os.rmdir(folder)
    except OSError as error:
        run.count("errors")
        journal_row(run, {"action": "error", "folder": name, "error": f"не удалена: {error}"})
        return False
    run.count("coauthor_folders")
    journal_row(run, {"action": "rmdir", "folder": name, "status": "done", "error": ""})
    return True


def remove_empty_folder(run: NightRun, folder: Path) -> bool:
    """Удаляет папку без файлов (только пустые подкаталоги) снизу вверх. True — удалена."""
    for current, _dirs, _files in os.walk(folder, topdown=False):
        try:
            os.rmdir(current)
        except OSError as error:
            run.count("errors")
            journal_row(run, {"action": "error", "folder": folder.name, "error": str(error)})
            return False
    run.count("emptied")
    journal_row(run, {"action": "rmdir", "folder": folder.name, "status": "done", "error": ""})
    return True


def author_variants(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Пары «одно ФИО — разное написание» (variant_candidates). Пустая папка без файлов удаляется, пара — склеена.
    Общие книги склеиваются в папку с полным ФИО через merge_folder (как merge-authors). Пары manual остаются в очереди
    на решение во вкладке «Авторы». Решённые пары не пересчитываются. Папка, уже ушедшая в другую пару в этом прогоне,
    пропускается. Журнал построчно: откат по move и rmdir."""
    root = Path(cfg["books_dir"])
    db_path = cfg["db_path"]
    run = NightRun(cfg, "author-variants", dry_run, None, out)
    candidates = variant_candidates(root, db_path)
    statuses = variant_statuses(db_path)
    todo = [c for c in candidates if statuses.get((c["a"], c["b"]), "pending") == "pending"]
    if not dry_run:
        migrate_database(db_path, out=out)
        store_variants(db_path, candidates)
    tiers = {tier: sum(c["tier"] == tier for c in todo) for tier in VARIANT_TIER_NAMES}
    gone: set[str] = set()
    taken: set[Path] = set()
    pending: list[tuple[str, str]] = []
    for cand in todo:
        a, b, tier = cand["a"], cand["b"], cand["tier"]
        if a in gone or b in gone or not (root / a).is_dir() or not (root / b).is_dir():
            run.count("skipped")
            continue
        coauthor = next((name for name in (a, b) if coauthor_parts(name)), "")
        if coauthor:  # соавторская папка: её книги — в папки обоих авторов, независимо от tier
            if dry_run:
                run.count("coauthor")
            elif distribute_coauthor(run, root, coauthor, taken, pending):
                set_variant(db_path, a, b, "merged", b if coauthor == a else a)
            gone.add(coauthor)
            continue
        if tier == "manual":
            continue
        if tier == "empty":
            empty = [n for n in (a, b) if not cand["files"][n]]
            if len(empty) != 1:  # обе без файлов — решает пользователь
                run.count("skipped")
                continue
            target = b if empty[0] == a else a
            if dry_run:
                run.count("emptied")
            elif not remove_empty_folder(run, root / empty[0]):
                continue
            gone.add(empty[0])
            if not dry_run:
                set_variant(db_path, a, b, "merged", target)
            continue
        source, target = variant_target(a, b, cand["files"])
        known = {row["path"] for row in books_under(db_path, source)}
        merge_folder(run, db_path, root, source, target, [target], known, taken, pending)
        gone.add(source)
        if not dry_run and not (root / source).exists():
            set_variant(db_path, a, b, "merged", target)
    flush_relinks(db_path, pending, force=True)
    s = run.stats
    prefix = "будет " if dry_run else ""
    line = (f"author-variants: пар {len(todo)} (пустых {tiers['empty']}, общих {tiers['common']}, "
            f"вручную {tiers['manual']}); {prefix}склеено {s.get('merged', 0)}, {prefix}удалено пустых {s.get('emptied', 0)}, "
            f"перенесено файлов {s.get('moved', 0)}, конфликтов имён {s.get('conflicts', 0)}, ошибок {s.get('errors', 0)}")
    out(line)
    say(line, "author_variants", detail=json.dumps({k: v for k, v in s.items() if k != "checked"}, ensure_ascii=False))
    if not dry_run:
        out("дальше: запустите scan — каталог books обновит пути книг; откат — по журналу (move, rmdir)")
    return run.finish({"summary": line})


def variant_action(cfg: dict[str, Any], a: str, b: str, action: str) -> str:
    """Решение по паре вариантов: общая логика веба и API. Склейка идёт под scan_lock (ScanBusyError — занято).
    Ошибки для пользователя — ValueError."""
    lo, hi = sorted((a, b))
    if lo == hi or action not in ("merge-into-a", "merge-into-b", "different"):
        raise ValueError("нужны две разные папки и action: merge-into-a, merge-into-b или different")
    db_path = cfg["db_path"]
    statuses = variant_statuses(db_path)
    if (lo, hi) not in statuses:
        raise ValueError("пара вариантов не найдена")
    if statuses[(lo, hi)] != "pending":
        raise ValueError("пара уже решена")
    if action == "different":
        set_variant(db_path, lo, hi, "different")
        say(f"варианты автора: «{lo}» и «{hi}» — разные авторы", "variant_action")
        return "Разные авторы: пара больше не предлагается."
    target = a if action == "merge-into-a" else b
    source = hi if target == lo else lo
    root = Path(cfg["books_dir"])
    if not (root / source).is_dir() or not (root / target).is_dir():
        raise ValueError("одной из папок уже нет: пара будет пересчитана при следующем прогоне")
    with scan_lock():
        run = NightRun(cfg, "author-variants", False, None, lambda _line: None)
        pending: list[tuple[str, str]] = []
        known = {row["path"] for row in books_under(db_path, source)}
        merge_folder(run, db_path, root, source, target, [target], known, set(), pending)
        flush_relinks(db_path, pending, force=True)
        run.finish()
    if (root / source).exists():
        raise ValueError(f"папка «{source}» не пуста после переноса: см. журнал прогона")
    set_variant(db_path, lo, hi, "merged", target)
    return (f"склеено в «{target}»: перенесено файлов {run.stats.get('moved', 0)}, "
            f"конфликтов имён {run.stats.get('conflicts', 0)}")


FILE_CHANGING = ("fix-encoding", "unpack-fb2zip", "convert", "dedup", "dedup-folders", "zip-books", "unzip-books",
                 "author-variants", "reconvert-fb2")
COMMANDS = ("scan", "migrate", *FILE_CHANGING)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_OUTPUT_LINES = 2000


def command_params(command: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Проверяет и нормализует параметры режима (общие для CLI и API). Неизвестные ключи — ошибка."""
    if command not in (*COMMANDS, *CLI_COMMANDS):
        raise ConfigError(f"неизвестная команда {command!r}; допустимо: {', '.join(COMMANDS)}")
    allowed = {
        "scan": set(), "migrate": set(), "fix-encoding": {"path", "only"}, "unpack-fb2zip": {"path", "max_mb"},
        "convert": {"path", "only", "to", "overwrite", "skip_newer", "exclude", "author", "title_mode", "toc", "toc_depth",
                    "delete_source", "workers"},
        "dedup": {"path", "workers"}, "dedup-folders": {"path", "workers"}, "zip-books": {"path", "workers"},
        "unzip-books": {"path", "workers"}, "unpack-collections": {"path"}, "merge-authors": set(),
        "author-variants": set(), "sort-underscore": {"limit"},
        "reconvert-fb2": {"path", "workers", "limit", "sample", "out", "epubcheck"},
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
    if "limit" in allowed:
        limit = raw.get("limit")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000000):
            raise ConfigError("limit: целое 1..1000000")
        params["limit"] = limit
    if command == "unpack-fb2zip":
        params["max_mb"] = raw.get("max_mb", 200)
        if not isinstance(params["max_mb"], int) or isinstance(params["max_mb"], bool) or not 1 <= params["max_mb"] <= 4096:
            raise ConfigError("max_mb: целое 1..4096")
    if command == "reconvert-fb2":
        params.update(epubcheck=raw.get("epubcheck", False), out=raw.get("out") or None)
        for key, high in (("limit", 1_000_000), ("sample", 10_000)):
            value = raw.get(key, 0) or 0
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= high:
                raise ConfigError(f"{key}: целое 0..{high}")
            params[key] = value
        if not isinstance(params["epubcheck"], bool) or not (params["out"] is None or isinstance(params["out"], str)):
            raise ConfigError("epubcheck — логическое значение, out — строка пути")
        if bool(params["sample"]) != bool(params["out"]):
            raise ConfigError("--sample и --out задаются вместе")
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
        if command == "dedup-folders":
            return dedup_folders(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "author-variants":
            return author_variants(cfg, dry_run, out=out)
        if command == "zip-books":
            return zip_books(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "unzip-books":
            return unzip_books(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "unpack-collections":
            return unpack_collections(cfg, dry_run, params["path"], out=out)
        if command == "sort-underscore":
            return sort_underscore(cfg, dry_run, params["limit"], out=out)
        if command == "merge-authors":
            return merge_authors(cfg, dry_run, out=out)
        if command == "reconvert-fb2":
            return reconvert_fb2(cfg, dry_run, params["path"], params["workers"], params["limit"], params["sample"],
                                 params["out"], params["epubcheck"], out=out)
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
    if command in CLI_COMMANDS:
        raise ConfigError(f"{command}: режим доступен только из командной строки")
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
       ("/shelves", "Полки"), ("/recent", "Недавние"), ("/fix", "Проверка"), ("/dups", "Дубликаты"), ("/stats", "Статистика")]


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


def book_collections(conn: sqlite3.Connection, paths: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Все сборники книг по путям из book_collections: {путь: [(раздел, сборник), …]}. До схемы v4 таблицы нет — пусто."""
    found: dict[str, list[tuple[str, str]]] = {}
    unique = list(dict.fromkeys(paths))
    if not unique or conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_COLLECTIONS:
        return found
    for start in range(0, len(unique), 500):
        chunk = unique[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for path, section, collection in conn.execute(
                f"SELECT DISTINCT path, section, collection FROM book_collections WHERE path IN ({marks}) "
                "ORDER BY path, section, collection", chunk):
            found.setdefault(path, []).append((section, collection))
    return found


def section_counts(conn: sqlite3.Connection, order: str) -> list[sqlite3.Row]:
    """Разделы и сборники с числом книг: сборники — из VIEW collections (схема v4), книги без сборника — из books."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_COLLECTIONS:
        source = ("SELECT section, collection, books AS n FROM collections UNION ALL "
                  "SELECT section, '', count(*) FROM books WHERE collection='' GROUP BY section")
    else:
        source = "SELECT section, collection, count(*) AS n FROM books GROUP BY section, collection"
    return conn.execute(f"SELECT section, collection, n FROM ({source}) ORDER BY {order}").fetchall()


def collection_tag(section: str, collection: str) -> str:
    """Тег-ссылка на сборник: поиск по разделу и сборнику, по папкам."""
    return (f'<a class="tag" href="/?section={quote(section)}&collection={quote(collection)}&sort=path">'
            f'{e(collection)}</a>')


def book_json(row: sqlite3.Row, pairs: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k not in ("mtime",)}  # noqa: SIM118 — у sqlite3.Row нет __iter__ по ключам
    data["path"] = display_path(row["path"], row["fmt"])
    data["authors"] = [a for a in row["authors"].split("; ") if a]
    data["genres"] = [g for g in row["genres"].split("; ") if g]
    data["cover"] = bool(row["cover"])
    data["links"] = {"page": f"/book/{row['id']}", "file": f"/file/{row['id']}", "read": f"/read/{row['id']}",
                     "cover": f"/cover/{row['id']}" if row["cover"] else None}
    if is_wrapped(row["path"], row["fmt"]):
        data["links"]["raw"] = f"/api/books/{row['id']}/file"  # исходный однокнижный архив — только через API
    data["collections"] = [{"section": s, "collection": c} for s, c in pairs or []]
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
    "POST /api/jobs": "{command: scan|migrate|fix-encoding|unpack-fb2zip|convert|dedup|dedup-folders|zip-books|unzip-books|"
                      "author-variants, dry_run: true (по умолчанию), "
                      "confirm: <id успешного dry-run с теми же параметрами> — обязателен для боевого запуска fix-encoding/unpack-fb2zip/"
                      "convert/dedup/dedup-folders/zip-books/unzip-books/author-variants, "
                      "параметры режима: path, only (для convert — fb2 и источники), workers, max_mb, to, overwrite, skip_newer, exclude, "
                      "author, title_mode, toc, "
                      "toc_depth, delete_source}",
    "GET /api/dups": "группы дублей: status=similar|auto|resolved (по умолчанию similar), page (по 50); "
                     "в members путь для показа — path, для действий — key_path",
    "POST /api/dups/<id>": "{action: keep-both|resolve|quarantine|restore, path: key_path участника} — решение по группе "
                           "(resolve — только пометить решённой, path не нужен)",
    "POST /api/progress": "позиция чтения (форма id, ch, p) — используется читалкой",
    "POST /api/books/<id>/edit": "{title, authors[], series, series_no, genres[], year, lang, annotation, rename?: bool} — "
                                 "правка метаданных; epub/fb2 переписываются в файле (прежняя версия в карантине edited/), "
                                 "остальные форматы — только в каталоге",
    "GET /api/fix": "проблемные метаданные: reason=semi|garbage|title|author, page (по 100); в items: id, path, title, "
                    "authors, reasons, suggestion {title, authors} | null",
    "POST /api/authors/rename": "{old, new} — переименовать автора во всех его книгах (слияние с существующим)",
    "GET /api/authors?variants=1": "варианты написания папок автора: status=pending|merged|different (по умолчанию pending)",
    "POST /api/authors": "{a, b, action: merge-into-a|merge-into-b|different} — решение по паре вариантов; склейка под блокировкой",
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
            ("GET", "zip"): self.view_zip, ("GET", "dups"): self.view_dups, ("POST", "dups"): self.post_dups,
            ("GET", "fix"): self.view_fix, ("POST", "fix"): self.post_fix, ("POST", "authors"): self.post_authors,
            ("GET", "authors"): self.view_authors, ("GET", "sections"): self.view_sections, ("GET", "series"): self.view_series, ("GET", "genres"): self.view_genres,
            ("GET", "browse"): self.view_browse, ("GET", "shelves"): self.view_shelves, ("GET", "recent"): self.view_recent,
            ("GET", "stats"): self.view_stats, ("GET", "opds"): self.view_opds, ("GET", "app.js"): self.view_js,
            ("POST", "book"): self.post_book, ("POST", "shelves"): self.post_shelves, ("POST", "scan"): self.post_scan,
            ("GET", "edit"): self.view_edit, ("POST", "edit"): self.post_edit,
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
            "series": "b.series=?", "fmt": "b.fmt=?", "lang": "b.lang=?", "status": "m.status=?",
            "shelf": "b.path IN (SELECT path FROM shelf_items WHERE shelf_id=?)",
        }
        for key, clause in filters.items():
            if params.get(key):
                where.append(clause)
                args.append(params[key])
        section, collection = params.get("section", ""), params.get("collection", "")
        if collection and conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_COLLECTIONS:
            # сборник — по book_collections: книга-keeper из другого раздела тоже попадает в свой сборник
            where.append("b.path IN (SELECT path FROM book_collections WHERE collection=? AND (?='' OR section=?))")
            args.extend([collection, section, section])
        else:
            if section:
                where.append("b.section=?")
                args.append(section)
            if collection:
                where.append("b.collection=?")
                args.append(collection)
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
        with closing(self.db()) as conn:  # сборники всех книг страницы — одним запросом
            colls = book_collections(conn, [row["path"] for row in rows])
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
                         f'{authors}{series}<br><span class="tag">{e(row["fmt"])}</span>'
                         f'{self.section_tag(row, colls.get(row["path"]))}<span class="mut">'
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
        rename = ""
        if params.get("author"):
            rename = (f'<form class="s" method="post" action="/authors/rename"><input type="hidden" name="old" '
                      f'value="{e(params["author"])}"><input name="new" value="{e(params["author"])}" maxlength="200" '
                      f'required size="40"><button>Переименовать / слить автора во всех книгах</button></form>')
        self.page(params.get("q") or "Поиск", form + active + rename + self.pager(total, page, size, params) + self.book_list(rows)
                  + self.pager(total, page, size, params))

    def grouped(self, title: str, sql: str, link: Callable[[str], str], label: Callable[[str], str] = str,
                prefix: str = "") -> None:
        """Списки авторов и серий: по первой букве и по подстроке. prefix — строка над списком (ссылка на варианты)."""
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
        self.page(title, f'<h2>{title}</h2>{prefix}<form class="s"><input name="q" value="{e(needle)}" placeholder="Найти">'
                         f'<button>Найти</button></form><p class="letters">{bar}</p><div class="cols">{items}</div>')

    def view_authors(self, _parts: list[str]) -> None:
        if self.query.get("variants") == "1":
            self.view_variants()
            return
        with closing(self.db()) as conn:
            pending = variants_pending(conn)
        link = f'<p><a href="/authors?variants=1">Варианты написания: ожидают решения {pending}</a></p>'
        self.grouped("Авторы", "SELECT name AS k, count(*) AS c FROM book_authors GROUP BY name",
                     lambda a: f"/?author={quote(a)}", prefix=link)

    def view_variants(self) -> None:
        """Пары папок одного автора с разным написанием: решения «Склеить в …», «Разные авторы» (POST /authors/variant)."""
        show_resolved = self.query.get("resolved") == "1"
        statuses = ("pending", "merged", "different") if show_resolved else ("pending",)
        page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
        marks = ",".join("?" * len(statuses))
        with closing(self.db()) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < VARIANT_SCHEMA:
                total, rows, books = 0, [], {}
            else:
                total = conn.execute(f"SELECT count(*) FROM author_variants WHERE status IN ({marks})", statuses).fetchone()[0]
                rows = conn.execute(
                    f"SELECT a, b, tier, status, target FROM author_variants WHERE status IN ({marks}) "
                    "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, a, b LIMIT ? OFFSET ?",
                    (*statuses, VARIANT_PAGE_SIZE, (page - 1) * VARIANT_PAGE_SIZE)).fetchall()
                books = folder_book_counts(conn)

        def folder_link(name: str) -> str:
            return f'<a href="/?dir={quote(name)}">{e(name)}</a> <span class="mut">книг {books.get(name, 0)}</span>'

        def button(a: str, b: str, action: str, label: str) -> str:
            return (f'<form class="s" method="post" action="/authors/variant"><input type="hidden" name="a" value="{e(a)}">'
                    f'<input type="hidden" name="b" value="{e(b)}"><input type="hidden" name="action" value="{action}">'
                    f'<button>{e(label)}</button></form>')

        items = []
        for row in rows:
            a, b = row["a"], row["b"]
            if row["status"] == "pending":
                actions = (button(a, b, "merge-into-a", f"Склеить в «{a}»") + button(a, b, "merge-into-b", f"Склеить в «{b}»")
                           + button(a, b, "different", "Разные авторы"))
            else:
                actions = e(VARIANT_STATUS_NAMES[row["status"]] + (f": {row['target']}" if row["target"] else ""))
            items.append(f'<tr><td>{folder_link(a)}</td><td>{folder_link(b)}</td>'
                         f'<td>{e(VARIANT_TIER_NAMES.get(row["tier"], row["tier"]))}</td><td>{actions}</td></tr>')
        checked = " checked" if show_resolved else ""
        heading = ('<h2>Варианты написания авторов</h2><form class="s" method="get" action="/authors">'
                   '<input type="hidden" name="variants" value="1"><label><input type="checkbox" name="resolved" value="1"'
                   f'{checked}> Решённые</label> <button>Показать</button></form>')
        table = ("<table><tr><th>Папка</th><th>Папка</th><th>Вид</th><th>Действия</th></tr>" + "".join(items) + "</table>"
                 if items else "<p>Вариантов нет. Их ищет режим author-variants (ночная обработка).</p>")
        params = {"variants": "1"}
        if show_resolved:
            params["resolved"] = "1"
        self.page("Варианты написания авторов", f"{heading}{table}{self.pager(total, page, VARIANT_PAGE_SIZE, params)}")

    def view_series(self, _parts: list[str]) -> None:
        self.grouped("Серии", "SELECT series AS k, count(*) AS c FROM books WHERE series <> '' GROUP BY series",
                     lambda s: f"/?series={quote(s)}&sort=series")

    @staticmethod
    def section_tag(row: sqlite3.Row, pairs: list[tuple[str, str]] | None = None) -> str:
        """Теги раздела книги и всех её сборников; pairs — из book_collections (None — прежний вид по books)."""
        tag = f'<a class="tag" href="/?section={quote(row["section"])}">{e(row["section"])}</a>' if row["section"] else ""
        if pairs is None:
            pairs = [(row["section"], row["collection"])] if row["collection"] else []
        return tag + "".join(collection_tag(s, c) for s, c in pairs if c)

    def view_sections(self, _parts: list[str]) -> None:
        """Разделы (папки «_…») с коллекциями: журналы, сборники, документация, неразобранное."""
        with closing(self.db()) as conn:
            rows = section_counts(conn, "section = '', section, collection")
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
            colls = book_collections(conn, [book["path"]]).get(book["path"], [])
        bid = book["id"]
        authors = ", ".join(f'<a href="/?author={quote(a)}">{e(a)}</a>' for a in book["authors"].split("; ") if a) or "—"
        genres = " ".join(f'<a class="tag" href="/?genre={quote(g)}">{e(genre_name(g))}</a>' for g in book["genres"].split("; ") if g)
        series = (f'<p>Серия: <a href="/?series={quote(book["series"])}&sort=series">{e(book["series"])}</a>'
                  f'{" #" + str(book["series_no"]) if book["series_no"] else ""}</p>') if book["series"] else ""
        collections = "".join(collection_tag(s, c) for s, c in colls if c)
        series += f'<p>Сборники: {collections}</p>' if collections else ""
        if book["fmt"] == "pdf":
            actions = [f'<a href="/file/{bid}?inline=1"><b>Открыть PDF</b></a>']
        elif book["fmt"] == "zip":
            actions = [f'<a href="/read/{bid}"><b>Содержимое архива</b></a>']
        else:
            actions = [f'<a href="/read/{bid}"><b>Читать</b></a>']
        actions.append(f'<a href="/file/{bid}">Скачать ({human_size(book["size"])})</a>')
        folder = str(PurePosixPath(book["path"]).parent)
        actions.append(f'<a href="/browse?dir={quote(folder if folder != "." else "")}">Папка</a>')
        actions.append(f'<a href="/edit/{bid}">Редактировать</a>')
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

    def view_edit(self, parts: list[str]) -> None:
        with closing(self.db()) as conn:
            book = self.book_or_404(conn, parts[0] if len(parts) == 1 else "")
            if not book:
                self.error_page(HTTPStatus.NOT_FOUND, "Книга не найдена")
                return
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            override = (conn.execute("SELECT * FROM meta_overrides WHERE path=?", (book["path"],)).fetchone()
                        if version >= 3 else None)
        values = {key: book[key] for key in EDIT_KEYS}
        if override is not None:
            values.update({key: override[key] for key in EDIT_KEYS if override[key] not in (None, "")})
        bid = book["id"]
        authors = "\n".join(a for a in str(values["authors"]).split("; ") if a)
        genres = ", ".join(g for g in str(values["genres"]).split("; ") if g)
        note = ("Изменения запишутся в файл книги (прежняя версия — в карантин edited/)" if book["fmt"] in ("epub", "fb2")
                else "Формат не поддерживает запись метаданных: изменения сохраняются только в каталоге")
        body = (f'<h2>Редактирование</h2><form method="post" action="/edit/{bid}">'
                f'<p><label>Название<br><input name="title" value="{e(values["title"])}" maxlength="500" size="60" required>'
                f'</label></p>'
                f'<p><label>Авторы (по одному в строке)<br><textarea name="authors" rows="3" cols="50">{e(authors)}</textarea>'
                f'</label></p>'
                f'<p><label>Серия<br><input name="series" value="{e(values["series"])}" maxlength="300" size="50"></label> '
                f'<label>№ <input name="series_no" value="{e(values["series_no"])}" size="6" maxlength="6"></label></p>'
                f'<p><label>Жанры (через запятую, коды fb2)<br><input name="genres" value="{e(genres)}" size="60"></label></p>'
                f'<p><label>Год <input name="year" value="{e(values["year"])}" size="6" maxlength="4"></label> '
                f'<label>Язык <input name="lang" value="{e(values["lang"])}" size="6" maxlength="20"></label></p>'
                f'<p><label>Аннотация<br><textarea name="annotation" rows="8" cols="60">{e(values["annotation"])}</textarea>'
                f'</label></p>'
                f'<p><label><input type="checkbox" name="rename" value="1"> '
                f'Переименовать файл по метаданным (Автор - Название)</label></p>'
                f'<p class="mut">{note}</p>'
                f'<p><button>Сохранить</button> <a href="/book/{bid}">Отмена</a></p></form>')
        self.page(f"Редактирование: {book['title']}", body)

    def post_edit(self, parts: list[str]) -> None:
        if len(parts) != 1 or not parts[0].isdigit():
            self.error_page(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        form = {k: v[-1] for k, v in self.form.items()}
        raw: dict[str, Any] = {
            "title": form.get("title", ""),
            "authors": [line.strip() for line in form.get("authors", "").splitlines() if line.strip()],
            "series": form.get("series", ""),
            "series_no": form.get("series_no", ""),
            "genres": [g.strip() for g in form.get("genres", "").split(",") if g.strip()],
            "year": form.get("year", ""),
            "lang": form.get("lang", ""),
            "annotation": form.get("annotation", ""),
        }
        try:
            edit_book(self.cfg, int(parts[0]), raw, rename=form.get("rename") == "1")
        except ValueError as error:
            self.error_page(HTTPStatus.BAD_REQUEST, str(error))
            return
        except ScanBusyError:
            self.error_page(HTTPStatus.CONFLICT, "Идёт обработка библиотеки, повторите позже")
            return
        self.redirect(f"/book/{parts[0]}")

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

    def view_fix(self, _parts: list[str]) -> None:
        reason = self.query.get("reason", "")
        reason = reason if reason in FIX_REASON_TEXT else ""
        page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
        with closing(self.db()) as conn:
            counts, total, items = fix_scan(conn, reason, page, FIX_PAGE_SIZE)
        tabs = []
        for code, label in [("", "все проблемные"), *FIX_REASON_TEXT.items()]:
            text = f"{label}: {counts['all'] if not code else counts[code]}"
            if code == reason:
                tabs.append(f"<b>{e(text)}</b>")
            else:
                href = f"/fix?reason={code}" if code else "/fix"
                tabs.append(f'<a href="{href}">{e(text)}</a>')
        blocks = []
        for row, reasons, suggestion in items:
            book_id = row["id"]
            authors_now = ", ".join(name for name in str(row["authors"]).split("; ") if name)
            proposal = ""
            if suggestion:
                proposal = (f'<p>Предложение: <b>{e(suggestion["title"])}</b> — {e(", ".join(suggestion["authors"]))}</p>'
                            f'<form class="s" method="post" action="/fix/{book_id}">'
                            f'<input type="hidden" name="reason" value="{e(reason)}"><button>Применить</button></form>')
            blocks.append(f'<li><div><a href="/book/{book_id}"><b>{e(row["title"])}</b></a> '
                          f'<span class="mut">{e(display_path(row["path"], row["fmt"]))}</span><br>'
                          f'<span class="tag">{e("; ".join(reasons))}</span>'
                          f'<p class="mut">Сейчас: {e(authors_now) or "—"}</p>{proposal}'
                          f'<a href="/edit/{book_id}">Редактировать</a></div></li>')
        listing = f'<ul class="list">{"".join(blocks)}</ul>' if blocks else "<p>Проблем не найдено.</p>"
        params = {"reason": reason} if reason else {}
        pager = self.pager(total, page, FIX_PAGE_SIZE, params)
        self.page("Проверка метаданных", f"<h2>Проверка метаданных</h2><p>{' · '.join(tabs)}</p>"
                  f"{pager}{listing}{pager}")

    def post_fix(self, parts: list[str]) -> None:
        if len(parts) != 1 or not parts[0].isdigit():
            self.error_page(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        form = {k: v[-1] for k, v in self.form.items()}
        reason = form.get("reason", "")
        reason = reason if reason in FIX_REASON_TEXT else ""
        with closing(self.db()) as conn:
            row = conn.execute(f"SELECT {FIX_COLUMNS} FROM books WHERE id=?", (int(parts[0]),)).fetchone()
        if row is None:
            self.error_page(HTTPStatus.NOT_FOUND, "Книга не найдена")
            return
        # Значения берутся из БД заново, а не из формы: форма передаёт только причину фильтра.
        _, suggestion = suspicious_fields(row)
        if not suggestion:
            self.error_page(HTTPStatus.BAD_REQUEST, "Для этой книги нет автоматического исправления")
            return
        try:
            edit_book(self.cfg, int(parts[0]), suggestion, rename=False)
        except ValueError as error:
            self.error_page(HTTPStatus.BAD_REQUEST, str(error))
            return
        except ScanBusyError:
            self.error_page(HTTPStatus.CONFLICT, "Идёт обработка библиотеки, повторите позже")
            return
        self.redirect(f"/fix?reason={reason}" if reason else "/fix")

    def post_authors(self, parts: list[str]) -> None:
        if parts == ["variant"]:
            form = {k: v[-1] for k, v in self.form.items()}
            try:
                variant_action(self.cfg, form.get("a", ""), form.get("b", ""), form.get("action", ""))
            except ValueError as error:
                self.error_page(HTTPStatus.BAD_REQUEST, str(error))
                return
            except ScanBusyError:
                self.error_page(HTTPStatus.CONFLICT, "Идёт обработка библиотеки, повторите позже")
                return
            self.redirect("/authors?variants=1")
            return
        if parts != ["rename"]:
            self.error_page(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        form = {k: v[-1] for k, v in self.form.items()}
        try:
            message = rename_author(self.cfg, form.get("old", ""), form.get("new", ""))
        except ValueError as error:
            self.error_page(HTTPStatus.BAD_REQUEST, str(error))
            return
        except ScanBusyError:
            self.error_page(HTTPStatus.CONFLICT, "Идёт обработка библиотеки, повторите позже")
            return
        target = form.get("new", "").strip()
        self.page("Автор переименован", f"<h2>Переименование автора</h2><p>{e(message)}</p>"
                  f'<p><a href="/?author={quote(target)}">Открыть «{e(target)}»</a></p>')

    def view_dups(self, _parts: list[str]) -> None:
        status = self.query.get("status", "similar")
        if status not in DUP_STATUS_NAMES:
            status = "similar"
        page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
        show_resolved = self.query.get("resolved") == "1" and status != "resolved"
        statuses = (status, "resolved") if show_resolved else (status,)
        with closing(self.db()) as conn:
            counts = dict(conn.execute("SELECT status, count(*) FROM dup_groups GROUP BY status").fetchall())
            total, groups = dup_groups_page(conn, status, page, DUP_PAGE_SIZE, statuses)
        tabs = []
        for key, label in DUP_STATUS_NAMES.items():
            if key == "resolved":
                continue  # решённые показываются галочкой «Решённые» рядом с заголовком
            text = f"{label}: {counts.get(key, 0)}"
            tabs.append(f"<b>{e(text)}</b>" if key == status else f'<a href="/dups?status={key}">{e(text)}</a>')
        heading = "<h2>Дубликаты</h2>"
        if status != "resolved":
            checked = " checked" if show_resolved else ""
            heading = ('<form id="dup-resolved" method="get" action="/dups" hidden>'
                       f'<input type="hidden" name="status" value="{e(status)}"></form>'
                       f'<h2>Дубликаты <label><input form="dup-resolved" type="checkbox" name="resolved" value="1"{checked}>'
                       f" Решённые: {counts.get('resolved', 0)}</label> <button form=\"dup-resolved\">Показать</button></h2>")
        keep_resolved = '<input type="hidden" name="resolved" value="1">' if show_resolved else ""

        def button(gid: int, action: str, path: str, label: str) -> str:
            return (f'<form class="s" method="post" action="/dups/{gid}"><input type="hidden" name="action" value="{action}">'
                    f'<input type="hidden" name="path" value="{e(path)}"><input type="hidden" name="back_status" '
                    f'value="{e(status)}">{keep_resolved}<button>{e(label)}</button></form>')

        blocks = []
        for group in groups:
            gid = group["id"]
            kept_count = sum(m["role"] == "kept" for m in group["members"])
            rows = []
            for m in group["members"]:
                shown = display_path(m["path"], m["fmt"])
                kept = m["role"] == "kept"
                role = "оставлена" if kept else ("в карантине" if m["quarantine"] else "дубль")
                coverage = "—" if m["coverage"] is None else f"{m['coverage'] * 100:.1f} %"
                if m["book_id"]:
                    book_id = m["book_id"]
                    links = f'<a href="/book/{book_id}">книга</a> · <a href="/read/{book_id}">читать</a>'
                else:
                    links = "<span class='mut'>нет в каталоге</span>"
                actions = ""
                if kept and kept_count > 1:
                    actions = button(gid, "quarantine", m["path"], "В карантин")
                elif m["role"] == "duplicate" and m["quarantine"]:
                    actions = button(gid, "restore", m["path"], "Восстановить")
                rows.append(f'<tr><td><span title="{e(shown)}">{e(PurePosixPath(shown).name)}</span></td>'
                            f'<td>{e(m["fmt"])}</td><td>{human_size(m["size"])}</td><td>{m["words"]}</td>'
                            f'<td>{coverage}</td><td>{role}</td><td>{links}</td><td>{actions}</td></tr>')
            keep = button(gid, "keep-both", "", "Оставить все") if group["status"] == "similar" else ""
            head = (f'<div style="display:flex;flex-wrap:wrap;gap:.4rem">{keep}{button(gid, "resolve", "", "Решено")}</div>'
                    if group["status"] in ("similar", "auto") else "")  # кнопки в одном ряду
            blocks.append(f'<section class="dup"><h3>{e(group["key"])} <span class="tag">'
                          f'{e(DUP_STATUS_NAMES.get(group["status"], group["status"]))}</span></h3>{head}'
                          f'<table><tr><th>Файл</th><th>Формат</th><th>Размер</th><th>Слов</th><th>Покрытие</th>'
                          f'<th>Роль</th><th>Ссылки</th><th>Действия</th></tr>{"".join(rows)}</table></section>')
        listing = "".join(blocks) or "<p>Групп нет. Дубликаты ищут режимы dedup и dedup-folders (ночная обработка).</p>"
        params = {"status": status}
        if show_resolved:
            params["resolved"] = "1"
        self.page("Дубликаты", f"{heading}<p>{' · '.join(tabs)}</p>{listing}"
                               f"{self.pager(total, page, DUP_PAGE_SIZE, params)}")

    def post_dups(self, parts: list[str]) -> None:
        if len(parts) != 1 or not parts[0].isdigit():
            self.error_page(HTTPStatus.NOT_FOUND, "Страница не найдена")
            return
        form = {k: v[-1] for k, v in self.form.items()}
        back = form.get("back_status", "similar")
        if back not in DUP_STATUS_NAMES:
            back = "similar"
        try:
            dup_action(self.cfg, int(parts[0]), form.get("action", ""), form.get("path", ""))
        except ValueError as error:
            self.error_page(HTTPStatus.BAD_REQUEST, str(error))
            return
        except ScanBusyError:
            self.error_page(HTTPStatus.CONFLICT, "Идёт обработка библиотеки, повторите позже")
            return
        keep_resolved = "&resolved=1" if form.get("resolved") == "1" else ""
        self.redirect(f"/dups?status={back}{keep_resolved}")

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
                colls = book_collections(conn, [book["path"]])
                self.send_json({**book_json(book, colls.get(book["path"])), "mark": dict(mark) if mark else None,
                                "shelves": [dict(x) for x in shelves]})
            elif name == "books":
                params = {k: v for k, v in self.query.items() if k not in ("page", "per_page") and v}
                page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
                size = min(max(int(self.query["per_page"]), 1), 500) if self.query.get("per_page", "").isdigit() \
                    else self.cfg["page_size"]
                total, rows = self.search_rows(conn, params, size, (page - 1) * size)
                colls = book_collections(conn, [r["path"] for r in rows])
                self.send_json({"total": total, "page": page, "per_page": size,
                                "items": [book_json(r, colls.get(r["path"])) for r in rows]})
            elif name == "authors" and self.query.get("variants") == "1":
                status = self.query.get("status", "pending")
                if status not in VARIANT_STATUS_NAMES:
                    self.send_json({"error": "status: pending, merged или different"}, HTTPStatus.BAD_REQUEST)
                    return
                rows = conn.execute("SELECT a, b, tier, status, target FROM author_variants WHERE status = ? "
                                    "ORDER BY a, b LIMIT 1000", (status,)).fetchall()
                self.send_json({"items": [dict(row) for row in rows]})
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
                rows = section_counts(conn, "1, 2")
                self.send_json([{"section": r[0] or FICTION_SECTION, "section_key": r[0], "collection": r[1], "count": r[2]}
                                for r in rows])
            elif name == "shelves":
                rows = conn.execute("SELECT s.id, s.name, count(i.path) AS count FROM shelves s LEFT JOIN shelf_items i "
                                    "ON i.shelf_id=s.id GROUP BY s.id ORDER BY s.name")
                self.send_json([dict(r) for r in rows])
            elif name == "recent":
                rows = conn.execute("SELECT b.* FROM marks m JOIN books b ON b.path=m.path WHERE m.opened IS NOT NULL "
                                    "ORDER BY m.opened DESC LIMIT 100").fetchall()
                colls = book_collections(conn, [r["path"] for r in rows])
                self.send_json([book_json(r, colls.get(r["path"])) for r in rows])
            elif name == "stats":
                total = conn.execute("SELECT count(*), coalesce(sum(size), 0) FROM books").fetchone()
                fmts = conn.execute("SELECT fmt, count(*), sum(size) FROM books GROUP BY fmt ORDER BY 2 DESC").fetchall()
                statuses = conn.execute("SELECT status, count(*) FROM marks WHERE status <> '' GROUP BY status").fetchall()
                last = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
                self.send_json({"books": total[0], "bytes": total[1], "last_scan": last[0] if last else None,
                                "formats": {r[0]: {"books": r[1], "bytes": r[2]} for r in fmts},
                                "statuses": {r[0]: r[1] for r in statuses}, "scan": SCAN_STATE})
            elif name == "fix":
                reason = self.query.get("reason", "")
                if reason and reason not in FIX_REASON_TEXT:
                    self.send_json({"error": "reason: semi, garbage, title или author"}, HTTPStatus.BAD_REQUEST)
                    return
                page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
                _, total, items = fix_scan(conn, reason, page, FIX_PAGE_SIZE)
                self.send_json({"total": total, "page": page, "items": [
                    {"id": row["id"], "path": display_path(row["path"], row["fmt"]), "title": row["title"],
                     "authors": [name for name in row["authors"].split("; ") if name], "reasons": reasons,
                     "suggestion": {"title": s["title"], "authors": s["authors"]} if s else None}
                    for row, reasons, s in items]})
            elif name == "dups":
                status = self.query.get("status", "similar")
                if status not in DUP_STATUS_NAMES:
                    self.send_json({"error": "status: similar, auto или resolved"}, HTTPStatus.BAD_REQUEST)
                    return
                page = max(int(self.query["page"]), 1) if self.query.get("page", "").isdigit() else 1
                total, groups = dup_groups_page(conn, status, page, DUP_PAGE_SIZE)
                self.send_json({"total": total, "page": page, "items": [dup_group_json(g) for g in groups]})
            else:
                self.send_json({"error": "неизвестный ресурс", "endpoints": API_HELP}, HTTPStatus.NOT_FOUND)

    def api_post(self, parts: list[str]) -> None:
        name = parts[0] if parts else ""
        if name == "progress":
            self.post_progress(parts)
            return
        body = self.json_body
        try:
            if name == "dups" and len(parts) == 2 and parts[1].isdigit():
                action, path = body.get("action", ""), body.get("path", "")
                if not isinstance(action, str) or not isinstance(path, str):
                    raise ValueError("action и path — строки")
                if len(path) > 4096 or "\x00" in path:
                    raise ValueError("path: строка до 4096 символов без NUL")
                message = dup_action(self.cfg, int(parts[1]), action, path)
                self.send_json({"ok": True, "message": message})
                return
            if name == "books" and len(parts) == 3 and parts[1].isdigit() and parts[2] == "edit":
                rename = body.get("rename", False)
                if not isinstance(rename, bool):
                    raise ValueError("rename: true или false")
                fields = {k: v for k, v in body.items() if k != "rename"}
                message = edit_book(self.cfg, int(parts[1]), fields, rename=rename)
                with closing(self.db()) as conn:
                    row = conn.execute("SELECT * FROM books WHERE id=?", (int(parts[1]),)).fetchone()
                    colls = book_collections(conn, [row["path"]])
                self.send_json({"ok": True, "message": message, "book": book_json(row, colls.get(row["path"]))})
                return
            if name == "authors" and len(parts) == 1:
                a, b, action = body.get("a", ""), body.get("b", ""), body.get("action", "")
                if not all(isinstance(value, str) for value in (a, b, action)):
                    raise ValueError("a, b и action — строки")
                self.send_json({"ok": True, "message": variant_action(self.cfg, a, b, action)})
                return
            if name == "authors" and len(parts) == 2 and parts[1] == "rename":
                old, new = body.get("old", ""), body.get("new", "")
                if not isinstance(old, str) or not isinstance(new, str):
                    raise ValueError("old и new — строки")
                self.send_json({"ok": True, "message": rename_author(self.cfg, old, new)})
                return
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
        except ScanBusyError:
            self.send_json({"error": "Идёт обработка библиотеки, повторите позже"}, HTTPStatus.CONFLICT)

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
    parser.add_argument("command", nargs="?", choices=("serve", "scan", *FILE_CHANGING, *CLI_COMMANDS), default="serve",
                        help="serve (по умолчанию), scan, fix-encoding, unpack-fb2zip, convert, reconvert-fb2, dedup, dedup-folders, "
                             "zip-books, unzip-books, unpack-collections, merge-authors, sort-underscore или author-variants")
    parser.add_argument("-p", "--path", help="fix-encoding/unpack-fb2zip/convert: каталог (по умолчанию каталог книг)")
    parser.add_argument("--only", default="all",
                        help="fix-encoding: all|fb2|txt; convert: all|fb2|doc|docx|rtf|odt|mht|mhtml (fb2 — в epub через Calibre)")
    parser.add_argument("--workers", type=int,
                        help="convert --only fb2, dedup, dedup-folders, zip-books: потоков (по умолчанию workers из конфига); "
                             "unzip-books: потоков (по умолчанию unzip_workers)")
    parser.add_argument("--max-mb", type=int, default=200, help="unpack-fb2zip: предел размера записи, МБ")
    parser.add_argument("--limit", type=int,
                        help="sort-underscore: не больше N книг; reconvert-fb2: не больше N сопоставленных книг")
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
    parser.add_argument("--sample", type=int, default=0,
                        help="reconvert-fb2: N книг в --out для проверки глазами; библиотека не меняется")
    parser.add_argument("--out", help="reconvert-fb2 с --sample: каталог выборки (вне каталога книг)")
    parser.add_argument("--epubcheck", action="store_true",
                        help="reconvert-fb2: проверять каждый новый epub утилитой epubcheck (медленно, выборочно)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="ничего не менять, показать план")
    parser.add_argument("--migrate-db", action="store_true", help="только миграции схемы БД")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="путь к books_library.json")
    args = parser.parse_args(argv)
    setup_logging()
    if args.limit is not None and args.command not in ("sort-underscore", "reconvert-fb2"):
        parser.error("--limit применяется только к sort-underscore и reconvert-fb2")
    try:
        cfg = load_config(args.config)
        if args.migrate_db or args.command != "serve":
            command = "migrate" if args.migrate_db else args.command
            raw: dict[str, Any] = {}
            if command in FILE_CHANGING:
                raw = {"path": args.path, "only": args.only}
            if command == "unpack-fb2zip":
                raw = {"path": args.path, "max_mb": args.max_mb}
            elif command == "reconvert-fb2":
                raw = {"path": args.path, "workers": args.workers, "limit": args.limit, "sample": args.sample,
                       "out": args.out, "epubcheck": args.epubcheck}
            elif command in ("dedup", "dedup-folders", "zip-books", "unzip-books"):
                raw = {"path": args.path, "workers": args.workers}
            elif command == "unpack-collections":
                raw = {"path": args.path}
            elif command == "sort-underscore":
                raw = {"limit": args.limit}
            elif command == "author-variants":
                raw = {}
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
