import importlib.util
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import ExitStack, suppress
from datetime import datetime

import cloudscraper
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]
LOG_NAME = f"{SCRIPT_NAME}_engine"
CONFIG_PATH = os.path.join(BASE_DIR, f"{SCRIPT_NAME}.json")
DB_PATH = os.path.join(BASE_DIR, f"{SCRIPT_NAME}.db")

def get_log_dir(config=None):
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

DB_QUOTE_TRANSLATION = str.maketrans(
    {
        '"': "",
        "`": "",
        "'": "",
        "‘": "",
        "’": "",
        "‚": "",
        "‛": "",
        "«": "",
        "»": "",
        "“": "",
        "”": "",
        "„": "",
        "‟": "",
        "‹": "",
        "›": "",
        "〝": "",
        "〞": "",
        "〟": "",
        "＂": "",
    }
)


def normalize_db_text(value):
    if not value:
        return ""
    normalized = str(value).translate(DB_QUOTE_TRANSLATION)
    return " ".join(normalized.split()).casefold()


class DatabaseManager:
    def __init__(self, path):
        self.path = path
        self._create_table()

    def _create_table(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER)"
            )
            conn.execute("DROP TABLE IF EXISTS allsongs")

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
            conn.execute("DROP INDEX IF EXISTS idx_downloads_artist_title")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_artist_title "
                "ON downloads(artist, title)"
            )

    def is_downloaded(self, track_id):
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute("SELECT 1 FROM downloads WHERE id = ?", (track_id,))
            return cur.fetchone() is not None

    def is_downloaded_by_artist_title(self, artist, title):
        """Проверка существования записи по artist и title без учёта регистра."""
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "SELECT 1 FROM downloads WHERE artist = ? AND title = ?",
                track_identity(artist, title),
            )
            return cur.fetchone() is not None

    def add_record(self, track_id, artist, title, site, src=None, filename=None, filesize=None):
        timestamp = datetime.now().isoformat()
        artist_normalized = normalize_db_text(artist)
        title_normalized = normalize_db_text(title)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO downloads (id, artist, title, site, timestamp, src, filename, filesize) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (track_id, artist_normalized, title_normalized, site, timestamp, src, filename, filesize),
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


def safe_filename(s: str) -> str:
    """Удаляет опасные символы из имени файла, сохраняя Unicode буквы и цифры."""
    if not s:
        return ""
    

    # Удаляем опасные для файловой системы символы
    dangerous = r'[\\/:*?"<>|\x00-\x1f]'
    cleaned = re.sub(dangerous, '', s)
    
    # Удаляем любые вариации кавычек и скобок
    # Включаем все известные типы кавычек на всех языках
    # Это более надежный способ чем перечисление всех вариантов
    # Удаляем символы которые НЕ являются буквами/цифрами/пробелами/дефисами/точками/амперсандом
    # но сохраняем Unicode буквы
    cleaned = re.sub(r"[^\w\s\-\.&]", '', cleaned, flags=re.UNICODE)
    cleaned = re.sub(r'[\(\)\[\]\{\}]', '', cleaned)  # удаляем скобки для верности
    
    return cleaned.strip()


def capitalize_first(value):
    text = (value or "").strip()
    if not text:
        return ""
    return text[:1].upper() + text[1:]


def track_identity(artist, title):
    """Регистронезависимый ключ трека для БД и очередей."""
    return normalize_db_text(artist), normalize_db_text(title)


class DownloadLimitExceeded(OSError):
    """Трек не помещается хотя бы в одну из общих папок."""


def save_response(response, download_paths, filename, expected_size=0):
    """Сохраняет поток во все каталоги, не превышая доступный для трека объём."""
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
    files = []
    temp_paths = []
    final_paths = []
    downloaded_bytes = 0
    try:
        with ExitStack() as stack:
            for folder in download_paths:
                final_path = os.path.join(folder["path"], filename)
                temp_path = f"{final_path}.part"
                files.append(stack.enter_context(open(temp_path, "wb")))
                temp_paths.append(temp_path)
                final_paths.append(final_path)

            for chunk in response.iter_content(chunk_size=131072):
                if not chunk:
                    continue
                if downloaded_bytes + len(chunk) > available:
                    raise DownloadLimitExceeded(
                        f"Лимит папки {limiting_path}: трек превышает доступные {available} байт"
                    )
                downloaded_bytes += len(chunk)
                for file_obj in files:
                    file_obj.write(chunk)

        if expected_size and downloaded_bytes != expected_size:
            raise OSError(f"получено {downloaded_bytes} из {expected_size} байт")

        for temp_path, final_path in zip(temp_paths, final_paths, strict=True):
            os.replace(temp_path, final_path)
        return downloaded_bytes, final_paths
    except Exception:
        for temp_path in temp_paths:
            with suppress(OSError):
                os.remove(temp_path)
        raise


