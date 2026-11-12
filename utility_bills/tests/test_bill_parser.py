"""Тесты разбора квитанции без tesseract и реальных PDF: синтетическая сетка и подменный OCR."""

import importlib.util
import itertools
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

MODULE_PATH = Path(__file__).resolve().parents[1] / "bill_parser.py"
SPEC = importlib.util.spec_from_file_location("utility_bills_bill_parser_test", MODULE_PATH)
bp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bp)

SETTINGS = dict(bp.DEFAULT_SETTINGS)


def synthetic_page(merge_prev_curr_in_row=None, inner_line_in_row=None, digit_stroke=False, split_accrued_in_row=None):
    """Лист 2000×2400 с таблицей: заголовок + 3 строки, 13 вертикальных линий."""
    page = np.full((2400, 2000), 255, dtype=np.uint8)
    rows = [600, 760, 840, 920, 1000]
    cols = [100 + 150 * index for index in range(13)]
    for y in rows:
        page[y : y + 2, cols[0] : cols[-1] + 2] = 0
    for index, x in enumerate(cols):
        for row in range(len(rows) - 1):
            y0, y1 = rows[row], rows[row + 1]
            if row == merge_prev_curr_in_row and index == bp.COLUMNS.index("curr_reading"):
                continue  # ячейки «Пред./Текущее показание» объединены
            page[y0 : y1 + 2, x : x + 2] = 0
    if inner_line_in_row is not None:
        y0, y1 = rows[inner_line_in_row], rows[inner_line_in_row + 1]
        middle = (y0 + y1) // 2
        # Внутренняя линия короче половины листа (иначе это была бы строка таблицы).
        x0, x1 = cols[bp.COLUMNS.index("quantity")], cols[bp.COLUMNS.index("penalty")]
        page[middle : middle + 2, x0 : x1 + 2] = 0
    if split_accrued_in_row is not None:
        # «Начислено» как у электроэнергии: вертикальная линия на всю высоту, горизонтальная — только справа.
        y0, y1 = rows[split_accrued_in_row], rows[split_accrued_in_row + 1]
        x0 = cols[bp.COLUMNS.index("accrued")]
        page[y0 : y1 + 2, x0 + 75 : x0 + 77] = 0
        page[(y0 + y1) // 2 : (y0 + y1) // 2 + 2, x0 + 75 : x0 + 152] = 0
    if digit_stroke:
        # Вертикальный штрих цифры (≈ 30 px) внутри ячейки не должен считаться линией таблицы.
        page[800:830, 1060:1063] = 0
    return page, rows, cols


def test_find_grid_detects_rows_and_columns():
    page, rows, cols = synthetic_page()
    grid = bp.find_grid(page, SETTINGS)
    assert len(grid.rows) == len(rows)
    assert len(grid.cols) == len(bp.COLUMNS) + 1
    assert all(abs(a - b) <= 2 for a, b in zip(grid.cols, cols, strict=True))


def test_find_grid_rejects_unexpected_layout():
    page, _rows, cols = synthetic_page()
    page[:, cols[5] : cols[5] + 2] = 255  # одна колонка пропала
    with pytest.raises(bp.BillParseError, match="неожиданная структура"):
        bp.find_grid(page, SETTINGS)


def test_digit_strokes_are_not_table_lines():
    page, _rows, _cols = synthetic_page(digit_stroke=True)
    grid = bp.find_grid(page, SETTINGS)
    assert not grid.ver[800:830, 1060:1063].any()


def test_merged_cells_detected_per_row():
    page, rows, _cols = synthetic_page(merge_prev_curr_in_row=2)
    grid = bp.find_grid(page, SETTINGS)
    cells = bp.row_cells(grid, grid.rows[2], grid.rows[3])
    spans = [(bp.COLUMNS[first], bp.COLUMNS[last]) for first, last, _x0, _x1 in cells]
    assert ("prev_reading", "curr_reading") in spans
    other = bp.row_cells(grid, grid.rows[1], grid.rows[2])
    assert len(other) == len(bp.COLUMNS)


def test_inner_lines_split_cell_into_sub_rows():
    page, _rows, _cols = synthetic_page(inner_line_in_row=1)
    grid = bp.find_grid(page, SETTINGS)
    index = bp.COLUMNS.index("quantity")
    x0, x1 = grid.cols[index], grid.cols[index + 1]
    assert len(bp.inner_lines(grid, x0, x1, grid.rows[1], grid.rows[2], 3)) == 1
    assert bp.inner_lines(grid, x0, x1, grid.rows[2], grid.rows[3], 3) == []


def test_numbers_keeps_separate_values():
    assert bp.numbers("285343 2853.43") == [285343.0, 2853.43]


def test_tariff_prices_drop_unit_digits():
    assert bp.tariff_prices("80.002") == [80.0]
    assert bp.tariff_prices("24.81 33.74 42.18") == [24.81, 33.74, 42.18]
    assert bp.tariff_prices("127.4113") == [127.41]


@pytest.mark.parametrize(
    ("text", "unit"),
    [("24.81 тг/кВт“ч", "тг/кВт*ч"), ("28.12 тг/кВт**ч", "тг/кВт*ч"), ("80.00 тг/м2", "тг/м2"), ("119.36 м3", "м3"), ("", None)],
)
def test_tariff_unit(text, unit):
    assert bp.tariff_unit(text) == unit


@pytest.mark.parametrize(
    ("text", "name", "phone"),
    [
        ("ТОО Пример Сервис +7 700 1234 567", "ТОО Пример Сервис", "+77001234567"),
        ("ТОО Example Waste 8 700 123 45 67", "ТОО Example Waste", "87001234567"),
        ("КСП(К) Пример", "КСП(К) Пример", None),
    ],
)
def test_split_provider(text, name, phone):
    assert bp.split_provider(text) == (name, phone)


@pytest.mark.parametrize(
    ("text", "service"),
    [
        ("ААЭК ЖҮҚ кеткен ЭЭ/ ЭЭ по ОДН Астана-РЭК", "Электроэнергия на ОДН"),
        ("ААЭК электроэнергиясы/ Электроэнергия Астана-РЭК", "Электроэнергия"),
        ("Сумен жабдықтау/ Водоснабжение по ПУ", "Водоснабжение"),
        ("ҚТҚ шығару/ Вывоз мусора", "Вывоз мусора"),
        ("Что-то новое", None),
    ],
)
def test_canonical_service(text, service):
    assert bp.canonical_service(text) == service


def item(**values):
    return bp.BillItem(position=1, sub_row=0, service="x", service_raw="x", **values)


def test_expected_to_pay_ignores_overpayment():
    # Оплата сверх сальдо не уменьшает начисление (так считает ЕРЦ).
    assert bp.to_pay_candidates(None, 6096.0, 6096.0, None, None)[0] == 6096.0
    assert bp.to_pay_candidates(3175.99, 3175.99, 2853.43, None, None)[0] == 2853.43
    assert bp.to_pay_candidates(2240.66, 2240.66, 1315.06, None, 875.89)[0] == 2190.95
    assert bp.to_pay_candidates(1682.0, None, None, None, None)[0] == 1682.0
    assert bp.to_pay_candidates(None, None, None, None, None) is None


def test_negative_saldo_payment_goes_to_next_month():
    # Переплата в сальдо погасила начисление, оплата месяца — в счёт следующего: −780 + 780 + 1.56.
    assert 1.56 in bp.to_pay_candidates(-780.0, 2340.0, 780.0, 1.56, None)
    # При положительном сальдо оплата всегда учитывается: третьего способа нет.
    assert bp.to_pay_candidates(100.0, 100.0, 50.0, None, None) == [50.0]


def tiered_item(quantity, accrued):
    tiers = [bp.TariffTier(tier=index, price=price, unit="тг/кВт*ч") for index, price in enumerate((24.81, 33.74, 42.18), 1)]
    return item(quantity=quantity, accrued=accrued, tariffs=tiers, tariff_unit="тг/кВт*ч")


def test_tiers_per_resident_put_volume_into_minimal_tariff():
    settings = {**SETTINGS, "tier_bounds_per_resident": True, "tier_residents_default": 2}
    row = tiered_item(115.0, 2853.43)  # цена в квитанции округлена: 24.8124 → 24.81
    assert bp.check_tariff(row, settings, residents=None) is True
    assert [tier.quantity for tier in row.tariffs] == [115.0, 0.0, 0.0]
    assert row.tariffs[0].name == "Минимальный тариф (до 140 кВт·ч)"
    assert row.tariffs[2].name == "Максимальный тариф (от 280 кВт·ч и выше)"


def test_tiers_per_apartment_split_volume():
    settings = {**SETTINGS, "tier_bounds_per_resident": False}
    row = tiered_item(115.0, 2853.43)
    assert bp.check_tariff(row, settings, residents=2) is False
    assert [tier.quantity for tier in row.tariffs] == [70.0, 45.0, 0.0]
    assert row.expected_accrued == 3255.0


def test_tiers_use_residents_from_header():
    settings = {**SETTINGS, "tier_bounds_per_resident": True, "tier_residents_default": 1}
    row = tiered_item(150.0, 150 * 24.8124)
    assert bp.check_tariff(row, settings, residents=3) is True
    assert bp.tier_bounds(settings, 3)[1] == 3


def test_single_tariff_check():
    row = item(quantity=76.2, accrued=6096.0, tariffs=[bp.TariffTier(tier=1, price=80.0)])
    assert bp.check_tariff(row, SETTINGS, None) is True
    row = item(quantity=2.0, accrued=1274.0, tariffs=[bp.TariffTier(tier=1, price=537.0)])
    assert bp.check_tariff(row, SETTINGS, None) is False


def test_vote_prefers_majority_then_first_scale():
    assert bp._vote(["196.82", "196.83", "196.83"], bp.numbers)[1][0] == (196.83,)
    assert bp._vote(["140.56", "140.55"], bp.numbers)[1][0] == (140.56,)


def test_repair_row_uses_alternative_reading():
    row = item(saldo_start=140.59, paid=140.59, accrued=196.82, to_pay=196.83)
    values = {
        "saldo_start": {"text": "140.59", "variants": [(140.59,)]},
        "paid": {"text": "140.59", "variants": [(140.59,)]},
        "accrued": {"text": "196.82", "variants": [(196.82,), (196.83,)]},
        "to_pay": {"text": "196.83", "variants": [(196.83,)]},
    }
    bp._check_row(row, SETTINGS)
    assert row.row_check_ok is False
    bp._repair_row(row, values, SETTINGS)
    assert row.row_check_ok is True
    assert row.accrued == 196.83
    assert row.raw["repaired"] == {"accrued": [196.82, 196.83]}


HEADER = """Төлеуші/Плательщик
Адам саны/Кол.человек 2
Жалпы ауданы/Общ.пл. 55.50
Жыл.ауданы/Отапл. пл. 54.10
Пайдалы ауданы/Полезн.
12345
05.03.2026 08:15:00
0000123456
АСТАНА ЕРЦ\\У/ Абай Құнанбаев көш., 1 үй, 2 п.
ул. Абая, дом 1, кв. 2
Наурыз/Март 2026
2026 жылдың ақпан айында көрсетілген қызметтер үшін
За услуги, оказанные в феврале 2026
Бақылаушы / Контролер: 1-01 доп.уч.
"""

FOOTER = """Төлеу мерзімі 25.03.2026 ж. дейін./Срок оплаты до 25.03.2026г. 12345.67
Барлығы/Итого
Тел.: 8 700 000 00 00
Дербес шот/Лицевой счет 0000123456
Наурыз/Март 2026 05.03.2026 08:15:00 12345
"""


def test_parse_header_and_footer():
    bill = bp.Bill()
    bp.parse_header(HEADER, bill)
    bp.parse_footer(FOOTER, bill)
    assert bill.account == "0000123456"
    assert bill.period == "2026-03"
    assert bill.service_period == "2026-02"
    assert bill.issued_at == "2026-03-05T08:15:00"
    assert bill.doc_number == "12345"
    assert bill.address == "ул. Абая, дом 1, кв. 2"
    assert bill.address_kz == "Абай Құнанбаев көш., 1 үй, 2 п."
    assert bill.residents == 2
    assert (bill.total_area, bill.heated_area, bill.useful_area) == (55.5, 54.1, None)
    assert bill.controller == "1-01 доп.уч."
    assert bill.payer is None
    assert bill.due_date == "2026-03-25"
    assert bill.total == 12345.67


def test_footer_fills_missing_header_fields():
    bill = bp.Bill()
    bp.parse_footer(FOOTER, bill)
    assert bill.account == "0000123456"
    assert bill.period == "2026-03"
    assert bill.doc_number == "12345"


def test_parse_filename():
    assert bp.parse_filename("A000123456-2026-03.PDF") == ("000123456", "2026-03")
    assert bp.parse_filename("bill.pdf") == (None, None)


def test_crosscheck_filename_fills_period():
    bill = bp.Bill(account="0000123456")
    bp._crosscheck_filename(bill, "A000123456-2026-03.PDF")
    assert bill.period == "2026-03"
    assert any("из имени файла" in note for note in bill.notes)
    assert not bill.warnings


def test_merged_numeric_cell_read_from_text():
    row = item()
    values = {"accrued": {"text": "6096.00", "variants": None}, "to_pay": {"text": "6096.00", "variants": [(6096.0,)]}}
    bp._fill_item(row, values, SETTINGS)
    assert row.accrued == 6096.0 and row.row_check_ok is True


def test_expected_to_pay_with_penalty_only():
    assert bp.to_pay_candidates(None, None, None, 12.5, None)[0] == 12.5


def test_cell_pieces_split_vertical_then_horizontal():
    page, _rows, _cols = synthetic_page(split_accrued_in_row=1)
    grid = bp.find_grid(page, SETTINGS)
    index = bp.COLUMNS.index("accrued")
    pieces = bp.cell_pieces(grid, grid.cols[index], grid.cols[index + 1], grid.rows[1], grid.rows[2], 3)
    # Левая часть на всю высоту — основная строка; правая — «по прибору учета» (0) и «потери» (1).
    assert [piece[4] for piece in pieces] == [0, 0, 1]
    assert pieces[0][3] == grid.rows[2] and pieces[1][3] < grid.rows[2]


def test_sub_row_item_created_even_when_empty():
    page, _rows, _cols = synthetic_page(inner_line_in_row=1)
    _grid, items = bp.parse_table(Image.fromarray(page), SETTINGS, ocr=lambda *_a, **_kw: "")
    assert [(item.position, item.sub_row) for item in items] == [(1, 0), (1, 1), (2, 0), (3, 0)]
    assert items[1].service.endswith("подстрока 1") and items[1].accrued is None


def test_combine_concatenates_pieces_in_order():
    assert bp._combine([[(2240.66,)], [(2240.66,), (2240.65,)]]) == [(2240.66, 2240.66), (2240.66, 2240.65)]
    assert bp._combine([[], [(17.0,)], [(2.0,)]]) == [(17.0, 2.0)]
    assert bp._combine([[], []]) == []


@pytest.mark.parametrize(
    ("text", "service"),
    [
        ('Жылыту (отоп) Жылу энергиясы/ Тепловая энергия "Астана-Теплотранзит"АҚ ЫСЖ (ГВС)', "Отопление"),
        ("ПИК максатты алымдары/ Целевые сборы КСК", "Целевые сборы КСК"),
    ],
)
def test_winter_services(text, service):
    assert bp.canonical_service(text) == service


def test_sub_row_names():
    heating = 'Жылыту (отоп) Тепловая энергия ЫСЖ (ГВС)'
    assert bp._sub_row_service("Отопление", heating, 1) == "Горячая вода (ГВС)"
    assert bp._sub_row_service("Электроэнергия", "ЭЭ Шығындар/ потери", 1) == "Электроэнергия — потери"
    assert bp._sub_row_service("Отопление", heating, 0) == "Отопление"


def test_row_check_uses_accrued_total_of_group():
    head = item(saldo_start=8310.55, paid=8310.55, accrued=10055.10, to_pay=11542.54)
    bp._check_row(head, SETTINGS)
    assert head.row_check_ok is False  # одной подстроки недостаточно
    bp._check_row(head, SETTINGS, round(10055.10 + 1487.44, 2))
    assert head.row_check_ok is True


def test_group_row_not_repaired_individually():
    head = item(saldo_start=8310.55, paid=8310.55, to_pay=11542.54)
    values = {"accrued": {"text": "10055.10", "variants": [(10055.10,), (10055.16,)]}, "to_pay": {"text": "11542.54", "variants": [(11542.54,)]}}
    bp._fill_item(head, values, SETTINGS, group=True)
    assert head.row_check_ok is None and "repaired" not in head.raw


def test_tariff_tolerance_covers_rounded_quantity():
    # 2.770 Гкал напечатано округлённо (фактически 2.7689): 2.77 × 3631.44 = 10059.09 при начислении 10055.10.
    row = item(quantity=2.77, accrued=10055.10, tariffs=[bp.TariffTier(tier=1, price=3631.44)])
    assert bp.check_tariff(row, SETTINGS, None) is True
    row = item(quantity=10.0, accrued=987.70, tariffs=[bp.TariffTier(tier=1, price=8.77)])
    assert bp.check_tariff(row, SETTINGS, None) is False  # потерянная цифра тарифа по-прежнему ловится


def test_parse_table_header_dates():
    bill = bp.Bill()
    bp.parse_table_header("Ай басындағы сальдо Сальдо на 31.05.2023 г. … 06.2023 ж. үшін есептелді Начислено за 06.2023 г.", bill)
    assert (bill.saldo_date, bill.accrual_month) == ("2023-05-31", "2023-06")


@pytest.mark.parametrize("amount", [100.0, 6096.0, 7746.0])
def test_whole_money_amount_is_not_rescaled(amount):
    row = item()
    cell = {"text": f"{amount:.2f}", "variants": [(amount,)]}
    bp._fill_item(row, {"accrued": cell, "to_pay": cell}, SETTINGS)
    assert row.accrued == amount and row.to_pay == amount
    assert row.row_check_ok is True


def test_printed_tiers_preserve_totals_without_inventing_bounds():
    row = tiered_item(0, 0)
    row.tariffs = [bp.TariffTier(tier=n, price=price) for n, price in enumerate((10.0, 20.0, 30.0), 1)]
    bp._apply_printed_tiers(row, {"quantity": [125, 70, 55, 0], "accrued": [1800, 700, 1100, 0]})
    assert row.quantity == 125 and row.accrued == 1800 and row.quantity_extra is None
    assert row.tiers_source == "printed"
    assert [(tier.quantity, tier.amount) for tier in row.tariffs] == [(70, 700), (55, 1100), (0, 0)]
    assert all(tier.from_qty is None and tier.to_qty is None for tier in row.tariffs)
    assert bp.check_tariff(row, SETTINGS, None) is True
    row.quantity = 130
    assert bp.check_tariff(row, SETTINGS, None) is False


def test_format_features_record_grouped_layout_and_ignore_prices():
    page, _rows, _cols = synthetic_page()
    grid = bp.find_grid(page, SETTINGS)
    head = tiered_item(115, 2853.43)
    head.service = "Электроэнергия"
    bp.check_tariff(head, SETTINGS, 2)
    loss = bp.BillItem(position=1, sub_row=1, service="Электроэнергия — потери", service_raw="потери")
    features, signature = bp.format_features((2000, 2400), grid, [head, loss], "первый заголовок")
    assert features["electricity_tiers"] == "computed"
    assert features["services"][0]["sub_rows"] == 2
    assert features["row_bounds_px"] == grid.rows
    head.tariffs[0].price = 99.0
    assert bp.format_features((2000, 2400), grid, [head, loss], "другой заголовок")[1] == signature
    head.tiers_source = "printed"
    assert bp.format_features((2000, 2400), grid, [head, loss], "")[1] != signature


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7746", [77.46]),  # потерянная точка
        ("4572.00", [4572.0]),
        ("4572,50", [4572.5]),
        ("12", [12.0]),  # меньше трёх цифр — не сотые
        ("7746 4572.00 12", [77.46, 4572.0, 12.0]),
        ("", []),
    ],
)
def test_money_numbers_restores_lost_decimal_point(text, expected):
    assert bp.money_numbers(text) == expected


