import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from contextlib import ExitStack, closing, suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import cloudscraper
from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TextFrame, UrlFrame
from requests.adapters import HTTPAdapter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DRIVER_SETTINGS_PATH = os.path.join(BASE_DIR, "drivers", "music_downloader_driver_settings.py")


def load_driver_settings_module():
    """Загружает `apply_driver_settings` по пути: каталог загрузчика не входит в `sys.path`."""
    spec = importlib.util.spec_from_file_location("music_downloader_driver_settings", DRIVER_SETTINGS_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {DRIVER_SETTINGS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def driver_settings_key(script_path):
    """Ключ блока `drivers` для драйвера: имя файла без `.py`."""
    return os.path.splitext(os.path.basename(script_path))[0]
SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]
LOG_NAME = f"{SCRIPT_NAME}_engine"
CONFIG_PATH = os.path.join(BASE_DIR, f"{SCRIPT_NAME}.json")
DB_PATH = os.path.join(BASE_DIR, f"{SCRIPT_NAME}.db")

def get_log_dir(config=None):
    # Переопределение каталога логов (используется тестами, чтобы не засорять logs/)
    env_dir = os.environ.get("MYTASKS_LOG_DIR")
    if env_dir:
        return env_dir
    if config and "log_dir" in config:
        path = config["log_dir"]
        if not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        return path
    return os.path.join(os.path.dirname(BASE_DIR), "logs")

# Инициализируем временно, потом обновим после загрузки конфига
TIMESTAMP = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
LOGS_DIR = get_log_dir()
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOGS_DIR, f"{LOG_NAME}-{TIMESTAMP}.jsonl")

def normalize_db_text(value):
    """Ключ дедупликации: NFKC-нормализация, casefold и только буквы и цифры.

    Любые разделители, пробелы и пунктуация отбрасываются, поэтому «Вася & Петя»,
    «Вася +Петя» и «ВасяПетя» дают один ключ. Слова при этом не удаляются —
    «Song (Remix)» и «Song» остаются разными.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(char for char in text if char.isalnum())

# --- Схема БД и версионированные миграции -------------------------------------------
# Версия схемы — PRAGMA user_version. Схема меняется ТОЛЬКО миграцией: новая миграция —
# следующий номер в DB_MIGRATIONS (DB_LATEST считается по нему); DDL таблиц и индексов
# хранится здесь, в одном месте, и им пользуются и создание, и пересборка.
DB_TIMEOUT_SECONDS = 30

DB_SCHEMA = {
    "downloads": {
        "columns": (
            "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, "
            "original_artist TEXT, original_title TEXT, site TEXT, "
            "timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER, filehash TEXT)"
        ),
        "indexes": {
            "idx_downloads_artist_title": (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_artist_title "
                "ON downloads(artist, title)"
            ),
            # Частичный UNIQUE: одна строка на содержимое; строки без хеша (NULL) —
            # в том числе намеренно записанные дубликаты — не ограничиваются.
            "idx_downloads_filehash": (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_filehash "
                "ON downloads(filehash) WHERE filehash IS NOT NULL"
            ),
        },
    },
    # Итоги запусков: метрики по мостам, источникам поиска и папкам назначения (JSON).
    "runs": {
        "columns": (
            "(started TEXT PRIMARY KEY, finished TEXT, seconds REAL, exit_code INTEGER, "
            "downloaded INTEGER, bytes INTEGER, sites TEXT, sources TEXT, destinations TEXT)"
        ),
        "indexes": {},
    },
}


class MigrationError(RuntimeError):
    """Миграция схемы БД не выполнена; база осталась в прежней версии."""


def table_ddl(table, name=None):
    """DDL таблицы из схемы кода; `name` — временное имя для пересборки (`T__new`)."""
    return f"CREATE TABLE IF NOT EXISTS {name or table} {DB_SCHEMA[table]['columns']}"


def schema_columns(table):
    """Колонки таблицы в каноническом порядке (из DDL кода, в памяти)."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(table_ddl(table))
        return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _table_columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _user_tables(conn):
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _rebuild_table(conn, table, out):
    """Миграция 1 для одной таблицы: пересборка в схему кода (порядок колонок, недостающие — NULL)."""
    if not _table_exists(conn, table):
        conn.execute(table_ddl(table))
        for ddl in DB_SCHEMA[table]["indexes"].values():
            conn.execute(ddl)
        return
    old_columns = _table_columns(conn, table)
    new_columns = schema_columns(table)
    common = [column for column in new_columns if column in old_columns]
    dropped = [column for column in old_columns if column not in new_columns]
    existing_indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,)
        )
    }
    new_name = f"{table}__new"
    conn.execute(f"DROP TABLE IF EXISTS {new_name}")
    conn.execute(table_ddl(table, new_name))
    column_list = ", ".join(f'"{column}"' for column in common)
    conn.execute(f'INSERT INTO {new_name} ({column_list}) SELECT {column_list} FROM "{table}"')
    old_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    new_count = conn.execute(f"SELECT COUNT(*) FROM {new_name}").fetchone()[0]
    if old_count != new_count:
        raise MigrationError(
            f"таблица {table}: число строк после копирования ({new_count}) не равно исходному ({old_count})"
        )
    if dropped:
        out(f"Миграция 1: таблица {table}: отбрасываются колонки {', '.join(dropped)}")
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE {new_name} RENAME TO "{table}"')
    # Уникальные индексы, которых в старой таблице не было, не создаём: данные могли быть
    # ещё не дедуплицированы. Недостающие индексы создаёт обычная последовательность
    # `_create_table` после перенормализации и дедупа. (AUTOINCREMENT в этих таблицах нет,
    # поэтому sqlite_sequence не затрагивается.)
    for index_name, ddl in DB_SCHEMA[table]["indexes"].items():
        if index_name in existing_indexes:
            conn.execute(ddl)
    out(f"Миграция 1: таблица {table}: {new_count} строк, колонки {', '.join(new_columns)}")


def _migration_1(conn, out):
    for table in DB_SCHEMA:
        _rebuild_table(conn, table, out)


DB_MIGRATIONS = {1: _migration_1}
DB_LATEST = max(DB_MIGRATIONS)


def _backup_path(path, version):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{path}.bak-v{version}-{stamp}"


def _plan_lines(conn, path, version):
    """Строки плана миграции (только чтение)."""
    lines = [f"миграция БД v{version} → v{DB_LATEST}: {path}"]
    lines.append(f"резервная копия: {_backup_path(path, version)}")
    for table in DB_SCHEMA:
        if not _table_exists(conn, table):
            lines.append(f"таблица {table}: нет, будет создана")
            continue
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        old_columns = _table_columns(conn, table)
        new_columns = schema_columns(table)
        dropped = [column for column in old_columns if column not in new_columns]
        lines.append(
            f"таблица {table}: {count} строк; колонки сейчас: {', '.join(old_columns)}; "
            f"после: {', '.join(new_columns)}; отбрасываются: {', '.join(dropped) or 'нет'}"
        )
    return lines


