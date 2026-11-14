"""Тесты движка utility_bills без сети, tesseract и реальных квитанций (поддельный IMAP и разбор)."""

import importlib.util
import json
import os
import sqlite3
import ssl
import stat
import tempfile
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path

import pytest

os.environ.setdefault("MYTASKS_LOG_DIR", tempfile.mkdtemp(prefix="utility_bills_test_logs_"))

MODULE_PATH = Path(__file__).resolve().parents[1] / "utility_bills.py"
SPEC = importlib.util.spec_from_file_location("utility_bills_engine_test", MODULE_PATH)
ub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ub)
bp = ub.bp


def noop(*_args, **_kwargs):
    return None


def write_config(tmp_path, **overrides):
    config = {
        "imap": {"host": "imap.example.test", "username": "user@example.test", "password": "secret", "folder": "Квитанции"},
        "search": {"from": ["noreply@example.test"]},
        "db_path": str(tmp_path / "bills.db"),
    }
    config.update(overrides)
    config["search"].setdefault("require_auth", False)  # проверка DKIM/SPF — в отдельных тестах
    path = tmp_path / "utility_bills.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)  # конфиг с паролем с правами шире 600 отклоняется
    return path


def fake_bill(period="2026-03", total=100.0):
    tier = bp.TariffTier(tier=1, price=10.0, unit="тг/кВт*ч", name="Минимальный тариф (до 140 кВт·ч)", from_qty=0, to_qty=140, quantity=10.0, amount=100.0)
    item = bp.BillItem(
        position=1,
        sub_row=0,
        service="Электроэнергия",
        service_raw="ЭЭ/ Электроэнергия",
        quantity=10.0,
        accrued=total,
        to_pay=total,
        tariffs=[tier],
        tariff_value=10.0,
        raw={"service": "ЭЭ/ Электроэнергия"},
    )
    return bp.Bill(account="0000123456", period=period, total=total, items_sum=total, checksum_ok=True, items=[item])


# ---------------------------------------------------------------------------
# Конфигурация и мелкие функции
# ---------------------------------------------------------------------------


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ub.ConfigError, match="нет файла"):
        ub.load_config(tmp_path / "nope.json", noop)


def test_load_config_bad_type(tmp_path):
    path = write_config(tmp_path, imap={"port": "993"})
    with pytest.raises(ub.ConfigError, match="imap.port"):
        ub.load_config(path, noop)


def test_load_config_resolves_paths_and_warns_unknown(tmp_path):
    warnings = []
    path = write_config(tmp_path, extra=1, ocr={"workers": 2, "bogus": 1}, db_path="bills.db")
    config = ub.load_config(path, lambda message, **_kw: warnings.append(message))
    assert config["db_path"] == ub.BASE_DIR / "bills.db"
    assert config["ocr"] == {"workers": 2}
    assert any("extra" in message for message in warnings)
    assert any("bogus" in message for message in warnings)


def test_config_password_wins_over_environment(tmp_path, monkeypatch):
    config = ub.load_config(write_config(tmp_path), noop)
    monkeypatch.setenv("UTILITY_BILLS_IMAP_PASSWORD", "from-env")
    assert ub.imap_password(config) == "secret"


def test_environment_password_used_when_config_empty(tmp_path, monkeypatch):
    imap = {"host": "imap.example.test", "username": "u", "password": ""}
    config = ub.load_config(write_config(tmp_path, imap=imap), noop)
    monkeypatch.setenv("UTILITY_BILLS_IMAP_PASSWORD", "from-env")
    assert ub.imap_password(config) == "from-env"
    monkeypatch.delenv("UTILITY_BILLS_IMAP_PASSWORD")
    assert ub.imap_password(config) == ""


def test_validate_imap_reports_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("UTILITY_BILLS_IMAP_PASSWORD", raising=False)
    config = ub.load_config(write_config(tmp_path, imap={"host": ""}, search={"from": []}), noop)
    with pytest.raises(ub.ConfigError) as error:
        ub.validate_imap(config)
    assert "host" in str(error.value) and "search.from" in str(error.value)


@pytest.mark.parametrize(
    ("name", "encoded"),
    [("INBOX", "INBOX"), ("Входящие", "&BBIERQQ+BDQETwRJBDgENQ-"), ("A&B", "A&-B"), ("Почта/ЖКУ", "&BB8EPgRHBEIEMA-/&BBYEGgQj-")],
)
def test_imap_utf7(name, encoded):
    assert ub.imap_utf7(name) == encoded


def test_safe_name():
    assert ub.safe_name("../../etc/passwd", "x.pdf") == "passwd"
    assert ub.safe_name("", "x.pdf") == "x.pdf"


def test_safe_name_strips_control_and_bidi_characters():
    assert ub.safe_name("A000123456\x1b[31m-2026-03.PDF", "x.pdf") == "A000123456[31m-2026-03.PDF"
    assert ub.safe_name("bill\u202efdp.exe", "x.pdf") == "billfdp.exe"  # U+202E не «переворачивает» расширение
    assert ub.safe_name("a\nb\x00.pdf", "x.pdf") == "a b.pdf"
    assert ub.safe_name("\x07\x1b", "x.pdf") == "x.pdf"


def test_safe_name_limits_length_and_keeps_suffix():
    name = ub.safe_name("Я" * 300 + ".PDF", "x.pdf")
    assert name.endswith(".PDF")
    stem = name[: -len(".PDF")]
    assert len(stem) <= ub.MAX_NAME_STEM and len(stem.encode("utf-8")) <= ub.MAX_NAME_STEM_BYTES
    assert len(ub.safe_name("a" * 300, "x.pdf")) == ub.MAX_NAME_STEM


def test_display_text_is_single_clean_line():
    assert ub.display_text("Квитанция\r\nFake: line\x1b]0;title\x07") == "Квитанция Fake: line]0;title"
    assert len(ub.display_text("x" * 1000)) == ub.MAX_DISPLAY_CHARS


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------


def test_new_database_has_latest_schema_and_views(tmp_path):
    path = tmp_path / "bills.db"
    ub.migrate_database(path, out=noop)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ub.DB_LATEST
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert set(ub.DB_SCHEMA) <= names
    assert set(ub.DB_VIEWS) <= names


def test_migrate_dry_run_creates_nothing(tmp_path):
    lines = []
    ub.migrate_database(tmp_path / "bills.db", dry_run=True, out=lines.append)
    assert not (tmp_path / "bills.db").exists()
    assert lines and lines[0].startswith("[dry-run] будет:")


def test_migrate_dry_run_on_current_schema_reports_no_changes(tmp_path):
    path = tmp_path / "bills.db"
    ub.migrate_database(path, out=noop)
    lines = []
    ub.migrate_database(path, dry_run=True, out=lines.append)
    assert len(lines) == 1
    assert lines[0].startswith(f"[dry-run] схема БД уже v{ub.DB_LATEST}")
    assert "будет:" not in lines[0]


