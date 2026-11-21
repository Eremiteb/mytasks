#!/usr/bin/env python3
"""Лёгкая домашняя библиотека: каталог книг в SQLite (FTS5) и веб-интерфейс в браузере.

Только стандартная библиотека Python, без зависимостей. Режимы:
  scan            — проиндексировать каталог книг (инкрементально по размеру и mtime);
  serve           — запустить веб-интерфейс (по умолчанию);
  fix-encoding    — вылечить кодировку: fb2 с заявленной не UTF-8 при тексте в UTF-8, txt в cp1251 → UTF-8;
  unpack-fb2zip   — извлечь .fb2 из *.fb2.zip рядом с архивом; с --multi — и из многокнижных .zip (папка, md5, --out);
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
import functools
import hashlib
import hmac
import html
import io
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import struct
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
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack, closing, contextmanager, nullcontext, suppress
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from email import policy as email_policy
from email.utils import parseaddr
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
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
    "watch_interval": 30,  # опрос каталога книг в serve, с; 0 — мониторинг выключен
    # Кэш извлечённого текста для dedup (восстановимый, на быстром диске): пустая строка в text_cache_dir — кэш выключен;
    # text_cache_mb — предел сжатых данных, МБ (0 — кэш выключен). Каталог создаёт администратор, движок его не создаёт.
    "text_cache_dir": "/var/cache/books_library",
    "text_cache_mb": 20000,
    # Карантин служебных режимов: вместо удаления файлы переносятся сюда (<причина>/<путь в каталоге книг>).
    "quarantine_dir": "/mnt/copy/Books-removed",
    # Личные решения (отметки, полки, правки метаданных, решённые дубли): вне репозитория, файлы 0600, каталог 0700.
    "decisions_dir": "~/.local/share/books_library/decisions",
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
# Служебные значения вместо серии (сравнение по author_norm): жанр или метка — не серия, а series_raw.
SERIES_STOP = {"рассказы", "повести", "сборник", "сборники", "стихи", "стихотворения", "роман", "романы",
               "без серии", "вне серий", "антология"}
FOLDER_SERIES_TAIL = re.compile(r"\s*\(\s*\d+(?:\s*[,;]\s*\d+)*\s*\)\s*$")  # хвост папки: «Серия (110,124)»
FOLDER_SERIES_NUMBER = re.compile(r"^\d{1,3}\.\s+")  # ведущий «N. » в имени папки
FILE_SERIES_NUMBER = re.compile(r"^(\d{1,3})[.\s_-]+")  # номер книги в серии — в начале имени файла
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
    -- keeper, переименованные позже упаковкой zip (name.ext -> name.ext.zip, режима больше нет):
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
    # v6: подраздел и вид строки сборника (book_collections.subsection/kind; заполняет rebuild_collections);
    # многозначные метаданные книги (book_meta: ключ → значения); VIEW collection_sections — сборники с подразделами.
    """
    ALTER TABLE book_collections ADD COLUMN subsection TEXT NOT NULL DEFAULT '';
    ALTER TABLE book_collections ADD COLUMN kind TEXT NOT NULL DEFAULT '';
    CREATE TABLE book_meta(
        path TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(path, key, value)
    ) WITHOUT ROWID;
    CREATE INDEX book_meta_key ON book_meta(key, value);
    CREATE INDEX book_collections_orig ON book_collections(orig_path, source);
    CREATE VIEW collection_sections AS
        SELECT bc.section, bc.collection, bc.subsection, count(DISTINCT bc.path) AS books
        FROM book_collections bc JOIN books b ON b.path = bc.path
        GROUP BY bc.section, bc.collection, bc.subsection;
    """,
    # v7: варианты книг из карантина. purge-quarantine до удаления сохраняет каждый файл причин duplicate,
    # replaced-calibre, unzipped: keeper — путь книги библиотеки, вариантом которой был файл; removed — время удаления.
    """
    CREATE TABLE book_variants(
        variant_path TEXT NOT NULL,      -- путь в карантине относительно quarantine_dir: причина/путь
        reason TEXT NOT NULL,
        keeper TEXT NOT NULL DEFAULT '', -- путь книги в библиотеке; '' — такой книги нет
        fmt TEXT,
        size INTEGER,
        words INTEGER,
        title TEXT,
        authors TEXT,
        series TEXT,
        series_no INTEGER,
        lang TEXT,
        year TEXT,
        meta TEXT NOT NULL DEFAULT '{}', -- JSON всех полей разбора (book_meta и детали)
        sha1 TEXT,
        removed REAL,                    -- время удаления файла; NULL — файл ещё в карантине
        PRIMARY KEY(variant_path, reason)
    ) WITHOUT ROWID;
    CREATE INDEX book_variants_keeper ON book_variants(keeper);
    """,
]
DB_LATEST = len(MIGRATIONS)
# Первая схема с book_collections и VIEW collections: новые запросы к ним — только при user_version >= этого значения.
SCHEMA_COLLECTIONS = 4
# Первая схема с book_meta и подразделами сборников (subsection, kind): новые запросы — только при user_version >= этого.
SCHEMA_META = 6
# Первая схема с book_variants: варианты книги в карточке и relink keeper — только при user_version >= этого.
SCHEMA_VARIANTS = 7


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
                            ("unzip_workers", 1, 64), ("text_cache_mb", 0, 10_000_000), ("watch_interval", 0, 86400)):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or not low <= cfg[key] <= high:
            raise ConfigError(f"{key} должен быть целым от {low} до {high}")
    for key in ("books_dir", "db_path", "host", "quarantine_dir", "decisions_dir"):
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
    decisions = Path(cfg["decisions_dir"]).expanduser()
    cfg["decisions_dir"] = str(decisions if decisions.is_absolute() else PROJECT_DIR / decisions)
    if Path(cfg["decisions_dir"]).resolve().is_relative_to(REPO_DIR.resolve()):
        raise ConfigError("decisions_dir не может быть внутри репозитория: личные решения в Git не хранятся")
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


def series_stop(name: str) -> bool:
    """Служебное значение вместо серии (SERIES_STOP): не серия, а жанр или метка."""
    return author_norm(name) in SERIES_STOP


def series_pair(name: str, number: str = "") -> tuple[str, str]:
    """Дополнительная серия книги для book_meta: «имя #номер»; служебное значение — в series_raw."""
    if series_stop(name):
        return "series_raw", name
    return "series_extra", f"{name} #{number}" if number else name


def folder_series(rel: str) -> tuple[str, int] | None:
    """Серия по папке: «Двойной Герой (110,124)/2. Книга.epub» → («Двойной Герой», 2). Папка автора верхнего уровня
    серией не считается. В разделах серия — подпапка сборника (последний сегмент subsection), в _разобрать — сама папка."""
    parts = PurePosixPath(rel).parts
    number = FILE_SERIES_NUMBER.match(parts[-1]) if len(parts) > 1 else None
    if not number:
        return None
    if parts[0].startswith("_"):
        folder = parts[-2] if parts[0] == "_разобрать" and len(parts) >= 3 else subsection_of(rel).rpartition("/")[2]
    elif len(parts) >= 3:
        folder = parts[-2]
    else:
        return None
    name = FOLDER_SERIES_NUMBER.sub("", FOLDER_SERIES_TAIL.sub("", folder)).strip()
    return (name, int(number.group(1))) if name else None


def series_fields(found: dict[str, Any], rel: str, orig: str) -> None:
    """Серии поверх разбора файла. Служебная серия из описания — в series_raw, books.series пуст. Серия по папке
    (путь книги, а при его отсутствии — orig_path сборника) — book_meta series_folder и books.series, если он пуст.
    meta_overrides применяются позже и имеют приоритет."""
    kv = found.setdefault("kv", [])
    primary = found.get("series", "")
    if primary and series_stop(primary):
        kv.append(("series_raw", primary))
        found["series"], found["series_no"] = "", None
    folder = folder_series(rel) or (folder_series(orig) if orig else None)
    if not folder:
        return
    name, number = folder
    if series_stop(name):
        kv.append(("series_raw", name))
        return
    kv.append(("series_folder", f"{name} #{number}"))
    if not found.get("series"):
        found["series"], found["series_no"] = name, number


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
    meta["kv"], meta["sets"] = fb2_details(text, info, title)
    return meta


def xml_blocks(name: str, source: str) -> list[str]:
    """Содержимое всех элементов name (с любым префиксом пространства имён)."""
    return re.findall(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", source, re.S)


def xml_section(name: str, source: str) -> str:
    """Весь элемент name вместе с тегами; пусто, если его нет."""
    match = re.search(rf"<(?:\w+:)?{name}\b.*?</(?:\w+:)?{name}>", source, re.S)
    return match.group(0) if match else ""


def fb2_person(block: str) -> str:
    """Человек из fb2 целиком: ФИО, ник, email, id — одной строкой через «; »."""
    name = " ".join(part for part in (xml_tag(k, block) for k in ("last-name", "first-name", "middle-name")) if part)
    extra = [f"{label}: {xml_tag(tag, block)}" for label, tag in (("ник", "nickname"), ("email", "email"), ("id", "id"))
             if xml_tag(tag, block)]
    return "; ".join(part for part in (name, *extra) if part)


def fb2_details(text: str, info: str, title: str) -> tuple[list[tuple[str, Any]], list[tuple[str, str]]]:
    """Поля описания fb2 сверх основных колонок books: пары ключ-значение для book_meta и сборники из метаданных
    (вид, название): edition — publish-info/book-name, если отличается от названия; publisher-series — издательская серия.
    title — название из title-info без подстановок (для сравнения с book-name)."""
    pairs: list[tuple[str, Any]] = [("author_full", fb2_person(block)) for block in xml_blocks("author", info)]
    pairs += [("translator", fb2_person(block)) for block in xml_blocks("translator", info)]
    pairs += [("keyword", word) for word in re.split(r"[,;]", xml_tag("keywords", info))]
    date = re.search(r"<(?:\w+:)?date\b([^>]*)>(.*?)<", info, re.S)
    if date:
        pairs.append(("date", parse_attrs(date.group(1)).get("value", "") or strip_tags(date.group(2))))
    pairs += [("src_lang", xml_tag("src-lang", info)), ("lang", xml_tag("lang", info))]
    for seq in re.findall(r"<(?:\w+:)?sequence\b([^>]*)>", info)[1:]:
        attrs = parse_attrs(seq)
        name, number = attrs.get("name", "").strip(), attrs.get("number", "").strip()
        if name:
            pairs.append(series_pair(name, number))
    cover = re.search(r"<(?:\w+:)?image\b[^>]*?href\s*=\s*[\"']#?([^\"']+)", info)
    if cover:
        pairs.append(("coverpage", cover.group(1)))
    sets: list[tuple[str, str]] = []
    src = xml_section("src-title-info", text)
    if src:
        pairs.append(("src_title", xml_tag("book-title", src)))
        pairs += [("src_author", fb2_person(block)) for block in xml_blocks("author", src)]
    doc = xml_section("document-info", text)
    if doc:
        pairs += [("doc_author", fb2_person(block)) for block in xml_blocks("author", doc)]
        rest = re.sub(r"<(?:\w+:)?author\b.*?</(?:\w+:)?author>", "", doc, flags=re.S)
        pairs += [("program_used", xml_tag("program-used", rest)), ("doc_date", xml_tag("date", rest)),
                  ("src_ocr", xml_tag("src-ocr", rest)), ("doc_id", xml_tag("id", rest)),
                  ("doc_version", xml_tag("version", rest)), ("history", xml_tag("history", rest)[:2000])]
        pairs += [("src_url", strip_tags(url)) for url in xml_blocks("src-url", rest)]
    pub = xml_section("publish-info", text)
    if pub:
        book_name = xml_tag("book-name", pub)
        pairs += [("book_name", book_name), ("publisher", xml_tag("publisher", pub)), ("city", xml_tag("city", pub)),
                  ("publish_year", xml_tag("year", pub)), ("isbn", xml_tag("isbn", pub))]
        if book_name and author_norm(book_name) != author_norm(title):
            sets.append(("edition", book_name))
        seq = re.search(r"<(?:\w+:)?sequence\b([^>]*)>", pub)
        if seq:
            attrs = parse_attrs(seq.group(1))
            series = attrs.get("name", "").strip()
            pairs += [("publisher_series", series), ("publisher_series_no", attrs.get("number", "").strip())]
            if series:
                sets.append(("publisher-series", series))
    for match in re.finditer(r"<(?:\w+:)?custom-info\b([^>]*)>(.*?)</(?:\w+:)?custom-info>", text, re.S):
        info_type = parse_attrs(match.group(1)).get("info-type", "").strip()
        if info_type:
            pairs.append((f"custom:{info_type}", strip_tags(match.group(2))))
    return pairs, sets


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
        meta["kv"], meta["sets"] = self.details()
        if self.series:
            number = re.match(r"\d+", self.series[0][1])
            meta["series"], meta["series_no"] = self.series[0][0], (int(number.group(0)) if number else None)
        return meta

    def details(self) -> tuple[list[tuple[str, Any]], list[tuple[str, str]]]:
        """Поля OPF сверх основных колонок: dc:* (publisher, subject, date, rights, source, language, coverage, type,
        identifier, isbn, contributor с ролью), meta (belongs-to-collection с collection-type и group-position,
        calibre:rating/timestamp/title_sort, dcterms:modified) и версия EPUB. Сборник «set» — в sets."""
        pairs: list[tuple[str, Any]] = []
        sets: list[tuple[str, str]] = []
        version = re.search(r"<(?:\w+:)?package\b[^>]*?\bversion\s*=\s*[\"']([^\"']+)", self.opf)
        if version:
            pairs.append(("epub_version", version.group(1)))
        for name in ("publisher", "subject", "date", "rights", "source", "language", "coverage", "type"):
            pairs += [(name, strip_tags(value)) for value in re.findall(rf"<dc:{name}\b[^>]*>(.*?)</dc:{name}>", self.opf, re.S)]
        for attrs_text, body in re.findall(r"<dc:identifier\b([^>]*)>(.*?)</dc:identifier>", self.opf, re.S):
            value, attrs = strip_tags(body), parse_attrs(attrs_text)
            scheme = attrs.get("opf:scheme", attrs.get("scheme", ""))
            pairs.append(("identifier", value))
            if scheme.lower() == "isbn" or value.lower().startswith("urn:isbn:"):
                pairs.append(("isbn", value.split(":")[-1]))
        roles = {"trl": "translator", "edt": "editor", "ill": "illustrator"}
        for attrs_text, body in re.findall(r"<dc:contributor\b([^>]*)>(.*?)</dc:contributor>", self.opf, re.S):
            attrs = parse_attrs(attrs_text)
            role = attrs.get("opf:role", attrs.get("role", "")).lower()
            pairs.append((roles.get(role, "contributor"), strip_tags(body)))
        refines: dict[str, dict[str, str]] = {}
        collections: list[tuple[str, str]] = []
        self.series: list[tuple[str, str]] = []  # belongs-to-collection типа series: (название, group-position) по порядку
        series: list[list[str]] = []  # [название, номер] издательской серии; номер относится к последней серии
        for attrs_text, body in re.findall(r"<(?:\w+:)?meta\b([^>]*?)(?:/>|>(.*?)</(?:\w+:)?meta\s*>)", self.opf, re.S):
            attrs = parse_attrs(attrs_text)
            value, prop, name = strip_tags(body or ""), attrs.get("property", ""), attrs.get("name", "")
            if prop == "belongs-to-collection":
                collections.append((attrs.get("id", ""), value))
            elif attrs.get("refines"):
                refines.setdefault(attrs["refines"].lstrip("#"), {})[prop] = value
            elif prop == "dcterms:modified":
                pairs.append(("modified", value))
            elif name in ("calibre:rating", "calibre:timestamp", "calibre:title_sort"):
                pairs.append((name.split(":", 1)[1], attrs.get("content", "")))
            elif name == "books_library:publisher-series":
                series.append([attrs.get("content", ""), ""])
            elif name == "books_library:publisher-series-index" and series:
                series[-1][1] = attrs.get("content", "")
        for ident, name in collections:
            props = refines.get(ident, {})
            ctype = props.get("collection-type", "")
            pairs += [("belongs_to_collection", name), ("collection_type", ctype),
                      ("group_position", props.get("group-position", ""))]
            if ctype == "set" and name:
                sets.append(("set", name))
            if ctype == "series" and name:
                self.series.append((name, props.get("group-position", "")))
        pairs += [series_pair(name, position) for name, position in self.series[1:]]  # первая — books.series
        for name, number in series:  # издательская серия, записанная конвертером fb2_to_epub3 (как fb2_details)
            if name:
                pairs += [("publisher_series", name), ("publisher_series_no", number)]
                sets.append(("publisher-series", name))
        return pairs, sets


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


def read_book_meta(root: Path, rel: str, size: int = 0, mtime: float = 0.0,
                   cache: TextCache | None = None, detail: bool = False, orig: str = "") -> dict[str, Any]:
    """Метаданные одной книги; при любой ошибке разбора — данные по пути (книга остаётся в каталоге).
    detail (только scan) — дополнительно pdf, djvu, mobi/azw, docx/odt, txt и число слов из cache, если текст книги
    уже в кэше dedup (без извлечения). Остальные вызовы (dedup, веб) разбирают прежний набор форматов."""
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
        elif detail and meta["fmt"] in OTHER_DETAIL_FORMATS and not rel.lower().endswith(".zip"):
            found = other_meta(full, meta["fmt"], split_name(rel)[0])
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
        for key, value in found.pop("fill", {}).items():
            if value and not meta.get(key):
                meta[key] = value
        series_fields(found, rel, orig)
        if detail and cache is not None:
            text = cache.get(rel, size, mtime, meta["fmt"])
            if text:
                found.setdefault("kv", []).append(("words", count_words(text)))
        for key, value in found.items():
            if value not in ("", [], None):
                meta[key] = value
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
        meta["error"] = f"{type(error).__name__}: {error}"
    return meta


def iter_book_files(root: Path, errors: list[str], failed: set[str] | None = None) -> Iterator[tuple[str, int, float]]:
    """Относительный путь, размер и mtime каждого файла книги; HTML и служебные каталоги пропускаются.
    failed — относительные пути каталогов и файлов, которые не прочитались ("." — сам корень)."""
    def onerror(error: OSError) -> None:
        errors.append(f"{error.filename}: {error.strerror}")
        if failed is not None and error.filename:
            failed.add(os.path.relpath(error.filename, root).replace(os.sep, "/"))

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
                if failed is not None:
                    failed.add(os.path.relpath(full, root).replace(os.sep, "/"))
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
    # book_meta и сборники из метаданных (source='meta') заменяются целиком при каждом разборе книги.
    conn.execute("DELETE FROM book_meta WHERE path=?", (rel,))
    conn.executemany("INSERT OR IGNORE INTO book_meta VALUES (?,?,?)", meta_rows(rel, meta.get("kv", [])))
    conn.execute("DELETE FROM book_collections WHERE path=? AND source='meta'", (rel,))
    set_rows = [(rel, clean_value(name)[:300], rel, kind) for kind, name in meta.get("sets", []) if clean_value(name)]
    conn.executemany("INSERT OR IGNORE INTO book_collections(path, section, collection, source, orig_path, subsection, kind) "
                     "VALUES (?,'',?,'meta',?,'',?)", set_rows)
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


class ScanPlan(NamedTuple):
    """Расхождение каталога книг с базой: общее для scan и мониторинга."""
    files: int
    todo: list[tuple[str, int, float]]  # новые и изменённые
    new: int
    removed: list[str]  # исчезли с диска (книги нечитаемых каталогов сюда не входят)
    renamed: list[tuple[str, str]]  # старый и новый путь одной книги
    unreadable: int  # книги базы в нечитаемых каталогах: остаются в каталоге как есть
    folders: set[str]  # папки верхнего уровня, где сейчас есть книги
    errors: list[str]


def under_any(rel: str, prefixes: set[str]) -> bool:
    return "." in prefixes or any(rel == p or rel.startswith(p + "/") for p in prefixes)


def match_renames(removed: list[str], todo: list[tuple[str, int, float]],
                  known: dict[str, tuple[int, float]]) -> list[tuple[str, str]]:
    """Переименование или перенос вне движка (вручную, Syncthing): исчезнувший и новый путь — одна книга, если совпали
    размер и mtime, а когда mtime сменился при копировании или пара неоднозначна — размер и имя файла.
    Связываются только однозначные пары: одинаковые копии не склеиваются."""
    def unique(items: list[tuple[Any, str]]) -> dict[Any, str]:
        found: dict[Any, str] = {}
        doubles: set[Any] = set()
        for key, rel in items:
            if key in found or key in doubles:
                found.pop(key, None)
                doubles.add(key)
            else:
                found[key] = rel
        return found

    gone = list(removed)
    new = [(rel, size, mtime) for rel, size, mtime in todo if rel not in known]
    pairs: list[tuple[str, str]] = []
    for gone_key, new_key in ((lambda rel: known[rel], lambda size, mtime, _rel: (size, mtime)),
                              (lambda rel: (known[rel][0], posixpath.basename(rel)),
                               lambda size, _mtime, rel: (size, posixpath.basename(rel)))):
        olds = unique([(gone_key(rel), rel) for rel in gone])
        news = unique([(new_key(size, mtime, rel), rel) for rel, size, mtime in new])
        matched = [(olds[key], news[key]) for key in olds.keys() & news.keys()]
        pairs += matched
        used = {rel for pair in matched for rel in pair}
        gone = [rel for rel in gone if rel not in used]
        new = [item for item in new if item[0] not in used]
    return sorted(pairs)


def plan_scan(root: Path, known: dict[str, tuple[int, float]], reparse: bool = False) -> ScanPlan:
    errors: list[str] = []
    failed: set[str] = set()
    todo: list[tuple[str, int, float]] = []
    seen: set[str] = set()
    new = 0
    for rel, size, mtime in iter_book_files(root, errors, failed):
        seen.add(rel)
        old = known.get(rel)
        if reparse or old is None or old[0] != size or old[1] != mtime:
            todo.append((rel, size, mtime))
            new += old is None
    missing = [p for p in known if p not in seen]
    removed = [p for p in missing if not under_any(p, failed)] if failed else missing
    return ScanPlan(len(seen), todo, new, removed, match_renames(removed, todo, known), len(missing) - len(removed),
                    {rel.partition("/")[0] for rel in seen if "/" in rel}, errors)


def folder_renames(plan: ScanPlan, known: dict[str, tuple[int, float]]) -> dict[str, str]:
    """Папки верхнего уровня, переименованные целиком: каждая книга папки связана с книгой одной новой папки, которой
    в базе не было. Перенос части книг в существующую папку — не переименование автора."""
    def top(rel: str) -> str:
        return rel.partition("/")[0] if "/" in rel else ""

    counts = Counter(top(rel) for rel in known)
    targets: dict[str, set[str]] = {}
    for old, new in plan.renamed:
        targets.setdefault(top(old), set()).add(top(new))
    return {old: next(iter(news)) for old, news in targets.items()
            if old and len(news) == 1 and "" not in news and old not in plan.folders and not counts[next(iter(news))]
            and sum(top(o) == old for o, _ in plan.renamed) == counts[old]}


def move_variant_decisions(conn: sqlite3.Connection, folders: dict[str, str]) -> int:
    """Решения по вариантам авторов (author_variants, по именам папок) переходят на новое имя папки.
    Решённая пара не теряется из-за pending-пары с тем же именем; пара папки с самой собой больше не нужна."""
    moved = 0
    for old, new in folders.items():
        conn.execute("UPDATE author_variants SET target=? WHERE target=?", (new, old))
        for row in conn.execute("SELECT * FROM author_variants WHERE a=? OR b=?", (old, old)).fetchall():
            a, b = sorted(new if name == old else name for name in (row["a"], row["b"]))
            conn.execute("DELETE FROM author_variants WHERE a=? AND b=?", (row["a"], row["b"]))
            if a == b:
                continue
            conn.execute("DELETE FROM author_variants WHERE a=? AND b=? AND status='pending'", (a, b))
            conn.execute("INSERT OR IGNORE INTO author_variants(a, b, tier, status, target, updated) VALUES (?,?,?,?,?,?)",
                         (a, b, row["tier"], row["status"], row["target"], time.time()))
            moved += 1
    return moved


def scan(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print,
         reparse: bool = False) -> dict[str, int]:
    """Инкрементальное сканирование: новые и изменённые файлы разбираются, исчезнувшие удаляются из каталога.
    reparse — перечитать метаданные всех книг, как если бы у каждой изменился mtime."""
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
    plan = plan_scan(root, known, reparse)
    if known and not plan.files and not plan.unreadable:
        raise ConfigError(f"в каталоге книг {root} нет ни одной книги, а в базе их {len(known)}: диск не смонтирован "
                          "или неверный books_dir? Каталог не очищается")
    todo, removed, renamed, errors = plan.todo, plan.removed, plan.renamed, plan.errors
    folders = folder_renames(plan, known)
    stats = {"files": plan.files, "new": plan.new, "changed": len(todo) - plan.new, "removed": len(removed),
             "renamed": len(renamed), "renamed_folders": len(folders), "unreadable_kept": plan.unreadable,
             "walk_errors": len(errors)}
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    if plan.unreadable:
        say(f"книг в нечитаемых каталогах: {plan.unreadable} — остаются в каталоге до следующего scan", "walk_kept",
            logging.WARNING, detail=str(plan.unreadable))
    if dry_run:
        out(f"[dry-run] будет: каталог {root} — книг {stats['files']}, добавить {stats['new']}, "
              f"обновить {stats['changed']}, удалить из каталога {stats['removed']} (файлы книг не трогаются)")
        if not Path(db_path).is_file():
            out(f"[dry-run] будет: создать БД {db_path} (схема v{DB_LATEST})")
        for rel, *_ in todo[:15]:
            out(f"[dry-run] будет: {'обновить' if rel in known else 'добавить'} {rel}")
        for old, new_rel in renamed[:15]:
            out(f"[dry-run] будет: переименование {old} → {new_rel} (отметки, полки, правки, сборники — на новый путь)")
        for old, new_rel in list(folders.items())[:15]:
            out(f"[dry-run] будет: папка {old} → {new_rel} (решения по вариантам авторов — на новое имя)")
        if plan.unreadable:
            out(f"[dry-run] будет: оставить в каталоге {plan.unreadable} книг нечитаемых каталогов (ошибки обхода)")
        for rel in removed[:15]:
            out(f"[dry-run] будет: удалить из каталога {rel}")
        say("план сканирования", "dry_scan", detail=json.dumps(stats, ensure_ascii=False))
        return stats

    migrate_database(db_path, out=out)
    if renamed:
        with closing(connect(db_path)) as conn, conn:
            for old, new_rel in renamed:
                conn.execute("UPDATE OR IGNORE meta_overrides SET path=? WHERE path=?", (new_rel, old))
                conn.execute("INSERT INTO edit_log(path, new_path, fields, backup, created) VALUES (?,?,?,?,?)",
                             (old, new_rel, json.dumps({"rename": "scan"}), "", time.time()))
        relink(db_path, renamed)
        say(f"переименования вне движка: {len(renamed)}", "scan_renamed", detail=json.dumps(renamed[:50], ensure_ascii=False))
    if folders:
        with closing(connect(db_path)) as conn, conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= VARIANT_SCHEMA:
                moved = move_variant_decisions(conn, folders)
                say(f"переименованы папки: {len(folders)}, решений по вариантам перенесено: {moved}", "scan_renamed_folders",
                    detail=json.dumps(folders, ensure_ascii=False))
    overrides = load_overrides(db_path)
    started = last_report = time.monotonic()
    parse_errors = 0
    SCAN_STATE.update(stage="разбор книг", total=len(todo))
    text_cache = TextCache.open(cfg)
    with closing(connect(db_path)) as conn, (closing(text_cache.conn) if text_cache else nullcontext()):
        for start in range(0, len(removed), 500):
            chunk = removed[start:start + 500]
            marks = ",".join("?" * len(chunk))
            with conn:
                conn.execute(f"DELETE FROM books_fts WHERE rowid IN (SELECT id FROM books WHERE path IN ({marks}))", chunk)
                conn.execute(f"DELETE FROM books WHERE path IN ({marks})", chunk)
                conn.execute(f"DELETE FROM book_meta WHERE path IN ({marks})", chunk)
                conn.execute(f"DELETE FROM book_collections WHERE source='meta' AND path IN ({marks})", chunk)
        moved = dict(conn.execute("SELECT path, orig_path FROM book_collections WHERE path <> orig_path"))
        with ThreadPoolExecutor(max_workers=cfg["scan_workers"]) as pool:
            for start in range(0, len(todo), 500):
                batch = todo[start:start + 500]
                metas = list(pool.map(
                    lambda item: read_book_meta(root, item[0], item[1], item[2], text_cache, detail=True,
                                                orig=moved.get(item[0], "")), batch))
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
        # База пуста по решениям (восстановлена из каталога, а не прошла author-variants): решения из файлов.
        if conn.execute("SELECT count(*) FROM author_variants").fetchone()[0] == 0 and decision_files_exist(cfg):
            try:
                import_decisions(cfg, conn, out=out)
            except ConfigError as error:
                say(f"решения не загружены: {error}", "decisions_skipped", logging.WARNING, detail=str(error))
        conn.execute("PRAGMA optimize")
    stats["parse_errors"] = parse_errors
    say(f"сканирование завершено: {json.dumps(stats, ensure_ascii=False)}", "scan_done", detail=json.dumps(stats))
    return stats


# ======================================================================
# Мониторинг каталога книг (serve)
# ======================================================================

# RuntimeDirectory= юнита ночной переделки: каталог в $XDG_RUNTIME_DIR есть, пока конвейер работает.
REWORK_RUNTIME_DIR = "books-library-rework"
# Столько исчезнувших (не переименованных) книг — уже не ручная правка, а отвал диска или ветка, удалённая по ошибке:
# каталог сам не чистится, решает человек.
WATCH_MAX_REMOVED = 1000
WATCH_STATE: dict[str, Any] = {"interval": 0, "started": None, "last_check": None, "last_scan": None, "pending": 0,
                               "scans": 0, "errors": 0, "last_error": "", "paused": ""}
_WATCH_LOCK = threading.Lock()
_WATCH: list[tuple[threading.Thread, threading.Event]] = []  # не больше одного: поток и событие остановки


def rework_active() -> bool:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(runtime) and (Path(runtime) / REWORK_RUNTIME_DIR).is_dir()


def scan_lock_free() -> bool:
    try:
        with scan_lock():
            return True
    except ScanBusyError:
        return False


def watch_pending(cfg: dict[str, Any]) -> tuple[frozenset[tuple[str, int, float]], ScanPlan]:
    """Расхождение каталога с базой — то, что подхватит scan: новые и изменённые книги и исчезнувшие (size -1)."""
    with closing(connect(cfg["db_path"], readonly=True)) as conn:
        known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, size, mtime FROM books")}
    plan = plan_scan(Path(cfg["books_dir"]), known)
    return frozenset(plan.todo) | {(rel, -1, 0.0) for rel in plan.removed}, plan


def watch_pause(reason: str) -> None:
    if WATCH_STATE["paused"] != reason:
        say(f"мониторинг: пауза — {reason}", "watch_paused", detail=reason)
    WATCH_STATE.update(paused=reason, pending=0)


def watch_tick(cfg: dict[str, Any], previous: frozenset[tuple[str, int, float]]) -> frozenset[tuple[str, int, float]]:
    """Один проход. scan запускается, когда расхождение есть и не менялось с прошлого прохода: копирование закончилось,
    недописанные файлы не разбираются. Возвращает расхождение для следующего прохода."""
    if rework_active():
        watch_pause("идёт ночная переделка библиотеки")
        return frozenset()
    if not Path(cfg["books_dir"]).is_dir():
        watch_pause("каталог книг недоступен")
        return frozenset()
    if not Path(cfg["db_path"]).is_file():
        watch_pause("база не создана, нужен первый scan")
        return frozenset()
    diff, plan = watch_pending(cfg)
    if plan.errors:
        WATCH_STATE["last_error"] = f"ошибок обхода {len(plan.errors)}, книги там не удаляются: {plan.errors[0]}"
    lost = len(plan.removed) - len(plan.renamed)
    if lost > WATCH_MAX_REMOVED:
        watch_pause(f"с диска исчезло {lost} книг (порог {WATCH_MAX_REMOVED}): проверьте диск и books_dir, "
                    "затем «Пересканировать»")
        return frozenset()
    WATCH_STATE.update(last_check=datetime.now().isoformat(timespec="seconds"), pending=len(diff), paused="")
    if not diff or diff != previous:
        return diff
    with JOBS_LOCK:
        busy = any(j["command"] == "scan" and j["state"] in ("queued", "running") for j in JOBS.values())
    if busy or not scan_lock_free():
        return diff
    job = start_job(cfg, "scan", False, {})
    WATCH_STATE.update(last_scan=datetime.now().isoformat(timespec="seconds"), scans=WATCH_STATE["scans"] + 1)
    say(f"мониторинг: изменений {len(diff)}, scan — задание {job['id']}", "watch_scan", detail=json.dumps({"changes": len(diff)}))
    return diff


def watch_loop(cfg: dict[str, Any], stop: threading.Event) -> None:
    previous: frozenset[tuple[str, int, float]] = frozenset()
    while not stop.is_set():
        try:
            previous = watch_tick(cfg, previous)
        except (OSError, sqlite3.Error, ConfigError) as error:
            previous = frozenset()
            WATCH_STATE["errors"] += 1
            if WATCH_STATE["last_error"] != str(error):
                say(f"мониторинг: {error}", "watch_error", logging.WARNING, detail=str(error))
            WATCH_STATE["last_error"] = str(error)
        stop.wait(cfg["watch_interval"])


def start_watch(cfg: dict[str, Any]) -> bool:
    """Запускает поток мониторинга; False — уже работает."""
    if cfg["watch_interval"] == 0:
        raise ConfigError("мониторинг выключен в настройках: watch_interval = 0")
    with _WATCH_LOCK:
        if _WATCH and _WATCH[0][0].is_alive():
            return False
        stop = threading.Event()
        thread = threading.Thread(target=watch_loop, args=(cfg, stop), name="watch", daemon=True)
        WATCH_STATE.update(interval=cfg["watch_interval"], started=datetime.now().isoformat(timespec="seconds"),
                           last_check=None, pending=0, errors=0, last_error="", paused="")
        _WATCH[:] = [(thread, stop)]
        thread.start()
    say(f"мониторинг каталога книг: опрос каждые {cfg['watch_interval']} с", "watch_start")
    return True


def stop_watch(timeout: float | None = None) -> None:
    """Останавливает мониторинг и ждёт конца текущего прохода (под блокировкой: start не создаст второй поток)."""
    with _WATCH_LOCK:
        if not _WATCH or not _WATCH[0][0].is_alive():
            return
        thread, stop = _WATCH[0]
        stop.set()
        thread.join(timeout)
    say("мониторинг каталога книг остановлен", "watch_stop")


def watch_action(cfg: dict[str, Any], action: object) -> str:
    """Кнопка интерфейса и POST /api/watch: start, stop или status; до перезапуска serve."""
    if action == "start":
        return "мониторинг запущен" if start_watch(cfg) else "мониторинг уже работает"
    if action == "stop":
        stop_watch()
        return "мониторинг остановлен"
    if action == "status":
        return "состояние мониторинга"
    raise ValueError("action: start, stop или status")


def watch_status() -> dict[str, Any]:
    running = bool(_WATCH) and _WATCH[0][0].is_alive() and not _WATCH[0][1].is_set()
    return {"running": running, **WATCH_STATE}


# ======================================================================
# Решения пользователя: выгрузка и загрузка (переживают удаление базы)
# ======================================================================

PUBLIC_DECISIONS_DIR = PROJECT_DIR / "decisions"  # в репозитории: только решения author_variants
PUBLIC_VARIANTS_PATH = PUBLIC_DECISIONS_DIR / "author_variants.json"
DECISIONS_VERSION = 1  # версия формата personal.json
PERSONAL_FILE = "personal.json"  # вне репозитория (decisions_dir): отметки, полки, правки метаданных, решённые дубли
PERSONAL_LISTS = ("marks", "shelves", "meta_overrides", "dup_groups")
VARIANT_DECISIONS = ("different", "merged")


def personal_decisions_path(cfg: dict[str, Any]) -> Path:
    return Path(cfg["decisions_dir"]) / PERSONAL_FILE


def decision_files_exist(cfg: dict[str, Any]) -> bool:
    return PUBLIC_VARIANTS_PATH.is_file() or personal_decisions_path(cfg).is_file()


def book_identity(authors: str, title: str) -> str:
    """Ключ книги для решений: первый автор (author_key — набор слов) и название (norm_key). Путь в ключ не входит:
    после восстановления каталога пути книг другие."""
    first = authors.split("; ")[0] if authors else ""
    author = author_key(first)
    return f"{' '.join(sorted(author)) if author else ''}|{norm_key(title)}"


def book_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """Путь книги → ключ книги по текущей базе."""
    return {row["path"]: book_identity(row["authors"], row["title"])
            for row in conn.execute("SELECT path, authors, title FROM books")}


def book_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Ключ книги → пути книг с этим ключом (одна книга в нескольких форматах — несколько путей)."""
    index: dict[str, list[str]] = {}
    for path, key in book_keys(conn).items():
        index.setdefault(key, []).append(path)
    return index


def write_private_json(path: Path, data: Any, mode: int) -> None:
    """JSON атомарно: временный файл рядом создаётся с правами mode до записи, затем os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def load_decision_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"не читается {path}: {error}") from error