def migrate_database(path, dry_run=False, out=None):
    """Приводит БД к DB_LATEST. dry_run только печатает план (через `out`, строки `[dry-run] будет: …`)."""
    out = out or logger.info
    path = os.fspath(path)
    if dry_run:
        if not os.path.exists(path):
            out(f"[dry-run] будет: создана новая база v{DB_LATEST}: {path}")
            return
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=DB_TIMEOUT_SECONDS)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if not _user_tables(conn):
                out(f"[dry-run] будет: создана новая база v{DB_LATEST}: {path}")
            elif version >= DB_LATEST:
                out(f"[dry-run] схема БД уже v{version} (последняя v{DB_LATEST}), изменений нет")
            else:
                for line in _plan_lines(conn, path, version):
                    out(f"[dry-run] будет: {line}")
        return

    conn = sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not _user_tables(conn):
            # Новая база: сразу текущая схема, миграции не нужны.
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table, spec in DB_SCHEMA.items():
                    conn.execute(table_ddl(table))
                    for ddl in spec["indexes"].values():
                        conn.execute(ddl)
                conn.execute(f"PRAGMA user_version = {DB_LATEST}")
                conn.execute("COMMIT")
            except sqlite3.Error as error:
                conn.execute("ROLLBACK")
                raise MigrationError(f"не удалось создать новую базу: {error}") from error
            out(f"Создана новая база v{DB_LATEST}: {path}")
            return
        if version >= DB_LATEST:
            return
        backup = _backup_path(path, version)
        try:
            with closing(sqlite3.connect(backup)) as target:
                conn.backup(target)
        except sqlite3.Error as error:
            raise MigrationError(f"не удалось сделать резервную копию {backup}: {error}") from error
        out(f"Резервная копия БД перед миграцией: {backup}")
        for number in range(version + 1, DB_LATEST + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                DB_MIGRATIONS[number](conn, out)
                conn.execute(f"PRAGMA user_version = {number}")
                conn.execute("COMMIT")
            except (sqlite3.Error, MigrationError) as error:
                conn.execute("ROLLBACK")
                raise MigrationError(
                    f"миграция {number} не выполнена, откат (база осталась v{number - 1}): {error}"
                ) from error
            out(f"Схема БД обновлена до v{number}")
    except sqlite3.Error as error:
        raise MigrationError(f"ошибка SQLite: {error}") from error
    finally:
        conn.close()


class DatabaseManager:
    def __init__(self, path):
        self.path = path
        self._create_table()

    def _create_table(self):
        # Версионированные миграции схемы — до перенормализации и дедупа ниже.
        migrate_database(self.path)
        with sqlite3.connect(self.path, timeout=DB_TIMEOUT_SECONDS) as conn:
            conn.execute(table_ddl("downloads"))

            # В старых строках точное исходное написание уже могло быть потеряно,
            # поэтому сохраняем лучшее доступное значение до очередной нормализации.
            conn.execute(
                "UPDATE downloads SET "
                "original_artist = COALESCE(original_artist, artist), "
                "original_title = COALESCE(original_title, title)"
            )

            # Индекс снимаем до перенормализации: при более широком ключе разные
            # прежде строки могут схлопнуться в один `artist/title`, и UPDATE
            # нарушил бы ещё живой UNIQUE до того, как отработает дедуп ниже.
            conn.execute("DROP INDEX IF EXISTS idx_downloads_artist_title")

            rows = conn.execute("SELECT id, artist, title FROM downloads").fetchall()
            conn.executemany(
                "UPDATE downloads SET artist = ?, title = ? WHERE id = ?",
                [
                    (normalize_db_text(artist), normalize_db_text(title), track_id)
                    for track_id, artist, title in rows
                ],
            )
            conn.execute(
                "DELETE FROM downloads WHERE id IN ("
                "SELECT id FROM ("
                "SELECT id, ROW_NUMBER() OVER ("
                "PARTITION BY artist, title ORDER BY timestamp DESC, id DESC"
                ") AS duplicate_number FROM downloads"
                ") WHERE duplicate_number > 1)"
            )
            # Одинаковый filehash — тот же файл под другим именем: оставляем самую
            # свежую строку, остальные с этим хешем удаляем (NULL не трогаем).
            conn.execute("DROP INDEX IF EXISTS idx_downloads_filehash")
            conn.execute(
                "DELETE FROM downloads WHERE id IN ("
                "SELECT id FROM ("
                "SELECT id, ROW_NUMBER() OVER ("
                "PARTITION BY filehash ORDER BY timestamp DESC, id DESC"
                ") AS duplicate_number FROM downloads WHERE filehash IS NOT NULL"
                ") WHERE duplicate_number > 1)"
            )
            for ddl in DB_SCHEMA["downloads"]["indexes"].values():
                conn.execute(ddl)

        # Итоги запусков: метрики по мостам, источникам поиска и папкам назначения (JSON).
        with sqlite3.connect(self.path, timeout=DB_TIMEOUT_SECONDS) as conn:
            conn.execute(table_ddl("runs"))

    def add_run(self, started, finished, seconds, exit_code, downloaded, total_bytes, sites, sources, destinations):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (started, finished, seconds, exit_code, downloaded, bytes, "
                "sites, sources, destinations) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    started, finished, round(seconds, 1), exit_code, downloaded, total_bytes,
                    json.dumps(sites, ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
                    json.dumps(destinations, ensure_ascii=False),
                ),
            )

    def is_downloaded(self, track_id):
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute("SELECT 1 FROM downloads WHERE id = ?", (track_id,))
            return cur.fetchone() is not None

    def is_downloaded_by_hash(self, filehash):
        """Проверка по SHA-256 содержимого: тот же файл мог прийти под другим именем."""
        if not filehash:
            return False
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "SELECT 1 FROM downloads WHERE filehash = ? LIMIT 1", (filehash,)
            )
            return cur.fetchone() is not None

    def is_downloaded_by_artist_title(self, artist, title):
        """Проверка существования записи по artist и title без учёта регистра."""
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "SELECT 1 FROM downloads WHERE artist = ? AND title = ?",
                track_identity(artist, title),
            )
            return cur.fetchone() is not None

    def add_record(
        self,
        track_id,
        artist,
        title,
        site,
        src=None,
        filename=None,
        filesize=None,
        filehash=None,
        original_artist=None,
        original_title=None,
    ):
        timestamp = datetime.now().isoformat()
        artist_normalized = normalize_db_text(artist)
        title_normalized = normalize_db_text(title)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO downloads "
                "(id, artist, title, original_artist, original_title, site, "
                "timestamp, src, filename, filesize, filehash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    track_id,
                    artist_normalized,
                    title_normalized,
                    artist if original_artist is None else original_artist,
                    title if original_title is None else original_title,
                    site,
                    timestamp,
                    src,
                    filename,
                    filesize,
                    filehash,
                ),
            )



