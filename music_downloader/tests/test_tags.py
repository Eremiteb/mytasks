import hashlib
import importlib.util
import os
import tempfile
from pathlib import Path

import pytest
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    PRIV,
    TALB,
    TCOP,
    TENC,
    TIT2,
    TLEN,
    TOWN,
    TPE1,
    TPUB,
    TXXX,
    UFID,
    USLT,
    WOAF,
    WXXX,
)

# Лог движка при импорте модуля — во временный каталог, а не в logs/ репозитория
os.environ.setdefault("MYTASKS_LOG_DIR", tempfile.mkdtemp(prefix="music_downloader_test_logs_"))

MODULE_PATH = Path(__file__).resolve().parents[1] / "music_downloader.py"
SPEC = importlib.util.spec_from_file_location("music_downloader_engine_tags", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)

AUDIO = b"\xff\xfb\x90\x00" + b"\x00" * 400


def _mp3(path, *frames, version=4):
    path.write_bytes(AUDIO)
    tags = ID3()
    for frame in frames:
        tags.add(frame)
    tags.save(path, v2_version=version)
    return path


@pytest.mark.parametrize(
    ("text", "expected", "removed"),
    [
        ("Среди Алматы (sefon.pro)", "Среди Алматы", ["sefon.pro"]),
        ("Song - www.site.com", "Song", ["www.site.com"]),
        ("https://muzofond.fm/x?id=1 - Song", "Song", ["https://muzofond.fm/x?id=1"]),
        ("Song скачано с Sefon.Pro", "Song", ["скачано с Sefon.Pro"]),
        ("Artist [download from mp3.kz]", "Artist", ["download from mp3.kz"]),
        ("Song (Remix)", "Song (Remix)", []),
        ("Mr.Big", "Mr.Big", []),
        ("A.S.A.P.", "A.S.A.P.", []),
        ("Fall in love", "Fall in love", []),
        ("mail@site.ru", "mail@site.ru", []),
        ("музыка.рф", "", ["музыка.рф"]),
        # Ложные срабатывания, найденные на библиотеке /mnt/copy/Music.
        ("Дофаминовая Prod.by Каролина Лонг", "Дофаминовая Prod.by Каролина Лонг", []),
        ("Stand By www.streetlife.kz", "Stand By", ["www.streetlife.kz"]),
        ("http:/butt-head.ru", "", ["http:/butt-head.ru"]),
        ("www.erdos.pro@mail.ru", "www.erdos.pro@mail.ru", []),
        ("PrimeMusic.ru August 2014", "August 2014", ["PrimeMusic.ru"]),
    ],
)
def test_strip_tag_urls(text, expected, removed):
    assert engine.strip_tag_urls(text) == (expected if removed else text, removed)


