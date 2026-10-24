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
from contextlib import ExitStack, suppress
from datetime import datetime

import cloudscraper
from mutagen.id3 import COMM, ID3, ID3NoHeaderError, TextFrame, UrlFrame
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


class DatabaseManager:
    def __init__(self, path):
        self.path = path
        self._create_table()

    def _create_table(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS downloads ("
                "id TEXT PRIMARY KEY, artist TEXT, title TEXT, "
                "original_artist TEXT, original_title TEXT, site TEXT, "
                "timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER, filehash TEXT)"
            )
            conn.execute("DROP TABLE IF EXISTS allsongs")

            columns = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
            for column in ("original_artist", "original_title", "filehash"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE downloads ADD COLUMN {column} TEXT")

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
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_artist_title "
                "ON downloads(artist, title)"
            )
            # Частичный UNIQUE: одна строка на содержимое; строки без хеша (NULL) —
            # в том числе намеренно записанные дубликаты — не ограничиваются.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_filehash "
                "ON downloads(filehash) WHERE filehash IS NOT NULL"
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


def save_response(response, download_paths, filename, expected_size=0):
    """Сохраняет поток во все каталоги, не превышая доступный для трека объём.

    Перед публикацией файла теги ID3 очищаются (``clean_mp3_tags``). Возвращает
    ``(число_байт, список_путей, sha256_hex)`` — размер и хеш относятся к итоговому
    файлу на диске (после очистки тегов); хеш используется для дедупликации.
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


def run():
    if not os.path.exists(CONFIG_PATH):
        return

    setup_logging()
    logger.info("Загружаю конфигурацию...")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    logger.info("Конфигурация загружена")

    perm_cfg = config.get("file_permissions")
    search_sources = config.get("search_sources")
    if search_sources is not None and not isinstance(search_sources, list):
        logger.error("Настройка search_sources должна быть списком источников")
        raise SystemExit(2)

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
                    resolver = getattr(site_data["provider"], "resolve_track", None)
                    if resolver is not None:
                        # Общий список источников MP3 для драйверов без собственного аудио.
                        t["_search_sources"] = search_sources
                        resolved = resolver(scraper, t)
                        if resolved is None:
                            queued_tracks.discard(track_identity(artist, title))
                            continue
                        t.update(resolved)
                    logger.info(f"Загружаю: {artist} - {title}", extra={"site": s_name})
                    headers = {"Referer": t.get("referer", s_cfg["base_url"])}
                    # Источник без прямой HTTP-ссылки (YouTube Music через yt-dlp) отдаёт
                    # fetch(): готовый файл в виде потокового ответа с теми же полями.
                    fetch = t.get("fetch")
                    with (
                        fetch() if callable(fetch)
                        else scraper.get(t["download_url"], timeout=60, headers=headers, stream=True)
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
                                r, download_paths, clean_fn, content_length
                            )

                            # Тот же файл уже скачан (другое имя/источник): физические
                            # копии удаляем. ID фиксируем, чтобы не качать снова, но с
                            # filehash=NULL — иначе нарушится частичный UNIQUE-индекс.
                            duplicate = db.is_downloaded_by_hash(filehash)
                            if duplicate:
                                for saved_path in saved_paths:
                                    with suppress(OSError):
                                        os.remove(saved_path)
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
                                logger.info(f"Успешно скачан: {clean_fn} ({downloaded_bytes} байт)", extra={"site": s_name})
                                time.sleep(1.2)
                        else:
                            logger.error(f"Ошибка соединения (код: {r.status_code})", extra={"site": s_name})
                except DownloadLimitExceeded as e:
                    logger.error(f"Загрузка остановлена: {e}", extra={"site": s_name})
                    raise SystemExit(2) from e
                except Exception as e:
                    queued_tracks.discard(track_identity(t["artist"], t["title"]))
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