def load_log_template():
    template_path = os.path.join(os.path.dirname(BASE_DIR), "conf", "log_template.conf")
    if not os.path.exists(template_path):
        template_path = os.path.join(os.path.dirname(BASE_DIR), "conf", "log_template.conf.example")
    
    config = {
        "LOG_SCHEMA_VERSION": "1.0",
        "LOG_COMPAT_TARGETS": "elk,opensearch,loki,graylog,splunk",
        "LOG_FIELD_TS": "@timestamp",
        "LOG_FIELD_LEVEL": "log.level",
        "LOG_FIELD_MESSAGE": "message",
        "LOG_FIELD_EVENT_ACTION": "event.action",
        "LOG_FIELD_SERVICE_NAME": "service.name",
        "LOG_FIELD_SCHEMA_VERSION": "schema.version",
    }
    
    if os.path.exists(template_path):
        with suppress(Exception), open(template_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    config[key.strip()] = val.strip().strip('"').strip("'")
    return config

LOG_TEMPLATE = load_log_template()

class JSONLHandler(logging.FileHandler):
    def emit(self, record):
        site_name = getattr(record, "site", "system")
        # Формат логов, совместимый с универсальным шаблоном ELK/OpenSearch/Loki (как в conf/log_template.conf.example)
        entry = {
            LOG_TEMPLATE["LOG_FIELD_TS"]: datetime.now().isoformat(),
            LOG_TEMPLATE["LOG_FIELD_LEVEL"]: record.levelname,
            LOG_TEMPLATE["LOG_FIELD_MESSAGE"]: record.getMessage(),
            LOG_TEMPLATE["LOG_FIELD_SERVICE_NAME"]: site_name,
            LOG_TEMPLATE["LOG_FIELD_SCHEMA_VERSION"]: LOG_TEMPLATE["LOG_SCHEMA_VERSION"],
        }
        try:
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.flush()
        except Exception:
            self.handleError(record)


logger = logging.getLogger("Engine")


def setup_logging():
    handler = JSONLHandler(LOG_PATH, encoding="utf-8")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def apply_file_permissions(file_path, perm_cfg):
    """
    USER-MODE: никаких chown/владельцев/групп. Только chmod (опционально).
    Если нет прав на chmod (например, файл не ваш) — просто логируем и идём дальше.
    """
    if not perm_cfg or not perm_cfg.get("enabled"):
        return
    try:
        mode = perm_cfg.get("mode")
        if mode:
            os.chmod(file_path, int(mode, 8))
    except Exception as e:
        logger.error(f"Ошибка chmod для {file_path}: {e}")


def get_dir_size_bytes(path):
    def on_error(error):
        raise error

    if not os.path.isdir(path):
        raise NotADirectoryError(f"Целевой каталог недоступен: {path}")
    total_size = 0
    for dirpath, _, filenames in os.walk(path, onerror=on_error):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total_size += os.path.getsize(fp)
    return total_size


def check_paths_and_limits(download_paths, s_name):
    """Проверка существования папок и лимитов."""
    for folder in download_paths:
        path = folder["path"]
        if not os.path.isdir(path):
            logger.error(
                f"КРИТИЧЕСКАЯ ОШИБКА: Целевой каталог {path} не найден.",
                extra={"site": s_name},
            )
            return True
        limit_mb = folder.get("max_size_mb", 1024)
        current_size = get_dir_size_bytes(path)
        logger.info(
            f"Статус {path}: {current_size / 1048576:.2f}/{limit_mb} МиБ", extra={"site": s_name}
        )
        if current_size >= limit_mb * 1048576:
            logger.error(
                f"КРИТИЧЕСКАЯ ОШИБКА: Превышен лимит в {path}.", extra={"site": s_name}
            )
            return True
    return False


def cleanup_old_logs(cleanup_days=None, max_files=None):
    """Очистка jsonl логов в папке logs/ по возрасту и/или количеству."""
    log_files = []
    if not os.path.exists(LOGS_DIR):
        return

    for f in os.listdir(LOGS_DIR):
        if not f.startswith(f"{LOG_NAME}-") or not f.endswith(".jsonl"):
            continue
        fp = os.path.join(LOGS_DIR, f)
        with suppress(Exception):
            st = os.stat(fp)
            log_files.append((fp, st.st_mtime))

    if cleanup_days is not None:
        threshold = time.time() - (cleanup_days * 86400)
        for fp, mtime in log_files:
            if mtime >= threshold:
                continue
            with suppress(Exception):
                os.remove(fp)

        refreshed_files = []
        for fp, _ in log_files:
            if not os.path.exists(fp):
                continue
            with suppress(Exception):
                refreshed_files.append((fp, os.stat(fp).st_mtime))
        log_files = refreshed_files

    if isinstance(max_files, int) and max_files >= 0 and len(log_files) > max_files:
        log_files.sort(key=lambda item: item[1], reverse=True)
        for fp, _ in log_files[max_files:]:
            with suppress(Exception):
                os.remove(fp)


MAX_FILENAME_BYTES = 200
# Папки синхронизируются Syncthing с Windows: эти имена там зарезервированы (с любым расширением).
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def safe_filename(s: str) -> str:
    """Удаляет опасные символы из имени файла, сохраняя Unicode буквы и цифры.

    Имя не длиннее MAX_FILENAME_BYTES байт UTF-8 (расширение сохраняется), основа
    без хвостовых точек и пробелов, зарезервированные имена Windows получают префикс «_».
    """
    if not s:
        return ""

    # Удаляем опасные для файловой системы символы
    dangerous = r'[\\/:*?"<>|\x00-\x1f]'
    cleaned = re.sub(dangerous, '', s)

    # Оставляем только Unicode буквы/цифры/пробелы/дефисы/точки/амперсанд; скобки и кавычки
    # любых языков удаляются.
    cleaned = re.sub(r"[^\w\s\-\.&]", '', cleaned, flags=re.UNICODE)
    cleaned = re.sub(r'[\(\)\[\]\{\}]', '', cleaned)
    cleaned = cleaned.strip()

    stem, ext = os.path.splitext(cleaned)
    if len(ext.encode("utf-8")) > 16 or re.search(r"\s", ext):  # «Mr. Smith» — точка в названии, не расширение
        stem, ext = cleaned, ""
    budget = MAX_FILENAME_BYTES - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
    stem = stem.rstrip(". ")
    if stem.split(".", 1)[0].rstrip().upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}{ext}" if stem else ""


def require_http_url(url):
    """Адрес для скачивания: только http/https (без file://, ftp:// и т.п.); иначе ValueError."""
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"Недопустимая схема адреса скачивания: {url!r}")
    return url


def resolve_driver_path(path):
    """Путь драйвера после realpath: только `*.py` внутри BASE_DIR/drivers; иначе ValueError."""
    drivers_dir = os.path.realpath(os.path.join(BASE_DIR, "drivers"))
    full_path = os.path.realpath(os.path.join(BASE_DIR, path))
    if not full_path.endswith(".py") or os.path.commonpath([drivers_dir, full_path]) != drivers_dir:
        raise ValueError(f"Драйвер должен быть файлом .py внутри {drivers_dir}: {path}")
    return full_path


def capitalize_first(value):
    text = (value or "").strip()
    if not text:
        return ""
    return text[:1].upper() + text[1:]


def track_identity(artist, title):
    """Регистронезависимый ключ трека для БД и очередей."""
    return normalize_db_text(artist), normalize_db_text(title)