def load_variants(path: Path) -> list[dict[str, str]]:
    """Публичный файл: список {a, b, status, target}; пары проверяются, битый файл — ошибка, а не частичная загрузка."""
    data = load_decision_file(path)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: ожидается список решений")
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("a"), str) or not isinstance(item.get("b"), str) \
                or not item["a"] or not item["b"] or item["a"] == item["b"] \
                or item.get("status") not in VARIANT_DECISIONS or not isinstance(item.get("target", ""), str):
            raise ConfigError(f"{path}: запись решения не разобрана: {item!r}"[:300])
    return data


def load_personal(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Личный файл: списки объектов по PERSONAL_LISTS; полки проверяются до загрузки, частичной загрузки не будет."""
    data = load_decision_file(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: ожидается JSON-объект")
    result: dict[str, list[dict[str, Any]]] = {}
    for name in PERSONAL_LISTS:
        items = data.get(name, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ConfigError(f"{path}: {name} — список объектов")
        result[name] = items
    for shelf in result["shelves"]:
        if not isinstance(shelf.get("name"), str) or not isinstance(shelf.get("items", []), list) \
                or not all(isinstance(item, dict) for item in shelf.get("items", [])):
            raise ConfigError(f"{path}: полка не разобрана: {shelf.get('name')!r}")
    return result


def keep_missing(previous: list[dict[str, Any]], exported: list[dict[str, Any]],
                 identity: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    """Записи прежнего файла, которых нет в выгрузке, сохраняются: решение не теряется, если его книги сейчас нет в базе."""
    current = {identity(item) for item in exported}
    return [item for item in previous if identity(item) not in current]


def export_decisions(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, int]:
    """Выгрузка решений, только чтение базы (без scan_lock): author_variants с решениями different/merged —
    books_library/decisions/author_variants.json (в репозиторий); отметки, полки, правки метаданных и решённые
    группы дублей — personal.json в decisions_dir (вне репозитория, файл 0600, каталог 0700). Ключ книги — по
    book_identity; путь сохраняется для справки. Записи прежнего файла, которых сейчас нет в базе, сохраняются."""
    db_path = cfg["db_path"]
    if not Path(db_path).is_file():
        raise ConfigError(f"база не найдена: {db_path} (сначала scan)")
    personal_path = personal_decisions_path(cfg)
    root = Path(cfg["books_dir"])
    with closing(connect(db_path, readonly=True)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < VARIANT_SCHEMA:
            raise ConfigError(f"схема базы старше v{VARIANT_SCHEMA}: сначала scan")
        keys = book_keys(conn)
        db_keys = set(keys.values())
        decided = ",".join("?" * len(VARIANT_DECISIONS))
        variants = [dict(row) for row in conn.execute(
            f"SELECT a, b, status, target FROM author_variants WHERE status IN ({decided}) ORDER BY a, b", VARIANT_DECISIONS)]
        marks = [{"key": keys.get(row["path"], ""), **dict(row)} for row in conn.execute("SELECT * FROM marks ORDER BY path")]
        shelves: dict[str, list[dict[str, str]]] = {}
        for row in conn.execute("SELECT s.name, i.path FROM shelves s LEFT JOIN shelf_items i ON i.shelf_id = s.id "
                                "ORDER BY s.name, i.path"):
            items = shelves.setdefault(row["name"], [])
            if row["path"] is not None:
                items.append({"key": keys.get(row["path"], ""), "path": row["path"]})
        overrides = []
        for row in conn.execute("SELECT * FROM meta_overrides ORDER BY path"):
            parsed = read_book_meta(root, row["path"]) if row["path"] in keys else {}
            parsed_key = book_identity("; ".join(parsed.get("authors", [])), parsed.get("title", "")) if parsed else ""
            overrides.append({"key": keys.get(row["path"], ""), "parsed_key": parsed_key,
                              **{name: row[name] for name in ("path", *EDIT_KEYS, "updated")}})
        groups = []
        for group in conn.execute("SELECT id, key FROM dup_groups WHERE status = 'resolved' ORDER BY key"):
            members = [{"key": keys.get(row["path"], ""), "path": row["path"]} for row in conn.execute(
                "SELECT path FROM dup_members WHERE group_id=? ORDER BY path", (group["id"],))]
            groups.append({"group_key": group["key"], "status": "resolved", "members": members})

    previous = load_personal(personal_path) if personal_path.is_file() else {name: [] for name in PERSONAL_LISTS}
    previous_variants = load_variants(PUBLIC_VARIANTS_PATH) if PUBLIC_VARIANTS_PATH.is_file() else []
    variants += keep_missing(previous_variants, variants, lambda item: (item["a"], item["b"]))
    variants.sort(key=lambda item: (item["a"], item["b"]))
    marks += [item for item in previous["marks"] if item.get("key") not in db_keys]
    overrides += [item for item in previous["meta_overrides"] if item.get("key") not in db_keys]
    for item in previous["shelves"]:
        kept = [entry for entry in item.get("items", []) if entry.get("key") not in db_keys]
        if kept:
            shelves.setdefault(item["name"], []).extend(kept)
    groups += [item for item in previous["dup_groups"]
               if not any(member.get("key") in db_keys for member in item.get("members", []))]
    shelf_list = [{"name": name, "items": items} for name, items in shelves.items()]
    personal = {"version": DECISIONS_VERSION, "exported": datetime.now().isoformat(timespec="seconds"),
                "marks": marks, "shelves": shelf_list, "meta_overrides": overrides, "dup_groups": groups}

    stats = {"variants": len(variants), "marks": len(marks), "shelves": len(shelf_list),
             "meta_overrides": len(overrides), "dup_groups": len(groups)}
    summary = (f"author_variants {stats['variants']} → {PUBLIC_VARIANTS_PATH}; отметки {stats['marks']}, полки "
               f"{stats['shelves']}, правки {stats['meta_overrides']}, решённые дубли {stats['dup_groups']} → {personal_path}")
    if dry_run:
        out(f"[dry-run] будет: выгрузить решения: {summary}; файлы не записаны")
        return stats
    decisions_dir = Path(cfg["decisions_dir"])
    decisions_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(decisions_dir, 0o700)
    write_private_json(personal_path, personal, 0o600)
    write_private_json(PUBLIC_VARIANTS_PATH, variants, 0o644)
    say(f"решения выгружены: {summary}", "decisions_export", detail=json.dumps(stats, ensure_ascii=False))
    out(f"выгружены решения: {summary}")
    return stats


def import_variants(conn: sqlite3.Connection, items: list[dict[str, str]], run: NightRun, dry_run: bool) -> None:
    """Пары решений: отсутствующие создаются со статусом из файла; решённые в базе не перетираются (только pending)."""
    for item in items:
        a, b = sorted((item["a"], item["b"]))
        row = conn.execute("SELECT status FROM author_variants WHERE a=? AND b=?", (a, b)).fetchone()
        if row is not None and row["status"] != "pending":
            continue
        if not dry_run:
            conn.execute(
                "INSERT INTO author_variants(a, b, tier, status, target, updated) VALUES (?,?,'manual',?,?,?) "
                "ON CONFLICT(a, b) DO UPDATE SET status=excluded.status, target=excluded.target, updated=excluded.updated "
                "WHERE author_variants.status='pending'",
                (a, b, item["status"], item.get("target", ""), time.time()))
        run.record("imported", f"{a} | {b}", kind="variant", status=item["status"])


def store_override(conn: sqlite3.Connection, path: str, item: dict[str, Any]) -> None:
    """Правка из файла — в meta_overrides; существующая запись не перезаписывается."""
    conn.execute(
        "INSERT OR IGNORE INTO meta_overrides(path, title, authors, series, series_no, genres, year, lang, annotation, updated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, item.get("title"), item.get("authors"), item.get("series"), item.get("series_no"), item.get("genres"),
         item.get("year"), item.get("lang"), item.get("annotation"), item.get("updated") or time.time()))


def apply_stored_override(cfg: dict[str, Any], conn: sqlite3.Connection, path: str) -> None:
    """Правка из meta_overrides — поверх разбора книги сразу: scan уже разобрал книги до загрузки решений."""
    row = conn.execute("SELECT * FROM meta_overrides WHERE path=?", (path,)).fetchone()
    root = Path(cfg["books_dir"])
    stat = (root / path).stat()
    meta = read_book_meta(root, path, stat.st_size, stat.st_mtime, detail=True)
    apply_override(meta, row)
    upsert_book(conn, path, stat.st_size, stat.st_mtime, meta)


def import_overrides(cfg: dict[str, Any], conn: sqlite3.Connection, items: list[dict[str, Any]],
                     index: dict[str, list[str]], run: NightRun, dry_run: bool) -> None:
    """Правки сопоставляются по разбору книги (parsed_key — до правки), затем применяются к книге."""
    for item in items:
        paths = index.get(str(item.get("parsed_key") or ""), [])
        if not paths:
            run.record("unmatched", str(item.get("path", "")), kind="meta_override", key=str(item.get("parsed_key") or ""))
            continue
        for path in paths:
            if not dry_run:
                store_override(conn, path, item)
                apply_stored_override(cfg, conn, path)
            run.record("imported", path, kind="meta_override")


def import_marks(conn: sqlite3.Connection, items: list[dict[str, Any]], index: dict[str, list[str]],
                 run: NightRun, dry_run: bool) -> None:
    for item in items:
        paths = index.get(str(item.get("key") or ""), [])
        if not paths:
            run.record("unmatched", str(item.get("path", "")), kind="mark", key=str(item.get("key") or ""))
            continue
        for path in paths:
            if not dry_run:
                conn.execute(
                    "INSERT INTO marks(path, status, rating, favorite, note, progress, opened, updated) VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(path) DO NOTHING",
                    (path, item.get("status", ""), item.get("rating", 0), item.get("favorite", 0), item.get("note", ""),
                     item.get("progress", 0), item.get("opened"), item.get("updated") or time.time()))
            run.record("imported", path, kind="mark")


def import_shelves(conn: sqlite3.Connection, shelves: list[dict[str, Any]], index: dict[str, list[str]],
                   run: NightRun, dry_run: bool) -> None:
    """Полка создаётся по имени (как и раньше — имя уникально); книги полки сопоставляются по ключу."""
    for shelf in shelves:
        name = shelf["name"]
        shelf_id = None
        if not dry_run:
            conn.execute("INSERT OR IGNORE INTO shelves(name) VALUES (?)", (name,))
            shelf_id = conn.execute("SELECT id FROM shelves WHERE name=?", (name,)).fetchone()[0]
        for item in shelf.get("items", []):
            paths = index.get(str(item.get("key") or ""), [])
            if not paths:
                run.record("unmatched", str(item.get("path", "")), kind="shelf", shelf=name, key=str(item.get("key") or ""))
                continue
            for path in paths:
                if shelf_id is not None:
                    conn.execute("INSERT OR IGNORE INTO shelf_items(shelf_id, path) VALUES (?,?)", (shelf_id, path))
                run.record("imported", path, kind="shelf", shelf=name)


def import_dup_groups(conn: sqlite3.Connection, groups: list[dict[str, Any]], index: dict[str, list[str]],
                      run: NightRun, dry_run: bool) -> None:
    """Решение по группе дублей — только для открытой группы (similar), в которой есть книги из файла."""
    for group in groups:
        ref = str(group.get("group_key", ""))
        paths = [path for member in group.get("members", []) for path in index.get(str(member.get("key") or ""), [])]
        if not paths:
            run.record("unmatched", ref, kind="dup_group")
            continue
        slots = ",".join("?" * len(paths))
        ids = [row[0] for row in conn.execute(
            f"SELECT DISTINCT g.id FROM dup_groups g JOIN dup_members m ON m.group_id = g.id "
            f"WHERE g.status = 'similar' AND m.path IN ({slots})", paths)]
        if not ids:
            run.record("unmatched", ref, kind="dup_group", reason="открытой группы нет")
            continue
        if not dry_run:
            conn.execute(f"UPDATE dup_groups SET status='resolved' WHERE id IN ({','.join('?' * len(ids))})", ids)
        run.record("imported", ref, kind="dup_group")


def import_decisions(cfg: dict[str, Any], conn: sqlite3.Connection, dry_run: bool = False,
                     out: Callable[[str], None] = print) -> dict[str, Any]:
    """Загрузка решений в базу: вызывается в конце scan, если author_variants пуста и есть файлы решений, и режимом
    import-decisions. Повтор безопасен: существующие записи не дублируются и не перезаписываются. Несопоставленное —
    в журнал (action unmatched). Порядок: пары → правки метаданных (книга разбирается с правкой) → отметки, полки, дубли."""
    variants = load_variants(PUBLIC_VARIANTS_PATH) if PUBLIC_VARIANTS_PATH.is_file() else []
    personal_path = personal_decisions_path(cfg)
    personal = load_personal(personal_path) if personal_path.is_file() else {name: [] for name in PERSONAL_LISTS}
    if not variants and not any(personal.values()):
        return {}  # решений нет: журнал и отчёт не создаются
    run = NightRun(cfg, "import-decisions", dry_run, None, out)
    try:
        with conn:
            import_variants(conn, variants, run, dry_run)
            import_overrides(cfg, conn, personal["meta_overrides"], book_index(conn), run, dry_run)
            index = book_index(conn)  # после правок ключи книг — уже с правками
            import_marks(conn, personal["marks"], index, run, dry_run)
            import_shelves(conn, personal["shelves"], index, run, dry_run)
            import_dup_groups(conn, personal["dup_groups"], index, run, dry_run)
    except BaseException:
        run.journal.close()
        raise
    return run.finish({"variants_file": str(PUBLIC_VARIANTS_PATH), "personal_file": str(personal_path)})


def import_decisions_command(cfg: dict[str, Any], dry_run: bool, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Режим import-decisions: загрузка решений независимо от содержимого базы (scan делает это сам при пустой author_variants)."""
    if not Path(cfg["db_path"]).is_file():
        raise ConfigError(f"база не найдена: {cfg['db_path']} (сначала scan)")
    if not dry_run:
        migrate_database(cfg["db_path"], out=out)
    with closing(connect(cfg["db_path"], readonly=dry_run)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < VARIANT_SCHEMA:
            raise ConfigError(f"схема базы старше v{VARIANT_SCHEMA}: сначала scan или migrate без -n")
        return import_decisions(cfg, conn, dry_run=dry_run, out=out)


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


def variant_values_at(cfg: dict[str, Any], full: Path | None) -> dict[str, str]:
    """Уточнения книги по её пути в базе (как у name-variants); без базы, без пути или вне каталога книг — пусто."""
    if full is None or not Path(cfg["db_path"]).is_file():
        return {}
    try:
        rel = full.relative_to(Path(cfg["books_dir"])).as_posix()
    except ValueError:
        return {}
    conn = connect(cfg["db_path"], readonly=True)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        return name_variant_values(conn, rel, version >= SCHEMA_META, version >= SCHEMA_COLLECTIONS)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def qualified_name(base: str, text: str, suffix: str) -> str:
    """«основа (уточнение)расширение»; основа укорачивается, пока имя не проходит правила Windows; пусто — не вышло."""
    stem = base
    while stem:
        candidate = f"{stem} ({text}){suffix}"
        if not windows_name_error(candidate):
            return candidate
        stem = stem[:-1]
    return ""


def free_variant_name(cfg: dict[str, Any], directory: Path, name: str, taken: set[Path], *, src: Path | None = None,
                      leaving: frozenset[Path] | set[Path] = frozenset()) -> Path:
    """Свободное имя в каталоге книг без «_N». Занятое имя — уточнение в скобках, каким src отличается от занявшего
    файла (пер. Фамилия, язык, источник, год — как у name-variants), иначе «(вариант N)». Файл из leaving (уходит в
    карантин) занятым не считается. Имя резервируется в taken. Ошибка — только если не найти имя за 999 попыток."""

    def free(candidate: Path) -> bool:
        return (not candidate.exists() or candidate in leaving) and candidate not in taken

    def take(candidate: Path) -> Path:
        taken.add(candidate)
        return candidate

    first = directory / name
    if free(first):
        return take(first)
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, dot + ext) if stem else (name, "")
    mine, other = variant_values_at(cfg, src), variant_values_at(cfg, first)
    for kind in NAME_VARIANT_KINDS:
        if mine.get(kind) and mine[kind] != other.get(kind):
            candidate = qualified_name(base, mine[kind], suffix)
            if candidate and free(directory / candidate):
                return take(directory / candidate)
    for number in range(1, 1000):
        candidate = qualified_name(base, f"вариант {number}", suffix)
        if candidate and free(directory / candidate):
            return take(directory / candidate)
    raise OSError(f"нет свободного имени рядом с «{name}»")


UNPACK_PROGRESS_SECONDS = 30
UNPACK_CHUNK = 1024 * 1024
MULTI_MIN_MEMBERS = 2
MD5_LINE = re.compile(r"^(.+?)\t+:\s*([0-9a-fA-F]{32})\s*$")
FB2_NUMBER_NAME = re.compile(r"[0-9]+\.fb2", re.IGNORECASE)
UNPACK_ERRORS = (OSError, ValueError, zipfile.BadZipFile, zlib.error, EOFError)


class UnpackProgress:
    """Прогресс распаковки: файлов сделано из всех, скорость и ETA — не чаще раза в UNPACK_PROGRESS_SECONDS."""

    def __init__(self, total: int) -> None:
        self.total = max(total, 1)
        self.done = 0
        self.started = self.last = time.monotonic()

    def advance(self, count: int = 1) -> None:
        self.done += count
        now = time.monotonic()
        if now - self.last < UNPACK_PROGRESS_SECONDS and self.done < self.total:
            return
        self.last = now
        rate = self.done / max(now - self.started, 1e-6)
        eta = (self.total - self.done) / rate / 60 if rate > 0 else 0.0
        say(f"unpack-fb2zip: файлов {self.done}/{self.total} ({self.done * 100 // self.total} %), "
            f"{rate:.1f} файлов/с, осталось ~{eta:.1f} мин", "progress")


def unpack_out_dir(cfg: dict[str, Any], out_dir: str, dry_run: bool) -> Path:
    """Каталог --out для --multi: абсолютный, не внутри каталога книг и карантина; создаётся с правами 0700 (не в dry-run)."""
    candidate = Path(out_dir).expanduser()
    if not candidate.is_absolute():
        raise ConfigError("--out: нужен абсолютный путь")
    resolved = candidate.resolve()
    if resolved in (Path("/"), Path.home().resolve()):
        raise ConfigError(f"--out: корневой или домашний каталог не подходит: {resolved}")
    for forbidden, what in ((Path(cfg["books_dir"]).resolve(), "каталога книг"),
                            (Path(cfg["quarantine_dir"]).resolve(), "карантина")):
        if resolved.is_relative_to(forbidden):
            raise ConfigError(f"--out не может быть внутри {what}: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise ConfigError(f"--out не каталог: {resolved}")
    if not dry_run:
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    return resolved


def file_md5(path: Path) -> str:
    """MD5 потоком (контроль целостности при копировании, не защита от подделки)."""
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(UNPACK_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_md5_list(directory: Path, cache: dict[Path, dict[str, str]]) -> dict[str, str]:
    """Строки «<имя архива>\\t\\t: <md5>» из md5_sum.txt каталога; кэш на каталог. Нет файла — пусто."""
    if directory not in cache:
        sums: dict[str, str] = {}
        listing = directory / "md5_sum.txt"
        if listing.is_file():
            for line in listing.read_text(encoding="utf-8", errors="replace").splitlines():
                found = MD5_LINE.match(line)
                if found:
                    sums[found.group(1).strip()] = found.group(2).lower()
        cache[directory] = sums
    return cache[directory]


def member_base(raw: str) -> str | None:
    """Имя записи без пути. None — путь, «..», скрытое или пустое имя: такая запись отбрасывается."""
    normalized = raw.replace("\\", "/")
    base = PurePosixPath(normalized).name
    if normalized != base or not base or base.startswith("."):
        return None
    return base


def member_problem(info: zipfile.ZipInfo, limit: int, max_mb: int) -> str | None:
    """Причина отказа от записи по размеру и степени сжатия (zip-бомба) или None."""
    if info.file_size > limit:
        return f"больше {max_mb} МБ"
    if info.file_size > 1024 * 1024 and info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
        return "подозрительная степень сжатия (zip-бомба?)"
    return None


def multi_name(base: str, head: bytes) -> str | None:
    """Имя файла: при имени-номере librusec («24.fb2») — «Фамилия - Название.fb2» по метаданным, без них —
    «librusec-24.fb2»; иное имя — очищенное исходное. None — имя пустое после очистки."""
    stem = base[:-4]
    if FB2_NUMBER_NAME.fullmatch(base):
        meta = fb2_meta(head)
        authors = meta.get("authors") or []
        title = str(meta.get("title") or "").strip()
        if authors and title:
            # fb2_meta пишет фамилию первой («Петров Иван»), поэтому берём первое слово.
            return lib_ru_name(authors[0].split(" ", 1)[0], title, "fb2")
        return f"librusec-{stem}.fb2"
    clean = folder_name(stem)
    return f"{clean}.fb2" if clean else None


def store_member(run: NightRun, archive: zipfile.ZipFile, archive_path: Path, info: zipfile.ZipInfo, base: str,
                 dest: Path, limit: int, taken: set[Path]) -> Path:
    """Одна запись многокнижного zip: потоковая запись во временный файл в dest (CRC проверяет zipfile при чтении
    до конца), затем атомарная замена на итоговое имя. В dry-run — только план."""
    if run.dry_run:
        with archive.open(info) as handle:
            head = handle.read(FB2_HEAD_BYTES)
        name = multi_name(base, head)
        if name is None:
            raise ValueError(f"{info.filename}: пустое имя после очистки")
        target = free_variant_name(run.cfg, dest, name, taken, src=archive_path)
        run.record("planned", archive_path, member=info.filename, to=str(target))
        return target
    fd, tmp_name = tempfile.mkstemp(prefix=".unpack-multi.", suffix=".tmp", dir=dest)
    tmp = Path(tmp_name)
    try:
        written = 0
        with os.fdopen(fd, "wb") as sink, archive.open(info) as source:
            while chunk := source.read(UNPACK_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"{info.filename}: фактический размер больше предела")
                sink.write(chunk)
        if written == 0:
            raise ValueError(f"{info.filename}: пустая запись")
        with tmp.open("rb") as check:
            head = check.read(FB2_HEAD_BYTES)
        name = multi_name(base, head)
        if name is None:
            raise ValueError(f"{info.filename}: пустое имя после очистки")
        target = free_variant_name(run.cfg, dest, name, taken, src=archive_path)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    run.record("extracted", archive_path, member=info.filename, to=str(target))
    return target


def unpack_multi(run: NightRun, archive_path: Path, members: list[zipfile.ZipInfo], *, limit: int, max_mb: int,
                 ignore_md5: bool, delete_source: bool, base_dir: Path, sums: dict[Path, dict[str, str]],
                 progress: UnpackProgress) -> None:
    """Многокнижный zip: записи в папку «<архив без .zip>/» в base_dir. Сначала сверка md5 из md5_sum.txt (если строка
    есть), затем распаковка. Архив переносится в карантин replaced/ только после полной распаковки и с delete_source."""
    expected = read_md5_list(archive_path.parent, sums).get(archive_path.name)
    if expected is None:
        run.count("md5_unlisted")
        say(f"нет строки md5 для {archive_path.name} в md5_sum.txt", "md5_unlisted", logging.WARNING, detail=str(archive_path))
    else:
        actual = file_md5(archive_path)
        if actual != expected:
            reason = f"ожидался {expected}, факт {actual}"
            if not ignore_md5:
                run.record("md5_mismatch", archive_path, reason=reason)
                say(f"md5 не совпал, архив пропущен: {archive_path} ({reason})", "md5_mismatch", logging.ERROR,
                    detail=reason)
                progress.advance(len(members))
                return
            run.record("md5_ignored", archive_path, reason=reason)
            say(f"md5 не совпал, распаковка по --ignore-md5: {archive_path} ({reason})", "md5_ignored",
                logging.WARNING, detail=reason)
    stem = folder_name(archive_path.name[:-4])
    if not stem:
        run.record("skipped", archive_path, reason="пустое имя папки")
        progress.advance(len(members))
        return
    dest = base_dir / stem
    if dest.exists():
        run.record("skipped", archive_path, reason=f"папка уже есть, прежняя распаковка: {dest}")
        progress.advance(len(members))
        return
    if not run.dry_run:
        dest.mkdir()
    taken: set[Path] = set()
    extracted = failed = skipped = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in members:
            progress.advance()
            base = member_base(info.filename)
            problem = "путь, «..» или скрытое имя" if base is None else member_problem(info, limit, max_mb)
            if base is None or problem is not None:
                skipped += 1
                run.record("skipped", archive_path, member=info.filename, reason=problem or "")
                continue
            try:
                store_member(run, archive, archive_path, info, base, dest, limit, taken)
            except UNPACK_ERRORS as error:
                failed += 1
                run.record("failed", archive_path, member=info.filename, reason=str(error))
            else:
                extracted += 1
    if not run.dry_run and extracted == 0 and not any(dest.iterdir()):
        dest.rmdir()
    if failed or skipped or extracted != len(members):
        say(f"архив {archive_path.name} остаётся на месте: распаковано {extracted} из {len(members)}, "
            f"ошибок {failed}, пропущено {skipped}", "archive_kept", logging.WARNING, detail=str(archive_path))
        return
    run.count("complete")
    if delete_source:
        run.quarantine(archive_path, "replaced", dest_dir=str(dest), extracted=extracted)


def unpack_single(run: NightRun, archive_path: Path, members: list[zipfile.ZipInfo], *, limit: int, max_mb: int,
                  taken: set[Path], progress: UnpackProgress) -> None:
    """Однокнижный *.fb2.zip, как раньше: запись рядом с архивом под исходным именем, конфликт — уточнение в скобках (free_variant_name)"""
    parent = archive_path.parent
    if not members:
        say(f"в архиве нет .fb2: {archive_path}", "no_fb2_inside", logging.WARNING, detail=str(archive_path))
    with zipfile.ZipFile(archive_path) as archive:
        for info in members:
            progress.advance()
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            problem = "скрытое или пустое имя" if not name or name.startswith(".") else member_problem(info, limit, max_mb)
            if problem is not None:
                run.record("skipped", archive_path, member=info.filename, reason=problem)
                continue
            target_file = free_variant_name(run.cfg, parent, name, taken, src=archive_path)
            if run.dry_run:
                run.record("planned", archive_path, member=info.filename, to=str(target_file))
                continue
            with archive.open(info) as handle:
                data = handle.read(limit + 1)
            if not data or len(data) > limit:
                raise ValueError(f"{info.filename}: пустая или слишком большая запись")
            fd, tmp_name = tempfile.mkstemp(prefix=".unpack-fb2zip.", dir=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.chmod(tmp_name, archive_path.stat().st_mode & 0o666)
                if target_file.exists():  # появился после планирования — новое свободное имя
                    target_file = free_variant_name(run.cfg, parent, name, taken, src=archive_path)
                os.replace(tmp_name, target_file)
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            run.record("extracted", archive_path, member=info.filename, to=str(target_file))


def scan_archives(run: NightRun, paths: list[Path], multi: bool,
                  errors: list[str]) -> list[tuple[Path, bool, list[zipfile.ZipInfo]]]:
    """Архивы для распаковки: (путь, многокнижный, записи .fb2). Многокнижный — с двумя и более .fb2 (--multi);
    *.fb2.zip без --multi или с одной записью — однокнижный; прочие zip пропускаются."""
    jobs: list[tuple[Path, bool, list[zipfile.ZipInfo]]] = []
    for archive_path in paths:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".fb2")]
        except (OSError, zipfile.BadZipFile, EOFError) as error:
            if archive_path.name.lower().endswith(".fb2.zip"):
                run.record("failed", archive_path, reason=str(error))
            else:
                run.count("not_multi")
            continue
        if multi and len(members) >= MULTI_MIN_MEMBERS:
            jobs.append((archive_path, True, members))
        elif archive_path.name.lower().endswith(".fb2.zip"):
            jobs.append((archive_path, False, members))
        else:
            run.count("not_multi")
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    return jobs


def unpack_fb2zip(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, max_mb: int = 200,
                  out: Callable[[str], None] = print, *, multi: bool = False, out_dir: str | None = None,
                  delete_source: bool = False, ignore_md5: bool = False) -> dict[str, Any]:
    """Извлекает .fb2 из *.fb2.zip рядом с архивом (без внутренних путей), архив остаётся на месте.

    --multi: многокнижные .zip (два и более .fb2) — записи в папку «<архив без .zip>/» рядом с архивом или в --out;
    имена по метаданным fb2, md5 из md5_sum.txt рядом (несовпадение — архив пропускается, если не --ignore-md5),
    архив в карантин replaced/ только с --delete-source и после полной распаковки. Журнал — NightRun (reports/).
    """
    if not multi and (out_dir or delete_source or ignore_md5):
        raise ConfigError("--out, --delete-source и --ignore-md5 применяются только с --multi")
    base_override = unpack_out_dir(cfg, out_dir, dry_run) if out_dir else None
    run = NightRun(cfg, "unpack-fb2zip", dry_run, path, out)
    limit = max_mb * 1024 * 1024
    errors: list[str] = []
    paths = [p for p, _size in walk_files(run.target, (".zip",) if multi else (".fb2.zip",), errors)]
    jobs = scan_archives(run, paths, multi, errors)
    progress = UnpackProgress(sum(len(members) for _p, _m, members in jobs))
    taken: set[Path] = set()
    sums: dict[Path, dict[str, str]] = {}
    for archive_path, is_multi, members in jobs:
        run.count("archives")
        try:
            if is_multi:
                unpack_multi(run, archive_path, members, limit=limit, max_mb=max_mb, ignore_md5=ignore_md5,
                             delete_source=delete_source, base_dir=base_override or archive_path.parent,
                             sums=sums, progress=progress)
            else:
                unpack_single(run, archive_path, members, limit=limit, max_mb=max_mb, taken=taken, progress=progress)
        except UNPACK_ERRORS as error:
            run.record("failed", archive_path, reason=str(error))
    return run.finish({"walk_errors": len(errors), "multi": multi, "out": str(base_override or "")})


# ----------------------------------------------------------------------
# Конвертация DOC/DOCX/RTF/ODT (замена doc2fb2.sh)
# ----------------------------------------------------------------------

CONVERT_SOURCES = ("doc", "docx", "rtf", "odt", "mht", "mhtml")
# HTML библиотеки Мошкова: только по явному --only html (в «all» не входит — ночной прогон их не трогает).
HTML_SOURCES = ("html", "htm")
HTML_INDEX = re.compile(r"^index.*\.html?$", re.I)
HTML_MIN_BYTES = 5000


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


# Библиотека Мошкова (зеркало lib.ru): текст книги в <pre>, абзац начинается со строки с отступом ≥ 3 пробела.
LIB_RU_PRE = re.compile(r"<pre\b[^>]*>(.*?)(?:</pre\s*>|\Z)", re.S | re.I)
LIB_RU_TAG = re.compile(r"<[^>]*>")
LIB_RU_INLINE = re.compile(r"<[^<>]*>")
LIB_RU_BLOCK = re.compile(r"<\s*(?:h[1-6]|hr|dir|img)\b|<a\s+name\b", re.I)
LIB_RU_KEEP = frozenset({"b", "i", "h2", "h3", "hr", "dir"})
# Ссылка или картинка с абсолютным адресом, протоколом или якорем не локальна: такие теги убираются.
LOCAL_URL_NOT = re.compile(r"(?:[a-z][a-z0-9+.-]*:|/|#)", re.I)


def lib_ru_text(raw: bytes) -> str:
    """HTML библиотеки Мошкова: UTF-8, при ошибке декодирования — cp1251, затем koi8-r."""
    for encoding in ("utf-8", "cp1251", "koi8-r"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("не удалось декодировать HTML")


def lib_ru_plain(match: re.Match[str] | None) -> str:
    """Текст найденного элемента без тегов, сущности раскрыты, пробелы схлопнуты; нет совпадения — пустая строка."""
    return " ".join(html.unescape(LIB_RU_TAG.sub(" ", match.group(1))).split()) if match else ""


def lib_ru_html(path: Path, limit: int) -> tuple[str, str, str]:
    """HTML, название и автор из страницы библиотеки Мошкова (lib.ru).

    Автор и название — по первому «. » в <title> («Имя Фамилия. Название»): автор — 1–4 слова без цифр, иначе автора
    нет и название — весь <title>; если <title> пуст — первый <h2>. Текст внутри каждого <pre> превращается в абзацы
    <p>; вне <pre> HTML не меняется.
    """
    text = lib_ru_text(read_limited(path, limit))
    title = lib_ru_plain(re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I))
    author = ""
    if not title:  # <h2> — только название, автора из него не выделяем
        title = lib_ru_plain(re.search(r"<h2[^>]*>(.*?)</h2>", text, re.S | re.I))
    else:
        left, sep, right = title.partition(". ")
        if sep and right.strip() and 1 <= len(left.split()) <= 4 and not re.search(r"\d", left):
            author, title = " ".join(left.split()), right.strip()
    body = LIB_RU_PRE.sub(lambda match: f"<div>{lib_ru_paragraphs(match.group(1))}</div>", text)
    return body, title, author


def lib_ru_paragraphs(inner: str) -> str:
    """Содержимое <pre> → абзацы: пустая строка или строка с отступом ≥ 3 пробела начинает абзац,
    остальные строки приклеиваются к нему через пробел. Строки с блочными тегами идут отдельно как есть."""
    out: list[str] = []
    lines: list[str] = []
    anchors: list[bool] = []  # стек открытых <a>: True — якорь <a name>, его </a> сохраняется

    def flush() -> None:
        if lines:
            out.append(f"<p>{' '.join(lines)}</p>")
            lines.clear()

    for raw_line in inner.expandtabs(8).split("\n"):
        if not raw_line.strip():
            flush()
            continue
        if LIB_RU_BLOCK.search(raw_line):
            flush()
            out.append(lib_ru_inline(raw_line.strip(), anchors))
            continue
        plain = LIB_RU_TAG.sub("", raw_line)
        if len(plain) - len(plain.lstrip(" ")) >= 3:
            flush()
        piece = lib_ru_inline(re.sub(r"[ \t]+", " ", raw_line.strip()), anchors)
        if piece:
            lines.append(piece)
    flush()
    return "".join(out)


def lib_ru_inline(segment: str, anchors: list[bool]) -> str:
    """Строка из <pre>: текст экранируется, теги проходят через lib_ru_tag."""
    out: list[str] = []
    pos = 0
    for match in LIB_RU_INLINE.finditer(segment):
        out.append(html.escape(html.unescape(segment[pos:match.start()]), quote=False))
        out.append(lib_ru_tag(match.group(0), anchors))
        pos = match.end()
    out.append(html.escape(html.unescape(segment[pos:]), quote=False))
    return "".join(out)


def lib_ru_tag(tag: str, anchors: list[bool]) -> str:
    """Один тег из <pre>: сохраняются <a name>, <b>, <i>, <h2>, <h3>, <hr>, <dir> и локальный <img>; прочее уходит."""
    found = re.match(r"<(/?)\s*([a-z0-9]+)\b([^>]*)>", tag, re.I)
    if not found:
        return ""
    closing, name, attrs = bool(found.group(1)), found.group(2).lower(), found.group(3)
    if name == "a":
        if closing:
            return "</a>" if anchors and anchors.pop() else ""
        named = re.search(r"""name\s*=\s*["']?([\w.-]+)""", attrs, re.I)
        anchors.append(named is not None)
        return f'<a name="{named.group(1)}">' if named else ""
    if name == "img" and not closing:
        src = re.search(r"""src\s*=\s*["']?([^"'\s>]+)""", attrs, re.I)
        if not src or LOCAL_URL_NOT.match(src.group(1)):
            return ""
        return f'<img src="{html.escape(src.group(1), quote=True)}" alt="">'
    if name in LIB_RU_KEEP:
        return f"<{'/' if closing else ''}{name}>"
    return ""


def local_images(markup: str, base: Path, media: Path, limit: int) -> str:
    """Картинки HTML: локальный файл внутри каталога исходника копируется в media (каталог песочницы pandoc),
    ссылка переписывается на копию; чужие, внешние, слишком большие и отсутствующие — убираются."""
    base_dir = base.resolve()
    copied = 0

    def one(match: re.Match[str]) -> str:
        nonlocal copied
        src = re.search(r"""src\s*=\s*["']?([^"'\s>]+)""", match.group(0), re.I)
        url = unquote(html.unescape(src.group(1))) if src else ""
        if not url or LOCAL_URL_NOT.match(url):
            return ""
        origin = (base_dir / url).resolve()
        if not origin.is_relative_to(base_dir) or not origin.is_file() or origin.stat().st_size > limit:
            return ""
        copied += 1
        target = media / f"img{copied}{origin.suffix.lower()}"
        shutil.copyfile(origin, target)
        return f'<img src="{html.escape(str(target), quote=True)}" alt="">'

    return re.sub(r"<img\b[^>]*>", one, markup, flags=re.I)


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


META_TEXT_MAX = 4000  # длинное значение метаданных обрезается до стольких символов
META_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
META_TOOL_TIMEOUT = 30  # секунд на pdfinfo или djvused одной книги
META_OUTPUT_MAX = 1024 * 1024  # вывод внешней программы сверх этого размера отбрасывается
MOBI_HEAD_BYTES = 1024 * 1024  # заголовки PalmDB, MOBI и EXTH лежат в начале файла
TEXT_HEAD_BYTES = 64 * 1024  # байт начала txt для поиска автора (TEXT_HEAD_CHARS символов)
OTHER_DETAIL_FORMATS = ("pdf", "djvu", "mobi", "azw", "azw3", "docx", "odt", "txt")  # scan: detail=True
PDF_FIELDS = ("Title", "Author", "Subject", "Keywords", "Creator", "Producer", "CreationDate", "ModDate", "Pages",
              "Encrypted", "Page size")
DJVU_META = re.compile(r'^\s*\((\w+)\s+"((?:[^"\\]|\\.)*)"\)\s*$')  # строка print-meta: (ключ "значение")
MOBI_EXTH_NAMES = {100: "author", 101: "publisher", 103: "description", 104: "isbn", 105: "subject",
                   106: "publishing_date", 108: "contributor", 503: "updated_title", 524: "language"}
DOCX_CORE_FIELDS = (("title", "title"), ("creator", "creator"), ("subject", "subject"), ("keywords", "keywords"),
                    ("description", "description"), ("created", "created"), ("modified", "modified"),
                    ("lastModifiedBy", "last_modified_by"))
ODT_META_FIELDS = (("title", "title"), ("creator", "creator"), ("subject", "subject"), ("keyword", "keywords"),
                   ("description", "description"), ("creation-date", "created"), ("date", "modified"))


def clean_value(value: Any) -> str:
    """Значение метаданных для БД: управляющие символы убраны, пробелы свёрнуты, длина до META_TEXT_MAX."""
    return " ".join(META_CONTROL.sub("", str(value)).split())[:META_TEXT_MAX].rstrip()


def meta_rows(path: str, pairs: list[tuple[str, Any]]) -> list[tuple[str, str, str]]:
    """Строки book_meta для книги: пустые значения не пишутся, повторы одной пары — одна строка."""
    rows: dict[tuple[str, str], None] = {}
    for key, value in pairs:
        text = clean_value(value)
        if key and text:
            rows[(key[:100], text)] = None
    return [(path, key, text) for key, text in rows]


def run_capture(args: list[str], timeout: int) -> str:
    """Вывод внешней программы: список аргументов без оболочки, stdin из /dev/null, тайм-аут. Сбой — ValueError."""
    try:
        done = subprocess.run(args, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"тайм-аут {timeout} с: {args[0]}") from error
    except OSError as error:
        raise ValueError(f"{args[0]}: {error.strerror or error}") from error
    if done.returncode != 0:
        tail = (done.stderr or b"").decode("utf-8", errors="replace")[-200:].strip()
        raise ValueError(f"{args[0]}: код {done.returncode}: {tail}")
    return (done.stdout or b"")[:META_OUTPUT_MAX].decode("utf-8", errors="replace")


def real_title(value: str, stem: str) -> str:
    """Название из метаданных, если оно не похоже на имя файла и не «Microsoft Word - …»; иначе — пусто (остаётся из пути)."""
    text = clean_value(value)
    if not text or text.lower().startswith("microsoft word"):
        return ""
    if fold(text) in {fold(stem), fold(stem.replace("_", " "))} or re.search(r"\.(pdf|docx?|djvu|epub|fb2|txt|rtf)$", text, re.I):
        return ""
    return text


def pdf_meta(path: Path, stem: str) -> dict[str, Any]:
    """pdfinfo (poppler, UTF-8, даты ISO): поля → book_meta pdf:<поле>; название — если не из имени файла;
    автор и тема (Subject) — в книгу, только если там пусто."""
    output = run_capture(["pdfinfo", "-enc", "UTF-8", "-isodates", str(path)], META_TOOL_TIMEOUT)
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in PDF_FIELDS:
            fields[key.strip()] = clean_value(value)
    pairs: list[tuple[str, Any]] = [(f"pdf:{key.lower().replace(' ', '_')}", value) for key, value in fields.items()]
    if fields.get("Pages"):
        pairs.append(("pages", fields["Pages"]))
    result: dict[str, Any] = {"kv": pairs, "fill": {"authors": [fields["Author"]] if fields.get("Author") else [],
                                                    "annotation": fields.get("Subject", "")}}
    title = real_title(fields.get("Title", ""), stem)
    if title:
        result["title"] = title
    return result


def djvu_meta(path: Path) -> dict[str, Any]:
    """djvused print-meta (djvulibre): строки (ключ "значение") → book_meta djvu:<ключ>; число из «n» — pages."""
    output = run_capture(["djvused", "-e", "print-meta; n", str(path)], META_TOOL_TIMEOUT)
    pairs: list[tuple[str, Any]] = []
    for line in output.splitlines():
        match = DJVU_META.match(line)
        if match:
            pairs.append((f"djvu:{match.group(1).lower()}", re.sub(r"\\(.)", r"\1", match.group(2))))
        elif line.strip().isdigit():
            pairs.append(("pages", line.strip()))
    return {"kv": pairs}


def mobi_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("MOBI: заголовок обрывается раньше, чем ожидалось")
    return int(struct.unpack_from(">I", data, offset)[0])


def mobi_meta(head: bytes, stem: str) -> dict[str, Any]:
    """MOBI/AZW: PalmDB → запись 0 → заголовок MOBI → EXTH (struct, без внешних программ). Ошибка формата — ValueError."""
    if len(head) < 82 or head[60:68] != b"BOOKMOBI":
        raise ValueError("не MOBI: нет заголовка PalmDB")
    rec = head[mobi_u32(head, 78):]
    if rec[16:20] != b"MOBI":
        raise ValueError("MOBI: нет заголовка записи 0")
    encoding = "utf-8" if mobi_u32(rec, 28) == 65001 else "cp1252"

    def text(start: int, size: int) -> str:
        return clean_value(rec[start:start + size].decode(encoding, errors="replace"))

    full_name = text(mobi_u32(rec, 100), mobi_u32(rec, 104))
    pairs: list[tuple[str, Any]] = []
    authors: list[str] = []
    updated = ""
    exth_at = 16 + mobi_u32(rec, 20)
    if mobi_u32(rec, 16 + 128) & 0x40 and rec[exth_at:exth_at + 4] == b"EXTH":
        position = exth_at + 12
        for _ in range(min(mobi_u32(rec, exth_at + 8), 1000)):
            kind, size = mobi_u32(rec, position), mobi_u32(rec, position + 4)
            if size < 8:
                raise ValueError("MOBI: запись EXTH короче заголовка")
            name = MOBI_EXTH_NAMES.get(kind, "")
            value = text(position + 8, size - 8)
            if name == "updated_title":
                updated = value
            if name == "author":
                authors.append(value)
            if name and value:
                pairs.append((name, value))
            position += size
    result: dict[str, Any] = {"kv": pairs, "fill": {"authors": authors}}
    title = real_title(updated or full_name, stem)
    if title:
        result["title"] = title
    return result


def office_meta(path: Path, fmt: str) -> dict[str, Any]:
    """DOCX: docProps/core.xml; ODT: meta.xml (dc:*, meta:keyword, meta:document-statistic page-count) → book_meta."""
    with zipfile.ZipFile(path) as archive:
        if fmt == "docx":
            core = zip_read(archive, "docProps/core.xml", 1024 * 1024).decode("utf-8", errors="replace")
            return {"kv": [(key, xml_tag(tag, core)) for tag, key in DOCX_CORE_FIELDS]}
        xml = zip_read(archive, "meta.xml", 1024 * 1024).decode("utf-8", errors="replace")
    pairs: list[tuple[str, Any]] = [(key, xml_tag(tag, xml)) for tag, key in ODT_META_FIELDS]
    stats = re.search(r"<meta:document-statistic\b([^>]*)>", xml)
    if stats:
        pairs.append(("pages", parse_attrs(stats.group(1)).get("meta:page-count", "")))
    return {"kv": pairs}


def text_meta(path: Path) -> dict[str, Any]:
    """TXT: первый кандидат-автор из начала текста (без папки — только как book_meta text_author)."""
    head = decode_text(read_head(path, TEXT_HEAD_BYTES))[:TEXT_HEAD_CHARS]
    name = next(text_names(head), "")
    return {"kv": [("text_author", name)]} if name else {}


def other_meta(full: Path, fmt: str, stem: str) -> dict[str, Any]:
    """Разбор форматов, которых раньше не касался scan: pdf, djvu, mobi/azw, docx/odt, txt."""
    if fmt == "pdf":
        return pdf_meta(full, stem)
    if fmt == "djvu":
        return djvu_meta(full)
    if fmt in ("mobi", "azw", "azw3"):
        return mobi_meta(read_head(full, MOBI_HEAD_BYTES), stem)
    if fmt in ("docx", "odt"):
        return office_meta(full, fmt)
    return text_meta(full)


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


@functools.lru_cache(maxsize=1)
def pandoc_chapter_option() -> str:
    """Разбивка EPUB на файлы по главам (уровень 2): --split-level с pandoc 3.0, --epub-chapter-level — для старых."""
    try:
        done = subprocess.run(["pandoc", "--version"], capture_output=True, text=True, timeout=30, check=False,
                              stdin=subprocess.DEVNULL)
        major = int(done.stdout.split()[1].split(".")[0])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return "--split-level=2"
    return "--split-level=2" if major >= 3 else "--epub-chapter-level=2"


def convert_one(src: Path, out: Path, fmt: str, cfg: dict[str, Any], options: dict[str, Any]) -> tuple[bool, str]:
    """Цепочка как в doc2fb2.sh: docx/odt → pandoc; doc/rtf → LibreOffice → docx → pandoc;
    запасные пути: rtf → pandoc напрямую → unrtf; doc → html/antiword/catdoc/wvText → текст → pandoc.
    mht/mhtml (как mht_to_fb2.py): HTML из MHT → pandoc; название — <title> или тема, автор — From."""
    timeout = cfg["convert_timeout"]
    ext = src.suffix.lower().lstrip(".")
    meta_title, meta_author = docx_core(src) if ext == "docx" else ("", "")
    limit = cfg["reader_max_mb"] * 1024 * 1024
    page_html = ""
    if ext in {"mht", "mhtml"}:
        page_html, meta_title, meta_author = mht_html(src, limit)
    elif ext in HTML_SOURCES:
        page_html, meta_title, meta_author = lib_ru_html(src, limit)
    title = meta_title if options["title_mode"] == "auto" and meta_title else path_meta(src.name)["title"]
    if ext in HTML_SOURCES and options.get("allow_no_author") and not meta_author:
        author = options["author"]  # HTML без автора: автора из имени папки не берём, в epub он не пишется
    else:
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
            if fmt == "epub" and not any(a.startswith(("--split-level", "--epub-chapter-level")) for a in args):
                args.append(pandoc_chapter_option())
            return run_tool(args, timeout, cwd=tmp)

        staged = tmp / f"source.{ext}"  # копия с простым именем: внешние программы не видят странных имён
        attempts: list[Callable[[], tuple[bool, str]]] = []
        if ext in {"mht", "mhtml"} or ext in HTML_SOURCES:
            staged = tmp / "source.html"
            if ext in HTML_SOURCES:
                # pandoc не видит каталог исходника: картинки копируются в media внутри tmp и ссылки переписываются
                (tmp / "media").mkdir()
                page_html = local_images(page_html, src.parent, tmp / "media", limit)
            staged.write_text(page_html, encoding="utf-8")
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


LIB_RU_INITIAL_PREFIX = re.compile(r"^(?:[A-ZА-ЯЁ]\.)+")
LIB_RU_INITIALS_WORD = re.compile(r"[A-ZА-ЯЁ](?:\.[A-ZА-ЯЁ])*\.?")


def lib_ru_surname(author: str) -> str:
    """Фамилия автора. Инициалы («И.», «М.А», приклеенные «И.Л.Галинская») убираются из слов. Если последнее слово —
    не инициалы, оно и есть фамилия («Михаил Булгаков», «И.Л.Галинская»); иначе — первое неинициальное слово
    («Булгаков М.А»)."""
    words = [w for w in (LIB_RU_INITIAL_PREFIX.sub("", part) for part in author.split()) if w]
    if not words:
        return author.strip()
    if not LIB_RU_INITIALS_WORD.fullmatch(words[-1]):
        return words[-1]
    plain = [w for w in words if not LIB_RU_INITIALS_WORD.fullmatch(w)]
    return plain[-1] if plain else words[-1]


def lib_ru_file_name(stem_source: str, fmt: str) -> str:
    """Имя файла результата HTML: folder_name, без пробелов и точек по краям, «.fmt», вместе не длиннее 120 символов."""
    stem = folder_name(stem_source)[: FOLDER_NAME_MAX - len(fmt) - 1]
    return f"{stem.strip(' .')}.{fmt}"


def lib_ru_name(author: str, title: str, fmt: str) -> str:
    """Имя результата HTML: «Фамилия - Название.fmt», фамилия — см. lib_ru_surname; не длиннее 120 символов."""
    return lib_ru_file_name(f"{lib_ru_surname(author)} - {title}", fmt)


def html_candidate(src: Path, size: int, limit: int, stats: dict[str, int], fmt: str,
                   allow_no_author: bool = False) -> tuple[str, bool] | None:
    """Имя результата для HTML библиотеки и признак «без автора» или None: пропуск index*.htm(l), файлов меньше
    5000 байт, без <pre>; без автора в <title> — пропуск, если не allow_no_author (тогда имя «Название.fmt», название —
    весь <title>, а при пустом <title> — первый <h2>); каждый пропуск — свой счётчик в stats."""
    if HTML_INDEX.match(src.name):
        stats["skipped_index"] += 1
        return None
    if size < HTML_MIN_BYTES:
        stats["skipped_small"] += 1
        return None
    if not re.search(rb"<pre[\s>]", read_limited(src, limit), re.I):
        stats["skipped_no_pre"] += 1
        return None
    _body, title, author = lib_ru_html(src, limit)
    if author:
        return lib_ru_name(author, title, fmt), False
    name = lib_ru_file_name(title, fmt)
    if not allow_no_author or name == f".{fmt}":  # пустое название (и нет <h2>) — пропуск
        stats["skipped_no_author"] += 1
        return None
    return name, True


def quarantine_source(cfg: dict[str, Any], source: Path) -> Path:
    """HTML-исходник после проверки результата не удаляется, а переносится в replaced/<путь в каталоге книг>."""
    root = Path(cfg["books_dir"])
    rel = source.relative_to(root) if source.is_relative_to(root) else Path(source.name)
    dest = Path(cfg["quarantine_dir"]) / "replaced" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest.parent, dest.name, set())
    shutil.move(source, dest)
    return dest


def convert_documents(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                      out: Callable[[str], None] = print, **options: Any) -> dict[str, int]:
    """Конвертирует DOC/DOCX/RTF/ODT/MHT (и по --only html — HTML библиотеки Мошкова) в EPUB (по правилу библиотеки)
    или FB2 рядом с исходником. HTML получает имя «Фамилия - Название.epub».

    Существующий результат не перезаписывается без overwrite. Исходник переносится в карантин replaced/ только с
    delete_source и только после проверки результата (verify_book); не удаляется ни для одного формата (правило
    карантина). Перед переносом — JSON-отчёт books_library/reports/convert-<время>.json.
    """
    if not shutil.which("pandoc"):
        raise ConfigError("не найден pandoc (нужен для convert)")
    target = target_dir(cfg, path)
    fmt = options["to"]
    html_mode = options["only"] == "html"
    only = CONVERT_SOURCES if options["only"] == "all" else HTML_SOURCES if html_mode else (options["only"],)
    limit = cfg["reader_max_mb"] * 1024 * 1024
    excludes = [x.casefold() for x in options["exclude"]]
    errors: list[str] = []
    plan: list[tuple[Path, Path]] = []
    taken: set[Path] = set()
    anonymous: set[Path] = set()  # HTML без автора в <title> (--allow-no-author)
    allow_no_author = bool(options.get("allow_no_author"))
    stats = {"sources": 0, "planned": 0, "skipped_exists": 0, "skipped_index": 0, "skipped_small": 0,
             "skipped_no_pre": 0, "skipped_no_author": 0, "converted": 0, "converted_no_author": 0, "failed": 0,
             "quarantined_sources": 0}
    for src, size in walk_files(target, tuple(f".{x}" for x in only), errors):
        stats["sources"] += 1
        if any(x in src.name.casefold() for x in excludes):
            continue
        if html_mode:
            try:
                found = html_candidate(src, size, limit, stats, fmt, allow_no_author)
            except (OSError, ValueError) as error:
                stats["failed"] += 1
                say(f"не прочитан {src}: {error}", "convert_failed", logging.ERROR, detail=str(error))
                continue
            if found is None:
                continue
            name, no_author = found
            if no_author:
                anonymous.add(src)
            result = src.parent / name
        else:
            result = src.with_suffix(f".{fmt}")
        if result.exists() and not (options["overwrite"] and not (options["skip_newer"] and result.stat().st_mtime >= src.stat().st_mtime)):
            stats["skipped_exists"] += 1
            continue
        if html_mode and result in taken:  # два HTML с одним автором и названием в одной папке
            result = free_variant_name(cfg, src.parent, result.name, taken, src=src)
        taken.add(result)
        plan.append((src, result))
    stats["planned"] = len(plan)
    for error in errors[:20]:
        say(f"ошибка обхода: {error}", "walk_error", logging.WARNING, detail=error)
    if dry_run:
        for src, result in plan[:30]:
            out(f"[dry-run] будет: {src} → {result}{' (исходник в карантин replaced/ после проверки)' if options['delete_source'] else ''}")
        out(f"[dry-run] будет: конвертировать {len(plan)} из {stats['sources']} документов в {fmt.upper()}, "
              f"уже есть {stats['skipped_exists']}, из них без автора {sum(1 for src, _ in plan if src in anonymous)}")
        say("план конвертации", "dry_convert", detail=json.dumps(stats))
        return stats
    done_list: list[dict[str, Any]] = []
    started = last = time.monotonic()
    for number, (src, result) in enumerate(plan, 1):
        ok, err = convert_one(src, result, fmt, cfg, options)
        if ok:
            stats["converted"] += 1
            if src in anonymous:
                stats["converted_no_author"] += 1
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
        report = write_report("convert", {"target": str(target), "quarantined_sources": done_list,
                                          "reason": f"исходник заменён проверенным {fmt.upper()}"})
        for item in done_list:
            if verify_book(Path(item["result"]), fmt) is None:
                quarantine_source(cfg, Path(item["source"]))
                stats["quarantined_sources"] += 1
        say(f"исходников в карантине replaced/: {stats['quarantined_sources']}, отчёт {report}", "sources_quarantined",
            detail=str(report))
    say(f"конвертация: {json.dumps(stats, ensure_ascii=False)}", "convert_done", detail=json.dumps(stats))
    return stats


# ======================================================================
# Ночные режимы: карантин, кодировки, fb2 → epub, дубликаты, распаковка однокнижных zip
# ======================================================================

# Причины карантина: <quarantine_dir>/<причина>/<путь в каталоге книг>. Карантин не индексируется.
QUARANTINE_REASONS = ("replaced", "duplicate", "edited", "rejected-convert", "rejected-zip", "rejected-encoding",
                      "rejected-name", "unzipped", "replaced-calibre", "not-book")
# Уже сжатые форматы кладутся в zip без сжатия: deflate их не уменьшит, только потратит процессор.
STORED_EXTENSIONS = {"pdf", "djvu", "chm", "docx", "odt", "mobi", "azw", "azw3", "epub", "mp3", "jpg", "jpeg", "png",
                     "gif", "webp"}
# Имена папок и файлов библиотеки должны быть допустимы в Windows (Syncthing): см. windows_name_reason.
WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"{dev}{n}" for dev in ("COM", "LPT") for n in "0123456789¹²³")})
WINDOWS_NAME_MESSAGES = {"empty": "пустое имя", "chars": "запрещённые символы или управляющие",
                         "trailing": "точка или пробел в конце", "reserved": "зарезервированное имя устройства Windows",
                         "long": "длиннее 255 байт UTF-8"}


