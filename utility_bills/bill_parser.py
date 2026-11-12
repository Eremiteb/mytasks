"""Разбор квитанции «Единый платёжный документ» Астана ЕРЦ (PDF со сканом без текстового слоя).

Порядок: растр страницы извлекается `pdfimages` (poppler) → в растре находятся линии таблицы →
каждая ячейка распознаётся `tesseract` отдельно (числовые колонки — только цифрами), ячейки с
внутренней горизонтальной линией делятся на подстроки (у электроэнергии — «потери») → шапка и низ
листа распознаются блоками и разбираются регулярными выражениями.

Контроль качества: каждая строка проверяется правилом
«к оплате = max(сальдо − оплата, 0) + начислено + пеня + перерасчёт», вся квитанция — суммой колонки
«К оплате» против «Итого». Расхождение (ошибка распознавания или самой квитанции) попадает в предупреждения.

Модуль не пишет ни в БД, ни в каталоги данных: временные файлы — только во временном каталоге.
"""

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# Настройки распознавания (DEFAULT_SETTINGS с перекрытием из конфигурации).
Settings = dict[str, Any]
# Функция распознавания: (изображение, настройки, numeric=…) → текст; в тестах подменяется.
OcrFunc = Callable[..., str]
# Ячейка таблицы: (первая колонка, последняя колонка, x0, x1).
Cell = tuple[int, int, int, int]

# Версия алгоритма разбора: пишется в БД, чтобы после улучшений перепарсить старые квитанции (--reparse).
PARSER_VERSION = 3

# Колонки таблицы услуг слева направо (12 колонок между 13 вертикальными линиями строки заголовка).
COLUMNS = (
    "service",  # Наименование услуг
    "saldo_start",  # Сальдо на начало месяца
    "paid",  # Оплата
    "prev_reading",  # Предыдущее показание
    "curr_reading",  # Текущее показание
    "quantity",  # Кол-во
    "tariff",  # Стоимость (тариф)
    "accrued",  # Начислено
    "penalty",  # Пеня
    "recalculation",  # Перерасчёт
    "to_pay",  # К оплате
    "paying",  # Оплачиваю (в электронной квитанции пусто)
)
TEXT_COLUMNS = frozenset({"service", "tariff"})
NUMERIC_COLUMNS = frozenset(COLUMNS) - TEXT_COLUMNS
MONEY_COLUMNS = ("saldo_start", "paid", "accrued", "penalty", "recalculation", "to_pay", "paying")

# Канонические названия услуг: первое подстрочное ключевое слово (casefold) в ячейке «Наименование».
# Порядок важен: строка ОДН тоже содержит «ЭЭ», поэтому проверяется раньше электроэнергии.
SERVICE_NAMES = (
    ("одн", "Электроэнергия на ОДН"),
    ("электроэнерг", "Электроэнергия"),
    ("ээ по пу", "Электроэнергия"),  # формат 2023–2025: «ЕК бойынша ЭЭ / ЭЭ по ПУ»
    ("эксп", "Эксплуатационные расходы КСК"),
    ("водоснаб", "Водоснабжение"),
    ("водоотвед", "Водоотведение"),
    ("газ", "Газ"),
    ("домофон", "Домофон"),
    ("накопит", "Накопительный взнос"),
    ("мусор", "Вывоз мусора"),
    # «Жылыту (отоп) Жылу энергиясы/ Тепловая энергия "Астана-Теплотранзит" … ЫСЖ (ГВС)»: отопление и ГВС.
    ("отоп", "Отопление"),
    ("теплов", "Отопление"),
    ("целев", "Целевые сборы КСК"),
    ("гвс", "Горячая вода (ГВС)"),  # формат 2023–2025: «ЫСЖ/ГВС» отдельной строкой (после «отоп»/«теплов»)
    ("горяч", "Горячая вода"),
    ("лифт", "Лифт"),
)

MONTHS_NOMINATIVE = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
MONTHS_PREPOSITIONAL = (
    "январе", "феврале", "марте", "апреле", "мае", "июне",
    "июле", "августе", "сентябре", "октябре", "ноябре", "декабре",
)

# Настройки распознавания по умолчанию; переопределяются блоком "ocr" конфигурации.
DEFAULT_SETTINGS = {
    "tesseract": "tesseract",
    "pdfimages": "pdfimages",
    # Языки текста (казахский, русский и латиница в названиях поставщиков).
    "languages": "rus+kaz+eng",
    "timeout_seconds": 60,
    # Порог яркости: пиксель темнее считается чернилами (0–255).
    "dark_threshold": 160,
    # Минимальная длина линии таблицы — доля ширины (горизонтальные) и высоты (вертикальные) листа.
    "hor_line_min": 0.025,
    "ver_line_min": 0.016,
    # Отступ внутрь ячейки от линий, пиксели исходного растра (текст бывает вплотную к линии).
    "cell_inset_px": 3,
    # На сколько пикселей вокруг найденных линий стирать серые края (сглаживание линий).
    "line_erase_px": 1,
    # Белая рамка вокруг ячейки перед распознаванием, пиксели исходного растра.
    "cell_padding_px": 12,
    # Варианты стирания линий для тарифной ячейки (см. parse_table), пиксели.
    "tariff_line_erase_px": [1, 2],
    # Масштабы ячейки для числовых колонок: каждая читается на всех, значение выбирается голосованием
    # (первый масштаб — при равенстве голосов и для текстовых ячеек). На одном масштабе 1–2 строки
    # квитанции получали ошибку в цифре (3↔2/5/6/9), причём на разных масштабах — в разных ячейках.
    "cell_scales": [2, 1, 3],
    # Параллельных процессов tesseract (каждый в один поток).
    "workers": 8,
    # Ячейка с меньшим числом тёмных пикселей считается пустой и не распознаётся.
    "empty_cell_pixels": 25,
    # Допустимое расхождение сумм при проверках строки и «Итого», тенге.
    "checksum_tolerance": 0.01,
    # Ступенчатый тариф (электроэнергия Астана-РЭК: три цены в поле «Стоимость»): верхние границы
    # ступеней, кВт·ч в месяц — минимальный до 70, средний от 70 до 140, максимальный от 140.
    "tier_bounds": [70, 140],
    # Границы ступеней умножаются на число жильцов из шапки квитанции (true) или едины на квартиру (false).
    # По квитанциям 2026 г. ЕРЦ считает на жильца: объём до 70 кВт·ч × число жильцов — весь по
    # минимальному тарифу (решение пользователя 2026-11-11).
    "tier_bounds_per_resident": True,
    # Число жильцов, если в шапке поле «Кол. человек» пустое (в части квитанций 2026 г. оно пустое).
    "tier_residents_default": 2,
    # Лимиты для недоверенного PDF (вложение письма): растр страницы A4 даже при 600 dpi — около 35 Мпикс
    # и одно-два изображения, поэтому лимиты пропускают квитанции с запасом, а «бомбу декомпрессии» или
    # тысячи картинок отклоняют до того, как pdfimages развернёт их на диск и Pillow/numpy — в память.
    "max_page_pixels": 60_000_000,
    "max_page_images": 20,
    # В таблице квитанции около 12 строк и до 4 подъячеек в ячейке: больше — не квитанция, а растр,
    # который дал бы тысячи вызовов tesseract и взрыв сочетаний вариантов (_combine).
    "max_table_rows": 40,
    "max_cell_pieces": 8,
}

# Допустимые диапазоны числовых настроек (для списков — каждого элемента); проверяет _check_ocr движка.
SETTING_RANGES: dict[str, tuple[float, float]] = {
    "timeout_seconds": (1, 3600),
    "dark_threshold": (1, 254),
    "hor_line_min": (0.001, 0.9),
    "ver_line_min": (0.001, 0.9),
    "cell_inset_px": (0, 50),
    "line_erase_px": (0, 10),
    "cell_padding_px": (0, 100),
    "tariff_line_erase_px": (1, 10),
    "cell_scales": (0.25, 6),
    "workers": (1, 64),
    "empty_cell_pixels": (0, 10_000),
    "checksum_tolerance": (0, 100),
    "tier_bounds": (1, 100_000),
    "tier_residents_default": (1, 50),
    # Сверху — порог DecompressionBombError Pillow (2 × MAX_IMAGE_PIXELS по умолчанию).
    "max_page_pixels": (1_000_000, 178_000_000),
    "max_page_images": (1, 1000),
    "max_table_rows": (3, 500),
    "max_cell_pieces": (1, 64),
}

# Сочетания вариантов подъячеек (_combine): не больше вариантов на подъячейку (столько прочтений даёт
# тарифная ячейка при настройках по умолчанию: 3 масштаба × 2 ширины стирания — результат не меняется)
# и не больше сочетаний в итоге и проверенных сочетаний — без перебора всего произведения.
MAX_PIECE_VARIANTS = 6
MAX_CELL_VARIANTS = 64
MAX_COMBOS_CHECKED = 4096
# Ремонт строки (_repair_row): не больше альтернатив на денежную колонку (у денежной ячейки 3 прочтения).
MAX_REPAIR_OPTIONS = 4