def test_save_bill_replaces_same_period(tmp_path):
    path = tmp_path / "bills.db"
    ub.migrate_database(path, out=noop)
    db = ub.Database(path)
    meta = {"file_name": "a.pdf", "file_sha256": "1" * 64, "file_size": 10}
    assert db.save_bill(fake_bill(), meta) == "new"
    meta2 = {**meta, "file_sha256": "2" * 64}
    assert db.save_bill(fake_bill(total=200.0), meta2) == "replaced"
    rows = db.conn.execute("SELECT total, file_sha256 FROM bills").fetchall()
    assert [tuple(row) for row in rows] == [(200.0, "2" * 64)]
    assert db.conn.execute("SELECT COUNT(*) FROM bill_items").fetchone()[0] == 1
    tier = db.conn.execute("SELECT name, quantity, amount FROM bill_item_tariffs").fetchone()
    assert tuple(tier) == ("Минимальный тариф (до 140 кВт·ч)", 10.0, 100.0)
    view = db.conn.execute("SELECT service, effective_price FROM v_items").fetchone()
    assert tuple(view) == ("Электроэнергия", 20.0)
    db.close()


def test_read_only_database_without_file(tmp_path):
    db = ub.Database(tmp_path / "missing.db", read_only=True)
    assert db.processed_keys() == set()
    assert db.bill_by_sha("x") is None
    assert not (tmp_path / "missing.db").exists()


# ---------------------------------------------------------------------------
# Почта (поддельный IMAP)
# ---------------------------------------------------------------------------


def make_email(
    subject, message_id, pdf_name="A000123456-2026-03.PDF", pdf=b"%PDF-1.3 fake", sender="ЕРЦ <noreply@example.test>", auth=None
):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Tue, 10 Mar 2026 07:37:03 +0500"
    if message_id:
        message["Message-ID"] = message_id
    for value in auth or []:
        message["Authentication-Results"] = value
    message.set_content("Квитанция во вложении")
    if pdf is not None:
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename=pdf_name)
    return message.as_bytes()


class FakeIMAP:
    """Минимальный IMAP: SELECT, UID SEARCH, UID FETCH заголовков и целого письма."""

    def __init__(self, messages):
        self.messages = messages
        self.search_args = None
        self.untagged_responses = {"UIDVALIDITY": [b"77"]}
        self.selected = None
        self.full_fetches = []

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.search_args = args
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages)]
        uids, spec = args
        response = []
        for uid in (int(value) for value in uids.split(",")):
            data = self.messages[uid]
            size = len(data)
            if "HEADER" in spec:
                data = data.split(b"\n\n", 1)[0] + b"\n\n"
            else:
                self.full_fetches.append(uid)
            response.append((f"{uid} (UID {uid} RFC822.SIZE {size} BODY[] {{{len(data)}}}".encode(), data))
            response.append(b")")
        return "OK", response

    def logout(self):
        return "BYE", []


def run_mail(tmp_path, monkeypatch, fake, dry_run=False, parse=None, **overrides):
    config = ub.load_config(write_config(tmp_path, **overrides), noop)
    monkeypatch.setattr(ub, "connect_imap", lambda _config, _warn=None: fake)
    calls = []

    def fake_parse(path, _settings=None, pdf_name=None, **_kw):
        calls.append(pdf_name)
        return (parse or fake_bill)()

    monkeypatch.setattr(ub.bp, "parse_bill", fake_parse)
    if not dry_run:
        ub.migrate_database(config["db_path"], out=noop)
    say = ub.Reporter()
    with ub.Database(config["db_path"], read_only=dry_run) as db:
        ctx = ub.RunContext(config=config, db=db, say=say, dry_run=dry_run)
        ub.run_mail(ctx)
    return config, ctx.stats, calls, say


def test_mail_imports_matching_messages_and_skips_them_next_time(tmp_path, monkeypatch):
    fake = FakeIMAP(
        {
            1: make_email("Квитанция за март", "<m1@example.test>"),
            2: make_email("Квитанция", "<m2@example.test>", sender="Рассылка <news@other.test>"),
        }
    )
    config, stats, calls, say = run_mail(tmp_path, monkeypatch, fake)
    assert fake.selected == ('"&BBoEMgQ4BEIEMAQ9BEYEOAQ4-"', True)  # только чтение
    assert stats["messages_found"] == 1 and stats["messages_processed"] == 1 and stats["bills_new"] == 1
    assert calls == ["A000123456-2026-03.PDF"]
    with sqlite3.connect(config["db_path"]) as conn:
        assert conn.execute("SELECT file_name, pdf FROM bills").fetchone() == ("A000123456-2026-03.PDF", b"%PDF-1.3 fake")
        assert conn.execute("SELECT key, status, pdf_count FROM messages").fetchall() == [("<m1@example.test>", "processed", 1)]
        assert conn.execute("SELECT message_key, email_subject FROM bills").fetchone() == ("<m1@example.test>", "Квитанция за март")
    assert say.errors == 0

    fake.full_fetches.clear()
    _config, stats, calls, _say = run_mail(tmp_path, monkeypatch, fake)
    assert stats["messages_skipped"] == 1 and stats["messages_processed"] == 0
    assert fake.full_fetches == [] and calls == []


def test_message_without_message_id_uses_uid_key(tmp_path, monkeypatch):
    fake = FakeIMAP({5: make_email("Квитанция", None)})
    config, _stats, _calls, _say = run_mail(tmp_path, monkeypatch, fake)
    with sqlite3.connect(config["db_path"]) as conn:
        assert conn.execute("SELECT key FROM messages").fetchone()[0] == "uid:Квитанции:77:5"


def test_parse_crash_of_one_pdf_does_not_stop_run(tmp_path, monkeypatch):
    fake = FakeIMAP(
        {
            1: make_email("Квитанция 1", "<m1@example.test>", pdf_name="A000123456-2026-02.PDF", pdf=b"%PDF-1.3 one"),
            2: make_email("Квитанция 2", "<m2@example.test>", pdf_name="A000123456-2026-03.PDF", pdf=b"%PDF-1.3 two"),
        }
    )
    bills = iter([ValueError("непредвиденный растр"), fake_bill()])

    def parse():
        result = next(bills)
        if isinstance(result, Exception):
            raise result
        return result

    config, stats, calls, say = run_mail(tmp_path, monkeypatch, fake, parse=parse)
    assert len(calls) == 2 and stats["messages_processed"] == 1 and stats["bills_new"] == 1
    assert say.errors == 1  # сбой разбора
    with sqlite3.connect(config["db_path"]) as conn:
        assert conn.execute("SELECT key FROM messages").fetchall() == [("<m2@example.test>",)]