def windows_name_reason(name: str) -> str:
    """Код причины, по которой имя недопустимо в Windows (empty, chars, trailing, reserved, long); пусто — имя годно.
    Зарезервированные имена проверяются без учёта регистра и с любым расширением: «con.txt» — тоже нельзя."""
    if not name:
        return "empty"
    if WINDOWS_BAD_CHARS.search(name):
        return "chars"
    if name.endswith((".", " ")):
        return "trailing"
    if name.partition(".")[0].upper() in WINDOWS_RESERVED:
        return "reserved"
    if len(name.encode("utf-8")) > 255:
        return "long"
    return ""


def windows_name_error(name: str) -> str:
    """Причина для сообщения (пусто — имя годно); см. windows_name_reason."""
    reason = windows_name_reason(name)
    return WINDOWS_NAME_MESSAGES[reason] if reason else ""


def windows_name_fix(name: str, is_dir: bool) -> str:
    """Допустимое имя вместо недопустимого (режим windows-names): запрещённые символы → «_», точки и пробелы в конце
    убираются, зарезервированная основа получает «_» («CON.txt» → «CON_.txt»), длиннее 255 байт — укорачивается основа
    (расширение файла сохраняется). Допустимое имя возвращается без изменений."""
    fixed = WINDOWS_BAD_CHARS.sub("_", name).rstrip(" .") or "_"
    base, dot, rest = fixed.partition(".")
    if base.upper() in WINDOWS_RESERVED:
        fixed = base + "_" + dot + rest
    if len(fixed.encode("utf-8")) <= 255:
        return fixed
    stem, dot, ext = fixed.rpartition(".")
    if is_dir or not dot or len(ext.encode("utf-8")) > 32:
        stem, ext = fixed, ""
    else:
        ext = dot + ext
    budget = 255 - len(ext.encode("utf-8"))
    return (stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .") or "_") + ext


def windows_dir_plan(entries: list[tuple[str, bool]]) -> list[tuple[str, str, str, str]]:
    """План windows-names для одной папки из (имя, это папка): (старое имя, новое, причина, twin). Сначала исправление
    недопустимых имён. Совпадение без учёта регистра с уже оставленным именем — конфликт регистра: имя не переименовывается,
    план (старое, старое, «case», оставленное имя). Первым остаётся неизменённое имя, затем по сортировке. Файлы и папки
    делят одно пространство имён; конфликты решает dedup-folders или человек."""
    fixed = []
    for name, is_dir in entries:
        reason = windows_name_reason(name)
        fixed.append((name, windows_name_fix(name, is_dir) if reason else name, reason))
    kept: dict[str, str] = {}
    plan = []
    for old, new, reason in sorted(fixed, key=lambda item: (item[1].casefold(), bool(item[2]), item[1])):
        twin = kept.get(new.casefold())
        if twin is not None:
            plan.append((old, old, "case", twin))
            continue
        kept[new.casefold()] = new
        plan.append((old, new, reason, ""))
    return plan


# Значимость форматов для dedup: остаётся самый значимый (epub → fb2 → txt → pdf → djvu → прочие).
FORMAT_RANK = {"epub": 0, "fb2": 1, "txt": 2, "pdf": 3, "djvu": 4}
DEDUP_MIN_WORDS = 200
DEDUP_COVERAGE = 0.97
DEDUP_GROUP_MAX = 16
DEDUP_SIMILAR = 0.85  # доля шинглов от оставленных, с которой книга «похожая»: разноимённые — решает человек; одноимённые (same_name) — дубль
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
            if version >= SCHEMA_META:
                conn.execute("UPDATE OR IGNORE book_meta SET path=? WHERE path=?", (new, old))
                conn.execute("DELETE FROM book_meta WHERE path=?", (old,))
            if version >= SCHEMA_COLLECTIONS:
                conn.execute("UPDATE OR IGNORE book_collections SET path=? WHERE path=?", (new, old))
                conn.execute("DELETE FROM book_collections WHERE path=?", (old,))
                conn.execute("UPDATE dup_members SET keeper=? WHERE keeper=?", (new, old))
                conn.execute("UPDATE OR IGNORE dup_members SET path=? WHERE path=? AND role='kept'", (new, old))
            if version >= SCHEMA_VARIANTS:
                conn.execute("UPDATE book_variants SET keeper=? WHERE keeper=?", (new, old))
    return len(pairs)


def rebuild_collections(conn: sqlite3.Connection) -> dict[str, int]:
    """Дополняет book_collections (схема v4) в транзакции вызывающего: строки не удаляются и не переписываются —
    принадлежность книги к сборнику переживает перенос файла из папки сборника (его путь переносит relink).
    folder — книги из папок сборников; dedup — копии из сборников, ушедшие в карантин как дубли: принадлежность
    переходит к оставленной книге (keeper), с учётом переименования keeper в name.ext.zip. Без книги — keeper не
    найден ни как путь, ни как .zip. Подраздел — путь между папкой сборника и файлом (subsection_of), вид — source."""
    counts = {"folder": 0, "dedup": 0, "unresolved": 0}
    folders = conn.execute("SELECT path, section, collection FROM books WHERE collection <> ''").fetchall()
    for path, section, collection in folders:
        counts["folder"] += conn.execute(
            "INSERT OR IGNORE INTO book_collections(path, section, collection, source, orig_path, subsection, kind) "
            "VALUES (?,?,?,'folder',?,?,'folder')", (path, section, collection, path, subsection_of(path))).rowcount
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
        counts["dedup"] += conn.execute(
            "INSERT OR IGNORE INTO book_collections(path, section, collection, source, orig_path, subsection, kind) "
            "VALUES (?,?,?,'dedup',?,?,'dedup')", (target, section, collection, path, subsection_of(path))).rowcount
    fill_collection_kinds(conn)
    return counts


def subsection_of(rel: str) -> str:
    """Подраздел сборника: путь между папкой сборника и именем файла. «_Раздел/Сборник/Подраздел/x.txt» → «Подраздел»."""
    parts = PurePosixPath(rel).parts
    if len(parts) > 1 and parts[0].startswith("_"):
        return "/".join(parts[2:-1])
    return ""


def fill_collection_kinds(conn: sqlite3.Connection) -> None:
    """Строки book_collections до схемы v6 дополняются пустыми subsection (из orig_path) и kind (= source).
    Заполненные строки не переписываются: условие kind = '' отбирает только старые строки."""
    for orig, source in conn.execute("SELECT DISTINCT orig_path, source FROM book_collections "
                                     "WHERE kind = '' AND source IN ('folder', 'dedup')").fetchall():
        conn.execute("UPDATE book_collections SET subsection=?, kind=source WHERE orig_path=? AND source=? AND kind=''",
                     (subsection_of(orig), orig, source))


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


# ----------------------------------------------------------------------
# Дописывание метаданных epub из fb2 (правило «epub не хуже fb2»)
# ----------------------------------------------------------------------

ENRICH_META_FIELDS = ("series", "series_no", "annotation", "genres", "translator", "publisher", "isbn", "year", "lang")


def _kv_map(meta: Mapping[str, Any]) -> dict[str, str]:
    """Первое непустое значение каждого ключа из kv разбора (пары ключ-значение)."""
    found: dict[str, str] = {}
    for key, value in meta.get("kv", []):
        if value and key not in found:
            found[key] = str(value)
    return found


def _int_or_none(value: Any) -> int | None:
    text = "" if value is None else str(value).strip()
    return int(text) if text.isdigit() else None


def fb2_epub_fields(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Поля для enrich_epub_from из разбора fb2: read_book_meta или meta JSON варианта (book_variants)."""
    kv = _kv_map(meta)
    return {
        "series": str(meta.get("series") or ""), "series_no": _int_or_none(meta.get("series_no")),
        "annotation": str(meta.get("annotation") or ""), "genres": [str(g) for g in meta.get("genres") or []],
        "translator": kv.get("translator", "").split("; ")[0], "publisher": kv.get("publisher", ""),
        "isbn": kv.get("isbn", ""), "year": str(meta.get("year") or ""), "lang": str(meta.get("lang") or ""),
    }


def merge_enrich_fields(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Первое непустое значение каждого поля по списку источников (порядок списка — приоритет)."""
    merged: dict[str, Any] = {}
    for fields in sources:
        for key in ENRICH_META_FIELDS:
            if fields.get(key) and not merged.get(key):
                merged[key] = fields[key]
    return merged


def _zip_rewrite(path: Path, payloads: dict[str, bytes]) -> None:
    """Пересборка zip во временный файл рядом: прежний порядок записей, mimetype первым и без сжатия. payloads заменяют
    запись с тем же именем или добавляются в конец. Затем os.replace и права исходного файла; временный файл — всегда удаляется."""
    pending = dict(payloads)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name[:80]}.", suffix=".enrich.tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(tmp, "w") as target:
            for info in sorted(source.infolist(), key=lambda item: item.filename != "mimetype"):
                data = pending.pop(info.filename) if info.filename in pending else read_member(source, info, EDIT_MEMBER_MAX)
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
                entry.external_attr = info.external_attr
                target.writestr(entry, data)
            for name, data in pending.items():
                target.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def enrich_epub_from(epub_path: Path, meta: Mapping[str, Any], cover: bytes | None, dry_run: bool = False) -> list[str]:
    """Дописывает в OPF epub поля meta (ENRICH_META_FIELDS) и обложку cover, которых в epub нет или они пусты.
    Непустые поля epub не меняются; title и авторы не трогаются. Возвращает добавленные поля; dry_run — только список.
    Без dc-префикса в OPF dc-поля не дописываются; префикс у metadata — ValueError (книга не меняется)."""
    with zipfile.ZipFile(epub_path) as source:
        epub = Epub(source)
        opf, opf_path = epub.opf, epub.opf_path
        match = OPF_METADATA_RE.search(opf)
        if not match:
            raise ValueError("в OPF нет блока metadata")
        if match.group(2):
            raise ValueError("у metadata в OPF префикс пространства имён: дописывание не выполняется")
        current = epub.meta()
    present = {key for key, value in current["kv"] if value}
    dc = "xmlns:dc" in opf
    lines: list[str] = []
    manifest: list[str] = []
    payloads: dict[str, bytes] = {}
    added: list[str] = []
    if meta.get("series") and not current.get("series"):
        series, number = _xml_text(str(meta["series"])), _int_or_none(meta.get("series_no"))
        lines += [f'<meta property="belongs-to-collection" id="enrich-series">{series}</meta>',
                  '<meta refines="#enrich-series" property="collection-type">series</meta>',
                  f'<meta name="calibre:series" content="{series}"/>']
        if number is not None:
            lines += [f'<meta refines="#enrich-series" property="group-position">{number}</meta>',
                      f'<meta name="calibre:series_index" content="{number}"/>']
        added.append("series")
    if dc:
        if meta.get("annotation") and not current.get("annotation"):
            lines.append(f"<dc:description>{_xml_text(str(meta['annotation']))}</dc:description>")
            added.append("annotation")
        if meta.get("genres") and not current.get("genres"):
            lines += [f"<dc:subject>{_xml_text(str(genre))}</dc:subject>" for genre in meta["genres"]]
            added.append("genres")
        if meta.get("year") and not current.get("year"):
            lines.append(f"<dc:date>{_xml_text(str(meta['year']))}</dc:date>")
            added.append("year")
        if meta.get("lang") and not current.get("lang"):
            lines.append(f"<dc:language>{_xml_text(str(meta['lang']))}</dc:language>")
            added.append("lang")
        if meta.get("translator") and "translator" not in present:
            name = _xml_text(str(meta["translator"]))
            lines += [f'<dc:contributor id="enrich-translator">{name}</dc:contributor>',
                      '<meta refines="#enrich-translator" property="role" scheme="marc:relators">trl</meta>']
            added.append("translator")
        if meta.get("publisher") and "publisher" not in present:
            lines.append(f"<dc:publisher>{_xml_text(str(meta['publisher']))}</dc:publisher>")
            added.append("publisher")
        if meta.get("isbn") and "isbn" not in present:
            lines.append(f"<dc:identifier>urn:isbn:{_xml_text(str(meta['isbn']))}</dc:identifier>")
            added.append("isbn")
    ext = _image_ext(cover) if cover else None
    if cover and ext and not current.get("cover"):
        base = posixpath.dirname(opf_path)
        href = f"img/enrich-cover.{ext}"
        inside = posixpath.join(base, href) if base else href
        if inside in epub.names:
            raise ValueError("имя файла обложки занято в epub")
        manifest.append(f'<item id="enrich-cover" href="{href}" media-type="{EPUB_IMAGE_TYPES[ext]}" '
                        'properties="cover-image"/>')
        if not re.search(r"<(?:[\w.-]+:)?meta\b[^>]*\bname\s*=\s*[\"']cover[\"']", opf):
            lines.append('<meta name="cover" content="enrich-cover"/>')
        payloads[inside] = cover
        added.append("cover")
    if not added:
        return []
    if dry_run:
        return added
    inner = match.group(3).rstrip() + "".join(f"\n    {line}" for line in lines) + "\n  " if lines else match.group(3)
    new_opf = opf[:match.start(3)] + inner + opf[match.end(3):]
    if manifest:
        if new_opf.count("</manifest>") != 1:
            raise ValueError("в OPF нет единственного блока manifest")
        new_opf = new_opf.replace("</manifest>", "".join(manifest) + "</manifest>")
    payloads[opf_path] = new_opf.encode("utf-8")
    _zip_rewrite(epub_path, payloads)
    return added


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
    while title and len(f"{authors} - {title}{ext}".encode()) > EDIT_NAME_MAX:
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
    return not windows_name_error(name)


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
            if infos[0].file_size != size or crc != infos[0].CRC:
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
                run.record("skipped", full, reason=str(error))  # битые архивы пропускаются
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
@namespace epub "http://www.idpf.org/2007/ops";
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
a[epub|type~="noteref"], a.noteref { -cr-hint: noteref; }
aside[epub|type~="footnote"], aside.footnote { -cr-hint: footnote-inpage; }
.footnotes{font-size:.85em;margin-top:1.5em}
.footnotes hr{width:30%;margin:0 0 .6em 0;text-align:left}
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
        self.counts: dict[Any, int] = {}
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
        """Ссылка на сноску: ведёт на копию сноски в этом же файле (note_copies); первая ссылка в файле получает id
        ref-<копия>, повторные — ref-<копия>-<номер>."""
        state = self.state
        key = (self.file, note)
        state.refs.append(note)
        count = state.counts.get(key, 0) + 1
        state.counts[key] = count
        state.labels.setdefault(note, _text(node))
        copy = self.book.note_copies.get(key, note)
        ref_id = f"ref-{copy}" if count == 1 else f"ref-{copy}-{count}"
        text = self.inline(node) or "*"
        return (f'<a epub:type="noteref" role="doc-noteref" class="noteref" href="#{_esc(copy)}" '
                f'id="{_esc(ref_id)}"><sup>{text}</sup></a>')

    def aside(self, note: str, out: list[str]) -> None:
        """Сноска в этом файле: <aside epub:type="footnote"> с текстом и обратной ссылкой на первую ссылку в файле;
        заголовок, совпадающий со ссылкой, опускается."""
        section = self.book.notes[note]
        key = (self.file, note)
        copy = self.book.note_copies.get(key, note)
        self.state.anchors.setdefault(note, self.file)
        out.append(f'<aside epub:type="footnote" role="doc-footnote" id="{_esc(copy)}" class="footnote">')
        label = self.state.labels.get(note)
        for child in _kids(section, "title"):
            if label is None or _text(child) != label:
                out.append(f'<p class="footnote-title">{self.lines(child)}</p>')
        self.blocks(section, 2, out, skip_title=True)
        if key in self.book.note_copies:
            out.append(f'<p class="backlink"><a href="#ref-{_esc(copy)}" role="doc-backlink">↩</a></p>')
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
        self.book_name = _text(_first(publish, "book-name"))  # бумажное издание-сборник (как fb2_details)
        self.publisher_series = [(_attr(seq, "name").strip(), _attr(seq, "number").strip())
                                 for seq in _kids(publish, "sequence") if _attr(seq, "name").strip()]
        seq = _first(ti, "sequence")
        self.series = _attr(seq, "name").strip()
        number = re.match(r"\d+", _attr(seq, "number").strip())
        self.series_no = number.group(0) if number else ""
        self.series_extra: list[tuple[str, str]] = []  # вторая и далее серии title-info: (название, номер)
        for extra in _kids(ti, "sequence")[1:]:
            extra_name = _attr(extra, "name").strip()
            extra_no = re.match(r"\d+", _attr(extra, "number").strip())
            if extra_name:
                self.series_extra.append((extra_name, extra_no.group(0) if extra_no else ""))
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
        self.note_copies: dict[tuple[str, str], str] = {}  # (файл, сноска) -> id её копии в этом файле
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
                    start = len(units)
                    self._plan_section(node, 1, units)
                    # заголовок книги и вступление перед разделом — в начало файла этого раздела, без отдельного файла
                    units[start].nodes[:0] = pending
                    pending = []
                else:
                    pending.append(node)
            if pending:
                units.append(_Unit(pending, set(), 1))
        for index, unit in enumerate(units, 1):
            unit.name = f"ch{index:03d}.xhtml"
        return units

    def _unit_title(self, unit: _Unit) -> str:
        first = next((node for node in unit.nodes if _local(node.tag) == "section"), unit.nodes[0])
        if _local(first.tag) == "section":
            return _text(_first(first, "title")) or "Раздел"
        return _text(_first(first, "title")) or self.title

    def _placement(self, file_refs: list[tuple[str, list[str]]], note_refs: dict[str, list[str]]
                   ) -> tuple[dict[str, str], dict[str, list[str]], list[str]]:
        """Размещение сносок. Каждый файл, где есть ссылка, получает копию сноски (aside) в конце себя вместе с теми
        сносками, на которые ссылаются сами сноски. Копии: первая — id сноски, далее nX-2, nX-3… Сноски, на которые нет
        ни одной ссылки из глав и служебных разделов, — блоками в notes.xhtml; их ссылки ведут внутрь notes.xhtml.
        Заполняет note_copies. Возвращает (первый файл сноски, сноски файлов, блоки notes.xhtml)."""
        def closure(roots: list[str]) -> list[str]:
            order: list[str] = []
            queue = deque(roots)
            while queue:
                note = queue.popleft()
                if note in self.notes and note not in order:
                    order.append(note)
                    queue.extend(note_refs.get(note, []))
            return order

        refs_by_file = dict(file_refs)
        chapters = [name for name, _refs in file_refs if name != EPUB_NOTES_FILE]
        placed: dict[str, list[str]] = {name: closure(refs_by_file[name]) for name in chapters}
        rest_roots = refs_by_file.get(EPUB_NOTES_FILE, [])
        used = {note for name in chapters for note in placed[name]} | set(closure(rest_roots))
        blocks = [note for note in self.notes if note not in used]
        block_roots = [ref for note in blocks for ref in note_refs.get(note, [])]
        placed[EPUB_NOTES_FILE] = [note for note in closure(rest_roots + block_roots) if note not in blocks]
        self.note_copies = {}
        seq: dict[str, int] = {}
        for name in [*chapters, EPUB_NOTES_FILE]:
            for note in placed[name]:
                seq[note] = seq.get(note, 0) + 1
                self.note_copies[(name, note)] = note if seq[note] == 1 else f"{note}-{seq[note]}"
        for note in blocks:
            self.note_copies[(EPUB_NOTES_FILE, note)] = note
        homes: dict[str, str] = {}
        for name in [*chapters, EPUB_NOTES_FILE]:
            for note in placed[name]:
                homes.setdefault(note, name)
        for note in blocks:
            homes.setdefault(note, EPUB_NOTES_FILE)
        return homes, placed, blocks

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
        self.homes, placed, blocks = self._placement(file_refs, note_refs)
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
            _footnote_section(renderer, placed.get(unit.name, []), out)
            texts.append((unit.name, self._unit_title(unit), "".join(out)))
        renderer = _Renderer(self, final, EPUB_NOTES_FILE)
        out = []
        for node in self.rest:
            renderer.block(node, 1, out)
        for note in blocks:
            renderer.block(self.notes[note], 1, out)
        _footnote_section(renderer, placed.get(EPUB_NOTES_FILE, []), out)
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


FOOTNOTES_OPEN = '<section class="footnotes" epub:type="footnotes"><hr/>'


def _footnote_section(renderer: _Renderer, notes: list[str], out: list[str]) -> None:
    """Все сноски файла одним блоком в самом конце: section.footnotes, порядок — по первой ссылке. Не скрываются."""
    if not notes:
        return
    asides: list[str] = []
    for note in notes:
        renderer.aside(note, asides)
    out.append(FOOTNOTES_OPEN + "".join(asides) + "</section>")


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

    Главы — файлы text/chNNN.xhtml; сноски — всплывающие <aside epub:type="footnote"> в каждом файле, где на них ссылаются (копии с id nX, nX-2…),
    несвязанные — блоками в notes.xhtml; NCX для читалок EPUB 2. Текст не меняется, только экранируется.
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
    for number, (extra_name, position) in enumerate(book.series_extra, 2):
        meta.append(f'<meta property="belongs-to-collection" id="c{number}">{_esc(extra_name)}</meta>')
        meta.append(f'<meta refines="#c{number}" property="collection-type">series</meta>')
        if position:
            meta.append(f'<meta refines="#c{number}" property="group-position">{_esc(position)}</meta>')
    if book.book_name and author_norm(book.book_name) != author_norm(book.title):
        meta.append(f'<meta property="belongs-to-collection" id="edition1">{_esc(book.book_name)}</meta>')
        meta.append('<meta refines="#edition1" property="collection-type">set</meta>')
    for name, number in book.publisher_series:  # издательская серия — не belongs-to-collection, чтобы не спутаться с авторской
        meta.append(f'<meta name="books_library:publisher-series" content="{_esc(name)}"/>')
        if number:
            meta.append(f'<meta name="books_library:publisher-series-index" content="{_esc(number)}"/>')
    meta.append(f'<meta property="dcterms:modified">{modified}</meta>')
    meta.append(f'<meta name="generator" content="{FB2_CONVERTER_GENERATOR}"/>')
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


# ----------------------------------------------------------------------
# Полнота книги и пересборка epub из fb2: оглавление и сноски
# ----------------------------------------------------------------------

EPUB_NOTE_RE = re.compile(r"\btype\s*=\s*[\"'][^\"']*\b(?:footnote|endnote|noteref)\b|<(?:\w+:)?aside\b")


def epub_has_toc(epub: Epub) -> bool:
    """Оглавление epub: nav-документ с epub:type="toc" или ncx, в каждом не меньше двух пунктов."""
    for item in epub.items.values():
        if "nav" in item.get("properties", "").split() and item["path"] in epub.names:
            text = decode_xml(zip_read(epub.zip, item["path"]))
            block = re.search(r"<(?:\w+:)?nav\b[^>]*\btype\s*=\s*[\"']toc[\"'][^>]*>(.*?)</(?:\w+:)?nav\s*>", text, re.S)
            if block and len(re.findall(r"<(?:\w+:)?a\b", block.group(1))) >= 2:
                return True
    for item in epub.items.values():
        if (item.get("media-type") == "application/x-dtbncx+xml" and item["path"] in epub.names
                and len(re.findall(r"<(?:\w+:)?navPoint\b", decode_xml(zip_read(epub.zip, item["path"])))) >= 2):
            return True
    return False


def epub_has_notes(epub: Epub) -> bool:
    """Сноски epub: epub:type footnote/endnote/noteref или aside в главах (spine); каждая глава — в пределах ZIP_MEMBER_MAX."""
    return any(EPUB_NOTE_RE.search(decode_xml(zip_read(epub.zip, path))) for path in epub.spine())


def fb2_structure(text: str) -> tuple[bool, bool]:
    """Оглавление fb2 (не меньше двух section с title вне тел сносок) и наличие тела сносок (FB2_NOTE_BODIES)."""
    notes, sections = False, 0
    for body in re.findall(r"<(?:\w+:)?body\b[^>]*>.*?</(?:\w+:)?body\s*>", text, re.S):
        name = parse_attrs(re.match(r"<[^>]*>", body).group(0)).get("name", "").strip().lower()
        if name in FB2_NOTE_BODIES:
            notes = True
            continue
        sections += len(re.findall(r"<(?:\w+:)?section\b[^>]*>\s*<(?:\w+:)?title\b", body))
    return sections >= 2, notes


NOTE_ID_RE = re.compile(r"^(?:note|footnote|n\d+)", re.I)
EXTERNAL_HREF = ("http:", "https:", "mailto:", "tel:")


def _completeness(meta: Mapping[str, Any], toc: bool, notes: bool, links_ok: bool | None = None) -> dict[str, bool]:
    kv = _kv_map(meta)
    result = {
        "title": bool(meta.get("title")), "authors": bool(meta.get("authors")), "series": bool(meta.get("series")),
        "annotation": bool(meta.get("annotation")), "cover": bool(meta.get("cover")), "genres": bool(meta.get("genres")),
        "lang": bool(meta.get("lang")), "year": bool(meta.get("year")), "translator": "translator" in kv,
        "publisher": "publisher" in kv, "isbn": "isbn" in kv, "toc": toc, "notes": notes,
    }
    if links_ok is not None:
        result["links_ok"] = links_ok
    return result


def _targets(text: str) -> dict[str, tuple[str, dict[str, str]]]:
    """Элементы файла с id: id → (локальное имя тега, атрибуты)."""
    found: dict[str, tuple[str, dict[str, str]]] = {}
    for tag, attrs_text in re.findall(r"<([\w:.-]+)\b([^>]*)>", text):
        attrs = parse_attrs(attrs_text)
        if "id" in attrs:
            found.setdefault(attrs["id"], (tag.rpartition(":")[2].lower(), attrs))
    return found


def _is_note_target(tag: str, attrs: Mapping[str, str]) -> bool:
    """Сноска: aside, epub:type footnote, класс note/footnote или id вида note…, footnote…, n<цифры>."""
    return (tag == "aside" or "footnote" in attrs.get("epub:type", "")
            or re.search(r"\b(?:note|footnote)\b", attrs.get("class", "")) is not None
            or NOTE_ID_RE.match(attrs.get("id", "")) is not None)


def _link_files(epub: Epub) -> list[str]:
    """Файлы с внутренними ссылками книги: главы (spine) и оглавление nav."""
    nav = [item["path"] for item in epub.items.values()
           if "nav" in item.get("properties", "").split() and item["path"] in epub.names]
    return list(dict.fromkeys([*epub.spine(), *nav]))


def _local_link(path: str, href: str) -> tuple[str, str] | None:
    """Внутренняя ссылка из файла path: (файл назначения в zip, id после «#») или None для внешней."""
    if not href or href.lower().startswith(EXTERNAL_HREF):
        return None
    name, _, frag = href.partition("#")
    return (Epub.resolve(posixpath.dirname(path), name) if name else path), frag


def epub_link_problems(epub: Epub) -> tuple[list[str], list[str]]:
    """Внутренние ссылки epub: (битые — нет файла или элемента с #id; сноски — ссылка на сноску без epub:type="noteref"
    или ведущая в другой файл). Внешние ссылки не проверяются. Только чтение."""
    cache: dict[str, dict[str, tuple[str, dict[str, str]]]] = {}
    broken: list[str] = []
    notes_bad: list[str] = []
    for path in _link_files(epub):
        text = decode_xml(zip_read(epub.zip, path))
        for attrs_text in re.findall(r"<a\b([^>]*)>", text):
            attrs = parse_attrs(attrs_text)
            href = attrs.get("href", "").strip()
            local = _local_link(path, href)
            if local is None:
                continue
            target, frag = local
            if target not in epub.names:
                broken.append(f"{path}: {href}")
                continue
            if not frag:
                continue
            if target not in cache:
                cache[target] = _targets(decode_xml(zip_read(epub.zip, target)))
            hit = cache[target].get(frag)
            if hit is None:
                broken.append(f"{path}: {href}")
            elif _is_note_target(*hit) and (target != path or "noteref" not in attrs.get("epub:type", "").split()):
                notes_bad.append(f"{path}: {href}")
    return broken, notes_bad


def _element_inner(text: str, frag: str) -> str | None:
    """Содержимое элемента с id frag; None, если элемента нет или он вложен в элемент того же тега."""
    match = re.search(r"<([\w:.-]+)\b[^>]*\bid\s*=\s*[\"']" + re.escape(frag) + r"[\"'][^>]*>(.*?)</\1\s*>", text, re.S)
    if match is None or re.search(rf"<{re.escape(match.group(1))}\b", match.group(2)):
        return None
    return match.group(2)


NOTES_CSS = ("\na.noteref { -cr-hint: noteref; }\n"
             "aside.footnote, div.footnote, p.footnote { -cr-hint: footnote-inpage; }\n")


def _with_class(attrs_text: str, token: str) -> str:
    """Атрибуты тега с классом token: дописывается к class, если атрибут уже есть."""
    found = re.search(r'\sclass\s*=\s*"([^"]*)"', attrs_text)
    if found is None:
        return f'{attrs_text} class="{token}"'
    if token in found.group(1).split():
        return attrs_text
    return f'{attrs_text[:found.start()]} class="{found.group(1)} {token}"{attrs_text[found.end():]}'


def _with_style(attrs_text: str, declaration: str) -> str:
    """Атрибуты тега с объявлением style — для читалок, которым нужна подсказка без CSS книги."""
    found = re.search(r'\sstyle\s*=\s*"([^"]*)"', attrs_text)
    if found is None:
        return f'{attrs_text} style="{declaration}"'
    if declaration in found.group(1):
        return attrs_text
    return f'{attrs_text[:found.start()]} style="{found.group(1)}; {declaration}"{attrs_text[found.end():]}'


def _style_attr(style: str) -> str:
    return f' style="{style}"' if style else ""


def _retag_a(attrs_text: str, href: str, types: list[str], style: str) -> str:
    """Открывающий тег <a> ссылки на сноску: href, epub:type с noteref, class noteref; прочие атрибуты сохраняются."""
    rest = re.sub(r"\s(?:href|epub:type)\s*=\s*(?:\"[^\"]*\"|'[^']*')", "", attrs_text)
    rest = _with_class(rest, "noteref")
    if style:
        rest = _with_style(rest, style)
    return f'<a epub:type="{" ".join(dict.fromkeys([*types, "noteref"]))}" href="{_esc(href)}"{rest}>'


def _mark_footnote(match: re.Match[str], style: str) -> str:
    """Открывающий тег сноски: epub:type="footnote" (если его нет), класс footnote, при нужде — style."""
    tag, attrs = match.group(1), match.group(2)
    if "epub:type" not in attrs:
        attrs = f' epub:type="footnote"{attrs}'
    attrs = _with_class(attrs, "footnote")
    if style:
        attrs = _with_style(attrs, style)
    return f"<{tag}{attrs}>"


def _add_footnotes(text: str, asides: list[str]) -> str | None:
    """Добавляет сноски в блок section.footnotes в конце файла (создаёт блок, если его нет); None — нет </body>."""
    if "</body>" not in text:
        return None
    block = re.search(r'<section class="footnotes"[^>]*>.*?</section>\s*(?=</body>)', text, re.S)
    if block is None:
        return text.replace("</body>", FOOTNOTES_OPEN + "".join(asides) + "</section></body>", 1)
    whole = block.group(0)
    close = whole.rindex("</section>")
    return text[:block.start()] + whole[:close] + "".join(asides) + whole[close:] + text[block.end():]


def _relink_file(epub: Epub, path: str, text_of: Callable[[str], str],
                 targets_of: Callable[[str], dict[str, tuple[str, dict[str, str]]]],
                 link_style: str, note_style: str) -> tuple[bytes | None, int]:
    """Правка сносок в одном файле главы. Возвращает новые байты (None — файл не меняется, в том числе когда сноски
    некуда поместить) и число правок."""
    used = set(targets_of(path))
    same_file: set[str] = set()
    copies: list[str] = []
    copied: dict[tuple[str, str], str] = {}
    count = [0]

    def relink(match: re.Match[str]) -> str:
        attrs_text = match.group(1)
        attrs = parse_attrs(attrs_text)
        local = _local_link(path, attrs.get("href", "").strip())
        if local is None or not local[1] or local[0] not in epub.names:
            return match.group(0)
        target, frag = local
        hit = targets_of(target).get(frag)
        if hit is None or not _is_note_target(*hit):
            return match.group(0)
        types = attrs.get("epub:type", "").split()
        if target == path:
            if "noteref" in types:
                return match.group(0)
            same_file.add(frag)
            count[0] += 1
            return _retag_a(attrs_text, f"#{frag}", types, link_style)
        if (target, frag) not in copied:
            inner = _element_inner(text_of(target), frag)
            if inner is None:
                return match.group(0)
            copy_id, number = frag, 2
            while copy_id in used:
                copy_id, number = f"{frag}-{number}", number + 1
            used.add(copy_id)
            copied[(target, frag)] = copy_id
            copies.append(f'<aside epub:type="footnote" role="doc-footnote" id="{copy_id}" class="footnote"'
                          f'{_style_attr(note_style)}>{inner}</aside>')
        count[0] += 1
        return _retag_a(attrs_text, f"#{copied[(target, frag)]}", types, link_style)

    new_text = re.sub(r"<a\b([^>]*)>", relink, text_of(path))
    for frag in same_file:
        new_text = re.sub(r"<([\w:.-]+)\b([^>]*\bid\s*=\s*[\"']" + re.escape(frag) + r"[\"'][^>]*)>",
                          lambda m: _mark_footnote(m, note_style), new_text, count=1)
    if copies:
        placed_text = _add_footnotes(new_text, copies)
        if placed_text is None:
            return None, 0
        new_text = placed_text
    if new_text == text_of(path):
        return None, 0
    return new_text.encode("utf-8"), count[0]


def fix_note_links(epub: Epub) -> tuple[dict[str, bytes], int]:
    """Точечная правка сносок без fb2. Ссылка на сноску в том же файле получает epub:type="noteref" и class noteref,
    целевой элемент — epub:type="footnote" и class footnote. Ссылка в другой файл заменяется на копию сноски (aside с
    новым id) в файле ссылки; исходный файл сносок не меняется. Подсказки читалкам (-cr-hint) дописываются в первую
    таблицу стилей книги, а без неё — в атрибут style. Битые href не трогаются.
    Возвращает изменённые файлы (путь → байты) и число правок."""
    css_paths = [item["path"] for item in epub.items.values()
                 if item.get("media-type") == "text/css" and item["path"] in epub.names]
    link_style = "" if css_paths else "-cr-hint: noteref"
    note_style = "" if css_paths else "-cr-hint: footnote-inpage"
    cache: dict[str, dict[str, tuple[str, dict[str, str]]]] = {}
    texts: dict[str, str] = {}

    def text_of(path: str) -> str:
        if path not in texts:
            texts[path] = decode_xml(zip_read(epub.zip, path))
        return texts[path]

    def targets_of(path: str) -> dict[str, tuple[str, dict[str, str]]]:
        if path not in cache:
            cache[path] = _targets(text_of(path))
        return cache[path]

    changed: dict[str, bytes] = {}
    fixed = 0
    for path in _link_files(epub):
        data, hits = _relink_file(epub, path, text_of, targets_of, link_style, note_style)
        if data is not None:
            changed[path] = data
            fixed += hits
    if fixed and css_paths and "-cr-hint: noteref" not in text_of(css_paths[0]):
        changed[css_paths[0]] = (text_of(css_paths[0]) + NOTES_CSS).encode("utf-8")
    return changed, fixed


def book_completeness(path: Path) -> dict[str, bool]:
    """Полнота описания и структуры книги epub или fb2 (True — есть): title, authors, series, annotation, cover, genres,
    lang, year, translator, publisher, isbn, toc, notes; у epub ещё links_ok (внутренние ссылки разрешаются, ссылки на
    сноски — epub:type="noteref" и в том же файле). Только чтение: epub — OPF и главы в пределах ZIP_MEMBER_MAX, fb2 —
    файл целиком до SOURCE_LIMIT. Ошибки чтения — исключения вызывающего."""
    if path.name.lower().endswith(".zip"):
        raise ValueError(f"{path.name}: zip-обёртка не разбирается")
    fmt = split_name(path.name)[1]
    if fmt == "epub":
        with zipfile.ZipFile(path) as archive:
            epub = Epub(archive)
            broken, notes_bad = epub_link_problems(epub)
            return _completeness(epub.meta(), epub_has_toc(epub), epub_has_notes(epub), not broken and not notes_bad)
    if fmt == "fb2":
        data = source_bytes(path)
        return _completeness(fb2_meta(data), *fb2_structure(decode_xml(data)))
    raise ValueError(f"{path.name}: полнота считается для epub и fb2")


def fb2_cover(data: bytes) -> bytes | None:
    """Байты обложки fb2 (картинка из coverpage) или None."""
    book = _Fb2Book(_parse_fb2(data))
    return dict(book.image_data).get(book.cover_name) if book.cover_name else None


def rebuild_epub_from_fb2(run: NightRun, epub_full: Path, fb2_data: bytes, reason: str) -> str | None:
    """Новый epub из fb2 на месте epub_full: сборка fb2_own_epub во временный файл рядом, старый epub — в карантин replaced.
    None — заменено; иначе причина отказа, файлы не тронуты. В dry-run — только проверка сборки во временном каталоге."""
    if run.dry_run:
        with tempfile.TemporaryDirectory(prefix="books_library-rebuild-") as tmp_name:
            return fb2_own_epub(fb2_data, Path(tmp_name) / "check.epub")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{epub_full.name[:80]}.", suffix=".rebuild.tmp", dir=epub_full.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        problem = fb2_own_epub(fb2_data, tmp)
        if problem:
            return problem
        os.chmod(tmp, epub_full.stat().st_mode & 0o777)
        old = run.quarantine(epub_full, "replaced", reason=reason)
        try:
            os.replace(tmp, epub_full)
        except OSError:
            shutil.move(old, epub_full)  # прежний epub возвращается на место
            raise
        return None
    finally:
        tmp.unlink(missing_ok=True)


def upgrade_epub_keeper(run: NightRun, keeper_rel: str, dup_rel: str) -> bool:
    """Перед уходом fb2-дубля в карантин оставленный epub не должен быть беднее fb2. Нет оглавления или сносок у epub,
    которые есть у fb2, — новый epub из fb2 (старый — в replaced). Иначе недостающие метаданные дописываются из fb2
    (старый — копия в edited). True — дубль можно уносить; False — ошибка, fb2 остаётся на месте для решения человека."""
    keeper_full, dup_full = run.root / keeper_rel, run.root / dup_rel
    try:
        data = source_bytes(dup_full)
        meta = read_book_meta(run.root, dup_rel)
        toc_fb, notes_fb = fb2_structure(decode_xml(data))
        with zipfile.ZipFile(keeper_full) as archive:
            epub = Epub(archive)
            toc_ep, notes_ep = epub_has_toc(epub), epub_has_notes(epub)
            broken, notes_bad = epub_link_problems(epub)
        gaps = [name for name, need, have in (("оглавление", toc_fb, toc_ep), ("сноски", notes_fb, notes_ep),
                                              ("ссылки", True, not broken and not notes_bad)) if need and not have]
        if gaps:
            problem = rebuild_epub_from_fb2(run, keeper_full, data, reason=f"rebuilt-from-fb2: {dup_rel}")
            if problem is None:
                run.record("rebuilt", keeper_rel, source=dup_rel, gaps=gaps)
                return True
            run.record("skip", keeper_rel, reason=f"пересборка из fb2 не удалась: {problem}")
        fields, cover = fb2_epub_fields(meta), fb2_cover(data)
        added = enrich_epub_from(keeper_full, fields, cover, dry_run=True)
        if added:
            if not run.dry_run:
                run.backup(keeper_full, "edited")
                enrich_epub_from(keeper_full, fields, cover)
            run.record("enriched", keeper_rel, fields=added, source=dup_rel)
        return True
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
        run.record("skip", keeper_rel, reason=f"{type(error).__name__}: {error}")
        return False


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

# book_meta-ключ отказа конвертации fb2: «причина|размер|mtime_ns». Пока файл не изменился, повторно не конвертируем
# (безнадёжный fb2 не пересобирается каждую ночь); изменился — пробуем снова.
CONVERT_ERROR = "convert_error"


def convert_error_value(reason: str, st: os.stat_result) -> str:
    return f"{' '.join(reason.split())[:300]}|{st.st_size}|{st.st_mtime_ns}"


def stored_convert_error(db_path: str, rel: str) -> str | None:
    """convert_error книги или None: нет БД, схемы v6 или записи. Сбой чтения трактуется как отсутствие записи."""
    if not Path(db_path).is_file():
        return None
    try:
        with closing(connect(db_path, readonly=True)) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_META:
                return None
            row = conn.execute("SELECT value FROM book_meta WHERE path=? AND key=?", (rel, CONVERT_ERROR)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def convert_error_current(value: str, st: os.stat_result) -> bool:
    """Файл не изменился с момента отказа: размер и mtime совпадают с записанными."""
    parts = value.rsplit("|", 2)
    return len(parts) == 3 and parts[1] == str(st.st_size) and parts[2] == str(st.st_mtime_ns)


def set_convert_error(db_path: str, rel: str, value: str | None) -> None:
    """Записывает convert_error книги (value) или снимает его (None)."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM book_meta WHERE path=? AND key=?", (rel, CONVERT_ERROR))
        if value is not None:
            conn.execute("INSERT INTO book_meta VALUES (?,?,?)", (rel, CONVERT_ERROR, value))


def convert_fb2(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None, workers: int | None = None,
                out: Callable[[str], None] = print) -> dict[str, Any]:
    """fb2 и однокнижный zip с fb2 → name.epub рядом (имя как у fb2), проверка verify_book, исходник — в карантин
    replaced/. Рядом уже есть epub с тем же нормализованным именем — исходник сразу в replaced/ без конвертации.
    Сначала собственный конвертер fb2_to_epub3; при его ошибке или провале проверок — Calibre. Поле engine в журнале:
    fb2epub3 или calibre. Сбой обоих или проверки — fb2 остаётся на месте (решение пользователя 18.11.2026):
    строка rejected-convert в журнале и book_meta convert_error; пока файл не изменился, он пропускается
    (skipped-rejected)."""
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
        st = src.stat()  # подпись до чтения: правка во время конвертации не попадёт в convert_error
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
        rel = run.rel(src)
        marker = stored_convert_error(cfg["db_path"], rel)
        if existing or result.exists():
            keeper = src.parent / (existing or result.name)
            if marker is not None and not dry_run:  # маркер уходит с исходником, а не переезжает на epub при relink
                set_convert_error(cfg["db_path"], rel, None)
            run.quarantine(src, "replaced", reason="рядом уже есть epub", keeper=str(keeper))
            run.moved(src, keeper)
            return
        if not name_ok(result.name):
            run.quarantine(src, "rejected-name", reason="имя epub недопустимо")
            return
        if marker is not None and convert_error_current(marker, st):
            run.record("skipped-rejected", src, reason=marker.rsplit("|", 2)[0])
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
            if problem:  # fb2 не трогаем: отказ — журнал и convert_error
                try:
                    set_convert_error(cfg["db_path"], rel, convert_error_value(problem, st))
                except sqlite3.Error as error:
                    run.record("failed", src, reason=f"convert_error: {error}")
                run.record("rejected-convert", src, reason=problem, engine=engine)
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
        if marker is not None:  # прежний отказ уходит с исходником, а не переезжает на epub
            set_convert_error(cfg["db_path"], rel, None)
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


# Метка собственного конвертера fb2_to_epub3 в OPF (<meta name="generator">); у выпусков до неё — манифест ниже.
FB2_CONVERTER_GENERATOR = "books_library fb2_to_epub3"
EPUB_CONVERTED_CACHE: dict[tuple[str, int, int], bool] = {}


def calibre_marked(opf: str) -> bool:
    """Метка Calibre: dc:contributor opf:role="bkp" с текстом calibre."""
    return any(re.search(r'\bopf:role\s*=\s*"bkp"', attrs) and strip_tags(body).startswith("calibre")
               for attrs, body in re.findall(r"<dc:contributor\b([^>]*)>(.*?)</dc:contributor>", opf, re.S))


def converted_opf(opf: str) -> bool:
    """Epub сделан конвертером, а не издателем: Calibre; fb2_to_epub3 (meta generator или фиксированный манифест
    css/book.css + title-page); pandoc (unique-identifier="epub-id-1" в package)."""
    if f'content="{FB2_CONVERTER_GENERATOR}"' in opf:
        return True
    if '<item id="css" href="css/book.css"' in opf and '<item id="title-page" href="text/title.xhtml"' in opf:
        return True
    return 'unique-identifier="epub-id-1"' in opf or calibre_marked(opf)


def epub_converted(path: Path) -> bool:
    """Признак «сконвертирован из fb2/doc/html» для epub (см. converted_opf). Кэш по (путь, размер, mtime) на время
    прогона. Нечитаемый epub — не сконвертирован: сортировка для него не меняется."""
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in EPUB_CONVERTED_CACHE:
            with zipfile.ZipFile(path) as archive:
                EPUB_CONVERTED_CACHE[key] = converted_opf(Epub(archive).opf)
        return EPUB_CONVERTED_CACHE[key]
    except (OSError, KeyError, ValueError, EOFError, zipfile.BadZipFile):
        return False


def epub_opf_facts(epub: Path) -> dict[str, Any]:
    """Признаки из OPF epub (читается только он): метка calibre в dc:contributor opf:role="bkp", дата
    calibre:timestamp, название и первый автор."""
    with zipfile.ZipFile(epub) as archive:
        opf = Epub(archive).opf
    calibre = calibre_marked(opf)
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
            dest = free_variant_name(cfg, target, f"{cand['stem']}.epub", set(), src=cand["full"])
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

    def __init__(self, conn: sqlite3.Connection, root: Path | None = None) -> None:
        self.conn = conn
        self.root = root  # каталог книг: запись чужого пути берётся, только если того файла уже нет (переименован)
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
        return TextCache(conn, Path(cfg["books_dir"]) if cfg.get("books_dir") else None)

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
                        "SELECT rowid, data, path FROM texts WHERE size = ? AND mtime BETWEEN ? AND ? AND fmt = ?",
                        (size, *window, fmt)).fetchall()
                    # файл с той же записью ещё на месте — это другая книга того же размера, а не переименование
                    if len(found) != 1 or (self.root is not None and (self.root / found[0][2]).exists()):
                        return None
                    row = found[0][:2]
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
        # уточнение в скобках от free_variant_name (конфликт имён при переносе) не мешает найти кандидата; решает сравнение текста
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
        # признак «сконвертирован» читается только для групп из двух и более epub
        epub_group = sum(metas[index]["fmt"] == "epub" for index in group) >= 2
        with tempfile.TemporaryDirectory(prefix="books_library-dedup-") as tmp_name:
            for index in group:
                rel, size, mtime = files[index]
                fmt = metas[index]["fmt"]
                converted = epub_group and fmt == "epub" and epub_converted(root / rel)
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
                                "authors": metas[index]["authors"], "converted": converted})
        members.sort(key=lambda m: (FORMAT_RANK.get(m["fmt"], 5), m["converted"], -m["words"], -m["size"]))
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
            duplicate = keeper is not None and member["words"] >= DEDUP_MIN_WORDS and best >= DEDUP_COVERAGE
            if duplicate and keeper["fmt"] == "epub" and member["fmt"] == "fb2":
                duplicate = upgrade_epub_keeper(run, keeper["path"], member["path"])  # False — fb2 остаётся
            if duplicate:
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


SAME_NAME_SUFFIX = re.compile(r"_[1-9][0-9]?$")  # хвост «_N» (N — 1..99) у копии с тем же именем
LATIN_SMALL_WORDS = frozenset({"a", "an", "the", "of", "and", "in", "on", "to", "for"})  # служебные слова Title Case


def same_name_base(rel: str) -> str:
    """Основа имени книги без расширения и хвоста «_N»: «Белка_1.epub» → «Белка»."""
    return SAME_NAME_SUFFIX.sub("", split_name(rel)[0])


def same_name_key(rel: str) -> str:
    """Ключ одноимённости для dedup-folders: основа без хвоста «_N» и без учёта регистра; расширения не сравниваются."""
    return same_name_base(rel).casefold()


def case_score(stem: str) -> tuple[int, int]:
    """Оценка регистра названия, больше — лучше. Название — часть после « - », если она есть, иначе вся основа.
    Кириллица — «как в предложении»: первая буква заглавная, в остальных словах меньше заглавных.
    Латиница — Title Case: заглавные у слов, кроме служебных (a, an, the, of…); служебные строчными."""
    words = stem.split(" - ", 1)[-1].split()
    if not words:
        return (0, 0)
    if CYRILLIC.search(stem):
        return (int(words[0][0].isupper()), -sum(int(word[0].isupper()) for word in words[1:]))
    score = 0
    for index, word in enumerate(words):
        capital = int(word[0].isupper())
        if index == 0 or word.lower() not in LATIN_SMALL_WORDS:
            score += capital
        else:
            score -= capital
    return (score, 0)


def better_case(bases: list[str]) -> str:
    """Лучшее по регистру основа из одноимённых. Первая — основа оставленного (keeper): при ничьей остаётся она."""
    best = bases[0]
    for base in bases[1:]:
        if case_score(base) > case_score(best):
            best = base
    return best


def same_name_renames(texts: list[dict[str, Any]]) -> dict[str, str]:
    """Оставленный файл с одноимёнными дублями (same_name) получает лучшее по регистру имя группы (better_case):
    основа без «_N» и расширение оставленного. Возвращает: старый путь → новое имя, только если имя меняется."""
    renames: dict[str, str] = {}
    for keeper in texts:
        if keeper["role"] != "kept":
            continue
        group = [t["path"] for t in texts if t.get("same_name") and t["role"] == "duplicate" and t["keeper"] == keeper["path"]]
        if not group:
            continue
        best = better_case([same_name_base(keeper["path"])] + [same_name_base(path) for path in group])
        name = PurePosixPath(keeper["path"]).name
        new_name = best + name[len(split_name(keeper["path"])[0]):]
        if new_name != name and name_ok(new_name):
            renames[keeper["path"]] = new_name
    return renames


def name_taken_ci(directory: Path, name: str, own: Path, leaving: set[Path]) -> bool:
    """Занято ли имя в каталоге без учёта регистра другим файлом: свой файл и уходящие в карантин не считаются."""
    return any(entry.casefold() == name.casefold() and directory / entry not in leaving and directory / entry != own
               for entry in os.listdir(directory))


def mark_roles(texts: list[dict[str, Any]]) -> None:
    """Жадный проход по значимости (формат → нативный раньше сконвертированного → кириллическое имя → больше слов →
    путь). Книга — дубль, если ≥ 97 % её шинглов уже есть у оставленных и похожих и в ней ≥ 200 слов; похожая — от 85 %
    до 97 %; иначе оставлена. Похожая с тем же именем без «_N» и регистра, что у keeper, — дубль (same_name): копия того же
    текста. Без текста оставлена и в сравнении не участвует. У дубля и похожего keeper — оставленный с наибольшей долей
    общих шинглов."""
    texts.sort(key=lambda t: (FORMAT_RANK.get(t["fmt"], 5), t.get("converted", False),
                              not CYRILLIC.search(split_name(t["path"])[0]), -t["words"], t["path"]))
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
        keeper = max(scored, key=lambda pair: pair[0])[1]["path"]
        same = role == "similar" and same_name_key(item["path"]) == same_name_key(keeper)
        item.update(role="duplicate" if same else role, keeper=keeper, same_name=same)
        if role == "similar" and not same:
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
    # признак «сконвертирован» читается только для папок из двух и более epub
    epub_folder = sum(t["fmt"] == "epub" for t in texts) >= 2
    for item in texts:
        item["converted"] = epub_folder and item["fmt"] == "epub" and epub_converted(run.root / item["path"])
    mark_roles(texts)
    for item in texts:
        if not item["s"]:
            run.record("no-text", item["path"], reason=item["reason"] or "текста нет")
        elif item["role"] == "similar":
            run.record("similar", item["path"], keeper=item["keeper"], coverage=item["coverage"])
    by_rel = {t["path"]: t for t in texts}
    for item in texts:
        if item["role"] != "duplicate":
            continue
        keeper_item = by_rel.get(item["keeper"])
        if keeper_item is not None and keeper_item["fmt"] == "epub" and item["fmt"] == "fb2" \
                and not upgrade_epub_keeper(run, item["keeper"], item["path"]):
            item["role"] = "kept"  # fb2 остаётся на месте
            continue
        extra = {"reason": "same-name"} if item.get("same_name") else {}
        try:
            dest = run.quarantine(run.root / item["path"], "duplicate", keeper=item["keeper"], coverage=item["coverage"],
                                  **extra)
        except OSError as error:
            run.record("failed", item["path"], reason=f"карантин: {error}")
            item["role"] = "kept"  # файл остался на месте: его связи и группа не меняются
            continue
        item["quarantine"] = str(dest)
    leaving = {run.root / t["path"] for t in texts if t["role"] == "duplicate"}
    taken: set[Path] = set()
    moves: dict[str, str] = {}
    renames = keeper_renames(texts)
    for old, new_name in same_name_renames(texts).items():
        full = run.root / old
        if old not in renames and not name_taken_ci(full.parent, new_name, full, leaving):
            renames[old] = new_name  # занятое без учёта регистра имя оставляем для strip-suffix
    for old, new_name in renames.items():
        full = run.root / old
        target = free_variant_name(run.cfg, full.parent, new_name, taken, src=full, leaving=leaving)
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


# ----------------------------------------------------------------------
# enrich-epub: дополнение уже сделанной библиотеки метаданными и структурой fb2
# ----------------------------------------------------------------------

def variant_fb2_meta(row: sqlite3.Row) -> dict[str, Any]:
    """Метаданные fb2-варианта: meta JSON book_variants, дополненный колонками (series, series_no, lang, year, authors)."""
    try:
        loaded = json.loads(row["meta"] or "{}")
    except ValueError:
        loaded = {}
    meta: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
    for column in ("series", "series_no", "lang", "year"):
        if not meta.get(column) and row[column] not in (None, ""):
            meta[column] = row[column]
    if not meta.get("authors") and row["authors"]:
        meta["authors"] = [name for name in row["authors"].split("; ") if name]
    return meta


def fb2_sources(conn: sqlite3.Connection | None, rel: str, qroot: Path,
                by_keeper: dict[str, list[str]]) -> list[tuple[dict[str, Any], Path | None]]:
    """fb2-дубли epub: строки book_variants (meta JSON и колонки) и дубли из dup_members, которых там ещё нет.
    Второй элемент — файл в карантине, если он ещё есть (тогда доступны обложка и пересборка), иначе None."""
    if conn is None:
        return []
    sources: list[tuple[dict[str, Any], Path | None]] = []
    seen: set[str] = set()
    for row in book_variants(conn, rel):
        if row["fmt"] != "fb2":
            continue
        seen.add(row["variant_path"])
        full = qroot / row["variant_path"]
        sources.append((variant_fb2_meta(row), full if full.is_file() else None))
    for variant in by_keeper.get(rel, []):
        if variant in seen or split_name(variant)[1] != "fb2" or not (qroot / variant).is_file():
            continue
        reason, _, inner = variant.partition("/")
        sources.append((read_book_meta(qroot / reason, inner), qroot / variant))
    return sources


def enrich_epub_one(run: NightRun, rel: str, sources: list[tuple[dict[str, Any], Path | None]]) -> None:
    """Один epub. Пробелы оглавления, сносок и ссылок при fb2 в карантине — пересборка из fb2. Иначе ссылки на сноски
    правятся точечно (fix_note_links), битые href не трогаются и считаются в журнале (broken_links). Затем дописываются
    недостающие метаданные из источников fb2 (enrich_epub_from)."""
    epub_full = run.root / rel
    fields = merge_enrich_fields([fb2_epub_fields(meta) for meta, _full in sources])
    with_file = [full for _meta, full in sources if full is not None]
    data = source_bytes(with_file[0]) if with_file else None
    with zipfile.ZipFile(epub_full) as archive:
        epub = Epub(archive)
        toc_ep, notes_ep = epub_has_toc(epub), epub_has_notes(epub)
        broken, notes_bad = epub_link_problems(epub)
        note_payloads, fixed = fix_note_links(epub)
    links_ep = not broken and not notes_bad
    if data is not None:
        toc_fb, notes_fb = fb2_structure(decode_xml(data))
        gaps = [name for name, need, have in (("оглавление", toc_fb, toc_ep), ("сноски", notes_fb, notes_ep),
                                              ("ссылки", True, links_ep)) if need and not have]
        if gaps:
            problem = rebuild_epub_from_fb2(run, epub_full, data, reason=f"rebuilt-from-fb2: {with_file[0].name}")
            if problem is None:
                run.record("rebuilt", rel, gaps=gaps, source=str(with_file[0]), broken_links=len(broken))
                return
            run.record("skip", rel, reason=f"пересборка из fb2 не удалась: {problem}")
    cover = fb2_cover(data) if data is not None else None
    added = enrich_epub_from(epub_full, fields, cover, dry_run=True)
    if not added and not note_payloads:
        run.record("skip", rel, reason="недостающих полей и правок ссылок нет", broken_links=len(broken))
        return
    if not run.dry_run:
        run.backup(epub_full, "edited")
        if note_payloads:
            _zip_rewrite(epub_full, note_payloads)
        if added:
            enrich_epub_from(epub_full, fields, cover)
    run.record("enriched", rel, fields=added, note_links=fixed, broken_links=len(broken))


def enrich_epub(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                out: Callable[[str], None] = print) -> dict[str, Any]:
    """Режим enrich-epub: epub в папках авторов дополняются метаданными и структурой своих fb2 (book_variants и карантин,
    dup_members). Недостающие поля дописываются enrich_epub_from; нет оглавления или сносок и fb2 ещё в карантине —
    пересборка из fb2. Старый epub — в replaced или копия в edited. Журнал: enriched, rebuilt, skip, failed.
    Ошибка одной книги не прерывает прогон."""
    run = NightRun(cfg, "enrich-epub", dry_run, path, out)
    root = run.root
    if not run.target.resolve().is_relative_to(root.resolve()):
        raise ConfigError("enrich-epub: --path должен быть внутри каталога книг")
    errors: list[str] = []
    by_keeper: dict[str, list[str]] = {}
    conn: sqlite3.Connection | None = None
    if Path(cfg["db_path"]).is_file():
        _books, dups = load_variant_context(cfg["db_path"], (run.qroot,))
        for variant, (keeper, _words) in dups.items():
            by_keeper.setdefault(keeper, []).append(variant)
        conn = connect(cfg["db_path"], readonly=True)
    try:
        for item_rel, _size, _mtime in iter_book_files(run.target, errors):
            rel = run.rel(run.target / item_rel)
            if split_name(rel)[1] != "epub" or rel.lower().endswith(".zip"):
                continue
            if path is None and not author_scope((root / rel).parent, root):
                continue
            run.tick()
            try:
                enrich_epub_one(run, rel, fb2_sources(conn, rel, run.qroot, by_keeper))
            except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as error:
                run.record("failed", rel, reason=f"{type(error).__name__}: {error}")
    finally:
        if conn is not None:
            conn.close()
    return run.finish({"walk_errors": errors[:200]})


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
# unzip-books: однокнижный name.ext.zip → name.ext (решение 17.11.2026: книги хранятся распакованными)
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
        os.utime(tmp, (st.st_atime, st.st_mtime))  # дата книги — дата архива
        with run.lock:
            target = free_variant_name(run.cfg, archive.parent, name, set(), src=archive)
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
            target = (free_variant_name(run.cfg, archive.parent, name, set(), src=archive) if dry_run
                      else unzip_extract(run, archive, info, name))
            if target.name != name:
                run.count("conflict")  # имя занято другим содержимым: книга — с уточнением в скобках
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

CLI_COMMANDS = ("unpack-collections", "merge-authors", "sort-underscore", "misplaced", "check-folders", "purge-quarantine",
                "move-books", "export-decisions", "import-decisions")
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
COAUTHOR_SPLIT = re.compile(r", | и ")  # соавторы внутри одной записи автора: «Иван Иванов, Пётр Петров», «А и Б»
INITIALS_WORD = re.compile(r"^((?:[А-ЯЁA-Z]\.)+)([А-ЯЁA-Z][а-яёa-z-]+)$")  # «И.Гарин», «И.О.Гарин» — инициалы у фамилии


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
    «д'»/«л'»/«o'», начинается с заглавной буквы; многословное имя целиком заглавными — годно при любой длине;
    одно слово целиком заглавными — аббревиатура, не имя; в смешанном
    регистре заглавных после первой буквы не больше половины (КОИ8-кракозябры вида «вБИТПЫЙО» отвергаются).
    Проверяется часть слова до первого дефиса. Слова без букв (инициалы с точкой и т. п.) пропускаются.
    Инициалы («А.», «Р») — годны. Нет ни одного проверенного слова — False."""
    checked = 0
    acronym = False
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
        checked += 1
        if part.isupper():
            acronym = len(letters) > 1
            continue
        rest = letters[1:]
        if sum(ch.isupper() for ch in rest) * 2 > len(rest):
            return False
    if checked == 1 and acronym:
        return False  # «DNS», «БСЭ»: единственное слово заглавными — аббревиатура
    return checked > 0


def split_authors(authors: str) -> list[str]:
    """Авторы книги из поля authors: записи через «; »; внутри записи «, » или « и » — соавторы, но только если каждая
    часть похожа на имя (person_name). Иначе запись остаётся целой: «Охота на драконов, эпос Японский» — не делится."""
    result: list[str] = []
    for entry in authors.split("; "):
        entry = entry.strip()
        if not entry:
            continue
        parts = [part.strip() for part in COAUTHOR_SPLIT.split(entry)]
        if len(parts) > 1 and all(person_name(part) for part in parts):
            result.extend(parts)
        else:
            result.append(entry)
    return result


def folder_name(name: str) -> str:
    """Имя папки для Windows и Syncthing: без запрещённых и управляющих символов, без пробелов и точек по краям,
    не длиннее FOLDER_NAME_MAX; зарезервированное имя устройства Windows получает «_». Пустой результат — имени нет."""
    clean = FOLDER_BAD_CHARS.sub("", name).strip(" .")[:FOLDER_NAME_MAX].strip(" .")
    return clean + "_" if clean.partition(".")[0].upper() in WINDOWS_RESERVED else clean


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
    (последнее слово автора первым). Пустое имя у new — имени нет. Автор «И.Гарин» (инициалы у фамилии) — существующая
    папка по initials («initials»), иначе новая «Гарин И»; частица из двух слов («Де Рокамболь») не переставляется.
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
    initials = INITIALS_WORD.match(author.strip())
    if initials is not None:  # «И.Гарин» → «Гарин И.» (folder_name уберёт точку на конце)
        surname_form = f"{initials.group(2)} {initials.group(1)}"
        found = initials_folder(surname_form, index)
        return (found, "initials") if found else (folder_name(surname_form), "new")
    if len(words) == 2 and words[0].casefold() in AUTHOR_PARTICLES:
        return folder_name(author), "new"  # «Де Рокамболь»: частица — часть фамилии, не переставляется
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
# Слово имени: «Имя» с заглавной и строчными или фамилия КАПСОМ
TEXT_WORD = r"(?:[А-ЯЁ][а-яё]+|[А-ЯЁ]{2,})"
# «Имя Фамилия», «ИМЯ ФАМИЛИЯ», «Имя ФАМИЛИЯ»; соавторы через запятую отбрасываются (берётся первый), точка — необязательна
# отдельной строкой в первых TEXT_ALONE_LINES строках + название следующей строкой
TEXT_NAME_ALONE = re.compile(rf"^\s*({TEXT_WORD}(?:[ \t]+{TEXT_WORD}){{1,2}})\.?(?:,\s*\S.*)?\s*$")
TEXT_INITIALS_ALONE = re.compile(r"^\s*((?:[А-ЯЁ]\.\s*){1,2})([А-ЯЁ][а-яё]+|[А-ЯЁ]{2,})\.?\s*$")  # «И. О. Фамилия»
TEXT_SURNAME_INITIALS = re.compile(r"^\s*([А-ЯЁ][а-яё]+|[А-ЯЁ]{2,})\s+((?:[А-ЯЁ]\.\s*){1,2})\s*$")  # «Фамилия И. О.»
TEXT_ALONE_LINES = 5
TEXT_SOURCE_FORMATS = ("txt", "fb2", "epub")  # форматы, из которых sort-underscore берёт автора из текста
SERIAL_AUTHOR = re.compile(r"\)\s*([а-яёa-z]+)\s*$")  # «Война Роботов 01(подзаголовок)кейт» — фамилия строчными после «)»
FOLDER_NUMBER = re.compile(r"^\d+[.)]\s*")  # номер в начале папки: «1. », «2) »
FOLDER_COAUTHORS = re.compile(r"\s+и\s+|\s*[,&]\s*")  # соавторы в папке: «… и …», «,», «&»
FOLDER_INITIALS = re.compile(r"^((?:[А-ЯЁ]\.)+)([А-ЯЁ][а-яё]+)$")  # «М.Уэйс», «М.Н.Загоскин»


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
                all(word.startswith(initial) for word, initial in zip(words[1:], initials, strict=False)):
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


def serial_filename_folder(stem: str, index: dict[frozenset[str], str]) -> str:
    """Папка автора для выпуска серии «Война Роботов 01(шагающая Смерть 1)кейт»: фамилия строчными после последней «)».
    Принимается, только если среди папок верхнего уровня ровно одна с такой фамилией (ё→е); иначе ''."""
    match = SERIAL_AUTHOR.search(stem)
    if match is None:
        return ""
    surname = author_norm(match.group(1))
    found = [folder for folder in index.values() if author_words(folder)[:1] == [surname]]
    return found[0] if len(found) == 1 else ""


def folder_author_name(folder: str) -> str:
    """Имя автора из папки «1. М.Уэйс и Т.Хикмен» или «2) Р.Сальваторе»: без номера, первый автор; «М.Уэйс» → «Уэйс М.»."""
    first = FOLDER_COAUTHORS.split(FOLDER_NUMBER.sub("", folder, count=1), maxsplit=1)[0].strip()
    match = FOLDER_INITIALS.match(first)
    return f"{match.group(2)} {''.join(match.group(1).split())}" if match is not None else first


def alone_name(line: str) -> str:
    """Имя из строки-одиночки: «Имя Фамилия», «ФАМИЛИЯ ИМЯ», «И. О. Фамилия» → «Фамилия И.О.», «Фамилия И. О.» → «Фамилия И.О.»
    (форма для folder_for_name). Не имя — ''."""
    match = TEXT_NAME_ALONE.match(line)
    if match is not None:
        return match.group(1)
    match = TEXT_INITIALS_ALONE.match(line)
    if match is not None:
        return f"{match.group(2)} {''.join(match.group(1).split())}"
    match = TEXT_SURNAME_INITIALS.match(line)
    if match is not None:
        return f"{match.group(1)} {''.join(match.group(2).split())}"
    return ""


def text_names(head: str) -> Iterator[str]:
    """Имена-кандидаты из начала текста по порядку: «Имя Фамилия. Название», «ИМЯ ФАМИЛИЯ» (название — следующей
    строкой) или «Имя Фамилия[.]» в первых TEXT_ALONE_LINES строках."""
    lines = [line for line in head.splitlines() if line.strip()][:40]
    for position, line in enumerate(lines):
        match = TEXT_NAME_LINE.match(line)
        name = match.group(1) if match is not None else ""
        if not name and position + 1 < len(lines):
            match = TEXT_CAPS_LINE.match(line)
            name = match.group(1) if match is not None else ""
            if not name and position < TEXT_ALONE_LINES:
                name = alone_name(line)
        if name:
            yield name


def text_author(head: str, index: dict[frozenset[str], str]) -> tuple[str, str]:
    """Автор из начала текста: первый кандидат из text_names, совпавший с существующей папкой.
    Возвращает (папка, имя); иначе ('', '')."""
    for name in text_names(head):
        folder = folder_for_name(name, index)
        if folder:
            return folder, name
    return "", ""


def epub_head(full: Path, limit: int) -> str:
    """Начало epub: главы spine по порядку, пока не набрано limit символов — книга целиком не читается."""
    parts: list[str] = []
    size = 0
    with zipfile.ZipFile(full) as archive:
        for path in Epub(archive).spine():
            chunk = re.sub(r"<[^>]*>", " ", decode_xml(zip_read(archive, path)))
            parts.append(chunk)
            size += len(chunk)
            if size >= limit:
                break
    return "\n".join(parts)  # строки сохраняются: имя-строка ищется по строкам (text_names)


def text_head(cfg: dict[str, Any], full: Path, fmt: str) -> str:
    """Начало текста книги (TEXT_HEAD_CHARS символов). Прямой epub — по главам до лимита; однокнижный zip — через book_text."""
    if fmt == "epub" and not full.name.lower().endswith(".zip"):
        return epub_head(full, TEXT_HEAD_CHARS)[:TEXT_HEAD_CHARS]
    with tempfile.TemporaryDirectory(prefix="books_library-sort-underscore-") as tmp_name:
        return book_text(cfg, full, fmt, Path(tmp_name))[:TEXT_HEAD_CHARS]


PERIODICALS_FILE = Path(__file__).resolve().parent / "periodicals.json"  # «автор из метаданных» → папка журнала
PERIODICAL_WORDS = frozenset({"газета", "газеты", "газету", "газетой", "газетам", "журнал", "журнала", "журналы",
                              "журналу", "журналом", "журнале"})  # слово, по которому автор — издание
PERIODICAL_NOISE = PERIODICAL_WORDS | {"оглавление"}  # слова, которые убираются из имени издания по умолчанию
PERIODICAL_QUOTES = str.maketrans({ch: " " for ch in "«»\"“”„"})
UNSORTED_SECTION = "_1tmp"  # безавторские книги разделов «_Сборники» и «_разобрать» (sort-underscore)
EXACT_MATCHES = frozenset({"periodical", "unsorted"})  # переносы без «_N»: занятое имя — conflict, книга на месте


def load_periodicals(path: Path | None = None) -> dict[str, str]:
    """Таблица «автор из метаданных» → «_Журналы/Издание». Файла нет — пустая таблица (работает правило по умолчанию).
    Битый файл и значения вне «_Журналы/…» — ошибка конфигурации: прогон не начинается."""
    path = path or PERIODICALS_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: не читается как JSON: {error}") from error
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ConfigError(f"{path}: нужен объект «автор» → «папка» из строк")
    for folder in data.values():
        parts = PurePosixPath(folder).parts
        if not (len(parts) >= 2 and parts[0] == PERIODICALS_SECTION):
            raise ConfigError(f"{path}: папка «{folder}» вне «{PERIODICALS_SECTION}/…»")
        if any(folder_name(part) != part for part in parts):  # Windows-имена (Syncthing), без «..» и абсолютных путей
            raise ConfigError(f"{path}: папка «{folder}» не проходит правила имён библиотеки")
    return dict(data)


def is_periodical(author: str) -> bool:
    """Автор похож на издание: есть слово «газета» или «журнал» (любой регистр)."""
    return any(word in PERIODICAL_WORDS for word in re.findall(r"\w+", author.casefold()))


def periodical_folder(author: str, table: dict[str, str]) -> str:
    """Папка журнала: из таблицы; иначе — имя издания без слов «газета»/«журнал»/«оглавление» и кавычек, повторы слов
    схлопнуты (первое вхождение), folder_name() → «_Журналы/<имя>». Пустое имя — пустая строка."""
    if author in table:
        return table[author]
    kept: list[str] = []
    seen: set[str] = set()
    for word in author.translate(PERIODICAL_QUOTES).split():
        low = word.casefold()
        if low in PERIODICAL_NOISE or low in seen:
            continue
        seen.add(low)
        kept.append(word)
    name = folder_name(" ".join(kept))
    return f"{PERIODICALS_SECTION}/{name}" if name else ""


def unsorted_rel(rel: str) -> str:
    """Путь в «_1tmp» для безавторской книги «_Сборники»/«_разобрать»: без имени верхнего сборника (второго сегмента),
    если внутри раздела есть вложенность: «_разобрать/Библиотека Мошкова/Мошков часть 4/LIB/x.txt» →
    «_1tmp/Мошков часть 4/LIB/x.txt»; «_Сборники/Сборник/a.epub» → «_1tmp/a.epub»; «_разобрать/a.txt» → «_1tmp/a.txt»."""
    parts = PurePosixPath(rel).parts
    rest = parts[2:] if len(parts) >= 3 else parts[1:]
    return PurePosixPath(UNSORTED_SECTION, *rest).as_posix()


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
    for scope, prefix in zip(scopes, prefixes, strict=True):
        for rel, _size, _mtime in iter_book_files(scope, errors):
            if prefix + rel not in books:
                run.count("not_book")
    pre_dirs = {entry.name for entry in root.iterdir() if entry.is_dir()}
    index = author_index(root)
    text_cache = TextCache.open(cfg) if "text" in sources else None  # сначала кэш текста dedup, затем чтение книги
    created: set[str] = set()
    taken: set[Path] = set()
    planned_gone: set[str] = set()
    pending: list[tuple[str, str]] = []
    breakdown: dict[str, dict[str, int]] = {}  # раздел → источник → число переносов
    examples: dict[str, list[str]] = {source: [] for source in sources}
    periodicals = load_periodicals()  # один раз на прогон

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

    def note_done(rel_src: str, dst_rel: str, label: str, source: str, who: str, match: str) -> None:
        """Счётчики перенесённого: безавторская книга — в to_unsorted (без источника), издание — в periodicals."""
        if match == "unsorted":
            run.count("moved")
            run.count("to_unsorted")
            return
        note_moved(rel_src, dst_rel, label, source, who)
        if match == "periodical":
            run.count("periodicals")

    for rel in candidates:
        src = root / rel
        top = root / PurePosixPath(rel).parts[0]
        label = section_name(top.name)
        authors = split_authors(books[rel]["authors"])  # первый — автор папки, остальные — соавторы (copy_coauthors)
        author = authors[0] if authors else ""
        key = author_key(author) if author else None
        parent = "" if src.parent == top or src.parent.name.startswith(("_", ".")) else src.parent.name
        folder, match, source, found_by, name, failure = "", "", "", "", "", ""
        if "meta" in sources and is_periodical(author):  # издание — в «_Журналы», не в папку автора
            folder = periodical_folder(author, periodicals)
            if not folder:
                run.count("no_author")
                journal_row(run, {"from": rel, "path": rel, "to": "", "author": author, "folder": "", "match": "",
                                  "source": "", "status": "no-author", "error": "издание без имени"})
                continue
            match, source, found_by = "periodical", "meta", author
        elif "meta" in sources and key is not None:
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
            folder = folder_for_name(folder_author_name(parent), index)
            if folder:
                source, found_by, match = "folder", parent, "existing"
        if not folder and "filename" in sources:
            base_name = split_name(rel)[0]
            name = filename_author(base_name)
            folder = folder_for_name(name, index) if name else ""
            if folder:
                source, found_by, match = "filename", name, "existing"
            else:
                folder = serial_filename_folder(base_name, index)
                if folder:
                    source, found_by, match = "filename", folder, "existing"
        if not folder and "text" in sources and books[rel]["fmt"] in TEXT_SOURCE_FORMATS:
            try:
                cached = text_cache.get(rel, books[rel]["size"], src.stat().st_mtime, books[rel]["fmt"]) if text_cache else None
                head = cached[:TEXT_HEAD_CHARS] if cached is not None else text_head(cfg, src, books[rel]["fmt"])
                folder, name = text_author(head, index)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError, subprocess.TimeoutExpired) as error:
                failure = f"{type(error).__name__}: {error}"
            if folder:
                source, found_by, match = "text", name, "existing"
        if not folder and kind == "sort-underscore" and top.name in ("_" + COLLECTIONS_SECTION, RAZOBRAT_SECTION):
            folder, match = PurePosixPath(unsorted_rel(rel)).parent.as_posix(), "unsorted"  # без автора → «_1tmp»
        if not folder:
            run.count("no_author")
            journal_row(run, {"from": rel, "path": rel, "to": "", "author": author, "folder": "", "match": "",
                              "source": "", "status": "no-author", "error": failure})
            continue
        co_authors = authors[1:] if source == "meta" and match != "periodical" else []  # копии — у каждого следующего
        exact = match in EXACT_MATCHES  # журнал и «_1tmp»: имя без «_N», занятое — conflict
        if not exact:
            fkey = author_key(folder)
            if fkey is not None:
                index.setdefault(fkey, folder)
            if folder in pre_dirs:
                run.count("into_existing")
            elif folder not in created:
                created.add(folder)
                run.count("new_folders")
        directory = root / folder
        if exact:
            dest = directory / src.name
            if dest == src:  # уже в своей папке (журнал или «_1tmp»): ничего не переносим
                continue
            if dest.exists() or dest in taken:
                run.count("conflicts")
                journal_row(run, {"from": rel, "path": rel, "to": dest.relative_to(root).as_posix(),
                                  "author": author, "folder": folder, "match": match, "source": source,
                                  "status": "conflict", "error": ""})
                continue
            taken.add(dest)
        else:
            dest = free_variant_name(cfg, directory, src.name, taken, src=src)
        if dest.name != src.name:
            run.count("conflicts")
        dst_rel = dest.relative_to(root).as_posix()
        base = {"from": rel, "path": rel, "to": dst_rel, "author": found_by or author, "folder": folder,
                "match": match, "source": source}
        if dry_run:
            planned_gone.add(str(src))
            note_done(rel, dst_rel, label, source, found_by or author, match)
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
            note_done(rel, dst_rel, label, source, found_by or author, match)
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
    if kind == "sort-underscore":
        line += f", журналы {s.get('periodicals', 0)}, в «{UNSORTED_SECTION}» {s.get('to_unsorted', 0)}"
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


PATRONYMIC_RE = re.compile(r"(?:ович|евич|ьич|ич|овна|евна|ична|инична|оглы|кызы)$", re.I)  # отчество по последнему слову
HEAD_WORD_RE = re.compile(r"[А-ЯЁ][а-яё]+|[А-ЯЁ]{2,}")  # слово имени в начале текста книги
HEAD_LINES = 40  # отчество ищется в первых стольких непустых строках начала текста книги


def is_patronymic(name: str) -> bool:
    """Результат patronymic_full из метаданных других книг или из текста принимается, только если третье слово — отчество."""
    words = author_words(name)
    return len(words) == 3 and PATRONYMIC_RE.search(words[2]) is not None


def global_patronymics(db_path: str) -> dict[frozenset[str], set[str]]:
    """Индекс имён из метаданных всех книг БД: пара слов трёхсловного имени автора → имена как в метаданных.
    Строится один раз на прогон merge-authors. Без базы — пусто."""
    index: dict[frozenset[str], set[str]] = {}
    if not Path(db_path).is_file():
        return index
    with closing(connect(db_path, readonly=True)) as conn:
        rows = conn.execute("SELECT authors FROM books WHERE authors <> ''").fetchall()
    for row in rows:
        for author in row["authors"].split("; "):
            name = author.strip()
            words = author_words(name)
            if len(words) != 3:
                continue
            for i in range(3):
                for j in range(i + 1, 3):
                    index.setdefault(frozenset((words[i], words[j])), set()).add(name)
    return index


def text_patronymics(cache: TextCache, root: Path, rows: list[sqlite3.Row], short_words: list[str]) -> list[str]:
    """Полные имена из начала текста книг папки. Только из кэша текста: промах — книга пропускается, текст не извлекается.
    По каждой книге — первая строка из HEAD_LINES непустых строк из трёх слов имени, у которой третье слово — отчество."""
    found: list[str] = []
    for row in rows:
        path = row["path"]
        try:
            stat = os.stat(root / path)
        except OSError:
            continue
        text = cache.get(path, stat.st_size, stat.st_mtime, Path(path).suffix[1:].lower())
        if not text:
            continue
        lines = [line.strip() for line in text[:TEXT_HEAD_CHARS].splitlines() if line.strip()][:HEAD_LINES]
        for line in lines:
            words = line.removesuffix(".").split()
            if len(words) != 3 or not all(HEAD_WORD_RE.fullmatch(word) for word in words):
                continue
            # «ТОЛСТОЙ ЛЕВ НИКОЛАЕВИЧ» — в имя папки с заглавной буквы: «Толстой Лев Николаевич»
            words = ["-".join(part.capitalize() for part in word.split("-")) if word.isupper() else word for word in words]
            name = patronymic_full(" ".join(words), short_words)
            if name and is_patronymic(name):
                found.append(name)
                break
    return found


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


def is_book_name(name: str) -> bool:
    """Файл, который индексирует scan: расширение из BOOK_EXTENSIONS, не скрытый. html/htm сюда не входят (не книги)."""
    return not name.startswith(".") and name.rpartition(".")[2].lower() in BOOK_EXTENSIONS


def merge_folder(run: NightRun, db_path: str, root: Path, short: str, full: str, candidates: list[str],
                 known: set[str], taken: set[Path], pending: list[tuple[str, str]], non_books: bool = False) -> None:
    """Склейка: файлы и подпапки «короткой» папки — в «полную» (с тем же относительным путём, при конфликте имени —
    unique_path); затем пустые папки короткой удаляются снизу вверх. Остатки не удаляются, а записываются ошибкой.
    non_books: файлы, которые не книги, уходят в карантин not-book/ вместо переноса в «полную» папку."""
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
            if non_books and not is_book_name(name):
                try:
                    run.quarantine(src, "not-book")  # в dry-run только запись плана, файл на месте
                except OSError as error:
                    run.count("errors")
                    journal_row(run, {"action": "error", "short": short, "full": full,
                                      "from": src.relative_to(root).as_posix(), "error": str(error)})
                else:
                    run.count("quarantined_not_book")
                continue
            dest = free_variant_name(run.cfg, target, name, taken, src=src)
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
            with suppress(OSError):
                os.rmdir(current)
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


NAME_FIRST_MIN_GIVEN = 10  # этап «Имя Фамилия»: сколько папок с этим словом на втором месте — признак порядка «Фамилия Имя»
NAME_FIRST_SKIP = frozenset({"Марк Аврелий", "Виктор Поль-Эмиль", "Марк Эдвина"})  # настоящие «Имя Фамилия», не переставлять


def merge_initials(run: NightRun, db_path: str, root: Path, pending: list[tuple[str, str]], taken: set[Path]) -> None:
    """Этап «инициалы»: папка «Фамилия И. О» или «Фамилия И» → единственная полная папка того же автора (initials_folder).
    Один инициал — только если папка с той же фамилией среди полных одна; иначе не трогается."""
    index = author_index(root)
    full = {key: name for key, name in index.items() if not is_initials(name)}
    surnames = Counter(author_norm(name.split()[0]) for name in full.values())
    for short in sorted(index.values()):
        if not is_initials(short) or not (root / short).is_dir():
            continue
        target = initials_folder(short, full)
        tokens = [token for token in re.split(r"[\s.]+", short) if token]
        if not target or (len(tokens) - 1 < 2 and surnames[author_norm(tokens[0])] != 1):
            continue
        known = {row["path"] for row in books_under(db_path, short)}
        merge_folder(run, db_path, root, short, target, [target], known, taken, pending)
        run.count("initials")


def merge_name_first(run: NightRun, db_path: str, root: Path, pending: list[tuple[str, str]], taken: set[Path]) -> None:
    """Этап «Имя Фамилия»: папка из двух слов, где первое слово — имя (часто встречается на втором месте в других
    папках), переставляется в «Фамилия Имя». Если уже есть ровно одна другая папка того же автора (≥ 2 общих слова,
    ключ ⊆ или ⊇), склейка в неё."""
    index = author_index(root)
    with os.scandir(root) as entries:
        names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith(("_", ".")))
    given: Counter[str] = Counter()
    sur: Counter[str] = Counter()
    for name in names:
        words = name.split()
        if len(words) >= 2:
            sur[author_norm(words[0])] += 1
            given[author_norm(words[1])] += 1
    for short in names:
        words = short.split()
        if len(words) != 2 or short in NAME_FIRST_SKIP or not (root / short).is_dir():
            continue
        first, last = words
        given_first, given_last = given[author_norm(first)], given[author_norm(last)]
        if given_first < NAME_FIRST_MIN_GIVEN or sur[author_norm(first)] * 10 > given_first or given_last > 1 \
                or not last.istitle() or len(last) <= 2:
            continue
        key = author_key(short)
        if key is None:
            continue
        close = [other for known_key, other in index.items()
                 if other != short and len(known_key & key) >= 2 and (known_key <= key or known_key >= key)]
        full = close[0] if len(close) == 1 else f"{last} {first}"
        if full == short:
            continue
        known = {row["path"] for row in books_under(db_path, short)}
        merge_folder(run, db_path, root, short, full, [full], known, taken, pending)
        run.count("name_first")