def run():
    if not os.path.exists(CONFIG_PATH):
        return

    setup_logging()
    logger.info("Загружаю конфигурацию...")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    logger.info("Конфигурация загружена")

    perm_cfg = config.get("file_permissions")

    download_paths = config.get("download_paths")
    if not isinstance(download_paths, list) or not download_paths:
        logger.error("В глобальной настройке download_paths нужен непустой список папок")
        raise SystemExit(2)
    if check_paths_and_limits(download_paths, "system"):
        raise SystemExit(2)

    logger.info("Инициализирую scraper...")
    db = DatabaseManager(DB_PATH)
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    scraper.mount("https://", SSLAdapter())
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

        spec = importlib.util.spec_from_file_location(
            "mod", os.path.join(BASE_DIR, site_cfg["site_script"])
        )
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

        # Сохраняем данные сайта для последующей обработки
        sites_data.append({
            "site_cfg": site_cfg,
            "provider": provider,
            "categories": site_cfg["categories"],
            "current_queue": [],
            "processed_count": 0
        })

    # Обработка по кругу: сначала разделы 0 всех сайтов, потом разделы 1 и т. д.
    max_categories = max(len(s["categories"]) for s in sites_data) if sites_data else 0
    
    for category_level in range(max_categories):
        for site_data in sites_data:
            if category_level >= len(site_data["categories"]):
                continue  # Этот сайт не имеет столько разделов
            
            s_cfg = site_data["site_cfg"]
            s_name = s_cfg["site_name"]
            cat = site_data["categories"][category_level]

            logger.info(f"Подключаюсь к {cat}...", extra={"site": s_name})
            # Вся специфика сайта скрыта в драйвере; сбой одного источника не
            # должен мешать обработке остальных.
            try:
                tracks = site_data["provider"].get_tracks(scraper, s_cfg["base_url"], cat)
            except Exception:
                logger.exception("Ошибка получения списка треков", extra={"site": s_name})
                continue
            logger.info(f"Получено ответов: {len(tracks) if isinstance(tracks, list) else 'ошибка'}", extra={"site": s_name})
            
            if isinstance(tracks, list):
                logger.info(f"Обрабатываю {len(tracks)} треков...", extra={"site": s_name})
                new_tracks_count = 0
                for t in tracks:
                    track_key = f"{s_name}_{t['id']}"
                    if db.is_downloaded(track_key):
                        continue
                    # Проверка на существование только по artist/title (в нижнем регистре)
                    artist = capitalize_first(t.get("artist")) or "Unknown Artist"
                    title = capitalize_first(t.get("title")) or "Unknown Title"
                    t["artist"] = artist
                    t["title"] = title
                    identity = track_identity(artist, title)
                    if identity not in queued_tracks and not db.is_downloaded_by_artist_title(artist, title):
                        t["_site_cfg"] = s_cfg
                        site_data["current_queue"].append(t)
                        queued_tracks.add(identity)
                        new_tracks_count += 1
                logger.info(f"В очередь добавлено {new_tracks_count} новых треков", extra={"site": s_name})

        # Скачивание по кругу: по одной песне от каждого сайта
        while any(site_data["current_queue"] for site_data in sites_data):
            for site_data in sites_data:
                if not site_data["current_queue"]:
                    continue
                
                t = site_data["current_queue"].pop(0)
                s_cfg = t["_site_cfg"]
                s_name = s_cfg["site_name"]
                track_key = f"{s_name}_{t['id']}"

                if db.is_downloaded(track_key) or db.is_downloaded_by_artist_title(t["artist"], t["title"]):
                    continue

                try:
                    artist = capitalize_first(t.get("artist")) or "Unknown Artist"
                    title = capitalize_first(t.get("title")) or "Unknown Title"
                    logger.info(f"Загружаю: {artist} - {title}", extra={"site": s_name})
                    headers = {"Referer": t.get("referer", s_cfg["base_url"])}
                    with scraper.get(
                        t["download_url"], timeout=60, headers=headers, stream=True
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
                            downloaded_bytes, saved_paths = save_response(
                                r, download_paths, clean_fn, content_length
                            )

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
                            )
                            site_data["processed_count"] += 1
                            logger.info(f"Успешно скачан: {clean_fn} ({downloaded_bytes} байт)", extra={"site": s_name})
                            time.sleep(1.2)
                        else:
                            logger.error(f"Ошибка соединения (код: {r.status_code})", extra={"site": s_name})
                except DownloadLimitExceeded as e:
                    logger.error(f"Загрузка остановлена: {e}", extra={"site": s_name})
                    raise SystemExit(2) from e
                except Exception as e:
                    logger.error(
                        f"Ошибка скачивания {t.get('title')}: {e}", extra={"site": s_name}
                    )

    # Итоговая статистика
    stats_parts = []
    total_downloaded = 0
    for site_data in sites_data:
        s_name = site_data["site_cfg"]["site_name"]
        count = site_data["processed_count"]
        total_downloaded += count
        stats_parts.append(f"{s_name}={count}")
    
    stats_msg = f"bari: {', '.join(stats_parts)}, total={total_downloaded}"
    logger.info(stats_msg, extra={"site": "system"})


if __name__ == "__main__":
    run()