def test_default_settings_for_tariff_erase():
    assert SETTINGS["tariff_line_erase_px"] == [1, 2]


def test_repair_tariff_picks_reading_that_matches_accrued():
    row = item(
        quantity=10.0, accrued=87.7, tariff_value=8.77, tariff_unit="тг/м3", tariff="3.77 тг/м3",
        tariffs=[bp.TariffTier(tier=1, price=3.77)], price_variants=[(3.77,), (8.77,)],
    )
    assert bp.check_tariff(row, SETTINGS, None) is True
    assert row.tariffs[0].price == row.tariff_value == 8.77
    assert row.tariff == "8.77 тг/м3"
    assert row.expected_accrued == 87.7
    assert row.raw["tariff_repaired"] == [3.77, 8.77]


def test_repair_tariff_without_matching_variant_leaves_item_unchanged():
    row = item(
        quantity=10.0, accrued=87.7, tariff_value=3.77,
        tariffs=[bp.TariffTier(tier=1, price=3.77)], price_variants=[(3.77,), (5.00,), (1.0, 2.0)],
    )
    assert bp.check_tariff(row, SETTINGS, None) is False
    assert row.tariffs[0].price == 3.77
    assert "tariff_repaired" not in row.raw


def test_repair_tariff_not_applied_when_price_already_matches():
    row = item(quantity=10.0, accrued=87.7, tariffs=[bp.TariffTier(tier=1, price=8.77)], price_variants=[(3.77,)])
    assert bp.check_tariff(row, SETTINGS, None) is True
    assert "tariff_repaired" not in row.raw


