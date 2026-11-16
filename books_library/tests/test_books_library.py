"""Тесты books_library: только синтетические данные во временных каталогах, без реальной библиотеки."""

from __future__ import annotations

import base64
import importlib
import json
import os
import sqlite3
import struct
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
    assert zipped["fmt"] == "fb2.zip"
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

    assert stats["wrong"] == 1
    assert broken.read_bytes() == before
    assert not (bl.REPORTS_DIR).exists()
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

    assert stats["wrong"] == 1
    assert stats["fixed"] == 1
    assert broken.read_bytes() == before_broken.replace(WRONG_DECL, RIGHT_DECL, 1)
    assert real.read_bytes() == before_real
    assert normal.read_bytes() == before_normal
    reports = list((bl.REPORTS_DIR).glob("books_library-fix-encoding-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert [item["path"] for item in report["files"]] == ["Автор/Ошибочная.fb2"]
    assert report["files"][0]["declared"] == "windows-1251"


def test_fix_encoding_second_run_finds_nothing(enc_cfg: dict[str, Any]) -> None:
    bl.fix_encoding(enc_cfg)

    stats = bl.fix_encoding(enc_cfg)

    assert stats["wrong"] == 0
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

    assert stats["wrong"] == 1
    assert stats["fixed"] == 1
    assert (root / "Автор" / "cp.txt").read_bytes() == TXT_TEXT.encode("utf-8")
    assert (root / "Автор" / "utf.txt").read_bytes() == utf_before
    assert len(list((bl.REPORTS_DIR).glob("books_library-fix-encoding-*.json"))) == 1


def test_fix_encoding_only_fb2_skips_txt(txt_cfg: dict[str, Any]) -> None:
    cp = Path(txt_cfg["books_dir"]) / "Автор" / "cp.txt"
    before = cp.read_bytes()

    stats = bl.fix_encoding(txt_cfg, only="fb2")

    assert stats["wrong"] == 0
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
    assert stats["extracted"] == 0
    assert not (root / "big.fb2").exists()


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