def merge_authors(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Правило: «Фамилия Имя» и «Фамилия Имя Отчество» в одном каталоге — всё из короткой в полную; если полной нет,
    отчество берётся из метаданных книг короткой папки и папка переименовывается. Неоднозначное (несколько полных
    или несколько отчеств) не трогается. Связи переносятся relink по ходу прогона; журнал — построчно (откат).
    Если в метаданных короткой папки отчества нет, ищется ещё в двух источниках (только если первый пуст): метаданные
    всех книг БД (индекс один раз на прогон) и начало текста книг папки из кэша текста (без извлечения текста;
    промах кэша — пропуск). Результат принимается, только если третье слово — отчество (PATRONYMIC_RE). Один полный
    вариант из источников — переименование или склейка; несколько — неоднозначное.
    После отчеств — два этапа, каждый со своим пересчётом author_index: «инициалы» (папка «Фамилия И. О» → полная
    папка того же автора, только однозначная) и «Имя Фамилия» (папка «Иван Петров» → «Петров Иван» или склейка
    в единственную полную папку; порядок определяется по частотам слов в именах папок)."""
    cache = TextCache.open(cfg)  # один раз на прогон; None — источник из текста пропускается
    try:
        return _merge_authors(cfg, dry_run, out, cache)
    finally:
        if cache is not None:
            cache.close()


def _merge_authors(cfg: dict[str, Any], dry_run: bool, out: Callable[[str], None],
                   cache: TextCache | None) -> dict[str, Any]:
    root = Path(cfg["books_dir"])
    db_path = cfg["db_path"]
    run = NightRun(cfg, "merge-authors", dry_run, None, out)
    global_index = global_patronymics(db_path)
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
            short_words = author_words(short)
            fulls: dict[str, str] = {}
            for row in rows:
                for author in row["authors"].split("; "):
                    name = patronymic_full(author.strip(), short_words)
                    if name:
                        fulls.setdefault(author_norm(name), name)
            sources: dict[str, str] = {}  # откуда взято отчество (только если собственные метаданные папки пусты)
            if not fulls:
                for name in sorted(global_index.get(frozenset(short_words), ())):
                    patronymic = patronymic_full(name, short_words)
                    if patronymic and is_patronymic(patronymic):
                        fulls.setdefault(author_norm(patronymic), patronymic)
                        sources.setdefault(author_norm(patronymic), "patronymic_db")
                if cache is not None:
                    for patronymic in text_patronymics(cache, root, rows, short_words):
                        fulls.setdefault(author_norm(patronymic), patronymic)
                        sources.setdefault(author_norm(patronymic), "patronymic_text")
            if not fulls:
                run.count("no_patronymic")  # только счётчик: журнал не раздувается
                continue
            if len(fulls) > 1:
                run.count("ambiguous")
                journal_row(run, {"action": "ambiguous", "short": short, "full": "", "candidates": sorted(fulls.values()),
                                  "moved": 0, "status": "skip", "error": ""})
                continue
            full = next(iter(fulls.values()))
            if sources:
                run.count(sources[author_norm(full)])
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
    merge_initials(run, db_path, root, pending, taken)
    merge_name_first(run, db_path, root, pending, taken)
    flush_relinks(db_path, pending, force=True)
    s = run.stats
    prefix = "будет " if dry_run else ""
    line = (f"merge-authors: {prefix}склеено папок {s.get('merged', 0)} (из них по инициалам {s.get('initials', 0)}, "
            f"по порядку «Имя Фамилия» {s.get('name_first', 0)}), {prefix}переименовано {s.get('renamed', 0)} "
            f"(отчество из метаданных других книг БД {s.get('patronymic_db', 0)}, из начала текста книг "
            f"{s.get('patronymic_text', 0)}), "
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


# Латинские буквы-двойники в кириллическом слове («Mихаил») — к кириллице; видимое соответствие, не фонетика.
HOMOGLYPHS = str.maketrans({"a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м", "o": "о", "p": "р",
                            "t": "т", "x": "х"})


def person_word(word: str) -> str:
    """Слово имени для same_person: смешанное слово — двойники в кириллицу, чисто латинское — транслит (VARIANT_TRANSLIT)."""
    if not re.search(r"[а-я]", word):
        return word.translate(VARIANT_TRANSLIT) if re.search(r"[a-z]", word) else word
    return word.translate(HOMOGLYPHS)


def _close(x: str, y: str) -> bool:
    """Написания одного слова: правок не больше трети длины короткого (минимум одна) — «Толкиен»/«Толкин»,
    «Бредбери»/«Брэдбери», «Mихаил»/«Михаил»; «Мария»/«Марина», «Джон»/«Джордж» — уже разные."""
    return edit_distance(x, y) <= max(1, min(len(x), len(y)) // 3)


def _same_given(x: str, y: str) -> bool:
    """Имя: равны, инициал, краткая форма-префикс от трёх букв («Грег»/«Грегори», «Влад»/«Владимир»)
    или близкое написание с той же первой буквой («Сэмюэл»/«Сэмюель»); «Алексей»/«Александр» — разные."""
    if x == y:
        return True
    if min(len(x), len(y)) == 1:
        return x[0] == y[0]
    if min(len(x), len(y)) >= 3 and (x.startswith(y) or y.startswith(x)):
        return True
    return x[0] == y[0] and _close(x, y)


def same_person(a: str, b: str) -> bool:
    """Обязательный фильтр author-variants: два написания — один человек. Фамилия (первое слово) — близкое написание
    (_close, латиница через транслит). Имя — _same_given. Отчества, оба полные, — равны или одна правка.
    Лишние слова более длинного имени не сравниваются; соавторские папки сюда не доходят."""
    words_a = [person_word(w) for w in author_words(a)]
    words_b = [person_word(w) for w in author_words(b)]
    if not words_a or not words_b or not _close(words_a[0], words_b[0]):
        return False
    for index in range(1, min(len(words_a), len(words_b))):
        x, y = words_a[index], words_b[index]
        if index == 2 and min(len(x), len(y)) >= 2:
            if edit_distance(x, y) > 1:
                return False
        elif not _same_given(x, y):
            return False
    return True


def variant_target(a: str, b: str, files: dict[str, int]) -> tuple[str, str]:
    """(источник, цель) для склейки: цель — сначала папка категории «ok» (folder_category, без инициалов), затем
    с большим числом слов (полное ФИО), затем с большим числом файлов, затем по имени."""
    target, source = sorted(
        (a, b), key=lambda n: (folder_category(n) != "ok", -len(author_words(n)), -files[n], n))
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
        target = free_variant_name(run.cfg, root / folder, dest.name, taken, src=dest)  # имя резервируется сразу
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
        dest = free_variant_name(run.cfg, root / first, src.name, taken, src=src)
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
        if not same_person(a, b):  # разные люди — не склеивать ни в одном tier
            run.count("different")
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


# ----------------------------------------------------------------------
# misplaced / fix-misplaced / merge-folder / strip-suffix: книги не в своей папке автора и разбор папок (CLI)
# ----------------------------------------------------------------------

CAT_OK = "ok"
CAT_NO_META = "no-meta"
CAT_META_NOT_AUTHOR = "meta-not-author"
CAT_MISPLACED_EXISTING = "misplaced-existing"
CAT_MISPLACED_NO_TARGET = "misplaced-no-target"
MISPLACED_CATEGORIES = (CAT_OK, CAT_NO_META, CAT_META_NOT_AUTHOR, CAT_MISPLACED_EXISTING, CAT_MISPLACED_NO_TARGET)
MISPLACED_EXAMPLES = 30  # примеров misplaced-existing в выводе и в JSON-отчёте
MISPLACED_TOP = 50  # папок в JSON-отчёте (по числу неверно лежащих книг)
MERGE_MIN_BOOKS = 2  # папка решается целиком только при стольких книгах с метаданными
FOLDER_TMP_SUFFIX = ".rename-tmp"  # временное имя при переименовании папки только по регистру
SUFFIX_RE = re.compile(r"^(?P<base>.+)_(?P<num>[1-9][0-9]?)(?P<ext>\.[^.]+)$")  # «имя_N.ext», N — 1..99


def same_author(folder_key: frozenset[str], key: frozenset[str]) -> bool:
    """Ключи совпадают или пересекаются ≥ 2 словами, и один из них вложен в другой (⊆ или ⊇)."""
    return folder_key == key or (
        len(folder_key & key) >= 2 and (folder_key <= key or folder_key >= key))


def folder_matches(folder: str, authors: list[str], index: dict[frozenset[str], str]) -> bool:
    """Папка подходит хотя бы одному автору книги: по ключу либо как инициалы этого автора (соавтор тоже годится)."""
    folder_key = author_key(folder)
    for author in authors:
        key = author_key(author)
        if key is None:
            continue
        if folder_key is not None and same_author(folder_key, key):
            return True
        if is_initials(author) and initials_folder(author, index) == folder:
            return True
    return False


def expected_folder(author: str, index: dict[frozenset[str], str]) -> tuple[str, str]:
    """Папка, которую выбрал бы unpack-collections по автору: (папка или '', способ). Инициалы — как в initials_folder."""
    if is_initials(author):
        return initials_folder(author, index), "initials"
    key = author_key(author)
    if key is None:
        return "", "no-key"
    return pick_folder(author, key, "", index)


def classify(folder: str, authors: list[str], index: dict[frozenset[str], str]) -> tuple[str, str]:
    """Категория книги и ожидаемая папка (только для misplaced-*; для остальных — '')."""
    if not authors:
        return CAT_NO_META, ""
    if folder_matches(folder, authors, index):
        return CAT_OK, ""
    if author_key(authors[0]) is None:
        return CAT_META_NOT_AUTHOR, ""
    expected, method = expected_folder(authors[0], index)
    if not expected or method in ("new", "not-person", "no-key"):
        return CAT_MISPLACED_NO_TARGET, ""
    return CAT_MISPLACED_EXISTING, expected


def top_level_folder(path: str) -> str:
    """Папка верхнего уровня, если книга лежит в папке автора (не «_…» и не «.…»); иначе ''."""
    if "/" not in path:
        return ""
    folder = PurePosixPath(path).parts[0]
    return "" if folder.startswith(("_", ".")) else folder


def misplaced_books(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Все книги в папках авторов верхнего уровня из БД: путь, папка, авторы (строка как в БД), категория и ожидаемая
    папка. Только чтение: база открывается readonly, файлы не трогаются. Общая выборка для misplaced и fix-misplaced."""
    db_path = cfg["db_path"]
    if not Path(db_path).is_file():
        raise ConfigError(f"база не найдена: {db_path}")
    index = author_index(Path(cfg["books_dir"]))
    rows: list[dict[str, Any]] = []
    with closing(connect(db_path, readonly=True)) as conn:
        for row in conn.execute("SELECT path, authors FROM books ORDER BY path"):
            folder = top_level_folder(row["path"])
            if not folder:
                continue
            authors = str(row["authors"] or "")
            names = [name.strip() for name in authors.split("; ") if name.strip()]
            category, expected = classify(folder, names, index)
            rows.append({"path": row["path"], "folder": folder, "authors": authors,
                         "category": category, "expected": expected})
    return rows


def misplaced(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, int]:
    """Отчёт без изменений: счётчики по категориям, EXAMPLES примеров misplaced-existing; JSON — в reports/ (кроме dry-run)."""
    rows = misplaced_books(cfg)
    stats = {category: 0 for category in MISPLACED_CATEGORIES}
    for row in rows:
        stats[row["category"]] += 1
    out("категории:")
    for category in MISPLACED_CATEGORIES:
        out(f"  {category}: {stats[category]}")
    existing = sorted((row for row in rows if row["category"] == CAT_MISPLACED_EXISTING), key=lambda row: row["path"])
    out(f"примеры misplaced-existing ({min(len(existing), MISPLACED_EXAMPLES)} из {len(existing)}):")
    for row in existing[:MISPLACED_EXAMPLES]:
        out(f"  {row['authors']} | {row['path']} → {row['expected']}")
    if dry_run:
        out("dry-run: JSON не записан")
        return stats
    misplaced_rows = [row for row in rows if row["category"] in (CAT_MISPLACED_EXISTING, CAT_MISPLACED_NO_TARGET)]
    by_folder = Counter(row["folder"] for row in misplaced_rows)
    top = sorted(by_folder.items(), key=lambda item: (-item[1], item[0]))[:MISPLACED_TOP]
    report = write_report("misplaced", {
        "stats": stats,
        "by_folder": [{"folder": folder, "misplaced": count} for folder, count in top],
        "rows": misplaced_rows,
    })
    out(f"JSON: {report}")
    return stats


CHECK_FOLDERS_CATEGORIES = ("ok", "not-person", "coauthors", "single", "initials")
CHECK_FOLDERS_EXAMPLES = 20  # папок на категорию в выводе; JSON-отчёт содержит все


def folder_category(name: str) -> str:
    """Категория папки верхнего уровня для check-folders: «Фамилия Имя [Отчество]» — ok. Проверяется первая подходящая:
    соавторы (разделители или больше трёх слов) — раньше проверки имени, иначе «Ильф Илья и Петров Евгений» (строчное «и»)
    попала бы в not-person; затем not-person, single (одно слово), initials (буква или точка в слове)."""
    words = name.split()
    if COAUTHOR_SEPARATOR.search(name) or " - " in name or len(words) > 3:
        return "coauthors"
    if not person_name(name):
        return "not-person"
    if len(words) == 1:
        return "single"
    if any(len(word) == 1 or "." in word for word in words):
        return "initials"
    return "ok"


def check_folders(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, int]:
    """Отчёт без изменений: папки верхнего уровня без «_» и «.» в начале — соответствие «Фамилия Имя [Отчество]».
    Счётчики по категориям, по CHECK_FOLDERS_EXAMPLES папок на категорию; JSON — reports/check-folders-*.json (кроме dry-run).
    Файлы не трогаются, база не открывается; симлинки-папки пропускаются."""
    root = Path(cfg["books_dir"])
    if not root.is_dir():
        raise ConfigError(f"каталог недоступен: {root}")
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False) and folder_top_ok(entry.name))
    groups: dict[str, list[dict[str, Any]]] = {category: [] for category in CHECK_FOLDERS_CATEGORIES}
    for name in names:
        books = sum(1 for _current, _dirs, files in os.walk(root / name) for file in files if is_book_name(file))
        groups[folder_category(name)].append({"folder": name, "books": books})
    stats = {category: len(groups[category]) for category in CHECK_FOLDERS_CATEGORIES}
    out(f"папок верхнего уровня: {len(names)}, книг в них: {sum(item['books'] for rows in groups.values() for item in rows)}")
    out("категории:")
    for category in CHECK_FOLDERS_CATEGORIES:
        out(f"  {category}: {stats[category]}")
    for category in CHECK_FOLDERS_CATEGORIES:
        rows = groups[category]
        out(f"примеры {category} ({min(len(rows), CHECK_FOLDERS_EXAMPLES)} из {len(rows)}):")
        for item in rows[:CHECK_FOLDERS_EXAMPLES]:
            out(f"  {item['folder']} — книг: {item['books']}")
    if dry_run:
        out("dry-run: JSON не записан")
        return stats
    report = write_report("check-folders", {"stats": stats, "folders": groups})
    out(f"JSON: {report}")
    return stats