TIER_NAMES = ("Минимальный", "Средний", "Максимальный")

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_MONEY_RE = re.compile(r"(?<![\d.,])\d+[.,]\d{2}(?![\d.,])")
_PRICE_RE = re.compile(r"\d+[.,]\d{2}")
_UNIT_RE = re.compile(r"тг\s*/\s*\S+|тг|м3|м2", re.IGNORECASE)
# Телефон: +7 или 8 и ещё 10 цифр с любой разбивкой («+7 700 0000 000», «8 700 000 00 00»).
_PHONE_RE = re.compile(r"(?:\+7|(?<!\d)8)(?:[\s\-()]*\d){10}(?!\d)")
_DATE_TIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}:\d{2}:\d{2})(?:\s+(\d{3,8})\b)?")


class BillParseError(RuntimeError):
    """Квитанцию не удалось разобрать (неожиданный формат или сбой OCR)."""


@dataclass
class TariffTier:
    tier: int
    price: float
    unit: str | None = None
    name: str | None = None  # применённая ступень с учётом жильцов: «Минимальный тариф (до 140 кВт·ч)»
    label: str | None = None  # ступень в справочнике тарифов, базовые границы: «Минимальный тариф (до 70 кВт·ч на жильца)»
    from_qty: float | None = None  # границы ступени с учётом жильцов
    to_qty: float | None = None  # None — без верхней границы
    quantity: float | None = None  # объём, пришедшийся на ступень
    amount: float | None = None  # объём × цена


@dataclass
class BillItem:
    position: int  # номер строки таблицы (1…); подстрока «потери» — та же позиция, sub_row = 1
    sub_row: int
    service: str
    service_raw: str
    provider: str | None = None
    provider_phone: str | None = None
    saldo_start: float | None = None
    paid: float | None = None
    prev_reading: float | None = None
    curr_reading: float | None = None
    consumption: float | None = None  # текущее − предыдущее показание
    quantity: float | None = None
    quantity_extra: float | None = None  # второе число подъячейки «Кол-во» (например, «17 | 2»)
    tariff: str | None = None
    tariff_value: float | None = None  # первая (или единственная) цена
    tariff_unit: str | None = None
    tariffs: list[TariffTier] = field(default_factory=list)
    accrued: float | None = None
    accrued_parts: list[float] = field(default_factory=list)  # все числа ячейки «Начислено»
    penalty: float | None = None
    recalculation: float | None = None
    to_pay: float | None = None
    paying: float | None = None
    expected_to_pay: float | None = None
    row_check_ok: bool | None = None
    # Проверка тарифа: начислено = кол-во × цена (по ступеням, если цен несколько).
    expected_accrued: float | None = None
    # Итог начислений по строке таблицы: у строки с подстроками — сумма подстрок (в подстроке 0).
    accrued_total: float | None = None
    tariff_check_ok: bool | None = None
    # Откуда объём и сумма ступеней: computed — по границам из настроек, printed — напечатаны в квитанции.
    tiers_source: str | None = None
    # Прочтения цены тарифной ячейки (по убыванию голосов): нужны только _repair_tariff, в БД не попадают.
    price_variants: list = field(default_factory=list)
    known_service: bool = True
    raw: dict = field(default_factory=dict)  # тексты OCR каждой ячейки


