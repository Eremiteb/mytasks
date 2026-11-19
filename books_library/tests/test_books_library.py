"""Тесты books_library: только синтетические данные во временных каталогах, без реальной библиотеки."""

from __future__ import annotations

import base64
import fcntl
import importlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
bl = importlib.import_module("books_library")

# Самая маленькая валидная PNG (1x1): для обложки fb2 в синтетической книге.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
FB2_REL = "Иванов Иван/Тёмный лес.fb2"
ZIP_REL = "Иванов Иван/Архив.fb2.zip"
EPUB_REL = "Сидоров Сергей/Электронная.epub"
TXT_REL = "Иванов Иван/Заметки о лесе.txt"
# Прочие форматы для просмотра в браузере (синтетические файлы; DjVu и Calibre подменяются в тестах).
VIEW_ZIP_REL = "Иванов Иван/Коллекция.zip"
DJVU_REL = "Иванов Иван/Скан.djvu"
MOBI_REL = "Иванов Иван/Электронная.mobi"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def make_fb2(title: str, first: str, last: str, *, body: str | None = None, cover: bool = False) -> bytes:
    """Синтетическая fb2: title-info с автором, серией, жанром, аннотацией, датой и (опционально) обложкой."""
    if body is None:
        body = "<p>Ёлка и тьма.</p><p><script>alert(1)</script>Текст книги.</p>"
    cover_block = '<coverpage><image l:href="#cover.png"/></coverpage>' if cover else ""
    binary = ""
    if cover:
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        binary = f'<binary id="cover.png" content-type="image/png">{encoded}</binary>'
    text = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">'
        "<description><title-info>"
        "<genre>sf_fantasy</genre>"
        f"<author><first-name>{first}</first-name><last-name>{last}</last-name></author>"
        f"<book-title>{title}</book-title>"
        "<annotation><p>Аннотация &amp; текст</p></annotation>"
        '<date value="2019-05-01">2019</date>'
        "<lang>ru</lang>"
        '<sequence name="Цикл" number="2"/>'
        f"{cover_block}"
        "</title-info></description>"
        f"<body><section><title><p>Глава 1</p></title>{body}</section></body>"
        f"{binary}"
        "</FictionBook>"
    )
    return text.encode("utf-8")


def make_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Электронная книга</dc:title>"
        "<dc:creator>Сидоров Сергей</dc:creator>"
        "<dc:language>ru</dc:language>"
        "<dc:date>2020-01-01</dc:date>"
        "</metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine>'
        "</package>"
    )
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Глава первая</p></body></html>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", chapter)


@pytest.fixture
def books_dir(tmp_path: Path) -> Path:
    root = tmp_path / "books"
    (root / "Иванов Иван").mkdir(parents=True)
    (root / "Сидоров Сергей").mkdir()
    (root / FB2_REL).write_bytes(make_fb2("Тёмный лес", "Иван", "Иванов", cover=True))
    with zipfile.ZipFile(root / ZIP_REL, "w") as archive:
        archive.writestr("архив.fb2", make_fb2("Архивная книга", "Пётр", "Петров"))
    make_epub(root / EPUB_REL)
    (root / TXT_REL).write_text("Первый абзац.\n\nВторой абзац.\n", encoding="utf-8")
    return root


def make_cfg(tmp_path: Path, books: Path, port: int = 8090) -> dict[str, Any]:
    config = tmp_path / "books_library.json"
    # Карантин и число потоков — во временном каталоге: служебные режимы тестов не трогают /mnt/copy.
    settings = {
        "books_dir": str(books),
        "db_path": str(tmp_path / "lib.db"),
        "port": port,
        "quarantine_dir": str(tmp_path / "quarantine"),
        "workers": 2,
        "text_cache_dir": "",  # кэш текста выключен: тесты не пишут в /var/cache
    }
    config.write_text(json.dumps(settings), encoding="utf-8")
    return bl.load_config(config)


@pytest.fixture
def cfg(tmp_path: Path, books_dir: Path) -> dict[str, Any]:
    return make_cfg(tmp_path, books_dir)


@pytest.fixture
def scanned(cfg: dict[str, Any]) -> dict[str, Any]:
    bl.scan(cfg)
    return cfg


def book_rows(cfg: dict[str, Any]) -> dict[str, Any]:
    with closing(bl.connect(cfg["db_path"])) as conn:
        return {row["path"]: row for row in conn.execute("SELECT * FROM books")}


def book_id(cfg: dict[str, Any], rel: str) -> int:
    with closing(bl.connect(cfg["db_path"])) as conn:
        return int(conn.execute("SELECT id FROM books WHERE path=?", (rel,)).fetchone()[0])


# ---- (а) сканирование всех форматов -----------------------------------------------------

def test_scan_reads_fields_of_each_format(cfg: dict[str, Any]) -> None:
    stats = bl.scan(cfg)
    assert stats["files"] == 4
    assert stats["new"] == 4
    rows = book_rows(cfg)

    fb2 = rows[FB2_REL]
    assert fb2["fmt"] == "fb2"
    assert fb2["title"] == "Тёмный лес"
    assert fb2["authors"] == "Иванов Иван"
    assert fb2["series"] == "Цикл"
    assert fb2["series_no"] == 2
    assert fb2["genres"] == "sf_fantasy"
    assert fb2["lang"] == "ru"
    assert fb2["year"] == "2019"
    assert fb2["annotation"] == "Аннотация & текст"
    assert fb2["cover"] == 1

    zipped = rows[ZIP_REL]
    assert zipped["fmt"] == "fb2"  # однокнижный zip прозрачен: книга внутреннего формата, путь name.ext
    assert zipped["title"] == "Архивная книга"
    assert zipped["authors"] == "Петров Пётр"

    epub = rows[EPUB_REL]
    assert epub["fmt"] == "epub"
    assert epub["title"] == "Электронная книга"
    assert epub["authors"] == "Сидоров Сергей"
    assert epub["year"] == "2020"

    txt = rows[TXT_REL]
    assert txt["fmt"] == "txt"
    assert txt["title"] == "Заметки о лесе"
    assert txt["authors"] == "Иванов Иван"

    with closing(bl.connect(cfg["db_path"])) as conn:
        authors = {
            row[0]
            for row in conn.execute(
                "SELECT ba.name FROM book_authors ba JOIN books b ON b.id = ba.book_id WHERE b.path=?", (FB2_REL,)
            )
        }
        genres = {
            row[0]
            for row in conn.execute(
                "SELECT bg.name FROM book_genres bg JOIN books b ON b.id = bg.book_id WHERE b.path=?", (FB2_REL,)
            )
        }
    assert authors == {"Иванов Иван"}
    assert genres == {"sf_fantasy"}


def test_fts_matches_yo_query_against_ye_text(scanned: dict[str, Any]) -> None:
    with closing(bl.connect(scanned["db_path"])) as conn:
        # В названии «Тёмный лес» (с ё): запросы с ё, без ё и префикс «тем» находят ту же книгу.
        for query in ("тёмный", "темный", "тем", "ТЁМНЫЙ ЛЕС"):
            found = {
                row[0]
                for row in conn.execute(
                    "SELECT b.path FROM books b JOIN books_fts f ON f.rowid = b.id WHERE books_fts MATCH ?",
                    (bl.fts_query(query),),
                )
            }
            assert found == {FB2_REL}, query


# ---- (б) dry-run ----------------------------------------------------------------------------