def move_guard(folder: str, target: str) -> str:
    """Причина, по которой книги или папку из folder нельзя переносить в target; '' — можно. Запреты общие, без списков:
    папка в скобках (псевдоним или настоящее имя); цель хуже исходной (меньше слов); цель — инициалы; в цели
    несколько авторов; цель не похожа на имя человека; у папки и цели нет общего слова и разные фамилии (другой человек)."""
    if "(" in folder or ")" in folder:
        return "skip-brackets"
    if len(author_words(target)) < len(author_words(folder)):
        return "skip-worse"
    if is_initials(target):
        return "skip-initials"
    if COAUTHOR_SEPARATOR.search(target):
        return "skip-coauthors-target"
    if not person_name(target):
        return "skip-not-person"
    folder_key, target_key = author_key(folder), author_key(target)
    if folder_key is None or target_key is None:
        return "skip-no-key"
    if not folder_key & target_key and author_norm(folder.split()[0]) != author_norm(target.split()[0]):
        return "skip-other-person"
    return ""


def edit_distance(a: str, b: str) -> int:
    """Расстояние Левенштейна: вставка, удаление и замена одной буквы стоят по одной правке."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def typo_fix(folder: str, name: str) -> bool:
    """Опечатка в одном слове: слова совпадают, кроме одного; различие не больше 2 правок; оба слова не короче 3 букв;
    это фамилия (первое слово папки) либо разница — одна лишняя или пропущенная буква (удвоение, пропуск)."""
    words_folder, words_name = author_words(folder), author_words(name)
    if len(words_folder) != len(words_name):
        return False
    diff = [i for i, (word, other) in enumerate(zip(words_folder, words_name, strict=True)) if word != other]
    if len(diff) != 1:
        return False
    i = diff[0]
    word, other = words_folder[i], words_name[i]
    if min(len(word), len(other)) < 3 or edit_distance(word, other) > 2:
        return False
    return i == 0 or (abs(len(word) - len(other)) == 1 and edit_distance(word, other) == 1)


def rename_guard(folder: str, name: str) -> str:
    """Причина, по которой папку folder нельзя переименовать в name; '' — можно. Сначала move_guard (с префиксом
    skip-rename-), затем правила переименования: ключ имени ⊇ ключ папки (меняется только дополнение, существующие
    слова не меняются) либо опечатка в одном слове (typo_fix); добавленные слова — не инициалы (≥ 3 буквы); имя не
    оканчивается частицей и частица стоит на той же позиции, что в папке."""
    reason = move_guard(folder, name)
    if reason:
        return "skip-rename-" + reason.removeprefix("skip-")
    folder_key, name_key = author_key(folder), author_key(name)
    if folder_key is None or name_key is None:
        return "skip-rename-changes"
    if not folder_key <= name_key and not typo_fix(folder, name):
        return "skip-rename-changes"
    if any(len(word) < 3 for word in name_key - folder_key):
        return "skip-rename-initials"
    words_folder, words_name = author_words(folder), author_words(name)
    if words_name[-1] in AUTHOR_PARTICLES or any(
            i >= len(words_name) or words_name[i] != word
            for i, word in enumerate(words_folder) if word in AUTHOR_PARTICLES):
        return "skip-rename-particle"
    return ""


def first_author(authors: str) -> str:
    return authors.split("; ")[0].strip()


def folder_action(run: NightRun, db_path: str, root: Path, index: dict[frozenset[str], str], folder: str,
                  rows: list[dict[str, Any]], taken: set[Path], pending: list[tuple[str, str]]) -> bool:
    """Решение по всей папке: склейка в существующую папку автора (100 % книг с метаданными голосуют за неё)
    или переименование в имя по pick_folder («new»). True — папка обработана, её книги на этапе книг не трогаются."""
    with_authors = [row for row in rows if row["authors"].strip()]
    total = len(with_authors)
    if total < MERGE_MIN_BOOKS:
        return False
    votes = Counter(row["expected"] for row in with_authors if row["category"] == CAT_MISPLACED_EXISTING)
    if votes:
        if len(votes) != 1:
            return False
        target, count = next(iter(votes.items()))
        if count != total or target == folder or not (root / target).is_dir() or move_guard(folder, target):
            return False
        run.out(f"{folder} → {target} (книг {total} из {total})")
        known = {row["path"] for row in rows}
        merge_folder(run, db_path, root, folder, target, [target], known, taken, pending)
        run.count("folder-merged")
        return True
    if total != len(rows):
        return False
    keys = {author_key(first_author(row["authors"])) for row in rows}
    if len(keys) != 1:
        return False
    key = keys.pop()
    author = first_author(rows[0]["authors"])
    if key is None or author_key(folder) == key:
        return False
    # сама папка не участвует в подборе имени, иначе «Петров Иван» нашла бы себя и не переименовалась бы
    name, method = pick_folder(author, key, "", {known: owner for known, owner in index.items() if owner != folder})
    if method != "new" or not name or name == folder or (root / name).exists():
        return False
    reason = rename_guard(folder, name)
    if reason:
        run.count(reason)
        return False
    run.out(f"{folder} → {name} (переименование, книг {total})")
    if run.dry_run:
        run.count("folder-renamed")
        journal_row(run, {"action": "rename", "from": folder, "to": name, "status": "dry", "error": ""})
        return True
    try:
        os.rename(root / folder, root / name)
    except OSError as error:
        run.count("errors")
        journal_row(run, {"action": "error", "from": folder, "to": name, "error": str(error)})
        return True
    run.count("folder-renamed")
    journal_row(run, {"action": "rename", "from": folder, "to": name, "status": "moved", "error": ""})
    pending.extend((row["path"], name + row["path"][len(folder):]) for row in rows)
    flush_relinks(db_path, pending)
    return True


class Move(NamedTuple):
    rel: str       # путь книги относительно каталога книг: «папка/файл»
    name: str      # имя файла
    expected: str  # папка автора, в которую переносится книга


def select_moves(rows: list[dict[str, Any]], root: Path, run: NightRun) -> list[Move]:
    """Книги к переносу: только misplaced-existing, путь «папка/файл», один автор, цель существует и проходит move_guard.
    Каждая отброшенная книга учитывается счётчиком своей причины."""
    moves: list[Move] = []
    for row in rows:
        if row["category"] != CAT_MISPLACED_EXISTING:
            continue
        path, expected = row["path"], row["expected"]
        parts = PurePosixPath(path).parts
        if not expected or not safe_relative(expected) or not (root / expected).is_dir() \
                or (len(parts) == 2 and parts[0] == expected):
            run.count("skip-no-target")
            continue
        if len(parts) != 2 or not safe_relative(path):
            run.count("skip-subfolder")
            continue
        if "; " in row["authors"]:
            run.count("skip-coauthors")
            continue
        reason = move_guard(parts[0], expected)
        if reason:
            run.count(reason)
            continue
        if not (root / path).is_file():
            run.count("skip-missing")
            continue
        moves.append(Move(path, parts[1], expected))
    return moves


def safe_relative(text: str) -> bool:
    """Относительный путь без абсолютного начала и без «..» — чтобы не выйти за пределы каталога книг."""
    posix = PurePosixPath(text)
    return bool(posix.parts) and not posix.is_absolute() and ".." not in posix.parts


def move_one(run: NightRun, root: Path, db_path: str, move: Move, taken: set[Path], pending: list[tuple[str, str]]) -> None:
    """Перенос одной книги в папку автора; при занятом имени — уточнение в скобках (free_variant_name). Связи — пачками."""
    dest = free_variant_name(run.cfg, root / move.expected, move.name, taken, src=root / move.rel)
    if dest.name != move.name:
        run.count("conflicts")
    dst = dest.relative_to(root).as_posix()
    if run.dry_run:
        run.count("moved")
        journal_row(run, {"action": "move", "from": move.rel, "to": dst, "status": "dry", "error": ""})
        return
    try:
        os.rename(root / move.rel, dest)
    except OSError as error:
        run.count("errors")
        journal_row(run, {"action": "error", "from": move.rel, "to": dst, "error": str(error)})
        return
    run.count("moved")
    journal_row(run, {"action": "move", "from": move.rel, "to": dst, "status": "moved", "error": ""})
    pending.append((move.rel, dst))
    flush_relinks(db_path, pending)


def remove_emptied(run: NightRun, root: Path, tops: set[str]) -> None:
    """Папки верхнего уровня, из которых перенесли книги и в которых не осталось файлов, — пустые подпапки и сама папка
    удаляются снизу вверх (remove_empty_folder, только os.rmdir)."""
    for top in sorted(tops):
        base = root / top
        if base.is_dir() and not any(path.is_file() for path in base.rglob("*")):
            remove_empty_folder(run, base)


def fix_misplaced(cfg: dict[str, Any], dry_run: bool = False, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Этап папок (folder_action), затем этап книг (select_moves/move_one), затем удаление опустевших папок.
    Журнал reports/fix-misplaced-*.jsonl: move/rename с полями from/to для отката."""
    root = Path(cfg["books_dir"])
    db_path = cfg["db_path"]
    rows = misplaced_books(cfg)
    index = author_index(root)
    by_folder: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_folder.setdefault(row["folder"], []).append(row)
    run = NightRun(cfg, "fix-misplaced", dry_run, None, out)
    taken: set[Path] = set()
    pending: list[tuple[str, str]] = []
    handled: set[str] = set()
    for folder in sorted(by_folder):
        if not (root / folder).is_dir():
            continue
        if folder_action(run, db_path, root, index, folder, by_folder[folder], taken, pending):
            handled.add(folder)
        flush_relinks(db_path, pending, force=True)
    moves = select_moves([row for row in rows if row["folder"] not in handled], root, run)
    run.out(f"к переносу: {len(moves)}")
    for move in moves:
        move_one(run, root, db_path, move, taken, pending)
    flush_relinks(db_path, pending, force=True)
    if not dry_run:
        remove_emptied(run, root, {PurePosixPath(move.rel).parts[0] for move in moves})
    return run.finish()


