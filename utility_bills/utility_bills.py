#!/usr/bin/env python3
"""Квитанции за коммунальные услуги: почта (IMAP) → PDF → OCR-разбор → SQLite для отчётов и графиков.

Запуск — обёртка `utility_bills.sh` в корне mytasks (таймер systemd `utility-bills.timer`, 20-го числа).

Режимы:
  (без ключей)        письма из папки ящика с подходящей темой → PDF → разбор → БД;
  --file PDF …        импорт уже скачанных PDF (например, архива квитанций);
  --reparse           повторный разбор всех PDF из БД текущей версией парсера;
  --check             проверка конфигурации и внешних программ;
  --migrate-db        только миграции схемы БД;
  -n, --dry-run       ничего не меняет (ни БД, ни почтовый ящик), печатает план
                      строками «[dry-run] будет: …»; почта и PDF при этом читаются и разбираются.

Обработанные письма запоминаются в таблице `messages` (ключ — Message-ID, без него — UIDVALIDITY:UID
папки) и при следующих запусках пропускаются без скачивания. Письмо, PDF из которого не удалось
разобрать, обработанным не помечается и повторяется в следующий раз; письмо больше лимита размера
не скачивается и помечается статусом too_large. Ящик открывается только на
чтение (EXAMINE, BODY.PEEK): письма не помечаются прочитанными и не перемещаются.
"""

import argparse
import email
import email.errors
import email.message
import email.policy
import fnmatch
import hashlib
import imaplib
import ipaddress
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import tempfile
import time
import unicodedata
from base64 import b64encode
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any, Self

# Конфигурация после проверки (DEFAULT_CONFIG с перекрытием из JSON).
Config = dict[str, Any]
# Права файлов с персональными данными (БД, PDF, конфиг с паролем) и каталога PDF.
PRIVATE_FILE_MODE = 0o600
# Маска процесса: всё, что создаёт программа (журналы с лицевым счётом и адресами почты, временные
# файлы), — только для владельца.
PRIVATE_UMASK = 0o077
MIB = 1_048_576

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
SCRIPT_NAME = Path(__file__).stem
LOG_NAME = f"{SCRIPT_NAME}_engine"
CONFIG_PATH = BASE_DIR / f"{SCRIPT_NAME}.json"