def test_table_header_date_below_labels():
    # Подписи («на»/«за») не требуются: дата стоит строкой ниже подписи.
    bill = bp.Bill()
    bp.parse_table_header("Сальдо\n31.05.2023 г\nНачислено\n06.2023 г.", bill)
    assert (bill.saldo_date, bill.accrual_month) == ("2023-05-31", "2023-06")


def test_table_header_month_is_not_part_of_full_date():
    bill = bp.Bill()
    bp.parse_table_header("31.05.2023", bill)
    assert (bill.saldo_date, bill.accrual_month) == ("2023-05-31", None)
    bill = bp.Bill()
    bp.parse_table_header("текст без дат", bill)
    assert (bill.saldo_date, bill.accrual_month) == (None, None)


def test_footer_pads_short_stub_account_with_zeros():
    bill = bp.Bill()
    bp.parse_footer("Лицевой счет 0123456\n", bill)
    assert bill.account == "0000123456"
    bill = bp.Bill()
    bp.parse_footer("Лицевой счет 0000123456\n", bill)
    assert bill.account == "0000123456"


# ---------------------------------------------------------------------------
# Лимиты для недоверенного PDF и растра
# ---------------------------------------------------------------------------

LISTING_HEADER = (
    "page   num  type   width height color comp bpc  enc interp  object ID x-ppi y-ppi size ratio\n"
    "--------------------------------------------------------------------------------------------\n"
)