def big_email(message_id):
    # Вложение 2 МиБ в base64 — около 2,8 МиБ, больше лимита письма при max_attachment_mb = 1 (≈ 2,3 МиБ).
    return make_email("Квитанция", message_id, pdf=b"%PDF-" + b"0" * (2 * 1048576))


SMALL_LIMIT = {"search": {"from": ["noreply@example.test"], "max_attachment_mb": 1}}


def test_too_large_message_skipped_without_fetch(tmp_path, monkeypatch):
    fake = FakeIMAP({1: big_email("<big@example.test>"), 2: make_email("Квитанция", "<m2@example.test>")})
    config, stats, calls, say = run_mail(tmp_path, monkeypatch, fake, **SMALL_LIMIT)
    assert fake.full_fetches == [2] and calls == ["A000123456-2026-03.PDF"]
    with sqlite3.connect(config["db_path"]) as conn:
        rows = conn.execute("SELECT key, status, pdf_count FROM messages ORDER BY key").fetchall()
    assert rows == [("<big@example.test>", "too_large", 0), ("<m2@example.test>", "processed", 1)]
    assert say.errors == 0 and say.warnings == 1 and stats["messages_processed"] == 1

    fake.full_fetches.clear()
    _config, stats, _calls, _say = run_mail(tmp_path, monkeypatch, fake, **SMALL_LIMIT)
    assert stats["messages_skipped"] == 2 and fake.full_fetches == []  # помеченное больше не запрашивается