def folder_top_ok(name: str) -> bool:
    """Имя папки верхнего уровня: непустое, без разделителей пути и NUL, не «_…» и не «.…»."""
    return bool(name) and "/" not in name and "\\" not in name and "\x00" not in name and not name.startswith(("_", "."))


def rename_folder(run: NightRun, db_path: str, root: Path, src: str, dst: str, pending: list[tuple[str, str]]) -> None:
    """Переименование папки верхнего уровня, если «dst» нет. Отличие только регистром — через временное имя.
    Связи (отметки, полки, коллекции) переносятся по всем книгам папки."""
    pairs = [(row["path"], dst + row["path"][len(src):]) for row in books_under(db_path, src)]
    if run.dry_run:
        run.count("renamed")
        journal_row(run, {"action": "rename", "from": src, "to": dst, "status": "dry", "error": ""})
        return
    try:
        if src.casefold() == dst.casefold():
            temp = root / (src + FOLDER_TMP_SUFFIX)
            os.rename(root / src, temp)
            os.rename(temp, root / dst)
        else:
            os.rename(root / src, root / dst)
    except OSError as error:
        run.count("errors")
        journal_row(run, {"action": "error", "from": src, "to": dst, "error": str(error)})
        return
    run.count("renamed")
    journal_row(run, {"action": "rename", "from": src, "to": dst, "status": "moved", "error": ""})
    pending.extend(pairs)
    flush_relinks(db_path, pending)


RAZOBRAT_SECTION = "_разобрать"  # служебный раздел: в него можно склеивать папки через --to «_разобрать/…»
PERIODICALS_SECTION = "_Журналы"  # служебный раздел журналов: move-books кладёт файлы в «_Журналы/Издание/…» (не короче трёх сегментов)


def service_path(root: Path, rel: str, min_parts: int) -> Path:
    """Папка внутри служебного раздела «_…» каталога книг: относительный путь без «..» и пустых сегментов, первый
    сегмент — «_…», не короче min_parts сегментов. После разрешения ссылок путь должен остаться внутри каталога книг."""
    parts = PurePosixPath(rel).parts
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/") or len(parts) < min_parts \
            or not parts[0].startswith("_") or any(part in ("", ".", "..") for part in parts):
        raise ConfigError(f"путь «{rel}» — папка внутри служебного раздела «_…» каталога книг")
    full = root / rel
    if not full.resolve().is_relative_to(root.resolve()):
        raise ConfigError(f"путь «{rel}» выходит за пределы каталога книг")
    if not full.is_dir():
        raise ConfigError(f"папки нет: {rel}")
    return full


def prepare_collections(cfg: dict[str, Any], dry_run: bool, out: Callable[[str], None]) -> None:
    """Перед переносом файлов: строки сборников книг из папок сборников (rebuild_collections в транзакции), чтобы
    relink перенёс принадлежность к сборнику со старого пути. В dry-run и без базы схемы сборников — ничего не делает."""
    db_path = cfg["db_path"]
    if dry_run or not Path(db_path).is_file():
        return
    with closing(connect(db_path)) as conn, conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_COLLECTIONS:
            return
        report_collections(rebuild_collections(conn), out)