def listing(*sizes):
    rows = "".join(f"   1     {index} image    {w}  {h}  gray    1   8  jpeg   no        {index + 8}  0   300   300  237K 6.3%\n"
                   for index, (w, h) in enumerate(sizes))
    return LISTING_HEADER + rows


def test_page_images_within_limits_pass():
    bp.check_page_images(listing((2480, 3508)), SETTINGS)


def test_too_many_page_images_rejected():
    with pytest.raises(bp.BillParseError, match="max_page_images"):
        bp.check_page_images(listing(*[(100, 100)] * 21), SETTINGS)


def test_huge_page_image_rejected():
    with pytest.raises(bp.BillParseError, match="max_page_pixels"):
        bp.check_page_images(listing((10000, 10000)), SETTINGS)


@pytest.mark.parametrize("text", ["", "мусор без разделителя", LISTING_HEADER + "   1   0 image  abc  def\n"])
def test_unexpected_pdfimages_listing_rejected(text):
    with pytest.raises(bp.BillParseError, match="pdfimages -list"):
        bp.check_page_images(text, SETTINGS)


def test_file_without_pdf_signature_rejected_before_pdfimages(tmp_path, monkeypatch):
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"<html>not a pdf</html>")

    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("pdfimages не должен запускаться")

    monkeypatch.setattr(bp.subprocess, "run", no_subprocess)
    with pytest.raises(bp.BillParseError, match="%PDF-"):
        bp.extract_page_image(path, SETTINGS)