def test_clean_mp3_tags_moves_links_and_extras_to_comment(tmp_path):
    path = _mp3(
        tmp_path / "a.mp3",
        TIT2(encoding=3, text="Среди Алматы (sefon.pro)"),
        TPE1(encoding=3, text="Ellaya | www.beufm.kz"),
        TALB(encoding=3, text="https://lmusic.kz"),
        TPUB(encoding=3, text="Sefon.Pro"),
        TCOP(encoding=3, text="Данная аудиозапись скачана с сайта http://muz.ru"),
        TENC(encoding=3, text="Закачано с muzofond.fm"),
        TOWN(encoding=3, text="Радио Beu FM"),
        TXXX(encoding=3, desc="MusicBrainz Album Id", text="5c63c2fb-882a-4cd3-9ea9-44c0f03dce4b"),
        TXXX(encoding=3, desc="url", text="www.bustarhymes.com"),
        TXXX(encoding=3, desc="major_brand", text="lmusic.kz"),
        TXXX(encoding=3, desc="LAVF61.7.100", text="lmusic.kz"),
        TXXX(encoding=3, desc="comment", text="R&S Collection"),
        TLEN(encoding=3, text="183000"),
        WOAF(url="https://sefon.pro/mp3/1"),
        WXXX(encoding=3, desc="Сайт", url="http://site.kz"),
        PRIV(owner="www.promo.com", data=b"\x01"),
        PRIV(owner="WM/MediaClassPrimaryID", data=b"\x02"),
        UFID(owner="http://musicbrainz.org", data=b"1c40e779"),
        COMM(encoding=3, lang="rus", desc="", text="Хорошая песня"),
        COMM(encoding=3, lang="eng", desc="iTunNORM", text="0000 0000"),
        USLT(encoding=3, lang="rus", desc="", text="Куплет\nТексты на lyrics.ru\nПрипев"),
        APIC(encoding=3, mime="image/jpeg", type=3, desc="sefon.pro", data=b"jpeg"),
    )
    assert engine.clean_mp3_tags(path) is True

    tags = ID3(path)
    assert tags["TIT2"].text == ["Среди Алматы"]
    assert tags["TPE1"].text == ["Ellaya"]
    assert sorted(tags.keys()) == sorted([
        "TIT2", "TPE1", "TXXX:MusicBrainz Album Id", "UFID:http://musicbrainz.org", "COMM::rus", "USLT::rus", "APIC:",
    ])
    assert tags["USLT::rus"].text == "Куплет\nПрипев"
    comments = tags.getall("COMM")
    assert len(comments) == 1 and comments[0].desc == "" and comments[0].lang == "rus"
    # Прежний комментарий первым, затем ссылки в короткой форме без повторов:
    # sefon.pro, Sefon.Pro и https://sefon.pro/mp3/1 дают одну ссылку на страницу.
    pieces = comments[0].text[0].split(" | ")
    assert pieces[0] == "Хорошая песня"
    assert sorted(pieces[1:]) == sorted([
        "beufm.kz", "lmusic.kz", "muz.ru", "muzofond.fm", "Радио Beu FM", "bustarhymes.com", "R&S Collection",
        "sefon.pro/mp3/1", "site.kz", "promo.com", "lyrics.ru",
    ])

    # Повторный проход ничего не меняет и файл не переписывает.
    before = path.read_bytes()
    assert engine.clean_mp3_tags(path) is False
    assert path.read_bytes() == before


def test_clean_mp3_tags_keeps_release_fields_without_links(tmp_path):
    path = _mp3(
        tmp_path / "c.mp3",
        TIT2(encoding=3, text="Song"),
        TPUB(encoding=3, text="Moon Records"),
        TCOP(encoding=3, text="© 2007 Zomba Songs"),
        TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text="-3.46 dB"),
    )
    before = path.read_bytes()
    assert engine.clean_mp3_tags(path) is False
    assert path.read_bytes() == before


def test_clean_mp3_tags_keeps_clean_file_and_version(tmp_path):
    path = _mp3(tmp_path / "b.mp3", TIT2(encoding=1, text="Song"), TPE1(encoding=1, text="Artist"), version=3)
    before = path.read_bytes()
    assert engine.clean_mp3_tags(path) is False
    assert path.read_bytes() == before

    _mp3(path, TIT2(encoding=1, text="Song www.x.com"), version=3)
    assert engine.clean_mp3_tags(path) is True
    tags = ID3(path)
    assert tags.version[:2] == (2, 3)
    assert tags["TIT2"].text == ["Song"]


def test_clean_mp3_tags_updates_existing_id3v1(tmp_path):
    path = tmp_path / "v1.mp3"
    v1 = b"TAG" + b"Song sefon.pro".ljust(30, b"\0") + b"Artist".ljust(30, b"\0") + b"".ljust(30, b"\0")
    path.write_bytes(AUDIO + v1 + b"2020" + b"".ljust(30, b"\0") + b"\xff")
    assert engine.clean_mp3_tags(path) is True
    tail = path.read_bytes()[-128:]
    assert tail.startswith(b"TAGSong\0") and b"sefon" not in tail
    assert "sefon.pro" in ID3(path).getall("COMM")[0].text[0]