def folder_merge(cfg: dict[str, Any], sources: list[str], target: str, dry_run: bool = False,
                 out: Callable[[str], None] = print, non_books_to_quarantine: bool = False) -> dict[str, Any]:
    """merge-folder: папка A → B. A — папка верхнего уровня или папка внутри служебного раздела «_…» (--from
    «_Сборники/Серия/Автор»). B — папка автора верхнего уровня, существующая служебная папка «_…» верхнего уровня
    (--to «_1tmp», новая не создаётся) или путь внутри служебного раздела «_…» (--to «_разобрать/Серия» — создаётся
    при отсутствии; --to «_Журналы/Если» — раздел должен существовать). Если B нет и A верхнего уровня — переименование
    A в B; иначе склейка (merge_folder: файлы и подпапки A переносятся в B с теми же относительными путями, конфликт
    имени — уточнение в скобках). Повторяемый --from — несколько папок подряд. Связи и принадлежность к сборнику
    переносятся (relink). Журнал reports/merge-folder-*.jsonl.
    non_books_to_quarantine: только с --to внутри «_разобрать»; файлы не книг (не из BOOK_EXTENSIONS, в т. ч. html)
    уходят в карантин not-book/ вместо переноса.
    Путь внутри раздела всегда склейка; сегменты проходят windows_name_error, итог — внутри каталога книг."""
    root = Path(cfg["books_dir"])
    db_path = cfg["db_path"]
    if not sources:
        raise ConfigError("merge-folder: укажите --from")
    if "/" in target:  # путь внутри служебного раздела «_…»: подпапки создаются, сохраняются
        parts = PurePosixPath(target).parts
        if len(parts) < 2 or not parts[0].startswith("_") or any(part in ("", ".", "..") for part in parts) \
                or "\x00" in target or "\\" in target or target.startswith("/") \
                or any(windows_name_error(part) for part in parts):
            raise ConfigError(f"merge-folder: --to «{target}» — папка автора, служебная «_…» или путь внутри «_…/…» "
                              "с именами, годными для Windows")
        if parts[0] != RAZOBRAT_SECTION and not (root / parts[0]).is_dir():
            raise ConfigError(f"merge-folder: раздела «{parts[0]}» нет: путь внутри него не создаёт раздел")
        if not (root / target).resolve().is_relative_to((root / parts[0]).resolve()):
            raise ConfigError(f"merge-folder: --to «{target}» выходит за пределы «{parts[0]}»")
    elif target.startswith("_"):  # служебная папка верхнего уровня: только существующая, склейка
        if not (root / target).is_dir():
            raise ConfigError(f"merge-folder: служебной папки «{target}» нет: новая «_…» не создаётся")
        if windows_name_error(target):
            raise ConfigError(f"merge-folder: «{target}»: {windows_name_error(target)}")
    elif not folder_top_ok(target) or folder_name(target) != target:
        raise ConfigError(f"merge-folder: имя «{target}» не подходит для папки автора")
    for name in sources:
        if "/" in name:
            service_path(root, name, 2)  # только внутри «_…», не короче двух сегментов
        elif not folder_top_ok(name):
            raise ConfigError(f"merge-folder: «{name}» — не папка верхнего уровня")
        elif not (root / name).is_dir():
            raise ConfigError(f"merge-folder: папки нет: {name}")
        if name == target or target.startswith(name + "/") or name.startswith(target + "/"):
            raise ConfigError("merge-folder: --from и --to совпадают или вложены друг в друга")
    if (root / target).exists() and not (root / target).is_dir():
        raise ConfigError(f"merge-folder: «{target}» есть, но это не папка")
    if non_books_to_quarantine and not target.startswith(RAZOBRAT_SECTION + "/"):
        raise ConfigError(f"merge-folder: --non-books-to-quarantine допустим только с --to внутри «{RAZOBRAT_SECTION}/…»")
    run = NightRun(cfg, "merge-folder", dry_run, None, out)
    prepare_collections(cfg, dry_run, out)
    target_exists = (root / target).is_dir()
    taken: set[Path] = set()
    pending: list[tuple[str, str]] = []
    for name in dict.fromkeys(sources):
        if not (root / name).is_dir():
            run.count("skip-missing")  # уже склеена предыдущим --from
            continue
        if target_exists or "/" in name or "/" in target:  # вложенные — всегда склейка: переименовать верхний уровень нельзя
            known = {row["path"] for row in books_under(db_path, name)}
            merge_folder(run, db_path, root, name, target, [target], known, taken, pending,
                         non_books=non_books_to_quarantine)
        else:
            rename_folder(run, db_path, root, name, target, pending)
        target_exists = True  # дальше склейка: в dry-run — план, после переименования и склейки цель уже есть
        flush_relinks(db_path, pending, force=True)
    return run.finish()


def author_scope(current: Path, root: Path) -> bool:
    """Каталог внутри папки автора верхнего уровня (не корень каталога книг и не «_…» / «.…»)."""
    rel = current.relative_to(root)
    return bool(rel.parts) and not rel.parts[0].startswith(("_", "."))


def numbered_sibling(name: str, base: str, ext: str) -> bool:
    """Имя «base_M.ext» (M — 1..99) с тем же основанием и расширением."""
    match = SUFFIX_RE.match(name)
    return bool(match) and match.group("base") == base and match.group("ext") == ext


def strip_suffix(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                 out: Callable[[str], None] = print) -> dict[str, Any]:
    """Файлы «имя_N.ext» (N — 1..99) → «имя.ext», если «имя.ext» свободно и в папке нет других «имя_M.ext».
    Без --path — только внутри папок авторов верхнего уровня; с --path — внутри указанного каталога.
    Связи переносятся relink по ходу прогона (NightRun.moved)."""
    root = Path(cfg["books_dir"])
    target = target_dir(cfg, path)
    run = NightRun(cfg, "strip-suffix", dry_run, path, out)
    walk_errors: list[str] = []
    for current, names in walk_dirs(target, walk_errors):
        if path is None and not author_scope(current, root):
            continue
        taken = {other.casefold() for other in names}  # имена папки без учёта регистра: Windows не различает «Книга» и «книга»
        for name in names:
            match = SUFFIX_RE.match(name)
            if not match:
                continue
            run.tick()
            base, ext = match.group("base"), match.group("ext")
            src, dest = current / name, current / (base + ext)
            if dest.exists() or dest.name.casefold() in taken:
                run.count("skip-original-exists")
                continue
            if any(other != name and numbered_sibling(other, base, ext) for other in names):
                run.count("skip-siblings")
                continue
            src_rel, dst_rel = run.rel(src), run.rel(dest)
            if dry_run:
                run.count("renamed")
                journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "dry", "error": ""})
                continue
            try:
                os.rename(src, dest)
            except OSError as error:
                run.count("errors")
                journal_row(run, {"action": "error", "from": src_rel, "to": dst_rel, "error": str(error)})
                continue
            run.count("renamed")
            taken.add(dest.name.casefold())
            journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "moved", "error": ""})
            run.moved(src, dest)
    return run.finish({"walk_errors": walk_errors})


NAME_VARIANT_KINDS = ("translator", "lang", "source", "year")  # уточнения по порядку предпочтения; «variant» — последним
NAME_VARIANT_YEAR_CUTOFF = 2025  # год от этого и позже — дата конвертации, а не издания


def short_person(value: str) -> str:
    """Фамилия из значения contributor: последнее слово без скобок и точек («Н. И. Виленская» → «Виленская»)."""
    words = re.sub(r"\([^()]*\)", " ", value).split()
    return words[-1].strip(" .,;:") if words else ""


def short_source(name: str) -> str:
    """Короткое имя сборника для имени файла: без текста в скобках и хвоста « - …», без префикса «Библиотека »."""
    text = re.sub(r"\s*\([^()]*\)", "", name).split(" - ", 1)[0].strip()
    return text.removeprefix("Библиотека ").strip()


def name_variant_values(conn: sqlite3.Connection, rel: str, has_meta: bool, has_collections: bool) -> dict[str, str]:
    """Уточнения книги в виде текста для имени файла; пустая строка — значения нет или оно не годится."""
    values = dict.fromkeys(NAME_VARIANT_KINDS, "")
    row = conn.execute("SELECT lang, year FROM books WHERE path=?", (rel,)).fetchone()
    if row is not None:
        lang = (row["lang"] or "").strip()
        if re.match(r"[A-Za-z]{2}", lang):
            values["lang"] = lang[:2].lower()
        year = (row["year"] or "").strip()
        if re.fullmatch(r"\d{4}", year) and int(year) < NAME_VARIANT_YEAR_CUTOFF:
            values["year"] = year
    if has_meta:
        found = conn.execute("SELECT value FROM book_meta WHERE path=? AND key IN ('translator', 'contributor') "
                             "ORDER BY CASE key WHEN 'translator' THEN 0 ELSE 1 END, value LIMIT 1", (rel,)).fetchone()
        surname = short_person(found[0]) if found else ""
        if surname:
            values["translator"] = f"пер. {surname}"
    if has_collections:
        found = conn.execute("SELECT collection FROM book_collections WHERE path=? AND source='folder' "
                             "ORDER BY collection LIMIT 1", (rel,)).fetchone()
        if found:
            values["source"] = short_source(found[0])
    return values


VARIANT_TAIL_RE = re.compile(r"(?:_[1-9][0-9]?| \([^()]*\))+$")  # «_1_1», « (Мошкова)» в конце основы


def name_variant_stem(stem: str) -> str:
    """Основа без хвостов «_N» (в т. ч. повторных «_1_1») и уже назначенных уточнений « (…)»."""
    return VARIANT_TAIL_RE.sub("", stem) or stem


def name_variant_groups(names: list[str]) -> dict[tuple[str, str], list[tuple[str, re.Match[str] | None]]]:
    """Одноимённые файлы папки без учёта регистра: «основа.ext», «основа (уточнение).ext» и «основа_N.ext»
    (match — у «_N»-файлов); «_1_1» сводится к той же основе."""
    groups: dict[tuple[str, str], list[tuple[str, re.Match[str] | None]]] = {}
    for name in names:
        match = SUFFIX_RE.match(name)
        stem, ext = (match.group("base"), match.group("ext")) if match else os.path.splitext(name)
        groups.setdefault((name_variant_stem(stem).casefold(), ext.casefold()), []).append((name, match))
    return groups


def name_variants(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                  out: Callable[[str], None] = print) -> dict[str, Any]:
    """«основа_N.ext» в папке, где есть «основа.ext» или другой «основа_M.ext» (без учёта регистра), → «основа (уточнение).ext».
    Уточнение — первое, что отличает файл от всей группы: пер. (переводчик), язык, источник, год; иначе «вариант N».
    Занятое имя или имя, не проходящее windows_name_error, — следующее уточнение; не нашлось — ошибка в журнале.
    Без --path — только внутри папок авторов верхнего уровня. База читается одним соединением, только SELECT."""
    root = Path(cfg["books_dir"])
    target = target_dir(cfg, path)
    conn = connect(cfg["db_path"], readonly=True)
    run = NightRun(cfg, "name-variants", dry_run, path, out)
    walk_errors: list[str] = []
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_meta, has_collections = version >= SCHEMA_META, version >= SCHEMA_COLLECTIONS
        for current, names in walk_dirs(target, walk_errors):
            if path is None and not author_scope(current, root):
                continue
            taken = {other.casefold() for other in names}  # имена папки без учёта регистра и назначенные в этом прогоне
            for group in name_variant_groups(names).values():
                if len(group) < 2 or all(match is None for _, match in group):
                    continue
                try:
                    values = {name: name_variant_values(conn, run.rel(current / name), has_meta, has_collections)
                              for name, _ in group}
                except sqlite3.Error as error:
                    for name, match in group:
                        if match is not None:
                            run.count("errors")
                            journal_row(run, {"action": "error", "from": run.rel(current / name), "to": "",
                                              "error": f"чтение базы: {error}", "qualifier": ""})
                    continue
                for name, match in group:
                    if match is None:
                        continue
                    run.tick()
                    base, ext, num = name_variant_stem(match.group("base")), match.group("ext"), match.group("num")
                    mine = values[name]
                    others = [values[other] for other, _ in group if other != name]
                    options = [(kind, mine[kind]) for kind in NAME_VARIANT_KINDS
                               if mine[kind] and all(item[kind] != mine[kind] for item in others)]
                    options.append(("variant", f"вариант {num}"))
                    src, src_rel = current / name, run.rel(current / name)
                    chosen: tuple[str, str] | None = None
                    for kind, text in options:
                        candidate = f"{base} ({text}){ext}"
                        if not windows_name_error(candidate) and candidate.casefold() not in taken \
                                and not (current / candidate).exists():
                            chosen = (kind, candidate)
                            break
                    if chosen is None:
                        run.count("errors")
                        journal_row(run, {"action": "error", "from": src_rel, "to": "", "qualifier": "",
                                          "error": "нет свободного имени: " + ", ".join(text for _, text in options)})
                        continue
                    kind, dest_name = chosen
                    dest = current / dest_name
                    dst_rel = run.rel(dest)
                    if dry_run:
                        taken.add(dest_name.casefold())
                        run.count("renamed")
                        run.count(f"qualifier-{kind}")
                        journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "dry",
                                          "error": "", "qualifier": kind})
                        continue
                    try:
                        os.rename(src, dest)
                    except OSError as error:
                        run.count("errors")
                        journal_row(run, {"action": "error", "from": src_rel, "to": dst_rel, "error": str(error),
                                          "qualifier": kind})
                        continue
                    taken.add(dest_name.casefold())
                    run.count("renamed")
                    run.count(f"qualifier-{kind}")
                    journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "moved",
                                      "error": "", "qualifier": kind})
                    run.moved(src, dest)
    finally:
        conn.close()
    return run.finish({"walk_errors": walk_errors})


def windows_names(cfg: dict[str, Any], dry_run: bool = False, path: str | None = None,
                  out: Callable[[str], None] = print) -> dict[str, Any]:
    """windows-names: имена папок и файлов, недопустимые в Windows (Syncthing), приводятся к допустимым (windows_dir_plan),
    конфликты регистра не переименовываются (строка conflict, решает dedup-folders или человек). Обход снизу вверх: переименование папки не меняет пути
    ещё не обработанных внутри неё. Файл — os.rename + run.moved; папка — со связями (books_under + flush_relinks).
    Служебные «.…» и симлинки пропускаются; ошибка одной записи записывается в журнал и не останавливает прогон."""
    db_path = cfg["db_path"]
    target = target_dir(cfg, path)
    run = NightRun(cfg, "windows-names", dry_run, path, out)
    walk_errors: list[str] = []

    def onerror(error: OSError) -> None:
        walk_errors.append(f"{error.filename}: {error.strerror}")

    def rename(current: Path, old: str, new: str, is_dir: bool, reason: str) -> None:
        src, dest = current / old, current / new
        src_rel, dst_rel = run.rel(src), run.rel(dest)
        action = "rename" if is_dir else "move"
        if os.path.lexists(dest):
            run.count("errors")
            message = "цель существует, нужна склейка merge-folder" if is_dir else "цель существует"
            journal_row(run, {"action": "error", "from": src_rel, "to": dst_rel, "reason": reason, "error": message})
            return
        if dry_run:
            run.count("renamed")
            journal_row(run, {"action": action, "from": src_rel, "to": dst_rel, "status": "dry", "error": "", "reason": reason})
            return
        pairs: list[tuple[str, str]] = []
        if is_dir:
            # файлы этого прогона уже переименованы: их отметки и полки должны быть в БД раньше, чем читаем пути папки
            if run.relinks:
                run.stats["relinked"] = run.stats.get("relinked", 0) + relink(db_path, run.relinks)
                run.relinks.clear()
            pairs = [(row["path"], dst_rel + row["path"][len(src_rel):]) for row in books_under(db_path, src_rel)]
        os.rename(src, dest)
        run.count("renamed")
        journal_row(run, {"action": action, "from": src_rel, "to": dst_rel, "status": "moved", "error": "", "reason": reason})
        if is_dir:
            flush_relinks(db_path, pairs, force=True)
        else:
            run.moved(src, dest)

    for current, dirs, files in os.walk(target, topdown=False, onerror=onerror):
        here = Path(current)
        if any(part.startswith(".") for part in here.relative_to(target).parts):
            continue
        kinds = {name: True for name in dirs if not name.startswith(".") and not (here / name).is_symlink()}
        kinds.update({name: False for name in files if not name.startswith(".") and not (here / name).is_symlink()})
        for old, new, reason, twin in windows_dir_plan(list(kinds.items())):
            run.tick()
            if twin:
                run.count("case_conflicts")
                journal_row(run, {"action": "conflict", "from": run.rel(here / old), "to": run.rel(here / twin),
                                  "reason": "case", "error": "конфликт регистра: решает dedup-folders или человек"})
                continue
            if new == old:
                continue
            try:
                rename(here, old, new, kinds[old], reason)
            except (OSError, sqlite3.Error) as error:
                run.count("errors")
                journal_row(run, {"action": "error", "from": run.rel(here / old), "to": run.rel(here / new),
                                  "reason": reason, "error": str(error)})
    return run.finish({"walk_errors": walk_errors})


def move_path_error(rel: str) -> str:
    """Форма относительного пути из списка переноса: непустой, без NUL, «\\», абсолютного пути и пустых, «.» или «..» сегментов."""
    if not rel:
        return "путь пустой"
    if "\x00" in rel or "\\" in rel:
        return "NUL или «\\» в пути"
    if rel.startswith("/"):
        return "путь абсолютный"
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return "пустой, «.» или «..» сегмент пути"
    return ""