# Технические кадры (кодировщик, длина, носитель, Windows Media, Sound Forge):
# удаляются, а найденные в них ссылки переносятся в комментарий.
TECHNICAL_TAG_FRAMES = frozenset({
    "TLEN", "TSIZ", "TFLT", "TMED", "TDLY", "TDTG", "TDEN", "TOFN", "TSSE", "TENC", "PRIV", "GEOB",
})
# Сведения о владельце файла и радиостанции — не данные песни: целиком в комментарий.
MOVED_TAG_FRAMES = frozenset({"TOWN", "TRSN", "TRSO"})
# Вспомогательные поля релиза: остаются, если без ссылки; значение со ссылкой —
# обычно реклама целиком («Данная аудиозапись скачана с сайта http://…») — в комментарий.
AUXILIARY_TAG_FRAMES = frozenset({"TCOP", "TPUB", "TXXX"})
# Технические TXXX (ffmpeg, iTunes, рипперы, Gracenote, обложка в base64); сравнение
# без учёта регистра. Данные MusicBrainz, ReplayGain, ISRC, LABEL и т. п. остаются.
TECHNICAL_TXXX = frozenset({
    "major_brand", "minor_version", "compatible_brands", "encoder", "encodersettings", "encoding params",
    "encoded by", "encodedby", "encoded-by", "handler_name", "length", "tagging time", "time_reference",
    "umid", "originator_reference", "coverart", "coverartmime", "ripping tool", "rip date", "log", "sourceid",
    "ctdbdiscconfidence", "ctdbtrackconfidence",
})
TECHNICAL_TXXX_PREFIXES = ("lavf", "itun", "gracenote")
# TXXX-комментарии сторонних программ — переносятся в общий комментарий.
COMMENT_TXXX = frozenset({"comment", "description", "notes"})
# Домены верхнего уровня, по которым «голое» имя сайта (sefon.pro) отличается от
# обычного текста. Короткие TLD, похожие на слова (in, to, am, us, co, de, by —
# «Prod.by», «Stand.by»), не берём: такие сайты ловятся по www. и схеме.
_TAG_TLDS = (
    "com|net|org|info|biz|pro|ru|su|kz|ua|uz|kg|tj|az|ge|md|lt|lv|ee|me|fm|tv|cc|io|"
    "xyz|club|online|site|top|mobi|ws|eu|pl|uk|tk|ml|ga|cf|gq|рф|қаз"
)
# Схема допускает и испорченную запись «http:/site.ru». Адрес почты ссылкой не
# считается: за доменом не должно идти «@», перед ним — «@» (possessive `++`
# не даёт отрезать от «www.site.pro@mail.ru» кусок до «@»).
_TAG_URL = (
    r"(?:(?:https?|ftp):/{0,2}|www\.)[^\s<>\"'()\[\]{}@]++(?!@)"
    rf"|(?<![\w@.-])(?:[^\W_](?:[\w-]*[^\W_])?\.)+(?:{_TAG_TLDS})(?![\w@-])(?:/[^\s<>\"'()\[\]{{}}]*)?"
)
_TAG_URL_RE = re.compile(_TAG_URL, re.IGNORECASE)
# Ссылка вместе с рекламной подводкой: «скачано с sefon.pro», «download from x.com».
# Подводка обязана начинаться с глагола: одиночные «by», «on», «с» перед ссылкой
# могут быть частью названия («Stand By www.site.kz» → «Stand By»).
_TAG_PROMO_URL_RE = re.compile(
    r"(?:(?<!\w)(?:скачано|скачан[аоы]|скачать|загружено|слушать|downloaded|download|listen|visit)"
    r"(?:\s+(?:бесплатно|free|с\s+сайта|сайта|на\s+сайте|с|со|на|from|on|at))*\s+)?"
    rf"(?:{_TAG_URL})",
    re.IGNORECASE,
)
# Рекламный хвост, оставшийся после выреза ссылки («Скачать музыку, mp3, песни бесплатно!»).
_TAG_PROMO_TEXT_RE = re.compile(r"(?<!\w)(?:скачать|скачано|бесплатно|download|free\s+mp3)(?!\w)", re.IGNORECASE)
_TAG_BRACKETS_RE = re.compile(r"[(\[{]([^()\[\]{}]*)[)\]}]")
_TAG_EDGE_CHARS = " \t-–—|/\\•·:;,~*_+@"


def strip_tag_urls(text, brackets_only=False):
    """Вырезает ссылки на сайты из значения тега; возвращает ``(текст, вырезанные_фрагменты)``.

    Скобки, внутри которых есть ссылка, удаляются целиком, а ссылка без скобок —
    вместе с рекламной подводкой (кроме режима ``brackets_only``). Оставшиеся по
    краям разделители срезаются.
    """
    removed = []

    def cut_brackets(match):
        if not _TAG_URL_RE.search(match[1]):
            return match[0]
        removed.append(match[1].strip())
        return " "

    def cut_url(match):
        removed.append(match[0].strip().rstrip(".,;:!?"))
        return " "

    cleaned = _TAG_BRACKETS_RE.sub(cut_brackets, text)
    if not brackets_only:
        cleaned = _TAG_PROMO_URL_RE.sub(cut_url, cleaned)
    if not removed:
        return text, []
    cleaned = re.sub(r"\s+", " ", cleaned).strip(_TAG_EDGE_CHARS)
    # «Исполнитель -  - Название» после выреза середины: двойной разделитель → один.
    cleaned = re.sub(r"(\s[-–—|])(?:\s+[-–—|])+(?=\s)", r"\1", cleaned)
    return cleaned, [fragment for fragment in removed if fragment]


def _url_fragments(text):
    return [match[0].rstrip(".,;:!?") for match in _TAG_URL_RE.finditer(text)]


def _compact_url(url):
    """Короткая форма ссылки для комментария: без схемы и www, хост в нижнем регистре."""
    match = re.match(r"(?:(?:https?|ftp)://)?(?:www\.)?([^/\s?#]+)(.*)$", url, re.IGNORECASE)
    if match is None:
        return url
    return match[1].lower().rstrip(".") + match[2].rstrip("/")


# Служебные шестнадцатеричные ряды (iTunNORM, iTunSMPB и подобные) — не комментарий.
_COMMENT_NOISE_RE = re.compile(r"(?:\s*[0-9A-Fa-f]{8})+\s*")