def test_too_many_table_rows_rejected():
    page, _rows, _cols = synthetic_page()
    with pytest.raises(bp.BillParseError, match="max_table_rows"):
        bp.find_grid(page, {**SETTINGS, "max_table_rows": 2})


def test_too_many_cell_pieces_rejected():
    page, _rows, _cols = synthetic_page(split_accrued_in_row=1)  # «Начислено» строки 1 — 3 подъячейки
    with pytest.raises(bp.BillParseError, match="max_cell_pieces"):
        bp.parse_table(Image.fromarray(page), {**SETTINGS, "max_cell_pieces": 2}, ocr=lambda *_a, **_kw: "")


@pytest.mark.parametrize("sizes", [[1], [3], [2, 3], [3, 1, 2], [2, 2, 2, 2]])
def test_ranked_combos_match_sorted_product(sizes):
    expected = sorted(itertools.product(*[range(size) for size in sizes]), key=lambda combo: (sum(combo), combo))
    assert list(bp._ranked_combos(sizes)) == expected


def test_combine_is_bounded_for_many_pieces():
    # 30 подъячеек по 6 вариантов: полный перебор — 6^30 сочетаний; результат ограничен и начинается с победителей.
    pieces = [[(float(index),) for index in range(6)] for _ in range(30)]
    result = bp._combine(pieces)
    assert len(result) == bp.MAX_CELL_VARIANTS
    assert result[0] == (0.0,) * 30


