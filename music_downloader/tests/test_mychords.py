import importlib.util
import logging
from pathlib import Path
from unittest.mock import Mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "drivers" / "music_downloader_mychords_net.py"
SPEC = importlib.util.spec_from_file_location("mychords", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
mychords = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mychords)

BASE_URL = "https://music.mychords.net"
LISTING = """
<li class="b-listing__full__item"><span class="b-listing__full__item__position">Вчера</span>
<a class="b-listing__full__item__name" href="/ru/augxst/281200-augxst-tenoh-money-fever.html">Augxst &amp; Tenoh - Money Fever</a></li>
<li class="b-listing__full__item"><a class="b-listing__full__item__name" href="/ru/x/281031-anna.html">Anna Grey - Das was bleibt</a></li>
<li class="b-listing__full__item"><a class="b-listing__full__item__name" href="/ru/x/no-id.html">Без номера</a></li>
<li class="b-listing__full__item"><a class="b-listing__full__item__name" href="/ru/x/5-solo.html">Только название</a></li>
"""
TRACK_PAGE = (
    '<div class="b-words__player" data-src=\'<iframe width="100%" src="https://audio.xpleer.com/embed/?id=abc"></iframe>\'></div>'
)
PLAYER_PAGE = (
    '<div id="file" data-id="file" data-mp3="https://storage.xpleer.com/get_file/?fileId=1" '
    'data-artist="Augxst &amp; Tenoh" data-song="Money Fever"></div>'
)


def test_parse_listing_extracts_ids_and_splits_artist_title():
    assert mychords.parse_listing(LISTING, BASE_URL) == [
        {
            "id": "281200",
            "artist": "Augxst & Tenoh",
            "title": "Money Fever",
            "src": "https://music.mychords.net/ru/augxst/281200-augxst-tenoh-money-fever.html",
        },
        {"id": "281031", "artist": "Anna Grey", "title": "Das was bleibt", "src": "https://music.mychords.net/ru/x/281031-anna.html"},
        {"id": "5", "artist": None, "title": "Только название", "src": "https://music.mychords.net/ru/x/5-solo.html"},
    ]


def test_parse_player_chain():
    assert mychords.parse_player_url(TRACK_PAGE, BASE_URL) == "https://audio.xpleer.com/embed/?id=abc"
    assert mychords.parse_player_url("<html></html>", BASE_URL) is None
    assert mychords.parse_player_file(PLAYER_PAGE) == "https://storage.xpleer.com/get_file/?fileId=1"
    assert mychords.parse_player_file('<div id="file" data-mp3="javascript:x"></div>') is None
    assert mychords.parse_player_file("<html></html>") is None


def test_resolve_track_returns_download_and_referer(monkeypatch):
    monkeypatch.setattr(mychords.time, "sleep", lambda _: None)
    session = Mock()
    session.get.side_effect = [Mock(text=TRACK_PAGE), Mock(text=PLAYER_PAGE)]
    track = {"id": "281200", "artist": "Augxst & Tenoh", "title": "Money Fever", "src": f"{BASE_URL}/ru/augxst/281200.html"}
    assert mychords.resolve_track(session, track) == {
        "download_url": "https://storage.xpleer.com/get_file/?fileId=1",
        "referer": "https://audio.xpleer.com/embed/?id=abc",
        "src": f"{BASE_URL}/ru/augxst/281200.html",
    }
    assert session.get.call_args_list[1].kwargs["headers"] == {"Referer": f"{BASE_URL}/ru/augxst/281200.html"}


def test_resolve_track_reports_missing_player_or_errors(monkeypatch, caplog):
    monkeypatch.setattr(mychords.time, "sleep", lambda _: None)
    track = {"id": "1", "artist": "A", "title": "B", "src": f"{BASE_URL}/ru/x/1-a.html"}
    session = Mock()
    session.get.return_value = Mock(text="<html></html>")
    assert mychords.resolve_track(session, track) is None
    session.get.side_effect = RuntimeError("HTTP 503")
    assert mychords.resolve_track(session, track) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.site == "mychords_net"]
    assert any("нет плеера" in message for message in warnings)
    assert any("HTTP 503" in message for message in warnings)


def test_get_tracks_logs_empty_and_failed_listing(caplog):
    session = Mock()
    session.get.return_value = Mock(text="<html></html>")
    assert mychords.get_tracks(session, BASE_URL, "/ru/novinki") == []
    session.get.side_effect = RuntimeError("отказ")
    assert mychords.get_tracks(session, BASE_URL, "/ru/novinki") == []
    levels = {r.levelno for r in caplog.records if r.site == "mychords_net"}
    assert levels == {logging.WARNING, logging.ERROR}