def _load_parser() -> ModuleType:
    """bill_parser подключается по пути: каталог проекта не в sys.path (как драйверы music_downloader)."""
    spec = spec_from_file_location("utility_bills_bill_parser", BASE_DIR / "bill_parser.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"не найден модуль разбора {BASE_DIR / 'bill_parser.py'}")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bp = _load_parser()

# Значения по умолчанию; перекрываются utility_bills.json.
DEFAULT_CONFIG = {
    "imap": {
        "host": "",
        "port": 993,
        "security": "ssl",  # ssl | starttls | none
        "username": "",
        "password": "",
        "password_env": "UTILITY_BILLS_IMAP_PASSWORD",
        "folder": "INBOX",
        "timeout_seconds": 60,
    },
    "search": {
        # Отбор писем только по отправителю: email из заголовка From совпадает с одним из адресов (целиком,
        # без учёта регистра). Тема письма не учитывается.
        "from": [],
        # Подлинность отправителя: письмо принимается, только если свой почтовый сервер (authserv_id)
        # записал в Authentication-Results dkim=pass или spf=pass с выравниванием по домену From (RFC 8601).
        # Fail closed: по умолчанию проверка включена, authserv_id обязателен.
        "require_auth": True,
        "authserv_id": "",
        "attachment_glob": "*.pdf",
        # Вложение больше лимита не сохраняется и не разбирается (защита от огромных файлов в письмах).
        "max_attachment_mb": 20,
    },
    "db_path": f"{SCRIPT_NAME}.db",
    "ocr": {},
}

# ---------------------------------------------------------------------------
# Логи
# ---------------------------------------------------------------------------


def load_log_template() -> dict[str, str]:
    config = {
        "LOG_SCHEMA_VERSION": "1.0",
        "LOG_COMPAT_TARGETS": "elk,opensearch,loki,graylog,splunk",
        "LOG_FIELD_TS": "@timestamp",
        "LOG_FIELD_LEVEL": "log.level",
        "LOG_FIELD_MESSAGE": "message",
        "LOG_FIELD_EVENT_ACTION": "event.action",
        "LOG_FIELD_SERVICE_NAME": "service.name",
        "LOG_FIELD_SCHEMA_VERSION": "schema.version",
        "KEEP_LOGS": "10",
    }
    for name in ("log_template.conf", "log_template.conf.example"):
        path = REPO_DIR / "conf" / name
        if not path.exists():
            continue
        with suppress(OSError), path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip().strip('"').strip("'")
        break
    return config


LOG_TEMPLATE = load_log_template()
logger = logging.getLogger("utility_bills")


class JSONLHandler(logging.FileHandler):
    """Одна JSON-строка на запись: ECS-поля шаблона и совместимые старые поля (script, event, msg)."""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        event = getattr(record, "event", "message")
        created = datetime.fromtimestamp(record.created).astimezone()
        entry = {
            LOG_TEMPLATE["LOG_FIELD_TS"]: created.isoformat(timespec="seconds"),
            LOG_TEMPLATE["LOG_FIELD_SCHEMA_VERSION"]: LOG_TEMPLATE["LOG_SCHEMA_VERSION"],
            "compat.targets": LOG_TEMPLATE["LOG_COMPAT_TARGETS"],
            LOG_TEMPLATE["LOG_FIELD_LEVEL"]: record.levelname.lower(),
            LOG_TEMPLATE["LOG_FIELD_MESSAGE"]: message,
            LOG_TEMPLATE["LOG_FIELD_EVENT_ACTION"]: event,
            LOG_TEMPLATE["LOG_FIELD_SERVICE_NAME"]: LOG_NAME,
            "script": f"{SCRIPT_NAME}.py",
            "event": event,
            "level": record.levelname.lower(),
            "msg": message,
            "detail": getattr(record, "detail", ""),
        }
        try:
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.flush()
        except Exception:
            # Как в logging.Handler: сбой записи лога не должен ронять обработку.
            self.handleError(record)


def log_dir() -> Path:
    return Path(os.environ.get("MYTASKS_LOG_DIR") or REPO_DIR / "logs")


def setup_logging() -> None:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    handler = JSONLHandler(directory / f"{LOG_NAME}-{stamp}.jsonl", encoding="utf-8")
    shutdown_logging()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def shutdown_logging() -> None:
    """Закрывает файлы логов (при повторном запуске main в одном процессе и в конце работы)."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def cleanup_logs() -> None:
    """Оставляет последние KEEP_LOGS логов движка (общий параметр conf/log_template.conf)."""
    try:
        keep = int(LOG_TEMPLATE.get("KEEP_LOGS", "10"))
    except ValueError:
        keep = 10
    files = sorted(log_dir().glob(f"{LOG_NAME}-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[keep:]:
        with suppress(OSError):
            path.unlink()


class Reporter:
    """Вывод для человека (stdout) и та же запись в JSONL-лог. Строки плана dry-run — через plan()."""

    def __init__(self) -> None:
        self.warnings = 0
        self.errors = 0

    def __call__(self, message: str, event: str = "info", level: int = logging.INFO, detail: str = "") -> None:
        if level >= logging.ERROR:
            self.errors += 1
        elif level >= logging.WARNING:
            self.warnings += 1
        prefix = {logging.WARNING: "ВНИМАНИЕ: ", logging.ERROR: "ОШИБКА: "}.get(level, "")
        print(f"{prefix}{message}", flush=True)
        logger.log(level, message, extra={"event": event, "detail": detail})

    def plan(self, message: str, event: str, detail: str = "") -> None:
        """Строка плана dry-run: «[dry-run] будет: …» и событие dry_<event>."""
        self(f"[dry-run] будет: {message}", event=f"dry_{event}", detail=detail)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """Конфигурация отсутствует или неверна (код выхода 2)."""


Warn = Callable[..., None]


def _type_ok(default: Any, value: Any) -> bool:
    """Тип значения конфигурации совпадает с типом значения по умолчанию (int подходит для float)."""
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def _merge(defaults: dict[str, Any], values: dict[str, Any], path: str, warn: Warn) -> dict[str, Any]:
    """Накладывает значения из JSON на значения по умолчанию с проверкой типов (вложенные объекты — рекурсивно)."""
    result = {}
    for key, default in defaults.items():
        if key not in values:
            result[key] = default
            continue
        value = values[key]
        if isinstance(default, dict) and key != "ocr":
            if not isinstance(value, dict):
                raise ConfigError(f"{path}{key}: ожидается объект")
            result[key] = _merge(default, value, f"{path}{key}.", warn)
        elif isinstance(default, list) and isinstance(value, str):
            raise ConfigError(f'{path}{key}: ожидается массив строк, например ["{value}"]')
        elif isinstance(default, list) and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
            raise ConfigError(f"{path}{key}: ожидается массив строк")
        elif not _type_ok(default, value):
            raise ConfigError(f"{path}{key}: неверный тип ({type(value).__name__})")
        else:
            result[key] = value
    for key in values:
        if key not in defaults and not key.startswith("_"):
            warn(f"Неизвестный ключ конфигурации {path}{key} — пропущен", event="config_unknown_key", level=logging.WARNING)
    return result


def _in_range(key: str, value: Any) -> bool:
    """Скаляр или каждый элемент списка ocr.<key> — в допустимом диапазоне bp.SETTING_RANGES (если он задан)."""
    bounds = bp.SETTING_RANGES.get(key)
    if bounds is None:
        return True
    low, high = bounds
    return all(low <= item <= high for item in (value if isinstance(value, list) else [value]))


def _check_ocr(values: dict[str, Any], warn: Warn) -> dict[str, Any]:
    """Параметры распознавания: известные ключи, верные типы, непустые списки из чисел, числа — в диапазоне
    (отрицательная рамка ячейки или нулевой тайм-аут ломали бы разбор непонятной ошибкой)."""
    ocr = {}
    for key, value in values.items():
        default = bp.DEFAULT_SETTINGS.get(key)
        valid = default is not None and _type_ok(default, value)
        if valid and isinstance(default, list):
            valid = bool(value) and all(_type_ok(1.0, item) for item in value)
        valid = valid and _in_range(key, value)
        if default is None:
            warn(f"Неизвестный параметр ocr.{key} — пропущен", event="config_unknown_key", level=logging.WARNING)
        elif not valid:
            bounds = bp.SETTING_RANGES.get(key)
            allowed = f" (допустимо {bounds[0]:g}–{bounds[1]:g})" if bounds else ""
            warn(f"ocr.{key}: неверное значение{allowed}, оставлено {default!r}", event="config_bad_value", level=logging.WARNING)
        else:
            ocr[key] = value
    return ocr


def _check_permissions(path: Path, info: os.stat_result, config: Config) -> None:
    """Файл с паролем — только владельца (600) и принадлежит текущему пользователю; иначе отказ (fail closed).

    info — fstat того же дескриптора, из которого прочитан конфиг: права проверяются у прочитанного файла,
    а не у того, что лежит по пути в момент проверки (без TOCTOU).
    """
    if not config["imap"]["password"]:
        return
    mode = info.st_mode & 0o777
    hint = f"или оставьте поле пустым и задайте пароль переменной {config['imap']['password_env']}"
    if mode & 0o077:
        raise ConfigError(
            f"файл {path} с паролем доступен другим пользователям (права {mode:o}): выполните chmod 600 {hint}"
        )
    if info.st_uid != os.getuid():
        raise ConfigError(
            f"файл {path} с паролем принадлежит другому пользователю (uid {info.st_uid}): смените владельца (chown) {hint}"
        )


def _read_config(path: Path) -> tuple[Any, os.stat_result]:
    """Содержимое JSON и fstat файла — из одного открытого дескриптора."""
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            raw = handle.read()
        return json.loads(raw.decode("utf-8")), info
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        hint = ""
        if isinstance(error, json.JSONDecodeError):
            hint = (
                '. Синтаксис JSON: строки — только в двойных кавычках, несколько значений — массив ["…", "…"], '
                'двойная кавычка внутри строки — \\"'
            )
        raise ConfigError(f"не удалось прочитать {path}: {error}{hint}") from error


def _check_host(host: str) -> None:
    """imap.host: имя или IP без схемы, пути и порта; IPv6 — без скобок; IP — только частный адрес.

    Для IP сертификат не проверяется (tls_context), поэтому публичный IP — отказ: подменивший адрес
    узел в интернете получил бы пароль.
    """
    if not host:
        return
    if "[" in host or "]" in host:
        raise ConfigError(f"imap.host: «{host}» — IPv6-адрес указывается без квадратных скобок, например ::1")
    if not _HOST_RE.fullmatch(host):
        raise ConfigError(f"imap.host: «{host}» — укажите имя или IP-адрес сервера без https://, пути и порта")
    if is_ip_address(host) and not ipaddress.ip_address(host).is_private:
        raise ConfigError(
            f"imap.host: «{host}» — публичный IP-адрес: для IP сертификат сервера не проверяется, поэтому IP "
            "допустим только частный (адрес в VPN или локальной сети); для сервера в интернете укажите его имя"
        )


def load_config(path: Path, warn: Warn) -> Config:
    if not path.exists():
        raise ConfigError(f"нет файла конфигурации {path}: скопируйте {SCRIPT_NAME}.json.example и заполните")
    values, info = _read_config(path)
    if not isinstance(values, dict):
        raise ConfigError(f"{path}: ожидается JSON-объект")
    config = _merge(DEFAULT_CONFIG, values, "", warn)
    if not 0 < config["imap"]["port"] < 65536:
        raise ConfigError("imap.port: ожидается число 1–65535")
    if config["imap"]["timeout_seconds"] <= 0:
        raise ConfigError("imap.timeout_seconds: ожидается положительное число")
    if config["search"]["max_attachment_mb"] <= 0:
        raise ConfigError("search.max_attachment_mb: ожидается положительное число")
    config["search"]["from"] = _sender_addresses(config["search"]["from"])
    _check_host(config["imap"]["host"])
    config["ocr"] = _check_ocr(config["ocr"], warn)
    config["db_path"] = _resolve(config["db_path"])
    _check_permissions(path, info, config)
    return config


# Имя хоста или IPv4/IPv6-адрес: без схемы (https://), пути, порта, пробелов и скобок.
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+|[0-9A-Fa-f:.]+")
_EMAIL_RE = re.compile(r"[^@\s<>\"(),;:]+@[^@\s<>\"(),;:]+\.[^@\s<>\"(),;:]+")


def _sender_addresses(items: list[str]) -> frozenset[str]:
    """Адреса фильтра search.from в нижнем регистре. Каждый — целый email."""
    addresses = set()
    for item in (item.strip() for item in items):
        if not item:
            continue
        if not _EMAIL_RE.fullmatch(item):
            raise ConfigError(f"search.from: «{item}» — не email (ожидается адрес отправителя, например noreply@example.kz)")
        addresses.add(item.casefold())
    return frozenset(addresses)


def sender_matches(header: str, addresses: frozenset[str]) -> bool:
    """Совпадение отправителя по email целиком (без учёта регистра), а не по подстроке заголовка From:
    фильтр erc@example.kz не пропустит noerc@example.kz и erc@example.kz.evil.test. В From должен быть ровно
    один адрес: «evil@x.test, erc@example.kz» не проходит. Пустой фильтр — никто (fail closed; validate_imap
    требует непустой search.from)."""
    found = [address for _name, address in getaddresses([header]) if address]
    return len(found) == 1 and found[0].casefold() in addresses


# ---------------------------------------------------------------------------
# Подлинность письма (RFC 8601, Authentication-Results)
# ---------------------------------------------------------------------------


def _strip_comments(value: str) -> str:
    """Убирает комментарии «(…)» (вложенные — тоже; скобки в кавычках не комментарий) и сворачивает пробелы."""
    out: list[str] = []
    depth = 0
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            escaped = False
            if depth == 0:
                out.append(char)
        elif char == "\\" and (quoted or depth):
            escaped = True
            if depth == 0:
                out.append(char)
        elif char == '"' and depth == 0:
            quoted = not quoted
            out.append(char)
        elif char == "(" and not quoted:
            depth += 1
        elif char == ")" and not quoted and depth:
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _split_unquoted(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == separator and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def parse_authentication_results(value: str) -> tuple[str, list[dict[str, str]]]:
    """Заголовок Authentication-Results: (authserv-id, результаты). Результат — словарь ключей в нижнем
    регистре: «dkim» → pass, «header.d» → example.kz и т.д. Порядок: authserv-id [версия] ; resinfo ; …"""
    sections = _split_unquoted(_strip_comments(value), ";")
    head = sections[0].split()
    authserv_id = head[0].strip('"').casefold() if head else ""
    results = []
    for section in sections[1:]:
        entry: dict[str, str] = {}
        for token in section.split():
            key, equals, val = token.partition("=")
            if equals and key:
                entry.setdefault(key.strip().casefold(), val.strip().strip('"'))
        if entry:
            results.append(entry)
    return authserv_id, results


def _domain_of(identity: str) -> str:
    """Домен из значения header.d / header.i / smtp.mailfrom: «user@mail.example.kz» и «@example.kz» → домен."""
    return identity.rpartition("@")[2].strip().rstrip(".").casefold()


def _domain_aligned(auth_domain: str, from_domain: str) -> bool:
    """Домен проверки совпадает с доменом From или является его родительским (не короче второго уровня)."""
    if not auth_domain or "." not in auth_domain:
        return False
    return from_domain == auth_domain or from_domain.endswith("." + auth_domain)


def sender_authenticated(headers: list[str], authserv_id: str, from_domain: str) -> bool:
    """Свой сервер (authserv-id без учёта регистра) подтвердил dkim=pass (header.d / header.i) или spf=pass
    (smtp.mailfrom) в домене отправителя. Заголовки других authserv-id игнорируются: их мог подставить
    отправитель. В записи берётся первое вхождение каждого ключа."""
    wanted = authserv_id.strip().casefold()
    domain = from_domain.strip().rstrip(".").casefold()
    if not wanted or not domain:
        return False
    for header in headers:
        server, results = parse_authentication_results(header)
        if server != wanted:
            continue
        for entry in results:
            if entry.get("dkim", "").casefold() == "pass" and any(
                _domain_aligned(_domain_of(entry.get(key, "")), domain) for key in ("header.d", "header.i")
            ):
                return True
            if entry.get("spf", "").casefold() == "pass" and _domain_aligned(_domain_of(entry.get("smtp.mailfrom", "")), domain):
                return True
    return False


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def imap_password(config: Config) -> str:
    """Пароль IMAP: заполненный imap.password в конфиге главнее; переменная окружения из imap.password_env
    используется, только если в конфиге пароль пустой (решение пользователя 2026-11-11)."""
    if config["imap"]["password"]:
        return config["imap"]["password"]
    name = config["imap"]["password_env"]
    return (os.environ.get(name) if name else None) or ""


def validate_imap(config: Config) -> None:
    imap = config["imap"]
    missing = [key for key in ("host", "username") if not imap[key]]
    if not imap_password(config):
        missing.append(f"password (или переменная {imap['password_env']})")
    if not config["search"]["from"]:
        missing.append("search.from (email отправителей квитанций, иначе были бы все письма папки)")
    if config["search"]["require_auth"] and not config["search"]["authserv_id"].strip():
        missing.append("search.authserv_id (имя своего почтового сервера в Authentication-Results; или require_auth = false)")
    if missing:
        raise ConfigError("в конфигурации не заполнено: " + ", ".join(missing))
    if imap["security"] not in ("ssl", "starttls", "none"):
        raise ConfigError("imap.security: допустимо ssl, starttls или none")
    local = ("localhost", "127.0.0.1", "::1")
    if imap["security"] == "none" and imap["host"] not in local:
        # Пароль открытым текстом по сети не передаётся (fail closed).
        raise ConfigError("imap.security = none допустим только для localhost: используйте ssl или starttls")


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
# Версия схемы — PRAGMA user_version. Схема меняется ТОЛЬКО миграцией: изменение = правка DB_SCHEMA
# + новая миграция в DB_MIGRATIONS через _rebuild_table (не ALTER TABLE). DDL — только здесь.

DB_TIMEOUT_SECONDS = 30

DB_SCHEMA = {
    # Квитанция: шапка, итог, сверки, исходный файл и письмо.
    "bills": {
        "columns": (
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, period TEXT NOT NULL, "
            "service_period TEXT, doc_number TEXT, issued_at TEXT, due_date TEXT, payer TEXT, address TEXT, "
            "address_kz TEXT, residents INTEGER, total_area REAL, heated_area REAL, useful_area REAL, "
            "controller TEXT, total REAL, items_sum REAL, checksum_ok INTEGER, rows_ok INTEGER, "
            "rows_failed INTEGER, tariff_checks_ok INTEGER, tariff_checks_failed INTEGER, tier_residents INTEGER, "
            "saldo_date TEXT, accrual_month TEXT, table_header_text TEXT, format_signature TEXT, format_features TEXT, "
            "warnings TEXT, notes TEXT, header_text TEXT, footer_text TEXT, parser_version INTEGER, "
            "file_name TEXT, file_sha256 TEXT, file_size INTEGER, needs_review INTEGER, review_issue TEXT, review_result TEXT, pdf BLOB, message_key TEXT, "
            "email_subject TEXT, email_from TEXT, email_date TEXT, imported_at TEXT, updated_at TEXT)"
        ),
        "indexes": {
            "idx_bills_account_period": (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_account_period ON bills(account, period)"
            ),
            "idx_bills_file_sha256": "CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_file_sha256 ON bills(file_sha256)",
        },
    },
    # Строки услуг; sub_row > 0 — подстрока ячейки (например, «потери» электроэнергии).
    "bill_items": {
        "columns": (
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE, position INTEGER, sub_row INTEGER, "
            "service TEXT, service_raw TEXT, known_service INTEGER, provider TEXT, provider_phone TEXT, "
            "saldo_start REAL, paid REAL, prev_reading REAL, curr_reading REAL, consumption REAL, quantity REAL, "
            "quantity_extra REAL, tariff TEXT, tariff_value REAL, tariff_unit TEXT, accrued REAL, "
            "accrued_parts TEXT, penalty REAL, recalculation REAL, to_pay REAL, paying REAL, "
            "expected_to_pay REAL, row_check_ok INTEGER, expected_accrued REAL, tariff_check_ok INTEGER, accrued_total REAL, "
            "tiers_source TEXT, raw TEXT)"
        ),
        "indexes": {
            "idx_bill_items_row": (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bill_items_row ON bill_items(bill_id, position, sub_row)"
            ),
            "idx_bill_items_service": "CREATE INDEX IF NOT EXISTS idx_bill_items_service ON bill_items(service)",
        },
    },
    # Ступени тарифа (у электроэнергии три: минимальный, средний, максимальный) и объём на каждой ступени.
    "bill_item_tariffs": {
        "columns": (
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "item_id INTEGER NOT NULL REFERENCES bill_items(id) ON DELETE CASCADE, tier INTEGER, name TEXT, "
            "label TEXT, price REAL, unit TEXT, from_qty REAL, to_qty REAL, quantity REAL, amount REAL)"
        ),
        "indexes": {
            "idx_bill_item_tariffs_tier": (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bill_item_tariffs_tier ON bill_item_tariffs(item_id, tier)"
            ),
        },
    },
    # Справочник тарифов с историей: одна запись — период действия одной цены услуги (ступени).
    # Месяцы — месяцы оказания услуг (ГГГГ-ММ), valid_to — последний месяц, в котором цена встречалась.
    # Записи source = 'bill' пересобираются из квитанций при каждой записи; 'manual' не трогаются.
    "tariffs": {
        "columns": (
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, tier INTEGER NOT NULL, tier_name TEXT, "
            "price REAL NOT NULL, unit TEXT, valid_from TEXT NOT NULL, valid_to TEXT, is_current INTEGER, "
            "bills_count INTEGER, first_bill_id INTEGER, last_bill_id INTEGER, provider TEXT, "
            "source TEXT NOT NULL DEFAULT 'bill', updated_at TEXT)"
        ),
        "indexes": {
            "idx_tariffs_period": "CREATE INDEX IF NOT EXISTS idx_tariffs_period ON tariffs(service, tier, valid_from)",
        },
    },
    # Справочник количеств (колонка «Кол-во») с историей, устроен как tariffs: kind = quantity — основное
    # число (площадь, человек, кВт·ч, м³), quantity_extra — второе число подъячейки («17 | 2»);
    # normative = 1 — нормативное количество (площадь м², число человек), 0 — объём (кВт·ч, м³) по счётчику
    # или расчётный (ОДН), меняется каждый месяц.
    "bill_formats": {
        "columns": (
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT NOT NULL UNIQUE, description TEXT, "
            "features TEXT, first_period TEXT, last_period TEXT, is_current INTEGER, bills_count INTEGER, "
            "first_bill_id INTEGER, last_bill_id INTEGER, updated_at TEXT)"
        ),
        "indexes": {},
    },
    "quantities": {
        "columns": (
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, kind TEXT NOT NULL, value REAL NOT NULL, "
            "unit TEXT, normative INTEGER, valid_from TEXT NOT NULL, valid_to TEXT, is_current INTEGER, "
            "bills_count INTEGER, first_bill_id INTEGER, last_bill_id INTEGER, "
            "source TEXT NOT NULL DEFAULT 'bill', updated_at TEXT)"
        ),
        "indexes": {
            "idx_quantities_period": "CREATE INDEX IF NOT EXISTS idx_quantities_period ON quantities(service, kind, valid_from)",
        },
    },
    # Обработанные письма: при следующих запусках пропускаются без скачивания.
    "messages": {
        "columns": (
            "(key TEXT PRIMARY KEY, message_id TEXT, folder TEXT, uidvalidity INTEGER, uid INTEGER, "
            "subject TEXT, sender TEXT, recipients TEXT, date TEXT, size INTEGER, status TEXT, pdf_count INTEGER, "
            "attachments TEXT, body_text TEXT, headers TEXT, processed_at TEXT)"
        ),
        "indexes": {},
    },
    # Итоги запусков.
    "runs": {
        "columns": (
            "(started TEXT PRIMARY KEY, finished TEXT, seconds REAL, exit_code INTEGER, mode TEXT, "
            "messages_found INTEGER, messages_skipped INTEGER, messages_processed INTEGER, pdfs INTEGER, "
            "bills_new INTEGER, bills_replaced INTEGER, bills_unchanged INTEGER, bills_review INTEGER, warnings INTEGER, "
            "errors INTEGER)"
        ),
        "indexes": {},
    },
}

# Представления для отчётов и графиков (пересоздаются при создании базы и после миграций).
DB_VIEWS = {
    # Плоская таблица: квитанция + строка услуги + расчётные поля.
    "v_items": (
        "CREATE VIEW IF NOT EXISTS v_items AS SELECT b.account, b.period, b.service_period, b.issued_at, "
        "b.due_date, b.residents, b.total_area, i.position, i.sub_row, i.service, i.provider, i.provider_phone, "
        "i.saldo_start, i.paid, i.prev_reading, i.curr_reading, i.consumption, i.quantity, i.quantity_extra, "
        "i.tariff, i.tariff_value, i.tariff_unit, i.accrued, i.expected_accrued, i.penalty, i.recalculation, "
        "i.to_pay, CASE WHEN i.quantity > 0 AND i.accrued IS NOT NULL THEN ROUND(i.accrued / i.quantity, 4) END "
        "AS effective_price, i.row_check_ok, i.tariff_check_ok, b.checksum_ok, b.id AS bill_id, i.id AS item_id "
        "FROM bill_items i JOIN bills b ON b.id = i.bill_id"
    ),
    # Итоги по месяцам.
    "v_monthly": (
        "CREATE VIEW IF NOT EXISTS v_monthly AS SELECT b.account, b.period, b.service_period, b.total, "
        "b.items_sum, b.checksum_ok, b.due_date, COUNT(i.id) AS services, SUM(i.saldo_start) AS saldo_start, "
        "SUM(i.paid) AS paid, SUM(i.accrued) AS accrued, SUM(i.penalty) AS penalty, "
        "SUM(i.recalculation) AS recalculation, SUM(i.to_pay) AS to_pay "
        "FROM bills b LEFT JOIN bill_items i ON i.bill_id = b.id GROUP BY b.id"
    ),
    # История тарифов (все ступени).
    "v_tariffs": (
        "CREATE VIEW IF NOT EXISTS v_tariffs AS SELECT b.account, b.period, b.service_period, i.service, "
        "t.tier, t.name, t.price, t.unit, t.from_qty, t.to_qty, t.quantity, t.amount FROM bill_item_tariffs t JOIN bill_items i ON i.id = t.item_id "
        "JOIN bills b ON b.id = i.bill_id"
    ),
    # Действующие тарифы (последний период каждой услуги и ступени).
    "v_tariffs_current": (
        "CREATE VIEW IF NOT EXISTS v_tariffs_current AS SELECT service, tier, tier_name, price, unit, valid_from, "
        "valid_to, provider, source FROM tariffs WHERE is_current = 1 ORDER BY service, tier"
    ),
    # Действующие количества (последний период каждой услуги).
    "v_quantities_current": (
        "CREATE VIEW IF NOT EXISTS v_quantities_current AS SELECT service, kind, value, unit, normative, valid_from, "
        "valid_to, source FROM quantities WHERE is_current = 1 ORDER BY service, kind"
    ),
    # Показания и расход по счётчикам.
    "v_consumption": (
        "CREATE VIEW IF NOT EXISTS v_consumption AS SELECT b.account, b.period, b.service_period, i.service, "
        "i.prev_reading, i.curr_reading, i.consumption, i.quantity, i.tariff_unit, i.accrued "
        "FROM bill_items i JOIN bills b ON b.id = i.bill_id WHERE i.curr_reading IS NOT NULL"
    ),
}


class MigrationError(RuntimeError):
    """Миграция схемы БД не выполнена; база осталась в прежней версии."""


def table_ddl(table: str, name: str | None = None) -> str:
    return f"CREATE TABLE IF NOT EXISTS {name or table} {DB_SCHEMA[table]['columns']}"


def schema_columns(table: str) -> list[str]:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(table_ddl(table))
        return _table_columns(conn, table)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return [row[0] for row in rows]


def _drop_views(conn: sqlite3.Connection) -> list[str]:
    custom = []
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view' ORDER BY rowid").fetchall():
        if name not in DB_VIEWS:
            custom.append(sql)
        conn.execute(f'DROP VIEW IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')
    return custom


def _create_views(conn: sqlite3.Connection) -> None:
    for ddl in DB_VIEWS.values():
        conn.execute(ddl)


def _rebuild_table(conn: sqlite3.Connection, table: str, out: Callable[[str], None]) -> None:
    """Пересборка таблицы в схему кода: порядок колонок, недостающие — NULL, лишние отбрасываются."""
    if not _table_exists(conn, table):
        conn.execute(table_ddl(table))
        for ddl in DB_SCHEMA[table]["indexes"].values():
            conn.execute(ddl)
        return
    old_columns = _table_columns(conn, table)
    new_columns = schema_columns(table)
    common = [column for column in new_columns if column in old_columns]
    dropped = [column for column in old_columns if column not in new_columns]
    sequence = None
    if _table_exists(conn, "sqlite_sequence"):
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
        sequence = row[0] if row else None
    new_name = f"{table}__new"
    conn.execute(f"DROP TABLE IF EXISTS {new_name}")
    conn.execute(table_ddl(table, new_name))
    column_list = ", ".join(f'"{column}"' for column in common)
    conn.execute(f'INSERT INTO {new_name} ({column_list}) SELECT {column_list} FROM "{table}"')
    old_count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    new_count = conn.execute(f"SELECT COUNT(*) FROM {new_name}").fetchone()[0]
    if old_count != new_count:
        raise MigrationError(f"таблица {table}: строк после копирования {new_count}, было {old_count}")
    if dropped:
        out(f"Миграция: таблица {table}: отбрасываются колонки {', '.join(dropped)}")
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE {new_name} RENAME TO "{table}"')
    if sequence is not None:
        # Счётчик AUTOINCREMENT не меньше прежнего: id удалённых строк не переиспользуются.
        conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (sequence, table)
        )
    for ddl in DB_SCHEMA[table]["indexes"].values():
        conn.execute(ddl)
    out(f"Миграция: таблица {table}: {new_count} строк, колонки {', '.join(new_columns)}")


def rebuild_formats(conn: sqlite3.Connection, now: str) -> None:
    """Справочник bill_formats из bills: по подписи — описание, признаки последней квитанции, период.

    conn — с row_factory = sqlite3.Row; функция общая для миграции и Database (без обхода __init__)."""
    rows = conn.execute(
        "SELECT id, format_signature AS signature, format_features AS features, "
        "COALESCE(service_period, period) AS month FROM bills WHERE format_signature IS NOT NULL ORDER BY month, id"
    ).fetchall()
    formats: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = formats.setdefault(
            row["signature"], {"signature": row["signature"], "first_period": row["month"], "first_bill_id": row["id"], "bills_count": 0}
        )
        features = json.loads(row["features"] or "{}")
        entry.update(
            description=features.get("description"),
            features=row["features"],
            last_period=row["month"],
            last_bill_id=row["id"],
            bills_count=entry["bills_count"] + 1,
        )
    latest = rows[-1]["signature"] if rows else None
    conn.execute("DELETE FROM bill_formats")
    for entry in formats.values():
        entry.update(is_current=int(entry["signature"] == latest), updated_at=now)
        columns = ", ".join(entry)
        marks = ", ".join("?" for _ in entry)
        conn.execute(f"INSERT INTO bill_formats ({columns}) VALUES ({marks})", list(entry.values()))


def _migration_1(conn: sqlite3.Connection, out: Callable[[str], None]) -> None:
    for table in DB_SCHEMA:
        _rebuild_table(conn, table, out)


def _migration_2(conn: sqlite3.Connection, out: Callable[[str], None]) -> None:
    _rebuild_table(conn, "bills", out)
    _rebuild_table(conn, "bill_formats", out)
    rebuild_formats(conn, datetime.now().astimezone().isoformat(timespec="seconds"))


def _migration_3(conn: sqlite3.Connection, out: Callable[[str], None]) -> None:
    """Путь к копии для сверки (review_path) → флаг needs_review и поля ручной сверки review_issue /
    review_result; путь к файлу архива (file_path) отбрасывается: каталогов pdf_dir/review_dir больше нет,
    PDF хранится только в самой БД."""
    flagged = []
    if "review_path" in _table_columns(conn, "bills"):
        flagged = [(row[0],) for row in conn.execute("SELECT id FROM bills WHERE review_path IS NOT NULL")]
    _rebuild_table(conn, "bills", out)
    conn.execute("UPDATE bills SET needs_review = 0")
    conn.executemany("UPDATE bills SET needs_review = 1 WHERE id = ?", flagged)
    out(f"Миграция: квитанций с флагом сверки: {len(flagged)}")


DB_MIGRATIONS = {1: _migration_1, 2: _migration_2, 3: _migration_3}
DB_LATEST = max(DB_MIGRATIONS)


def _backup_path(path: Path, version: int) -> str:
    return f"{path}.bak-v{version}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def migrate_database(path: Path | str, dry_run: bool = False, out: Callable[[str], None] = print) -> None:
    """Приводит БД к DB_LATEST: новая — сразу текущая схема; старая — резервная копия и миграции."""
    path = Path(path)
    if dry_run:
        if not path.exists():
            out(f"[dry-run] будет: создана новая база v{DB_LATEST}: {path}")
            return
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < DB_LATEST:
                out(f"[dry-run] будет: миграция БД v{version} → v{DB_LATEST} с резервной копией {_backup_path(path, version)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Файл БД создаётся сразу с правами 600: в квитанциях адрес, лицевой счёт, телефоны.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, PRIVATE_FILE_MODE))
    conn = sqlite3.connect(path, timeout=DB_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not _user_tables(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table, spec in DB_SCHEMA.items():
                    conn.execute(table_ddl(table))
                    for ddl in spec["indexes"].values():
                        conn.execute(ddl)
                _create_views(conn)
                conn.execute(f"PRAGMA user_version = {DB_LATEST}")
                conn.execute("COMMIT")
            except sqlite3.Error as error:
                conn.execute("ROLLBACK")
                raise MigrationError(f"не удалось создать базу: {error}") from error
            out(f"Создана новая база v{DB_LATEST}: {path}")
            return
        if version >= DB_LATEST:
            return
        backup = _backup_path(path, version)
        os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE))
        with closing(sqlite3.connect(backup)) as target:
            conn.backup(target)
        out(f"Резервная копия БД перед миграцией: {backup}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            custom_views = _drop_views(conn)
            for number in range(version + 1, DB_LATEST + 1):
                DB_MIGRATIONS[number](conn, out)
                conn.execute(f"PRAGMA user_version = {number}")
            _create_views(conn)
            for ddl in custom_views:
                conn.execute(ddl)
            conn.execute("COMMIT")
        except (sqlite3.Error, MigrationError) as error:
            conn.execute("ROLLBACK")
            raise MigrationError(f"миграция до v{DB_LATEST} не выполнена, откат (база v{version}): {error}") from error
        out(f"Схема БД обновлена до v{DB_LATEST}")
    except sqlite3.Error as error:
        raise MigrationError(f"ошибка SQLite: {error}") from error
    finally:
        conn.close()


class Database:
    """Доступ к БД. read_only — для dry-run: файл открывается только на чтение (или отсутствует)."""

    def __init__(self, path: Path | str, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self.conn: sqlite3.Connection | None = None
        if read_only:
            if self.path.exists():
                self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        else:
            self.conn = sqlite3.connect(self.path, timeout=DB_TIMEOUT_SECONDS)
            self.conn.execute("PRAGMA foreign_keys = ON")
        if self.conn is not None:
            self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _require_writable(self) -> None:
        if self.conn is None or self.read_only:
            raise sqlite3.ProgrammingError("база открыта только на чтение")

    def _query(self, table: str, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Чтение; в dry-run базы может ещё не быть — тогда пустой результат."""
        if self.conn is None or not _table_exists(self.conn, table):
            return []
        return self.conn.execute(sql, params).fetchall()

    def processed_keys(self) -> set[str]:
        return {row["key"] for row in self._query("messages", "SELECT key FROM messages")}

    def bill_by_sha(self, sha: str) -> sqlite3.Row | None:
        rows = self._query("bills", "SELECT * FROM bills WHERE file_sha256 = ?", (sha,))
        return rows[0] if rows else None

    def bill_by_period(self, account: str, period: str) -> sqlite3.Row | None:
        rows = self._query("bills", "SELECT * FROM bills WHERE account = ? AND period = ?", (account, period))
        return rows[0] if rows else None

    def all_bills(self) -> list[sqlite3.Row]:
        return self._query("bills", "SELECT * FROM bills ORDER BY period")

    def save_bill(self, bill: Any, meta: dict[str, Any]) -> str:
        """Пишет квитанцию со строками в одной транзакции; квитанция за тот же счёт и месяц заменяется.

        Заменяются все записи, конфликтующие с новой по любому уникальному ключу (счёт + месяц или
        SHA-256 файла), иначе вставка нарушила бы второй ключ. Возвращает "new" или "replaced".
        """
        self._require_writable()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.conn:
            conflicts = self.conn.execute(
                "SELECT id, imported_at, file_sha256, pdf, warnings, review_issue, review_result FROM bills "
                "WHERE (account = ? AND period = ?) OR file_sha256 = ? "
                "ORDER BY imported_at",
                (bill.account, bill.period, meta["file_sha256"]),
            ).fetchall()
            old = conflicts[0] if conflicts else None
            for row in conflicts:
                self.conn.execute("DELETE FROM bills WHERE id = ?", (row["id"],))
            warnings = json.dumps(bill.warnings, ensure_ascii=False)
            # Итог ручной сверки переживает замену и --reparse; сверка снова нужна, только если изменились
            # предупреждения (иной разбор — иные расхождения).
            reviewed = old is not None and bool(old["review_result"]) and old["warnings"] == warnings
            values = {
                "account": bill.account,
                "period": bill.period,
                "service_period": bill.service_period,
                "doc_number": bill.doc_number,
                "issued_at": bill.issued_at,
                "due_date": bill.due_date,
                "payer": bill.payer,
                "address": bill.address,
                "address_kz": bill.address_kz,
                "residents": bill.residents,
                "total_area": bill.total_area,
                "heated_area": bill.heated_area,
                "useful_area": bill.useful_area,
                "controller": bill.controller,
                "total": bill.total,
                "items_sum": bill.items_sum,
                "checksum_ok": int(bill.checksum_ok),
                "rows_ok": bill.rows_ok,
                "rows_failed": bill.rows_failed,
                "tariff_checks_ok": bill.tariff_checks_ok,
                "tariff_checks_failed": bill.tariff_checks_failed,
                "tier_residents": bill.tier_residents,
                "warnings": warnings,
                "notes": json.dumps(bill.notes, ensure_ascii=False),
                "header_text": bill.header_text,
                "footer_text": bill.footer_text,
                "saldo_date": bill.saldo_date,
                "accrual_month": bill.accrual_month,
                "table_header_text": bill.table_header_text,
                "format_signature": bill.format_signature,
                "format_features": json.dumps(bill.format_features, ensure_ascii=False),
                # Сам PDF хранится только в БД: отдельного архива файлов нет.
                "pdf": meta.get("pdf") or next((row["pdf"] for row in conflicts if row["file_sha256"] == meta["file_sha256"]), None),
                "parser_version": bill.parser_version,
                "file_name": meta["file_name"],
                # Флаг сверки: что-то не сошлось (bills.warnings) и итога ручной сверки ещё нет.
                "needs_review": int(bool(bill.warnings) and not reviewed),
                # Описание проблемы и итог сверки заполняет человек (SQL UPDATE), разбор их не трогает.
                "review_issue": old["review_issue"] if old is not None else None,
                "review_result": old["review_result"] if old is not None else None,
                "file_sha256": meta["file_sha256"],
                "file_size": meta["file_size"],
                "message_key": meta.get("message_key"),
                "email_subject": meta.get("email_subject"),
                "email_from": meta.get("email_from"),
                "email_date": meta.get("email_date"),
                "imported_at": old["imported_at"] if old is not None else now,
                "updated_at": now,
            }
            columns = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            bill_id = self.conn.execute(f"INSERT INTO bills ({columns}) VALUES ({marks})", list(values.values())).lastrowid
            for item in bill.items:
                row = {
                    "bill_id": bill_id,
                    "position": item.position,
                    "sub_row": item.sub_row,
                    "service": item.service,
                    "service_raw": item.service_raw,
                    "known_service": int(item.known_service),
                    "provider": item.provider,
                    "provider_phone": item.provider_phone,
                    "saldo_start": item.saldo_start,
                    "paid": item.paid,
                    "prev_reading": item.prev_reading,
                    "curr_reading": item.curr_reading,
                    "consumption": item.consumption,
                    "quantity": item.quantity,
                    "quantity_extra": item.quantity_extra,
                    "tariff": item.tariff,
                    "tariff_value": item.tariff_value,
                    "tariff_unit": item.tariff_unit,
                    "accrued": item.accrued,
                    "accrued_parts": json.dumps(item.accrued_parts) if item.accrued_parts else None,
                    "penalty": item.penalty,
                    "recalculation": item.recalculation,
                    "to_pay": item.to_pay,
                    "paying": item.paying,
                    "expected_to_pay": item.expected_to_pay,
                    "row_check_ok": None if item.row_check_ok is None else int(item.row_check_ok),
                    "expected_accrued": item.expected_accrued,
                    "tariff_check_ok": None if item.tariff_check_ok is None else int(item.tariff_check_ok),
                    "accrued_total": item.accrued_total,
                    "tiers_source": item.tiers_source,
                    "raw": json.dumps(item.raw, ensure_ascii=False) if item.raw else None,
                }
                columns = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                item_id = self.conn.execute(
                    f"INSERT INTO bill_items ({columns}) VALUES ({marks})", list(row.values())
                ).lastrowid
                for tier in item.tariffs:
                    self.conn.execute(
                        "INSERT INTO bill_item_tariffs (item_id, tier, name, label, price, unit, from_qty, to_qty, "
                        "quantity, amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item_id,
                            tier.tier,
                            tier.name,
                            tier.label,
                            tier.price,
                            tier.unit,
                            tier.from_qty,
                            tier.to_qty,
                            tier.quantity,
                            tier.amount,
                        ),
                    )
            self._rebuild_history(now)
        return "replaced" if old is not None else "new"

    def _rebuild_history(self, now: str) -> None:
        """Пересборка справочников tariffs и quantities из всех квитанций (внутри транзакции записи).

        Пересборка целиком, а не дописывание: справочники верны при любом порядке импорта (архив
        задним числом), замене квитанции и --reparse. Записи source = 'manual' не трогаются.
        """
        # Месяцы, за которые в базе есть квитанции: значение, пропавшее в одном из них, разрывает период.
        months = sorted({row[0] for row in self.conn.execute("SELECT COALESCE(service_period, period) FROM bills")})
        tariffs = self.conn.execute(
            "SELECT i.service, t.tier, t.label AS tier_name, t.price, t.unit, i.provider, b.id AS bill_id, "
            "COALESCE(b.service_period, b.period) AS month FROM bill_item_tariffs t "
            "JOIN bill_items i ON i.id = t.item_id JOIN bills b ON b.id = i.bill_id "
            "WHERE t.price IS NOT NULL ORDER BY i.service, t.tier, month, b.id"
        ).fetchall()
        segments = _history_segments(tariffs, ("service", "tier"), "price", ("tier_name", "unit", "provider"), months)
        self._replace_history("tariffs", segments, now)
        quantities = []
        for row in self.conn.execute(
            "SELECT i.service, k.kind, CASE k.kind WHEN 'quantity' THEN i.quantity ELSE i.quantity_extra END AS value, "
            "i.tariff_unit, i.curr_reading IS NOT NULL AS metered, b.id AS bill_id, "
            "COALESCE(b.service_period, b.period) AS month FROM bill_items i JOIN bills b ON b.id = i.bill_id "
            "CROSS JOIN (SELECT 'quantity' AS kind UNION ALL SELECT 'quantity_extra') k "
            "WHERE value IS NOT NULL ORDER BY i.service, k.kind, month, b.id"
        ):
            # Единица — из тарифа и только для основного количества: второе число подъячейки — не объём.
            unit = bp.quantity_unit(row["tariff_unit"]) if row["kind"] == "quantity" else None
            # Объём (кВт·ч, м³) — по счётчику или расчётный (ОДН) — меняется каждый месяц; норматив — площадь,
            # человек. Второе число подъячейки — не объём счётчика («2» у водоотведения), поэтому норматив.
            volume = unit in VOLUME_UNITS or (row["metered"] and row["kind"] == "quantity")
            normative = int(not volume)
            quantities.append({**dict(row), "unit": unit, "normative": normative})
        segments = _history_segments(quantities, ("service", "kind"), "value", ("unit", "normative"), months)
        self._replace_history("quantities", segments, now)
        self._rebuild_formats(now)

    def _rebuild_formats(self, now: str) -> None:
        rebuild_formats(self.conn, now)

    def _replace_history(self, table: str, segments: list[dict[str, Any]], now: str) -> None:
        # table — константа кода (tariffs | quantities), значения — только параметрами.
        self.conn.execute(f"DELETE FROM {table} WHERE source = 'bill'")
        for segment in segments:
            columns = ", ".join(segment)
            marks = ", ".join("?" for _ in segment)
            self.conn.execute(
                f"INSERT INTO {table} ({columns}, source, updated_at) VALUES ({marks}, 'bill', ?)", [*segment.values(), now]
            )

    def _history_changes(self, table: str, key: str, value: str, month: str, where: str = "") -> list[sqlite3.Row]:
        """Записи справочника, значение которых сменилось с месяца month, и предыдущее значение.

        table, key, value, where — константы кода; month — параметр.
        """
        return self._query(
            table,
            f"SELECT n.service, n.{key} AS key, n.{value} AS value, p.{value} AS old_value, p.valid_to AS old_to, n.* "
            f"FROM {table} n JOIN {table} p ON p.service = n.service AND p.{key} = n.{key} AND p.source = n.source "
            f"AND p.valid_to = (SELECT MAX(q.valid_to) FROM {table} q WHERE q.service = n.service AND q.{key} = n.{key} "
            f"AND q.source = n.source AND q.valid_to < n.valid_from) "
            f"WHERE n.valid_from = ? AND n.source = 'bill' {where} ORDER BY n.service, n.{key}",
            (month,),
        )

    def tariff_changes(self, month: str) -> list[sqlite3.Row]:
        """Тарифы, цена которых сменилась с месяца month."""
        return self._history_changes("tariffs", "tier", "price", month)

    def quantity_changes(self, month: str) -> list[sqlite3.Row]:
        """Нормативные количества (площадь, человек — не счётчики), изменившиеся с месяца month."""
        return self._history_changes("quantities", "kind", "value", month, "AND n.normative = 1")

    def mark_message(self, message: dict[str, Any], status: str, content: FetchedMessage) -> None:
        """Письмо обработано: всё о нём — заголовки, получатели, размер, текст, вложения (без их содержимого)."""
        self._require_writable()
        row = {
            "key": message["key"],
            "message_id": message["message_id"],
            "folder": message["folder"],
            "uidvalidity": message["uidvalidity"],
            "uid": message["uid"],
            "subject": message["subject"],
            "sender": message["sender"],
            "recipients": message.get("recipients"),
            "date": message["date"],
            "size": message.get("size"),
            "status": status,
            "pdf_count": len(content.pdfs),
            "attachments": json.dumps(content.attachments, ensure_ascii=False),
            "body_text": content.body_text,
            "headers": message.get("headers"),
            "processed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.conn:
            self.conn.execute(f"INSERT OR REPLACE INTO messages ({columns}) VALUES ({marks})", list(row.values()))

    def save_run(self, stats: dict[str, Any]) -> None:
        self._require_writable()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs (started, finished, seconds, exit_code, mode, messages_found, "
                "messages_skipped, messages_processed, pdfs, bills_new, bills_replaced, bills_unchanged, bills_review, "
                "warnings, errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stats["started"],
                    stats["finished"],
                    stats["seconds"],
                    stats["exit_code"],
                    stats["mode"],
                    stats["messages_found"],
                    stats["messages_skipped"],
                    stats["messages_processed"],
                    stats["pdfs"],
                    stats["bills_new"],
                    stats["bills_replaced"],
                    stats["bills_unchanged"],
                    stats["bills_review"],
                    stats["warnings"],
                    stats["errors"],
                ),
            )