def test_clean_mp3_tags_ignores_files_without_tags(tmp_path):
    path = tmp_path / "raw.mp3"
    path.write_bytes(AUDIO)
    assert engine.clean_mp3_tags(path) is False
    assert path.read_bytes() == AUDIO


def test_save_response_cleans_tags_in_every_copy_and_hashes_result(tmp_path):
    source = _mp3(tmp_path / "src.mp3", TIT2(encoding=3, text="Song (sefon.pro)"))
    payload = source.read_bytes()

    class Response:
        def iter_content(self, chunk_size):
            yield payload

    paths = []
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        paths.append({"path": str(tmp_path / name)})
    size, saved, filehash = engine.save_response(Response(), paths, "track.mp3", expected_size=len(payload))
    contents = [Path(item).read_bytes() for item in saved]
    assert contents[0] == contents[1] != payload
    assert size == len(contents[0])
    assert filehash == hashlib.sha256(contents[0]).hexdigest()
    assert ID3(saved[0])["TIT2"].text == ["Song"]
    assert not list(tmp_path.glob("*/*.part*"))


def test_save_response_keeps_file_when_tag_cleaning_fails(tmp_path, monkeypatch):
    def broken(_path):
        raise ValueError("битый тег")

    monkeypatch.setattr(engine, "clean_mp3_tags", broken)

    class Response:
        def iter_content(self, chunk_size):
            yield AUDIO

    (tmp_path / "one").mkdir()
    size, saved, filehash = engine.save_response(Response(), [{"path": str(tmp_path / "one")}], "t.mp3")
    assert Path(saved[0]).read_bytes() == AUDIO
    assert (size, filehash) == (len(AUDIO), hashlib.sha256(AUDIO).hexdigest())
    assert not list(tmp_path.glob("*/*.part*"))


def test_optimize_comment_deduplicates_and_compacts():
    assert engine.optimize_comment(
        [
            "Скачано с https://www.Sefon.Pro/ | Хорошая песня",
            "[sefon.pro]",
            "Данная аудиозапись скачана с сайта http://muz.ru\nХорошая песня",
            " 00000666 00000660 000050DC ",
            "https://lmusic.kz/api/download/1",
            "lmusic.kz",
            "Ellaya",
            " - ",
        ],
        {"ellaya"},
    ) == ["sefon.pro", "Хорошая песня", "muz.ru", "lmusic.kz/api/download/1"]


def test_clean_mp3_tags_optimizes_existing_comments(tmp_path):
    path = _mp3(
        tmp_path / "d.mp3",
        TIT2(encoding=3, text="Song"),
        TPE1(encoding=3, text="Artist"),
        COMM(encoding=3, lang="eng", desc="", text="lmusic.kz"),
        COMM(encoding=3, lang="eng", desc="ID3v1 Comment", text="https://lmusic.kz/"),
        COMM(encoding=3, lang="eng", desc="note", text="Artist"),
    )
    assert engine.clean_mp3_tags(path) is True
    assert [frame.text for frame in ID3(path).getall("COMM")] == [["lmusic.kz"]]


def test_clean_mp3_tags_protects_title_and_moves_promo_tail(tmp_path):
    path = _mp3(
        tmp_path / "e.mp3",
        TIT2(encoding=3, text="Евродэнс.ru (lmusic.kz)"),
        TALB(encoding=3, text="Musiclife.kz - Скачать музыку, mp3, песни бесплатно!"),
        USLT(encoding=3, lang="eng", desc="", text="www.RnBXclusive.com\r\nПервая строка\r\n\r\n"),
        COMM(encoding=3, lang="eng", desc="ID3v1 Comment", text="www.RnBXc"),
    )
    assert engine.clean_mp3_tags(path) is True
    tags = ID3(path)
    assert tags["TIT2"].text == ["Евродэнс.ru"]
    assert "TALB" not in tags
    assert tags["USLT::eng"].text == "Первая строка\r\n\r\n"
    assert tags.getall("COMM")[0].text == ["lmusic.kz | musiclife.kz | rnbxclusive.com"]
    assert engine.clean_mp3_tags(path) is False