def existing_ancestor(path: Path) -> Path:
    """Ближайший существующий предок пути (сам путь, если он существует)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def move_line_error(root: Path, src_rel: str, dst_rel: str) -> str:
    """Причина, по которой строка списка переноса не выполняется (пусто — можно переносить)."""
    for rel in (src_rel, dst_rel):
        reason = move_path_error(rel)
        if reason:
            return reason
    dst_parts = dst_rel.split("/")
    for part in dst_parts:
        reason = windows_name_error(part)
        if reason:
            return f"«куда»: сегмент «{part}» — {reason}"
    if dst_parts[0] == PERIODICALS_SECTION:
        if len(dst_parts) < 3:
            return f"«куда» в «{PERIODICALS_SECTION}» короче трёх сегментов"
    elif not folder_top_ok(dst_parts[0]):
        return "«куда» — не папка автора верхнего уровня"
    elif len(dst_parts) < 2:
        return "«куда» короче двух сегментов"
    src = root / src_rel
    if src.is_symlink() or not src.is_file():
        return "источник — не обычный файл"
    dst = root / dst_rel
    if dst.exists() or dst.is_symlink():
        return "цель существует"
    if dst.parent.is_dir() and any(entry.casefold() == dst.name.casefold() for entry in os.listdir(dst.parent)):
        return "цель существует (без учёта регистра)"
    root_real = root.resolve()
    if not src.resolve().is_relative_to(root_real):
        return "источник выходит за пределы каталога книг"
    if not existing_ancestor(dst.parent).resolve().is_relative_to(root_real):
        return "«куда» выходит за пределы каталога книг"
    return ""


def remove_emptied_parents(run: NightRun, root: Path, moved_from: list[str]) -> None:
    """Папки-предки перенесённых файлов, в которых не осталось файлов, удаляются снизу вверх (remove_empty_folder).
    Корень каталога и папки верхнего уровня «_…» не удаляются."""
    chain: set[tuple[str, ...]] = set()
    for rel in moved_from:
        parts = rel.split("/")[:-1]
        chain.update(tuple(parts[:depth]) for depth in range(1, len(parts) + 1))
    for parts in sorted(chain, key=len, reverse=True):
        if len(parts) == 1 and parts[0].startswith("_"):
            continue
        folder = root.joinpath(*parts)
        if folder.is_dir() and not folder.is_symlink() and not any(path.is_file() for path in folder.rglob("*")):
            remove_empty_folder(run, folder)


def move_books(cfg: dict[str, Any], list_path: Path, dry_run: bool = False,
               out: Callable[[str], None] = print) -> dict[str, Any]:
    """move-books: перенос отдельных книг по списку «откуда<TAB>куда» (UTF-8, пути относительно каталога книг; пустые
    строки и строки с «#» пропускаются). Ошибка строки пропускает её, прогон продолжается. Связи переносятся relink,
    опустевшие папки-источники удаляются. Журнал reports/move-books-*.jsonl: строки move с from/to для отката."""
    root = Path(cfg["books_dir"])
    try:
        text = list_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"move-books: не прочитать список {list_path}: {error}") from error
    run = NightRun(cfg, "move-books", dry_run, None, out)
    prepare_collections(cfg, dry_run, out)
    moved_from: list[str] = []
    for number, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        run.tick()
        fields = line.split("\t")
        if len(fields) != 2:
            run.count("errors")
            journal_row(run, {"action": "error", "from": line, "to": "",
                              "error": f"строка {number}: нужны два поля через TAB"})
            continue
        src_rel, dst_rel = fields
        reason = move_line_error(root, src_rel, dst_rel)
        if reason:
            run.count("errors")
            journal_row(run, {"action": "error", "from": src_rel, "to": dst_rel, "error": f"строка {number}: {reason}"})
            continue
        if dry_run:
            run.count("moved")
            journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "dry", "error": ""})
            continue
        src, dest = root / src_rel, root / dst_rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dest)
        except OSError as error:
            run.count("errors")
            journal_row(run, {"action": "error", "from": src_rel, "to": dst_rel, "error": str(error)})
            continue
        run.count("moved")
        journal_row(run, {"action": "move", "from": src_rel, "to": dst_rel, "status": "moved", "error": ""})
        run.moved(src, dest)
        moved_from.append(src_rel)
    if not dry_run:
        remove_emptied_parents(run, root, moved_from)
    return run.finish()


# ----------------------------------------------------------------------
# purge-quarantine: окончательное удаление папок карантина по причинам (только CLI; НЕОБРАТИМО, только по решению пользователя)
# ----------------------------------------------------------------------

PURGE_PROGRESS_SECONDS = 30


VARIANT_REASONS = ("duplicate", "replaced-calibre", "unzipped")  # варианты этих причин сохраняются в book_variants до удаления
VARIANT_REASON_TEXT = {"duplicate": "дубль", "replaced-calibre": "старый epub Calibre", "unzipped": "zip-архив"}
VARIANT_BATCH = 200  # строк book_variants в одной транзакции
VARIANT_INSERT = ("INSERT OR REPLACE INTO book_variants(variant_path, reason, keeper, fmt, size, words, title, authors, "
                  "series, series_no, lang, year, meta, sha1, removed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)")


class PurgeProgress:
    """Прогресс этапа: файлов сделано из всех, скорость и ETA — не чаще PURGE_PROGRESS_SECONDS."""

    def __init__(self, total: int, label: str) -> None:
        self.total = max(total, 1)
        self.label = label
        self.done = 0
        self.started = self.last = time.monotonic()

    def advance(self) -> None:
        self.done += 1
        now = time.monotonic()
        if now - self.last < PURGE_PROGRESS_SECONDS and self.done < self.total:
            return
        self.last = now
        rate = self.done / max(now - self.started, 1e-6)
        eta = (self.total - self.done) / rate / 60 if rate > 0 else 0.0
        say(f"{self.label}: файлов {self.done}/{self.total} ({self.done * 100 // self.total} %), "
            f"{rate:.1f} файлов/с, осталось ~{eta:.1f} мин", "progress")


def purge_fail(stats: dict[str, int], message: str) -> None:
    """Ошибка одной записи: счётчик errors и журнал, удаление продолжается."""
    stats["errors"] += 1
    say(f"purge-quarantine: {message}", "purge_error", logging.WARNING)


def purge_inventory(qroot: Path, reason_dir: Path, errors: list[str]) -> list[tuple[str, int]]:
    """Файлы и симлинки папки причины: (путь от quarantine_dir, размер). Симлинк — размер самой ссылки; каталоги по ссылкам не обходятся."""
    items: list[tuple[str, int]] = []

    def onerror(error: OSError) -> None:
        errors.append(f"{error.filename}: {error.strerror}")

    for current, dirs, files in os.walk(reason_dir, onerror=onerror, followlinks=False):
        found = [Path(current) / name for name in files]
        found += [Path(current) / name for name in dirs if os.path.islink(Path(current) / name)]
        for full in found:
            try:
                size = full.lstat().st_size
            except OSError as error:
                errors.append(f"{full}: {error.strerror}")
                size = 0
            items.append((full.relative_to(qroot).as_posix(), size))
    return items


def quarantine_relpath(path: str, bases: tuple[Path, ...]) -> str | None:
    """Путь относительно карантина (причина первым сегментом) или None, если путь не внутри карантина."""
    target = Path(os.path.normpath(path))
    for base in bases:
        if target.is_relative_to(base):
            return target.relative_to(base).as_posix()
    return None


def variant_keeper_for(variant: str, books: set[str], dups: dict[str, tuple[str, int]]) -> str:
    """Путь книги библиотеки, вариантом которой был файл карантина. duplicate — keeper из dup_members, иначе путь;
    replaced-calibre — та же позиция (новый epub на месте старого); unzipped — путь без «.zip». '' — книги нет в библиотеке."""
    reason, _, rest = variant.partition("/")
    if variant in dups and dups[variant][0]:
        return dups[variant][0]
    candidate = rest[:-4] if reason == "unzipped" and rest.lower().endswith(".zip") else rest
    return candidate if candidate in books else ""


def load_variant_context(db_path: str, bases: tuple[Path, ...]) -> tuple[set[str], dict[str, tuple[str, int]]]:
    """Пути книг библиотеки и дубли из dup_members: путь варианта в карантине → (keeper, words).
    Без файла базы — пусто; dup_members есть со схемы v2."""
    if not Path(db_path).is_file():
        return set(), {}
    with closing(connect(db_path, readonly=True)) as conn:
        books = {row[0] for row in conn.execute("SELECT path FROM books")}
        dups: dict[str, tuple[str, int]] = {}
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
            for quarantine, keeper, words in conn.execute(
                    "SELECT quarantine, keeper, words FROM dup_members WHERE role='duplicate' AND quarantine <> ''"):
                rel = quarantine_relpath(quarantine, bases)
                if rel is not None:
                    dups[rel] = (keeper, words)
    return books, dups


def variant_meta(qroot: Path, variant: str, cache: TextCache | None) -> tuple[dict[str, Any], str | None, int]:
    """Разбор файла карантина тем же read_book_meta, что у scan (detail), и sha1 потоком. Симлинк не разыменовывается.
    Ошибки не прерывают прогон: в meta — поле error, sha1 — None."""
    reason, _, rest = variant.partition("/")
    full = qroot / variant
    try:
        info = full.lstat()
    except OSError as error:
        return {**path_meta(rest), "error": f"OSError: {error}"}, None, 0
    if full.is_symlink():
        return {**path_meta(rest), "error": "симлинк: не читается и не разбирается"}, None, info.st_size
    meta = read_book_meta(qroot / reason, rest, info.st_size, info.st_mtime, cache, detail=True)
    digest = hashlib.sha1(usedforsecurity=False)
    try:
        with full.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        meta.setdefault("error", f"OSError: {error}")
        return meta, None, info.st_size
    return meta, digest.hexdigest(), info.st_size


def variant_row(qroot: Path, variant: str, books: set[str], dups: dict[str, tuple[str, int]],
                cache: TextCache | None) -> tuple[Any, ...]:
    """Строка book_variants (порядок VARIANT_INSERT) для файла карантина."""
    reason = variant.partition("/")[0]
    meta, sha1, size = variant_meta(qroot, variant, cache)
    dup = dups.get(variant)
    words = dup[1] if dup else next((value for key, value in meta.get("kv", []) if key == "words"), None)
    series_no = meta.get("series_no")
    return (variant, reason, variant_keeper_for(variant, books, dups), str(meta.get("fmt", "")), size, words,
            str(meta.get("title", "")), "; ".join(meta.get("authors", [])), str(meta.get("series", "")),
            series_no if isinstance(series_no, int) else None, str(meta.get("lang", "")), str(meta.get("year", "")),
            json.dumps(meta, ensure_ascii=False, default=str), sha1)


def record_variants(db_path: str, qroot: Path, variants: list[str], books: set[str],
                    dups: dict[str, tuple[str, int]], cache: TextCache | None, progress: PurgeProgress) -> tuple[int, int]:
    """Запись вариантов пачками по VARIANT_BATCH в транзакции. Возвращает (записано, с keeper).
    Сбой записи (sqlite3.Error) — исключение: вызывающий не начинает удаление."""
    written = with_keeper = 0
    batch: list[tuple[Any, ...]] = []
    with closing(connect(db_path)) as conn:
        for variant in variants:
            row = variant_row(qroot, variant, books, dups, cache)
            batch.append(row)
            with_keeper += bool(row[2])
            progress.advance()
            if len(batch) >= VARIANT_BATCH:
                with conn:
                    conn.executemany(VARIANT_INSERT, batch)
                written += len(batch)
                batch.clear()
        if batch:
            with conn:
                conn.executemany(VARIANT_INSERT, batch)
            written += len(batch)
    return written, with_keeper


def purge_delete(reason_dir: Path, progress: PurgeProgress, stats: dict[str, int],
                 on_removed: Callable[[str], None]) -> None:
    """Удаление снизу вверх: файлы и симлинки — os.unlink (ссылка, не цель), каталоги — os.rmdir; затем сама папка причины.
    on_removed(путь) вызывается после удаления каждого файла или симлинка (не должна бросать исключений)."""
    def onerror(error: OSError) -> None:
        purge_fail(stats, f"обход {error.filename}: {error.strerror}")

    for current, dirs, files in os.walk(reason_dir, topdown=False, onerror=onerror, followlinks=False):
        for name in files:
            full = os.path.join(current, name)
            try:
                size = os.lstat(full).st_size
                os.unlink(full)
            except OSError as error:
                purge_fail(stats, f"не удалён файл {full}: {error.strerror}")
                continue
            stats["files"] += 1
            stats["bytes"] += size
            on_removed(full)
            progress.advance()
        for name in dirs:
            full = os.path.join(current, name)
            try:
                if os.path.islink(full):
                    size = os.lstat(full).st_size
                    os.unlink(full)
                    stats["files"] += 1
                    stats["bytes"] += size
                    on_removed(full)
                    progress.advance()
                else:
                    os.rmdir(full)
                    stats["dirs"] += 1
            except OSError as error:
                purge_fail(stats, f"не удалён {full}: {error.strerror}")
    try:
        os.rmdir(reason_dir)
        stats["dirs"] += 1
    except OSError as error:
        purge_fail(stats, f"не удалена папка причины {reason_dir}: {error.strerror}")


def purge_quarantine(cfg: dict[str, Any], reasons: list[str], dry_run: bool) -> dict[str, Any]:
    """Окончательное удаление папок карантина по причинам. Сначала инвентарь; в dry-run — только счётчики.
    Варианты причин VARIANT_REASONS сохраняются в book_variants ДО JSON-отчёта и удаления; сбой записи — ничего не удаляется.
    Боевой запуск: JSON-отчёт со всеми путями пишется ДО удаления, при сбое записи отчёта ничего не удаляется."""
    qroot = Path(cfg["quarantine_dir"]).resolve()
    inventory: dict[str, list[tuple[str, int]]] = {}
    errors: list[str] = []
    for reason in reasons:
        if reason not in QUARANTINE_REASONS:
            raise ConfigError(f"неизвестная причина карантина {reason!r}; допустимо: {', '.join(QUARANTINE_REASONS)}")
        reason_dir = qroot / reason
        if reason_dir.is_symlink():
            raise ConfigError(f"{reason}: папка причины — симлинк, удаление отменено")
        if not reason_dir.exists():
            inventory[reason] = []
            continue
        if not reason_dir.is_dir():
            raise ConfigError(f"{reason}: в карантине не каталог")
        resolved = reason_dir.resolve()
        if resolved == qroot or not resolved.is_relative_to(qroot):
            raise ConfigError(f"{reason}: путь вне карантина: {resolved}")
        inventory[reason] = purge_inventory(qroot, reason_dir, errors)

    total = sum(len(items) for items in inventory.values())
    size = sum(s for items in inventory.values() for _, s in items)
    for reason, items in inventory.items():
        say(f"purge-quarantine{' (dry-run)' if dry_run else ''}: {reason}: файлов {len(items)}, "
            f"{sum(s for _, s in items)} байт", "purge_plan")
    db_path = cfg["db_path"]
    bases = (Path(os.path.normpath(cfg["quarantine_dir"])), qroot)
    variants = [rel for reason in VARIANT_REASONS for rel, _ in inventory.get(reason, [])]

    if dry_run:
        books, dups = load_variant_context(db_path, bases)
        with_keeper = sum(bool(variant_keeper_for(v, books, dups)) for v in variants)
        say(f"purge-quarantine (dry-run): вариантов в book_variants {len(variants)}, с keeper {with_keeper}, "
            f"без keeper {len(variants) - with_keeper}", "purge_plan")
        say(f"purge-quarantine (dry-run): всего файлов {total}, {size} байт; ничего не удалено, отчёт не записан",
            "purge_plan")
        return {"files": total, "bytes": size, "variants": len(variants), "variants_with_keeper": with_keeper,
                "dry_run": True}

    variant_stats = {"written": 0, "with_keeper": 0}
    if variants:
        migrate_database(db_path, out=lambda line: say(line, "purge_migrate"))
        books, dups = load_variant_context(db_path, bases)
        cache = TextCache.open(cfg)
        try:
            written, with_keeper = record_variants(db_path, qroot, variants, books, dups, cache,
                                                   PurgeProgress(len(variants), "purge-quarantine: варианты"))
        finally:
            if cache is not None:
                cache.close()
        variant_stats = {"written": written, "with_keeper": with_keeper}
        say(f"purge-quarantine: в book_variants записано {written}, с keeper {with_keeper}", "purge_variants")

    data = {"quarantine_dir": str(qroot), "created": datetime.now().isoformat(timespec="seconds"),
            "reasons": {reason: {"files": len(items), "bytes": sum(s for _, s in items),
                                 "items": [{"path": p, "bytes": s} for p, s in items]}
                        for reason, items in inventory.items()},
            "variants": variant_stats, "inventory_errors": errors}
    try:
        report = write_report("purge-quarantine", data)
    except OSError as error:
        raise OSError(f"отчёт не записан, карантин не тронут: {error}") from error
    say(f"purge-quarantine: отчёт {report}; к удалению файлов {total}, {size} байт", "purge_report")

    progress = PurgeProgress(total, "purge-quarantine: удаление")
    stats = {"files": 0, "bytes": 0, "dirs": 0, "errors": 0}
    marker = connect(db_path) if variants else None

    def on_removed(full: str) -> None:
        """Время удаления — в book_variants.removed; сбой записи не прерывает удаление."""
        if marker is None:
            return
        rel = Path(full).relative_to(qroot).as_posix()
        reason = rel.partition("/")[0]
        if reason not in VARIANT_REASONS:
            return
        try:
            with marker:
                marker.execute("UPDATE book_variants SET removed=? WHERE variant_path=? AND reason=?",
                               (time.time(), rel, reason))
        except sqlite3.Error as error:
            purge_fail(stats, f"removed не записан для {rel}: {error}")

    try:
        for reason in inventory:
            reason_dir = qroot / reason
            if not reason_dir.exists():
                continue
            if reason_dir.is_symlink() or not reason_dir.is_dir():
                purge_fail(stats, f"{reason}: папка причины изменилась после отчёта, пропущена")
                continue
            purge_delete(reason_dir, progress, stats, on_removed)
    finally:
        if marker is not None:
            marker.close()
    say(f"purge-quarantine: готово, файлов {stats['files']}, каталогов {stats['dirs']}, байт {stats['bytes']}, "
        f"ошибок {stats['errors']}", "purge_done")
    return stats


FILE_CHANGING = ("fix-encoding", "unpack-fb2zip", "convert", "dedup", "dedup-folders", "unzip-books",
                 "author-variants", "reconvert-fb2", "fix-misplaced", "merge-folder", "strip-suffix", "name-variants", "windows-names", "enrich-epub")
COMMANDS = ("scan", "migrate", *FILE_CHANGING)
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_OUTPUT_LINES = 2000


def command_params(command: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Проверяет и нормализует параметры режима (общие для CLI и API). Неизвестные ключи — ошибка."""
    if command not in (*COMMANDS, *CLI_COMMANDS):
        raise ConfigError(f"неизвестная команда {command!r}; допустимо: {', '.join(COMMANDS)}")
    allowed = {
        "scan": {"reparse"}, "migrate": set(), "fix-encoding": {"path", "only"},
        "unpack-fb2zip": {"path", "max_mb", "multi", "out", "delete_source", "ignore_md5"},
        "convert": {"path", "only", "to", "overwrite", "skip_newer", "exclude", "author", "title_mode", "toc", "toc_depth",
                    "delete_source", "workers", "allow_no_author"},
        "dedup": {"path", "workers"}, "dedup-folders": {"path", "workers"},
        "unzip-books": {"path", "workers"}, "unpack-collections": {"path"}, "merge-authors": set(),
        "author-variants": set(), "sort-underscore": {"limit"},
        "reconvert-fb2": {"path", "workers", "limit", "sample", "out", "epubcheck"},
        "misplaced": set(), "check-folders": set(), "fix-misplaced": set(),
        "export-decisions": set(), "import-decisions": set(),
        "merge-folder": {"from", "to", "non_books_to_quarantine"},
        "strip-suffix": {"path"}, "name-variants": {"path"}, "windows-names": {"path"}, "enrich-epub": {"path"}, "purge-quarantine": {"reasons"}, "move-books": {"list_path"},
    }[command]
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{command}: неизвестные параметры {', '.join(sorted(unknown))}")
    params: dict[str, Any] = {}
    if "reparse" in allowed:
        if not isinstance(raw.get("reparse", False), bool):
            raise ConfigError("reparse: логическое значение")
        params["reparse"] = raw.get("reparse", False)
    if "path" in allowed:
        path = raw.get("path")
        if path is not None and (not isinstance(path, str) or not path or "\x00" in path):
            raise ConfigError("path должен быть непустой строкой")
        params["path"] = path
    if "only" in allowed:
        valid = ("all", "fb2", "txt") if command == "fix-encoding" else ("all", "fb2", "html", *CONVERT_SOURCES)
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
        params.update(multi=raw.get("multi", False), delete_source=raw.get("delete_source", False),
                      ignore_md5=raw.get("ignore_md5", False), out=raw.get("out") or None)
        if not all(isinstance(params[k], bool) for k in ("multi", "delete_source", "ignore_md5")) or \
                not (params["out"] is None or isinstance(params["out"], str)):
            raise ConfigError("multi, delete_source, ignore_md5 — логические значения; out — строка пути")
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
        for key in ("overwrite", "skip_newer", "delete_source", "allow_no_author"):
            params[key] = raw.get(key, False)
        params["toc"] = raw.get("toc", True)
        if params["to"] not in ("epub", "fb2") or params["title_mode"] not in ("auto", "filename", "keep"):
            raise ConfigError("to: epub|fb2; title_mode: auto|filename|keep")
        if not isinstance(params["toc_depth"], int) or isinstance(params["toc_depth"], bool) or not 1 <= params["toc_depth"] <= 6:
            raise ConfigError("toc_depth: целое 1..6")
        if not all(isinstance(params[k], bool) for k in ("overwrite", "skip_newer", "delete_source", "toc", "allow_no_author")):
            raise ConfigError("overwrite, skip_newer, delete_source, toc, allow_no_author — логические значения")
        if params["allow_no_author"] and params["only"] != "html":
            raise ConfigError("allow_no_author задаётся только вместе с only=html")
        if not isinstance(params["author"], str) or not isinstance(params["exclude"], list) or \
                not all(isinstance(x, str) for x in params["exclude"]):
            raise ConfigError("author — строка, exclude — список строк")
    if command == "merge-folder":
        sources, target = raw.get("from"), raw.get("to")
        if not isinstance(sources, list) or not sources or not all(isinstance(x, str) and x for x in sources):
            raise ConfigError("from: список непустых имён папок")
        if not isinstance(target, str) or not target:
            raise ConfigError("to: имя папки")
        non_books = raw.get("non_books_to_quarantine", False)
        if not isinstance(non_books, bool):
            raise ConfigError("non_books_to_quarantine: true или false")
        params.update(sources=list(sources), target=target, non_books_to_quarantine=non_books)
    if command == "purge-quarantine":
        reasons = raw.get("reasons")
        if not isinstance(reasons, list) or not reasons or not all(isinstance(x, str) for x in reasons):
            raise ConfigError("reasons: непустой список причин карантина (--reason)")
        unknown = [x for x in reasons if x not in QUARANTINE_REASONS]
        if unknown:
            raise ConfigError(f"purge-quarantine: неизвестная причина {', '.join(unknown)}; "
                              f"допустимо: {', '.join(QUARANTINE_REASONS)}")
        params["reasons"] = list(dict.fromkeys(reasons))
    if command == "move-books":
        if not isinstance(raw.get("list_path"), Path):
            raise ConfigError("list_path: путь к файлу списка (--list)")
        params["list_path"] = raw["list_path"]
    return params


def run_command(cfg: dict[str, Any], command: str, dry_run: bool, params: dict[str, Any],
                out: Callable[[str], None] = print) -> Any:
    """Единая точка запуска режимов для CLI и API. Изменяющий запуск берёт общую блокировку (код 3 — занято)."""
    def action() -> Any:
        if command == "scan":
            return scan(cfg, dry_run, out=out, reparse=params.get("reparse", False))
        if command == "migrate":
            return {"schema": migrate_database(cfg["db_path"], dry_run, out=out)}
        if command == "fix-encoding":
            return fix_encoding(cfg, dry_run, params["path"], params["only"], out=out)
        if command == "unpack-fb2zip":
            return unpack_fb2zip(cfg, dry_run, params["path"], params["max_mb"], out=out, multi=params["multi"],
                                 out_dir=params["out"], delete_source=params["delete_source"],
                                 ignore_md5=params["ignore_md5"])
        if command == "dedup":
            return dedup(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "dedup-folders":
            return dedup_folders(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "author-variants":
            return author_variants(cfg, dry_run, out=out)
        if command == "unzip-books":
            return unzip_books(cfg, dry_run, params["path"], params["workers"], out=out)
        if command == "unpack-collections":
            return unpack_collections(cfg, dry_run, params["path"], out=out)
        if command == "sort-underscore":
            return sort_underscore(cfg, dry_run, params["limit"], out=out)
        if command == "merge-authors":
            return merge_authors(cfg, dry_run, out=out)
        if command == "misplaced":
            return misplaced(cfg, dry_run, out=out)
        if command == "check-folders":
            return check_folders(cfg, dry_run, out=out)
        if command == "fix-misplaced":
            return fix_misplaced(cfg, dry_run, out=out)
        if command == "merge-folder":
            return folder_merge(cfg, params["sources"], params["target"], dry_run, out=out,
                                non_books_to_quarantine=params["non_books_to_quarantine"])
        if command == "strip-suffix":
            return strip_suffix(cfg, dry_run, params["path"], out=out)
        if command == "name-variants":
            return name_variants(cfg, dry_run, params["path"], out=out)
        if command == "enrich-epub":
            return enrich_epub(cfg, dry_run, params["path"], out=out)
        if command == "windows-names":
            return windows_names(cfg, dry_run, params["path"], out=out)
        if command == "purge-quarantine":
            return purge_quarantine(cfg, params["reasons"], dry_run)
        if command == "move-books":
            return move_books(cfg, params["list_path"], dry_run, out=out)
        if command == "export-decisions":
            return export_decisions(cfg, dry_run, out=out)
        if command == "import-decisions":
            return import_decisions_command(cfg, dry_run, out=out)
        if command == "reconvert-fb2":
            return reconvert_fb2(cfg, dry_run, params["path"], params["workers"], params["limit"], params["sample"],
                                 params["out"], params["epubcheck"], out=out)
        if params["only"] == "fb2":
            return convert_fb2(cfg, dry_run, params["path"], params["workers"], out=out)
        return convert_documents(cfg, dry_run, params["path"], out=out,
                                 **{k: v for k, v in params.items() if k not in ("path", "workers")})

    # purge-quarantine пишет в book_variants общей БД, поэтому боевой запуск берёт scan_lock как остальные изменяющие
    # режимы (карантин — не каталог книг, но база одна). Dry-run только читает и блокировку не берёт.
    # check-folders и export-decisions только читают и блокировку не берут (как dry-run).
    if dry_run or command in ("migrate", "check-folders", "export-decisions"):
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


def read_head(path: Path, size: int) -> bytes:
    """Начало файла не длиннее size байт (для метаданных: большой файл — не ошибка, в отличие от read_limited)."""
    with path.open("rb") as handle:
        return handle.read(size)


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
.details{width:100%;max-width:48rem;margin:.4rem 0 1rem}.details th,.details td{vertical-align:top;padding:.25rem .6rem .25rem 0;
border-bottom:1px solid var(--line)}.details th{color:var(--mut);font-weight:400;white-space:nowrap;width:1%}
.details td{overflow-wrap:anywhere}.letters a{display:inline-block;min-width:1.6rem}.reader{max-width:42rem;margin:0 auto;padding:1rem;font:1.15rem/1.65 Georgia,serif}
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


class Collection(NamedTuple):
    """Строка book_collections книги. subsection и kind — со схемы v6 (раньше пусто)."""
    section: str
    collection: str
    subsection: str
    kind: str  # folder, dedup, а для source='meta' — edition, publisher-series, set
    source: str  # folder, dedup или meta


META_LABELS = {
    CONVERT_ERROR: "Отказ конвертации fb2",
    "author_full": "Автор (полностью)", "translator": "Переводчик", "editor": "Редактор",
    "illustrator": "Иллюстратор", "contributor": "Участник", "keyword": "Ключевые слова", "keywords": "Ключевые слова",
    "date": "Дата", "src_lang": "Язык оригинала", "lang": "Язык", "language": "Язык", "series_extra": "Серия (доп.)",
    "coverpage": "Обложка (файл)", "src_title": "Оригинальное название", "src_author": "Автор оригинала",
    "doc_author": "Автор документа", "program_used": "Программа", "doc_date": "Дата документа", "src_ocr": "OCR",
    "doc_id": "ID документа", "doc_version": "Версия документа", "history": "История правок",
    "src_url": "Ссылка на источник", "book_name": "Название издания", "publisher": "Издательство", "city": "Город",
    "publish_year": "Год издания", "isbn": "ISBN", "publisher_series": "Издательская серия",
    "publisher_series_no": "Номер в издательской серии", "epub_version": "Версия EPUB", "identifier": "Идентификатор",
    "subject": "Тема", "rights": "Права", "source": "Источник", "coverage": "Охват", "type": "Тип",
    "modified": "Изменено", "rating": "Оценка", "timestamp": "Добавлено в библиотеку",
    "title_sort": "Сортировочное название", "belongs_to_collection": "Сборник (EPUB)",
    "collection_type": "Тип сборника", "group_position": "Номер в сборнике", "pages": "Страниц",
    "author": "Автор (MOBI)", "description": "Описание", "publishing_date": "Дата публикации",
    "updated_title": "Название (обновлённое)", "title": "Название (метаданные)", "creator": "Автор (метаданные)",
    "created": "Создано", "last_modified_by": "Кто изменил", "text_author": "Автор (из текста)",
}
META_KIND_LABELS = {"edition": "издание", "publisher-series": "издательская серия", "set": "сборник"}


def meta_label(key: str) -> str:
    """Подпись ключа book_meta; pdf:/djvu:/custom: — с префиксом формата, неизвестный ключ — сам ключ."""
    if key in META_LABELS:
        return META_LABELS[key]
    prefix, _, field = key.partition(":")
    return {"pdf": f"PDF: {field}", "djvu": f"DjVu: {field}", "custom": f"Доп.: {field}"}.get(prefix, key)


def book_details(conn: sqlite3.Connection, path: str) -> list[tuple[str, list[str]]]:
    """Все book_meta книги: (ключ, значения) — в порядке META_LABELS, затем прочие ключи по алфавиту.
    Подпись — meta_label(ключ). До схемы v6 таблицы book_meta нет — пусто."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_META:
        return []
    grouped: dict[str, list[str]] = {}
    for key, value in conn.execute("SELECT key, value FROM book_meta WHERE path=? ORDER BY key, value", (path,)):
        grouped.setdefault(key, []).append(value)
    order = {key: number for number, key in enumerate(META_LABELS)}
    return sorted(grouped.items(), key=lambda item: (order.get(item[0], len(order)), item[0]))


def details_html(details: list[tuple[str, list[str]]]) -> str:
    """Блок «Сведения»: таблица подпись → значения; значения экранируются, длинные переносятся (CSS .details)."""
    if not details:
        return ""
    rows = []
    for key, values in details:
        cells = "<br>".join(e(value) for value in values)
        rows.append(f"<tr><th>{e(meta_label(key))}</th><td>{cells}</td></tr>")
    return f'<h3>Сведения</h3><table class="details">{"".join(rows)}</table>'


def book_variants(conn: sqlite3.Connection, path: str) -> list[sqlite3.Row]:
    """Варианты книги из карантина (book_variants, схема v7): keeper — путь книги. До схемы v7 таблицы нет — пусто."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VARIANTS:
        return []
    return conn.execute("SELECT * FROM book_variants WHERE keeper=? ORDER BY reason, variant_path", (path,)).fetchall()


def variants_html(variants: list[sqlite3.Row]) -> str:
    """Блок «Варианты»: причина, формат, размер, слов, название и авторы варианта, дата удаления; значения экранируются."""
    if not variants:
        return ""
    rows = []
    for variant in variants:
        removed = (datetime.fromtimestamp(variant["removed"]).strftime("%Y-%m-%d %H:%M")
                   if variant["removed"] else "ещё в карантине")
        size = human_size(variant["size"]) if variant["size"] is not None else "—"
        words = str(variant["words"]) if variant["words"] is not None else "—"
        name = variant["title"] or PurePosixPath(variant["variant_path"]).name
        reason = VARIANT_REASON_TEXT.get(variant["reason"], variant["reason"])
        rows.append(f"<tr><td>{e(reason)}</td><td>{e(variant['fmt'])}</td><td>{e(size)}</td><td>{e(words)}</td>"
                    f"<td>{e(name)}<br><span class=mut>{e(variant['authors'])}</span></td><td>{e(removed)}</td></tr>")
    return ('<h3>Варианты</h3><table class="details"><tr><th>Причина</th><th>Формат</th><th>Размер</th><th>Слов</th>'
            '<th>Название и авторы</th><th>Удалён</th></tr>' + "".join(rows) + "</table>")


def book_collections(conn: sqlite3.Connection, paths: list[str]) -> dict[str, list[Collection]]:
    """Все сборники книг по путям из book_collections (папки, дубли и сборники из метаданных).
    До схемы v4 таблицы нет — пусто; subsection и kind появляются со схемы v6."""
    found: dict[str, list[Collection]] = {}
    unique = list(dict.fromkeys(paths))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if not unique or version < SCHEMA_COLLECTIONS:
        return found
    columns = "subsection, kind, source" if version >= SCHEMA_META else "'' AS subsection, '' AS kind, source"
    for start in range(0, len(unique), 500):
        chunk = unique[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT DISTINCT path, section, collection, {columns} FROM book_collections WHERE path IN ({marks}) "
                "ORDER BY path, source, section, collection, subsection", chunk):
            found.setdefault(row[0], []).append(Collection(*row[1:]))
    return found


def section_counts(conn: sqlite3.Connection, order: str) -> list[sqlite3.Row]:
    """Разделы и сборники с числом книг: сборники — как VIEW collections (схема v4), но без сборников из метаданных
    (source='meta' — их вид в section_counts не попадает); книги без сборника — из books."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_COLLECTIONS:
        source = ("SELECT bc.section AS section, bc.collection AS collection, count(DISTINCT bc.path) AS n "
                  "FROM book_collections bc JOIN books b ON b.path = bc.path WHERE bc.source <> 'meta' "
                  "GROUP BY bc.section, bc.collection UNION ALL "
                  "SELECT section, '', count(*) FROM books WHERE collection='' GROUP BY section")
    else:
        source = "SELECT section, collection, count(*) AS n FROM books GROUP BY section, collection"
    return conn.execute(f"SELECT section, collection, n FROM ({source}) ORDER BY {order}").fetchall()


def meta_collection_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Сборники из метаданных (source='meta') по виду с числом книг каталога; до схемы v6 — пусто."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_META:
        return []
    return conn.execute("SELECT bc.kind AS kind, bc.collection AS collection, count(DISTINCT bc.path) AS n "
                        "FROM book_collections bc JOIN books b ON b.path = bc.path WHERE bc.source = 'meta' "
                        "GROUP BY bc.kind, bc.collection ORDER BY bc.kind, bc.collection").fetchall()


def collection_tag(section: str, collection: str, subsection: str = "") -> str:
    """Тег-ссылка на сборник (и его подраздел): поиск по разделу, сборнику и подразделу. Подпись: «Сборник / подраздел»."""
    link = f"/?section={quote(section)}&collection={quote(collection)}"
    label = collection
    if subsection:
        link += f"&subsection={quote(subsection, safe='')}"
        label += " / " + " / ".join(part for part in subsection.split("/") if part)
    return f'<a class="tag" href="{link}&sort=path">{e(label)}</a>'


def book_json(row: sqlite3.Row, colls: list[Collection] | None = None) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k not in ("mtime",)}  # noqa: SIM118 — у sqlite3.Row нет __iter__ по ключам
    data["path"] = display_path(row["path"], row["fmt"])
    data["authors"] = [a for a in row["authors"].split("; ") if a]
    data["genres"] = [g for g in row["genres"].split("; ") if g]
    data["cover"] = bool(row["cover"])
    data["links"] = {"page": f"/book/{row['id']}", "file": f"/file/{row['id']}", "read": f"/read/{row['id']}",
                     "cover": f"/cover/{row['id']}" if row["cover"] else None}
    if is_wrapped(row["path"], row["fmt"]):
        data["links"]["raw"] = f"/api/books/{row['id']}/file"  # исходный однокнижный архив — только через API
    data["collections"] = [c._asdict() for c in colls or []]
    return data


API_HELP = {
    "GET /api/books": "поиск: q, author, series, genre, section, collection, subsection (с collection), fmt, lang, status, "
                      "shelf, fav=1, dir, sort, page, per_page (≤500)",
    "GET /api/books/<id>": "книга с отметками, полками, сведениями (details: key, label, values) и сборниками",
    "POST /api/books/<id>/mark": "{status?, rating?, favorite?, note?} — частичное обновление",
    "POST /api/books/<id>/shelves": "{shelf_ids: [..]} — заменить полки книги",
    "GET /api/authors | /api/series": "q (подстрока) или l (первая буква); не больше 1000",
    "GET /api/genres | /api/sections | /api/shelves | /api/recent | /api/stats": "справочники и статистика",
    "POST /api/shelves": "{name} — создать полку",
    "POST /api/shelves/<id>/delete": "удалить полку (книги не трогаются)",
    "GET /api/jobs, GET /api/jobs/<id>": "фоновые задания: состояние, вывод плана, результат",
    "POST /api/jobs": "{command: scan|migrate|fix-encoding|unpack-fb2zip|convert|dedup|dedup-folders|unzip-books|"
                      "author-variants, dry_run: true (по умолчанию), "
                      "confirm: <id успешного dry-run с теми же параметрами> — обязателен для боевого запуска fix-encoding/unpack-fb2zip/"
                      "convert/dedup/dedup-folders/unzip-books/author-variants/enrich-epub, "
                      "параметры режима: path, only (для convert — fb2 и источники), workers, max_mb, multi, out, ignore_md5 (unpack-fb2zip), to, overwrite, skip_newer, exclude, "
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
    "POST /api/watch": "{action: start|stop|status} — мониторинг каталога книг до перезапуска serve; состояние — в watch",
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
            ("POST", "book"): self.post_book, ("POST", "shelves"): self.post_shelves, ("POST", "scan"): self.post_scan, ("POST", "watch"): self.post_watch,
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
        subsection = params.get("subsection", "")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if collection and version >= SCHEMA_COLLECTIONS:
            # сборник — по book_collections: книга-keeper из другого раздела тоже попадает в свой сборник;
            # подраздел (схема v6) — только вместе со сборником; сборники из метаданных сюда не входят
            sub_sql = " AND subsection=?" if subsection and version >= SCHEMA_META else ""
            where.append("b.path IN (SELECT path FROM book_collections WHERE collection=? AND (?='' OR section=?) "
                         f"AND source <> 'meta'{sub_sql})")
            args.extend([collection, section, section, *([subsection] if sub_sql else [])])
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
                         for k in ("author", "series", "genre", "dir", "lang", "collection", "subsection") if params.get(k))
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
                          for k, v in params.items()
                          if k in {"author", "series", "genre", "dir", "lang", "collection", "subsection"})
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
    def section_tag(row: sqlite3.Row, colls: list[Collection] | None = None) -> str:
        """Теги раздела книги и её сборников (без сборников из метаданных); colls — из book_collections
        (None — прежний вид по books)."""
        tag = f'<a class="tag" href="/?section={quote(row["section"])}">{e(row["section"])}</a>' if row["section"] else ""
        if colls is None:
            colls = [Collection(row["section"], row["collection"], "", "", "")] if row["collection"] else []
        return tag + "".join(collection_tag(c.section, c.collection, c.subsection)
                             for c in colls if c.collection and c.source != "meta")

    def view_sections(self, _parts: list[str]) -> None:
        """Разделы (папки «_…») с коллекциями: журналы, сборники, документация, неразобранное."""
        with closing(self.db()) as conn:
            rows = section_counts(conn, "section = '', section, collection")
            meta = meta_collection_counts(conn)
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
        if meta:  # сборники из метаданных — отдельно от разделов, без ссылок: поиск по ним не ведётся
            meta_items = "".join(f'<span>{e(META_KIND_LABELS.get(row["kind"], row["kind"]))}: {e(row["collection"])} '
                            f'<span class="mut">{row["n"]}</span></span>' for row in meta)
            html_parts.append(f'<h3>Сборники из метаданных</h3><div class="cols">{meta_items}</div>')
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
        watch = watch_status()
        if not self.cfg["watch_interval"]:
            watch_info = "<p class='mut'>Мониторинг выключен в настройках (watch_interval = 0).</p>"
        elif watch["running"]:
            details = [f"опрос каждые {watch['interval']} с", f"последняя проверка: {watch['last_check'] or '—'}",
                       f"ждут scan: {watch['pending']}", f"запущено scan: {watch['scans']}", f"ошибок: {watch['errors']}"]
            details += [f"пауза: {watch['paused']}"] if watch["paused"] else []
            details += [f"последняя ошибка: {watch['last_error']}"] if watch["last_error"] else []
            watch_info = (f"<p>Мониторинг включён: {e(' · '.join(details))}.</p><form method='post' action='/watch'>"
                          "<input type='hidden' name='action' value='stop'><button>Остановить мониторинг</button></form>")
        else:
            watch_info = ("<p>Мониторинг остановлен.</p><form method='post' action='/watch'>"
                          "<input type='hidden' name='action' value='start'><button>Включить мониторинг</button></form>")
        scan_info = (f'{progress}<form method="post" action="/scan"><button{" disabled" if running else ""}>Пересканировать '
                     f'библиотеку</button></form>' + (f"<h3>Задания</h3><table>{job_rows}</table>" if job_rows else ""))
        self.page("Статистика", f"<h2>Статистика</h2><p>Книг: <b>{total[0]}</b> ({human_size(total[1])}), авторов: {authors}. "
                                f"Каталог: {e(self.cfg['books_dir'])}. Последнее сканирование: {e(last[0] if last else 'не было')}.</p>"
                                f"<table><tr><th>Формат</th><th>Книг</th><th>Объём</th></tr>{rows}</table>"
                                f"<h3>Чтение</h3><table>{marks or '<tr><td>отметок пока нет</td></tr>'}</table>"
                                f"<h3>Каталог</h3>{scan_info}"
                                f"<h3>Мониторинг</h3>{watch_info}"
                                f"<p class='mut'>OPDS для читалок: /opds · JSON API: <a href='/api'>/api</a></p>")

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
            details = book_details(conn, book["path"])
            variants = book_variants(conn, book["path"])
        bid = book["id"]
        authors = ", ".join(f'<a href="/?author={quote(a)}">{e(a)}</a>' for a in book["authors"].split("; ") if a) or "—"
        genres = " ".join(f'<a class="tag" href="/?genre={quote(g)}">{e(genre_name(g))}</a>' for g in book["genres"].split("; ") if g)
        series = (f'<p>Серия: <a href="/?series={quote(book["series"])}&sort=series">{e(book["series"])}</a>'
                  f'{" #" + str(book["series_no"]) if book["series_no"] else ""}</p>') if book["series"] else ""
        collections = "".join(collection_tag(c.section, c.collection, c.subsection)
                              for c in colls if c.collection and c.source != "meta")
        series += f'<p>Сборники: {collections}</p>' if collections else ""
        from_meta = [c for c in colls if c.source == "meta"]
        if from_meta:
            series += ('<p class="mut">Из метаданных:</p><ul>'
                       + "".join(f"<li>{e(META_KIND_LABELS.get(c.kind, c.kind))}: {e(c.collection)}</li>" for c in from_meta)
                       + "</ul>")
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
        body += details_html(details)
        body += variants_html(variants)
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

    def post_watch(self, _parts: list[str]) -> None:
        try:
            watch_action(self.cfg, self.form.get("action", [""])[-1])
        except (ConfigError, ValueError) as error:
            say(f"мониторинг: {error}", "watch_failed", logging.ERROR)
        self.redirect("/stats")

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
                details = [{"key": key, "label": meta_label(key), "values": values}
                           for key, values in book_details(conn, book["path"])]
                self.send_json({**book_json(book, colls.get(book["path"])), "mark": dict(mark) if mark else None,
                                "shelves": [dict(x) for x in shelves], "details": details})
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
                                "statuses": {r[0]: r[1] for r in statuses}, "scan": SCAN_STATE, "watch": watch_status()})
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
            if name == "watch" and len(parts) == 1:
                self.send_json({"ok": True, "message": watch_action(self.cfg, body.get("action")), "watch": watch_status()})
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
        print(f"[dry-run] будет: мониторинг каталога книг каждые {cfg['watch_interval']} с, при изменениях — scan"
              if cfg["watch_interval"] else "[dry-run] будет: мониторинг каталога книг выключен (watch_interval = 0)")
        migrate_database(cfg["db_path"], dry_run=True)
        say("план запуска сервера", "dry_serve", detail=url)
        return
    if not Path(cfg["books_dir"]).is_dir():
        say(f"каталог книг сейчас недоступен: {cfg['books_dir']}", "books_dir_missing", logging.WARNING)
    migrate_database(cfg["db_path"])
    Handler.cfg = cfg
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    server.daemon_threads = True

    if cfg["watch_interval"]:
        start_watch(cfg)
    say(f"веб-интерфейс: {url}", "serve", detail=url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        say("веб-интерфейс остановлен", "serve_stop")
        stop_watch(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Лёгкая веб-библиотека книг: каталог в SQLite и интерфейс в браузере.")
    parser.add_argument("command", nargs="?", choices=("serve", "scan", *FILE_CHANGING, *CLI_COMMANDS), default="serve",
                        help="serve (по умолчанию), scan, fix-encoding, unpack-fb2zip, convert, reconvert-fb2, dedup, dedup-folders, "
                             "unzip-books, unpack-collections, merge-authors, sort-underscore, author-variants, "
                             "misplaced, check-folders (только отчёт), fix-misplaced, merge-folder (--from A --to B), strip-suffix, "
                             "name-variants, windows-names, enrich-epub, move-books (--list ФАЙЛ) "
                             "или purge-quarantine (--reason R, необратимо)")
    parser.add_argument("--reason", dest="reasons", action="append", default=[],
                        help="purge-quarantine: причина карантина, папка которой удаляется окончательно (можно повторять): "
                             + ", ".join(QUARANTINE_REASONS))
    parser.add_argument("-p", "--path", help="fix-encoding/unpack-fb2zip/convert/strip-suffix/name-variants/windows-names/enrich-epub: каталог (по умолчанию каталог книг)")
    parser.add_argument("--from", dest="from_folders", action="append", default=[],
                        help="merge-folder: папка верхнего уровня или папка внутри «_…» (например «_Сборники/Серия/Автор»), "
                             "которую склеить или переименовать (можно повторять)")
    parser.add_argument("--list", dest="list_path", type=Path,
                        help="move-books: файл списка UTF-8, строки «откуда<TAB>куда» (пути относительно каталога книг)")
    parser.add_argument("--only", default="all",
                        help="fix-encoding: all|fb2|txt; convert: all|fb2|doc|docx|rtf|odt|mht|mhtml|html (fb2 — в epub через Calibre; html — только явно)")
    parser.add_argument("--workers", type=int,
                        help="convert --only fb2, dedup, dedup-folders: потоков (по умолчанию workers из конфига); "
                             "unzip-books: потоков (по умолчанию unzip_workers)")
    parser.add_argument("--max-mb", type=int, default=200, help="unpack-fb2zip: предел размера записи, МБ")
    parser.add_argument("--limit", type=int,
                        help="sort-underscore: не больше N книг; reconvert-fb2: не больше N сопоставленных книг")
    parser.add_argument("--to", help="convert: epub|fb2 (по умолчанию epub); merge-folder: папка автора, существующая служебная «_…» "
                                     "(склейка в неё) или путь внутри служебного раздела «_…» («_разобрать/…», «_Журналы/Если»)")
    parser.add_argument("--non-books-to-quarantine", action="store_true",
                        help="merge-folder: только с --to «_разобрать/…»; файлы не книг (оглавления, картинки, служебные) "
                             "уходят в карантин not-book/, а не в папку назначения")
    parser.add_argument("--overwrite", action="store_true", help="convert: перезаписывать существующий результат")
    parser.add_argument("--skip-newer", action="store_true", help="convert: с --overwrite не трогать результат новее исходника")
    parser.add_argument("--allow-no-author", action="store_true",
                        help="convert --only html: конвертировать HTML без автора в <title> (название — весь <title>, автор в epub не пишется)")
    parser.add_argument("--exclude", action="append", default=[], help="convert: пропускать имена с этой подстрокой")
    parser.add_argument("--author", default="", help="convert: автор для всех документов")
    parser.add_argument("--title-mode", choices=("auto", "filename", "keep"), default="auto", help="convert: откуда название")
    parser.add_argument("--no-toc", action="store_true", help="convert: без оглавления")
    parser.add_argument("--toc-depth", type=int, default=3, help="convert: глубина оглавления")
    parser.add_argument("--delete-source", action="store_true",
                        help="convert: удалить исходник после проверки результата (с JSON-отчётом); "
                             "unpack-fb2zip --multi: архив в карантин после полной распаковки")
    parser.add_argument("--multi", action="store_true",
                        help="unpack-fb2zip: и многокнижные .zip (два и более .fb2) — в папку «<архив без .zip>/»")
    parser.add_argument("--ignore-md5", action="store_true",
                        help="unpack-fb2zip --multi: распаковать, даже если md5 архива не совпал (предупреждение в журнале)")
    parser.add_argument("--sample", type=int, default=0,
                        help="reconvert-fb2: N книг в --out для проверки глазами; библиотека не меняется")
    parser.add_argument("--out", help="reconvert-fb2 с --sample: каталог выборки (вне каталога книг); "
                                      "unpack-fb2zip --multi: абсолютный каталог распаковки вне каталога книг и карантина")
    parser.add_argument("--epubcheck", action="store_true",
                        help="reconvert-fb2: проверять каждый новый epub утилитой epubcheck (медленно, выборочно)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="ничего не менять, показать план")
    parser.add_argument("--reparse", action="store_true",
                        help="scan: перечитать метаданные всех книг (не только новых и изменённых)")
    parser.add_argument("--migrate-db", action="store_true", help="только миграции схемы БД")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="путь к books_library.json")
    args = parser.parse_args(argv)
    setup_logging()
    if args.limit is not None and args.command not in ("sort-underscore", "reconvert-fb2"):
        parser.error("--limit применяется только к sort-underscore и reconvert-fb2")
    if args.reparse and args.command != "scan":
        parser.error("--reparse применяется только к scan")
    if (args.multi or args.ignore_md5) and args.command != "unpack-fb2zip":
        parser.error("--multi и --ignore-md5 применяются только к unpack-fb2zip")
    if args.reasons and args.command != "purge-quarantine":
        parser.error("--reason применяется только к purge-quarantine")
    if args.list_path is not None and args.command != "move-books":
        parser.error("--list применяется только к move-books")
    if args.command == "move-books" and args.list_path is None:
        parser.error("move-books требует --list ФАЙЛ")
    try:
        cfg = load_config(args.config)
        if args.migrate_db or args.command != "serve":
            command = "migrate" if args.migrate_db else args.command
            raw: dict[str, Any] = {}
            if command in FILE_CHANGING:
                raw = {"path": args.path, "only": args.only}
            if command == "unpack-fb2zip":
                raw = {"path": args.path, "max_mb": args.max_mb, "multi": args.multi, "out": args.out,
                       "delete_source": args.delete_source, "ignore_md5": args.ignore_md5}
            elif command == "reconvert-fb2":
                raw = {"path": args.path, "workers": args.workers, "limit": args.limit, "sample": args.sample,
                       "out": args.out, "epubcheck": args.epubcheck}
            elif command in ("dedup", "dedup-folders", "unzip-books"):
                raw = {"path": args.path, "workers": args.workers}
            elif command == "unpack-collections":
                raw = {"path": args.path}
            elif command == "sort-underscore":
                raw = {"limit": args.limit}
            elif command in ("author-variants", "misplaced", "check-folders", "fix-misplaced",
                             "export-decisions", "import-decisions"):
                raw = {}
            elif command == "merge-folder":
                raw = {"from": args.from_folders, "to": args.to, "non_books_to_quarantine": args.non_books_to_quarantine}
            elif command in ("strip-suffix", "name-variants", "windows-names", "enrich-epub"):
                raw = {"path": args.path}
            elif command == "purge-quarantine":
                raw = {"reasons": args.reasons}
            elif command == "move-books":
                raw = {"list_path": args.list_path}
            elif command == "scan":
                raw = {"reparse": args.reparse}
            elif command == "convert" and args.only == "fb2":
                raw.update(workers=args.workers)
            elif command == "convert":
                raw.update(to=args.to or "epub", overwrite=args.overwrite, skip_newer=args.skip_newer, exclude=args.exclude,
                           author=args.author, title_mode=args.title_mode, toc=not args.no_toc, toc_depth=args.toc_depth,
                           delete_source=args.delete_source, allow_no_author=args.allow_no_author)
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