VOLUME_UNITS = frozenset({"кВт*ч", "м3"})


def _history_segments(
    rows: Iterable[Any],
    key_fields: tuple[str, ...],
    value_field: str,
    carry_fields: tuple[str, ...],
    bill_months: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Периоды неизменного значения: строки отсортированы по ключу и месяцу; подряд идущие месяцы с тем
    же значением — один период (valid_from … valid_to), смена значения закрывает период (is_current = 0).

    Период разрывается и тогда, когда между двумя месяцами есть квитанция (bill_months), где значения
    нет: услуга в тот месяц отсутствовала. Месяц без квитанции в базе разрывом не считается.
    Поля carry_fields берутся из последней квитанции периода."""
    segments: list[dict[str, Any]] = []
    for row in rows:
        last = segments[-1] if segments else None
        same_key = last is not None and all(last[field] == row[field] for field in key_fields)
        gap = same_key and any(last["valid_to"] < month < row["month"] for month in bill_months)
        if last is not None and same_key and not gap and abs(last[value_field] - row[value_field]) < 1e-9:
            last.update(valid_to=row["month"], last_bill_id=row["bill_id"], bills_count=last["bills_count"] + 1)
            for field in carry_fields:
                if row[field] is not None:
                    last[field] = row[field]
            continue
        if last is not None and same_key:
            last["is_current"] = 0
        segment = {field: row[field] for field in (*key_fields, value_field, *carry_fields)}
        segment.update(
            valid_from=row["month"],
            valid_to=row["month"],
            is_current=1,
            bills_count=1,
            first_bill_id=row["bill_id"],
            last_bill_id=row["bill_id"],
        )
        segments.append(segment)
    return segments


# ---------------------------------------------------------------------------
# Контекст запуска
# ---------------------------------------------------------------------------

STAT_KEYS = (
    "messages_found",
    "messages_skipped",
    "messages_processed",
    "pdfs",
    "bills_new",
    "bills_replaced",
    "bills_unchanged",
    "bills_review",
)


@dataclass
class RunContext:
    """Всё, что нужно шагам запуска: конфигурация, база, вывод, счётчики, режимы."""

    config: Config
    db: Database
    say: Reporter
    dry_run: bool = False
    force: bool = False  # --reparse: разбирать и PDF, уже записанные в базу
    stats: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAT_KEYS, 0))


# ---------------------------------------------------------------------------
# PDF: хранение и разбор
# ---------------------------------------------------------------------------


# Имя файла из письма: основа не длиннее MAX_NAME_STEM символов и MAX_NAME_STEM_BYTES байт UTF-8. Байты
# ограничены отдельно: предел имени в ФС — 255 байт (PDF на время разбора пишется во временный каталог под
# этим именем); без лимита длинное имя вложения давало бы ENAMETOOLONG и вечный повтор.
MAX_NAME_STEM = 120
MAX_NAME_STEM_BYTES = 180
MAX_NAME_SUFFIX = 10
# Тема и отправитель письма в выводе — не длиннее этого числа символов.
MAX_DISPLAY_CHARS = 200


def strip_controls(text: str | None) -> str:
    """Без управляющих, bidi- и прочих служебных символов Unicode (категории C*): ESC-последовательности
    управляют терминалом, \\n подделывает строки вывода, U+202E «переворачивает» расширение файла.
    Табуляция и переводы строк заменяются пробелом."""
    return "".join(
        " " if char in "\t\r\n" else char
        for char in text or ""
        if char in "\t\r\n" or not unicodedata.category(char).startswith("C")
    )


def display_text(text: str | None, limit: int = MAX_DISPLAY_CHARS) -> str:
    """Недоверенная строка (тема, отправитель) для вывода: одна строка без служебных символов, не длиннее limit."""
    cleaned = " ".join(strip_controls(text).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def safe_name(name: str | None, fallback: str) -> str:
    """Имя файла из недоверенного источника: без каталогов, служебных символов и точек по краям; основа
    обрезается до MAX_NAME_STEM символов / MAX_NAME_STEM_BYTES байт, расширение сохраняется."""
    name = Path(strip_controls(str(name or ""))).name.strip().strip(".")
    suffix = Path(name).suffix
    if len(suffix) > MAX_NAME_SUFFIX:
        suffix = ""
    stem = name[: len(name) - len(suffix)][:MAX_NAME_STEM]
    stem = stem.encode("utf-8")[:MAX_NAME_STEM_BYTES].decode("utf-8", "ignore").rstrip(" .")
    return f"{stem}{suffix}" if stem else fallback


def describe_bill(bill: Any) -> str:
    check = "сходится" if bill.checksum_ok else "НЕ сходится"
    return (
        f"счёт {bp.mask_account(bill.account)}, {bill.period} (услуги за {bill.service_period or '?'}), "
        f"итого {bill.total}, строк {len(bill.items)}, сверка с «Итого» {check}, проверка строк "
        f"{bill.rows_ok} ок/{bill.rows_failed} нет, тарифов {bill.tariff_checks_ok} ок/{bill.tariff_checks_failed} нет"
    )


# Неожиданные сбои разбора одного PDF (ошибка парсера на необычном растре, нехватка памяти): относятся
# только к этому файлу — пишутся в лог, запуск продолжается.
PARSE_CRASH_ERRORS: tuple[type[BaseException], ...] = (ValueError, TypeError, LookupError, ArithmeticError, MemoryError)


def _parse(ctx: RunContext, path: Path, file_name: str) -> tuple[Any | None, str | None]:
    """Разбор PDF: (квитанция, None) или (None, текст ошибки); ошибка уже записана в лог."""
    try:
        return bp.parse_bill(path, ctx.config["ocr"], pdf_name=file_name), None
    except bp.BillParseError as error:
        ctx.say(f"{bp.mask_digits(file_name)}: не разобран: {error}", event="parse_failed", level=logging.ERROR, detail=bp.mask_digits(str(path)))
        return None, str(error)
    except PARSE_CRASH_ERRORS as error:
        reason = f"сбой разбора ({type(error).__name__}): {error}"
        ctx.say(f"{bp.mask_digits(file_name)}: {reason}", event="parse_crashed", level=logging.ERROR, detail=bp.mask_digits(str(path)))
        return None, reason


def process_pdf(ctx: RunContext, data: bytes, file_name: str, meta: dict[str, Any]) -> bool:
    """Один PDF: дубликат → пропуск; иначе сохранение, разбор, проверки, запись. Возвращает True при успехе.

    Если что-то не сошлось (предупреждения разбора), квитанция пишется с флагом bills.needs_review = 1,
    в лог — событие bill_needs_review. PDF хранится только в БД (bills.pdf): на время разбора он пишется во
    временный каталог. Неразобранный PDF в БД не попадает — ошибка в логе (parse_failed), письмо не
    помечается обработанным и разбирается снова в следующий запуск.
    Ошибки разбора, диска и БД относятся только к этому PDF: запуск продолжается.
    """
    sha = hashlib.sha256(data).hexdigest()
    say, stats = ctx.say, ctx.stats
    shown = bp.mask_digits(file_name)  # имя файла содержит лицевой счёт: в лог — только маска
    stats["pdfs"] += 1
    existing = ctx.db.bill_by_sha(sha)
    if existing is not None and not ctx.force:
        stats["bills_unchanged"] += 1
        say(f"{shown}: уже в базе (счёт {bp.mask_account(existing['account'])}, {existing['period']}) — пропуск", event="pdf_known")
        return True
    meta = {**meta, "file_name": file_name, "file_sha256": sha, "file_size": len(data), "pdf": data}
    # Каталог создаётся с правами 700 (tempfile), файл — 600 (umask 077 обёртки и main).
    with tempfile.TemporaryDirectory(prefix="utility_bills-") as tmp:
        path = Path(tmp) / file_name
        path.write_bytes(data)
        bill, _error = _parse(ctx, path, file_name)
    if bill is None:
        return False
    for note in bill.notes:
        say(f"{shown}: {note}", event="parse_note")
    for warning in bill.warnings:
        say(f"{shown}: {warning}", event="parse_warning", level=logging.WARNING)
    if bill.warnings:
        say(
            f"{shown}: требует сверки ({len(bill.warnings)} предупр.), флаг bills.needs_review",
            event="bill_needs_review",
            level=logging.WARNING,
        )
    if ctx.dry_run:
        old = ctx.db.bill_by_period(bill.account, bill.period)
        result = "replaced" if old is not None else "new"
        say.plan(f"{'заменить' if old is not None else 'записать'} квитанцию: {describe_bill(bill)}", "save_bill")
        for item in bill.items:
            say(f"    {item.position}.{item.sub_row} {item.service}: начислено {_amount(item.accrued)}, к оплате {_amount(item.to_pay)}")
    else:
        result = ctx.db.save_bill(bill, meta)
    stats["bills_replaced" if result == "replaced" else "bills_new"] += 1
    if bill.warnings:
        stats["bills_review"] += 1
    if ctx.dry_run:
        return True
    month = bill.service_period or bill.period
    for change in ctx.db.tariff_changes(month):
        tier = f", {change['tier_name']}" if change["tier_name"] else ""
        unit = f" {change['unit']}" if change["unit"] else ""
        say(
            f"Тариф изменился: {change['service']}{tier}: {change['old_value']:.2f} → {change['value']:.2f}{unit} "
            f"с {month} (прежний — по {change['old_to']})",
            event="tariff_changed",
        )
    for change in ctx.db.quantity_changes(month):
        kind = "" if change["kind"] == "quantity" else " (второе число)"
        unit = f" {change['unit']}" if change["unit"] else ""
        say(
            f"Количество изменилось: {change['service']}{kind}: {change['old_value']:g} → {change['value']:g}{unit} "
            f"с {month} (прежнее — по {change['old_to']})",
            event="quantity_changed",
        )
    verb = "заменена" if result == "replaced" else "записана"
    say(f"Квитанция {verb}: {describe_bill(bill)}", event=f"bill_{result}", detail=bp.mask_digits(str(path)))
    return True


def process_pdf_isolated(ctx: RunContext, data: bytes, file_name: str, meta: dict[str, Any]) -> bool:
    """process_pdf, сбой которого не выходит за пределы этого PDF: ошибка БД (например, «database is
    locked» дольше тайм-аута), диска или непредвиденная ошибка пишется в лог, возвращается False —
    письмо не помечается обработанным, остальные письма и файлы обрабатываются."""
    try:
        return process_pdf(ctx, data, file_name, meta)
    except (sqlite3.Error, OSError, *PARSE_CRASH_ERRORS) as error:
        ctx.say(f"{bp.mask_digits(file_name)}: не обработан ({type(error).__name__}): {error}", event="pdf_failed", level=logging.ERROR)
        return False


def _amount(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


# ---------------------------------------------------------------------------
# Почта
# ---------------------------------------------------------------------------


def imap_utf7(name: str) -> str:
    """Имя папки в modified UTF-7 (RFC 3501), например «Квитанции» → «&BBoEMgQ4BEIEMAQ9BEYEOAQ4-»."""
    result = []
    buffer = []

    def flush() -> None:
        if buffer:
            encoded = b64encode("".join(buffer).encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")
            result.append(f"&{encoded}-")
            buffer.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            buffer.append(char)
    flush()
    return "".join(result)


def imap_quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _header_message(data: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(data, policy=email.policy.default)


def _iso_date(value: str | None) -> str | None:
    with suppress(TypeError, ValueError, IndexError):
        return parsedate_to_datetime(value).isoformat()
    return value or None


def is_ip_address(host: str) -> bool:
    """IPv4 или IPv6 (IPv6 — без квадратных скобок, их запрещает _check_host)."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def tls_context(host: str) -> ssl.SSLContext:
    """TLS-контекст для imaplib (явный обязателен: без него imaplib берёт ssl._create_stdlib_context(),
    который сертификат не проверяет).

    host — имя: полная проверка цепочки и имени (ssl.create_default_context()).
    host — частный IP-адрес: шифрование без проверки сертификата (решение пользователя 2026-11-11;
    сертификат выдан на имя, а не на адрес). Подменивший адрес узел сможет получить пароль, поэтому IP
    допустим только частный — адрес почтового сервера в частной сети (VPN), где подлинность сервера
    обеспечивает сам туннель. Публичный IP отклоняет load_config (_check_host).
    """
    context = ssl.create_default_context()
    if is_ip_address(host):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def connect_imap(config: Config, warn: Warn | None = None) -> imaplib.IMAP4:
    """Соединение с IMAP и вход. security = none допустим только для localhost (validate_imap)."""
    imap = config["imap"]
    timeout = imap["timeout_seconds"]
    context = tls_context(imap["host"])
    if warn is not None and imap["security"] != "none" and context.verify_mode == ssl.CERT_NONE:
        warn(
            f"imap.host {imap['host']} — IP-адрес: сертификат сервера не проверяется (только шифрование); "
            "допустимо только для почтового сервера в частной сети (VPN)",
            event="imap_tls_unverified",
            level=logging.WARNING,
        )
    if imap["security"] == "ssl":
        conn = imaplib.IMAP4_SSL(imap["host"], imap["port"], ssl_context=context, timeout=timeout)
    else:
        conn = imaplib.IMAP4(imap["host"], imap["port"], timeout=timeout)
    try:
        if imap["security"] == "starttls":
            conn.starttls(ssl_context=context)
        conn.login(imap["username"], imap_password(config))
    except BaseException:
        # Соединение уже открыто, а вызывающий его не получит: закрыть здесь, иначе сокет висит до сборки мусора.
        with suppress(OSError):
            conn.shutdown()
        raise
    return conn


def _fetch_parts(response: list[Any]) -> list[tuple[bytes, bytes]]:
    """Части ответа FETCH: [(служебная строка ответа, данные литерала)].

    imaplib возвращает литерал кортежем (строка до литерала, данные), а то, что сервер прислал после
    литерала (например, «UID 42)»), — отдельным элементом bytes; он приклеивается к служебной строке,
    чтобы UID находился независимо от порядка атрибутов (RFC 3501 его не фиксирует).
    """
    parts: list[list[bytes]] = []
    for item in response:
        if isinstance(item, tuple):
            parts.append([item[0], item[1]])
        elif isinstance(item, bytes) and parts:
            parts[-1][0] += b" " + item
    return [(head, body) for head, body in parts]


_UID_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
# Текст письма в БД — не больше этого числа символов (письма ЕРЦ короткие; защита от огромных писем).
MAX_BODY_CHARS = 100_000
PDF_SIGNATURE = b"%PDF-"


def _uidvalidity(conn: imaplib.IMAP4) -> int:
    """UIDVALIDITY папки из ответа SELECT; нет его или он не число — 0 (как и раньше при отсутствии).

    Нужен только для ключа письма без Message-ID; непонятный ответ сервера не должен ронять запуск.
    """
    raw = conn.untagged_responses.get("UIDVALIDITY", [b"0"])[0]
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


def message_size_limit(max_bytes: int) -> int:
    """Наибольший размер письма (RFC822.SIZE), которое стоит скачивать: вложение до max_bytes в base64
    растёт в 4/3 раза, плюс 1 МиБ на заголовки, текст и прочие части."""
    return max_bytes * 4 // 3 + MIB


def attachment_limit(config: Config) -> int:
    """search.max_attachment_mb в байтах."""
    return int(config["search"]["max_attachment_mb"] * MIB)


def find_messages(
    conn: imaplib.IMAP4, config: Config, warn: Warn | None = None, known: frozenset[str] | set[str] = frozenset()
) -> list[dict[str, Any]]:
    """Полный список писем папки от отправителей из search.from (за всё время), по дате: [{key, uid, …}].

    Только заголовки, ящик открыт на чтение. Отбор — на нашей стороне по заголовку From (sender_matches),
    а не серверным UID SEARCH FROM: почтовый сервер в частной сети (VPN) возвращал по FROM 0 писем при
    12 подходящих (проверено 2026-11-11), и программа молча пропустила бы все квитанции. Заголовки всех
    писем папки запрашиваются пачками по 100 — это дёшево. Письмо с несколькими заголовками From не
    отбирается: неясно, какому отправителю верить. Подлинность (require_auth) проверяется только у писем,
    ключа которых нет в known (уже обработанные): старые письма из прежнего почтового сервера могут быть
    без Authentication-Results, и без этого каждый запуск предупреждал бы о них заново.
    """
    folder = config["imap"]["folder"]
    status, data = conn.select(imap_quote(imap_utf7(folder)), readonly=True)
    if status != "OK":
        raise imaplib.IMAP4.error(f"папка {folder!r} не открыта: {data!r}")
    uidvalidity = _uidvalidity(conn)
    status, data = conn.uid("SEARCH", None, "ALL")
    if status != "OK":
        raise imaplib.IMAP4.error(f"поиск писем не выполнен: {data!r}")
    uids = data[0].split() if data and data[0] else []
    found = []
    for start in range(0, len(uids), 100):
        chunk = b",".join(uids[start : start + 100]).decode()
        status, response = conn.uid("FETCH", chunk, "(UID RFC822.SIZE BODY.PEEK[HEADER])")
        if status != "OK":
            raise imaplib.IMAP4.error(f"заголовки писем не получены: {response!r}")
        for head, body in _fetch_parts(response):
            # UID ищется по имени: сервер может поставить его и до, и после BODY[...] (RFC 3501).
            uid_match = _UID_RE.search(head)
            if uid_match is None:
                continue
            uid = int(uid_match.group(1))
            message = _header_message(body)
            subject = str(message.get("Subject", ""))
            # Несколько заголовков From склеиваются: sender_matches увидит несколько адресов и откажет.
            sender = ", ".join(str(header) for header in message.get_all("From") or [])
            if not sender_matches(sender, config["search"]["from"]):
                continue
            message_id = str(message.get("Message-ID", "")).strip() or None
            key = message_id or f"uid:{folder}:{uidvalidity}:{uid}"
            if config["search"]["require_auth"] and key not in known:
                from_domain = _domain_of(getaddresses([sender])[0][1])
                results = [str(header) for header in message.get_all("Authentication-Results") or []]
                if not sender_authenticated(results, config["search"]["authserv_id"], from_domain):
                    if warn is not None:
                        warn(
                            f"Письмо UID {uid} (домен {from_domain}): нет подтверждения DKIM/SPF от {config['search']['authserv_id']} — пропуск",
                            event="message_auth_failed",
                            level=logging.WARNING,
                        )
                    continue
            size_match = _SIZE_RE.search(head)
            found.append(
                {
                    "key": key,
                    "message_id": message_id,
                    "folder": folder,
                    "uidvalidity": uidvalidity,
                    "uid": uid,
                    "subject": subject,
                    "sender": sender,
                    "recipients": ", ".join(str(message.get(name, "")) for name in ("To", "Cc") if message.get(name)) or None,
                    "date": _iso_date(message.get("Date")),
                    "size": int(size_match.group(1)) if size_match else None,
                    "headers": body.decode("utf-8", "replace"),
                }
            )
    found.sort(key=lambda message: (message["date"] or "", message["uid"]))
    return found


@dataclass
class FetchedMessage:
    """Содержимое письма: PDF для разбора, метаданные всех вложений и текст письма."""

    pdfs: list[tuple[str, bytes]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    body_text: str | None = None


def fetch_message(conn: imaplib.IMAP4, uid: int, glob: str, max_bytes: int, warn: Warn) -> FetchedMessage:
    """Письмо целиком (BODY.PEEK — без пометки прочитанным): PDF-вложения, сведения о всех вложениях
    (имя, тип, размер, SHA-256, взято ли в разбор) и текст письма. PDF больше max_bytes пропускается."""
    status, response = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK":
        raise imaplib.IMAP4.error(f"письмо UID {uid} не получено: {response!r}")
    parts = _fetch_parts(response)
    result = FetchedMessage()
    if not parts:
        return result
    message = _header_message(parts[0][1])
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        with suppress(LookupError, ValueError):
            result.body_text = str(body.get_content())[:MAX_BODY_CHARS]
    for index, part in enumerate(message.walk(), start=1):
        if part.is_multipart() or part is body:
            continue
        name = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        if not name and content_type.startswith("text/"):
            continue  # альтернативная часть текста письма, а не вложение
        file_name = safe_name(name, f"uid{uid}-{index}")
        is_pdf = content_type == "application/pdf" or bool(name and fnmatch.fnmatch(name.casefold(), glob.casefold()))
        is_pdf = is_pdf and payload[:5] == PDF_SIGNATURE  # тип и имя заявлены отправителем: верим только содержимому
        taken = is_pdf and 0 < len(payload) <= max_bytes
        if is_pdf and len(payload) > max_bytes:
            warn(
                f"{bp.mask_digits(file_name)}: вложение {len(payload) / MIB:.1f} МиБ больше лимита {max_bytes / MIB:.0f} МиБ — пропуск",
                event="attachment_too_large",
                level=logging.WARNING,
            )
        result.attachments.append(
            {
                "name": file_name,
                "content_type": content_type,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "parsed": taken,
            }
        )
        if taken:
            result.pdfs.append((file_name if name else f"{file_name}.pdf", payload))
    return result


def run_mail(ctx: RunContext) -> None:
    """Сначала полный список писем от отправителей из search.from (за всё время, с пометкой «новое» /
    «обработано»), затем обработка только новых — по порядку дат. Число дней не задаётся: уже
    обработанные письма отсекаются таблицей messages, а не периодом поиска."""
    config, say, stats = ctx.config, ctx.say, ctx.stats
    validate_imap(config)
    imap = config["imap"]
    max_bytes = attachment_limit(config)
    say(f"Подключение к {imap['host']}:{imap['port']}, папка «{imap['folder']}»", event="imap_connect")
    try:
        conn = connect_imap(config, say)
    except (OSError, imaplib.IMAP4.error) as error:
        # ssl.SSLCertVerificationError — подкласс OSError: подмена сервера по имени тоже попадает сюда.
        say(f"Не удалось подключиться к почте: {error}", event="imap_failed", level=logging.ERROR)
        return
    say(f"Подключено к {imap['host']}", event="imap_connected")
    try:
        # Шаг 1: список писем.
        known = ctx.db.processed_keys()
        messages = find_messages(conn, config, say, known)
        new = [message for message in messages if message["key"] not in known]
        stats["messages_found"] = len(messages)
        stats["messages_skipped"] = len(messages) - len(new)
        say(
            f"Список писем от {', '.join(sorted(config['search']['from']))}: всего {len(messages)}, "
            f"уже обработаны {stats['messages_skipped']}, новых {len(new)}",
            event="imap_list",
        )
        for number, message in enumerate(messages, start=1):
            mark = "новое" if message["key"] not in known else "обработано"
            date = (message["date"] or "?")[:10]
            say(
                f"  {number:>3}. {date}  {display_text(message['sender'])}  «{display_text(message['subject'])}»  [{mark}]",
                event="imap_list_item",
            )
        # Шаг 2: обработка новых писем.
        for number, message in enumerate(new, start=1):
            _process_message(ctx, conn, message, max_bytes, f"[{number}/{len(new)}]")
    except (OSError, imaplib.IMAP4.error, sqlite3.Error, ValueError) as error:
        # Сбой списка писем или чтения базы: итог запуска всё равно записывается (_run), ошибка — в лог.
        say(f"Ошибка обработки почты ({type(error).__name__}): {error}", event="imap_failed", level=logging.ERROR)
    finally:
        with suppress(OSError, imaplib.IMAP4.error):
            conn.logout()


def _mark_message(ctx: RunContext, message: dict[str, Any], status: str, content: FetchedMessage) -> None:
    """Пометка письма обработанным (status: processed | no_pdf | too_large); в dry-run — только план."""
    if ctx.dry_run:
        ctx.say.plan(f"пометить письмо обработанным со статусом {status} ({display_text(message['key'])})", "mark_message")
        return
    try:
        ctx.db.mark_message(message, status, content)
    except sqlite3.Error as error:
        ctx.say(f"Письмо не помечено обработанным (ошибка БД): {error}", event="db_failed", level=logging.ERROR)


def _process_message(ctx: RunContext, conn: imaplib.IMAP4, message: dict[str, Any], max_bytes: int, progress: str = "") -> None:
    """Одно письмо: вложения → квитанции; при успехе всех PDF письмо помечается обработанным.

    Письмо больше message_size_limit не скачивается (размер известен из RFC822.SIZE заголовков): иначе
    огромное письмо целиком попало бы в память ради вложения, которое всё равно больше лимита. Оно
    помечается статусом too_large и больше не запрашивается.
    """
    say = ctx.say
    subject, sender = display_text(message["subject"]), display_text(message["sender"])
    say(f"{progress} Письмо «{subject}» от {sender}, {message['date']}".lstrip(), event="message")
    size, limit = message.get("size"), message_size_limit(max_bytes)
    if size is None:
        # Без RFC822.SIZE лимит не проверить: письмо не скачивается (fail closed) и повторится в следующий запуск.
        say(
            f"Сервер не вернул RFC822.SIZE письма UID {message['uid']} — пропуск без скачивания",
            event="message_size_unknown",
            level=logging.WARNING,
        )
        return
    if size > limit:
        say(
            f"Письмо {size / MIB:.1f} МиБ больше лимита {limit / MIB:.1f} МиБ (search.max_attachment_mb с запасом "
            "на base64) — пропуск без скачивания",
            event="message_too_large",
            level=logging.WARNING,
        )
        _mark_message(ctx, message, "too_large", FetchedMessage())
        return
    try:
        content = fetch_message(conn, message["uid"], ctx.config["search"]["attachment_glob"], max_bytes, say)
    except (OSError, imaplib.IMAP4.error, email.errors.MessageError, ValueError, LookupError) as error:
        say(f"Письмо не получено ({type(error).__name__}): {error}", event="message_failed", level=logging.ERROR)
        return
    pdfs = content.pdfs
    if not pdfs:
        say("В письме нет PDF-вложений", event="message_no_pdf", level=logging.WARNING)
    meta = {
        "message_key": message["key"],
        "email_subject": message["subject"],
        "email_from": message["sender"],
        "email_date": message["date"],
    }
    # Список, а не генератор: каждый PDF обрабатывается, даже если предыдущий не удался.
    results = [process_pdf_isolated(ctx, data, name, meta) for name, data in pdfs]
    if not all(results):
        say("Письмо не помечено обработанным: будет повторено при следующем запуске", event="message_retry")
        return
    ctx.stats["messages_processed"] += 1
    _mark_message(ctx, message, "processed" if pdfs else "no_pdf", content)


def run_files(ctx: RunContext, paths: Iterable[str]) -> None:
    max_bytes = attachment_limit(ctx.config)
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            ctx.say(f"Файл не найден: {bp.mask_digits(str(path))}", event="file_missing", level=logging.ERROR)
            continue
        try:
            if path.stat().st_size > max_bytes:
                ctx.say(f"{bp.mask_digits(str(path))}: файл больше лимита search.max_attachment_mb — пропуск", event="file_too_large", level=logging.ERROR)
                continue
            data = path.read_bytes()
        except OSError as error:
            ctx.say(f"{bp.mask_digits(str(path))}: не прочитан: {error}", event="file_failed", level=logging.ERROR)
            continue
        process_pdf_isolated(ctx, data, safe_name(path.name, "bill.pdf"), {"email_subject": None})


def run_reparse(ctx: RunContext) -> None:
    """Повторный разбор PDF из БД текущей версией парсера (после его улучшения)."""
    bills = ctx.db.all_bills()
    ctx.say(f"Квитанций в базе: {len(bills)}; разбор заново парсером v{bp.PARSER_VERSION}", event="reparse_start")
    ctx.force = True
    for row in bills:
        if row["pdf"] is None:
            ctx.say(f"{row['period']}: нет PDF в БД — пропуск", event="reparse_missing", level=logging.WARNING)
            continue
        meta = {key: row[key] for key in ("message_key", "email_subject", "email_from", "email_date")}
        process_pdf_isolated(ctx, row["pdf"], row["file_name"] or f"{row['period']}.pdf", meta)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Квитанции ЖКУ из почты → разбор PDF → SQLite (utility_bills)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="ничего не менять, показать план")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"файл настроек (по умолчанию {CONFIG_PATH.name})")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--file", nargs="+", metavar="PDF", help="импортировать указанные PDF вместо почты")
    modes.add_argument("--reparse", action="store_true", help="разобрать заново все PDF из базы")
    modes.add_argument("--check", action="store_true", help="проверить конфигурацию и программы и выйти")
    modes.add_argument("--migrate-db", action="store_true", help="только миграции схемы БД")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Права создаваемых файлов (журналы и прочее) — через umask процесса; прежняя маска возвращается,
    # чтобы вызов main из тестов не менял её процессу.
    previous_umask = os.umask(PRIVATE_UMASK)
    try:
        setup_logging()
        try:
            return _run(args)
        finally:
            cleanup_logs()
            shutdown_logging()
    finally:
        os.umask(previous_umask)