def optimize_comment(fragments, song_values=frozenset()):
    """Собирает компактный список частей комментария без повторов.

    Части прежнего комментария делятся по `` | `` и переводам строк. Часть со
    ссылкой сводится к самим ссылкам в короткой форме (``sefon.pro``,
    ``sefon.pro/mp3/1``): одна и та же ссылка в разных написаниях
    (``https://www.Sefon.Pro/``, ``[sefon.pro]``, «скачано с sefon.pro») остаётся
    одна, а голый домен не дублирует ссылку на страницу того же сайта. Пустые
    части, служебные hex-ряды и повторы названия, исполнителя или альбома
    (``song_values`` в casefold) отбрасываются.
    """
    items = []
    seen = set()
    for fragment in fragments:
        for part in re.split(r"\s+\|\s+|[\r\n]+", fragment):
            part = part.strip(_TAG_EDGE_CHARS)
            if not part or _COMMENT_NOISE_RE.fullmatch(part):
                continue
            # Ссылка без точки в хосте — обрезок (ID3v1 хранит 30 символов): «www.RnBXc» → не ссылка.
            urls = [url for url in _url_fragments(part) if "." in _compact_url(url).split("/", 1)[0]]
            if not urls and _TAG_URL_RE.search(part):
                continue
            for item, is_url in ([(_compact_url(url), True) for url in urls] if urls else [(part, False)]):
                key = item.casefold()
                if key in seen or key in song_values:
                    continue
                seen.add(key)
                items.append((item, is_url))
    hosts_with_pages = {item.split("/", 1)[0].casefold() for item, is_url in items if is_url and "/" in item}
    return [item for item, is_url in items if not (is_url and "/" not in item and item.casefold() in hosts_with_pages)]


def _frame_strings(frame):
    """Все строковые значения кадра, в которых может оказаться ссылка."""
    if isinstance(frame, UrlFrame):
        return [frame.url]
    if isinstance(frame, TextFrame):
        return [str(value) for value in frame.text]
    # PRIV — поле владельца, GEOB — описание и имя файла.
    return [value for name in ("owner", "desc", "filename") if isinstance(value := getattr(frame, name, None), str)]


def clean_mp3_tags(path):
    """Оставляет в ID3 только данные о песне, остальное переносит в комментарий.

    Из названия, исполнителя, альбома и других полей песни вырезаются ссылки на
    сайты (вместе с подводкой «скачано с…»); из текста песни — строки со ссылкой;
    из подписи обложки — ссылка. Значения TCOP/TPUB/TXXX со ссылкой, URL-кадры
    W***, сведения о владельце и радиостанции, TXXX-комментарии переносятся в
    комментарий целиком. Технические кадры (кодировщик, ffmpeg, iTunes, Windows
    Media, рипперы) удаляются, ссылки из них — в комментарий. Все комментарии
    сливаются в один и сжимаются (``optimize_comment``). Существующий ID3v1 обновляется из очищенного ID3v2.
    Возвращает ``True``, если файл изменён; файл без ID3 не трогается.
    """
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return False

    comments, comment_frames, extras = [], [], []
    changed = False
    for key, frame in list(tags.items()):
        frame_id = frame.FrameID
        desc = getattr(frame, "desc", "")
        txxx = desc.casefold() if frame_id == "TXXX" else ""
        if frame_id == "COMM":
            # Служебные комментарии iTunes (iTunNORM, iTunSMPB…) — техническая информация.
            if desc.startswith("iTun"):
                changed = True
            else:
                comment_frames.append(frame)
                comments.extend(value for value in frame.text if value.strip())
            del tags[key]
        elif isinstance(frame, UrlFrame):
            del tags[key]
            changed = True
            extras.append(f"{desc}: {frame.url}" if desc else frame.url)
        elif frame_id in TECHNICAL_TAG_FRAMES or txxx in TECHNICAL_TXXX or txxx.startswith(TECHNICAL_TXXX_PREFIXES):
            del tags[key]
            changed = True
            for value in _frame_strings(frame):
                extras.extend(_url_fragments(value))
        elif frame_id in MOVED_TAG_FRAMES or txxx in COMMENT_TXXX:
            del tags[key]
            changed = True
            extras.extend(value.strip() for value in _frame_strings(frame) if value.strip())
        elif frame_id in AUXILIARY_TAG_FRAMES:
            values = _frame_strings(frame)
            kept = [value for value in values if not _TAG_URL_RE.search(value)]
            if len(kept) != len(values):
                changed = True
                extras.extend(
                    f"{desc}: {value.strip()}" if desc else value.strip()
                    for value in values if _TAG_URL_RE.search(value)
                )
                if kept:
                    frame.text = kept
                else:
                    del tags[key]
        elif frame_id == "USLT":
            # Строка текста песни со ссылкой — реклама целиком («Тексты на lyrics.ru»).
            # Переводы строк (\r\n, \n) сохраняются как были — иначе повторная очистка снова меняет файл.
            lines = []
            for line in frame.text.splitlines(keepends=True):
                if _TAG_URL_RE.search(line):
                    extras.append(line.strip())
                else:
                    lines.append(line)
            text = "".join(lines)
            if text != frame.text:
                frame.text, changed = text, True
        elif frame_id == "APIC":
            cleaned, removed = strip_tag_urls(desc)
            if removed:
                frame.desc, changed = cleaned, True
                extras.extend(removed)
        elif isinstance(frame, TextFrame):
            values = []
            for value in frame.text:
                if not isinstance(value, str):
                    values.append(value)
                    continue
                cleaned, removed = strip_tag_urls(value)
                if removed and not cleaned and frame_id in ("TIT2", "TPE1"):
                    # Название и исполнитель не пропадают целиком: «Евродэнс.ru (lmusic.kz)»
                    # → «Евродэнс.ru»; значение, целиком похожее на сайт, остаётся как есть.
                    cleaned, removed = strip_tag_urls(value, brackets_only=True)
                    if not cleaned:
                        cleaned, removed = value, []
                elif removed and frame_id not in ("TIT2", "TPE1") and _TAG_PROMO_TEXT_RE.search(cleaned):
                    # После ссылки осталась реклама — значение целиком в комментарий.
                    cleaned, removed = "", [value.strip()]
                extras.extend(removed)
                if cleaned:
                    values.append(cleaned)
            if values != list(frame.text):
                changed = True
                if values:
                    frame.text = values
                else:
                    del tags[key]
        # Остальное (UFID, MCDI, POPM, PCNT, RVA2, SYLT, TIPL…) — данные трека, без изменений.

    # Один общий оптимизированный комментарий вместо нескольких кадров COMM.
    song_values = {
        str(value).casefold()
        for frame_id in ("TIT2", "TPE1", "TPE2", "TALB")
        if frame_id in tags
        for value in tags[frame_id].text
    }
    text = " | ".join(optimize_comment([*comments, *extras], song_values))
    if [(frame.desc, list(frame.text)) for frame in comment_frames] != ([("", [text])] if text else []):
        changed = True
    if not changed:
        return False
    if text:
        lang = comment_frames[0].lang if comment_frames else "eng"
        tags.add(COMM(encoding=3, lang=lang, desc="", text=[text]))
    version = 3 if tags.version[:2] == (2, 3) else 4
    tags.save(path, v1=1, v2_version=version)
    return True


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DownloadLimitExceeded(OSError):
    """Трек не помещается хотя бы в одну из общих папок."""


class TrackDownloadError(OSError):
    """Ошибка одного трека (дедлайн или лимит размера файла): остальной запуск продолжается."""


DOWNLOAD_MAX_SECONDS = 600
DOWNLOAD_MAX_MB = 100