def test_repair_row_limits_options_per_column():
    row = item(saldo_start=0.0, paid=0.0, accrued=10.0, to_pay=15.0)
    variants = [(10.0 + index,) for index in range(10)]  # верное 15.0 — шестое прочтение, за лимитом
    values = {"accrued": {"text": "10.00", "variants": variants}, "to_pay": {"text": "15.00", "variants": [(15.0,)]}}
    bp._check_row(row, SETTINGS)
    bp._repair_row(row, values, SETTINGS)
    assert row.row_check_ok is False and row.accrued == 10.0 and "repaired" not in row.raw


def digit_row(accrued, to_pay, price=None):
    tariffs = [bp.TariffTier(tier=1, price=price)] if price is not None else []
    row = item(quantity=14.0, accrued=accrued, to_pay=to_pay, tariffs=tariffs)
    values = {
        "accrued": {"text": f"{accrued:.2f}", "variants": [(accrued,)]},
        "to_pay": {"text": f"{to_pay:.2f}", "variants": [(to_pay,)]},
    }
    bp._check_row(row, SETTINGS)
    bp._repair_row(row, values, SETTINGS)
    return row


def test_repair_row_fixes_last_digit_of_accrued_confirmed_by_tariff():
    # Напечатано 323.79, все прочтения — 323.76; тариф 14 × 23.13 = 323.82 подтверждает 9, а не 6.
    row = digit_row(323.76, 323.79, price=23.13)
    assert row.row_check_ok is True and row.accrued == 323.79
    assert row.raw["repaired"] == {"accrued": [323.76, 323.79]}