def _run(args: argparse.Namespace) -> int:
    say = Reporter()
    started = time.time()
    mode = "check" if args.check else "migrate" if args.migrate_db else "reparse" if args.reparse else "files" if args.file else "mail"
    say(f"Запуск utility_bills: режим {mode}{' (dry-run)' if args.dry_run else ''}", event="start")
    try:
        config = load_config(args.config, say)
    except ConfigError as error:
        say(f"Конфигурация: {error}", event="config_error", level=logging.ERROR)
        return 2

    def migrate_out(line: str) -> None:
        say(line, event="migrate")

    if args.migrate_db:
        try:
            migrate_database(config["db_path"], dry_run=args.dry_run, out=migrate_out)
        except MigrationError as error:
            say(f"Миграция БД не выполнена: {error}", event="migrate_failed", level=logging.ERROR)
            return 1
        return 0

    problems = bp.check_tools(config["ocr"])
    for problem in problems:
        say(problem, event="tool_missing", level=logging.ERROR)
    if args.check:
        try:
            validate_imap(config)
            say("Настройки почты заполнены", event="check_imap")
        except ConfigError as error:
            say(f"Почта: {error}", event="check_imap", level=logging.WARNING)
        say("Проверка завершена" if not problems else "Проверка: есть проблемы", event="check_done")
        return 2 if problems else 0
    if problems:
        return 2

    try:
        migrate_database(config["db_path"], dry_run=args.dry_run, out=migrate_out)
    except MigrationError as error:
        say(f"Миграция БД не выполнена: {error}", event="migrate_failed", level=logging.ERROR)
        return 1
    with Database(config["db_path"], read_only=args.dry_run) as db:
        ctx = RunContext(config=config, db=db, say=say, dry_run=args.dry_run)
        exit_code = 0
        try:
            if args.reparse:
                run_reparse(ctx)
            elif args.file:
                run_files(ctx, args.file)
            else:
                run_mail(ctx)
        except ConfigError as error:
            say(f"Конфигурация: {error}", event="config_error", level=logging.ERROR)
            exit_code = 2
        exit_code = exit_code or (1 if say.errors else 0)
        stats = ctx.stats
        finished = time.time()
        say(
            f"Итог: писем найдено {stats['messages_found']}, пропущено как обработанные {stats['messages_skipped']}, "
            f"обработано {stats['messages_processed']}; PDF {stats['pdfs']}: новых квитанций {stats['bills_new']}, "
            f"заменено {stats['bills_replaced']}, без изменений {stats['bills_unchanged']}, "
            f"на сверку {stats['bills_review']}; "
            f"предупреждений {say.warnings}, ошибок {say.errors}",
            event="done",
            detail=f"rc={exit_code}",
        )
        if not args.dry_run:
            run = {
                **stats,
                "started": datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds"),
                "finished": datetime.fromtimestamp(finished).astimezone().isoformat(timespec="seconds"),
                "seconds": round(finished - started, 1),
                "exit_code": exit_code,
                "mode": mode,
                "warnings": say.warnings,
                "errors": say.errors,
            }
            try:
                db.save_run(run)
            except sqlite3.Error as error:
                say(f"Итог запуска не записан в БД: {error}", event="db_failed", level=logging.WARNING)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