@dataclass
class Bill:
    account: str | None = None
    period: str | None = None  # месяц квитанции, ГГГГ-ММ
    service_period: str | None = None  # месяц оказания услуг, ГГГГ-ММ
    doc_number: str | None = None
    issued_at: str | None = None  # ГГГГ-ММ-ДДTЧЧ:ММ:СС
    due_date: str | None = None  # ГГГГ-ММ-ДД
    payer: str | None = None
    address: str | None = None
    address_kz: str | None = None
    residents: int | None = None
    total_area: float | None = None
    heated_area: float | None = None
    useful_area: float | None = None
    controller: str | None = None
    total: float | None = None
    items_sum: float = 0.0
    checksum_ok: bool = False
    rows_ok: int = 0
    rows_failed: int = 0
    tariff_checks_ok: int = 0
    tariff_checks_failed: int = 0
    tier_residents: int | None = None  # число жильцов, по которому считались границы ступеней
    header_text: str = ""
    footer_text: str = ""
    table_header_text: str = ""  # OCR строки заголовка таблицы («Сальдо на 31.05.2023 г.», «Начислено за 06.2023 г.»)
    saldo_date: str | None = None  # дата сальдо из заголовка таблицы, ГГГГ-ММ-ДД
    accrual_month: str | None = None  # месяц начислений из заголовка таблицы, ГГГГ-ММ
    parser_version: int = PARSER_VERSION
    items: list[BillItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # расхождения: квитанция уходит на сверку
    notes: list[str] = field(default_factory=list)  # примечания разбора, не требующие сверки
    # Формат квитанции: признаки раскладки (format_features) и хеш устойчивых из них (format_signature).
    format_features: dict = field(default_factory=dict)
    format_signature: str | None = None


# ---------------------------------------------------------------------------
# Числа и текст
# ---------------------------------------------------------------------------


def _to_float(text: str) -> float | None:
    try:
        return float(Decimal(text.replace(",", ".")))
    except InvalidOperation:
        return None


def numbers(text: str | None) -> list[float]:
    """Все числа строки по порядку; запятая — десятичный разделитель."""
    return [value for value in (_to_float(match) for match in _NUMBER_RE.findall(text or "")) if value is not None]


def money_numbers(text: str | None) -> list[float]:
    """Денежные суммы из OCR. В квитанциях все суммы — с копейками («77.46», «4572.00»), поэтому запись
    без точки и запятой из 3 и более цифр («7746») — потерянная точка: 77.46. Решение принимается по
    тексту, а не по числу: «4572.00» и «4572» после перевода в число неразличимы."""
    result = []
    for token in _NUMBER_RE.findall(text or ""):
        value = _to_float(token)
        if value is None:
            continue
        digits = token.lstrip("-")
        if "." not in token and "," not in token and len(digits) >= 3:
            value = round(value / 100, 2)
        result.append(value)
    return result


def tariff_prices(numbers_text: str | None) -> list[float]:
    """Цены тарифа из числового прохода OCR: ровно два знака после точки, хвост единицы («м2») отбрасывается."""
    return [_to_float(value) for value in _PRICE_RE.findall(numbers_text or "")]


def tariff_unit(words_text: str | None) -> str | None:
    """Единица тарифа из текстового прохода («тг/кВт*ч», «тг/м3», «м3», «тг»)."""
    match = _UNIT_RE.search(words_text or "")
    if not match:
        return None
    unit = match.group(0).replace(" ", "")
    # Звёздочка в «кВт*ч» читается как «%», «“», «**» — приводится к одному виду.
    return re.sub(r"(?i)(квт)\W*(ч)", r"\1*\2", unit)


def canonical_service(text: str | None) -> str | None:
    """Каноническое название услуги по ключевым словам; не найдено — None."""
    folded = (text or "").casefold().replace("ё", "е")
    for key, name in SERVICE_NAMES:
        if key in folded:
            return name
    return None


def clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def russian_part(text: str | None) -> str:
    """Из двуязычного «Қазақша/Русский» оставляет русскую часть (после последней «/»)."""
    text = clean_text(text)
    return text.rsplit("/", 1)[-1].strip() if "/" in text else text


def split_provider(text: str | None) -> tuple[str | None, str | None]:
    """Поставщик и телефон из объединённой ячейки «ТОО Пример 8 700 000 00 00»."""
    text = clean_text(text)
    match = _PHONE_RE.search(text)
    if not match:
        return text or None, None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    name = clean_text(text[: match.start()] + " " + text[match.end() :])
    return name or None, phone


def to_pay_candidates(saldo: float | None, paid: float | None, accrued: float | None, penalty: float | None,
                      recalculation: float | None) -> list[float] | None:
    """Возможные суммы «К оплате» по двум способам учёта оплаты, встречающимся в квитанциях ЕРЦ:

    1) оплата гасит только долг на начало месяца: max(сальдо − оплата, 0) + начислено + пеня + перерасчёт
       (так в 2026 г., например КСК: оплата 6096 при пустом сальдо, к оплате — всё начисление 6096);
    2) оплата идёт и в счёт текущих начислений: max(сальдо − оплата + начислено + пеня + перерасчёт, 0)
       (так в 2024 г.: вода 10550.25 − 11019.15 + 468.90 + 0.64 = 0.64);
    3) сальдо отрицательное — переплата уже погасила долг, оплата месяца уходит в следующий:
       max(сальдо + начислено + пеня + перерасчёт, 0) (2024-05: мусор −780 + 780 + 1.56 = 1.56 при оплате 2340).
    К оплате не бывает меньше нуля: переплата переходит в сальдо следующего месяца. Нечем считать — None.
    """
    if all(value is None for value in (saldo, paid, accrued, penalty, recalculation)):
        return None
    saldo, paid = saldo or 0.0, paid or 0.0
    charges = (accrued or 0.0) + (penalty or 0.0) + (recalculation or 0.0)
    candidates = [round(max(max(saldo - paid, 0.0) + charges, 0.0), 2), round(max(saldo - paid + charges, 0.0), 2)]
    if saldo < 0:
        candidates.append(round(max(saldo + charges, 0.0), 2))
    return list(dict.fromkeys(candidates))


def _match_to_pay(candidates: list[float] | None, to_pay: float | None, tolerance: float) -> tuple[float | None, bool | None]:
    """(ожидаемая сумма, сошлась ли): совпадение с любым из способов; при несовпадении — первый способ."""
    if candidates is None:
        return None, None
    for candidate in candidates:
        if abs(candidate - (to_pay or 0.0)) <= tolerance:
            return candidate, True
    return candidates[0], False


# ---------------------------------------------------------------------------
# Растр и линии таблицы
# ---------------------------------------------------------------------------


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Отрезки True одномерного массива: [(начало, конец_не_включая)]."""
    diff = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(diff == 1).tolist(), np.flatnonzero(diff == -1).tolist(), strict=True))


def line_mask(dark: np.ndarray, length: int, axis: int) -> np.ndarray:
    """Пиксели, лежащие на сплошном тёмном отрезке длиной не меньше `length` вдоль оси `axis`."""
    dark = np.moveaxis(dark, axis, -1).astype(np.int32)
    n = dark.shape[-1]
    if n < length:
        return np.moveaxis(np.zeros(dark.shape, dtype=bool), -1, axis)
    zero = np.zeros(dark.shape[:-1] + (1,), dtype=np.int32)
    csum = np.concatenate([zero, np.cumsum(dark, axis=-1)], axis=-1)
    starts = (csum[..., length:] - csum[..., :-length]) >= length  # окно [i, i+length) целиком тёмное
    # Пиксель j принадлежит линии, если хотя бы одно полное окно начинается в [j-length+1, j].
    starts_sum = np.concatenate([zero, np.cumsum(starts.astype(np.int32), axis=-1)], axis=-1)
    idx = np.arange(n)
    hi = np.minimum(idx, starts.shape[-1] - 1) + 1
    lo = np.clip(idx - length + 1, 0, starts.shape[-1])
    covered = (starts_sum[..., hi] - starts_sum[..., lo]) > 0
    return np.moveaxis(covered, -1, axis)


def _centers(segments: Sequence[tuple[int, int]]) -> list[int]:
    return [(start + end - 1) // 2 for start, end in segments]


@dataclass
class Grid:
    rows: list[int]  # y горизонтальных линий таблицы (первая — верх заголовка)
    cols: list[int]  # x вертикальных линий строки заголовка
    hor: np.ndarray
    ver: np.ndarray


def find_grid(gray: np.ndarray, settings: Settings | None = None) -> Grid:
    """Линии таблицы услуг. Возвращает Grid или поднимает BillParseError."""
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    dark = gray < settings["dark_threshold"]
    height, width = dark.shape
    # Минимальная длина линии больше высоты цифр (~1 % высоты листа), иначе вертикальные штрихи «9», «6», «1»
    # принимаются за линии таблицы, стираются, и 9 читается как 5, 6 — как 5. Строка таблицы выше ~2 %.
    hor = line_mask(dark, max(int(width * settings["hor_line_min"]), 20), axis=1)
    ver = line_mask(dark, max(int(height * settings["ver_line_min"]), 15), axis=0)
    row_lines = _centers(runs(hor.sum(axis=1) > width * 0.5))
    if len(row_lines) < 3:
        raise BillParseError(f"не найдена таблица услуг: горизонтальных линий {len(row_lines)}")
    if len(row_lines) - 2 > settings["max_table_rows"]:
        raise BillParseError(f"в таблице {len(row_lines) - 2} строк, больше лимита ocr.max_table_rows {settings['max_table_rows']}")
    header_top, header_bottom = row_lines[0], row_lines[1]
    band = ver[header_top:header_bottom]
    col_lines = _centers(runs(band.sum(axis=0) > (header_bottom - header_top) * 0.8))
    if len(col_lines) != len(COLUMNS) + 1:
        raise BillParseError(
            f"неожиданная структура таблицы: в заголовке {len(col_lines) - 1} колонок вместо {len(COLUMNS)}"
        )
    return Grid(rows=row_lines, cols=col_lines, hor=hor, ver=ver)


def boundary_present(grid: Grid, x: int, y0: int, y1: int) -> bool:
    """Есть ли вертикальная линия x на всей высоте строки (y0, y1) — иначе ячейки объединены."""
    span = grid.ver[y0 + 3 : y1 - 2, max(x - 3, 0) : x + 4].any(axis=1)
    return span.size > 0 and span.mean() > 0.8


def row_cells(grid: Grid, y0: int, y1: int) -> list[Cell]:
    """Ячейки строки с учётом объединений: [(первая колонка, последняя колонка, x0, x1)]."""
    cells = []
    first = 0
    for index in range(1, len(COLUMNS)):
        if boundary_present(grid, grid.cols[index], y0, y1):
            cells.append((first, index - 1, grid.cols[first], grid.cols[index]))
            first = index
    cells.append((first, len(COLUMNS) - 1, grid.cols[first], grid.cols[-1]))
    return cells


def inner_verticals(grid: Grid, x0: int, x1: int, y0: int, y1: int, inset: int) -> list[int]:
    """Внутренние вертикальные линии ячейки (делят её на подколонки, например «17 | 2»), x центров."""
    band = grid.ver[y0 + inset : y1 - inset, x0 + inset : x1 - inset]
    if band.size == 0:
        return []
    return [x0 + inset + x for x in _centers(runs(band.mean(axis=0) > 0.6))]


# Подъячейка: (x0, x1, y0, y1, номер подстроки). Номер подстроки = число внутренних горизонтальных линий
# над ней в её подколонке: 0 — основная строка услуги, 1 — подстрока (у электроэнергии — «потери»).
Piece = tuple[int, int, int, int, int]


def cell_pieces(grid: Grid, x0: int, x1: int, y0: int, y1: int, inset: int) -> list[Piece]:
    """Подъячейки ячейки: сначала деление по внутренним вертикальным линиям, затем каждой части — по
    горизонтальным. Так в «Начислено» электроэнергии левая часть на всю высоту (итог строки) остаётся в
    основной строке, а правая делится на «по прибору учета» (0) и «потери» (1)."""
    xs = [x0, *inner_verticals(grid, x0, x1, y0, y1, inset), x1]
    pieces = []
    for left, right in zip(xs, xs[1:], strict=False):
        bounds = [y0, *inner_lines(grid, left, right, y0, y1, inset), y1]
        pieces.extend((left, right, top, bottom, sub_row) for sub_row, (top, bottom) in enumerate(zip(bounds, bounds[1:], strict=False)))
    return pieces


def inner_lines(grid: Grid, x0: int, x1: int, y0: int, y1: int, inset: int) -> list[int]:
    """Внутренние горизонтальные линии ячейки (делят её на подстроки), y центров."""
    band = grid.hor[y0 + inset : y1 - inset, x0 + inset : x1 - inset]
    if band.size == 0:
        return []
    return [y0 + inset + y for y in _centers(runs(band.mean(axis=1) > 0.8))]


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


# Переменные окружения, которые нужны внешним программам; остальные (в том числе пароль IMAP) в
# подпроцессы не передаются.
TOOL_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX")


def tool_env(**extra: str) -> dict[str, str]:
    """Минимальное окружение для pdfimages/tesseract (CWE-200: секреты родителя не наследуются)."""
    env = {key: os.environ[key] for key in TOOL_ENV_KEYS if key in os.environ}
    env.update(extra)
    return env


def mask_account(account: str | None) -> str:
    """Лицевой счёт для логов: только последние 4 символа (***3456)."""
    return f"***{account[-4:]}" if account else "***"


def mask_digits(text: str) -> str:
    """Длинные цифровые последовательности (лицевой счёт в имени файла) → ***NNNN для логов."""
    return re.sub(r"\d{6,}", lambda match: mask_account(match.group()), text)


def run_tesseract(image: Image.Image, settings: Settings, numeric: bool = False, psm: int = 6) -> str:
    """Распознаёт PIL-изображение через tesseract (stdin → stdout)."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    command = [settings["tesseract"], "stdin", "stdout", "-l", settings["languages"], "--psm", str(psm)]
    if numeric:
        command += ["-c", "tessedit_char_whitelist=0123456789.,-"]
    # Параллелизм — на уровне процессов (ocr_many), поэтому внутри tesseract один поток.
    env = tool_env(OMP_THREAD_LIMIT="1")
    try:
        result = subprocess.run(
            command,
            input=buffer.getvalue(),
            capture_output=True,
            timeout=settings["timeout_seconds"],
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BillParseError(f"tesseract не запустился: {error}") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise BillParseError(f"tesseract завершился с кодом {result.returncode}: {message}")
    return result.stdout.decode("utf-8", "replace").strip()


def check_tools(settings: Settings | None = None) -> list[str]:
    """Список проблем с внешними программами (пустой — всё есть)."""
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    problems = [f"не найдена программа {settings[key]}" for key in ("pdfimages", "tesseract") if not shutil.which(settings[key])]
    if not problems:
        try:
            result = subprocess.run(
                [settings["tesseract"], "--list-langs"],
                capture_output=True,
                text=True,
                timeout=settings["timeout_seconds"],
                check=False,
                env=tool_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return [f"tesseract не запускается: {error}"]
        available = set(result.stdout.split())
        missing = [lang for lang in settings["languages"].split("+") if lang not in available]
        if missing:
            problems.append(f"у tesseract нет языков: {', '.join(missing)}")
    return problems


def _run_pdfimages(arguments: list[str], settings: Settings) -> str:
    """pdfimages с тайм-аутом; сбой запуска или ненулевой код — BillParseError. Возвращает stdout."""
    try:
        result = subprocess.run(
            [settings["pdfimages"], *arguments],
            capture_output=True,
            text=True,
            timeout=settings["timeout_seconds"],
            check=False,
            env=tool_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BillParseError(f"pdfimages не запустился: {error}") from error
    if result.returncode != 0:
        raise BillParseError(f"pdfimages: код {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def check_page_images(listing: str, settings: Settings) -> None:
    """Вывод `pdfimages -list` первой страницы: не больше max_page_images изображений, каждое не больше
    max_page_pixels. Проверка до извлечения: иначе pdfimages развернул бы «бомбу» или тысячи картинок
    на диск (tmpfs — это память). Непонятный вывод — отказ (fail closed)."""
    lines = listing.splitlines()
    separator = next((index for index, line in enumerate(lines) if line.startswith("---")), None)
    if separator is None:
        raise BillParseError("pdfimages -list: непонятный вывод")
    rows = [line.split() for line in lines[separator + 1 :] if line.strip()]
    if len(rows) > settings["max_page_images"]:
        raise BillParseError(f"на странице {len(rows)} изображений, больше лимита ocr.max_page_images {settings['max_page_images']}")
    for row in rows:
        try:
            width, height = int(row[3]), int(row[4])
        except (IndexError, ValueError) as error:
            raise BillParseError(f"pdfimages -list: непонятная строка «{' '.join(row)}»") from error
        if width * height > settings["max_page_pixels"]:
            raise BillParseError(
                f"изображение {width}×{height} больше лимита ocr.max_page_pixels {settings['max_page_pixels']}"
            )


def extract_page_image(pdf_path: Path, settings: Settings) -> Image.Image:
    """Самый крупный растр первой страницы PDF (через pdfimages) как PIL-изображение в оттенках серого.

    PDF — недоверенный вход: сначала сигнатура %PDF-, затем список изображений страницы (check_page_images),
    и только потом извлечение; размер растра проверяется ещё раз после открытия, до распаковки пикселей.
    """
    try:
        with pdf_path.open("rb") as handle:
            signature = handle.read(5)
    except OSError as error:
        raise BillParseError(f"PDF не читается: {error}") from error
    if signature != b"%PDF-":
        raise BillParseError("файл не PDF: нет сигнатуры %PDF- в начале")
    check_page_images(_run_pdfimages(["-list", "-f", "1", "-l", "1", str(pdf_path)], settings), settings)
    with tempfile.TemporaryDirectory(prefix="utility_bills-") as tmp:
        _run_pdfimages(["-png", "-f", "1", "-l", "1", str(pdf_path), str(Path(tmp) / "img")], settings)
        images = sorted(Path(tmp).glob("img-*.png"), key=lambda p: p.stat().st_size, reverse=True)
        if not images:
            raise BillParseError("в PDF нет растрового изображения страницы")
        try:
            with Image.open(images[0]) as image:
                # Image.open читает только заголовок: размер проверяется до распаковки (convert).
                if image.width * image.height > settings["max_page_pixels"]:
                    raise BillParseError(f"растр {image.width}×{image.height} больше лимита ocr.max_page_pixels")
                return image.convert("L")
        except (OSError, Image.DecompressionBombError) as error:
            raise BillParseError(f"растр страницы не читается: {error}") from error


def ocr_many(requests: Sequence[tuple[Image.Image, bool]], settings: Settings, ocr: OcrFunc) -> list[str]:
    """Параллельное распознавание: requests — [(image, numeric)], результат — тексты в том же порядке."""
    if not requests:
        return []
    with ThreadPoolExecutor(max_workers=max(1, int(settings["workers"]))) as pool:
        return list(pool.map(lambda request: ocr(request[0], settings, numeric=request[1]), requests))


def _cell_image(clean: np.ndarray, x0: int, x1: int, y0: int, y1: int, settings: Settings, scale: float) -> Image.Image:
    """Ячейка без краёв, увеличенная, бинаризованная, в белой рамке (так tesseract точнее читает цифры)."""
    image = Image.fromarray(clean[y0:y1, x0:x1])
    if scale != 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    cell = np.where(np.asarray(image) < settings["dark_threshold"], 0, 255).astype(np.uint8)
    cell = np.pad(cell, round(settings["cell_padding_px"] * scale), constant_values=255)
    return Image.fromarray(cell)


def _erase_lines(gray: np.ndarray, grid: Grid, grow: int) -> np.ndarray:
    """Копия растра со стёртыми линиями таблицы и их серыми краями (иначе они читаются как «|», «1», «4»)."""
    lines = grid.hor | grid.ver
    for _ in range(grow):
        grown = lines.copy()
        grown[1:, :] |= lines[:-1, :]
        grown[:-1, :] |= lines[1:, :]
        grown[:, 1:] |= lines[:, :-1]
        grown[:, :-1] |= lines[:, 1:]
        lines = grown
    clean = gray.copy()
    clean[lines] = 255
    return clean


def _vote(texts: Sequence[str], extract: Callable[[str], list[float]]) -> tuple[str, list[tuple[float, ...]]]:
    """Голосование прочтений одной ячейки на разных масштабах.

    Возвращает (текст победителя, [варианты значений по убыванию голосов]); вариант — кортеж чисел
    `extract(text)`. При равенстве голосов побеждает прочтение на первом масштабе.
    """
    variants = [tuple(extract(text)) for text in texts]
    counts = Counter(variant for variant in variants if variant)
    order = sorted(counts, key=lambda variant: (-counts[variant], variants.index(variant)))
    winner = clean_text(texts[variants.index(order[0])]) if order else clean_text(texts[0]) if texts else ""
    return winner, order


def _fill_item(item: BillItem, values: dict[str, dict[str, Any]], settings: Settings, group: bool = False) -> None:
    """Числовые поля строки из текстов ячеек; values[name] — {"text", "variants"} или текст."""
    printed: dict[str, list[float]] = {}
    for name in NUMERIC_COLUMNS:
        cell = values.get(name)
        if not cell:
            continue
        # Объединённая числовая ячейка читается текстовым проходом (variants = None): числа — из текста.
        extract = money_numbers if name in MONEY_COLUMNS else numbers
        found = list(cell["variants"][0]) if cell["variants"] else extract(cell["text"])
        if not found:
            continue
        printed[name] = found
        if name == "accrued":
            # В ячейке «Начислено» сумма повторяется в подъячейках: берётся последнее число, все — в accrued_parts.
            item.accrued_parts = found
            item.accrued = found[-1]
        elif name == "quantity":
            item.quantity = found[0]
            item.quantity_extra = found[1] if len(found) > 1 else None
        else:
            setattr(item, name, found[0])
    tariff = values.get("tariff")
    if tariff and tariff.get("variants") is not None:
        prices = list(tariff["variants"][0]) if tariff["variants"] else numbers(tariff["text"])
        unit = tariff_unit(tariff["text"])
        item.tariffs = [TariffTier(tier=index, price=price, unit=unit) for index, price in enumerate(prices, start=1)]
        item.price_variants = [list(variant) for variant in tariff["variants"] or []]
        item.tariff_unit = unit
        item.tariff_value = prices[0] if prices else None
        if prices:
            item.tariff = "; ".join(f"{price:.2f}" for price in prices) + (f" {unit}" if unit else "")
        else:
            item.tariff = tariff["text"] or None
    _apply_printed_tiers(item, printed)
    if item.prev_reading is not None and item.curr_reading is not None:
        item.consumption = round(item.curr_reading - item.prev_reading, 3)
    if group:
        # Строка с подстроками проверяется целиком (в parse_table): «К оплате» одно на все подстроки.
        return
    item.accrued_total = item.accrued
    _check_row(item, settings)
    if item.row_check_ok is False:
        _repair_row(item, values, settings)
        item.accrued_total = item.accrued
    if item.row_check_ok is False:
        item.raw["to_pay_mismatch"] = _to_pay_mismatch_source(item)


def _apply_printed_tiers(item: BillItem, printed: dict[str, list[float]]) -> None:
    """Ступени, напечатанные в квитанции (формат 2023–2025): при N ценах в «Кол-во» и «Начислено» по N+1
    числу — слева итог, справа объём и сумма каждой ступени («2196 | 131 131 1934», «58558.73 | 1904.43 …»).
    Итог — в quantity/accrued, ступени — в tariffs (tiers_source = printed)."""
    count = len(item.tariffs)
    quantities, amounts = printed.get("quantity", []), printed.get("accrued", [])
    if count < 2 or len(quantities) != count + 1 or len(amounts) != count + 1:
        return
    item.quantity, item.quantity_extra = quantities[0], None
    item.accrued = amounts[0]
    item.tiers_source = "printed"
    for index, (tier, quantity, amount) in enumerate(zip(item.tariffs, quantities[1:], amounts[1:], strict=True)):
        tier.quantity, tier.amount = quantity, amount
        # Расход на ступени не раскрывает её нормативную границу, если ступень выбрана не полностью.
        tier.from_qty = tier.to_qty = None
        tier.name = tier.label = f"{_tier_label(index, count)} тариф (данные квитанции)"


# Расхождение «К оплате» не больше этого (тенге) при единогласных прочтениях — округление в самой квитанции.
BILL_ROUNDING_TOLERANCE = 0.05


def _to_pay_mismatch_source(item: BillItem) -> str:
    """Причина несовпадения «К оплате», оставшегося после перебора прочтений (_repair_row): "bill" — разница
    не больше BILL_ROUNDING_TOLERANCE, ни одно прочтение ячеек её не объясняет — расхождение в самой
    квитанции (вероятно, округление ЕРЦ: в 2023-07 пеня напечатана 1.2, к оплате 463.41 при расчёте 463.38);
    иначе "ocr" — вероятна ошибка распознавания. Метка — догадка: при сверке 2026-11-12 копеечные
    расхождения чаще оказывались единогласно неверно прочитанной цифрой."""
    difference = abs((item.to_pay or 0.0) - (item.expected_to_pay or 0.0))
    return "bill" if difference <= BILL_ROUNDING_TOLERANCE else "ocr"


def _check_row(item: BillItem, settings: Settings, accrued: float | None = None) -> None:
    """Правило строки «к оплате» (to_pay_candidates). accrued — начислено для строки с подстроками
    (Σ подстрок); без него берётся item.accrued."""
    accrued = item.accrued if accrued is None else accrued
    candidates = to_pay_candidates(item.saldo_start, item.paid, accrued, item.penalty, item.recalculation)
    item.expected_to_pay, item.row_check_ok = _match_to_pay(candidates, item.to_pay, settings["checksum_tolerance"])


def _repair_row(item: BillItem, values: dict[str, dict[str, Any]], settings: Settings) -> None:
    """Строка не сошлась: перебор альтернативных прочтений денежных ячеек, пока правило строки не выполнится.

    На колонку — не больше MAX_REPAIR_OPTIONS прочтений (перебор — их произведение по 6 колонкам); к «Начислено»
    добавляются варианты последней цифры (_accrued_digit_options), подтверждённые тарифом.
    Из подходящих сочетаний берётся то, где меньше всего ячеек заменено (затем — с большим числом голосов).
    Заменённые значения записываются в raw["repaired"].
    """
    columns = [name for name in MONEY_COLUMNS if name != "paying" and values.get(name) and values[name].get("variants")]
    choices = []
    for name in columns:
        # «Начислено» — последнее число ячейки (как в _fill_item), остальные — первое.
        position = -1 if name == "accrued" and item.tiers_source != "printed" else 0
        options = []
        for variant in values[name]["variants"]:
            value = variant[position]
            if value not in options:
                options.append(value)
                if len(options) == MAX_REPAIR_OPTIONS:
                    break
        if name == "accrued":
            options += [value for value in _accrued_digit_options(item, options[0], settings) if value not in options]
        choices.append(options)
    if not columns or all(len(options) == 1 for options in choices):
        return
    original = {name: getattr(item, name) for name in columns}
    best = None
    for combo in itertools.product(*[range(len(options)) for options in choices]):
        changed = sum(1 for index in combo if index)
        if best is not None and (changed, sum(combo)) >= best[0]:
            continue
        for name, options, index in zip(columns, choices, combo, strict=True):
            setattr(item, name, options[index])
        _check_row(item, settings)
        if item.row_check_ok:
            best = ((changed, sum(combo)), {name: getattr(item, name) for name in columns})
    if best is None:
        for name, value in original.items():
            setattr(item, name, value)
        _check_row(item, settings)
        return
    for name, value in best[1].items():
        setattr(item, name, value)
    _check_row(item, settings)
    item.raw["repaired"] = {name: [original[name], value] for name, value in best[1].items() if value != original[name]}


# Как OCR путает последнюю цифру суммы: прочитанная → напечатанные (сверка 2026-11-12 по 37 квитанциям:
# 248.70 → 248.76, 323.79 → 323.76, 1621.29 → 1621.26, 6.69 → 6.65).
LAST_DIGIT_CONFUSIONS = {6: (0, 9), 5: (9,)}


def _accrued_digit_options(item: BillItem, accrued: float, settings: Settings) -> list[float]:
    """«Начислено» с исправленной последней цифрой (LAST_DIGIT_CONFUSIONS) — только если оно ближе к
    кол-во × цена, чем прочитанное, и совпадает с ним с точностью до округления цены (полкопейки на единицу
    объёма). Полный допуск тарифа (_tariff_tolerance) не годится: он учитывает и округление количества, у
    отопления это ±18 тг, и любая цифра «подтвердилась» бы (2024-03: 10608.75 → 10608.79 вместо перерасчёта
    118.16 → 118.19). Без тарифа подтверждения нет: в другой ячейке такая же замена «объяснила» бы и
    настоящее расхождение квитанции (2023-07: пеня 1.2 вместо 1.23 у ЕРЦ)."""
    if item.quantity is None or len(item.tariffs) != 1 or item.tiers_source == "printed":
        return []
    expected = item.quantity * item.tariffs[0].price
    tolerance = item.quantity * 0.005 + settings["checksum_tolerance"]
    cents = round(accrued * 100)
    options = []
    for digit in LAST_DIGIT_CONFUSIONS.get(cents % 10, ()):
        value = (cents - cents % 10 + digit) / 100
        if abs(value - expected) < abs(accrued - expected) and abs(value - expected) <= tolerance:
            options.append(value)
    return sorted(options, key=lambda value: abs(value - expected))


def _ranked_combos(sizes: Sequence[int]) -> Iterator[tuple[int, ...]]:
    """Сочетания мест (индексов вариантов) в порядке (сумма мест, сочетание) — как sorted(product(...)),
    но лениво: всё произведение не строится (при 8 подъячейках по 6 вариантов это 1,7 млн кортежей)."""
    # spare[i] — сколько мест могут «забрать» части с i-й до конца (для отсечения невозможных ветвей).
    spare = [0] * (len(sizes) + 1)
    for index in range(len(sizes) - 1, -1, -1):
        spare[index] = spare[index + 1] + sizes[index] - 1

    def with_sum(position: int, remaining: int) -> Iterator[tuple[int, ...]]:
        if position == len(sizes):
            if remaining == 0:
                yield ()
            return
        for index in range(max(0, remaining - spare[position + 1]), min(remaining, sizes[position] - 1) + 1):
            for rest in with_sum(position + 1, remaining - index):
                yield (index, *rest)

    for total in range(spare[0] + 1):
        yield from with_sum(0, total)


def _combine(piece_variants: Sequence[list[tuple[float, ...]]]) -> list[tuple[float, ...]]:
    """Варианты ячейки из нескольких подъячеек: сочетания вариантов частей (числа по порядку слева
    направо), сначала сочетание победителей, затем — с меньшей суммой мест в голосовании.

    Лимиты (MAX_PIECE_VARIANTS на часть, MAX_CELL_VARIANTS в итоге, MAX_COMBOS_CHECKED проверок) не дают
    растру с десятками линий в ячейке устроить взрыв сочетаний; квитанции укладываются в них с запасом.
    """
    lists = [variants[:MAX_PIECE_VARIANTS] for variants in piece_variants if variants]
    if not lists:
        return []
    result: list[tuple[float, ...]] = []
    for combo in itertools.islice(_ranked_combos([len(variants) for variants in lists]), MAX_COMBOS_CHECKED):
        value = tuple(number for variants, index in zip(lists, combo, strict=True) for number in variants[index])
        if value not in result:
            result.append(value)
            if len(result) == MAX_CELL_VARIANTS:
                break
    return result


def parse_table(
    gray_image: Image.Image, settings: Settings | None = None, ocr: OcrFunc = run_tesseract
) -> tuple[Grid, list[BillItem]]:
    """Строки услуг таблицы. `ocr(image, settings, numeric=…)` подменяется в тестах.

    Строка таблицы с внутренними горизонтальными линиями даёт несколько строк услуги (sub_row 0, 1, …):
    подстрока создаётся всегда, даже пустая, — она отдельная строка квитанции («потери» электроэнергии).
    """
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    scales = list(settings["cell_scales"]) or [1]
    gray = np.asarray(gray_image)
    grid = find_grid(gray, settings)
    erased: dict[int, np.ndarray] = {}

    def erased_lines(grow: int) -> np.ndarray:
        """Изображение со стёртыми линиями сетки заданной ширины (одно на ширину)."""
        if grow not in erased:
            erased[grow] = _erase_lines(gray, grid, grow)
        return erased[grow]

    clean = erased_lines(settings["line_erase_px"])
    # Тарифная ячейка — при нескольких вариантах стирания линий: цифра вплотную к линии теряется по-разному
    # (1 px: «3929.99» → «3029.99»; 2 px: «95.37» → «5.37»), голосование и проверка тарифа выбирают верное.
    tariff_cleans = [(grow, erased_lines(grow)) for grow in settings["tariff_line_erase_px"]] or [(settings["line_erase_px"], clean)]
    inset = settings["cell_inset_px"]

    # 1. Подъячейки и запросы на распознавание.
    pieces = []
    requests = []
    sub_rows: dict[int, int] = {}  # строка таблицы → номер последней подстроки (по линиям, не по тексту)
    for position, (y0, y1) in enumerate(zip(grid.rows[1:-1], grid.rows[2:], strict=True), start=1):
        sub_rows[position] = 0
        for first, last, x0, x1 in row_cells(grid, y0, y1):
            name = COLUMNS[first]
            merged = last > first
            key = name if not merged else f"{name}..{COLUMNS[last]}"
            parts = [(x0, x1, y0, y1, 0)] if name == "service" or merged else cell_pieces(grid, x0, x1, y0, y1, inset)
            if len(parts) > settings["max_cell_pieces"]:
                raise BillParseError(
                    f"строка {position}, колонка {name}: {len(parts)} подъячеек, больше лимита ocr.max_cell_pieces "
                    f"{settings['max_cell_pieces']}"
                )
            for left, right, top, bottom, sub_row in parts:
                sub_rows[position] = max(sub_rows[position], sub_row)
                box = (left + inset, right - inset, top + inset, bottom - inset)
                if box[3] - box[2] < 8 or box[1] - box[0] < 8:
                    continue
                if int((clean[box[2] : box[3], box[0] : box[1]] < settings["dark_threshold"]).sum()) < settings["empty_cell_pixels"]:
                    continue
                piece = {"position": position, "sub_row": sub_row, "name": name, "merged": merged, "key": key}
                piece["box"] = [int(left), int(top), int(right), int(bottom)]
                piece["provider"] = merged and {"prev_reading", "curr_reading"} <= set(COLUMNS[first : last + 1])
                piece["text_request"] = None
                piece["number_requests"] = []
                if name in TEXT_COLUMNS or merged:
                    piece["text_request"] = len(requests)
                    requests.append((_cell_image(clean, *box, settings, scales[0]), False))
                if (name in NUMERIC_COLUMNS or name == "tariff") and not merged:
                    sources = tariff_cleans if name == "tariff" else [(settings["line_erase_px"], clean)]
                    piece["number_labels"] = []
                    for grow, source in sources:
                        for scale in scales:
                            piece["number_requests"].append(len(requests))
                            piece["number_labels"].append({"erase_px": grow, "scale": scale})
                            requests.append((_cell_image(source, *box, settings, scale), True))
                pieces.append(piece)

    # 2. Распознавание (параллельно).
    texts = ocr_many(requests, settings, ocr)

    # 3. Сборка: подъячейки одной колонки и подстроки объединяются слева направо.
    groups: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    for piece in pieces:
        groups.setdefault((piece["position"], piece["sub_row"], piece["key"]), []).append(piece)
    rows: dict[int, dict[str, Any]] = {
        position: {"values": [{} for _ in range(count + 1)], "raw": {}, "provider": None} for position, count in sub_rows.items()
    }
    for (position, sub_row, key), group in groups.items():
        head = group[0]
        name = head["name"]
        row = rows[position]
        text = " | ".join(t for t in (clean_text(texts[p["text_request"]]) for p in group if p["text_request"] is not None) if t)
        extract = tariff_prices if name == "tariff" else money_numbers if name in MONEY_COLUMNS else numbers
        votes = [_vote([texts[index] for index in p["number_requests"]], extract) for p in group if p["number_requests"]]
        winner = " | ".join(w for w, _ in votes if w)
        variants = _combine([v for _, v in votes])
        shown = text if name in TEXT_COLUMNS or head["merged"] else winner
        if not shown and not variants:
            continue
        raw_key = key if sub_row == 0 else f"{key}#{sub_row}"
        row["raw"][raw_key] = shown if name != "tariff" else f"{text} | {winner}"
        row["raw"][f"{raw_key}~ocr"] = [
            {"box": piece["box"],
             "text": texts[piece["text_request"]] if piece["text_request"] is not None else None,
             "numeric": [{**label, "text": texts[index]}
                         for label, index in zip(piece.get("number_labels", []), piece["number_requests"], strict=True)]}
            for piece in group
        ]
        if head["provider"]:
            # Объединённые «Пред./Текущее показание» содержат поставщика услуги и его телефон.
            row["provider"] = text
            continue
        row["values"][sub_row][name] = {"text": shown, "variants": variants if votes else None}

    items = []
    for position in sorted(rows):
        row = rows[position]
        service_raw = (row["values"][0].get("service") or {}).get("text", "")
        known = canonical_service(service_raw)
        service = known or russian_part(service_raw) or f"строка {position}"
        provider_name, provider_phone = split_provider(row["provider"]) if row["provider"] else (None, None)
        group = len(row["values"]) > 1
        for sub_row, sub_values in enumerate(row["values"]):
            sub_service = _sub_row_service(service, service_raw, sub_row)
            item = BillItem(
                position=position,
                sub_row=sub_row,
                service=sub_service,
                service_raw=service_raw,
                provider=provider_name,
                provider_phone=provider_phone,
                known_service=known is not None,
                raw=row["raw"] if sub_row == 0 else {key: value for key, value in row["raw"].items() if f"#{sub_row}" in key},
            )
            _fill_item(item, sub_values, settings, group=group)
            items.append(item)
        if group:
            row_items = [item for item in items if item.position == position]
            # Сальдо, оплата, пеня, перерасчёт и «К оплате» общие на строку (в подстроке 0), начисления —
            # у каждой подстроки: итог (accrued_total подстроки 0) = Σ начислений подстрок.
            head = row_items[0]
            accrued = [item.accrued for item in row_items if item.accrued is not None]
            head.accrued_total = round(sum(accrued), 2) if accrued else None
            _check_row(head, settings, head.accrued_total)
            if row_items[0].row_check_ok is False:
                row_items[0].raw["to_pay_mismatch"] = _to_pay_mismatch_source(row_items[0])
    return grid, items


def _sub_row_service(service: str, service_raw: str, sub_row: int) -> str:
    """Название подстроки услуги: у электроэнергии вторая подстрока — «потери», у отопления — ГВС."""
    folded = service_raw.casefold()
    if sub_row == 0:
        return service
    if "потер" in folded:
        return f"{service} — потери"
    if "гвс" in folded and sub_row == 1:
        return "Горячая вода (ГВС)"
    return f"{service} — подстрока {sub_row}"


# ---------------------------------------------------------------------------
# Шапка и низ листа
# ---------------------------------------------------------------------------


def _find_period(text: str) -> str | None:
    lowered = text.casefold().replace("ё", "е")
    month_re = "|".join(MONTHS_NOMINATIVE)
    match = re.search(rf"({month_re})\s+(\d{{4}})", lowered)
    return f"{match.group(2)}-{MONTHS_NOMINATIVE.index(match.group(1)) + 1:02d}" if match else None


def _find_service_period(text: str) -> str | None:
    lowered = text.casefold().replace("ё", "е")
    month_re = "|".join(MONTHS_PREPOSITIONAL)
    match = re.search(rf"\bв\s+({month_re})\s+(\d{{4}})", lowered)
    return f"{match.group(2)}-{MONTHS_PREPOSITIONAL.index(match.group(1)) + 1:02d}" if match else None


def _label_value(text: str, pattern: str) -> str | None:
    """Значение после подписи в той же строке (пустая подпись — None)."""
    match = re.search(pattern + r"[ \t:]*([^\n]*)", text, re.IGNORECASE)
    value = clean_text(match.group(1)) if match else ""
    return value or None


def parse_header(text: str, bill: Bill) -> None:
    """Поля шапки (над таблицей)."""
    lowered = text.casefold().replace("ё", "е")
    match = re.search(r"\b(\d{10})\b", text)
    if match:
        bill.account = match.group(1)
    bill.period = _find_period(text)
    bill.service_period = _find_service_period(text)
    match = _DATE_TIME_RE.search(text)
    if match:
        day, month, year, clock, _number = match.groups()
        bill.issued_at = f"{year}-{month}-{day}T{clock}"
    match = re.search(r"^\s*(\d{3,8})\s*$", text, re.MULTILINE)
    if match:
        bill.doc_number = match.group(1)
    match = re.search(r"\b(ул\..*?кв\.?\s*\d+)", text)
    if match:
        bill.address = clean_text(match.group(1))
    # Казахский адрес: до двух слов из букв перед «көш.» (имя и фамилия: «Абай Құнанбаев көш.»);
    # только буквы — чтобы не захватить мусор распознавания логотипа в начале строки.
    match = re.search(r"((?:[^\W\d_]+\s+){1,2}к[өo]ш\..*?\d+\s*п\.)", text)
    if match:
        bill.address_kz = clean_text(match.group(1))
    payer = _label_value(text, r"Плат[еe]льщик")
    if payer and not re.match(r"(?i)адам|кол\.", payer):
        bill.payer = payer
    controller = _label_value(text, r"Контрол[еe]р")
    if controller:
        bill.controller = controller
    patterns = {
        # Значение — в той же строке ([ \t]), иначе пустое поле захватит число со следующей строки.
        "total_area": r"об\S*\s*пл\S*[ \t]+(\d+[.,]\d+)",
        "heated_area": r"отапл\S*\s*пл\S*[ \t]+(\d+[.,]\d+)",
        "useful_area": r"полезн\S*[ \t]+(\d+[.,]\d+)",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, lowered)
        if match:
            setattr(bill, name, _to_float(match.group(1)))
    match = re.search(r"кол\.?\s*человек\S*[ \t]+(\d+)\b", lowered)
    if match:
        bill.residents = int(match.group(1))


def parse_table_header(text: str, bill: Bill) -> None:
    """Даты из строки заголовка таблицы: «Сальдо на 31.05.2023 г.» и «Начислено за 06.2023 г.».

    Дата стоит строкой ниже подписи («Сальдо на» … «31.05.2023 г»), поэтому подписи не ищутся: полная дата
    в заголовке одна — дата сальдо, месяц «ММ.ГГГГ» (и казахское «06.2023 ж. үшін») — месяц начислений."""
    match = re.search(r"(?<![\d.])(\d{2})\.(\d{2})\.(\d{4})(?![\d.])", text)
    if match:
        day, month, year = match.groups()
        bill.saldo_date = f"{year}-{month}-{day}"
    match = re.search(r"(?<![\d.])(\d{2})\.(\d{4})(?![\d.])", text)
    if match:
        bill.accrual_month = f"{match.group(2)}-{match.group(1)}"


def parse_footer(text: str, bill: Bill) -> None:
    """«Итого», срок оплаты и отрывной корешок (счёт, месяц, дата, номер документа) — под таблицей."""
    match = re.search(r"до\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    if match:
        day, month, year = match.groups()
        bill.due_date = f"{year}-{month}-{day}"
    amounts = _MONEY_RE.findall(text)
    if amounts:
        bill.total = _to_float(amounts[0])
    # Корешок дублирует шапку: недостающее берётся отсюда.
    # На корешке 2023–2025 гг. счёт напечатан без ведущих нулей («Лицевой счет 0123456»).
    match = re.search(r"сч[её]т\D{0,10}(\d{6,10})(?!\d)", text, re.IGNORECASE)
    if match and not bill.account:
        bill.account = match.group(1).zfill(10)
    bill.period = bill.period or _find_period(text)
    bill.service_period = bill.service_period or _find_service_period(text)
    for match in _DATE_TIME_RE.finditer(text):
        day, month, year, clock, number = match.groups()
        bill.issued_at = bill.issued_at or f"{year}-{month}-{day}T{clock}"
        if number:
            bill.doc_number = bill.doc_number or number
    if not bill.controller:
        bill.controller = _label_value(text, r"Контрол[еe]р")


def tier_bounds(settings: Settings, residents: int | None) -> tuple[list[tuple[float, float | None]], int]:
    """Границы ступеней (нижняя, верхняя) с учётом жильцов; последняя ступень без верхней границы."""
    scale = 1
    if settings["tier_bounds_per_resident"]:
        scale = residents or settings["tier_residents_default"] or 1
    uppers = [bound * scale for bound in settings["tier_bounds"]]
    lowers = [0, *uppers]
    return list(zip(lowers, [*uppers, None], strict=True)), scale


def _tier_label(index: int, count: int) -> str:
    return TIER_NAMES[index] if count == len(TIER_NAMES) else f"Ступень {index + 1}"


def _tier_name(index: int, count: int, low: float, high: float | None, suffix: str = "") -> str:
    label = _tier_label(index, count)
    if high is None:
        return f"{label} тариф (от {low:g} кВт·ч{suffix} и выше)"
    if low == 0:
        return f"{label} тариф (до {high:g} кВт·ч{suffix})"
    return f"{label} тариф (от {low:g} до {high:g} кВт·ч{suffix})"


def quantity_unit(tariff_unit: str | None) -> str | None:
    """Единица количества из единицы тарифа: «тг/м2» → «м2», «тг/кВт*ч» → «кВт*ч», «м3» → «м3», «тг» → None."""
    if not tariff_unit:
        return None
    unit = tariff_unit.split("/", 1)[1] if "/" in tariff_unit else tariff_unit
    return None if unit == "тг" else unit


def _tariff_tolerance(quantity: float, price: float, settings: Settings) -> float:
    return quantity * 0.005 + price * 0.005 + settings["checksum_tolerance"]


def check_tariff(item: BillItem, settings: Settings, residents: int | None) -> bool | None:
    """Начислено = кол-во × цена; при нескольких ценах — по ступеням. Возвращает True/False/None (нечем проверить).

    Цена и количество напечатаны с округлением до сотых (фактически 24.8124 тг, 2.7689 Гкал), поэтому
    допуск (_tariff_tolerance) — половина копейки цены на объём плюс половина сотой количества на цену,
    плюс копейка суммы.
    """
    if item.quantity is None or item.accrued is None or not item.tariffs:
        return None
    if item.tiers_source == "printed":
        return _check_printed_tiers(item, settings)
    prices = [tier.price for tier in item.tariffs]
    tolerance = _tariff_tolerance(item.quantity, max(prices), settings)
    if len(prices) == 1:
        item.expected_accrued = round(item.quantity * prices[0], 2)
        item.tariff_check_ok = abs(item.expected_accrued - item.accrued) <= tolerance
        if not item.tariff_check_ok:
            _repair_tariff(item, settings)
        return item.tariff_check_ok
    bounds, _scale = tier_bounds(settings, residents)
    if len(bounds) != len(prices):
        return None
    item.tiers_source = "computed"
    # Базовые границы (без умножения на жильцов) — для справочника тарифов: они не зависят от квитанции.
    base_bounds, _ = tier_bounds({**settings, "tier_bounds_per_resident": False}, None)
    suffix = " на жильца" if settings["tier_bounds_per_resident"] else ""
    total = 0.0
    for index, (tier, (low, high), (base_low, base_high)) in enumerate(zip(item.tariffs, bounds, base_bounds, strict=True)):
        part = float(max(0.0, (item.quantity if high is None else min(item.quantity, high)) - low))
        tier.name = _tier_name(index, len(bounds), low, high)
        tier.label = _tier_name(index, len(bounds), base_low, base_high, suffix)
        tier.from_qty, tier.to_qty = low, high
        tier.quantity = round(part, 3)
        tier.amount = round(part * tier.price, 2)
        total += part * tier.price
    item.expected_accrued = round(total, 2)
    item.tariff_check_ok = abs(item.expected_accrued - item.accrued) <= tolerance
    return item.tariff_check_ok


def _repair_tariff(item: BillItem, settings: Settings) -> None:
    """Цена не сошлась: из других прочтений тарифной ячейки берётся первая, при которой кол-во × цена =
    начислено. Исправление — в raw["tariff_repaired"] и примечаниях квитанции."""
    old = item.tariffs[0].price
    for variant in item.price_variants:
        if len(variant) != 1 or variant[0] == old:
            continue
        expected = round(item.quantity * variant[0], 2)
        tolerance = _tariff_tolerance(item.quantity, variant[0], settings)
        if abs(expected - item.accrued) <= tolerance:
            item.tariffs[0].price = item.tariff_value = variant[0]
            item.tariff = f"{variant[0]:.2f}" + (f" {item.tariff_unit}" if item.tariff_unit else "")
            item.expected_accrued, item.tariff_check_ok = expected, True
            item.raw["tariff_repaired"] = [old, variant[0]]
            return


def _check_printed_tiers(item: BillItem, settings: Settings) -> bool:
    """Напечатанные ступени: сумма каждой = объём × цена (с допуском на округление) и итог = Σ сумм ступеней."""
    ok = True
    expected = 0.0
    for tier in item.tariffs:
        tolerance = _tariff_tolerance(tier.quantity, tier.price, settings)
        expected += tier.quantity * tier.price
        ok = ok and abs(tier.quantity * tier.price - tier.amount) <= tolerance
    item.expected_accrued = round(expected, 2)
    total = round(sum(tier.amount for tier in item.tariffs), 2)
    ok = ok and abs(total - item.accrued) <= settings["checksum_tolerance"] * len(item.tariffs)
    quantity = sum(tier.quantity for tier in item.tariffs)
    ok = ok and abs(quantity - item.quantity) <= settings["checksum_tolerance"] * len(item.tariffs)
    item.tariff_check_ok = ok
    return ok


def parse_filename(name: str) -> tuple[str | None, str | None]:
    """Лицевой счёт и месяц из имени вида A000123456-2026-10.PDF: (цифры счёта, ГГГГ-ММ)."""
    match = re.search(r"(\d{6,})-(\d{4})-(\d{2})", name)
    if not match:
        return None, None
    return match.group(1), f"{match.group(2)}-{match.group(3)}"


def _crosscheck_filename(bill: Bill, pdf_name: str) -> None:
    file_account, file_period = parse_filename(pdf_name)
    if bill.period is None and file_period:
        bill.period = file_period
        bill.notes.append(f"месяц квитанции не распознан, взят из имени файла: {file_period}")
    elif file_period and bill.period != file_period:
        bill.warnings.append(f"месяц квитанции {bill.period} не совпадает с именем файла ({file_period})")
    if bill.account is None and file_account:
        bill.account = file_account.zfill(10)
        bill.notes.append(f"лицевой счёт не распознан в квитанции, взят из имени файла: {mask_account(bill.account)}")
    elif file_account and bill.account and bill.account.lstrip("0") != file_account.lstrip("0"):
        bill.warnings.append(f"лицевой счёт {mask_account(bill.account)} не совпадает с именем файла ({mask_account(file_account)})")


def format_features(page_size: tuple[int, int], grid: Grid, items: list[BillItem], table_header: str) -> tuple[dict, str]:
    """Признаки формата квитанции и их хеш.

    Подпись описывает наблюдаемую раскладку, а не дату введения официального бланка.
    Текст OCR, цены и объёмы в подпись не входят. Отсутствие отопления — отдельный наблюдаемый вариант.
    """
    left, right = grid.cols[0], grid.cols[-1]
    bounds = [round((x - left) / (right - left), 2) for x in grid.cols]
    by_position: dict[int, list[BillItem]] = {}
    for item in items:
        by_position.setdefault(item.position, []).append(item)
    electricity = [item for item in items if item.service.startswith("Электроэнергия") and "ОДН" not in item.service]
    tiers = next((item.tiers_source for item in electricity if item.tiers_source), None)
    losses = any(item.sub_row and "потери" in item.service for item in electricity)
    heating = None
    if any(item.service == "Горячая вода (ГВС)" and item.sub_row for item in items):
        heating = "ГВС подстрокой отопления"
    elif any(item.service == "Горячая вода (ГВС)" and not item.sub_row for item in items):
        heating = "ГВС отдельной строкой"
    stable = {
        "signature_version": 1,
        "page_size": list(page_size),
        "column_bounds": bounds,
        "electricity_tiers": tiers,
        "electricity_losses_row": losses,
        "heating_layout": heating,
    }
    features = {
        **stable,
        "parser_version": PARSER_VERSION,
        "table_rows": len(grid.rows) - 2,
        "row_bounds_px": [int(y) for y in grid.rows],
        "column_bounds_px": [int(x) for x in grid.cols],
        "services": [
            {"position": position, "service": group[0].service, "sub_rows": len(group),
             "sub_services": [item.service for item in group], "tiers": group[0].tiers_source}
            for position, group in by_position.items()
        ],
        "table_header": " ".join(table_header.split()),
    }
    description = []
    description.append({"printed": "ступени электроэнергии напечатаны в квитанции", "computed": "электроэнергия одной строкой, ступени по границам"}.get(tiers or "", "электроэнергия без ступеней"))
    if losses:
        description.append("подстрока «потери» электроэнергии")
    if heating:
        description.append(heating)
    features["description"] = "; ".join(description)
    signature = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:12]
    return features, signature


def parse_bill(
    pdf_path: Path | str, settings: Settings | None = None, ocr: OcrFunc = run_tesseract, pdf_name: str | None = None
) -> Bill:
    """Полный разбор PDF-квитанции → Bill. Сбой формата или OCR — BillParseError.

    `pdf_name` — исходное имя вложения (для сверки счёта и месяца), если файл лежит под другим именем.
    """
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    pdf_path = Path(pdf_path)
    gray_image = extract_page_image(pdf_path, settings)
    grid, items = parse_table(gray_image, settings, ocr=ocr)
    bill = Bill(items=items)
    width, height = gray_image.size
    header, table_header, footer = ocr_many(
        [
            (gray_image.crop((0, 0, width, grid.rows[0])), False),
            (gray_image.crop((grid.cols[0], grid.rows[0], grid.cols[-1], grid.rows[1])), False),
            (gray_image.crop((0, grid.rows[-1] + 3, width, height)), False),
        ],
        settings,
        ocr,
    )
    bill.header_text, bill.table_header_text, bill.footer_text = header, table_header, footer
    parse_table_header(table_header, bill)
    parse_header(header, bill)
    parse_footer(footer, bill)
    _crosscheck_filename(bill, pdf_name or pdf_path.name)
    if not bill.account or not bill.period:
        raise BillParseError("не распознаны лицевой счёт или месяц квитанции")

    bill.items_sum = round(sum(item.to_pay or 0.0 for item in items), 2)
    if bill.total is None:
        bill.warnings.append("не распознана строка «Итого»")
    else:
        bill.checksum_ok = abs(bill.items_sum - bill.total) <= settings["checksum_tolerance"]
        if not bill.checksum_ok:
            bill.warnings.append(
                f"сумма колонки «К оплате» {bill.items_sum:.2f} не равна «Итого» {bill.total:.2f}: проверьте распознавание"
            )
    _bounds, scale = tier_bounds(settings, bill.residents)
    bill.tier_residents = scale if settings["tier_bounds_per_resident"] else None
    for item in items:
        result = check_tariff(item, settings, bill.residents)
        if result is True:
            bill.tariff_checks_ok += 1
        elif result is False:
            bill.tariff_checks_failed += 1
            detail = ""
            if len(item.tariffs) > 1:
                parts = ", ".join(f"{tier.quantity:g}×{tier.price:.2f}" for tier in item.tariffs if tier.quantity)
                detail = f" ({parts})"
            bill.warnings.append(
                f"строка {item.position} «{item.service}»: начислено {item.accrued}, а по тарифу "
                f"{item.expected_accrued:.2f}{detail} за {item.quantity:g} {quantity_unit(item.tariff_unit) or ''}".rstrip()
            )
        if item.raw.get("repaired"):
            changes = ", ".join(f"{name} {old} → {new}" for name, (old, new) in item.raw["repaired"].items())
            bill.notes.append(f"строка {item.position} «{item.service}» исправлена по проверке строки: {changes}")
        if item.raw.get("tariff_repaired"):
            old, new = item.raw["tariff_repaired"]
            bill.notes.append(f"строка {item.position} «{item.service}»: тариф {old} → {new} по проверке «кол-во × цена»")
        if not item.known_service and item.sub_row == 0:
            bill.warnings.append(f"услуга строки {item.position} не опознана: «{item.service_raw}»")
        if item.row_check_ok is False:
            bill.rows_failed += 1
            cause = (
                "расхождение в самой квитанции (ни одно прочтение ячеек его не объясняет, вероятно округление ЕРЦ)"
                if item.raw.get("to_pay_mismatch") == "bill"
                else "вероятна ошибка распознавания"
            )
            bill.warnings.append(
                f"строка {item.position} «{item.service}»: к оплате {item.to_pay}, а по сальдо, оплате и начислениям "
                f"{item.expected_to_pay:.2f} — {cause}"
            )
        elif item.row_check_ok:
            bill.rows_ok += 1
    bill.format_features, bill.format_signature = format_features(gray_image.size, grid, items, table_header)
    return bill