def test_dry_run_does_not_create_db(cfg: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    stats = bl.scan(cfg, dry_run=True)

    assert stats["files"] == 4
    assert stats["new"] == 4
    assert not Path(cfg["db_path"]).exists()
    assert "[dry-run] будет" in capsys.readouterr().out


# ---- (в) повторное сканирование и удаление ---------------------------------------------------

def snapshot(cfg: dict[str, Any]) -> list[tuple[Any, ...]]:
    with closing(bl.connect(cfg["db_path"])) as conn:
        return [tuple(row) for row in conn.execute("SELECT id, path, size, mtime, title, authors FROM books ORDER BY id")]


def test_rescan_changes_nothing_and_removed_file_keeps_marks(cfg: dict[str, Any], books_dir: Path) -> None:
    bl.scan(cfg)
    before = snapshot(cfg)

    stats = bl.scan(cfg)
    assert stats["new"] == 0
    assert stats["changed"] == 0
    assert stats["removed"] == 0
    assert snapshot(cfg) == before

    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", (TXT_REL, "done", time.time()))

    (books_dir / TXT_REL).unlink()
    stats = bl.scan(cfg)

    assert stats["removed"] == 1
    assert TXT_REL not in book_rows(cfg)
    assert len(book_rows(cfg)) == 3
    with closing(bl.connect(cfg["db_path"])) as conn:
        status = conn.execute("SELECT status FROM marks WHERE path=?", (TXT_REL,)).fetchone()
    assert status is not None
    assert status[0] == "done"


# ---- (г) очистка разметки -------------------------------------------------------------------

def test_sanitize_drops_script_and_attributes_keeps_text() -> None:
    markup = '<p onclick="x()">Текст <script>alert(1)</script><a href="http://e" onclick="y">ссылка</a></p>'

    assert bl.sanitize(markup, lambda _ref: None) == "<p>Текст ссылка</p>"


def test_sanitize_fb2_emphasis_becomes_em() -> None:
    assert bl.sanitize("<emphasis>курсив</emphasis>", lambda _ref: None, fb2=True) == "<em>курсив</em>"


# ---- (д) конфигурация -----------------------------------------------------------------------

def test_load_config_rejects_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "books_library.json"
    path.write_text('{"prot": 8090}', encoding="utf-8")

    with pytest.raises(bl.ConfigError):
        bl.load_config(path)


def test_load_config_rejects_port_out_of_range(tmp_path: Path) -> None:
    path = tmp_path / "books_library.json"
    path.write_text('{"port": 70000}', encoding="utf-8")

    with pytest.raises(bl.ConfigError):
        bl.load_config(path)


def test_load_config_rejects_text_cache_mb_out_of_range(tmp_path: Path) -> None:
    path = tmp_path / "books_library.json"
    path.write_text('{"text_cache_mb": -1}', encoding="utf-8")

    with pytest.raises(bl.ConfigError):
        bl.load_config(path)


# ---- (е) веб-интерфейс ----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Не следовать перенаправлениям: проверяется сам ответ 303 после POST."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with OPENER.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def _serving(cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Веб-сервер на свободном порту с заданной конфигурацией; останавливается после теста."""
    monkeypatch.setattr(bl.Handler, "cfg", cfg)
    httpd = bl.ThreadingHTTPServer(("127.0.0.1", 0), bl.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def web(scanned: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    yield from _serving(scanned, monkeypatch)


def test_index_page_lists_book(web: str) -> None:
    status, _headers, body = fetch(f"{web}/")

    assert status == 200
    assert "Тёмный лес" in body.decode("utf-8")


def test_book_page_opens(web: str, scanned: dict[str, Any]) -> None:
    status, _headers, body = fetch(f"{web}/book/{book_id(scanned, FB2_REL)}")

    assert status == 200
    assert "Тёмный лес" in body.decode("utf-8")


def test_file_returns_original_bytes(web: str, scanned: dict[str, Any], books_dir: Path) -> None:
    status, _headers, body = fetch(f"{web}/file/{book_id(scanned, FB2_REL)}")

    assert status == 200
    assert body == (books_dir / FB2_REL).read_bytes()


def test_read_fb2_in_browser_drops_script(web: str, scanned: dict[str, Any]) -> None:
    status, _headers, body = fetch(f"{web}/read/{book_id(scanned, FB2_REL)}")
    text = body.decode("utf-8")

    assert status == 200
    assert "Текст книги" in text
    assert "alert(1)" not in text
    assert "<script>alert" not in text


def test_cover_is_png(web: str, scanned: dict[str, Any]) -> None:
    status, headers, body = fetch(f"{web}/cover/{book_id(scanned, FB2_REL)}")

    assert status == 200
    assert headers.get("Content-Type") == "image/png"
    assert body == PNG_BYTES


def test_post_mark_without_origin_is_forbidden(web: str, scanned: dict[str, Any]) -> None:
    # Тело пустое: сервер отклоняет запрос до чтения тела, непрочитанные байты дали бы сброс соединения.
    status, _headers, _body = fetch(f"{web}/book/{book_id(scanned, FB2_REL)}/mark", data=b"")

    assert status == 403


def test_post_mark_with_origin_saves_status(web: str, scanned: dict[str, Any]) -> None:
    bid = book_id(scanned, FB2_REL)

    status, headers, _body = fetch(
        f"{web}/book/{bid}/mark", data=b"status=done&rating=5&favorite=1", headers={"Origin": web}
    )

    assert status == 303
    assert headers.get("Location") == f"/book/{bid}"
    with closing(bl.connect(scanned["db_path"])) as conn:
        row = conn.execute("SELECT status, rating, favorite FROM marks WHERE path=?", (FB2_REL,)).fetchone()
    assert tuple(row) == ("done", 5, 1)


def test_unknown_book_id_is_404(web: str) -> None:
    status, _headers, _body = fetch(f"{web}/book/abc")

    assert status == 404


def test_opds_is_atom_feed(web: str) -> None:
    status, headers, body = fetch(f"{web}/opds")

    assert status == 200
    assert str(headers.get("Content-Type", "")).startswith("application/atom+xml")
    assert b"<feed" in body


# ---- разделы и коллекции --------------------------------------------------------------------

def test_path_meta_section_collection_and_author_from_file_name() -> None:
    meta = bl.path_meta("_Сборники/Коллекция/Автор Имя - Название.fb2")

    assert meta["section"] == "Сборники"
    assert meta["collection"] == "Коллекция"
    assert meta["authors"] == ["Автор Имя"]
    assert meta["title"] == "Название"


# ---- fix-encoding -----------------------------------------------------------------------------

WRONG_DECL = b'encoding="windows-1251"'
RIGHT_DECL = b'encoding="utf-8"'


@pytest.fixture
def enc_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "enc"
    (root / "Автор").mkdir(parents=True)
    # Объявлена windows-1251, а текст в UTF-8: такой файл должен исправиться.
    broken = make_fb2("Ошибочная", "Иван", "Иванов").replace(RIGHT_DECL, WRONG_DECL, 1)
    (root / "Автор" / "Ошибочная.fb2").write_bytes(broken)
    # Настоящая cp1251 с верным объявлением: исправлению не подлежит.
    real = make_fb2("Настоящая", "Пётр", "Петров").replace(RIGHT_DECL, WRONG_DECL, 1).decode("utf-8").encode("cp1251")
    (root / "Автор" / "Настоящая.fb2").write_bytes(real)
    (root / "Автор" / "Обычная.fb2").write_bytes(make_fb2("Обычная", "Иван", "Иванов"))
    # Отчёт пишется в books_library/reports/: подменяем каталог отчётов на временный.
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def test_fix_encoding_dry_run_changes_nothing(enc_cfg: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    broken = Path(enc_cfg["books_dir"]) / "Автор" / "Ошибочная.fb2"
    before = broken.read_bytes()

    stats = bl.fix_encoding(enc_cfg, dry_run=True)

    assert stats["fixed"] == 1
    assert broken.read_bytes() == before
    assert not list((bl.REPORTS_DIR).glob("fix-encoding-2*"))  # dry-run пишет только журнал и отчёт «-dry-run»
    assert "[dry-run] будет" in capsys.readouterr().out


def test_fix_encoding_changes_only_declaration_and_writes_report(enc_cfg: dict[str, Any]) -> None:
    root = Path(enc_cfg["books_dir"])
    broken = root / "Автор" / "Ошибочная.fb2"
    real = root / "Автор" / "Настоящая.fb2"
    normal = root / "Автор" / "Обычная.fb2"
    before_broken = broken.read_bytes()
    before_real = real.read_bytes()
    before_normal = normal.read_bytes()

    stats = bl.fix_encoding(enc_cfg)

    assert stats["fixed"] == 1
    assert broken.read_bytes() == before_broken.replace(WRONG_DECL, RIGHT_DECL, 1)
    assert real.read_bytes() == before_real
    assert normal.read_bytes() == before_normal
    journals = list((bl.REPORTS_DIR).glob("fix-encoding-2*.jsonl"))
    assert len(journals) == 1
    rows = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
    assert [Path(row["path"]).name for row in rows if row["action"] == "fixed"] == ["Ошибочная.fb2"]


def test_fix_encoding_second_run_finds_nothing(enc_cfg: dict[str, Any]) -> None:
    bl.fix_encoding(enc_cfg)

    stats = bl.fix_encoding(enc_cfg)

    assert "fixed" not in stats


# ---- fix-encoding для txt -----------------------------------------------------------------------

TXT_TEXT = "Глава первая. Тихий лес и старый дом.\n"


@pytest.fixture
def txt_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "txt"
    (root / "Автор").mkdir(parents=True)
    (root / "Автор" / "cp.txt").write_bytes(TXT_TEXT.encode("cp1251"))
    (root / "Автор" / "utf.txt").write_bytes(TXT_TEXT.encode("utf-8"))
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def test_fix_encoding_txt_cp1251_becomes_utf8(txt_cfg: dict[str, Any]) -> None:
    root = Path(txt_cfg["books_dir"])
    utf_before = (root / "Автор" / "utf.txt").read_bytes()

    stats = bl.fix_encoding(txt_cfg, only="txt")

    assert stats["fixed"] == 1
    assert (root / "Автор" / "cp.txt").read_bytes() == TXT_TEXT.encode("utf-8")
    assert (root / "Автор" / "utf.txt").read_bytes() == utf_before
    assert len(list((bl.REPORTS_DIR).glob("fix-encoding-2*.json"))) == 1


def test_fix_encoding_only_fb2_skips_txt(txt_cfg: dict[str, Any]) -> None:
    cp = Path(txt_cfg["books_dir"]) / "Автор" / "cp.txt"
    before = cp.read_bytes()

    stats = bl.fix_encoding(txt_cfg, only="fb2")

    assert "fixed" not in stats
    assert cp.read_bytes() == before


# ---- каталог обработки ----------------------------------------------------------------------

def test_target_dir_rejects_home_directory(cfg: dict[str, Any]) -> None:
    with pytest.raises(bl.ConfigError):
        bl.target_dir(cfg, str(Path.home()))


# ---- unpack-fb2zip --------------------------------------------------------------------------

def make_archive(path: Path, inner: str, data: bytes) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(inner, data)


@pytest.fixture
def zip_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "zips"
    root.mkdir()
    make_archive(root / "a.fb2.zip", "a.fb2", b"<FictionBook>a</FictionBook>")
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def test_unpack_dry_run_creates_nothing(zip_cfg: dict[str, Any]) -> None:
    root = Path(zip_cfg["books_dir"])

    stats = bl.unpack_fb2zip(zip_cfg, dry_run=True)

    assert stats["archives"] == 1
    assert not (root / "a.fb2").exists()
    assert (root / "a.fb2.zip").is_file()


def test_unpack_extracts_next_to_archive_and_keeps_archive(zip_cfg: dict[str, Any]) -> None:
    root = Path(zip_cfg["books_dir"])

    stats = bl.unpack_fb2zip(zip_cfg)

    assert stats["extracted"] == 1
    assert (root / "a.fb2").read_bytes() == b"<FictionBook>a</FictionBook>"
    assert (root / "a.fb2.zip").is_file()


def test_unpack_conflict_gets_numeric_suffix(zip_cfg: dict[str, Any]) -> None:
    root = Path(zip_cfg["books_dir"])
    (root / "a.fb2").write_bytes(b"old")

    bl.unpack_fb2zip(zip_cfg)

    assert (root / "a.fb2").read_bytes() == b"old"
    assert (root / "a_1.fb2").read_bytes() == b"<FictionBook>a</FictionBook>"


def test_unpack_skips_entry_over_max_mb(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    make_archive(root / "big.fb2.zip", "big.fb2", b"x" * (2 * 1024 * 1024))
    cfg = make_cfg(tmp_path, root)

    stats = bl.unpack_fb2zip(cfg, max_mb=1)

    assert stats["skipped"] == 1
    assert stats.get("extracted", 0) == 0
    assert not (root / "big.fb2").exists()


def make_multi(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def multi_members() -> dict[str, bytes]:
    """Три записи: две с метаданными/без (номера librusec) и запись с путём «..», которая должна быть отброшена."""
    return {
        "24.fb2": make_fb2("Книга", "Иван", "Петров"),
        "3.fb2": b"<FictionBook>no meta</FictionBook>",
        "../x.fb2": make_fb2("Чужая", "Иван", "Чужой"),
    }


@pytest.fixture
def multi_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "lib"
    root.mkdir()
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def test_multi_extracts_into_folder_with_metadata_names(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-3.zip", multi_members())

    stats = bl.unpack_fb2zip(multi_cfg, multi=True)

    folder = root / "lib-3"
    assert sorted(p.name for p in folder.iterdir()) == ["librusec-3.fb2", "Петров - Книга.fb2"]
    assert (folder / "librusec-3.fb2").read_bytes() == b"<FictionBook>no meta</FictionBook>"
    assert not (root / "x.fb2").exists()
    assert not (root.parent / "x.fb2").exists()
    assert (root / "lib-3.zip").is_file()  # запись отброшена — распаковка неполная, архив остаётся
    assert stats["extracted"] == 2
    assert stats["skipped"] == 1


def test_multi_dry_run_writes_nothing(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-3.zip", multi_members())

    bl.unpack_fb2zip(multi_cfg, dry_run=True, multi=True)

    assert not (root / "lib-3").exists()
    assert (root / "lib-3.zip").is_file()


def test_multi_md5_mismatch_skips_archive(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-3.zip", multi_members())
    (root / "md5_sum.txt").write_text("lib-3.zip\t\t: " + "0" * 32 + "\n", encoding="utf-8")

    stats = bl.unpack_fb2zip(multi_cfg, multi=True)

    assert stats["md5_mismatch"] == 1
    assert not (root / "lib-3").exists()
    assert (root / "lib-3.zip").is_file()


def test_multi_ignore_md5_extracts_with_warning(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-3.zip", multi_members())
    (root / "md5_sum.txt").write_text("lib-3.zip\t\t: " + "0" * 32 + "\n", encoding="utf-8")

    stats = bl.unpack_fb2zip(multi_cfg, multi=True, ignore_md5=True)

    assert stats["md5_ignored"] == 1
    assert stats["extracted"] == 2


def test_multi_matching_md5_passes(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-3.zip", multi_members())
    (root / "md5_sum.txt").write_text(f"lib-3.zip\t\t: {bl.file_md5(root / 'lib-3.zip')}\n", encoding="utf-8")

    stats = bl.unpack_fb2zip(multi_cfg, multi=True)

    assert stats["extracted"] == 2
    assert "md5_mismatch" not in stats


def test_multi_delete_source_quarantines_archive_after_full_unpack(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-2.zip", {"24.fb2": make_fb2("Книга", "Иван", "Петров"), "3.fb2": b"<FictionBook/>"})

    stats = bl.unpack_fb2zip(multi_cfg, multi=True, delete_source=True)

    assert (root / "lib-2" / "Петров - Книга.fb2").is_file()
    assert not (root / "lib-2.zip").exists()
    assert (Path(multi_cfg["quarantine_dir"]) / "replaced" / "lib-2.zip").is_file()
    assert stats["replaced"] == 1


def test_multi_out_dir_outside_books(multi_cfg: dict[str, Any], tmp_path: Path) -> None:
    root = Path(multi_cfg["books_dir"])
    make_multi(root / "lib-3.zip", multi_members())
    out = tmp_path / "ssd" / "unpacked"

    bl.unpack_fb2zip(multi_cfg, multi=True, out_dir=str(out))

    assert sorted(p.name for p in (out / "lib-3").iterdir()) == ["librusec-3.fb2", "Петров - Книга.fb2"]
    assert not (root / "lib-3").exists()


def test_multi_out_dir_inside_books_rejected(multi_cfg: dict[str, Any]) -> None:
    root = Path(multi_cfg["books_dir"])
    with pytest.raises(bl.ConfigError):
        bl.unpack_fb2zip(multi_cfg, dry_run=True, multi=True, out_dir=str(root / "inside"))


def test_out_requires_multi(zip_cfg: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(bl.ConfigError):
        bl.unpack_fb2zip(zip_cfg, out_dir=str(tmp_path / "out"))


def test_single_fb2zip_unchanged_with_multi_flag(zip_cfg: dict[str, Any]) -> None:
    root = Path(zip_cfg["books_dir"])

    bl.unpack_fb2zip(zip_cfg, multi=True)

    assert (root / "a.fb2").read_bytes() == b"<FictionBook>a</FictionBook>"
    assert (root / "a.fb2.zip").is_file()


# ---- convert: безопасность и проверка результата --------------------------------------------

def test_strip_foreign_images_keeps_only_media_paths(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()

    def image(url: str) -> dict[str, Any]:
        return {"t": "Image", "c": [["", [], []], [], [url, ""]]}

    tree = [image("/etc/passwd"), image("http://example.com/x.png"), image(str(media / "img.png"))]

    bl.strip_foreign_images(tree, media)

    assert tree[0] == {"t": "Str", "c": ""}
    assert tree[1] == {"t": "Str", "c": ""}
    assert tree[2]["t"] == "Image"


def test_verify_book_rejects_epub_without_mimetype(tmp_path: Path) -> None:
    path = tmp_path / "bad.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')

    assert bl.verify_book(path, "epub") is not None


def test_convert_without_pandoc_is_config_error(cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl.shutil, "which", lambda _name: None)

    with pytest.raises(bl.ConfigError):
        bl.convert_documents(cfg, dry_run=True, to="epub", only="all", exclude=[], author="", title_mode="auto",
                             toc=True, toc_depth=3, delete_source=False, overwrite=False, skip_newer=False)


# ---- MHT ----------------------------------------------------------------------------------------

MHT_LIMIT = 10 * 1024 * 1024


def test_mht_html_part_in_declared_encoding(tmp_path: Path) -> None:
    html_body = "<html><head><title>Тихий лес</title></head><body><p>Текст книги.</p></body></html>"
    mht = (
        "MIME-Version: 1.0\r\n"
        "From: Petrov Petr <p@example.com>\r\n"
        'Content-Type: multipart/related; boundary="BOUND"\r\n'
        "\r\n"
        "--BOUND\r\n"
        'Content-Type: text/html; charset="windows-1251"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        f"{html_body}\r\n"
        "--BOUND--\r\n"
    )
    path = tmp_path / "book.mht"
    path.write_bytes(mht.encode("cp1251"))

    html_text, title, author = bl.mht_html(path, MHT_LIMIT)

    assert "Текст книги." in html_text
    assert title == "Тихий лес"
    assert author == "Petrov Petr"


def test_mht_plain_text_is_wrapped_in_pre(tmp_path: Path) -> None:
    mht = (
        "From: Ivanov Ivan <i@example.com>\r\n"
        "Subject: Plain Book\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        "Первая строка <b>\r\n"
    )
    path = tmp_path / "plain.mht"
    path.write_bytes(mht.encode("utf-8"))

    html_text, title, author = bl.mht_html(path, MHT_LIMIT)

    assert html_text.startswith("<pre>")
    assert "&lt;b&gt;" in html_text
    assert title == "Plain Book"
    assert author == "Ivanov Ivan"


def test_mht_without_text_raises(tmp_path: Path) -> None:
    mht = (
        "From: x <x@example.com>\r\n"
        "Content-Type: application/octet-stream\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        "AAAA\r\n"
    )
    path = tmp_path / "empty.mht"
    path.write_bytes(mht.encode("ascii"))

    with pytest.raises(ValueError):
        bl.mht_html(path, MHT_LIMIT)


# ---- HTML библиотеки Мошкова (--only html) ----------------------------------------------------

LIB_RU_PAGE = (
    '<html><head><title>Михаил Булгаков. Собачье сердце</title></head><body bgcolor="E9E3DB"><pre>\n'
    "<dir><a name=0></a><h2>Михаил Булгаков. Собачье сердце</h2></dir>\n"
    "\n"
    "     Первый абзац книги,\n"
    "строка два.\n"
    "\n"
    "     Второй абзац <a href=http://example.com/>ссылка</a> &amp; конец.\n"
    "строка два.\n"
    "</pre></body></html>\n"
)


def test_lib_ru_html_author_title_and_paragraphs(tmp_path: Path) -> None:
    path = tmp_path / "dog.html"
    path.write_bytes(LIB_RU_PAGE.encode("utf-8"))

    html_text, title, author = bl.lib_ru_html(path, MHT_LIMIT)

    assert author == "Михаил Булгаков"
    assert title == "Собачье сердце"
    assert '<a name="0"></a><h2>Михаил Булгаков. Собачье сердце</h2>' in html_text
    assert "<p>Первый абзац книги, строка два.</p>" in html_text
    assert "<p>Второй абзац ссылка &amp; конец. строка два.</p>" in html_text
    assert "<pre" not in html_text
    assert "example.com" not in html_text


def test_lib_ru_html_title_with_digit_has_no_author(tmp_path: Path) -> None:
    path = tmp_path / "chapter.html"
    path.write_text(LIB_RU_PAGE.replace("Михаил Булгаков. Собачье сердце", "Глава 3. Текст"), encoding="utf-8")

    _html_text, title, author = bl.lib_ru_html(path, MHT_LIMIT)

    assert author == ""
    assert title == "Глава 3. Текст"


def test_lib_ru_name_skips_initials_and_limits_length() -> None:
    assert bl.lib_ru_name("Михаил Булгаков", "Собачье сердце", "epub") == "Булгаков - Собачье сердце.epub"
    assert bl.lib_ru_name("И.Л.Галинская", "X", "epub") == "Галинская - X.epub"
    assert bl.lib_ru_name("Булгаков М.А", "X", "epub") == "Булгаков - X.epub"
    assert bl.lib_ru_name("Иванов И", "X", "epub") == "Иванов - X.epub"
    assert bl.lib_ru_name("Иван Петров И.И.", "X", "epub") == "Петров - X.epub"
    long_name = bl.lib_ru_name("Иван Иванов", "я" * 300, "epub")
    assert long_name.endswith(".epub")
    assert len(long_name) <= 120


def test_local_images_copies_only_files_inside_source_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "pic.gif").write_bytes(b"GIF89a")
    (tmp_path / "secret.gif").write_bytes(b"x")
    media = tmp_path / "media"
    media.mkdir()
    markup = '<img src="pic.gif"><img src="../secret.gif"><img src="http://example.com/c.gif">'

    out = bl.local_images(markup, src, media, 1024)

    assert out.count("<img") == 1
    assert (media / "img1.gif").read_bytes() == b"GIF89a"
    assert "secret.gif" not in out


def html_page(title: str, *, pre: bool = True) -> str:
    body = "Текст книги. " * 460  # больше 5000 байт
    inner = f"<pre>\n     {body}\n</pre>" if pre else f"<p>{body}</p>"
    return f"<html><head><title>{title}</title></head><body>{inner}</body></html>"


def test_convert_html_selection_skips_index_small_no_pre_and_no_author(cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    folder = Path(cfg["books_dir"]) / "Булгаков"
    folder.mkdir()
    (folder / "index.html").write_text(html_page("Михаил Булгаков. Оглавление"), encoding="utf-8")
    (folder / "small.html").write_text("<pre>x</pre>", encoding="utf-8")
    (folder / "nopre.html").write_text(html_page("Михаил Булгаков. Без пре", pre=False), encoding="utf-8")
    (folder / "noauthor.html").write_text(html_page("Заметки без автора"), encoding="utf-8")
    (folder / "good.html").write_text(html_page("Михаил Булгаков. Собачье сердце"), encoding="utf-8")
    monkeypatch.setattr(bl.shutil, "which", lambda _name: "/usr/bin/pandoc")

    stats = bl.convert_documents(cfg, dry_run=True, only="html", to="epub", exclude=[], author="", title_mode="auto",
                                 toc=True, toc_depth=3, delete_source=False, overwrite=False, skip_newer=False)

    assert stats["skipped_index"] == 1
    assert stats["skipped_small"] == 1
    assert stats["skipped_no_pre"] == 1
    assert stats["skipped_no_author"] == 1
    assert stats["planned"] == 1
    assert "Булгаков - Собачье сердце.epub" in capsys.readouterr().out


def test_convert_html_allow_no_author_names_by_title(cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    folder = Path(cfg["books_dir"]) / "Фольклор"
    folder.mkdir()
    (folder / "anon.html").write_text(html_page("Заметки без автора"), encoding="utf-8")
    (folder / "h2.html").write_text(html_page("").replace("<title></title>", "<title></title><h2>Сказка о рыбаке</h2>"),
                                    encoding="utf-8")
    (folder / "nameless.html").write_text(html_page(""), encoding="utf-8")
    monkeypatch.setattr(bl.shutil, "which", lambda _name: "/usr/bin/pandoc")

    stats = bl.convert_documents(cfg, dry_run=True, only="html", to="epub", exclude=[], author="", title_mode="auto",
                                 toc=True, toc_depth=3, delete_source=False, overwrite=False, skip_newer=False,
                                 allow_no_author=True)

    assert stats["planned"] == 2
    assert stats["skipped_no_author"] == 1
    out = capsys.readouterr().out
    assert "Заметки без автора.epub" in out
    assert "Сказка о рыбаке.epub" in out
    assert "из них без автора 2" in out


def test_convert_all_does_not_pick_html(cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    folder = Path(cfg["books_dir"]) / "Булгаков"
    folder.mkdir()
    (folder / "good.html").write_text(html_page("Михаил Булгаков. Собачье сердце"), encoding="utf-8")
    monkeypatch.setattr(bl.shutil, "which", lambda _name: "/usr/bin/pandoc")

    stats = bl.convert_documents(cfg, dry_run=True, only="all", to="epub", exclude=[], author="", title_mode="auto",
                                 toc=True, toc_depth=3, delete_source=False, overwrite=False, skip_newer=False)

    assert stats["sources"] == 0


# ---- просмотр прочих форматов: PNM → PNG, кэш ----------------------------------------------------

def read_png(png: bytes) -> tuple[int, int, int, int, bytes]:
    """Разбор PNG: сигнатура, CRC каждого фрагмента, IEND; возвращает ширину, высоту, глубину, тип цвета и IDAT."""
    assert png[:8] == PNG_SIGNATURE
    pos = 8
    chunks: dict[bytes, bytes] = {}
    while pos < len(png):
        length = int.from_bytes(png[pos:pos + 4], "big")
        kind = png[pos + 4:pos + 8]
        body = png[pos + 8:pos + 8 + length]
        assert zlib.crc32(kind + body) == int.from_bytes(png[pos + 8 + length:pos + 12 + length], "big")
        chunks[kind] = body
        pos += 12 + length
    assert b"IEND" in chunks
    width, height, depth, color, _compression, _filter, _interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    return width, height, depth, color, zlib.decompress(chunks[b"IDAT"])


def test_pnm_p6_becomes_rgb_png() -> None:
    pixels = bytes(range(12))  # 2x2, по 3 байта на пиксель

    width, height, depth, color, raw = read_png(bl.pnm_to_png(b"P6\n2 2\n255\n" + pixels))

    assert (width, height, depth, color) == (2, 2, 8, 2)
    assert raw == b"\x00" + pixels[0:6] + b"\x00" + pixels[6:12]
    assert len(raw) == (1 + 6) * 2


def test_pnm_p5_becomes_gray_png() -> None:
    width, height, depth, color, raw = read_png(bl.pnm_to_png(b"P5\n3 1\n255\n\x10\x20\x30"))

    assert (width, height, depth, color) == (3, 1, 8, 0)
    assert raw == b"\x00\x10\x20\x30"
    assert len(raw) == (1 + 3) * 1


def test_pnm_p4_bits_are_inverted() -> None:
    # 9 пикселей — строка из 2 байт. Бит 1 в PNM — чёрный, в PNG 1 — белый, поэтому биты инвертируются.
    width, height, depth, color, raw = read_png(bl.pnm_to_png(b"P4\n9 2\n\xff\x80\x00\x01"))

    assert (width, height, depth, color) == (9, 2, 1, 0)
    assert raw == b"\x00\x00\x7f" + b"\x00\xff\xfe"
    assert len(raw) == (1 + 2) * 2


@pytest.mark.parametrize("data", [b"P3\n1 1\n255\n0 0 0\n", b"P7\n1 1\n255\n\x00"])
def test_pnm_unsupported_magic_is_value_error(data: bytes) -> None:
    with pytest.raises(ValueError):
        bl.pnm_to_png(data)


@pytest.mark.parametrize("data", [b"P6\n2 2\n255\n" + bytes(5), b"P4\n9 2\n\xff"])
def test_pnm_truncated_data_is_value_error(data: bytes) -> None:
    with pytest.raises(ValueError):
        bl.pnm_to_png(data)


def test_cache_prune_removes_oldest_file_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "CACHE_DIR", tmp_path)
    mib = 1024 * 1024
    for name, atime in (("old.png", 1_000), ("mid.png", 2_000), ("new.png", 3_000)):
        path = tmp_path / name
        path.write_bytes(b"x" * mib)
        os.utime(path, (atime, atime))

    bl.cache_prune(2)  # 3 МБ при пределе 2 МБ: достаточно удалить один самый давно использованный файл

    assert sorted(item.name for item in tmp_path.iterdir()) == ["mid.png", "new.png"]


def cache_row(file_id: int, size: int, mtime: float) -> sqlite3.Row:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT ? AS id, ? AS size, ? AS mtime", (file_id, size, mtime)).fetchone()


def test_cache_key_changes_with_size_and_mtime() -> None:
    base = bl.cache_key(cache_row(7, 100, 1_700_000_000.5))

    assert base == "7-100-1700000000"
    assert bl.cache_key(cache_row(7, 101, 1_700_000_000.5)) != base
    assert bl.cache_key(cache_row(7, 100, 1_700_000_001.5)) != base


# ---- кэш текста dedup -----------------------------------------------------------------------------

@pytest.fixture
def tcache(tmp_path: Path) -> Iterator[Any]:
    directory = tmp_path / "text_cache"
    directory.mkdir()
    cache = bl.TextCache.open({"text_cache_dir": str(directory), "text_cache_mb": 10})
    assert cache is not None
    yield cache
    cache.close()


def test_text_cache_get_by_path_checks_size_and_mtime(tcache: Any) -> None:
    tcache.put("Иванов/книга.txt", 10, 1000.0, "txt", "Текст книги")
    tcache.put("Иванов/пусто.txt", 0, 5.0, "txt", "")

    assert tcache.get("Иванов/книга.txt", 10, 1000.0, "txt") == "Текст книги"
    assert tcache.get("Иванов/пусто.txt", 0, 5.0, "txt") == ""  # пустой текст тоже кэшируется
    assert tcache.get("Иванов/книга.txt", 11, 1000.0, "txt") is None  # размер изменился
    assert tcache.get("Иванов/книга.txt", 10, 1000.5, "txt") is None  # mtime изменился
    assert tcache.get("Иванов/книга.txt", 10, 1000.0, "fb2") is None  # формат другой


def test_text_cache_finds_renamed_file_and_moves_path(tcache: Any) -> None:
    tcache.put("Иванов/старое.txt", 10, 1000.0, "txt", "текст")

    assert tcache.get("Иванов/новое.txt", 10, 1000.0, "txt") == "текст"
    assert [row[0] for row in tcache.conn.execute("SELECT path FROM texts")] == ["Иванов/новое.txt"]


def test_text_cache_two_candidates_give_none(tcache: Any) -> None:
    tcache.put("а.txt", 10, 1000.0, "txt", "один")
    tcache.put("б.txt", 10, 1000.0, "txt", "два")

    assert tcache.get("в.txt", 10, 1000.0, "txt") is None


def test_text_cache_prune_removes_least_recently_used(tcache: Any) -> None:
    # Случайный hex сжимается примерно вдвое: три записи ≈ 1.8 МБ, при пределе 1 МБ остаётся самая свежая.
    for name, used in (("старая.txt", 1000.0), ("средняя.txt", 2000.0), ("новая.txt", 3000.0)):
        tcache.put(name, 1, 1.0, "txt", os.urandom(600_000).hex())
        tcache.conn.execute("UPDATE texts SET used = ? WHERE path = ?", (used, name))

    tcache.prune(1)

    assert [row[0] for row in tcache.conn.execute("SELECT path FROM texts")] == ["новая.txt"]


def test_text_cache_open_is_none_without_directory(tmp_path: Path) -> None:
    assert bl.TextCache.open({"text_cache_dir": str(tmp_path / "нет"), "text_cache_mb": 10}) is None
    assert bl.TextCache.open({"text_cache_dir": "", "text_cache_mb": 10}) is None
    assert not (tmp_path / "нет").exists()  # каталог не создаётся


def test_dedup_dry_run_second_pass_hits_text_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "dups"
    folder = root / "Иванов Иван"
    folder.mkdir(parents=True)
    text = " ".join(f"слово{i}" for i in range(300))  # больше DEDUP_MIN_WORDS, чтобы копия стала дублем
    (folder / "Заметки.txt").write_text(text, encoding="utf-8")
    (folder / "Заметки_1.txt").write_text(text, encoding="utf-8")
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    cache_dir = tmp_path / "text_cache"
    cache_dir.mkdir()
    cfg["text_cache_dir"] = str(cache_dir)

    first = bl.dedup(cfg, dry_run=True, out=lambda _line: None)
    second = bl.dedup(cfg, dry_run=True, out=lambda _line: None)

    assert first["groups"] == 1
    assert first["text_cache_miss"] == 2
    assert second["text_cache_hit"] == 2
    assert (folder / "Заметки.txt").is_file() and (folder / "Заметки_1.txt").is_file()  # dry-run ничего не переносит


# ---- просмотр прочих форматов в веб-интерфейсе ---------------------------------------------------

@pytest.fixture
def view_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Каталог с ZIP, DjVu и MOBI; ddjvu/djvused/djvutxt и ebook-convert подменены, реальные программы не вызываются."""
    root = tmp_path / "view_books"
    (root / "Иванов Иван").mkdir(parents=True)
    with zipfile.ZipFile(root / VIEW_ZIP_REL, "w") as archive:
        archive.writestr("a.txt", "Привет, тихий лес.\n".encode("cp1251"))
        archive.writestr("img.png", PNG_BYTES)
        archive.writestr("b.bin", b"\x00\x01\x02binary")
        archive.writestr("doc.html", "<html><body><p>Текст документа</p><script>alert(1)</script></body></html>")
    (root / DJVU_REL).write_bytes(b"AT&TFORM synthetic djvu bytes")
    (root / MOBI_REL).write_bytes(b"synthetic mobi bytes")
    converted = tmp_path / "converted.epub"
    make_epub(converted)
    cfg = make_cfg(tmp_path, root)
    monkeypatch.setattr(bl, "djvu_meta", lambda path: {})  # djvused не вызывается: файл синтетический
    bl.scan(cfg)
    monkeypatch.setattr(bl, "djvu_page_count", lambda path, timeout: 3)
    monkeypatch.setattr(bl, "djvu_page_text", lambda path, page, timeout: "текст")
    monkeypatch.setattr(bl, "djvu_page_png", lambda cfg_, book, path, page: bl.pnm_to_png(b"P5\n1 1\n255\n\x00"))
    monkeypatch.setattr(bl, "calibre_epub", lambda cfg_, book, path, create=True: converted)
    return cfg


@pytest.fixture
def view_web(view_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    yield from _serving(view_cfg, monkeypatch)


def test_zip_read_redirects_to_listing(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, VIEW_ZIP_REL)

    status, headers, _body = fetch(f"{view_web}/read/{bid}")

    assert status == 303
    assert headers.get("Location") == f"/zip/{bid}"


def test_zip_listing_shows_file_names(view_web: str, view_cfg: dict[str, Any]) -> None:
    status, _headers, body = fetch(f"{view_web}/zip/{book_id(view_cfg, VIEW_ZIP_REL)}")
    text = body.decode("utf-8")

    assert status == 200
    for name in ("a.txt", "img.png", "b.bin", "doc.html"):
        assert name in text


def test_zip_text_entry_is_decoded_from_cp1251(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, VIEW_ZIP_REL)

    status, _headers, body = fetch(f"{view_web}/zip/{bid}?e=a.txt")

    assert status == 200
    assert "Привет, тихий лес." in body.decode("utf-8")


def test_zip_image_entry_is_png(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, VIEW_ZIP_REL)

    status, headers, body = fetch(f"{view_web}/zip/{bid}?e=img.png")

    assert status == 200
    assert headers.get("Content-Type") == "image/png"
    assert body == PNG_BYTES


def test_zip_binary_entry_is_download(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, VIEW_ZIP_REL)

    status, headers, _body = fetch(f"{view_web}/zip/{bid}?e=b.bin")

    assert status == 200
    assert str(headers.get("Content-Disposition", "")).startswith("attachment")


def test_zip_html_entry_drops_script(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, VIEW_ZIP_REL)

    status, _headers, body = fetch(f"{view_web}/zip/{bid}?e=doc.html")
    text = body.decode("utf-8")

    assert status == 200
    assert "Текст документа" in text
    assert "<script>" not in text
    assert "alert(1)" not in text


def test_zip_entry_outside_archive_is_404(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, VIEW_ZIP_REL)

    status, _headers, _body = fetch(f"{view_web}/zip/{bid}?e={quote('../../etc/passwd', safe='')}")

    assert status == 404


def test_djvu_read_shows_page_and_position(view_web: str, view_cfg: dict[str, Any]) -> None:
    bid = book_id(view_cfg, DJVU_REL)

    status, _headers, body = fetch(f"{view_web}/read/{bid}?page=2")
    text = body.decode("utf-8")

    assert status == 200
    assert f"/page/{bid}/2" in text
    assert "2/3" in text
    assert "текст" in text


def test_djvu_page_is_png(view_web: str, view_cfg: dict[str, Any]) -> None:
    status, headers, body = fetch(f"{view_web}/page/{book_id(view_cfg, DJVU_REL)}/1")

    assert status == 200
    assert headers.get("Content-Type") == "image/png"
    assert body.startswith(PNG_SIGNATURE)


def test_djvu_page_zero_is_404(view_web: str, view_cfg: dict[str, Any]) -> None:
    status, _headers, _body = fetch(f"{view_web}/page/{book_id(view_cfg, DJVU_REL)}/0")

    assert status == 404


def test_mobi_read_shows_converted_chapter(view_web: str, view_cfg: dict[str, Any]) -> None:
    status, _headers, body = fetch(f"{view_web}/read/{book_id(view_cfg, MOBI_REL)}")

    assert status == 200
    assert "Глава первая" in body.decode("utf-8")


# ---- JSON API -----------------------------------------------------------------------------------

@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Отчёты и файл блокировки — во временном каталоге, не в репозитории.
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(bl, "LOCK_PATH", tmp_path / ".books_library.lock")


def api_call(url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """Запрос к API: payload — JSON-тело (POST), иначе GET. Возвращает код и разобранный JSON."""
    head = {"Content-Type": "application/json", **(headers or {})} if payload is not None else (headers or {})
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    status, _headers, body = fetch(url, data=data, headers=head)
    return status, json.loads(body.decode("utf-8")) if body else None


def wait_job(base: str, job_id: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        _status, job = api_call(f"{base}/api/jobs/{job_id}")
        if job["state"] in ("done", "failed") or time.monotonic() > deadline:
            return job
        time.sleep(0.1)


def test_api_help_lists_endpoints(web: str, api_env: None) -> None:
    status, body = api_call(f"{web}/api")

    assert status == 200
    assert "GET /api/books" in body["endpoints"]


def test_api_books_search_returns_total_and_items(web: str, api_env: None) -> None:
    status, body = api_call(f"{web}/api/books?q={quote('тёмный')}")

    assert status == 200
    assert body["total"] == 1
    item = body["items"][0]
    assert item["title"] == "Тёмный лес"
    assert item["authors"] == ["Иванов Иван"]


def test_api_book_detail_has_mark_and_shelves(web: str, api_env: None, scanned: dict[str, Any]) -> None:
    status, body = api_call(f"{web}/api/books/{book_id(scanned, FB2_REL)}")

    assert status == 200
    assert body["mark"] is None
    assert body["shelves"] == []


def test_api_mark_json_saves_fields(web: str, api_env: None, scanned: dict[str, Any]) -> None:
    bid = book_id(scanned, FB2_REL)

    status, _body = api_call(f"{web}/api/books/{bid}/mark", {"status": "done", "rating": 5, "favorite": True})

    assert status == 200
    with closing(bl.connect(scanned["db_path"])) as conn:
        row = conn.execute("SELECT status, rating, favorite FROM marks WHERE path=?", (FB2_REL,)).fetchone()
    assert tuple(row) == ("done", 5, 1)


def test_api_mark_rejects_bad_values(web: str, api_env: None, scanned: dict[str, Any]) -> None:
    bid = book_id(scanned, FB2_REL)

    status, body = api_call(f"{web}/api/books/{bid}/mark", {"rating": 9})
    assert status == 400
    assert "rating" in body["error"]

    status, body = api_call(f"{web}/api/books/{bid}/mark", {"bogus": 1})
    assert status == 400
    assert "bogus" in body["error"]


def test_api_shelves_create_and_assign(web: str, api_env: None, scanned: dict[str, Any]) -> None:
    bid = book_id(scanned, FB2_REL)

    status, shelf = api_call(f"{web}/api/shelves", {"name": "Любимые"})
    assert status == 201
    shelf_id = shelf["id"]

    status, _body = api_call(f"{web}/api/books/{bid}/shelves", {"shelf_ids": [shelf_id]})
    assert status == 200

    _status, book = api_call(f"{web}/api/books/{bid}")
    assert [s["id"] for s in book["shelves"]] == [shelf_id]


def test_api_form_post_without_origin_is_forbidden(web: str, api_env: None) -> None:
    # Тело пустое: запрет наступает до чтения тела (см. тест отметки без Origin).
    status, _headers, _body = fetch(f"{web}/api/shelves", data=b"")

    assert status == 403


def test_api_fix_encoding_real_job_needs_confirm(web: str, api_env: None, tmp_path: Path) -> None:
    fix_dir = tmp_path / "fix"
    fix_dir.mkdir()
    (fix_dir / "Ошибочная.fb2").write_bytes(make_fb2("Ошибочная", "Иван", "Иванов").replace(RIGHT_DECL, WRONG_DECL, 1))

    status, body = api_call(f"{web}/api/jobs", {"command": "fix-encoding", "dry_run": False, "path": str(fix_dir)})

    assert status == 409
    assert "confirm" in body["error"]


def test_api_fix_encoding_dry_run_then_confirmed_real_job(web: str, api_env: None, tmp_path: Path) -> None:
    fix_dir = tmp_path / "fix"
    fix_dir.mkdir()
    target = fix_dir / "Ошибочная.fb2"
    target.write_bytes(make_fb2("Ошибочная", "Иван", "Иванов").replace(RIGHT_DECL, WRONG_DECL, 1))

    status, dry = api_call(f"{web}/api/jobs", {"command": "fix-encoding", "path": str(fix_dir)})
    assert status == 202
    assert dry["dry_run"] is True
    assert wait_job(web, dry["id"])["state"] == "done"
    assert target.read_bytes().count(WRONG_DECL) == 1  # dry-run ничего не меняет

    status, real = api_call(f"{web}/api/jobs", {"command": "fix-encoding", "dry_run": False, "path": str(fix_dir),
                                                "confirm": dry["id"]})
    assert status == 202
    assert wait_job(web, real["id"])["state"] == "done"
    assert target.read_bytes().count(RIGHT_DECL) == 1
    assert target.read_bytes().count(WRONG_DECL) == 0


def test_api_confirm_with_other_params_is_refused(web: str, api_env: None, tmp_path: Path) -> None:
    fix_dir = tmp_path / "fix"
    fix_dir.mkdir()
    (fix_dir / "Ошибочная.fb2").write_bytes(make_fb2("Ошибочная", "Иван", "Иванов").replace(RIGHT_DECL, WRONG_DECL, 1))
    _status, dry = api_call(f"{web}/api/jobs", {"command": "fix-encoding", "path": str(fix_dir)})
    assert wait_job(web, dry["id"])["state"] == "done"

    status, body = api_call(f"{web}/api/jobs", {"command": "fix-encoding", "dry_run": False, "path": str(fix_dir),
                                                "only": "txt", "confirm": dry["id"]})

    assert status == 409
    assert "confirm" in body["error"]


def test_api_unknown_parameter_is_bad_request(web: str, api_env: None) -> None:
    status, body = api_call(f"{web}/api/jobs", {"command": "scan", "bogus": 1})

    assert status == 400
    assert "bogus" in body["error"]


def insert_dup_group(conn: sqlite3.Connection, key: str, status: str) -> int:
    """Группа дублей с двумя оставленными книгами (для страницы и действий)."""
    gid = conn.execute("INSERT INTO dup_groups(key, status, created) VALUES (?,?,?)", (key, status, 1.0)).lastrowid
    for path, fmt in ((FB2_REL, "fb2"), (TXT_REL, "txt")):
        conn.execute("INSERT INTO dup_members(group_id, path, fmt, size, words, coverage, role, keeper, quarantine) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (gid, path, fmt, 1, 300, 0.99, "kept", "", ""))
    return gid


def test_dup_resolve_marks_group_and_keeps_files(scanned: dict[str, Any], api_env: None) -> None:
    with closing(bl.connect(scanned["db_path"])) as conn, conn:
        gid = insert_dup_group(conn, "Решить без файлов", "similar")

    message = bl.dup_action(scanned, gid, "resolve")

    assert message == "Группа помечена решённой, файлы не менялись."
    with closing(bl.connect(scanned["db_path"])) as conn:
        assert conn.execute("SELECT status FROM dup_groups WHERE id=?", (gid,)).fetchone()[0] == "resolved"
    assert (Path(scanned["books_dir"]) / FB2_REL).is_file()


def test_dup_resolve_unknown_group_is_refused(scanned: dict[str, Any], api_env: None) -> None:
    with pytest.raises(ValueError):
        bl.dup_action(scanned, 999999, "resolve")


def test_dups_page_hides_resolved_until_checkbox(web: str, api_env: None, scanned: dict[str, Any]) -> None:
    with closing(bl.connect(scanned["db_path"])) as conn, conn:
        insert_dup_group(conn, "Группа похожая", "similar")
        insert_dup_group(conn, "Группа решённая", "resolved")

    status, _headers, body = fetch(f"{web}/dups?status=similar")
    text = body.decode("utf-8")

    assert status == 200
    assert "Группа похожая" in text
    assert "Группа решённая" not in text
    assert "Решено</button>" in text

    _status, _headers, body = fetch(f"{web}/dups?status=similar&resolved=1")
    text = body.decode("utf-8")

    assert "Группа похожая" in text
    assert "Группа решённая" in text
    assert "checked>" in text


def test_command_params_rejects_out_of_range_toc_depth() -> None:
    with pytest.raises(bl.ConfigError):
        bl.command_params("convert", {"toc_depth": 9})


def test_command_params_accepts_workers_for_dedup_and_zip_books() -> None:
    assert bl.command_params("dedup", {"workers": 4})["workers"] == 4
    assert bl.command_params("zip-books", {})["workers"] is None


def test_command_params_rejects_zero_workers() -> None:
    with pytest.raises(bl.ConfigError):
        bl.command_params("dedup", {"workers": 0})


def test_load_config_rejects_quarantine_inside_books(tmp_path: Path) -> None:
    books = tmp_path / "books"
    config = tmp_path / "books_library.json"
    config.write_text(json.dumps({"books_dir": str(books), "quarantine_dir": str(books / "removed")}), encoding="utf-8")
    with pytest.raises(bl.ConfigError):
        bl.load_config(config)


# ---- однокнижные zip, пути и кодировки -----------------------------------------------------------

LONG_RU = (
    "Глава первая. Тихий лес окружал старый дом на опушке, и каждое утро туман медленно поднимался над рекой. "
    "Иван Петрович вышел на крыльцо, посмотрел на далёкие холмы и подумал, что сегодня наконец закончит письмо брату. "
    "Ветер шевелил листья берёз, а в траве звенели первые кузнечики. Никто в деревне не знал, что за рекой давно никто "
    "не жил, и только Маша иногда слышала оттуда странные голоса по вечерам."
)


def test_split_name_understands_single_book_zip() -> None:
    assert bl.split_name("a.fb2.zip") == ("a", "fb2")
    assert bl.split_name("a.zip") == ("a", "zip")


def test_display_path_hides_single_book_zip() -> None:
    assert bl.display_path("x/a.fb2.zip", "fb2") == "x/a.fb2"
    assert bl.display_path("x/b.zip", "pdf") == "x/b.pdf"
    assert bl.display_path("x/m.zip", "zip") == "x/m.zip"


@pytest.mark.parametrize("encoding", ["cp866", "koi8_r", "cp1251"])
def test_detect_txt_recognizes_cyrillic_encodings(encoding: str) -> None:
    text, found = bl.detect_txt(LONG_RU.encode(encoding))
    assert text == LONG_RU
    assert found == encoding


def test_detect_txt_binary_is_not_text() -> None:
    assert bl.detect_txt(b"\x00\x01abc") == (None, "binary")


def test_detect_txt_drops_isolated_nul() -> None:
    text, encoding = bl.detect_txt("Привет мир\r\n".encode() * 50 + b"\x00")
    assert text == "Привет мир\r\n" * 50
    assert "\x00" not in text
    assert encoding == "utf-8"


def test_normalize_txt_converts_line_endings_to_lf() -> None:
    assert bl.normalize_txt(b"a\r\nb\rc")[0] == b"a\nb\nc"


def test_fix_semi_cp866_restores_capitals_in_field() -> None:
    assert bl.fix_semi_cp866("‘тейнбек „жон", field=True)[0] == "Стейнбек Джон"


def test_norm_key_folds_yo_case_and_punctuation() -> None:
    assert bl.norm_key("Ёлка_Два!") == "елка два"


@pytest.fixture
def night_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "night"
    root.mkdir()
    (root / "cp.txt").write_bytes(TXT_TEXT.encode("cp1251"))
    (root / "bin.txt").write_bytes(b"\x00\x01abc")
    # Журналы и отчёты пишутся в REPORTS_DIR: подменяем на временный каталог.
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def test_fix_encoding_moves_binary_txt_to_quarantine(night_cfg: dict[str, Any]) -> None:
    root = Path(night_cfg["books_dir"])

    stats = bl.fix_encoding(night_cfg, only="txt")

    assert stats["fixed"] == 1
    assert stats["rejected-encoding"] == 1
    assert (root / "cp.txt").read_bytes() == TXT_TEXT.encode("utf-8")
    assert not (root / "bin.txt").exists()
    quarantined = Path(night_cfg["quarantine_dir"]) / "rejected-encoding" / "bin.txt"
    assert quarantined.read_bytes() == b"\x00\x01abc"


def test_zip_books_wraps_txt_and_removes_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "zip_src"
    root.mkdir()
    data = TXT_TEXT.encode("utf-8")
    (root / "a.txt").write_bytes(data)
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)

    stats = bl.zip_books(cfg)

    assert stats["zipped"] == 1
    assert not (root / "a.txt").exists()
    with zipfile.ZipFile(root / "a.txt.zip") as archive:
        assert archive.namelist() == ["a.txt"]
        assert archive.read("a.txt") == data


# ---- unzip-books: распаковка однокнижных zip обратно (решение: книги хранятся распакованными) ----

@pytest.fixture
def unzip_relinks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Пары переноса путей (relink) без базы: собираются для проверки."""
    pairs: list[tuple[str, str]] = []

    def fake_relink(db_path: str, moved: list[tuple[str, str]]) -> int:
        pairs.extend(moved)
        return len(moved)

    monkeypatch.setattr(bl, "relink", fake_relink)
    return pairs


@pytest.fixture
def unzip_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unzip_relinks: list[tuple[str, str]]) -> dict[str, Any]:
    root = tmp_path / "unzip"
    root.mkdir()
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def make_multi_pair(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("a.txt", b"one")
        archive.writestr("b.txt", b"two")


def test_unzip_dry_run_changes_nothing(unzip_cfg: dict[str, Any], unzip_relinks: list[tuple[str, str]]) -> None:
    root = Path(unzip_cfg["books_dir"])
    make_archive(root / "a.txt.zip", "a.txt", TXT_TEXT.encode("utf-8"))

    stats = bl.unzip_books(unzip_cfg, dry_run=True)

    assert stats["extracted"] == 1
    assert (root / "a.txt.zip").is_file()
    assert not (root / "a.txt").exists()
    assert not (Path(unzip_cfg["quarantine_dir"]) / "unzipped").exists()
    assert unzip_relinks == []


def test_unzip_txt_archive_becomes_plain_file(unzip_cfg: dict[str, Any], unzip_relinks: list[tuple[str, str]]) -> None:
    root = Path(unzip_cfg["books_dir"])
    data = TXT_TEXT.encode("utf-8")
    make_archive(root / "a.txt.zip", "a.txt", data)
    os.utime(root / "a.txt.zip", (1_600_000_000, 1_600_000_000))

    stats = bl.unzip_books(unzip_cfg)

    assert stats["extracted"] == 1
    assert (root / "a.txt").read_bytes() == data
    assert int((root / "a.txt").stat().st_mtime) == 1_600_000_000
    assert not (root / "a.txt.zip").exists()
    assert (Path(unzip_cfg["quarantine_dir"]) / "unzipped" / "a.txt.zip").is_file()
    assert ("a.txt.zip", "a.txt") in unzip_relinks


def test_unzip_multi_file_archive_untouched(unzip_cfg: dict[str, Any]) -> None:
    root = Path(unzip_cfg["books_dir"])
    make_multi_pair(root / "pack.txt.zip")

    stats = bl.unzip_books(unzip_cfg)

    assert stats["multi"] == 1
    assert (root / "pack.txt.zip").is_file()
    assert not (root / "a.txt").exists()


def test_unzip_identical_target_is_twin_archive_goes_to_quarantine(
        unzip_cfg: dict[str, Any], unzip_relinks: list[tuple[str, str]]) -> None:
    root = Path(unzip_cfg["books_dir"])
    data = TXT_TEXT.encode("utf-8")
    make_archive(root / "a.txt.zip", "a.txt", data)
    (root / "a.txt").write_bytes(data)

    stats = bl.unzip_books(unzip_cfg)

    assert stats["twin"] == 1
    assert "extracted" not in stats
    assert sorted(p.name for p in root.iterdir()) == ["a.txt"]
    assert (Path(unzip_cfg["quarantine_dir"]) / "unzipped" / "a.txt.zip").is_file()
    assert ("a.txt.zip", "a.txt") in unzip_relinks


def test_unzip_different_target_gets_numeric_suffix(unzip_cfg: dict[str, Any]) -> None:
    root = Path(unzip_cfg["books_dir"])
    data = TXT_TEXT.encode("utf-8")
    make_archive(root / "a.txt.zip", "a.txt", data)
    (root / "a.txt").write_bytes(b"other")

    stats = bl.unzip_books(unzip_cfg)

    assert stats["conflict"] == 1
    assert (root / "a.txt").read_bytes() == b"other"
    assert (root / "a_1.txt").read_bytes() == data


def test_unzip_entry_path_cannot_leave_folder(unzip_cfg: dict[str, Any]) -> None:
    root = Path(unzip_cfg["books_dir"])
    make_archive(root / "t.txt.zip", "../evil.txt", b"abc")  # имя цели берётся из архива, путь записи отбрасывается
    make_archive(root / "x.zip", "../evil2.txt", b"def")  # без вложенного расширения — basename записи

    bl.unzip_books(unzip_cfg)

    assert (root / "t.txt").read_bytes() == b"abc"
    assert (root / "evil2.txt").read_bytes() == b"def"
    assert not (root.parent / "evil.txt").exists()
    assert not (root.parent / "evil2.txt").exists()


# ---- сборники книг (схема v4: book_collections, VIEW collections) -----------------------------

def insert_book(conn: sqlite3.Connection, path: str) -> None:
    """Строка books по пути; раздел и сборник — как при разборе пути в scan."""
    section, collection = bl.path_collection(path)
    conn.execute("INSERT INTO books(path, size, mtime, fmt, title, added, section, collection) VALUES (?,?,?,?,?,?,?,?)",
                 (path, 1, 1.0, "epub", path.rsplit("/", 1)[-1], 1.0, section, collection))


def insert_duplicate(conn: sqlite3.Connection, path: str, keeper: str) -> None:
    """Удалённая копия в карантин: одна группа дублей на каждую копию."""
    gid = conn.execute("INSERT INTO dup_groups(key, status, created) VALUES (?,?,?)", (path, "auto", 1.0)).lastrowid
    conn.execute("INSERT INTO dup_members(group_id, path, fmt, size, words, coverage, role, keeper, quarantine) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (gid, path, "epub", 1, 300, 0.99, "duplicate", keeper, ""))


@pytest.fixture
def coll_db(tmp_path: Path) -> str:
    db = str(tmp_path / "coll.db")
    bl.migrate_database(db, out=lambda _line: None)
    with closing(bl.connect(db)) as conn, conn:
        for path in ("_Сборники/Серия X/a.epub", "Автор/b.epub", "Автор/c.txt.zip"):
            insert_book(conn, path)
        insert_duplicate(conn, "_Сборники/Серия Y/b.epub", "Автор/b.epub")
        insert_duplicate(conn, "_Сборники/Серия Y/c.txt", "Автор/c.txt")  # keeper упакован в zip-books
        insert_duplicate(conn, "_Сборники/Серия Z/d.epub", "Нет/d.epub")  # keeper пропал совсем
    return db


def test_migration_v4_creates_book_collections_and_view(tmp_path: Path) -> None:
    empty = str(tmp_path / "empty.db")
    old = str(tmp_path / "v3.db")
    with closing(sqlite3.connect(old)) as conn:
        for number, sql in enumerate(bl.MIGRATIONS[:3], start=1):
            conn.executescript(f"BEGIN;{sql};PRAGMA user_version={number};COMMIT;")

    assert bl.migrate_database(empty, out=lambda _line: None) == bl.DB_LATEST
    assert bl.migrate_database(old, out=lambda _line: None) == bl.DB_LATEST

    for path in (empty, old):
        with closing(bl.connect(path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == bl.DB_LATEST
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('book_collections', 'collections')")}
            assert names == {"book_collections", "collections"}


def test_rebuild_collections_folder_dedup_zip_and_unresolved(coll_db: str) -> None:
    with closing(bl.connect(coll_db)) as conn, conn:
        counts = bl.rebuild_collections(conn)
        rows = {tuple(row) for row in conn.execute("SELECT path, section, collection, source, orig_path FROM book_collections")}
        books_in_series_y = conn.execute("SELECT books FROM collections WHERE collection='Серия Y'").fetchone()[0]

    section = bl.section_name("_Сборники")
    assert counts == {"folder": 1, "dedup": 2, "unresolved": 1}
    assert rows == {
        ("_Сборники/Серия X/a.epub", section, "Серия X", "folder", "_Сборники/Серия X/a.epub"),
        ("Автор/b.epub", section, "Серия Y", "dedup", "_Сборники/Серия Y/b.epub"),
        ("Автор/c.txt.zip", section, "Серия Y", "dedup", "_Сборники/Серия Y/c.txt"),
    }
    assert books_in_series_y == 2


def test_relink_moves_collection_rows_and_keeper(coll_db: str) -> None:
    with closing(bl.connect(coll_db)) as conn, conn:
        bl.rebuild_collections(conn)

    bl.relink(coll_db, [("Автор/b.epub", "Автор/b2.epub")])

    with closing(bl.connect(coll_db)) as conn:
        paths = {row[0] for row in conn.execute("SELECT path FROM book_collections")}
        keeper = conn.execute("SELECT keeper FROM dup_members WHERE path=?", ("_Сборники/Серия Y/b.epub",)).fetchone()[0]
    assert "Автор/b2.epub" in paths
    assert "Автор/b.epub" not in paths
    assert keeper == "Автор/b2.epub"


def test_collection_filter_finds_keeper_from_other_section(
        tmp_path: Path, api_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "books"
    (root / "Автор").mkdir(parents=True)
    make_epub(root / "Автор" / "b.epub")
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        insert_duplicate(conn, "_Сборники/Серия Y/b.epub", "Автор/b.epub")
        bl.rebuild_collections(conn)

    server = _serving(cfg, monkeypatch)
    base = next(server)
    try:
        status, body = api_call(f"{base}/api/books?collection={quote('Серия Y')}&section={quote(bl.section_name('_Сборники'))}")
    finally:
        server.close()

    assert status == 200
    assert body["total"] == 1
    assert body["items"][0]["path"] == "Автор/b.epub"
    assert body["items"][0]["collections"] == [{"section": bl.section_name("_Сборники"), "collection": "Серия Y",
                                                "subsection": "", "kind": "dedup", "source": "dedup"}]


def test_collections_view_counts_only_books_in_catalog_and_rebuild_keeps_rows(coll_db: str) -> None:
    with closing(bl.connect(coll_db)) as conn, conn:
        bl.rebuild_collections(conn)
        conn.execute("DELETE FROM books WHERE path=?", ("Автор/c.txt.zip",))
        bl.rebuild_collections(conn)  # повторная пересборка только дополняет: строка для удалённой книги остаётся
        kept = conn.execute("SELECT count(*) FROM book_collections WHERE path=?", ("Автор/c.txt.zip",)).fetchone()[0]
        books_in_series_y = conn.execute("SELECT books FROM collections WHERE collection='Серия Y'").fetchone()[0]

    assert kept == 1
    assert books_in_series_y == 1


def test_rebuild_keeps_collection_of_book_moved_by_relink(coll_db: str) -> None:
    old, new = "_Сборники/Серия X/a.epub", "Иванов/a.epub"
    with closing(bl.connect(coll_db)) as conn, conn:
        bl.rebuild_collections(conn)
    bl.relink(coll_db, [(old, new)])
    with closing(bl.connect(coll_db)) as conn, conn:
        conn.execute("DELETE FROM books WHERE path=?", (old,))
        insert_book(conn, new)  # файл переехал: в books уже без сборника
        bl.rebuild_collections(conn)
        rows = {tuple(row) for row in conn.execute(
            "SELECT path, section, collection, source, orig_path FROM book_collections WHERE path=?", (new,))}

    assert rows == {(new, bl.section_name("_Сборники"), "Серия X", "folder", old)}


def test_author_key_rejects_placeholders_and_digits() -> None:
    assert bl.author_key("Ёлкин Пётр") == frozenset({"елкин", "петр"})
    assert bl.author_key("Пётр Елкин") == bl.author_key("Ёлкин Пётр")
    for junk in ("", "Unknown", "Неизвестный автор", "esimych", "Автор 2", "А", "Один Два Три Четыре Пять Шесть", "-"):
        assert bl.author_key(junk) is None, junk


def test_pick_folder_exact_parent_subset_new() -> None:
    key = bl.author_key("Иван Иванов")
    assert bl.pick_folder("Иван Иванов", key, "", {key: "Иванов Иван"}) == ("Иванов Иван", "exact")
    petr = bl.author_key("Пётр Петров")
    assert bl.pick_folder("Пётр Петров", petr, "Петров Пётр", {}) == ("Петров Пётр", "parent")
    patronymic = bl.author_key("Павел Сергеевич Рототаев")
    assert bl.pick_folder("Павел Сергеевич Рототаев", patronymic, "Рототаев Павел", {}) == ("Рототаев Павел", "parent")
    assert bl.author_key("Составитель Андрей Синицын Коллектив Авторов") is None
    author = "Иван Петрович Сидоров"
    one = {bl.author_key("Сидоров Иван"): "Сидоров Иван"}
    assert bl.pick_folder(author, bl.author_key(author), "", one) == ("Сидоров Иван", "subset")
    two = {bl.author_key("Сидоров Иван"): "Сидоров Иван", bl.author_key("Сидоров Петрович"): "Сидоров Петрович"}
    assert bl.pick_folder(author, bl.author_key(author), "", two) == ("Сидоров Иван Петрович", "new")
    assert bl.pick_folder("Иван", bl.author_key("Иван"), "", {}) == ("Иван", "new")


def test_person_name_accepts_people_and_rejects_titles_and_genres() -> None:
    for person in ("Толстой Лев Николаевич", "де Сервантес Мигель", "д'Артаньян Шарль", "Бонч-Бруевич Владимир",
                   "Толкин Дж. Р. Р.", "Кадыр-заде Салам", "АЛЬТШУЛЛЕР Г. С", "Хью Дэфид аб"):
        assert bl.person_name(person), person
    for junk in ("драконов Охота на", "эпос Японский", "вБИТПЫЙО оЙЛПМБК", "автора без", "Tritsch by Bernhard",
                 "Рафел Н и", "Р‘СЂР°СѓРЅ Р¤СЂРµРґРµСЂРёРє", "ЧЧЧЧ"):
        assert not bl.person_name(junk), junk
    assert not bl.person_name("DNS")
    assert not bl.person_name("БСЭ")
    assert bl.person_name("ИВАНОВ ИВАН")
    assert bl.person_name("Гомер")
    assert bl.person_name("А. Иванов")
    assert bl.author_key("ВЕЧЕ ИЗДАТЕЛЬСТВО") is None


def test_pick_folder_not_person_creates_no_folder() -> None:
    assert bl.pick_folder("Охота на драконов", frozenset({"охота", "на", "драконов"}), "", {}) == ("", "not-person")


def test_split_authors_first_author_is_folder_and_rest_are_coauthors() -> None:
    assert bl.split_authors("Григорий Белых, Алексей Пантелеев") == ["Григорий Белых", "Алексей Пантелеев"]
    assert bl.split_authors("Григорий Белых и Алексей Пантелеев; Иван Иванов") == \
        ["Григорий Белых", "Алексей Пантелеев", "Иван Иванов"]
    assert bl.split_authors("Охота на драконов, эпос Японский") == ["Охота на драконов, эпос Японский"]
    first = bl.split_authors("Григорий Белых, Алексей Пантелеев")[0]
    assert bl.pick_folder(first, bl.author_key(first), "", {}) == ("Белых Григорий", "new")


def test_pick_folder_initials_before_surname_and_particle_keep_order() -> None:
    assert bl.pick_folder("И.Гарин", bl.author_key("И.Гарин"), "", {}) == ("Гарин И", "new")
    assert bl.pick_folder("И.О.Гарин", bl.author_key("И.О.Гарин"), "", {}) == ("Гарин И.О", "new")
    existing = {bl.author_key("Гарин Иван"): "Гарин Иван"}
    assert bl.pick_folder("И.Гарин", bl.author_key("И.Гарин"), "", existing) == ("Гарин Иван", "initials")
    assert bl.pick_folder("Де Рокамболь", bl.author_key("Де Рокамболь"), "", {}) == ("Де Рокамболь", "new")


def unpack_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Каталог: автор «Иванов Иван» (папка есть), раздел сборников с двумя книгами одного имени, книгой без автора
    и пустой папкой."""
    root = tmp_path / "unpack_books"
    (root / "Иванов Иван").mkdir(parents=True)
    (root / "_Сборники" / "Серия X").mkdir(parents=True)
    (root / "_Сборники" / "Серия Y").mkdir(parents=True)
    (root / "_Сборники" / "Пустая").mkdir()
    (root / "_Сборники" / "Серия X" / "Иван Иванов - Книга.fb2").write_bytes(make_fb2("Книга", "Иван", "Иванов"))
    (root / "_Сборники" / "Серия Y" / "Иван Иванов - Книга.fb2").write_bytes(make_fb2("Книга", "Иван", "Иванов"))
    (root / "_Сборники" / "Серия X" / "Заметки.txt").write_text("Текст без автора.\n", encoding="utf-8")
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)
    return cfg


def test_unpack_collections_moves_books_relinks_and_removes_empty_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = unpack_cfg(tmp_path, monkeypatch)
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)",
                     ("_Сборники/Серия X/Иван Иванов - Книга.fb2", "done", 1.0))

    stats = bl.unpack_collections(cfg)

    assert stats["moved"] == 2
    assert stats["into_existing"] == 2
    assert stats["conflicts"] == 1
    assert stats["no_author"] == 1
    assert stats["removed_dirs"] == 2  # «Серия Y» опустела, «Пустая» была пустой; «Серия X» с заметкой остаётся
    assert (root / "Иванов Иван" / "Иван Иванов - Книга.fb2").is_file()
    assert (root / "Иванов Иван" / "Иван Иванов - Книга_1.fb2").is_file()
    assert (root / "_Сборники" / "Серия X" / "Заметки.txt").is_file()
    assert not (root / "_Сборники" / "Серия Y").exists()
    assert not (root / "_Сборники" / "Пустая").exists()
    assert (root / "_Сборники").is_dir()
    with closing(bl.connect(cfg["db_path"])) as conn:
        collection = dict(conn.execute("SELECT path, collection FROM book_collections WHERE source='folder'").fetchall())
        marks = [r[0] for r in conn.execute("SELECT path FROM marks")]
    assert collection["Иванов Иван/Иван Иванов - Книга.fb2"] == "Серия X"
    assert collection["Иванов Иван/Иван Иванов - Книга_1.fb2"] == "Серия Y"
    assert marks == ["Иванов Иван/Иван Иванов - Книга.fb2"]


def test_unpack_collections_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = unpack_cfg(tmp_path, monkeypatch)
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn:
        before = conn.execute("SELECT count(*) FROM book_collections").fetchone()[0]

    stats = bl.unpack_collections(cfg, dry_run=True, out=lambda _line: None)

    assert stats["moved"] == 2
    assert stats["removed_dirs"] == 2
    assert (root / "_Сборники" / "Серия X" / "Иван Иванов - Книга.fb2").is_file()
    assert (root / "_Сборники" / "Серия Y").is_dir()
    assert not (root / "Иванов Иван" / "Иван Иванов - Книга.fb2").exists()
    with closing(bl.connect(cfg["db_path"])) as conn:
        assert conn.execute("SELECT count(*) FROM book_collections").fetchone()[0] == before


def sort_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Каталог: автор «Иванов Иван Иванович» (папка есть), служебные папки «_» с книгами, у которых автор берётся из
    имени файла, текста или папки; книга без автора."""
    root = tmp_path / "sort_books"
    (root / "Иванов Иван Иванович").mkdir(parents=True)
    (root / "Иванов Иван Иванович" / "Старая.fb2").write_bytes(make_fb2("Старая", "Иван", "Иванов"))
    (root / "_documentation").mkdir()
    (root / "_documentation" / "Иванов Иван.-.Заметка.txt").write_text("Заметка.\n", encoding="utf-8")
    (root / "_разобрать").mkdir()
    (root / "_разобрать" / "Лесные заметки.txt").write_text("Иван Иванов. Лесные заметки\n\nТекст.\n", encoding="utf-8")
    (root / "_разобрать" / "Пустой.txt").write_text("Текст без автора.\n", encoding="utf-8")
    (root / "_Журналы" / "Иванов Иван Иванович").mkdir(parents=True)
    (root / "_Журналы" / "Иванов Иван Иванович" / "Номер 1.txt").write_text("Номер.\n", encoding="utf-8")
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)
    return cfg


def test_sort_underscore_takes_author_from_filename_text_and_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = sort_cfg(tmp_path, monkeypatch)
    root = Path(cfg["books_dir"])

    stats = bl.sort_underscore(cfg, out=lambda _line: None)

    assert stats["moved"] == 4  # три с автором и «Пустой.txt» в «_1tmp»
    assert stats["to_unsorted"] == 1
    assert stats["source_filename"] == 1
    assert stats["source_text"] == 1
    assert stats["source_folder"] == 1
    assert stats.get("no_author", 0) == 0
    assert (root / "Иванов Иван Иванович" / "Иванов Иван.-.Заметка.txt").is_file()
    assert (root / "Иванов Иван Иванович" / "Лесные заметки.txt").is_file()
    assert (root / "Иванов Иван Иванович" / "Номер 1.txt").is_file()
    assert (root / "_1tmp" / "Пустой.txt").is_file()
    assert not (root / "_разобрать" / "Пустой.txt").exists()
    assert not (root / "_Журналы" / "Иванов Иван Иванович").exists()


def test_sort_underscore_dry_run_respects_limit_and_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = sort_cfg(tmp_path, monkeypatch)
    root = Path(cfg["books_dir"])

    stats = bl.sort_underscore(cfg, dry_run=True, limit=1, out=lambda _line: None)

    assert stats["moved"] == 1
    assert (root / "_documentation" / "Иванов Иван.-.Заметка.txt").is_file()
    assert (root / "_разобрать" / "Лесные заметки.txt").is_file()


def test_variant_target_prefers_folder_without_initials() -> None:
    same = {"Д.Саймак Клиффорд": 3, "Саймак Клиффорд": 3}
    assert bl.variant_target("Д.Саймак Клиффорд", "Саймак Клиффорд", same) == ("Д.Саймак Клиффорд", "Саймак Клиффорд")
    assert bl.variant_target("Саймак Клиффорд", "Д.Саймак Клиффорд", same) == ("Д.Саймак Клиффорд", "Саймак Клиффорд")
    more_files = {"Ж.Фармер Филип": 9, "Фармер Филип Жозе": 1}
    assert bl.variant_target("Ж.Фармер Филип", "Фармер Филип Жозе", more_files) == (
        "Ж.Фармер Филип", "Фармер Филип Жозе")


def test_periodical_rules_and_table_validation(tmp_path: Path) -> None:
    assert bl.is_periodical("Газета Завтра Газета")
    assert bl.is_periodical("Оглавление журнала \"Киносценарии\"")
    assert not bl.is_periodical("Иванов Иван Иванович")
    assert bl.periodical_folder("Газета Завтра Газета", {}) == "_Журналы/Завтра"
    assert bl.periodical_folder("Журнал «Вестник» Журнал", {}) == "_Журналы/Вестник"
    assert bl.periodical_folder("Журнал Газета", {}) == ""
    assert bl.load_periodicals(tmp_path / "нет.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"Журнал А": "Иванов Иван"}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(bl.ConfigError):
        bl.load_periodicals(bad)


def test_periodical_author_goes_to_journals_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table = tmp_path / "periodicals.json"
    table.write_text(json.dumps({"Газета Завтра Газета": "_Журналы/Завтра"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bl, "PERIODICALS_FILE", table)
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    root = tmp_path / "periodical_books"
    (root / "_Сборники" / "X").mkdir(parents=True)
    (root / "_Сборники" / "X" / "Номер 1.fb2").write_bytes(make_fb2_author("Номер 1", "Газета Завтра Газета"))
    (root / "_Сборники" / "X" / "Номер 2.fb2").write_bytes(make_fb2_author("Номер 2", "Газета Завтра Газета"))
    (root / "_Журналы" / "Завтра").mkdir(parents=True)
    (root / "_Журналы" / "Завтра" / "Номер 2.fb2").write_bytes(make_fb2_author("Номер 2", "Газета Завтра Газета"))
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    stats = bl.unpack_collections(cfg, out=lambda _line: None)

    assert stats["periodicals"] == 1
    assert stats["conflicts"] == 1
    assert (root / "_Журналы" / "Завтра" / "Номер 1.fb2").is_file()
    assert (root / "_Сборники" / "X" / "Номер 2.fb2").is_file()  # занято — книга на месте, без «_N»
    assert not (root / "_Журналы" / "Завтра" / "Номер 2_1.fb2").exists()
    assert not (root / "Газета Завтра Газета").exists()


def test_sort_underscore_moves_authorless_books_to_1tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "unsorted_books"
    (root / "_разобрать" / "Библиотека Мошкова" / "Мошков часть 4" / "LIB").mkdir(parents=True)
    (root / "_разобрать" / "Библиотека Мошкова" / "Мошков часть 4" / "LIB" / "x.txt").write_text(
        "Текст без автора один.\n", encoding="utf-8")
    (root / "_Сборники" / "Сборник").mkdir(parents=True)
    (root / "_Сборники" / "Сборник" / "a.txt").write_text("Текст без автора два.\n", encoding="utf-8")
    (root / "_разобрать" / "Конфликт.txt").write_text("Текст без автора три.\n", encoding="utf-8")
    (root / "_1tmp").mkdir()
    (root / "_1tmp" / "Конфликт.txt").write_text("Занято.\n", encoding="utf-8")
    (root / "_Серии").mkdir()
    (root / "_Серии" / "Серия.txt").write_text("Текст без автора четыре.\n", encoding="utf-8")
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    stats = bl.sort_underscore(cfg, out=lambda _line: None)

    assert stats["to_unsorted"] == 2
    assert stats["conflicts"] == 1
    assert (root / "_1tmp" / "Мошков часть 4" / "LIB" / "x.txt").is_file()
    assert (root / "_1tmp" / "a.txt").is_file()
    assert (root / "_разобрать" / "Конфликт.txt").is_file()
    assert (root / "_1tmp" / "Конфликт.txt").read_text(encoding="utf-8") == "Занято.\n"
    assert (root / "_Серии" / "Серия.txt").is_file()  # не служебный раздел из набора — остаётся на месте


def test_unsorted_path_drops_only_collection_name() -> None:
    assert bl.unsorted_rel("_разобрать/Библиотека Мошкова/Мошков часть 4/LIB/x.txt") == "_1tmp/Мошков часть 4/LIB/x.txt"
    assert bl.unsorted_rel("_Сборники/Сборник/a.epub") == "_1tmp/a.epub"
    assert bl.unsorted_rel("_разобрать/a.txt") == "_1tmp/a.txt"


def test_filename_author_separators_and_limits() -> None:
    assert bl.filename_author("Иванов И.О. - Книга") == "Иванов И.О."
    assert bl.filename_author("Иванов_Иван_-_Книга") == "Иванов Иван"
    assert bl.filename_author("Иванов Иван.-.Книга") == "Иванов Иван"
    assert bl.filename_author("Книга без автора") == ""
    assert bl.filename_author("Глава 2 - Книга") == ""


def test_text_author_name_alone_on_line() -> None:
    index = {frozenset({"лем", "станислав"}): "Лем Станислав", frozenset({"чапек", "карел"}): "Чапек Карел"}
    assert bl.text_author("Станислав Лем\n\n   КАК МИКРОМИЛ\n", index) == ("Лем Станислав", "Станислав Лем")
    assert bl.text_author("Карел Чапек. \n\n Эксперимент\n", index) == ("Чапек Карел", "Карел Чапек")
    # имя-строка на 7-й непустой строке (позиция 6) не принимается
    head = "".join(f"строка {i}\n" for i in range(6)) + "Станислав Лем\nНазвание\n"
    assert bl.text_author(head, index) == ("", "")
    # одиночная последняя строка «Станислав Лем» без следующей строки не принимается
    assert bl.text_author("Станислав Лем", index) == ("", "")


def test_text_author_name_alone_forms() -> None:
    index = {
        frozenset({"булычев", "кир"}): "Булычев Кир",
        frozenset({"мерфи", "уоррен"}): "Мерфи Уоррен",
        frozenset({"загоскин", "михаил", "николаевич"}): "Загоскин Михаил Николаевич",
        frozenset({"маринина", "александра", "борисовна"}): "Маринина Александра Борисовна",
    }
    # фамилия КАПСОМ
    assert bl.text_author("Кир БУЛЫЧЕВ\nБЕЛОЕ ПЛАТЬЕ\n", index) == ("Булычев Кир", "Кир БУЛЫЧЕВ")
    # соавторы через запятую: берётся первый
    assert bl.text_author("Уоррен МЕРФИ, Ричард СЭПИР\n\"СВЯЩЕННЫЙ УЖАС\"\n", index) == ("Мерфи Уоррен", "Уоррен МЕРФИ")
    # «И. О. Фамилия» → «Фамилия И.О.»
    assert bl.text_author("М. Н. Загоскин.\nРоман\n", index) == ("Загоскин Михаил Николаевич", "Загоскин М.Н.")
    # «Фамилия И. О.» → «Фамилия И.О.»
    assert bl.text_author("Маринина А. Б.\nСветлый лик смерти\n", index) == \
        ("Маринина Александра Борисовна", "Маринина А.Б.")
    # без папки в библиотеке — пусто
    assert bl.text_author("Джон СТЕЙНБЕК\nЗаблудившийся автобус\n", index) == ("", "")


def write_epub_chapters(path: Path, chapters: list[str]) -> None:
    """EPUB без метаданных автора: главы — тела <body> в порядке spine."""
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )
    manifest = "".join(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
                       for i in range(len(chapters)))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Роман</dc:title></metadata>'
           f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        for i, body in enumerate(chapters):
            archive.writestr(f"OEBPS/ch{i}.xhtml", '<?xml version="1.0" encoding="utf-8"?>'
                             f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>')


def test_serial_filename_folder_needs_exactly_one_folder_with_surname() -> None:
    index = {frozenset({"кейт", "дэвид"}): "Кейт Дэвид", frozenset({"андерсон", "кевин"}): "Андерсон Кевин",
             frozenset({"андерсон", "пол"}): "Андерсон Пол", frozenset({"фёдоров", "иван"}): "Фёдоров Иван"}
    assert bl.serial_filename_folder("Война Роботов 01(шагающая Смерть 1)кейт", index) == "Кейт Дэвид"
    # две папки с фамилией «Андерсон» — не угадываем
    assert bl.serial_filename_folder("Звездные Войны 002(проклятье Ситхов)андерсон", index) == ""
    # ё→е при сравнении
    assert bl.serial_filename_folder("Вселенная 3(тень)федоров", index) == "Фёдоров Иван"
    # папки с такой фамилией нет
    assert bl.serial_filename_folder("Война Роботов 11(естественный  Отбор)стакпол", index) == ""
    # фамилия должна быть строчными после «)»
    assert bl.serial_filename_folder("Книга (часть)Кейт", index) == ""
    assert bl.serial_filename_folder("Книга без скобок", index) == ""


def test_folder_author_number_coauthors_and_initials() -> None:
    index = {frozenset({"уэйс", "михаил"}): "Уэйс Михаил", frozenset({"сальваторе", "роберт"}): "Сальваторе Роберт"}
    assert bl.folder_author_name("1. М.Уэйс и Т.Хикмен") == "Уэйс М."
    assert bl.folder_author_name("2) Р.Сальваторе") == "Сальваторе Р."
    assert bl.folder_author_name("3) Иван Иванов & Пётр Петров") == "Иван Иванов"
    assert bl.folder_author_name("Иван Иванов") == "Иван Иванов"
    assert bl.folder_for_name(bl.folder_author_name("1. М.Уэйс и Т.Хикмен"), index) == "Уэйс Михаил"
    assert bl.folder_for_name(bl.folder_author_name("2) Р.Сальваторе"), index) == "Сальваторе Роберт"
    assert bl.folder_for_name(bl.folder_author_name("1. Д.Кейт"), index) == ""


def test_epub_head_reads_only_leading_chapters(tmp_path: Path) -> None:
    path = tmp_path / "Роман.epub"
    write_epub_chapters(path, ["<p>Станислав Лем</p>\n<p>КАК МИКРОМИЛ</p>\n", "<p>ВТОРАЯ ГЛАВА</p>\n"])
    head = bl.epub_head(path, 10)
    assert "Станислав Лем" in head
    assert "ВТОРАЯ" not in head
    assert "ВТОРАЯ" in bl.epub_head(path, 10 ** 6)


def test_sort_underscore_serial_numbered_folder_and_epub_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "sort_books"
    for folder in ("Кейт Дэвид", "Андерсон Кевин", "Андерсон Пол", "Фёдоров Иван", "Уэйс Михаил",
                   "Сальваторе Роберт", "Лем Станислав"):
        (root / folder).mkdir(parents=True)
    serial = root / "_Серии"
    serial.mkdir()
    (serial / "Война Роботов 01(шагающая Смерть 1)кейт.txt").write_text("Текст.\n", encoding="utf-8")
    (serial / "Война Роботов 11(естественный  Отбор)кейт.txt").write_text("Текст.\n", encoding="utf-8")
    (serial / "Звездные Войны 002(проклятье Ситхов)андерсон.txt").write_text("Текст.\n", encoding="utf-8")
    (serial / "Вселенная 3(тень)федоров.txt").write_text("Текст.\n", encoding="utf-8")
    (root / "_разобрать" / "1. М.Уэйс и Т.Хикмен").mkdir(parents=True)
    (root / "_разобрать" / "1. М.Уэйс и Т.Хикмен" / "Книга.txt").write_text("Текст.\n", encoding="utf-8")
    (root / "_разобрать" / "2) Р.Сальваторе").mkdir(parents=True)
    (root / "_разобрать" / "2) Р.Сальваторе" / "Другая.txt").write_text("Текст.\n", encoding="utf-8")
    write_epub_chapters(root / "_разобрать" / "Роман.epub", ["<p>Станислав Лем</p>\n<p>КАК МИКРОМИЛ</p>\n"])
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    stats = bl.sort_underscore(cfg, out=lambda _line: None)

    assert stats["moved"] == 6
    assert stats["source_filename"] == 3
    assert stats["source_folder"] == 2
    assert stats["source_text"] == 1
    assert stats["no_author"] == 1
    assert (root / "Кейт Дэвид" / "Война Роботов 01(шагающая Смерть 1)кейт.txt").is_file()
    assert (root / "Кейт Дэвид" / "Война Роботов 11(естественный  Отбор)кейт.txt").is_file()
    assert (root / "Фёдоров Иван" / "Вселенная 3(тень)федоров.txt").is_file()
    assert (root / "Уэйс Михаил" / "Книга.txt").is_file()
    assert (root / "Сальваторе Роберт" / "Другая.txt").is_file()
    assert (root / "Лем Станислав" / "Роман.epub").is_file()
    assert (serial / "Звездные Войны 002(проклятье Ситхов)андерсон.txt").is_file()


# ---- merge-authors: склейка «Фамилия Имя» + «Фамилия Имя Отчество» ---------------------------------------

def merge_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str | bytes]) -> dict[str, Any]:
    """Каталог с файлами (относительные пути → содержимое), журналы — во временном каталоге, затем scan."""
    root = tmp_path / "merge_books"
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)
    return cfg


def test_merge_moves_short_folder_into_full_and_relinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/a.txt": "А.\n", "Иванов Иван/sub/b.txt": "Б.\n",
        "Иванов Иван Петрович/a.txt": "Другой.\n", "Иванов Иван Петрович/c.txt": "В.\n",
    })
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", ("Иванов Иван/a.txt", "done", 1.0))

    stats = bl.merge_authors(cfg)

    full = root / "Иванов Иван Петрович"
    assert stats["merged"] == 1
    assert stats["conflicts"] == 1
    assert stats["moved"] == 2
    assert not (root / "Иванов Иван").exists()
    assert (full / "a.txt").read_text(encoding="utf-8") == "Другой.\n"
    assert (full / "a_1.txt").read_text(encoding="utf-8") == "А.\n"
    assert (full / "sub" / "b.txt").is_file()
    assert (full / "c.txt").is_file()
    with closing(bl.connect(cfg["db_path"])) as conn:
        marks = [r[0] for r in conn.execute("SELECT path FROM marks")]
    assert marks == ["Иванов Иван Петрович/a_1.txt"]


def test_merge_renames_folder_by_patronymic_from_book_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Сидоров Пётр/Книга.fb2": make_fb2("Книга", "Пётр Ильич", "Сидоров"),  # автор в метаданных: «Сидоров Пётр Ильич»
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats["renamed"] == 1
    assert not (root / "Сидоров Пётр").exists()
    assert (root / "Сидоров Пётр Ильич" / "Книга.fb2").is_file()


def test_merge_ambiguous_patronymics_change_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Петров Пётр/a.txt": "А.\n",
        "Петров Пётр Иванович/b.txt": "Б.\n",
        "Петров Пётр Сергеевич/c.txt": "В.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats["ambiguous"] == 1
    assert stats.get("merged", 0) == 0
    assert (root / "Петров Пётр" / "a.txt").is_file()
    assert (root / "Петров Пётр Иванович" / "b.txt").is_file()
    assert (root / "Петров Пётр Сергеевич" / "c.txt").is_file()


def test_merge_initial_is_not_a_full_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Петров Иван/a.txt": "А.\n",
        "Петров Иван В/b.txt": "Б.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats.get("merged", 0) == 0
    assert stats.get("renamed", 0) == 0
    assert (root / "Петров Иван" / "a.txt").is_file()
    assert (root / "Петров Иван В" / "b.txt").is_file()


def test_merge_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/a.txt": "А.\n",
        "Иванов Иван Петрович/c.txt": "В.\n",
    })
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", ("Иванов Иван/a.txt", "done", 1.0))

    stats = bl.merge_authors(cfg, dry_run=True, out=lambda _line: None)

    assert stats["moved"] == 1
    assert (root / "Иванов Иван" / "a.txt").is_file()
    assert not (root / "Иванов Иван Петрович" / "a.txt").exists()
    with closing(bl.connect(cfg["db_path"])) as conn:
        assert [r[0] for r in conn.execute("SELECT path FROM marks")] == ["Иванов Иван/a.txt"]


def test_merge_initials_folder_joins_full_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Загоскин М. Н/a.txt": "А.\n",
        "Загоскин Михаил Николаевич/b.txt": "Б.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats["initials"] == 1
    assert not (root / "Загоскин М. Н").exists()
    assert (root / "Загоскин Михаил Николаевич" / "a.txt").read_text(encoding="utf-8") == "А.\n"
    assert (root / "Загоскин Михаил Николаевич" / "b.txt").is_file()


def test_merge_ambiguous_initials_change_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Борисов А/a.txt": "А.\n",
        "Борисов Андрей/b.txt": "Б.\n",
        "Борисов Алексей Петрович/c.txt": "В.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats.get("initials", 0) == 0
    assert (root / "Борисов А" / "a.txt").is_file()
    assert (root / "Борисов Андрей" / "b.txt").is_file()
    assert (root / "Борисов Алексей Петрович" / "c.txt").is_file()


SURNAMES_WITH_IVAN = ["Аксенов", "Белкин", "Волков", "Гаврилов", "Громов", "Данилов", "Егоров", "Жуков", "Зайцев", "Ильин"]


def test_merge_name_first_folder_is_reordered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str] = {f"{surname} Иван/a.txt": "А.\n" for surname in SURNAMES_WITH_IVAN}
    files["Иван Тестовый/b.txt"] = "Б.\n"
    cfg = merge_cfg(tmp_path, monkeypatch, files)
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats["name_first"] == 1
    assert not (root / "Иван Тестовый").exists()
    assert (root / "Тестовый Иван" / "b.txt").is_file()


def test_merge_name_first_joins_existing_full_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str] = {f"{surname} Иван/a.txt": "А.\n" for surname in SURNAMES_WITH_IVAN}
    files["Иван Петров/a.txt"] = "А.\n"
    files["Петров Иван Сергеевич/c.txt"] = "В.\n"
    cfg = merge_cfg(tmp_path, monkeypatch, files)
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats["name_first"] == 1
    assert not (root / "Иван Петров").exists()
    assert (root / "Петров Иван Сергеевич" / "a.txt").is_file()
    assert (root / "Петров Иван Сергеевич" / "c.txt").is_file()
    assert not (root / "Петров Иван").exists()


def test_merge_patronymic_from_other_book_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Петров Иван/a.txt": "А.\n",  # в метаданных папки отчества нет
        "Иванов Сергей/Книга.fb2": make_fb2("Книга", "Иван Сергеевич", "Петров"),  # в БД другой книги: «Петров Иван Сергеевич»
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats["renamed"] == 1
    assert stats["patronymic_db"] == 1
    assert not (root / "Петров Иван").exists()
    assert (root / "Петров Иван Сергеевич" / "a.txt").is_file()


def test_merge_other_book_name_without_patronymic_is_not_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Петров Иван/a.txt": "А.\n",
        "Иванов Сергей/Книга.fb2": make_fb2("Книга", "Иван Натали", "Петров"),  # «Натали» — не отчество
    })
    root = Path(cfg["books_dir"])

    stats = bl.merge_authors(cfg)

    assert stats.get("renamed", 0) == 0
    assert stats.get("patronymic_db", 0) == 0
    assert (root / "Петров Иван" / "a.txt").is_file()

def test_unzip_keeps_double_dot_inside_name(unzip_cfg: dict[str, Any]) -> None:
    root = Path(unzip_cfg["books_dir"])
    make_archive(root / "Смолски С..djvu.zip", "Смолски С..djvu", b"djvu-data")

    bl.unzip_books(unzip_cfg)

    assert (root / "Смолски С..djvu").read_bytes() == b"djvu-data"
    assert not (Path(unzip_cfg["quarantine_dir"]) / "rejected-name").exists()


# ---- dedup-folders, author-variants, соавторские копии --------------------------------------------

def folder_text(words: int, tag: str) -> str:
    """Текст из слов tag0, tag1…: разные теги дают тексты без общих шинглов."""
    return " ".join(f"{tag}{i}" for i in range(words))


def write_txt(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def db_rows(cfg: dict[str, Any], sql: str) -> list[tuple[Any, ...]]:
    with closing(bl.connect(cfg["db_path"], readonly=True)) as conn:
        return [tuple(row) for row in conn.execute(sql).fetchall()]


@pytest.fixture
def authors_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "authors"
    root.mkdir()
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    return make_cfg(tmp_path, root)


def test_dedup_folders_dry_run_changes_nothing(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    write_txt(folder / "Тёмный лес.txt", folder_text(300, "слово"))
    write_txt(folder / "Temny les.txt", folder_text(300, "слово"))

    result = bl.dedup_folders(authors_cfg, dry_run=True, out=lambda _line: None)

    assert result["duplicate"] == 1
    assert (folder / "Temny les.txt").is_file()
    assert not Path(authors_cfg["quarantine_dir"]).exists()
    assert not Path(authors_cfg["db_path"]).exists()


def test_dedup_folders_copy_goes_to_quarantine_and_group_is_written(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    write_txt(folder / "Тёмный лес.txt", folder_text(300, "слово"))
    write_txt(folder / "Temny les.txt", folder_text(300, "слово"))
    write_txt(folder / "Заметки.txt", folder_text(300, "иное"))

    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result["duplicate"] == 1
    assert (folder / "Тёмный лес.txt").is_file() and (folder / "Заметки.txt").is_file()
    assert not (folder / "Temny les.txt").exists()
    assert (Path(authors_cfg["quarantine_dir"]) / "duplicate" / "Иванов Иван Иванович" / "Temny les.txt").is_file()
    assert db_rows(authors_cfg, "SELECT key, status FROM dup_groups") == [("folder:Иванов Иван Иванович", "auto")]
    members = db_rows(authors_cfg, "SELECT path, role, keeper FROM dup_members ORDER BY path")
    assert len(members) == 3
    assert ("Иванов Иван Иванович/Temny les.txt", "duplicate", "Иванов Иван Иванович/Тёмный лес.txt") in members


def test_dedup_folders_keeps_fuller_copy(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    write_txt(folder / "Тёмный лес.txt", folder_text(300, "слово"))
    write_txt(folder / "Тёмный лес (полная).txt", folder_text(390, "слово"))

    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result["duplicate"] == 1
    assert (folder / "Тёмный лес (полная).txt").is_file()
    assert not (folder / "Тёмный лес.txt").exists()


def test_dedup_folders_renames_translit_keeper_and_relinks(authors_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    folder.mkdir(parents=True)
    text = folder_text(300, "слово")
    (folder / "Temny.fb2").write_bytes(make_fb2("Тёмный лес", "Иван", "Иванов", body=f"<p>{text}</p>"))
    write_txt(folder / "Тёмный лес.txt", f"Глава 1 {text}")
    pairs: list[tuple[str, str]] = []

    def fake_relink(db_path: str, moved: list[tuple[str, str]]) -> int:
        pairs.extend(moved)
        return len(moved)

    monkeypatch.setattr(bl, "relink", fake_relink)
    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result["duplicate"] == 1 and result["renamed"] == 1
    assert not (folder / "Temny.fb2").exists()
    assert (folder / "Тёмный лес.fb2").is_file()
    assert (Path(authors_cfg["quarantine_dir"]) / "duplicate" / "Иванов Иван Иванович" / "Тёмный лес.txt").is_file()
    assert ("Иванов Иван Иванович/Temny.fb2", "Иванов Иван Иванович/Тёмный лес.fb2") in pairs
    assert ("Иванов Иван Иванович/Тёмный лес.txt", "Иванов Иван Иванович/Тёмный лес.fb2") in pairs


def test_dedup_folders_similar_copy_gets_own_group(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    original = folder_text(4000, "слово")
    # 3600 слов общих, 400 новых: доля общих шинглов около 0.9 — «похожая» (85–97 %)
    partly = " ".join(original.split()[:3600] + [f"новое{i}" for i in range(400)])
    write_txt(folder / "Лес.txt", original)
    write_txt(folder / "Лес2.txt", partly)  # другое имя: «Лес_2» после снятия «_N» было бы одноимённым → дубль

    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result.get("duplicate", 0) == 0 and result["similar"] == 1
    assert (folder / "Лес2.txt").is_file()
    assert db_rows(authors_cfg, "SELECT key, status FROM dup_groups") == [("folder:Иванов Иван Иванович/Лес2.txt", "similar")]
    assert sorted(db_rows(authors_cfg, "SELECT role FROM dup_members")) == [("kept",), ("kept",)]


def test_dedup_folders_same_name_similar_copy_is_duplicate(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    original = folder_text(4000, "слово")
    # то же имя без «_1», 3600 слов общих: доля около 0.9 — «похожая», но одноимённая → дубль
    partly = " ".join(original.split()[:3600] + [f"новое{i}" for i in range(400)])
    make_text_epub(folder / "А - Белка.epub", original, converted=False)
    make_text_epub(folder / "А - Белка_1.epub", partly, converted=False)

    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result["duplicate"] == 1 and result.get("similar", 0) == 0
    assert (folder / "А - Белка.epub").is_file()
    assert not (folder / "А - Белка_1.epub").exists()
    assert (Path(authors_cfg["quarantine_dir"]) / "duplicate" / "Иванов Иван Иванович" / "А - Белка_1.epub").is_file()


def test_dedup_folders_skips_section_and_too_large_folder(authors_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(authors_cfg["books_dir"])
    write_txt(root / "_Раздел" / "A.txt", folder_text(300, "слово"))
    write_txt(root / "_Раздел" / "B.txt", folder_text(300, "слово"))
    monkeypatch.setattr(bl, "DEDUP_FOLDER_MAX", 2)
    for name in ("A.txt", "B.txt", "C.txt"):
        write_txt(root / "Иванов Иван Иванович" / name, folder_text(300, "слово"))

    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result.get("duplicate", 0) == 0 and result["folder-too-large"] == 1
    assert (root / "_Раздел" / "B.txt").is_file() and (root / "Иванов Иван Иванович" / "C.txt").is_file()


def test_dedup_folders_rerun_keeps_normal_dedup_groups(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    write_txt(folder / "Тёмный лес.txt", folder_text(300, "слово"))
    write_txt(folder / "Temny les.txt", folder_text(300, "слово"))
    bl.dedup_folders(authors_cfg, out=lambda _line: None)
    with closing(bl.connect(authors_cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO dup_groups(key, status, created) VALUES ('Сидоров Сергей/Книга.fb2', 'similar', 1.0)")

    bl.dedup_folders(authors_cfg, out=lambda _line: None)

    groups = db_rows(authors_cfg, "SELECT key, status FROM dup_groups ORDER BY key")
    assert ("Сидоров Сергей/Книга.fb2", "similar") in groups
    assert groups.count(("folder:Иванов Иван Иванович", "auto")) == 1


def coauthor_fb2() -> bytes:
    """fb2 с двумя авторами в метаданных: «Иванов Иван» и «Петров Пётр» (как папки двух соавторов)."""
    book = make_fb2("Тёмный лес", "Иван", "Иванов", body=f"<p>{folder_text(300, 'слово')}</p>")
    second = "<author><first-name>Пётр</first-name><last-name>Петров</last-name></author>"
    return book.replace(b"</author>", ("</author>" + second).encode("utf-8"), 1)


def test_dedup_coauthor_copy_is_not_a_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "dups"
    book = coauthor_fb2()
    (root / "Иванов Иван").mkdir(parents=True)
    (root / "Петров Пётр").mkdir()
    (root / "Иванов Иван" / "Книга.fb2").write_bytes(book)
    (root / "Петров Пётр" / "Книга.fb2").write_bytes(book)
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)

    stats = bl.dedup(cfg, out=lambda _line: None)

    assert stats["coauthor-copy"] == 1 and stats["groups"] == 0
    assert (root / "Иванов Иван" / "Книга.fb2").is_file() and (root / "Петров Пётр" / "Книга.fb2").is_file()


def test_dedup_same_folder_copy_is_still_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "dups"
    book = coauthor_fb2()
    (root / "Иванов Иван").mkdir(parents=True)
    (root / "Петров Пётр").mkdir()
    (root / "Иванов Иван" / "Книга.fb2").write_bytes(book)
    (root / "Иванов Иван" / "Книга копия.fb2").write_bytes(book)
    (root / "Петров Пётр" / "Книга.fb2").write_bytes(book)
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)

    stats = bl.dedup(cfg, out=lambda _line: None)

    assert stats["coauthor-copy"] == 1 and stats["duplicate"] == 1
    assert len(list((Path(cfg["quarantine_dir"]) / "duplicate" / "Иванов Иван").iterdir())) == 1
    assert (root / "Петров Пётр" / "Книга.fb2").is_file()
    assert len(list((root / "Иванов Иван").iterdir())) == 1


def test_author_variants_empty_folder_is_removed(authors_cfg: dict[str, Any]) -> None:
    root = Path(authors_cfg["books_dir"])
    (root / "Ivanov Ivan" / "пусто").mkdir(parents=True)
    write_txt(root / "Иванов Иван" / "Тёмный лес.txt", TXT_TEXT)

    dry = bl.author_variants(authors_cfg, dry_run=True, out=lambda _line: None)

    assert dry["emptied"] == 1 and (root / "Ivanov Ivan").is_dir()
    bl.author_variants(authors_cfg, out=lambda _line: None)
    assert not (root / "Ivanov Ivan").exists()
    assert db_rows(authors_cfg, "SELECT a, b, status, target FROM author_variants") == [
        ("Ivanov Ivan", "Иванов Иван", "merged", "Иванов Иван")]


def test_author_variants_common_merges_into_full_name(authors_cfg: dict[str, Any]) -> None:
    root = Path(authors_cfg["books_dir"])
    write_txt(root / "Petrov Petr" / "Тёмный лес.txt", TXT_TEXT)
    write_txt(root / "Петров Пётр Иванович" / "Тёмный лес.txt", TXT_TEXT)
    bl.scan(authors_cfg)

    result = bl.author_variants(authors_cfg, out=lambda _line: None)

    assert result["merged"] == 1 and result["conflicts"] == 1
    assert not (root / "Petrov Petr").exists()
    target = root / "Петров Пётр Иванович"
    assert (target / "Тёмный лес.txt").is_file() and (target / "Тёмный лес_1.txt").is_file()
    assert db_rows(authors_cfg, "SELECT status, target FROM author_variants") == [("merged", "Петров Пётр Иванович")]


def test_author_variants_dry_run_and_different_are_not_recomputed(authors_cfg: dict[str, Any]) -> None:
    root = Path(authors_cfg["books_dir"])
    write_txt(root / "Smirnov Ivan" / "Лес.txt", TXT_TEXT)
    write_txt(root / "Смирнова Иван" / "Озеро.txt", TXT_TEXT)
    lines: list[str] = []

    dry = bl.author_variants(authors_cfg, dry_run=True, out=lines.append)

    assert dry.get("merged", 0) == 0 and (root / "Smirnov Ivan").is_dir() and not Path(authors_cfg["db_path"]).exists()
    bl.author_variants(authors_cfg, out=lambda _line: None)
    assert db_rows(authors_cfg, "SELECT tier, status FROM author_variants") == [("manual", "pending")]
    assert "Разные" in bl.variant_action(authors_cfg, "Смирнова Иван", "Smirnov Ivan", "different")
    lines.clear()
    bl.author_variants(authors_cfg, out=lines.append)
    assert lines[0].startswith("author-variants: пар 0 ")
    assert db_rows(authors_cfg, "SELECT tier, status FROM author_variants") == [("manual", "different")]


def test_author_variants_web_different_button_saves_status(web: str, scanned: dict[str, Any]) -> None:
    root = Path(scanned["books_dir"])
    write_txt(root / "Smirnov Ivan" / "Лес.txt", TXT_TEXT)
    write_txt(root / "Смирнова Иван" / "Озеро.txt", TXT_TEXT)
    bl.author_variants(scanned, out=lambda _line: None)

    form = f"a={quote('Smirnov Ivan')}&b={quote('Смирнова Иван')}&action=different".encode("ascii")
    status, headers, _body = fetch(f"{web}/authors/variant", data=form, headers={"Origin": web})

    assert status == 303
    assert headers.get("Location") == "/authors?variants=1"
    assert db_rows(scanned, "SELECT status FROM author_variants") == [("different",)]
    status, _headers, body = fetch(f"{web}/authors?variants=1")
    assert status == 200 and "Варианты написания".encode() in body


def test_author_variants_coauthor_folder_is_distributed_by_copy(authors_cfg: dict[str, Any]) -> None:
    root = Path(authors_cfg["books_dir"])
    write_txt(root / "Громыко Ольга" / "Другая.txt", folder_text(50, "иное"))
    write_txt(root / "Громыко Ольга, Уланов Андрей" / "Книга.txt", TXT_TEXT)

    bl.author_variants(authors_cfg, out=lambda _line: None)

    assert not (root / "Громыко Ольга, Уланов Андрей").exists()
    first, second = root / "Громыко Ольга" / "Книга.txt", root / "Уланов Андрей" / "Книга.txt"
    assert first.read_bytes() == second.read_bytes() == TXT_TEXT.encode("utf-8")
    assert not os.path.samefile(first, second)  # настоящая копия, не ссылка
    assert db_rows(authors_cfg, "SELECT status, target FROM author_variants") == [("merged", "Громыко Ольга")]


@pytest.mark.parametrize(("a", "b", "expected"), [
    ("Волков Алексей Алексеевич", "Волков Александр", False),
    ("Брэдбери Рэй Дуглас", "Бредбери Рэй", True),
    ("Плеханов Андрей Вячеславович", "Пллеханов Андрей", True),
    ("Ахманов Михаил Сергеевич", "Ахманов Mихаил", True),
    ("Волков А", "Волков Алексей", True),
    ("Иванов Иван Петрович", "Иванов Иван Сергеевич", False),
    ("Дилэни Сэмюэл Р", "Дилэни Сэмюель", True),
    ("Бир Грег", "Бир Грегори", True),
    ("Tolkien John", "Толкин Джон", True),
    ("Мамонова Мария", "Мамонова Марина", False),
    ("Гордон Джон", "Гордон Джордж", False),
])
def test_same_person(a: str, b: str, expected: bool) -> None:
    assert bl.same_person(a, b) is expected


def test_author_variants_does_not_glue_different_people(authors_cfg: dict[str, Any]) -> None:
    root = Path(authors_cfg["books_dir"])
    write_txt(root / "Волков Алексей Алексеевич" / "Тёмный лес.txt", TXT_TEXT)
    write_txt(root / "Волков Александр" / "Тёмный лес.txt", TXT_TEXT)
    bl.scan(authors_cfg)

    result = bl.author_variants(authors_cfg, out=lambda _line: None)

    assert result.get("merged", 0) == 0 and result["different"] == 1
    assert (root / "Волков Алексей Алексеевич").is_dir() and (root / "Волков Александр").is_dir()
    assert db_rows(authors_cfg, "SELECT status FROM author_variants") == [("pending",)]


def test_unpack_collections_coauthor_book_is_copied_to_second_author(authors_cfg: dict[str, Any]) -> None:
    root = Path(authors_cfg["books_dir"])
    name = "Иванов Иван, Петров Пётр - Книга.txt"
    write_txt(root / "_Сборники" / "Сборник" / name, TXT_TEXT)
    (root / "Иванов Иван").mkdir()
    (root / "Петров Пётр").mkdir()
    bl.scan(authors_cfg)

    stats = bl.unpack_collections(authors_cfg, out=lambda _line: None)

    first, second = root / "Иванов Иван" / name, root / "Петров Пётр" / name
    assert first.read_bytes() == second.read_bytes() == TXT_TEXT.encode("utf-8")
    assert not os.path.samefile(first, second)
    assert stats["copied"] == 1
    assert not (root / "_Сборники" / "Сборник" / name).exists()


# ----------------------------------------------------------------------
# Собственный конвертер fb2 → EPUB 3 и режим reconvert-fb2 (синтетические данные)
# ----------------------------------------------------------------------

RICH_FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info>
<genre>sf_fantasy</genre>
<author><first-name>Иван</first-name><last-name>Иванов</last-name></author>
<translator><first-name>Пётр</first-name><last-name>Петров</last-name></translator>
<book-title>Тёмный лес</book-title>
<annotation><p>Первая строка аннотации.</p></annotation>
<coverpage><image l:href="#cover.png"/></coverpage>
<lang>ru</lang>
<sequence name="Лесная сага" number="2"/>
</title-info>
<document-info><id>doc-42</id></document-info>
<publish-info><publisher>Лес</publisher><isbn>978-5-00000-000-0</isbn></publish-info>
</description>
<body>
<title><p>Тёмный лес</p></title>
<section id="ch-a">
<title><p>Глава один</p></title>
<p>Текст со <emphasis>словом</emphasis> и сноской<a l:href="#n1" type="note">1</a>.</p>
<p>Ссылка на вторую главу: <a l:href="#ch-b">дальше</a>.</p>
<image l:href="#pic.png"/>
<poem><stanza><v>Строка первая</v><v>Строка вторая</v></stanza></poem>
</section>
<section id="ch-b">
<title><p>Глава</p><p>два</p></title>
<p>Вторая глава, повтор сноски<a l:href="#n1" type="note">1</a> и ещё<a l:href="#n2" type="note">2</a>.</p>
</section>
</body>
<body name="notes">
<section id="n1"><title><p>1</p></title><p>Текст сноски первой.</p></section>
<section id="n2"><title><p>2</p></title><p>Сноска без ссылки, всё равно текст.</p></section>
<section id="n3"><title><p>3</p></title><p>Никем не упомянутая сноска.</p></section>
</body>
<binary id="cover.png" content-type="image/png">{png}</binary>
<binary id="pic.png" content-type="image/png">{png}</binary>
<binary id="bad.jpg" content-type="image/jpeg">!!не base64!!</binary>
</FictionBook>
"""


def rich_fb2() -> bytes:
    """fb2 с обложкой, серией, двумя главами, сноской, стихом, картинкой и служебным телом сносок."""
    png = base64.b64encode(PNG_BYTES).decode("ascii")
    return RICH_FB2.replace("{png}", png).encode("utf-8")


def read_epub(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_fb2_to_epub3_packaging(tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    bl.fb2_to_epub3(rich_fb2(), out)
    with zipfile.ZipFile(out) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
    files = read_epub(out)
    opf = files["OEBPS/content.opf"].decode("utf-8")
    assert 'version="3.0"' in opf
    assert 'properties="cover-image"' in opf
    assert 'property="belongs-to-collection"' in opf
    assert 'name="calibre:series_index"' in opf
    assert 'refines="#creator1" property="role" scheme="marc:relators">aut' in opf
    assert 'scheme="marc:relators">trl' in opf
    assert b'epub:type="toc"' in files["OEBPS/nav.xhtml"]
    assert b'epub:type="landmarks"' in files["OEBPS/nav.xhtml"]
    assert b"<navMap>" in files["OEBPS/toc.ncx"]
    assert "OEBPS/img/001.png" in files
    assert "OEBPS/text/cover.xhtml" in files


def test_footnote_is_aside_in_chapter_where_first_referenced(tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    bl.fb2_to_epub3(rich_fb2(), out)
    files = read_epub(out)
    first = files["OEBPS/text/ch001.xhtml"].decode("utf-8")
    assert 'epub:type="noteref"' in first
    assert 'aside epub:type="footnote"' in first and 'id="n1"' in first
    assert 'role="doc-backlink"' in first
    second = files["OEBPS/text/ch002.xhtml"].decode("utf-8")
    assert 'href="ch001.xhtml#n1"' in second  # повторная ссылка ведёт в файл сноски
    assert 'id="ref-n1-2"' in second


def test_note_without_reference_goes_to_notes_xhtml(tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    bl.fb2_to_epub3(rich_fb2(), out)
    files = read_epub(out)
    notes = files["OEBPS/text/notes.xhtml"].decode("utf-8")
    assert 'id="n3"' in notes and "Никем не упомянутая сноска." in notes
    chapter = files["OEBPS/text/ch002.xhtml"].decode("utf-8")
    assert 'id="n3"' not in chapter  # сноска без ссылок в главу не попадает; n2 (ссылка в ch002) — законно там


def test_own_epub_passes_text_loss_check(tmp_path: Path) -> None:
    raw = rich_fb2()
    out = tmp_path / "book.epub"
    bl.fb2_to_epub3(raw, out)
    assert bl.verify_book(out, "epub") is None
    assert bl.epub_text_loss(raw, out) is None


def test_fb2_with_entity_declaration_is_rejected() -> None:
    data = b'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE FictionBook [<!ENTITY x "boom">]>' + rich_fb2()
    with pytest.raises(ValueError):
        bl.fb2_to_epub3(data, Path("unused.epub"))


def test_broken_fb2_xml_is_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        bl.fb2_to_epub3(b"<FictionBook><description>", tmp_path / "x.epub")


def test_reconvert_match_takes_single_candidate_and_resolves_by_stem() -> None:
    index = {
        "темный лес": [("иванов", "Иванов - Тёмный лес", Path("/q/a.fb2")),
                       ("иванов", "Иванов - Тёмный лес (другое издание)", Path("/q/b.fb2"))],
        "другая": [("петров", "Петров - Другая", Path("/q/c.fb2"))],
    }
    assert bl.reconvert_match("другая", "петр петров", "Петров - Другая", index) == (Path("/q/c.fb2"), "")
    # два кандидата с одним автором: выбирает тот, чей стем совпадает с epub без суффикса _N
    assert bl.reconvert_match("темный лес", "иван иванов", "Иванов - Тёмный лес_1", index) == (Path("/q/a.fb2"), "")
    found, reason = bl.reconvert_match("темный лес", "иван иванов", "Иванов - Другой стем", index)
    assert found is None and reason.startswith("неоднозначно")
    assert bl.reconvert_match("нет такой", "кто-то", "x", index)[0] is None


def test_command_params_reconvert_needs_out_with_sample() -> None:
    with pytest.raises(bl.ConfigError):
        bl.command_params("reconvert-fb2", {"sample": 3})
    with pytest.raises(bl.ConfigError):
        bl.command_params("reconvert-fb2", {"out": "/tmp/x"})
    params = bl.command_params("reconvert-fb2", {"sample": 2, "out": "/tmp/x"})
    assert params["sample"] == 2 and params["limit"] == 0 and params["epubcheck"] is False


# ----------------------------------------------------------------------
# Вторая попытка разбора, ссылки без хоста, <p> в ячейках, тело с названием, счёт слов без <head>
# ----------------------------------------------------------------------


def _fb2_doc(bodies: str) -> bytes:
    """Минимальная fb2 с заданными телами (разметка передаётся как есть, в том числе испорченная)."""
    text = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">'
        "<description><title-info>"
        "<genre>sf_fantasy</genre>"
        "<author><first-name>Иван</first-name><last-name>Иванов</last-name></author>"
        "<book-title>Книга</book-title>"
        "<lang>ru</lang>"
        "</title-info></description>"
        f"{bodies}"
        "</FictionBook>"
    )
    return text.encode("utf-8")


def _epub_text(tmp_path: Path, bodies: str) -> str:
    """Весь текст глав собранного fb2_to_epub3 EPUB (все файлы OEBPS/text)."""
    out = tmp_path / "book.epub"
    bl.fb2_to_epub3(_fb2_doc(bodies), out)
    files = read_epub(out)
    return "".join(data.decode("utf-8") for name, data in files.items()
                   if name.startswith("OEBPS/text/") and name.endswith(".xhtml"))


def test_valid_url_rejects_hostless_spaced_and_broken_urls() -> None:
    assert bl._valid_url("http:///") is False
    assert bl._valid_url("http://a b") is False
    assert bl._valid_url("http://[::1") is False


def test_valid_url_accepts_https_with_host() -> None:
    assert bl._valid_url("https://ya.ru/x") is True


def test_fb2_link_without_host_becomes_plain_text(tmp_path: Path) -> None:
    text = _epub_text(tmp_path, '<body><section><title><p>Глава 1</p></title>'
                                '<p>Смотри <a l:href="http:///">текст</a> здесь.</p></section></body>')
    assert "текст" in text
    assert 'href="http:///' not in text


def test_table_cell_paragraphs_are_joined_with_br(tmp_path: Path) -> None:
    text = _epub_text(tmp_path, '<body><section><title><p>Глава 1</p></title>'
                                "<table><tr><td><p>enum a;</p><p>class b{</p></td></tr></table>"
                                "</section></body>")
    assert "enum a;<br/>class b{" in text
    assert "enum a;class b{" not in text


def test_repair_fb2_control_char_bare_amp_and_entities(tmp_path: Path) -> None:
    # \x15 и голый «&» делают XML неразборчивым: первая попытка падает, вторая — с починкой
    text = _epub_text(tmp_path, "<body><section><title><p>Глава 1</p></title>"
                                "<p>Кот\x15 сидит. Harper & Brothers&nbsp;издали.</p>"
                                "</section></body>")
    assert "Harper &amp; Brothers" in text
    assert "Кот сидит." in text


def test_fb2_with_entity_and_control_char_is_still_rejected(tmp_path: Path) -> None:
    data = b'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE FictionBook [<!ENTITY x "boom">]>' + _fb2_doc(
        "<body><section><p>Кот\x15</p></section></body>")
    with pytest.raises(ValueError):
        bl.fb2_to_epub3(data, tmp_path / "x.epub")


def test_first_named_body_is_main_and_note_body_is_footnote(tmp_path: Path) -> None:
    text = _epub_text(tmp_path,
                      '<body name="Название книги"><section id="ch1"><title><p>Глава 1</p></title>'
                      '<p>Основной текст книги.<a l:href="#n1" type="note">1</a></p></section></body>'
                      '<body name="notes"><section id="n1"><title><p>1</p></title>'
                      "<p>Текст сноски.</p></section></body>")
    assert "Основной текст книги." in text
    assert 'aside epub:type="footnote"' in text
    assert "Текст сноски." in text
    for chunk in text.split("<aside")[1:]:
        assert "Основной" not in chunk.split("</aside>")[0]


def test_epub_head_is_not_counted_as_words() -> None:
    markup = "<html><head><title>Раз два</title></head><body>три</body></html>"
    stripped = bl.EPUB_HEAD.sub(" ", markup)
    assert "Раз" not in stripped
    assert bl.count_words(bl.markup_plain(stripped)) == 1


class _CapsNameCache:
    """Кэш текста с одной строкой «РЫЖКОВ ИГОРЬ АЛЕКСЕЕВИЧ» в начале книги."""

    def get(self, rel: str, size: int, mtime: float, fmt: str) -> str:
        return "РЫЖКОВ ИГОРЬ АЛЕКСЕЕВИЧ\nНазвание\n"


def test_text_patronymics_caps_name_is_title_cased(tmp_path: Path) -> None:
    folder = tmp_path / "Рыжков Игорь"
    folder.mkdir()
    (folder / "a.txt").write_text("текст книги\n", encoding="utf-8")
    rows = [{"path": "Рыжков Игорь/a.txt"}]
    assert bl.text_patronymics(_CapsNameCache(), tmp_path, rows,
                               bl.author_words("Рыжков Игорь")) == ["Рыжков Игорь Алексеевич"]


# ---- misplaced / fix-misplaced / merge-folder / strip-suffix --------------------------------------

def test_misplaced_counts_categories_and_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/Ok.fb2": make_fb2("Ok", "Иван", "Иванов"),
        "Иванов Иван/Чужая.fb2": make_fb2("Чужая", "Пётр", "Петров"),
        "Петров Пётр/Своя.txt": "Текст.\n",
        "Сидоров Сергей/Николай.fb2": make_fb2("Николай", "Николай", "Николаев"),
    })
    lines: list[str] = []

    stats = bl.misplaced(cfg, out=lines.append)

    assert stats == {"ok": 2, "no-meta": 0, "meta-not-author": 0, "misplaced-existing": 1, "misplaced-no-target": 1}
    assert len(list((tmp_path / "reports").glob("misplaced-*.json"))) == 1
    assert any("Иванов Иван/Чужая.fb2 → Петров Пётр" in line for line in lines)


def test_misplaced_dry_run_writes_no_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/Чужая.fb2": make_fb2("Чужая", "Пётр", "Петров"),
        "Петров Пётр/Своя.txt": "Текст.\n",
    })

    stats = bl.misplaced(cfg, dry_run=True, out=lambda _line: None)

    assert stats["misplaced-existing"] == 1
    assert not list((tmp_path / "reports").glob("misplaced-*.json"))


def test_fix_misplaced_merges_whole_folder_when_every_book_votes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/A.fb2": make_fb2("A", "Иван", "Сидоров"),
        "Иванов Иван/B.fb2": make_fb2("B", "Иван", "Сидоров"),
        "Сидоров Иван/Своя.txt": "Текст.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats["folder-merged"] == 1
    assert stats["merged"] == 1
    assert not (root / "Иванов Иван").exists()
    assert (root / "Сидоров Иван" / "A.fb2").is_file()
    assert (root / "Сидоров Иван" / "B.fb2").is_file()


def test_fix_misplaced_moves_only_misplaced_books_when_folder_is_not_unanimous(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/Ok.fb2": make_fb2("Ok", "Иван", "Иванов"),
        "Иванов Иван/A.fb2": make_fb2("A", "Иван", "Сидоров"),
        "Иванов Иван/B.fb2": make_fb2("B", "Иван", "Сидоров"),
        "Сидоров Иван/Своя.txt": "Текст.\n",
    })
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", ("Иванов Иван/A.fb2", "done", 1.0))

    stats = bl.fix_misplaced(cfg)

    assert stats.get("folder-merged", 0) == 0
    assert stats.get("emptied", 0) == 0
    assert stats["moved"] == 2
    assert (root / "Иванов Иван" / "Ok.fb2").is_file()
    assert (root / "Сидоров Иван" / "A.fb2").is_file()
    assert (root / "Сидоров Иван" / "B.fb2").is_file()
    with closing(bl.connect(cfg["db_path"])) as conn:
        assert [r[0] for r in conn.execute("SELECT path FROM marks")] == ["Сидоров Иван/A.fb2"]


def test_fix_misplaced_other_person_is_not_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/A.fb2": make_fb2("A", "Пётр", "Петров"),
        "Иванов Иван/B.fb2": make_fb2("B", "Пётр", "Петров"),
        "Петров Пётр/Своя.txt": "Текст.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats.get("folder-merged", 0) == 0
    assert stats["skip-other-person"] == 2
    assert (root / "Иванов Иван" / "A.fb2").is_file()
    assert (root / "Иванов Иван" / "B.fb2").is_file()


def test_fix_misplaced_bracket_folder_is_not_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван (Ps)/A.fb2": make_fb2("A", "Пётр", "Петров"),
        "Иванов Иван (Ps)/B.fb2": make_fb2("B", "Пётр", "Петров"),
        "Петров Пётр/Своя.txt": "Текст.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats.get("folder-merged", 0) == 0
    assert stats["skip-brackets"] == 2
    assert (root / "Иванов Иван (Ps)" / "A.fb2").is_file()


def test_fix_misplaced_fewer_words_and_initials_targets_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван Иванович/A.fb2": make_fb2("A", "Пётр", "Петров"),
        "Петров Пётр/Своя.txt": "Текст.\n",
        "Иванов Иван/B.fb2": make_fb2("B", "И.", "Петров"),
        "Петров И/Своя.txt": "Текст.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats["skip-worse"] == 1
    assert stats["skip-initials"] == 1
    assert stats.get("moved", 0) == 0
    assert (root / "Иванов Иван Иванович" / "A.fb2").is_file()
    assert (root / "Иванов Иван" / "B.fb2").is_file()


def make_fb2_author(title: str, name: str) -> bytes:
    """fb2 с автором «Слово Слово Слово» в порядке базы (как books.authors): тот же порядок, что у _fb2_author."""
    raw = make_fb2(title, "", "")
    return raw.replace(b"<author><first-name></first-name><last-name></last-name></author>",
                       bl._fb2_author(name).encode("utf-8"))


def test_fix_misplaced_renames_folder_when_name_only_grows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Петров Иван/A.fb2": make_fb2_author("A", "Иван Сергеевич Петров"),
        "Петров Иван/B.fb2": make_fb2_author("B", "Иван Сергеевич Петров"),
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats["folder-renamed"] == 1
    assert not (root / "Петров Иван").exists()
    assert (root / "Петров Иван Сергеевич" / "A.fb2").is_file()
    assert (root / "Петров Иван Сергеевич" / "B.fb2").is_file()


@pytest.mark.parametrize("folder, author, reason", [
    ("Мамонова Марина", "Мария Мамонова", "skip-rename-changes"),  # «Марина» → «Мария»: меняется существующее слово
    ("Тейлор Джули", "Джуди Тейлор", "skip-rename-changes"),  # «Джули» → «Джуди»
    ("Хаббард Рон", "Рон Л Хаббард", "skip-rename-initials"),  # добавлен инициал «Л»
    ("де Сад Донасьен", "Донасьен Альфонс Франсуа Де Сад", "skip-rename-particle"),  # имя оканчивается частицей «Де»
])
def test_fix_misplaced_rename_refused_by_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               folder: str, author: str, reason: str) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        f"{folder}/A.fb2": make_fb2_author("A", author),
        f"{folder}/B.fb2": make_fb2_author("B", author),
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats.get(reason, 0) == 1
    assert stats.get("folder-renamed", 0) == 0
    assert (root / folder / "A.fb2").is_file()


@pytest.mark.parametrize("folder, author, target", [
    ("Игнан Грег", "Грег Иган", "Иган Грег"),  # опечатка в фамилии: «Игнан» → «Иган»
    ("Дубининская Яна", "Яна Дубинянская", "Дубинянская Яна"),  # опечатка в фамилии
    ("Трубалаини Николай", "Николай Трублаини", "Трублаини Николай"),  # пропуск буквы в фамилии
    ("Кизи Кенн", "Кен Кизи", "Кизи Кен"),  # удвоенная буква в имени
])
def test_fix_misplaced_renames_typo_in_one_word(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                folder: str, author: str, target: str) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        f"{folder}/A.fb2": make_fb2_author("A", author),
        f"{folder}/B.fb2": make_fb2_author("B", author),
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg)

    assert stats["folder-renamed"] == 1
    assert not (root / folder).exists()
    assert (root / target / "A.fb2").is_file()
    assert (root / target / "B.fb2").is_file()


def test_fix_misplaced_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/Ok.fb2": make_fb2("Ok", "Иван", "Иванов"),
        "Иванов Иван/A.fb2": make_fb2("A", "Иван", "Сидоров"),
        "Сидоров Иван/Своя.txt": "Текст.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.fix_misplaced(cfg, dry_run=True, out=lambda _line: None)

    assert stats["moved"] == 1
    assert (root / "Иванов Иван" / "A.fb2").is_file()
    assert not (root / "Сидоров Иван" / "A.fb2").exists()


def test_merge_folder_renames_when_target_missing_and_relinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/a.txt": "А.\n"})
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", ("Иван Петров/a.txt", "done", 1.0))

    stats = bl.folder_merge(cfg, ["Иван Петров"], "Петров Иван")

    assert stats["renamed"] == 1
    assert not (root / "Иван Петров").exists()
    assert (root / "Петров Иван" / "a.txt").is_file()
    with closing(bl.connect(cfg["db_path"])) as conn:
        assert [r[0] for r in conn.execute("SELECT path FROM marks")] == ["Петров Иван/a.txt"]


def test_merge_folder_merges_into_existing_and_counts_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/a.txt": "А.\n",
        "Иванов Иван/sub/b.txt": "Б.\n",
        "Иванов Иван Петрович/a.txt": "Другой.\n",
    })
    root = Path(cfg["books_dir"])
    full = root / "Иванов Иван Петрович"

    stats = bl.folder_merge(cfg, ["Иванов Иван"], "Иванов Иван Петрович")

    assert stats["merged"] == 1
    assert stats["conflicts"] == 1
    assert not (root / "Иванов Иван").exists()
    assert (full / "a.txt").read_text(encoding="utf-8") == "Другой.\n"
    assert (full / "a_1.txt").read_text(encoding="utf-8") == "А.\n"
    assert (full / "sub" / "b.txt").is_file()


def test_merge_folder_rejects_bad_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/a.txt": "А.\n", "_Разбор/b.txt": "Б.\n"})
    root = Path(cfg["books_dir"])

    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, ["Нет Такой"], "Петров Иван")
    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, ["_Разбор"], "Петров Иван")
    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, ["Иван Петров"], "Иван Петров")
    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, ["Иван Петров"], "Петров/Иван")
    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, [], "Петров Иван")
    assert (root / "Иван Петров" / "a.txt").is_file()


def test_merge_folder_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/a.txt": "А.\n"})
    root = Path(cfg["books_dir"])

    stats = bl.folder_merge(cfg, ["Иван Петров"], "Петров Иван", dry_run=True, out=lambda _line: None)

    assert stats["renamed"] == 1
    assert (root / "Иван Петров" / "a.txt").is_file()
    assert not (root / "Петров Иван").exists()


def test_merge_folder_into_existing_service_folder_always_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Автор неизвестен/a.txt": "А.\n",
        "Автор неизвестен/sub/b.txt": "Б.\n",
        "_1tmp/c.txt": "В.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.folder_merge(cfg, ["Автор неизвестен"], "_1tmp")

    assert stats["merged"] == 1
    assert stats.get("renamed", 0) == 0
    assert not (root / "Автор неизвестен").exists()
    assert (root / "_1tmp" / "a.txt").read_text(encoding="utf-8") == "А.\n"
    assert (root / "_1tmp" / "sub" / "b.txt").is_file()
    assert (root / "_1tmp" / "c.txt").is_file()


def test_merge_folder_service_target_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/a.txt": "А.\n"})
    root = Path(cfg["books_dir"])

    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, ["Иван Петров"], "_нет")
    assert not (root / "_нет").exists()
    assert (root / "Иван Петров" / "a.txt").is_file()


def test_check_folders_categorizes_top_level_folders_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/a.txt": "А.\n",
        "Петров Пётр Петрович/a.txt": "А.\n",
        "Сидоров С/a.txt": "А.\n",
        "Агриппа/a.txt": "А.\n",
        "Ильф Илья и Петров Евгений/a.txt": "А.\n",
        "братья/a.txt": "А.\n",
        "_служебная/a.txt": "А.\n",
    })
    root = Path(cfg["books_dir"])
    lines: list[str] = []

    stats = bl.check_folders(cfg, out=lines.append)

    assert stats == {"ok": 2, "not-person": 1, "coauthors": 1, "single": 1, "initials": 1}
    assert any(line.startswith("  initials: 1") for line in lines)
    assert any("Сидоров С — книг: 1" in line for line in lines)
    assert len(list((tmp_path / "reports").glob("check-folders-*.json"))) == 1
    assert (root / "братья" / "a.txt").is_file()  # режим ничего не переносит
    assert (root / "_служебная" / "a.txt").is_file()


NON_BOOK_FILES = {
    "_Сборники/X/a.epub": b"not really epub",  # битый epub: scan оставляет книгу по пути, расширение решает
    "_Сборники/X/index.html": "<html></html>",
    "_Сборники/X/pic.gif": b"GIF89a",
}


def test_merge_folder_non_books_go_to_quarantine_not_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, NON_BOOK_FILES)
    root = Path(cfg["books_dir"])

    stats = bl.folder_merge(cfg, ["_Сборники/X"], "_разобрать/X", non_books_to_quarantine=True)

    assert stats["moved"] == 1
    assert stats["quarantined_not_book"] == 2
    assert stats["not-book"] == 2
    assert (root / "_разобрать" / "X" / "a.epub").is_file()
    assert not (root / "_разобрать" / "X" / "index.html").exists()
    assert not (root / "_разобрать" / "X" / "pic.gif").exists()
    quarantine = Path(cfg["quarantine_dir"]) / "not-book" / "_Сборники" / "X"
    assert (quarantine / "index.html").is_file()
    assert (quarantine / "pic.gif").is_file()


def test_merge_folder_non_books_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, NON_BOOK_FILES)
    root = Path(cfg["books_dir"])

    stats = bl.folder_merge(cfg, ["_Сборники/X"], "_разобрать/X", dry_run=True, out=lambda _line: None,
                            non_books_to_quarantine=True)

    assert stats["quarantined_not_book"] == 2
    assert (root / "_Сборники" / "X" / "a.epub").is_file()
    assert (root / "_Сборники" / "X" / "index.html").is_file()
    assert (root / "_Сборники" / "X" / "pic.gif").is_file()
    assert not (root / "_разобрать").exists()
    assert not (Path(cfg["quarantine_dir"]) / "not-book").exists()


def test_merge_folder_non_books_flag_requires_razobrat_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/a.txt": "А.\n"})

    with pytest.raises(bl.ConfigError):
        bl.folder_merge(cfg, ["Иван Петров"], "Петров Иван", non_books_to_quarantine=True)
    assert (Path(cfg["books_dir"]) / "Иван Петров" / "a.txt").is_file()


def test_strip_suffix_renames_free_names_and_skips_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/A_1.txt": "А.\n",
        "Иванов Иван/B_1.txt": "Б1.\n",
        "Иванов Иван/B.txt": "Б.\n",
        "Иванов Иван/C_1.txt": "В1.\n",
        "Иванов Иван/C_2.txt": "В2.\n",
        "_Разбор/D_1.txt": "Г.\n",
    })
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", ("Иванов Иван/A_1.txt", "done", 1.0))

    stats = bl.strip_suffix(cfg)

    assert stats["renamed"] == 1
    assert stats["skip-original-exists"] == 1
    assert stats["skip-siblings"] == 2
    assert (root / "Иванов Иван" / "A.txt").read_text(encoding="utf-8") == "А.\n"
    assert not (root / "Иванов Иван" / "A_1.txt").exists()
    assert (root / "Иванов Иван" / "B_1.txt").is_file()
    assert (root / "Иванов Иван" / "C_1.txt").is_file()
    assert (root / "_Разбор" / "D_1.txt").is_file()
    with closing(bl.connect(cfg["db_path"])) as conn:
        assert [r[0] for r in conn.execute("SELECT path FROM marks")] == ["Иванов Иван/A.txt"]

    explicit = bl.strip_suffix(cfg, path="_Разбор")

    assert explicit["renamed"] == 1
    assert (root / "_Разбор" / "D.txt").is_file()


def test_strip_suffix_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иванов Иван/A_1.txt": "А.\n"})
    root = Path(cfg["books_dir"])

    stats = bl.strip_suffix(cfg, dry_run=True, out=lambda _line: None)

    assert stats["renamed"] == 1
    assert (root / "Иванов Иван" / "A_1.txt").is_file()
    assert not (root / "Иванов Иван" / "A.txt").exists()


def test_name_variants_qualifies_by_lang_and_falls_back_to_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Иванов Иван/А - X.txt": "А.\n",
        "Иванов Иван/А - X_1.txt": "Б.\n",
        "Иванов Иван/В - Y_1.txt": "В1.\n",
        "Иванов Иван/В - Y_2.txt": "В2.\n",
    })
    root = Path(cfg["books_dir"])
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("UPDATE books SET lang='bg' WHERE path=?", ("Иванов Иван/А - X_1.txt",))

    dry = bl.name_variants(cfg, dry_run=True, out=lambda _line: None)

    assert dry["renamed"] == 3
    assert (root / "Иванов Иван" / "А - X_1.txt").is_file()
    assert not (root / "Иванов Иван" / "А - X (bg).txt").exists()

    stats = bl.name_variants(cfg)

    assert stats["renamed"] == 3
    assert stats["qualifier-lang"] == 1
    assert stats["qualifier-variant"] == 2
    assert (root / "Иванов Иван" / "А - X (bg).txt").read_text(encoding="utf-8") == "Б.\n"
    assert (root / "Иванов Иван" / "А - X.txt").is_file()
    assert (root / "Иванов Иван" / "В - Y (вариант 1).txt").read_text(encoding="utf-8") == "В1.\n"
    assert (root / "Иванов Иван" / "В - Y (вариант 2).txt").read_text(encoding="utf-8") == "В2.\n"
    assert not (root / "Иванов Иван" / "А - X_1.txt").exists()


def test_short_source_and_person_for_name_variants() -> None:
    assert bl.short_source("Библиотека Trauma 2.0 (69660 файлов,fb2)") == "Trauma 2.0"
    assert bl.short_source("Lib.rus.ec - Официальная") == "Lib.rus.ec"
    assert bl.short_source("Библиотека Мошкова") == "Мошкова"
    assert bl.short_person("Н. И. Виленская") == "Виленская"


def test_windows_name_error_rules() -> None:
    assert bl.windows_name_error("a.txt") == ""
    assert bl.windows_name_error("CONSOLE.txt") == ""
    for bad in ("a.", "a ", "a:b", "CON", "con.txt", "LPT1.epub", "COM¹.txt", "x" * 256, ""):
        assert bl.windows_name_error(bad), bad
    assert bl.windows_name_fix("CON.txt", is_dir=False) == "CON_.txt"
    assert bl.windows_name_fix("Автор И.", is_dir=True) == "Автор И"
    assert bl.folder_name("CON") == "CON_"


def test_windows_names_renames_folders_and_case_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Автор И./Книга2.epub": "А.\n",
        "Иванов Иван/Книга.epub": "Б.\n",
        "Иванов Иван/книга.epub": "В.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.windows_names(cfg, out=lambda _line: None)

    assert stats["renamed"] == 1
    assert stats["case_conflicts"] == 1
    assert stats.get("errors", 0) == 0
    assert (root / "Автор И" / "Книга2.epub").is_file()
    assert not (root / "Автор И.").exists()
    assert sorted(os.listdir(root / "Иванов Иван")) == ["Книга.epub", "книга.epub"]


def test_windows_names_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "Автор И./Книга2.epub": "А.\n",
        "Иванов Иван/Книга.epub": "Б.\n",
        "Иванов Иван/книга.epub": "В.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.windows_names(cfg, dry_run=True, out=lambda _line: None)

    assert stats["renamed"] == 1
    assert stats["case_conflicts"] == 1
    assert (root / "Автор И." / "Книга2.epub").is_file()
    assert sorted(os.listdir(root / "Иванов Иван")) == ["Книга.epub", "книга.epub"]


def test_command_params_merge_folder_and_strip_suffix() -> None:
    assert bl.command_params("merge-folder", {"from": ["A"], "to": "B"}) == {
        "sources": ["A"], "target": "B", "non_books_to_quarantine": False}
    assert bl.command_params("merge-folder", {"from": ["A"], "to": "_разобрать/X", "non_books_to_quarantine": True})[
        "non_books_to_quarantine"] is True
    with pytest.raises(bl.ConfigError):
        bl.command_params("merge-folder", {"from": ["A"], "to": "B", "non_books_to_quarantine": "yes"})
    assert bl.command_params("strip-suffix", {"path": "Иванов Иван"}) == {"path": "Иванов Иван"}
    assert bl.command_params("name-variants", {"path": "Иванов Иван"}) == {"path": "Иванов Иван"}
    with pytest.raises(bl.ConfigError):
        bl.command_params("merge-folder", {"from": [], "to": "B"})
    with pytest.raises(bl.ConfigError):
        bl.command_params("fix-misplaced", {"path": "x"})


def test_merge_folder_from_nested_service_path_into_new_folder_keeps_collection(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "_Сборники/Серия/Иван Петров/a.txt": "А.\n",
        "_Сборники/Серия/Иван Петров/sub/b.txt": "Б.\n",
    })
    root = Path(cfg["books_dir"])
    old = "_Сборники/Серия/Иван Петров/a.txt"
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", (old, "done", 1.0))

    stats = bl.folder_merge(cfg, ["_Сборники/Серия/Иван Петров"], "Петров Иван")

    assert stats["merged"] == 1
    assert stats["moved"] == 2
    assert (root / "Петров Иван" / "a.txt").is_file()
    assert (root / "Петров Иван" / "sub" / "b.txt").is_file()
    assert not (root / "_Сборники" / "Серия" / "Иван Петров").exists()
    assert (root / "_Сборники" / "Серия").is_dir()
    with closing(bl.connect(cfg["db_path"])) as conn:
        marks = [r[0] for r in conn.execute("SELECT path FROM marks")]
        rows = conn.execute("SELECT collection, orig_path FROM book_collections WHERE path=? AND source='folder'",
                            ("Петров Иван/a.txt",)).fetchall()
    assert marks == ["Петров Иван/a.txt"]
    assert [tuple(r) for r in rows] == [("Серия", old)]


def test_merge_folder_nested_into_existing_folder_counts_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "_Сборники/Серия/Иван Петров/a.txt": "А.\n",
        "Петров Иван/a.txt": "Другой.\n",
    })
    root = Path(cfg["books_dir"])

    stats = bl.folder_merge(cfg, ["_Сборники/Серия/Иван Петров"], "Петров Иван")

    assert stats["merged"] == 1
    assert stats["conflicts"] == 1
    assert (root / "Петров Иван" / "a.txt").read_text(encoding="utf-8") == "Другой.\n"
    assert (root / "Петров Иван" / "a_1.txt").read_text(encoding="utf-8") == "А.\n"


def test_merge_folder_rejects_nested_path_outside_service_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/sub/a.txt": "А.\n", "_Сборники/Серия/x.txt": "Х.\n"})
    root = Path(cfg["books_dir"])

    for bad in ("Иван Петров/sub", "_Сборники/../Иван Петров", "../outside", "_Сборники", "_Сборники/Серия/Нет"):
        with pytest.raises(bl.ConfigError):
            bl.folder_merge(cfg, [bad], "Петров Иван")
    assert (root / "Иван Петров" / "sub" / "a.txt").is_file()
    assert (root / "_Сборники" / "Серия" / "x.txt").is_file()


def test_merge_folder_into_razobrat_subfolder_keeps_subfolders_and_collection(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {
        "_Сборники/X/_эпос/Книга.fb2": make_fb2("Книга", "Иван", "Иванов"),
        "_Сборники/X/_эпос/Том 2/Книга2.txt": "Текст.\n",
        "_Сборники/X/Остаётся.txt": "Х.\n",
    })
    root = Path(cfg["books_dir"])
    old, new = "_Сборники/X/_эпос/Книга.fb2", "_разобрать/X/Книга.fb2"
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        conn.execute("INSERT INTO marks(path, status, updated) VALUES (?,?,?)", (old, "done", 1.0))

    stats = bl.folder_merge(cfg, ["_Сборники/X/_эпос"], "_разобрать/X")

    assert stats["merged"] == 1
    assert stats["moved"] == 2
    assert (root / new).is_file()
    assert (root / "_разобрать" / "X" / "Том 2" / "Книга2.txt").is_file()
    assert not (root / "_Сборники" / "X" / "_эпос").exists()
    assert (root / "_Сборники" / "X" / "Остаётся.txt").is_file()
    with closing(bl.connect(cfg["db_path"])) as conn:
        marks = [r[0] for r in conn.execute("SELECT path FROM marks")]
        rows = conn.execute("SELECT collection, orig_path FROM book_collections WHERE path=? AND source='folder'",
                            (new,)).fetchall()
    assert marks == [new]
    assert [tuple(r) for r in rows] == [("X", old)]


def test_merge_folder_target_must_be_author_folder_or_inside_razobrat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = merge_cfg(tmp_path, monkeypatch, {"Иван Петров/a.txt": "А.\n", "_Сборники/X/Книга.txt": "Х.\n"})
    root = Path(cfg["books_dir"])

    for bad in ("_Сборники/X", "_разобрать", "_разобрать/../x", "Петров/Иван"):
        with pytest.raises(bl.ConfigError):
            bl.folder_merge(cfg, ["Иван Петров"], bad)
    assert (root / "Иван Петров" / "a.txt").is_file()


# ---- (в) схема v6: book_meta, подразделы сборников, сборники из метаданных --------------------

def make_fb2_full() -> bytes:
    """fb2 с переводчиком, второй серией, издательскими данными, custom-info и серией издательства."""
    text = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
        "<description><title-info><genre>sf</genre>"
        "<author><first-name>Иван</first-name><last-name>Иванов</last-name><nickname>ivan</nickname></author>"
        "<translator><first-name>Пётр</first-name><last-name>Петров</last-name></translator>"
        "<book-title>Тёмный лес</book-title><keywords>лес, тьма</keywords>"
        '<date value="1999">1999</date><lang>ru</lang><src-lang>en</src-lang>'
        '<sequence name="Серия А" number="2"/><sequence name="Серия Б" number="5"/>'
        "</title-info>"
        "<publish-info><book-name>Тёмный лес (издание)</book-name><publisher>Издатель</publisher>"
        '<year>2005</year><isbn>9785000000001</isbn><sequence name="Издательская" number="7"/></publish-info>'
        '<custom-info info-type="ISBN-ALT">X1</custom-info>'
        "</description><body><section><p>Текст.</p></section></body></FictionBook>"
    )
    return text.encode("utf-8")


def make_epub_opf(path: Path, metadata: str) -> None:
    """EPUB 3 с произвольным содержимым metadata (элементы dc:* и meta внутри <metadata>)."""
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
        f"<dc:title>Электронная книга</dc:title><dc:creator>Сидоров Сергей</dc:creator>{metadata}"
        "</metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine>'
        "</package>"
    )
    chapter = '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>Глава</p></body></html>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", chapter)


def meta_pairs(cfg: dict[str, Any], rel: str) -> set[tuple[str, str]]:
    with closing(bl.connect(cfg["db_path"])) as conn:
        return {(row[0], row[1]) for row in conn.execute("SELECT key, value FROM book_meta WHERE path=?", (rel,))}


def meta_collections(cfg: dict[str, Any], rel: str) -> set[tuple[str, str]]:
    with closing(bl.connect(cfg["db_path"])) as conn:
        return {(row[0], row[1]) for row in conn.execute(
            "SELECT kind, collection FROM book_collections WHERE path=? AND source='meta'", (rel,))}


def test_fb2_details_go_to_book_meta_and_meta_collections(tmp_path: Path) -> None:
    root = tmp_path / "books"
    (root / "Иванов Иван").mkdir(parents=True)
    rel = "Иванов Иван/Издание.fb2"
    (root / rel).write_bytes(make_fb2_full())
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    pairs = meta_pairs(cfg, rel)
    assert ("author_full", "Иванов Иван; ник: ivan") in pairs
    assert ("translator", "Петров Пётр") in pairs
    assert ("publisher", "Издатель") in pairs
    assert ("publisher_series_no", "7") in pairs
    assert ("series_extra", "Серия Б #5") in pairs
    assert ("custom:ISBN-ALT", "X1") in pairs
    assert meta_collections(cfg, rel) == {("edition", "Тёмный лес (издание)"), ("publisher-series", "Издательская")}


def test_epub_belongs_to_collection_set_becomes_meta_collection(tmp_path: Path) -> None:
    root = tmp_path / "books"
    (root / "Сидоров Сергей").mkdir(parents=True)
    rel = "Сидоров Сергей/Серийная.epub"
    make_epub_opf(root / rel, (
        '<meta property="belongs-to-collection" id="c01">Серия Эпуб</meta>'
        '<meta refines="#c01" property="collection-type">set</meta>'
        '<meta refines="#c01" property="group-position">3</meta>'
        "<dc:publisher>Издательство</dc:publisher>"
        '<dc:identifier opf:scheme="ISBN">9785000000002</dc:identifier>'
        '<dc:contributor opf:role="trl">Смирнов А.</dc:contributor>'
    ))
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    assert meta_collections(cfg, rel) == {("set", "Серия Эпуб")}
    pairs = meta_pairs(cfg, rel)
    assert ("translator", "Смирнов А.") in pairs
    assert ("isbn", "9785000000002") in pairs
    assert ("group_position", "3") in pairs
    assert ("epub_version", "3.0") in pairs


def test_fb2_to_epub3_keeps_publish_info_as_set_and_publisher_series(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    bl.fb2_to_epub3(make_fb2_full(), epub)
    with zipfile.ZipFile(epub) as archive:
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
    assert '<meta property="belongs-to-collection" id="edition1">Тёмный лес (издание)</meta>' in opf
    assert '<meta refines="#edition1" property="collection-type">set</meta>' in opf
    assert '<meta name="books_library:publisher-series" content="Издательская"/>' in opf
    assert '<meta name="books_library:publisher-series-index" content="7"/>' in opf


def test_converted_epub_publish_info_becomes_meta_collections(tmp_path: Path) -> None:
    root = tmp_path / "books"
    (root / "Иванов Иван").mkdir(parents=True)
    rel = "Иванов Иван/Издание.epub"
    bl.fb2_to_epub3(make_fb2_full(), root / rel)
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    assert meta_collections(cfg, rel) == {("set", "Тёмный лес (издание)"), ("publisher-series", "Издательская")}
    pairs = meta_pairs(cfg, rel)
    assert ("publisher_series", "Издательская") in pairs
    assert ("publisher_series_no", "7") in pairs


def test_subsection_from_orig_path_and_v6_backfill(tmp_path: Path) -> None:
    rel = "_Сборники/Библиотека Мошкова/Мошков часть 4/LIB/trnslsf/x.txt"
    db = str(tmp_path / "v5.db")
    section, collection = bl.path_collection(rel)
    with closing(sqlite3.connect(db)) as conn:
        for number, sql in enumerate(bl.MIGRATIONS[:5], start=1):
            conn.executescript(f"BEGIN;{sql};PRAGMA user_version={number};COMMIT;")
        conn.execute("INSERT INTO books(path, size, mtime, fmt, title, added, section, collection) VALUES (?,?,?,?,?,?,?,?)",
                     (rel, 1, 1.0, "txt", "x", 1.0, section, collection))
        conn.execute("INSERT INTO book_collections(path, section, collection, source, orig_path) VALUES (?,?,?,?,?)",
                     (rel, section, collection, "folder", rel))
        conn.commit()

    assert bl.migrate_database(db, out=lambda _line: None) == bl.DB_LATEST
    with closing(bl.connect(db)) as conn, conn:
        bl.rebuild_collections(conn)
        row = conn.execute("SELECT subsection, kind FROM book_collections WHERE path=?", (rel,)).fetchone()
    assert tuple(row) == ("Мошков часть 4/LIB/trnslsf", "folder")


def test_relink_moves_book_meta(tmp_path: Path) -> None:
    db = str(tmp_path / "meta.db")
    bl.migrate_database(db, out=lambda _line: None)
    with closing(bl.connect(db)) as conn, conn:
        conn.execute("INSERT INTO book_meta VALUES (?,?,?)", ("Автор/a.epub", "publisher", "Издатель"))

    bl.relink(db, [("Автор/a.epub", "Автор/a2.epub")])

    with closing(bl.connect(db)) as conn:
        rows = [tuple(row) for row in conn.execute("SELECT path, key, value FROM book_meta")]
    assert rows == [("Автор/a2.epub", "publisher", "Издатель")]


def test_pdf_meta_uses_pdfinfo_output_and_fills_only_real_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "Автор - Книга.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    output = ("Title:          Настоящее название\nAuthor:         Автор Иванов\nSubject:        Тема книги\n"
              "Pages:          12\nPage size:      595 x 842 pts (A4)\n")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=output.encode("utf-8"), stderr=b"")

    monkeypatch.setattr(bl.subprocess, "run", fake_run)
    meta = bl.pdf_meta(pdf, "Автор - Книга")

    assert calls[0][:3] == ["pdfinfo", "-enc", "UTF-8"]
    assert meta["title"] == "Настоящее название"
    assert meta["fill"] == {"authors": ["Автор Иванов"], "annotation": "Тема книги"}
    assert ("pages", "12") in meta["kv"]
    assert ("pdf:page_size", "595 x 842 pts (A4)") in meta["kv"]


def test_real_title_rejects_file_name_and_word_template() -> None:
    assert bl.real_title("Microsoft Word - Глава 1", "Книга") == ""
    assert bl.real_title("Книга.pdf", "Книга") == ""
    assert bl.real_title("Тёмный лес", "Иванов - Тёмный лес") == "Тёмный лес"


def test_djvu_meta_parses_print_meta_and_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = '(metadata\n    (title "Скан \\"Т\\"")\n    (year "1990")\n)\n12\n'

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert args[:3] == ["djvused", "-e", "print-meta; n"]
        return subprocess.CompletedProcess(args, 0, stdout=output.encode("utf-8"), stderr=b"")

    monkeypatch.setattr(bl.subprocess, "run", fake_run)
    meta = bl.djvu_meta(tmp_path / "scan.djvu")

    assert ("djvu:title", 'Скан "Т"') in meta["kv"]
    assert ("djvu:year", "1990") in meta["kv"]
    assert ("pages", "12") in meta["kv"]


def make_mobi(title: bytes, author: bytes, updated: bytes | None = None) -> bytes:
    """Минимальный PalmDB с одной записью MOBI и EXTH (100 — автор, 503 — обновлённое название); без текста книги."""
    records = [(100, author)] + ([(503, updated)] if updated else [])
    body = b"".join(struct.pack(">II", kind, len(data) + 8) + data for kind, data in records)
    exth = b"EXTH" + struct.pack(">II", len(body) + 12, len(records)) + body
    mobi = bytearray(232)
    mobi[0:4] = b"MOBI"
    struct.pack_into(">I", mobi, 4, 232)
    struct.pack_into(">I", mobi, 12, 65001)
    struct.pack_into(">I", mobi, 128, 0x40)
    struct.pack_into(">II", mobi, 84, 16 + 232 + len(exth), len(title))
    record0 = bytes(16) + bytes(mobi) + exth + title
    palm = bytearray(78)
    palm[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", palm, 76, 1)
    return bytes(palm) + struct.pack(">II", 86, 0) + record0


def test_mobi_exth_and_full_title(tmp_path: Path) -> None:
    plain = tmp_path / "plain.mobi"
    plain.write_bytes(make_mobi("Полное название".encode(), "Автор Авторов".encode()))
    meta = bl.other_meta(plain, "mobi", "Файл")
    assert meta["title"] == "Полное название"
    assert ("author", "Автор Авторов") in meta["kv"]
    assert meta["fill"] == {"authors": ["Автор Авторов"]}

    updated = tmp_path / "updated.mobi"
    updated.write_bytes(make_mobi("Старое".encode(), "Автор".encode(), "Обновлённое".encode()))
    assert bl.other_meta(updated, "mobi", "Файл")["title"] == "Обновлённое"


def test_scan_reparse_rereads_every_book(cfg: dict[str, Any]) -> None:
    first = bl.scan(cfg)
    again = bl.scan(cfg, reparse=True)
    assert first["files"] == again["files"]
    assert again["changed"] == again["files"]
    assert again["new"] == 0


def test_scan_reparse_is_a_checked_parameter() -> None:
    assert bl.command_params("scan", {"reparse": True}) == {"reparse": True}
    assert bl.command_params("scan", {}) == {"reparse": False}
    with pytest.raises(bl.ConfigError):
        bl.command_params("scan", {"reparse": "yes"})


# ---- (ж) веб v6: сведения, подразделы сборников, сборники из метаданных ------------------------

CARD_REL = "_Сборники/Библиотека Мошкова/Мошков часть 4/LIB/Тёмный лес.fb2"
CARD_OTHER_REL = "_Сборники/Библиотека Мошкова/Другой/Вторая.fb2"


@pytest.fixture
def card_cfg(tmp_path: Path) -> dict[str, Any]:
    """Книга в подразделе сборника с метаданными fb2 и книга в другом подразделе того же сборника."""
    root = tmp_path / "card_books"
    (root / CARD_REL).parent.mkdir(parents=True)
    (root / CARD_REL).write_bytes(make_fb2_full())
    (root / CARD_OTHER_REL).parent.mkdir(parents=True)
    (root / CARD_OTHER_REL).write_bytes(make_fb2("Вторая", "Пётр", "Сидоров"))
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        bl.rebuild_collections(conn)
    return cfg


@pytest.fixture
def card_web(card_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    yield from _serving(card_cfg, monkeypatch)


def test_card_shows_details_subsection_and_meta_collections(card_web: str, card_cfg: dict[str, Any]) -> None:
    status, _headers, body = fetch(f"{card_web}/book/{book_id(card_cfg, CARD_REL)}")
    text = body.decode("utf-8")

    assert status == 200
    assert "<h3>Сведения</h3>" in text
    assert "<th>Переводчик</th><td>Петров Пётр</td>" in text
    assert "<th>ISBN</th><td>9785000000001</td>" in text
    assert "Библиотека Мошкова / Мошков часть 4 / LIB" in text
    assert "Из метаданных" in text
    assert "издательская серия: Издательская" in text
    assert "издание: Тёмный лес (издание)" in text


def test_api_book_details_and_collections(card_web: str, card_cfg: dict[str, Any]) -> None:
    status, body = api_call(f"{card_web}/api/books/{book_id(card_cfg, CARD_REL)}")

    assert status == 200
    details = {item["key"]: item for item in body["details"]}
    assert details["translator"] == {"key": "translator", "label": "Переводчик", "values": ["Петров Пётр"]}
    assert details["isbn"]["label"] == "ISBN"
    folder = [c for c in body["collections"] if c["source"] == "folder"]
    assert folder == [{"section": bl.section_name("_Сборники"), "collection": "Библиотека Мошкова",
                       "subsection": "Мошков часть 4/LIB", "kind": "folder", "source": "folder"}]
    assert {c["kind"] for c in body["collections"] if c["source"] == "meta"} == {"edition", "publisher-series"}


def test_collection_filter_by_subsection(card_web: str, card_cfg: dict[str, Any]) -> None:
    base = f"{card_web}/api/books?collection={quote('Библиотека Мошкова')}&section={quote(bl.section_name('_Сборники'))}"

    _status, body = api_call(base)
    assert body["total"] == 2

    _status, body = api_call(f"{base}&subsection={quote('Мошков часть 4/LIB', safe='')}")
    assert body["total"] == 1
    assert "Мошков часть 4" in body["items"][0]["path"]

    _status, body = api_call(f"{base}&subsection={quote('Другой', safe='')}")
    assert body["total"] == 1
    assert body["items"][0]["path"].endswith("Вторая.fb2")

    _status, body = api_call(f"{base}&subsection={quote('нет', safe='')}")
    assert body["total"] == 0


def test_meta_collections_stay_out_of_section_lists(card_cfg: dict[str, Any]) -> None:
    with closing(bl.connect(card_cfg["db_path"])) as conn:
        listed = {row["collection"] for row in bl.section_counts(conn, "1, 2")}
        meta = {(row["kind"], row["collection"]) for row in bl.meta_collection_counts(conn)}

    assert "Библиотека Мошкова" in listed
    assert not listed & {"Издательская", "Тёмный лес (издание)"}
    assert meta == {("edition", "Тёмный лес (издание)"), ("publisher-series", "Издательская")}


def test_book_details_is_empty_before_v6(tmp_path: Path) -> None:
    db = str(tmp_path / "v5.db")
    with closing(sqlite3.connect(db)) as conn:
        for number, sql in enumerate(bl.MIGRATIONS[:5], start=1):
            conn.executescript(f"BEGIN;{sql};PRAGMA user_version={number};COMMIT;")
    with closing(bl.connect(db)) as conn:
        assert bl.book_details(conn, "x.fb2") == []


def test_meta_label_prefixes_and_details_escaping() -> None:
    assert bl.meta_label("translator") == "Переводчик"
    assert bl.meta_label("pdf:page_size") == "PDF: page_size"
    assert bl.meta_label("djvu:year") == "DjVu: year"
    assert bl.meta_label("custom:ISBN-ALT") == "Доп.: ISBN-ALT"
    assert bl.meta_label("неизвестно") == "неизвестно"
    html_block = bl.details_html([("keyword", ["<script>x</script>"])])
    assert "<script>" not in html_block and "&lt;script&gt;" in html_block


# ---- нативный epub предпочтительнее сконвертированного из fb2 -------------------------------------

def make_text_epub(path: Path, text: str, *, converted: bool) -> None:
    """Epub с одним текстом. converted — метка fb2_to_epub3 в OPF; без неё epub выглядит как издательский."""
    generator = f'<meta name="generator" content="{bl.FB2_CONVERTER_GENERATOR}"/>' if converted else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Книга</dc:title><dc:creator>Иванов Иван</dc:creator><dc:language>ru</dc:language>"
        f"{generator}</metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chapter = ('<?xml version="1.0" encoding="utf-8"?>'
               f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{text}</p></body></html>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", chapter)


def test_fb2_to_epub3_writes_generator_meta_and_is_converted(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    bl.fb2_to_epub3(make_fb2("Тёмный лес", "Иван", "Иванов"), epub)
    with zipfile.ZipFile(epub) as archive:
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
    assert f'<meta name="generator" content="{bl.FB2_CONVERTER_GENERATOR}"/>' in opf
    assert bl.epub_converted(epub) is True


def test_native_epub_is_not_converted(tmp_path: Path) -> None:
    epub = tmp_path / "native.epub"
    make_epub(epub)
    assert bl.epub_converted(epub) is False


def test_converted_opf_markers_calibre_pandoc_and_fb2_manifest() -> None:
    calibre = '<dc:contributor opf:role="bkp">calibre 5.44.0 [https://calibre-ebook.com]</dc:contributor>'
    pandoc = '<package version="3.0" unique-identifier="epub-id-1">'
    fb2_manifest = ('<manifest><item id="css" href="css/book.css" media-type="text/css"/>'
                    '<item id="title-page" href="text/title.xhtml" media-type="application/xhtml+xml"/></manifest>')
    assert bl.converted_opf(f"<metadata>{calibre}</metadata>") is True
    assert bl.converted_opf(pandoc) is True
    assert bl.converted_opf(fb2_manifest) is True
    assert bl.converted_opf('<package unique-identifier="pub-id"><manifest/></package>') is False


def test_mark_roles_native_epub_wins_over_converted() -> None:
    base = {"fmt": "epub", "s": {1, 2, 3, 4}, "reason": "", "role": "kept", "coverage": None, "keeper": "", "quarantine": ""}
    texts = [{**base, "path": "A/Конверт.epub", "words": 300, "converted": True},
             {**base, "path": "A/Нативный.epub", "words": 250, "converted": False}]
    bl.mark_roles(texts)
    assert {t["path"]: t["role"] for t in texts} == {"A/Конверт.epub": "duplicate", "A/Нативный.epub": "kept"}


def test_same_name_key_ignores_suffix_case_and_extension() -> None:
    assert bl.same_name_key("Автор/Белка.epub") == bl.same_name_key("Автор/белка_1.fb2") == "белка"
    assert bl.same_name_key("Автор/Белка_100.epub") == "белка_100"


def test_better_case_prefers_sentence_case_for_cyrillic_and_title_case_for_latin() -> None:
    assert bl.better_case(["Чаша и Клинок", "Чаша и клинок"]) == "Чаша и клинок"
    assert bl.better_case(["Озма из Страны Оз", "Озма из страны Оз"]) == "Озма из страны Оз"
    assert bl.better_case(["Иванов - Чаша и Клинок", "Иванов - Чаша и клинок"]) == "Иванов - Чаша и клинок"
    assert bl.better_case(["Чаша и Клинок", "Чаша и клинок"]) == "Чаша и клинок"
    assert bl.better_case(["the lord of the rings", "The Lord of the Rings"]) == "The Lord of the Rings"
    assert bl.better_case(["Ancient Tale of the Sea", "Ancient Tale Of The Sea"]) == "Ancient Tale of the Sea"


def test_dedup_folders_keeps_native_epub_though_converted_is_larger(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    folder.mkdir(parents=True)
    text = folder_text(300, "слово")
    make_text_epub(folder / "Тёмный лес (конверт).epub", text, converted=True)
    make_text_epub(folder / "Тёмный лес.epub", text, converted=False)

    result = bl.dedup_folders(authors_cfg, out=lambda _line: None)

    assert result["duplicate"] == 1
    assert (folder / "Тёмный лес.epub").is_file()
    assert not (folder / "Тёмный лес (конверт).epub").exists()

# ----------------------------------------------------------------------
# purge-quarantine: окончательное удаление папок карантина по причинам (синтетика во временном карантине)
# ----------------------------------------------------------------------

def make_purge_tree(cfg: dict[str, Any], tmp_path: Path) -> tuple[Path, Path, Path]:
    """duplicate/a (файл и симлинк на внешний файл) и replaced/b (файл); отчёты — во временном каталоге."""
    qroot = Path(cfg["quarantine_dir"])
    dup = qroot / "duplicate" / "a"
    dup.mkdir(parents=True)
    (dup / "x.fb2").write_bytes(b"12345")
    (dup / "link").symlink_to(tmp_path / "outside.txt")
    kept = qroot / "replaced" / "b"
    kept.mkdir(parents=True)
    (kept / "y.fb2").write_bytes(b"678")
    (tmp_path / "outside.txt").write_bytes(b"keep")
    return qroot, dup, kept


def test_purge_quarantine_removes_one_reason_and_writes_report(cfg: dict[str, Any], tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(bl, "LOCK_PATH", tmp_path / ".books_library.lock")
    qroot, dup, kept = make_purge_tree(cfg, tmp_path)
    params = bl.command_params("purge-quarantine", {"reasons": ["duplicate"]})
    stats = bl.run_command(cfg, "purge-quarantine", False, params)
    assert not (qroot / "duplicate").exists()
    assert (kept / "y.fb2").read_bytes() == b"678"
    assert (tmp_path / "outside.txt").read_bytes() == b"keep"  # симлинк удалён как ссылка, цель не тронута
    assert stats["files"] == 2 and stats["errors"] == 0
    report_file = next((tmp_path / "reports").glob("purge-quarantine-*.json"))
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert list(report["reasons"]) == ["duplicate"]
    assert "duplicate/a/x.fb2" in [item["path"] for item in report["reasons"]["duplicate"]["items"]]


def test_purge_quarantine_dry_run_deletes_nothing_and_writes_no_report(cfg: dict[str, Any], tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    qroot, dup, _kept = make_purge_tree(cfg, tmp_path)
    bl.run_command(cfg, "purge-quarantine", True, bl.command_params("purge-quarantine", {"reasons": ["duplicate"]}))
    assert (dup / "x.fb2").read_bytes() == b"12345"
    assert not (tmp_path / "reports").exists()


def run_cli(argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """main() с изолированными логом и блокировкой: возвращает код выхода, как sys.exit(main())."""
    monkeypatch.setenv("MYTASKS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(bl, "LOCK_PATH", tmp_path / ".books_library.lock")
    return bl.main([*argv, "--config", str(tmp_path / "books_library.json")])


def test_purge_quarantine_busy_scan_lock_refuses_without_deleting(cfg: dict[str, Any], tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(bl, "LOCK_PATH", tmp_path / ".books_library.lock")  # блокировка — во временном каталоге
    _qroot, dup, _kept = make_purge_tree(cfg, tmp_path)
    params = bl.command_params("purge-quarantine", {"reasons": ["duplicate"]})
    with (tmp_path / ".books_library.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # другой процесс сканирования держит блокировку
        with pytest.raises(bl.ScanBusyError):
            bl.run_command(cfg, "purge-quarantine", False, params)
        assert run_cli(["purge-quarantine", "--reason", "duplicate"], tmp_path, monkeypatch) == 3
    assert (dup / "x.fb2").read_bytes() == b"12345"
    assert not (tmp_path / "reports").exists()  # отчёт не пишется, если блокировка не получена


def test_cli_exit_code_missing_folder_is_config_error_2(cfg: dict[str, Any], tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(["merge-folder", "--from", "Нет Такой", "--to", "Петров Иван"], tmp_path, monkeypatch) == 2


def test_cli_exit_code_database_error_is_1(cfg: dict[str, Any], tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bl, "run_command", locked)
    assert run_cli(["purge-quarantine", "--reason", "duplicate"], tmp_path, monkeypatch) == 1


def test_purge_quarantine_report_failure_deletes_nothing(cfg: dict[str, Any], tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_report(kind: str, data: dict[str, Any]) -> Path:
        raise OSError("отчёт недоступен")

    monkeypatch.setattr(bl, "LOCK_PATH", tmp_path / ".books_library.lock")
    monkeypatch.setattr(bl, "write_report", broken_report)
    _qroot, dup, _kept = make_purge_tree(cfg, tmp_path)
    params = bl.command_params("purge-quarantine", {"reasons": ["duplicate"]})
    with pytest.raises(OSError):
        bl.run_command(cfg, "purge-quarantine", False, params)
    assert (dup / "x.fb2").read_bytes() == b"12345"


def test_purge_quarantine_params_and_api_refusal(cfg: dict[str, Any]) -> None:
    with pytest.raises(bl.ConfigError):
        bl.command_params("purge-quarantine", {"reasons": ["unknown"]})
    with pytest.raises(bl.ConfigError):
        bl.command_params("purge-quarantine", {"reasons": []})
    assert bl.command_params("purge-quarantine", {"reasons": ["duplicate", "duplicate"]}) == {"reasons": ["duplicate"]}
    with pytest.raises(bl.ConfigError):
        bl.start_job(cfg, "purge-quarantine", False, {"reasons": ["duplicate"]})


def make_fb2_series(sequences: str) -> bytes:
    """fb2 без издательских данных с заданными sequence в title-info."""
    text = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
        "<description><title-info><genre>sf</genre>"
        "<author><first-name>Иван</first-name><last-name>Иванов</last-name></author>"
        f"<book-title>Книга</book-title><lang>ru</lang>{sequences}"
        "</title-info></description>"
        "<body><section><p>Текст.</p></section></body></FictionBook>"
    )
    return text.encode("utf-8")


def test_fb2_two_series_go_to_epub_and_book_meta(tmp_path: Path) -> None:
    epub = tmp_path / "two.epub"
    bl.fb2_to_epub3(make_fb2_series('<sequence name="Первая" number="2"/><sequence name="Вторая" number="5"/>'), epub)
    with zipfile.ZipFile(epub) as archive:
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
    assert '<meta property="belongs-to-collection" id="c2">Вторая</meta>' in opf
    assert '<meta refines="#c2" property="group-position">5</meta>' in opf
    root = tmp_path / "books"
    (root / "Иванов Иван").mkdir(parents=True)
    rel = "Иванов Иван/Две серии.epub"
    (root / rel).write_bytes(epub.read_bytes())
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    row = book_rows(cfg)[rel]
    assert row["series"] == "Первая"
    assert row["series_no"] == 2
    assert ("series_extra", "Вторая #5") in meta_pairs(cfg, rel)


def test_series_from_folder_and_stop_words(tmp_path: Path) -> None:
    assert bl.folder_series("Иванов Иван/Двойной Герой (110,124)/2. Книга.epub") == ("Двойной Герой", 2)
    assert bl.folder_series("_Сборники/Сборник/Двойной Герой (562,810)/2. Книга.epub") == ("Двойной Герой", 2)
    assert bl.folder_series("Иванов Иван/2. Книга.epub") is None
    assert bl.folder_series("Иванов Иван/Двойной Герой/Книга.epub") is None

    root = tmp_path / "books"
    (root / "Иванов Иван" / "Двойной Герой (110,124)").mkdir(parents=True)
    (root / "Иванов Иван" / "Рассказы").mkdir()
    folder_rel = "Иванов Иван/Двойной Герой (110,124)/2. Книга.fb2"
    (root / folder_rel).write_bytes(make_fb2_series(""))
    stop_folder_rel = "Иванов Иван/Рассказы/3. Ещё.fb2"
    (root / stop_folder_rel).write_bytes(make_fb2_series(""))
    stop_desc_rel = "Иванов Иван/Сборный.fb2"
    (root / stop_desc_rel).write_bytes(make_fb2_series('<sequence name="Рассказы" number="1"/>'))
    cfg = make_cfg(tmp_path, root)
    bl.scan(cfg)

    rows = book_rows(cfg)
    assert rows[folder_rel]["series"] == "Двойной Герой"
    assert rows[folder_rel]["series_no"] == 2
    assert ("series_folder", "Двойной Герой #2") in meta_pairs(cfg, folder_rel)
    assert rows[stop_folder_rel]["series"] == ""
    assert ("series_raw", "Рассказы") in meta_pairs(cfg, stop_folder_rel)
    assert rows[stop_desc_rel]["series"] == ""
    assert ("series_raw", "Рассказы") in meta_pairs(cfg, stop_desc_rel)


# ----------------------------------------------------------------------
# book_variants (схема v7): варианты из карантина сохраняются до удаления (синтетика во временном каталоге)
# ----------------------------------------------------------------------

def make_variant_tree(cfg: dict[str, Any]) -> Path:
    """Библиотека: A/x.epub, B/y.epub, C/z.fb2. Карантин: duplicate/A/x.epub (dup_members: keeper A/x.epub),
    replaced-calibre/B/y.epub, unzipped/C/z.fb2.zip (однокнижный архив с z.fb2 внутри)."""
    library = Path(cfg["books_dir"])
    for folder in ("A", "B", "C"):
        (library / folder).mkdir()
    make_epub(library / "A" / "x.epub")
    make_epub(library / "B" / "y.epub")
    (library / "C" / "z.fb2").write_bytes(make_fb2("Зелёная книга", "Иван", "Иванов"))
    qroot = Path(cfg["quarantine_dir"])
    (qroot / "duplicate" / "A").mkdir(parents=True)
    make_epub(qroot / "duplicate" / "A" / "x.epub")
    (qroot / "replaced-calibre" / "B").mkdir(parents=True)
    make_epub(qroot / "replaced-calibre" / "B" / "y.epub")
    (qroot / "unzipped" / "C").mkdir(parents=True)
    with zipfile.ZipFile(qroot / "unzipped" / "C" / "z.fb2.zip", "w") as archive:
        archive.writestr("z.fb2", make_fb2("Зелёная книга", "Иван", "Иванов"))
    bl.scan(cfg)
    with closing(bl.connect(cfg["db_path"])) as conn, conn:
        group = conn.execute("INSERT INTO dup_groups(key, status, created) VALUES ('test', 'resolved', 0)").lastrowid
        conn.execute("INSERT INTO dup_members(group_id, path, fmt, size, words, coverage, role, keeper, quarantine) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (group, "A/x.epub", "epub", 10, 7, None, "duplicate", "A/x.epub",
                      str(qroot / "duplicate" / "A" / "x.epub")))
    return qroot


def variant_rows(cfg: dict[str, Any]) -> dict[str, sqlite3.Row]:
    with closing(bl.connect(cfg["db_path"])) as conn:
        return {row["reason"]: row for row in conn.execute("SELECT * FROM book_variants")}


VARIANT_PARAMS = {"reasons": ["duplicate", "replaced-calibre", "unzipped"]}


def test_purge_writes_variants_with_keepers_then_deletes(cfg: dict[str, Any], tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    qroot = make_variant_tree(cfg)
    bl.run_command(cfg, "purge-quarantine", False, bl.command_params("purge-quarantine", VARIANT_PARAMS))

    rows = variant_rows(cfg)
    assert set(rows) == {"duplicate", "replaced-calibre", "unzipped"}
    assert rows["duplicate"]["variant_path"] == "duplicate/A/x.epub"
    assert rows["duplicate"]["keeper"] == "A/x.epub"
    assert rows["duplicate"]["words"] == 7  # из dup_members
    assert rows["replaced-calibre"]["keeper"] == "B/y.epub"
    assert rows["replaced-calibre"]["title"] == "Электронная книга"
    assert rows["unzipped"]["variant_path"] == "unzipped/C/z.fb2.zip"
    assert rows["unzipped"]["keeper"] == "C/z.fb2"  # распакованная книга — путь без .zip
    assert rows["unzipped"]["fmt"] == "fb2"  # zip разобран как однокнижный архив
    assert rows["unzipped"]["title"] == "Зелёная книга"
    assert json.loads(rows["unzipped"]["meta"])["fmt"] == "fb2"
    assert all(len(row["sha1"]) == 40 for row in rows.values())
    assert all(row["removed"] is not None for row in rows.values())
    assert not (qroot / "duplicate").exists()
    assert not (qroot / "replaced-calibre").exists()
    assert not (qroot / "unzipped").exists()


def test_purge_keeps_files_when_variant_write_fails(cfg: dict[str, Any], tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    qroot = make_variant_tree(cfg)
    monkeypatch.setattr(bl, "VARIANT_INSERT", "INSERT INTO no_such_table VALUES (?)")
    with pytest.raises(sqlite3.Error):
        bl.run_command(cfg, "purge-quarantine", False, bl.command_params("purge-quarantine", VARIANT_PARAMS))
    assert (qroot / "duplicate" / "A" / "x.epub").is_file()
    assert (qroot / "unzipped" / "C" / "z.fb2.zip").is_file()
    assert not (tmp_path / "reports").exists()  # отчёт не пишется до успешной записи вариантов
    assert variant_rows(cfg) == {}


def test_purge_dry_run_counts_variants_without_writing(cfg: dict[str, Any], tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    qroot = make_variant_tree(cfg)
    stats = bl.run_command(cfg, "purge-quarantine", True, bl.command_params("purge-quarantine", VARIANT_PARAMS))
    assert stats["variants"] == 3 and stats["variants_with_keeper"] == 3
    assert (qroot / "duplicate" / "A" / "x.epub").is_file()
    assert variant_rows(cfg) == {}


def test_variant_card_shows_variants_of_kept_book(cfg: dict[str, Any], tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    make_variant_tree(cfg)
    bl.run_command(cfg, "purge-quarantine", False, bl.command_params("purge-quarantine", VARIANT_PARAMS))
    for web in _serving(cfg, monkeypatch):
        status, _headers, body = fetch(f"{web}/book/{book_id(cfg, 'C/z.fb2')}")
        text = body.decode("utf-8")
        assert status == 200
        assert "<h3>Варианты</h3>" in text
        assert "zip-архив" in text and "Зелёная книга" in text
        status, _headers, body = fetch(f"{web}/book/{book_id(cfg, 'A/x.epub')}")
        assert "дубль" in body.decode("utf-8")


# ---- convert --only fb2: отказ оставляет fb2 в библиотеке (решение пользователя 18.11.2026) ---------

FB2_FAIL_REL = "Автор/Плохая.fb2"


@pytest.fixture
def fb2_fail_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "fb2fail"
    (root / "Автор").mkdir(parents=True)
    (root / FB2_FAIL_REL).write_bytes(make_fb2("Плохая", "Иван", "Иванов"))
    monkeypatch.setattr(bl, "REPORTS_DIR", tmp_path / "reports")
    cfg = make_cfg(tmp_path, root)
    bl.migrate_database(cfg["db_path"])  # схема с book_meta: convert_error пишется в неё
    return cfg


def fail_both_converters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bl, "fb2_own_epub", lambda data, produced: "собственный конвертер: тест")
    monkeypatch.setattr(bl, "calibre_fb2_problem", lambda data, staged, produced, tmp, timeout: "calibre: тест")


def test_convert_fb2_rejected_stays_in_library(fb2_fail_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    fail_both_converters(monkeypatch)
    src = Path(fb2_fail_cfg["books_dir"]) / FB2_FAIL_REL
    before = src.read_bytes()

    stats = bl.convert_fb2(fb2_fail_cfg)

    assert src.read_bytes() == before  # fb2 на месте, не тронут
    assert stats["rejected-convert"] == 1
    assert not (Path(fb2_fail_cfg["quarantine_dir"]) / "rejected-convert").exists()
    st = src.stat()
    assert bl.stored_convert_error(fb2_fail_cfg["db_path"], FB2_FAIL_REL) == f"calibre: тест|{st.st_size}|{st.st_mtime_ns}"


def test_convert_fb2_marker_skips_unchanged_file(fb2_fail_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    fail_both_converters(monkeypatch)
    bl.convert_fb2(fb2_fail_cfg)

    def must_not_run(*_args: Any) -> None:
        raise AssertionError("повторная конвертация неизменённого fb2")

    monkeypatch.setattr(bl, "fb2_own_epub", must_not_run)
    monkeypatch.setattr(bl, "calibre_fb2_problem", must_not_run)
    stats = bl.convert_fb2(fb2_fail_cfg)

    assert stats["skipped-rejected"] == 1
    assert "rejected-convert" not in stats
    assert (Path(fb2_fail_cfg["books_dir"]) / FB2_FAIL_REL).is_file()


def test_convert_fb2_changed_file_is_retried(fb2_fail_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    fail_both_converters(monkeypatch)
    bl.convert_fb2(fb2_fail_cfg)
    src = Path(fb2_fail_cfg["books_dir"]) / FB2_FAIL_REL
    src.write_bytes(make_fb2("Плохая вторая редакция", "Иван", "Иванов"))  # другой размер — подпись не совпадает

    calls: list[int] = []
    monkeypatch.setattr(bl, "fb2_own_epub", lambda data, produced: calls.append(1) or "собственный конвертер: тест")
    stats = bl.convert_fb2(fb2_fail_cfg)

    assert calls == [1]  # конвертация запущена снова
    assert stats["rejected-convert"] == 1
    assert "skipped-rejected" not in stats
    assert src.is_file()


# ---- epub не хуже fb2: полнота, дополнение из fb2, сноски и ссылки ---------------------------------

def fb2_with_notes() -> bytes:
    """fb2 с серией, аннотацией, двумя главами с заголовками и телом сносок (name="notes")."""
    text = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">'
        "<description><title-info><genre>sf</genre>"
        "<author><first-name>Иван</first-name><last-name>Иванов</last-name></author>"
        "<book-title>Книга</book-title><annotation><p>Аннотация</p></annotation>"
        '<lang>ru</lang><sequence name="Цикл" number="2"/>'
        "</title-info></description>"
        '<body><section><title><p>Глава 1</p></title><p>Текст <a l:href="#n1" type="note">[1]</a>.</p></section>'
        "<section><title><p>Глава 2</p></title><p>Ещё текст.</p></section></body>"
        '<body name="notes"><section id="n1"><title><p>1</p></title><p>Сноска.</p></section></body>'
        "</FictionBook>"
    )
    return text.encode("utf-8")


def make_note_epub(path: Path, chapter_body: str) -> None:
    """Epub с одной главой OEBPS/ch1.xhtml (тело как есть), без оглавления и сносок."""
    path.parent.mkdir(parents=True, exist_ok=True)
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Книга</dc:title><dc:creator>Иванов Иван</dc:creator><dc:language>ru</dc:language></metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f"<body>{chapter_body}</body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/ch1.xhtml", chapter)


def test_book_completeness_fb2_reports_series_toc_and_notes(tmp_path: Path) -> None:
    fb2 = tmp_path / "book.fb2"
    fb2.write_bytes(fb2_with_notes())
    result = bl.book_completeness(fb2)
    assert result["series"] is True
    assert result["annotation"] is True
    assert result["toc"] is True
    assert result["notes"] is True
    assert result["cover"] is False
    assert result["translator"] is False


def test_enrich_epub_from_adds_missing_fields_and_keeps_title(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    make_text_epub(epub, "текст", converted=False)
    added = bl.enrich_epub_from(epub, {"series": "Цикл", "series_no": 2, "annotation": "Аннотация"}, None)
    assert added == ["series", "annotation"]
    with zipfile.ZipFile(epub) as archive:
        assert archive.namelist()[0] == "mimetype"
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
    assert "<dc:title>Книга</dc:title>" in opf
    assert '<meta name="calibre:series" content="Цикл"/>' in opf
    assert "<dc:description>Аннотация</dc:description>" in opf
    assert bl.enrich_epub_from(epub, {"series": "Другой", "annotation": "Иная"}, None) == []


def test_dedup_rebuilds_epub_without_toc_from_fb2_chapters(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    words = [f"слово{i}" for i in range(300)]
    first, second = " ".join(words[:150]), " ".join(words[150:])
    make_text_epub(folder / "Тёмный лес.epub", f"Глава 1 {first} Глава 2 {second}", converted=False)
    bodies = (
        "<body>"
        f"<section><title><p>Глава 1</p></title><p>{first}</p></section>"
        f"<section><title><p>Глава 2</p></title><p>{second}</p></section>"
        "</body>"
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Тёмный лес.fb2").write_bytes(_fb2_doc(bodies))

    bl.dedup(authors_cfg, out=lambda _line: None)

    quarantine = Path(authors_cfg["quarantine_dir"])
    assert bl.book_completeness(folder / "Тёмный лес.epub")["toc"] is True
    assert not (folder / "Тёмный лес.fb2").exists()
    assert (quarantine / "duplicate" / "Иванов Иван Иванович" / "Тёмный лес.fb2").is_file()
    old = quarantine / "replaced" / "Иванов Иван Иванович" / "Тёмный лес.epub"
    assert old.is_file()
    assert bl.book_completeness(old)["toc"] is False


def test_note_referenced_from_two_chapters_gets_own_aside_in_each(tmp_path: Path) -> None:
    bodies = (
        "<body>"
        '<section><title><p>Глава 1</p></title><p>Текст <a l:href="#n1" type="note">[1]</a>.</p></section>'
        '<section><title><p>Глава 2</p></title><p>Ещё <a l:href="#n1" type="note">[1]</a>.</p></section>'
        "</body>"
        '<body name="notes"><section id="n1"><title><p>1</p></title><p>Сноска.</p></section></body>'
    )
    epub = tmp_path / "notes.epub"
    bl.fb2_to_epub3(_fb2_doc(bodies), epub)
    files = read_epub(epub)
    first = files["OEBPS/text/ch001.xhtml"].decode("utf-8")
    second = files["OEBPS/text/ch002.xhtml"].decode("utf-8")
    assert 'id="ref-n1"' in first and 'href="#n1"' in first
    assert '<aside epub:type="footnote" role="doc-footnote" id="n1"' in first
    assert 'href="#ref-n1"' in first
    assert 'id="ref-n1-2"' in second and 'href="#n1-2"' in second
    assert '<aside epub:type="footnote" role="doc-footnote" id="n1-2"' in second
    assert 'href="#ref-n1-2"' in second
    assert 'href="#n1"' not in second and 'id="n1"' not in second
    # блок сносок — последний элемент тела главы, сноски в нём не скрыты
    assert re.search(r'<section class="footnotes"[^>]*>.*</section></body>', second, re.S)
    assert 'class="footnote"' in second
    assert "display:none" not in second
    assert "-cr-hint: footnote-inpage" in files["OEBPS/css/book.css"].decode("utf-8")


def test_enrich_epub_adds_noteref_and_footnote_without_fb2(authors_cfg: dict[str, Any]) -> None:
    folder = Path(authors_cfg["books_dir"]) / "Иванов Иван Иванович"
    epub = folder / "Книга.epub"
    make_note_epub(epub, '<p>Текст <a href="#n1">1</a>.</p><div id="n1" class="note">Сноска.</div>')

    bl.enrich_epub(authors_cfg, out=lambda _line: None)

    with zipfile.ZipFile(epub) as archive:
        chapter = archive.read("OEBPS/ch1.xhtml").decode("utf-8")
    assert 'epub:type="noteref" href="#n1"' in chapter
    assert '<div epub:type="footnote" id="n1" class="note footnote"' in chapter
    assert bl.book_completeness(epub)["links_ok"] is True