def save_response(response, download_paths, filename, expected_size=0, max_seconds=None, max_bytes=None):
    """Сохраняет поток во все каталоги, не превышая доступный для трека объём.

    Перед публикацией файла теги ID3 очищаются (``clean_mp3_tags``). Возвращает
    ``(число_байт, список_путей, sha256_hex)`` — размер и хеш относятся к итоговому
    файлу на диске (после очистки тегов); хеш используется для дедупликации.
    ``max_seconds`` — общий дедлайн на трек, ``max_bytes`` — лимит размера одного файла;
    превышение — ``TrackDownloadError`` (``.part`` удаляются).
    """
    if not download_paths:
        raise ValueError("Не заданы папки для сохранения")
    # ponytail: бюджет рассчитан для одного писателя; для параллельных нужны квоты файловой системы.
    paths = [os.path.realpath(folder["path"]) for folder in download_paths]
    if len(set(paths)) != len(paths):
        raise ValueError("Папки для сохранения не должны повторяться")
    budgets = []
    for folder, path in zip(download_paths, paths, strict=True):
        # В лимит родительской папки входят также копии трека в дочерних папках.
        copies = sum(os.path.commonpath([path, destination]) == path for destination in paths)
        remaining = int(folder.get("max_size_mb", 1024) * 1048576) - get_dir_size_bytes(path)
        budgets.append((remaining // copies, folder["path"]))
    available, limiting_path = min(budgets)
    if available <= 0 or expected_size > available:
        raise DownloadLimitExceeded(
            f"Лимит папки {limiting_path}: доступно {max(available, 0)} байт, размер трека {expected_size} байт"
        )
    if max_bytes is not None and expected_size > max_bytes:
        raise TrackDownloadError(f"Размер трека {expected_size} байт больше лимита файла {max_bytes} байт")
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    files = []
    temp_paths = []
    final_paths = []
    downloaded_bytes = 0
    digest = hashlib.sha256()
    try:
        with ExitStack() as stack:
            for folder in download_paths:
                final_path = os.path.join(folder["path"], filename)
                temp_path = f"{final_path}.part"
                # O_NOFOLLOW: симлинк на месте .part — ошибка, а не запись по чужому пути.
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
                files.append(stack.enter_context(os.fdopen(fd, "wb")))
                temp_paths.append(temp_path)
                final_paths.append(final_path)

            for chunk in response.iter_content(chunk_size=131072):
                if deadline is not None and time.monotonic() > deadline:
                    raise TrackDownloadError(f"Превышено время скачивания трека ({max_seconds} с)")
                if not chunk:
                    continue
                if max_bytes is not None and downloaded_bytes + len(chunk) > max_bytes:
                    raise TrackDownloadError(f"Трек превышает лимит файла {max_bytes} байт")
                if downloaded_bytes + len(chunk) > available:
                    raise DownloadLimitExceeded(
                        f"Лимит папки {limiting_path}: трек превышает доступные {available} байт"
                    )
                downloaded_bytes += len(chunk)
                digest.update(chunk)
                for file_obj in files:
                    file_obj.write(chunk)

        if expected_size and downloaded_bytes != expected_size:
            raise OSError(f"получено {downloaded_bytes} из {expected_size} байт")

        # Теги чистятся на отдельной копии: сбой mutagen не должен испортить
        # уже скачанный файл. Очищенный вариант раскладывается во все папки, а
        # размер и хеш пересчитываются по нему — так в БД то, что лежит на диске.
        filehash = digest.hexdigest()
        work_path = f"{temp_paths[0]}.tags"
        try:
            shutil.copyfile(temp_paths[0], work_path)
            try:
                tags_changed = clean_mp3_tags(work_path)
            except Exception as error:
                logger.warning(f"Не удалось очистить теги {filename}, файл сохранён как есть: {error}")
                tags_changed = False
            if tags_changed:
                cleaned_size = os.path.getsize(work_path)
                if cleaned_size > available:
                    raise DownloadLimitExceeded(
                        f"Лимит папки {limiting_path}: трек после очистки тегов превышает доступные {available} байт"
                    )
                for temp_path in temp_paths:
                    shutil.copyfile(work_path, temp_path)
                downloaded_bytes = cleaned_size
                filehash = _file_sha256(work_path)
                logger.info(f"Теги очищены от ссылок и служебных данных: {filename}")
        finally:
            with suppress(OSError):
                os.remove(work_path)

        for temp_path, final_path in zip(temp_paths, final_paths, strict=True):
            os.replace(temp_path, final_path)
        return downloaded_bytes, final_paths, filehash
    except Exception:
        for temp_path in temp_paths:
            with suppress(OSError):
                os.remove(temp_path)
        raise


def _mib(value):
    return f"{value / 1048576:.1f} МиБ"


def _duration(seconds):
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин {sec} с"


def destination_status(download_paths, typical_track_bytes=6 * 1048576):
    """Занято/лимит/свободно по каждой папке назначения и сколько ещё треков влезет."""
    result = []
    for folder in download_paths:
        limit = int(folder.get("max_size_mb", 1024) * 1048576)
        try:
            used = get_dir_size_bytes(folder["path"])
        except OSError:
            used = None
        free = None if used is None else max(0, limit - used)
        result.append({
            "path": folder["path"], "used": used, "limit": limit, "free": free,
            "tracks_left": None if free is None else free // typical_track_bytes,
        })
    return result


def report_run(db, started, clock, exit_code, sites_data, search_stats, download_paths):
    """Итог запуска: строки в лог движка и запись в таблицу runs (метрики для анализа)."""
    seconds = time.monotonic() - clock
    sites = {site["site_cfg"]["site_name"]: dict(site["stats"]) for site in sites_data}
    for name, stat in sites.items():
        stat["seconds"] = round(stat["seconds"], 1)
        logger.info(
            f"Итог {name}: в списках {stat['listed']}, новых в очереди {stat['queued']}, скачано "
            f"{stat['downloaded']} ({_mib(stat['bytes'])}), не найдено {stat['not_found']}, дублей "
            f"{stat['duplicates']}, ошибок {stat['errors']}, время {_duration(stat['seconds'])}",
            extra={"site": name},
        )
    for name, partial in list(search_stats.items()):
        # Драйверы могут вести не все счётчики: недостающие считаются нулём.
        stat = search_stats[name] = {
            key: partial.get(key, 0)
            for key in ("queries", "candidates", "exact", "relaxed", "chosen", "errors", "seconds")
        }
        stat["seconds"] = round(stat["seconds"], 1)
        logger.info(
            f"Итог поиска {name}: запросов {stat['queries']}, кандидатов {stat['candidates']}, точных "
            f"{stat['exact']}, мягких {stat['relaxed']}, выбран {stat['chosen']} раз, ошибок {stat['errors']}, "
            f"время {_duration(stat['seconds'])}",
            extra={"site": "search"},
        )
    destinations = destination_status(download_paths)
    for item in destinations:
        if item["used"] is None:
            logger.warning(f"Место: {item['path']} недоступен", extra={"site": "system"})
            continue
        level = logging.WARNING if item["tracks_left"] < 50 else logging.INFO
        logger.log(
            level,
            f"Место: {item['path']} — занято {_mib(item['used'])} из {_mib(item['limit'])}, свободно "
            f"{_mib(item['free'])} (~{item['tracks_left']} треков)",
            extra={"site": "system"},
        )
    downloaded = sum(stat["downloaded"] for stat in sites.values())
    total_bytes = sum(stat["bytes"] for stat in sites.values())
    # Прежняя строка итога сохраняется для совместимости (описана в README).
    parts = ", ".join(f"{site['site_cfg']['site_name']}={site['processed_count']}" for site in sites_data)
    logger.info(f"bari: {parts}, total={downloaded}", extra={"site": "system"})
    logger.info(
        f"Итог запуска: скачано {downloaded} ({_mib(total_bytes)}) за {_duration(seconds)}, код {exit_code}",
        extra={"site": "system"},
    )
    try:
        db.add_run(started, datetime.now().isoformat(timespec="seconds"), seconds, exit_code, downloaded,
                   total_bytes, sites, search_stats, destinations)
    except sqlite3.Error as error:
        logger.error(f"Не удалось записать итог запуска в БД: {error}", extra={"site": "system"})


DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_PAUSE_SECONDS = 1.2


def number_setting(config, key, default, minimum=0):
    """Число из корня music_downloader.json; нет ключа или значение не число ≥ minimum — default."""
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        logger.warning(f"Настройка {key} должна быть числом не меньше {minimum}, беру {default}")
        return default
    return value


def run():
    if not os.path.exists(CONFIG_PATH):
        return

    setup_logging()
    logger.info("Загружаю конфигурацию...")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    logger.info("Конфигурация загружена")

    download_timeout = number_setting(config, "download_timeout_seconds", DOWNLOAD_TIMEOUT_SECONDS, minimum=1)
    download_pause = number_setting(config, "download_pause_seconds", DOWNLOAD_PAUSE_SECONDS)
    run_started = datetime.now().isoformat(timespec="seconds")
    run_clock = time.monotonic()
    # Метрики источников поиска (find_audio дополняет по имени источника).
    search_stats = {}
    perm_cfg = config.get("file_permissions")
    search_sources = config.get("search_sources")
    if search_sources is not None and not isinstance(search_sources, list):
        logger.error("Настройка search_sources должна быть списком источников")
        raise SystemExit(2)
    driver_settings = config.get("drivers", {})
    if not isinstance(driver_settings, dict):
        logger.error("Настройка drivers должна быть объектом: имя файла драйвера -> настройки")
        raise SystemExit(2)
    apply_driver_settings = load_driver_settings_module().apply_driver_settings
    artist_aliases = config.get("artist_aliases") or []
    if not isinstance(artist_aliases, list) or not all(
        isinstance(group, list) and all(isinstance(name, str) for name in group) for group in artist_aliases
    ):
        logger.error("Настройка artist_aliases должна быть списком групп имён одного исполнителя")
        raise SystemExit(2)

    download_paths = config.get("download_paths")
    if not isinstance(download_paths, list) or not download_paths:
        logger.error("В глобальной настройке download_paths нужен непустой список папок")
        raise SystemExit(2)
    if check_paths_and_limits(download_paths, "system"):
        raise SystemExit(2)

    download_max_seconds = number_setting(config, "download_max_seconds", DOWNLOAD_MAX_SECONDS, minimum=1)
    download_max_bytes = int(number_setting(config, "download_max_mb", DOWNLOAD_MAX_MB, minimum=1) * 1048576)
    logger.info("Инициализирую scraper...")
    try:
        db = DatabaseManager(DB_PATH)
    except MigrationError as error:
        logger.error(f"Миграция БД не выполнена: {error}")
        raise SystemExit(1) from error
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    # TLS — штатный HTTPAdapter requests: проверка сертификата и имени хоста,
    # уровень безопасности OpenSSL по умолчанию (прежний SSLAdapter с SECLEVEL=1 снят
    # 2026-11-12). CipherSuiteAdapter cloudscraper не годится: он разрешает только
    # кривую prime256v1, и CDN Sefon (cdn2/cdn3.sefon.pro) обрывают рукопожатие.
    scraper.mount("https://", HTTPAdapter())
    logger.info("Scraper инициализирован")

    # Очистка старых логов
    cleanup_days = config.get("log_cleanup_days", 10)
    max_log_files = config.get("log_max_files", 10)
    cleanup_old_logs(cleanup_days=cleanup_days, max_files=max_log_files)

    # Инициализация драйверов и очередей для поочерёдной обработки
    sites_data = []
    queued_tracks = set()
    for site_cfg in config.get("sites", []):
        s_name = site_cfg["site_name"]
        logger.info(f"Выбираю драйвер: {s_name}", extra={"site": s_name})

        try:
            driver_path = resolve_driver_path(site_cfg["site_script"])
        except ValueError as error:
            logger.error(f"Драйвер {s_name} не загружен: {error}", extra={"site": s_name})
            continue
        spec = importlib.util.spec_from_file_location("mod", driver_path)
        if spec is None or spec.loader is None:
            logger.error(f"Не удалось загрузить драйвер {s_name}", extra={"site": s_name})
            continue
        provider = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(provider)
        except Exception:
            logger.exception(f"Не удалось загрузить драйвер {s_name}", extra={"site": s_name})
            continue
        logger.info(f"Драйвер {s_name} загружен", extra={"site": s_name})
        apply_driver_settings(
            provider, driver_settings.get(driver_settings_key(site_cfg["site_script"]), {}), logger
        )

        # Сохраняем данные сайта для последующей обработки
        sites_data.append({
            "site_cfg": site_cfg,
            "provider": provider,
            "categories": site_cfg["categories"],
            "current_queue": [],
            "next_category": 0,
            "processed_count": 0,
            # Метрики запуска по мосту: что было в списках, что дошло до очереди и чем кончилось.
            "stats": {
                "listed": 0, "queued": 0, "downloaded": 0, "duplicates": 0, "not_found": 0,
                "errors": 0, "bytes": 0, "seconds": 0.0,
            },
        })

    def fill_queue(site_data):
        """Пока очередь сайта пуста, читает его следующую категорию (по порядку из конфига)."""
        while not site_data["current_queue"] and site_data["next_category"] < len(site_data["categories"]):
            read_category(site_data)

    def read_category(site_data):
        s_cfg = site_data["site_cfg"]
        s_name = s_cfg["site_name"]
        cat = site_data["categories"][site_data["next_category"]]
        site_data["next_category"] += 1

        logger.info(f"Подключаюсь к {cat}...", extra={"site": s_name})
        # Вся специфика сайта скрыта в драйвере; сбой одного источника не
        # должен мешать обработке остальных.
        try:
            tracks = site_data["provider"].get_tracks(scraper, s_cfg["base_url"], cat)
        except Exception:
            logger.exception("Ошибка получения списка треков", extra={"site": s_name})
            return
        logger.info(f"Получено ответов: {len(tracks) if isinstance(tracks, list) else 'ошибка'}", extra={"site": s_name})
        
        if isinstance(tracks, list):
            logger.info(f"Обрабатываю {len(tracks)} треков...", extra={"site": s_name})
            site_data["stats"]["listed"] += len(tracks)
            new_tracks_count = 0
            for t in tracks:
                track_key = f"{s_name}_{t['id']}"
                if db.is_downloaded(track_key):
                    continue
                # Проверка на существование только по artist/title (в нижнем регистре)
                original_artist = (t.get("artist") or "").strip()
                original_title = (t.get("title") or "").strip()
                artist = capitalize_first(original_artist) or "Unknown Artist"
                title = capitalize_first(original_title) or "Unknown Title"
                t["artist"] = artist
                t["title"] = title
                t["_original_artist"] = original_artist or artist
                t["_original_title"] = original_title or title
                identity = track_identity(artist, title)
                if identity not in queued_tracks and not db.is_downloaded_by_artist_title(artist, title):
                    t["_site_cfg"] = s_cfg
                    site_data["current_queue"].append(t)
                    queued_tracks.add(identity)
                    new_tracks_count += 1
            site_data["stats"]["queued"] += new_tracks_count
            logger.info(f"В очередь добавлено {new_tracks_count} новых треков", extra={"site": s_name})

    # Скачивание по кругу: по одной песне от каждого сайта, пока у кого-то есть
    # треки. Опустевшая очередь сайта пополняется его следующей категорией, а
    # остальные сайты не ждут: большая очередь одного сайта (журнал эфира Beu FM —
    # сотни песен) не задерживает вторые категории Sefon/LMusic. Категория читается
    # только когда нужна — ссылки на файлы со страниц бывают с меткой времени.
    exit_code = 0
    try:
        while True:
            active = False
            for site_data in sites_data:
                fill_queue(site_data)
                if not site_data["current_queue"]:
                    continue
                active = True
            
                t = site_data["current_queue"].pop(0)
                s_cfg = t["_site_cfg"]
                s_name = s_cfg["site_name"]
                track_key = f"{s_name}_{t['id']}"

                if db.is_downloaded(track_key) or db.is_downloaded_by_artist_title(t["artist"], t["title"]):
                    continue

                stats = site_data["stats"]
                track_started = time.monotonic()
                try:
                    artist = capitalize_first(t.get("artist")) or "Unknown Artist"
                    title = capitalize_first(t.get("title")) or "Unknown Title"
                    resolver = getattr(site_data["provider"], "resolve_track", None)
                    if resolver is not None:
                        # Общий список источников MP3 для драйверов без собственного аудио.
                        t["_search_sources"] = search_sources
                        t["_artist_aliases"] = artist_aliases
                        t["_driver_settings"] = driver_settings
                        t["_search_stats"] = search_stats
                        resolved = resolver(scraper, t)
                        if resolved is None:
                            queued_tracks.discard(track_identity(artist, title))
                            stats["not_found"] += 1
                            continue
                        t.update(resolved)
                    logger.info(f"Загружаю: {artist} - {title}", extra={"site": s_name})
                    headers = {"Referer": t.get("referer", s_cfg["base_url"])}
                    # Источник без прямой HTTP-ссылки (YouTube Music через yt-dlp) отдаёт
                    # fetch(): готовый файл в виде потокового ответа с теми же полями.
                    fetch = t.get("fetch")
                    with (
                        fetch() if callable(fetch)
                        else scraper.get(require_http_url(t["download_url"]), timeout=download_timeout, headers=headers, stream=True)
                    ) as r:
                        if r.status_code == 200:
                            # Получаем размер файла из заголовка Content-Length
                            raw_cl = r.headers.get('content-length', 0)
                            try:
                                content_length = int(raw_cl or 0)
                            except ValueError:
                                logger.warning(f"Некорректный content-length: {raw_cl}", extra={"site": s_name})
                                content_length = 0
                            clean_fn = safe_filename(
                                f"{artist} - {title}.mp3"
                            )
                            logger.info(f"Соединение установлено (код: {r.status_code})", extra={"site": s_name})

                            logger.info(f"Сохраняю файл: {clean_fn}", extra={"site": s_name})
                            downloaded_bytes, saved_paths, filehash = save_response(
                                r, download_paths, clean_fn, content_length,
                                max_seconds=download_max_seconds, max_bytes=download_max_bytes,
                            )

                            # Тот же файл уже скачан (другое имя/источник): физические
                            # копии удаляем. ID фиксируем, чтобы не качать снова, но с
                            # filehash=NULL — иначе нарушится частичный UNIQUE-индекс.
                            duplicate = db.is_downloaded_by_hash(filehash)
                            if duplicate:
                                for saved_path in saved_paths:
                                    with suppress(OSError):
                                        os.remove(saved_path)
                                stats["duplicates"] += 1
                                logger.info(
                                    f"Дубликат по содержимому, копии удалены: {clean_fn}",
                                    extra={"site": s_name},
                                )
                            else:
                                # Применение прав доступа к файлу без смены владельца
                                for saved_path in saved_paths:
                                    apply_file_permissions(saved_path, perm_cfg)

                            db.add_record(
                                track_key,
                                artist,
                                title,
                                s_name,
                                t.get("src") or t.get("referer") or t.get("download_url"),
                                clean_fn,
                                downloaded_bytes,
                                None if duplicate else filehash,
                                t["_original_artist"],
                                t["_original_title"],
                            )
                            if not duplicate:
                                site_data["processed_count"] += 1
                                stats["downloaded"] += 1
                                stats["bytes"] += downloaded_bytes
                                logger.info(f"Успешно скачан: {clean_fn} ({downloaded_bytes} байт)", extra={"site": s_name})
                                time.sleep(download_pause)
                        else:
                            stats["errors"] += 1
                            logger.error(f"Ошибка соединения (код: {r.status_code})", extra={"site": s_name})
                except DownloadLimitExceeded as e:
                    logger.error(f"Загрузка остановлена: {e}", extra={"site": s_name})
                    raise SystemExit(2) from e
                except Exception as e:
                    stats["errors"] += 1
                    queued_tracks.discard(track_identity(t["artist"], t["title"]))
                    logger.error(
                        f"Ошибка скачивания {t.get('title')}: {e}", extra={"site": s_name}
                    )
                finally:
                    stats["seconds"] += time.monotonic() - track_started
            if not active:
                break
    except SystemExit as stop:
        exit_code = stop.code if isinstance(stop.code, int) else 1
        raise
    finally:
        report_run(db, run_started, run_clock, exit_code, sites_data, search_stats, download_paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Загрузчик музыки с мостов (music_downloader)")
    parser.add_argument("--migrate-db", action="store_true", help="только миграции схемы БД и выход")
    parser.add_argument(
        "--dry-run", "-n", action="store_true", help="с --migrate-db: показать план, ничего не меняя"
    )
    args = parser.parse_args(argv)
    if args.dry_run and not args.migrate_db:
        parser.error("--dry-run поддерживается только вместе с --migrate-db")
    if not args.migrate_db:
        run()
        return 0
    if not args.dry_run:
        setup_logging()

    def out(message):
        print(message)
        if not args.dry_run:
            logger.info(message)

    try:
        migrate_database(DB_PATH, dry_run=args.dry_run, out=out)
    except MigrationError as error:
        message = f"Миграция БД не выполнена: {error}"
        print(message)
        logger.error(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