def test_repair_row_keeps_digit_without_tariff():
    # Без тарифа подтверждения нет: расхождение остаётся на сверку.
    row = digit_row(323.76, 323.79)
    assert row.row_check_ok is False and row.accrued == 323.76


def test_repair_row_rejects_digit_farther_from_tariff():
    # 14 × 23.12 = 323.68: замена 6 → 9 уводит от тарифа — не применяется.
    row = digit_row(323.76, 323.79, price=23.12)
    assert row.row_check_ok is False and row.accrued == 323.76


# ---------------------------------------------------------------------------
# Причина несовпадения «К оплате»: распознавание или сама квитанция
# ---------------------------------------------------------------------------


def unanimous_values(to_pay):
    return {
        "saldo_start": {"text": "100.00", "variants": [(100.0,)]},
        "paid": {"text": "100.00", "variants": [(100.0,)]},
        "accrued": {"text": "50.00", "variants": [(50.0,)]},
        "to_pay": {"text": f"{to_pay:.2f}", "variants": [(to_pay,)]},
    }


def test_small_mismatch_is_bill_rounding():
    row = item()
    bp._fill_item(row, unanimous_values(50.03), SETTINGS)
    assert row.row_check_ok is False and row.raw["to_pay_mismatch"] == "bill"


def test_small_mismatch_not_explained_by_alternative_readings_is_bill_rounding():
    # Альтернативное прочтение есть, но строку не сводит: перебор исчерпан, разница — в самой квитанции.
    row = item()
    values = unanimous_values(50.03)
    values["accrued"]["variants"] = [(50.0,), (50.01,)]
    bp._fill_item(row, values, SETTINGS)
    assert row.row_check_ok is False and row.raw["to_pay_mismatch"] == "bill"


def test_large_mismatch_is_ocr():
    row = item()
    bp._fill_item(row, unanimous_values(51.00), SETTINGS)
    assert row.row_check_ok is False and row.raw["to_pay_mismatch"] == "ocr"


def test_matching_row_has_no_mismatch_mark():
    row = item()
    bp._fill_item(row, unanimous_values(50.0), SETTINGS)
    assert row.row_check_ok is True and "to_pay_mismatch" not in row.raw