def test_too_large_message_only_planned_in_dry_run(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP({1: big_email("<big@example.test>")})
    config, _stats, calls, _say = run_mail(tmp_path, monkeypatch, fake, dry_run=True, **SMALL_LIMIT)
    assert fake.full_fetches == [] and calls == []
    assert not config["db_path"].exists()
    assert "[dry-run] будет: пометить письмо обработанным со статусом too_large" in capsys.readouterr().out


def test_message_size_limit():
    assert ub.message_size_limit(3 * 1048576) == 4 * 1048576 + 1048576


def test_subject_and_sender_printed_without_control_characters(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP({1: make_email("Квитанция\x1b[2J\u202e", "<m1@example.test>")})
    run_mail(tmp_path, monkeypatch, fake)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\u202e" not in out and "«Квитанция[2J»" in out


def test_garbage_uidvalidity_does_not_stop_listing(tmp_path):
    fake = FakeIMAP({5: make_email("Квитанция", None)})
    fake.untagged_responses = {"UIDVALIDITY": [b"not-a-number"]}
    config = ub.load_config(write_config(tmp_path), noop)
    assert [message["key"] for message in ub.find_messages(fake, config)] == ["uid:Квитанции:0:5"]


def test_mail_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})
    config, stats, calls, _say = run_mail(tmp_path, monkeypatch, fake, dry_run=True)
    assert calls == ["A000123456-2026-03.PDF"]  # разбор выполняется
    assert stats["bills_new"] == 1
    assert not config["db_path"].exists()
    output = capsys.readouterr().out
    assert "[dry-run] будет: записать квитанцию" in output
    assert "[dry-run] будет: пометить письмо обработанным" in output


def test_main_dry_run_files_creates_nothing(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    pdf = tmp_path / "A000123456-2026-03.PDF"
    pdf.write_bytes(b"%PDF-1.3 fake")
    monkeypatch.setattr(ub.bp, "check_tools", lambda *_a: [])
    monkeypatch.setattr(ub.bp, "parse_bill", lambda *_a, **_kw: fake_bill())
    assert ub.main(["--config", str(path), "--dry-run", "--file", str(pdf)]) == 0
    assert not (tmp_path / "bills.db").exists() and not (tmp_path / "pdf").exists()


def test_main_imports_file_once(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    pdf = tmp_path / "A000123456-2026-03.PDF"
    pdf.write_bytes(b"%PDF-1.3 fake")
    monkeypatch.setattr(ub.bp, "check_tools", lambda *_a: [])
    monkeypatch.setattr(ub.bp, "parse_bill", lambda *_a, **_kw: fake_bill())
    assert ub.main(["--config", str(path), "--file", str(pdf)]) == 0
    assert ub.main(["--config", str(path), "--file", str(pdf)]) == 0
    with sqlite3.connect(tmp_path / "bills.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1
        runs = conn.execute("SELECT bills_new, bills_unchanged FROM runs ORDER BY started").fetchall()
    assert runs[-1] == (0, 1)


def test_main_missing_config_returns_2(tmp_path):
    assert ub.main(["--config", str(tmp_path / "none.json"), "--dry-run"]) == 2


# ---------------------------------------------------------------------------
# Безопасность и устойчивость
# ---------------------------------------------------------------------------


def check_ocr(values):
    warnings = []
    result = ub._check_ocr(values, lambda message, **_kw: warnings.append(message))
    return result, warnings


def test_ocr_rejects_empty_required_list_and_bad_numbers():
    result, warnings = check_ocr({"cell_scales": [], "tariff_line_erase_px": [0, 1], "tier_bounds": [70, "x"]})
    assert result == {}
    assert len(warnings) == 3 and all("неверное значение" in message for message in warnings)


def test_ocr_accepts_nonempty_lists():
    result, warnings = check_ocr({"cell_scales": [2, 1], "tariff_line_erase_px": [1, 3]})
    assert result == {"cell_scales": [2, 1], "tariff_line_erase_px": [1, 3]}
    assert warnings == []


def test_ocr_rejects_out_of_range_numbers():
    values = {"cell_padding_px": -5, "timeout_seconds": 0, "workers": 1000, "dark_threshold": 999, "cell_scales": [50]}
    result, warnings = check_ocr(values)
    assert result == {}
    assert len(warnings) == 5 and all("допустимо" in message for message in warnings)


def test_ocr_accepts_in_range_numbers_and_limits():
    values = {"workers": 4, "timeout_seconds": 120, "max_page_pixels": 80_000_000, "max_table_rows": 60}
    result, warnings = check_ocr(values)
    assert result == values and warnings == []


def test_removed_reserve_setting_is_unknown():
    result, warnings = check_ocr({"reserve_line_erase_px": [2]})
    assert result == {} and any("Неизвестный параметр" in message for message in warnings)


def test_type_ok():
    assert ub._type_ok(True, False)
    assert not ub._type_ok(True, 1)
    assert not ub._type_ok(1, True)
    assert ub._type_ok(0.5, 1)
    assert not ub._type_ok(1, 1.5)


def test_imap_ssl_verifies_certificate(tmp_path, monkeypatch):
    seen = {}

    class FakeSSL:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            seen["context"] = ssl_context

        def login(self, user, password):
            seen["login"] = user

    monkeypatch.setattr(ub.imaplib, "IMAP4_SSL", FakeSSL)
    ub.connect_imap(ub.load_config(write_config(tmp_path), noop))
    context = seen["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_imap_by_ip_encrypts_without_certificate_check(tmp_path, monkeypatch):
    seen, warnings = {}, []

    class FakeSSL:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            seen["context"] = ssl_context

        def login(self, user, password):
            return "OK", []

    monkeypatch.setattr(ub.imaplib, "IMAP4_SSL", FakeSSL)
    imap = {"host": "10.0.0.5", "username": "u", "password": "p"}
    ub.connect_imap(ub.load_config(write_config(tmp_path, imap=imap), noop), lambda message, **_kw: warnings.append(message))
    assert seen["context"].verify_mode == ssl.CERT_NONE and not seen["context"].check_hostname
    assert any("не проверяется" in message for message in warnings)


@pytest.mark.parametrize(("host", "expected"), [("10.0.0.5", True), ("::1", True), ("[::1]", False), ("mail.example.test", False)])
def test_is_ip_address(host, expected):
    assert ub.is_ip_address(host) is expected


@pytest.mark.parametrize("host", ["[::1]", "[fd00::5]", "fd00::5]"])
def test_ipv6_host_without_brackets(tmp_path, host):
    with pytest.raises(ub.ConfigError, match="без квадратных скобок"):
        ub.load_config(write_config(tmp_path, imap={"host": host}), noop)


@pytest.mark.parametrize("host", ["8.8.8.8", "2001:4860:4860::8888"])
def test_public_ip_host_rejected(tmp_path, host):
    with pytest.raises(ub.ConfigError, match="публичный IP"):
        ub.load_config(write_config(tmp_path, imap={"host": host}), noop)


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.10", "fd00::5", "::1", "mail.example.test"])
def test_private_ip_and_name_accepted(tmp_path, host):
    assert ub.load_config(write_config(tmp_path, imap={"host": host}), noop)["imap"]["host"] == host


def test_failed_login_closes_connection(tmp_path, monkeypatch):
    seen = {}

    class FailingSSL:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            seen["open"] = True

        def login(self, user, password):
            raise ub.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

        def shutdown(self):
            seen["closed"] = True

    monkeypatch.setattr(ub.imaplib, "IMAP4_SSL", FailingSSL)
    with pytest.raises(ub.imaplib.IMAP4.error):
        ub.connect_imap(ub.load_config(write_config(tmp_path), noop))
    assert seen == {"open": True, "closed": True}


@pytest.mark.parametrize("host", ["https://mail.example.test/", "mail.example.test:993", "mail example"])
def test_host_must_be_plain_name(tmp_path, host):
    with pytest.raises(ub.ConfigError, match="без https://"):
        ub.load_config(write_config(tmp_path, imap={"host": host}), noop)


def test_plain_imap_only_for_localhost(tmp_path):
    imap = {"host": "imap.example.test", "username": "u", "password": "p", "security": "none"}
    config = ub.load_config(write_config(tmp_path, imap=imap), noop)
    with pytest.raises(ub.ConfigError, match="localhost"):
        ub.validate_imap(config)
    config["imap"]["host"] = "localhost"
    ub.validate_imap(config)


def test_config_with_password_must_be_private(tmp_path):
    path = write_config(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ub.ConfigError, match="chmod 600"):
        ub.load_config(path, noop)
    path.chmod(0o600)
    ub.load_config(path, noop)


def test_config_without_password_may_be_readable(tmp_path):
    path = write_config(tmp_path, imap={"host": "imap.example.test", "username": "u", "password": ""})
    path.chmod(0o644)
    ub.load_config(path, noop)


def test_config_with_password_of_other_owner_rejected(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    owner = path.stat().st_uid
    monkeypatch.setattr(ub.os, "getuid", lambda: owner + 1)
    with pytest.raises(ub.ConfigError, match="другому пользователю"):
        ub.load_config(path, noop)


def test_main_restores_umask_and_writes_private_log(tmp_path):
    previous = os.umask(0o022)
    try:
        assert ub.main(["--config", str(tmp_path / "none.json"), "--dry-run"]) == 2
        assert os.umask(0o022) == 0o022  # маска процесса возвращена
    finally:
        os.umask(previous)
    newest = max(ub.log_dir().glob(f"{ub.LOG_NAME}-*.jsonl"), key=lambda path: path.stat().st_mtime)
    assert stat.S_IMODE(newest.stat().st_mode) == 0o600


def test_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        ub.build_parser().parse_args(["--reparse", "--check"])


def test_uid_parsed_when_it_follows_body(tmp_path):
    """UID после литерала imaplib отдаёт отдельным элементом bytes, а не в кортеже."""

    class LateUID(FakeIMAP):
        def uid(self, command, *args):
            if command != "FETCH":
                return super().uid(command, *args)
            data = self.messages[42].split(b"\n\n", 1)[0] + b"\n\n"
            return "OK", [(b"9 (BODY[HEADER.FIELDS (SUBJECT)] {%d}" % len(data), data), b" UID 42)"]

    fake = LateUID({42: make_email("Квитанция", "<late@example.test>")})
    config = ub.load_config(write_config(tmp_path), noop)
    assert [message["uid"] for message in ub.find_messages(fake, config)] == [42]


def test_fetch_parts_joins_trailing_attributes():
    response = [(b"1 (BODY[] {3}", b"abc"), b" UID 7)", (b"2 (UID 8 BODY[] {1}", b"x"), b")"]
    assert ub._fetch_parts(response) == [(b"1 (BODY[] {3}  UID 7)", b"abc"), (b"2 (UID 8 BODY[] {1} )", b"x")]


def test_save_bill_resolves_conflict_on_both_unique_keys(tmp_path):
    path = tmp_path / "bills.db"
    ub.migrate_database(path, out=noop)
    with ub.Database(path) as db:
        meta = {"file_name": "a.pdf", "file_size": 10}
        db.save_bill(fake_bill(period="2026-02"), {**meta, "file_sha256": "a" * 64})
        db.save_bill(fake_bill(period="2026-03"), {**meta, "file_sha256": "b" * 64})
        # Повторный разбор файла «a» дал месяц 2026-03: конфликт и по SHA, и по месяцу.
        assert db.save_bill(fake_bill(period="2026-03"), {**meta, "file_sha256": "a" * 64}) == "replaced"
        rows = db.conn.execute("SELECT period, file_sha256 FROM bills").fetchall()
    assert [tuple(row) for row in rows] == [("2026-03", "a" * 64)]


def test_database_is_private(tmp_path):
    db_path = tmp_path / "bills.db"
    ub.migrate_database(db_path, out=noop)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_large_attachment_skipped():
    big = b"%PDF-" + b"0" * (2 * 1048576)
    fake = FakeIMAP({1: make_email("Квитанция", "<big@example.test>", pdf=big)})
    warnings = []
    content = ub.fetch_message(fake, 1, "*.pdf", 1048576, lambda message, **_kw: warnings.append(message))
    assert content.pdfs == []
    assert content.attachments[0]["parsed"] is False and content.attachments[0]["size"] > 1048576
    assert any("больше лимита" in message for message in warnings)


def test_max_attachment_must_be_positive(tmp_path):
    with pytest.raises(ub.ConfigError, match="max_attachment_mb"):
        ub.load_config(write_config(tmp_path, search={"from": ["noreply@example.test"], "max_attachment_mb": 0}), noop)


def test_db_error_does_not_stop_run(tmp_path, monkeypatch):
    fake = FakeIMAP(
        {
            1: make_email("Квитанция 1", "<m1@example.test>", pdf_name="A000123456-2026-02.PDF", pdf=b"%PDF-one"),
            2: make_email("Квитанция 2", "<m2@example.test>", pdf_name="A000123456-2026-03.PDF", pdf=b"%PDF-two"),
        }
    )
    original = ub.Database.save_bill
    calls = {"n": 0}

    def flaky(self, bill, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(self, bill, meta)

    monkeypatch.setattr(ub.Database, "save_bill", flaky)
    config, stats, _calls, say = run_mail(tmp_path, monkeypatch, fake)
    assert say.errors == 1 and stats["messages_processed"] == 1
    with sqlite3.connect(config["db_path"]) as conn:
        assert conn.execute("SELECT key FROM messages").fetchall() == [("<m2@example.test>",)]


# ---------------------------------------------------------------------------
# Сверка: несошедшиеся и неразобранные квитанции сохраняются на диск
# ---------------------------------------------------------------------------


def bill_with_mismatch():
    bill = fake_bill()
    bill.total, bill.checksum_ok = 150.0, False
    bill.warnings = ["сумма колонки «К оплате» 100.00 не равна «Итого» 150.00: проверьте распознавание"]
    return bill


def test_mismatch_flags_bill_for_review(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})
    config, stats, _calls, say = run_mail(tmp_path, monkeypatch, fake, parse=bill_with_mismatch)
    assert stats["bills_review"] == 1 and stats["bills_new"] == 1
    with sqlite3.connect(config["db_path"]) as conn:
        row = conn.execute("SELECT needs_review, review_issue, review_result, warnings, pdf FROM bills").fetchone()
    assert row[:3] == (1, None, None)
    assert json.loads(row[3]) == bill_with_mismatch().warnings
    assert row[4] == b"%PDF-1.3 fake"  # PDF для сверки — в самой БД, отдельной копии нет
    assert "требует сверки" in capsys.readouterr().out
    assert say.errors == 0 and say.warnings >= 2


def test_failed_parse_leaves_message_for_retry_and_pdf_in_archive(tmp_path, monkeypatch):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})

    def broken():
        raise bp.BillParseError("неожиданная структура таблицы")

    config, stats, _calls, say = run_mail(tmp_path, monkeypatch, fake, parse=broken)
    assert stats["messages_processed"] == 0
    with sqlite3.connect(config["db_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 0
    assert say.errors == 1  # не разобран


def test_clean_bill_not_flagged_for_review(tmp_path, monkeypatch):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})
    config, stats, _calls, _say = run_mail(tmp_path, monkeypatch, fake)
    assert stats["bills_review"] == 0
    with sqlite3.connect(config["db_path"]) as conn:
        assert conn.execute("SELECT needs_review FROM bills").fetchone()[0] == 0


def test_dry_run_reports_review_without_writing(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})
    _config, stats, _calls, _say = run_mail(tmp_path, monkeypatch, fake, dry_run=True, parse=bill_with_mismatch)
    assert stats["bills_review"] == 1
    assert "требует сверки" in capsys.readouterr().out
    assert not (tmp_path / "bills.db").exists() and not (tmp_path / "pdf").exists()


def saved_review(db, bill, sha):
    meta = {"file_name": "a.pdf", "file_sha256": sha, "file_size": 10}
    db.save_bill(bill, meta)
    return tuple(db.conn.execute("SELECT needs_review, review_issue, review_result FROM bills").fetchone())


def test_review_result_survives_reparse_with_same_warnings(tmp_path):
    path = tmp_path / "bills.db"
    ub.migrate_database(path, out=noop)
    with ub.Database(path) as db:
        assert saved_review(db, bill_with_mismatch(), "1" * 64) == (1, None, None)
        with db.conn:
            db.conn.execute("UPDATE bills SET needs_review = 0, review_issue = 'округление ЕРЦ', review_result = 'верно'")
        # Тот же разбор (--reparse): итог сверки и снятый флаг сохраняются.
        assert saved_review(db, bill_with_mismatch(), "1" * 64) == (0, "округление ЕРЦ", "верно")
        # Иные предупреждения — иной разбор: флаг ставится снова, прежний итог остаётся для истории.
        changed = bill_with_mismatch()
        changed.warnings = ["строка 1 «Электроэнергия»: начислено 100.0, а по тарифу 110.00 за 10 кВт*ч"]
        assert saved_review(db, changed, "2" * 64) == (1, "округление ЕРЦ", "верно")


def test_migration_3_turns_review_copy_into_flag_and_drops_file_path(tmp_path):
    path = tmp_path / "bills.db"
    ub.migrate_database(path, out=noop)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("ALTER TABLE bills ADD COLUMN review_path TEXT")
        conn.execute("ALTER TABLE bills ADD COLUMN file_path TEXT")
        conn.execute("INSERT INTO bills (account, period, review_path) VALUES ('0000123456', '2026-02', 'pdf/review/a.PDF')")
        conn.execute("INSERT INTO bills (account, period) VALUES ('0000123456', '2026-03')")
        conn.execute("PRAGMA user_version = 2")
    ub.migrate_database(path, out=noop)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ub.DB_LATEST
        assert {"review_path", "file_path"}.isdisjoint(row[1] for row in conn.execute("PRAGMA table_info(bills)"))
        assert conn.execute("SELECT period, needs_review FROM bills ORDER BY period").fetchall() == [("2026-02", 1), ("2026-03", 0)]


# ---------------------------------------------------------------------------
# Справочник тарифов с историей
# ---------------------------------------------------------------------------


def tariff_bill(month, price, total=100.0):
    bill = fake_bill(period=month, total=total)
    bill.service_period = month
    bill.items[0].tariffs = [bp.TariffTier(tier=1, price=price, unit="тг/кВт*ч", label="Минимальный тариф (до 70 кВт·ч на жильца)")]
    return bill


def save(db, tmp_path, bill, sha):
    meta = {"file_name": f"{sha}.pdf", "file_sha256": sha * 64, "file_size": 1}
    return db.save_bill(bill, meta)


def tariff_rows(db):
    return [
        tuple(row)
        for row in db.conn.execute(
            "SELECT service, tier, tier_name, price, valid_from, valid_to, is_current, bills_count FROM tariffs ORDER BY valid_from"
        )
    ]


def test_tariff_history_built_in_any_import_order(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        save(db, tmp_path, tariff_bill("2026-08", 24.81), "a")
        save(db, tmp_path, tariff_bill("2026-06", 20.37), "b")  # архив задним числом
        save(db, tmp_path, tariff_bill("2026-07", 24.81), "c")
        name = "Минимальный тариф (до 70 кВт·ч на жильца)"
        assert tariff_rows(db) == [
            ("Электроэнергия", 1, name, 20.37, "2026-06", "2026-06", 0, 1),
            ("Электроэнергия", 1, name, 24.81, "2026-07", "2026-08", 1, 2),
        ]
        changes = db.tariff_changes("2026-07")
        assert [(row["old_value"], row["value"], row["old_to"]) for row in changes] == [(20.37, 24.81, "2026-06")]
        assert db.tariff_changes("2026-08") == []
        current = db.conn.execute("SELECT price, valid_from FROM v_tariffs_current").fetchall()
        assert [tuple(row) for row in current] == [(24.81, "2026-07")]


def test_tariff_history_follows_replaced_bill(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        save(db, tmp_path, tariff_bill("2026-07", 24.81), "a")
        save(db, tmp_path, tariff_bill("2026-08", 24.81), "b")
        save(db, tmp_path, tariff_bill("2026-08", 30.00), "c")  # исправленная квитанция за тот же месяц
        assert [(row[3], row[4], row[5], row[6]) for row in tariff_rows(db)] == [
            (24.81, "2026-07", "2026-07", 0),
            (30.0, "2026-08", "2026-08", 1),
        ]


def test_manual_tariffs_survive_rebuild(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        db.conn.execute(
            "INSERT INTO tariffs (service, tier, price, valid_from, source) VALUES ('Отопление', 1, 150.0, '2026-10', 'manual')"
        )
        db.conn.commit()
        save(db, tmp_path, tariff_bill("2026-07", 24.81), "a")
        sources = db.conn.execute("SELECT service, source FROM tariffs ORDER BY service").fetchall()
        assert [tuple(row) for row in sources] == [("Отопление", "manual"), ("Электроэнергия", "bill")]


def test_tariff_change_is_logged(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    monkeypatch.setattr(ub.bp, "check_tools", lambda *_a: [])
    bills = iter([tariff_bill("2026-06", 20.37), tariff_bill("2026-07", 24.81)])
    monkeypatch.setattr(ub.bp, "parse_bill", lambda *_a, **_kw: next(bills))
    for index, month in enumerate(("06", "07")):
        pdf = tmp_path / f"A000123456-2026-{month}.PDF"
        pdf.write_bytes(f"%PDF {index}".encode())
        assert ub.main(["--config", str(path), "--file", str(pdf)]) == 0
    assert "Тариф изменился: Электроэнергия, Минимальный тариф (до 70 кВт·ч на жильца): 20.37 → 24.81" in capsys.readouterr().out


def quantity_bill(month, quantity, extra=None, metered=False):
    bill = tariff_bill(month, 80.0)
    item = bill.items[0]
    item.service, item.quantity, item.quantity_extra, item.tariff_unit = "Эксплуатационные расходы КСК", quantity, extra, "тг/м2"
    if metered:
        item.prev_reading, item.curr_reading = 0.0, quantity
    return bill


def quantity_rows(db):
    return [
        tuple(row)
        for row in db.conn.execute(
            "SELECT kind, value, unit, normative, valid_from, valid_to, is_current, bills_count FROM quantities ORDER BY kind, valid_from"
        )
    ]


def test_quantity_history(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        save(db, tmp_path, quantity_bill("2026-08", 80.0, extra=2.0), "c")
        save(db, tmp_path, quantity_bill("2026-06", 76.2, extra=2.0), "a")
        save(db, tmp_path, quantity_bill("2026-07", 76.2, extra=2.0), "b")
        assert quantity_rows(db) == [
            ("quantity", 76.2, "м2", 1, "2026-06", "2026-07", 0, 2),
            ("quantity", 80.0, "м2", 1, "2026-08", "2026-08", 1, 1),
            ("quantity_extra", 2.0, None, 1, "2026-06", "2026-08", 1, 3),
        ]
        changes = db.quantity_changes("2026-08")
        assert [(row["kind"], row["old_value"], row["value"]) for row in changes] == [("quantity", 76.2, 80.0)]
        current = db.conn.execute("SELECT kind, value FROM v_quantities_current").fetchall()
        assert [tuple(row) for row in current] == [("quantity", 80.0), ("quantity_extra", 2.0)]


def test_extra_number_of_metered_service_is_normative(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        save(db, tmp_path, quantity_bill("2026-06", 16.0, extra=2.0, metered=True), "a")
        assert [(row[0], row[3]) for row in quantity_rows(db)] == [("quantity", 0), ("quantity_extra", 1)]


def test_volume_quantity_changes_not_reported(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        save(db, tmp_path, quantity_bill("2026-06", 16.0, metered=True), "a")
        save(db, tmp_path, quantity_bill("2026-07", 17.0, metered=True), "b")
        assert [row[3] for row in quantity_rows(db)] == [0, 0]  # история есть, но это объём, не норматив
        assert db.quantity_changes("2026-07") == []  # и изменение — не событие


def test_odn_volume_without_meter_is_not_normative(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        for sha, (month, value) in zip("ab", (("2026-06", 3.0), ("2026-07", 5.0)), strict=True):
            bill = quantity_bill(month, value)
            bill.items[0].tariff_unit = "тг/кВт*ч"
            save(db, tmp_path, bill, sha)
        assert {row[3] for row in quantity_rows(db)} == {0}
        assert db.quantity_changes("2026-07") == []


def test_missing_value_in_billed_month_breaks_period(tmp_path):
    ub.migrate_database(tmp_path / "bills.db", out=noop)
    with ub.Database(tmp_path / "bills.db") as db:
        save(db, tmp_path, quantity_bill("2026-06", 3.0), "a")
        save(db, tmp_path, quantity_bill("2026-07", None), "b")  # квитанция есть, количества нет
        save(db, tmp_path, quantity_bill("2026-08", 3.0), "c")
        save(db, tmp_path, quantity_bill("2026-10", 3.0), "d")  # за сентябрь квитанции нет — не разрыв
        periods = [(row[4], row[5]) for row in quantity_rows(db)]
        assert periods == [("2026-06", "2026-06"), ("2026-08", "2026-10")]


@pytest.mark.parametrize(
    ("tariff_unit", "unit"), [("тг/м2", "м2"), ("тг/кВт*ч", "кВт*ч"), ("м3", "м3"), ("тг", None), (None, None)]
)
def test_quantity_unit(tariff_unit, unit):
    assert bp.quantity_unit(tariff_unit) == unit


# ---------------------------------------------------------------------------
# Фильтр отправителя: email целиком
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("ЕРЦ <noreply@erc.example.kz>", True),
        ("NoReply@ERC.example.kz", True),
        ("=?utf-8?b?0JXQoNCm?= <noreply@erc.example.kz>", True),
        ("Кто-то <xnoreply@erc.example.kz>", False),
        ("Подделка <noreply@erc.example.kz.evil.test>", False),
        ("noreply@erc.example.kz <attacker@evil.test>", False),
        ("evil@evil.test, noreply@erc.example.kz", False),  # в From ровно один адрес
        ("noreply@erc.example.kz, noreply@erc.example.kz", False),
        ("", False),
    ],
)
def test_sender_matches_whole_address(header, expected):
    addresses = ub._sender_addresses(["noreply@erc.example.kz"])
    assert ub.sender_matches(header, addresses) is expected


def test_sender_filter_list_and_empty():
    assert ub.sender_matches("a@b.kz", ub._sender_addresses(["x@y.kz", "A@B.kz"]))
    assert not ub.sender_matches("кто угодно <z@z.kz>", ub._sender_addresses([]))  # пустой фильтр — никто


def test_sender_filter_rejects_non_email(tmp_path):
    with pytest.raises(ub.ConfigError, match="не email"):
        ub.load_config(write_config(tmp_path, search={"from": ["erc.kz"]}), noop)


def test_mail_selects_only_listed_senders(tmp_path):
    """Отбор только по email отправителя; тема не учитывается."""
    fake = FakeIMAP(
        {
            1: make_email("Квитанция", "<m1@example.test>"),
            2: make_email("Новости", "<m2@example.test>"),
            3: make_email("Квитанция", "<m3@example.test>", sender="ТОО <info@other.test>"),
            4: make_email("Квитанция", "<m4@example.test>", sender="Чужой <spam@evil.test>"),
        }
    )

    def selected(senders):
        config = ub.load_config(write_config(tmp_path, search={"from": senders}), noop)
        return [message["uid"] for message in ub.find_messages(fake, config)]

    assert selected(["NOREPLY@example.test"]) == [1, 2]
    assert selected(["noreply@example.test", "info@other.test"]) == [1, 2, 3]
    assert selected(["nobody@example.test"]) == []


@pytest.mark.parametrize(
    ("search", "message"), [({"from": "noreply@example.test"}, r'например \["noreply@example.test"\]'), ({"from": 5}, "массив строк")]
)
def test_from_must_be_array(tmp_path, search, message):
    with pytest.raises(ub.ConfigError, match=message):
        ub.load_config(write_config(tmp_path, search=search), noop)


def test_json_syntax_error_has_hint(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("""{"search": {"from": 'info@example.test'}}""", encoding="utf-8")
    with pytest.raises(ub.ConfigError, match="двойных кавычках"):
        ub.load_config(path, noop)

# ---------------------------------------------------------------------------
# Сначала полный список писем, затем обработка новых (без ограничения по дням)
# ---------------------------------------------------------------------------


def test_find_messages_searches_all_time_by_senders(tmp_path):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})
    config = ub.load_config(write_config(tmp_path), noop)
    ub.find_messages(fake, config)
    # Все письма папки, без SINCE и без серверного FROM (почтовый сервер отвечал на FROM пустым списком).
    assert fake.search_args == (None, "ALL")


def test_list_printed_before_processing(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP(
        {
            1: make_email("Квитанция 1", "<m1@example.test>", pdf_name="A000123456-2026-02.PDF", pdf=b"%PDF-a"),
            2: make_email("Квитанция 2", "<m2@example.test>", pdf_name="A000123456-2026-03.PDF", pdf=b"%PDF-b"),
        }
    )
    run_mail(tmp_path, monkeypatch, fake)  # первый прогон: оба письма новые
    out = capsys.readouterr().out
    assert out.index("Список писем") < out.index("[1/2] Письмо")
    run_mail(tmp_path, monkeypatch, fake)
    out = capsys.readouterr().out
    assert "всего 2, уже обработаны 2, новых 0" in out and out.count("[обработано]") == 2 and "Письмо «" not in out


def test_message_with_two_from_headers_not_selected(tmp_path):
    raw = make_email("Квитанция", "<m1@example.test>")
    raw = raw.replace(b"From: ", b"From: Evil <evil@evil.test>\nFrom: ", 1)
    fake = FakeIMAP({1: raw})
    config = ub.load_config(write_config(tmp_path), noop)
    assert ub.find_messages(fake, config) == []


# ---------------------------------------------------------------------------
# В базе — всё: PDF, письмо целиком по метаданным, даты заголовка таблицы
# ---------------------------------------------------------------------------


def test_fetch_message_collects_body_and_all_attachments():
    message = EmailMessage()
    message["From"] = "ЕРЦ <noreply@example.test>"
    message["Subject"] = "Квитанция"
    message.set_content("Уважаемый абонент, квитанция во вложении.")
    message.add_attachment(b"%PDF-one", maintype="application", subtype="pdf", filename="A000123456-2026-03.PDF")
    message.add_attachment(b"PNG", maintype="image", subtype="png", filename="logo.png")
    fake = FakeIMAP({7: message.as_bytes()})
    content = ub.fetch_message(fake, 7, "*.pdf", 1048576, noop)
    assert content.body_text.strip() == "Уважаемый абонент, квитанция во вложении."
    assert [(item["name"], item["content_type"], item["parsed"]) for item in content.attachments] == [
        ("A000123456-2026-03.PDF", "application/pdf", True),
        ("logo.png", "image/png", False),
    ]
    assert content.pdfs == [("A000123456-2026-03.PDF", b"%PDF-one")]


def test_everything_is_stored_in_database(tmp_path, monkeypatch):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})

    def bill():
        result = fake_bill()
        result.saldo_date, result.accrual_month, result.table_header_text = "2026-02-28", "2026-03", "Сальдо на 28.02.2026 г."
        return result

    config, _stats, _calls, _say = run_mail(tmp_path, monkeypatch, fake, parse=bill)
    with sqlite3.connect(config["db_path"]) as conn:
        pdf, saldo_date, accrual_month = conn.execute("SELECT pdf, saldo_date, accrual_month FROM bills").fetchone()
        message = conn.execute("SELECT recipients, size, attachments, body_text, headers FROM messages").fetchone()
    assert pdf == b"%PDF-1.3 fake"  # сам PDF — в базе
    assert (saldo_date, accrual_month) == ("2026-02-28", "2026-03")
    recipients, size, attachments, body_text, headers = message
    assert size > 0 and "Message-ID: <m1@example.test>" in headers
    assert json.loads(attachments)[0]["name"] == "A000123456-2026-03.PDF"
    assert "Квитанция во вложении" in body_text


# ---------------------------------------------------------------------------
# Подлинность письма: Authentication-Results (RFC 8601)
# ---------------------------------------------------------------------------

AUTH = {"search": {"from": ["noreply@example.test"], "require_auth": True, "authserv_id": "mail.example.kz"}}


def auth_run(tmp_path, monkeypatch, auth, **overrides):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>", auth=auth)})
    _config, stats, _calls, say = run_mail(tmp_path, monkeypatch, fake, **(overrides or AUTH))
    return fake, stats, say


def test_auth_dkim_pass_accepts_message(tmp_path, monkeypatch):
    header = "mail.example.kz; dkim=pass (2048-bit key) header.d=example.test header.i=@example.test; spf=none"
    fake, stats, say = auth_run(tmp_path, monkeypatch, [header])
    assert stats["messages_processed"] == 1 and say.warnings == 0 and fake.full_fetches == [1]


def test_auth_spf_pass_aligned_with_parent_domain_and_case_insensitive_id(tmp_path, monkeypatch):
    # From — noreply@example.test; smtp.mailfrom в том же домене; authserv-id сравнивается без учёта регистра.
    header = "MAIL.Example.KZ 1; dkim=fail; spf=pass smtp.mailfrom=bounce@example.test"
    _fake, stats, _say = auth_run(tmp_path, monkeypatch, [header])
    assert stats["messages_processed"] == 1


def test_auth_spf_pass_in_subdomain_is_not_aligned(tmp_path, monkeypatch):
    header = "mail.example.kz; spf=pass smtp.mailfrom=bounce@mail.example.test"
    _fake, stats, _say = auth_run(tmp_path, monkeypatch, [header])
    assert stats["messages_found"] == 0


def test_auth_fail_skips_with_warning_without_personal_data(tmp_path, monkeypatch):
    fake, stats, say = auth_run(tmp_path, monkeypatch, ["mail.example.kz; dkim=fail header.d=example.test; spf=softfail smtp.mailfrom=example.test"])
    assert stats["messages_found"] == 0 and fake.full_fetches == [] and say.warnings == 1


def test_auth_foreign_authserv_id_ignored(tmp_path, monkeypatch):
    fake, stats, _say = auth_run(tmp_path, monkeypatch, ["evil.example.net; dkim=pass header.d=example.test"])
    assert stats["messages_found"] == 0 and fake.full_fetches == []


def test_auth_missing_header_rejected(tmp_path, monkeypatch):
    _fake, stats, say = auth_run(tmp_path, monkeypatch, None)
    assert stats["messages_found"] == 0 and say.warnings == 1


def test_auth_not_rechecked_for_processed_message(tmp_path, monkeypatch):
    # Уже обработанное письмо прежнего сервера без Authentication-Results: в списке, без предупреждения.
    fake = FakeIMAP({1: make_email("Квитанция", "<old@example.test>", auth=None)})
    config = ub.load_config(write_config(tmp_path, **AUTH), noop)
    warnings = []
    found = ub.find_messages(fake, config, lambda message, **_kw: warnings.append(message), {"<old@example.test>"})
    assert [message["key"] for message in found] == ["<old@example.test>"] and warnings == []
    assert ub.find_messages(fake, config, lambda message, **_kw: warnings.append(message)) == [] and len(warnings) == 1


def test_auth_domain_mismatch_rejected(tmp_path, monkeypatch):
    _fake, stats, _say = auth_run(tmp_path, monkeypatch, ["mail.example.kz; dkim=pass header.d=evil.test; spf=pass smtp.mailfrom=evil.test"])
    assert stats["messages_found"] == 0


def test_auth_not_required_accepts_unsigned_message(tmp_path, monkeypatch):
    _fake, stats, _say = auth_run(tmp_path, monkeypatch, None, search={"from": ["noreply@example.test"], "require_auth": False})
    assert stats["messages_processed"] == 1


def test_auth_required_without_authserv_id_is_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("UTILITY_BILLS_IMAP_PASSWORD", raising=False)
    config = ub.load_config(write_config(tmp_path, search={"from": ["noreply@example.test"], "require_auth": True}), noop)
    with pytest.raises(ub.ConfigError, match="authserv_id"):
        ub.validate_imap(config)


def test_parse_authentication_results_handles_comments_and_quotes():
    server, results = ub.parse_authentication_results(
        'Mail.Example.KZ (internal; relay) 1 ; dkim=pass (ok; signature) header.d="example.test" ; spf=pass smtp.mailfrom=a@example.test'
    )
    assert server == "mail.example.kz"
    assert results[0]["dkim"] == "pass" and results[0]["header.d"] == "example.test" and results[1]["spf"] == "pass"


def test_sender_authenticated_rejects_public_suffix_alignment():
    assert not ub.sender_authenticated(["mail.example.kz; dkim=pass header.d=kz"], "mail.example.kz", "example.kz")
    assert ub.sender_authenticated(["mail.example.kz; dkim=pass header.i=user@example.kz"], "mail.example.kz", "sub.example.kz")


# ---------------------------------------------------------------------------
# Персональные данные в логах, размер письма, сигнатура PDF, миграция
# ---------------------------------------------------------------------------


def test_logs_mask_account_and_hide_username(tmp_path, monkeypatch, capsys):
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>")})
    run_mail(tmp_path, monkeypatch, fake)
    out = capsys.readouterr().out
    assert "0000123456" not in out and "000123456" not in out and "user@example.test" not in out
    assert "***3456" in out


def test_message_without_size_is_skipped_with_warning(tmp_path, monkeypatch):
    class NoSize(FakeIMAP):
        def uid(self, command, *args):
            status, response = super().uid(command, *args)
            return status, [(item[0].replace(b" RFC822.SIZE", b" X"), item[1]) if isinstance(item, tuple) else item for item in response]

    fake = NoSize({1: make_email("Квитанция", "<m1@example.test>")})
    _config, stats, _calls, say = run_mail(tmp_path, monkeypatch, fake)
    assert fake.full_fetches == [] and stats["messages_processed"] == 0 and say.warnings == 1


def test_non_pdf_content_is_not_taken_even_with_pdf_type_and_name():
    fake = FakeIMAP({1: make_email("Квитанция", "<m1@example.test>", pdf=b"MZ\x90 not a pdf")})
    content = ub.fetch_message(fake, 1, "*.pdf", 1048576, noop)
    assert content.pdfs == [] and content.attachments[0]["parsed"] is False


def test_migration_2_rebuilds_formats_without_database_instance(tmp_path):
    with closing(sqlite3.connect(tmp_path / "x.db")) as conn:
        conn.row_factory = sqlite3.Row
        ub._migration_1(conn, noop)
        ub._migration_2(conn, noop)
        assert conn.execute("SELECT COUNT(*) FROM bill_formats").fetchone()[0] == 0
