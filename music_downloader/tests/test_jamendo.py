import importlib.util
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "drivers" / "music_downloader_jamendo_com.py"
SPEC = importlib.util.spec_from_file_location("jamendo", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
jamendo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jamendo)

BASE_URL = "https://www.jamendo.com"
TRACK = {
    "id": "123456",
    "artist_name": "Исполнитель",
    "name": "Песня",
    "audiodownload_allowed": True,
    "audiodownload": "https://example.invalid/download.mp3",
    "audio": "https://example.invalid/stream.mp3",
}


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(jamendo, "_get_client_id", lambda: "test-client")
    monkeypatch.setattr(jamendo.time, "sleep", lambda _: None)
    session = Mock()
    session.get.return_value.json.return_value = {
        "headers": {"status": "success", "results_count": 1},
        "results": [TRACK.copy()],
    }
    return session


@pytest.mark.parametrize("category", ["/", "/playlist/123/example"])
def test_api_tracks_and_playlist_compatibility(session, category):
    if category != "/":
        session.get.return_value.json.return_value["results"] = [{"tracks": [TRACK.copy()]}]
    assert jamendo.get_tracks(session, BASE_URL, category) == [{
        "id": "123456",
        "artist": "Исполнитель",
        "title": "Песня",
        "download_url": TRACK["audiodownload"],
        "referer": BASE_URL + category,
    }]
    if category == "/":
        session.get.assert_called_once_with(
            "https://api.jamendo.com/v3.0/tracks/",
            params={"client_id": "test-client", "format": "json", "limit": 200, "order": "releasedate_desc"},
            timeout=30,
        )
    else:
        session.get.assert_called_once_with(
            "https://api.jamendo.com/v3.0/playlists/tracks/",
            params={"client_id": "test-client", "id": "123", "limit": 200, "offset": 0},
            timeout=30,
        )


@pytest.mark.parametrize("category", ["/", "/playlist/123/example"])
def test_download_disabled_items_skipped(session, category):
    entries = [TRACK.copy()]
    for index, changes in enumerate([
        {"audiodownload_allowed": False},
        {"audiodownload_allowed": None},
        {"audiodownload_allowed": "false"},
        {"audiodownload_allowed": "true"},
        {"audiodownload": ""},
        {"audiodownload": " "},
        {"audiodownload": None},
    ], start=1):
        entries.append(TRACK | {"id": str(123456 + index)} | changes)
    missing_flag = TRACK | {"id": "654321"}
    del missing_flag["audiodownload_allowed"]
    entries.append(missing_flag)
    session.get.return_value.json.return_value["results"] = (
        entries if category == "/" else [{"tracks": entries}]
    )
    tracks = jamendo.get_tracks(session, BASE_URL, category)
    assert [track["id"] for track in tracks] == ["123456"]
    assert tracks[0]["download_url"] == TRACK["audiodownload"]
    assert session.get.call_count == 1


@pytest.mark.parametrize("category", ["/", "/playlist/123/example"])
@pytest.mark.parametrize("failure", ["request", "http", "json", "api", "empty", "disabled"])
def test_api_errors_and_empty_not_silent(session, caplog, category, failure):
    response = session.get.return_value
    if failure == "request":
        session.get.side_effect = TimeoutError("тайм-аут")
    elif failure == "http":
        response.raise_for_status.side_effect = RuntimeError("HTTP 503")
    elif failure == "json":
        response.json.side_effect = ValueError("не JSON")
    elif failure == "api":
        response.json.return_value["headers"] = {"status": "failed", "error_message": "ошибка клиента"}
    elif failure == "empty":
        response.json.return_value["results"] = []
    else:
        entries = [TRACK | {"audiodownload_allowed": False}]
        response.json.return_value["results"] = entries if category == "/" else [{"tracks": entries}]
    assert jamendo.get_tracks(session, BASE_URL, category) == []
    expected_level = logging.WARNING if failure in ("empty", "disabled") else logging.ERROR
    assert any(record.levelno == expected_level for record in caplog.records)
    assert all(record.site == "jamendo_com" for record in caplog.records)
    assert "JAMENDO:" in caplog.text
    assert session.get.call_count == 1


@pytest.mark.parametrize("payload", [None, {}, {"headers": None}, {"headers": {"status": "success"}, "results": {}}])
def test_root_malformed_response_logged(session, caplog, payload):
    session.get.return_value.json.return_value = payload
    assert jamendo.get_tracks(session, BASE_URL, "/") == []
    assert any(record.levelno == logging.ERROR and record.site == "jamendo_com" for record in caplog.records)


def test_root_api_warning_logged(session, caplog):
    session.get.return_value.json.return_value["headers"]["warnings"] = "неизвестный параметр"
    assert len(jamendo.get_tracks(session, BASE_URL, "/")) == 1
    assert any(
        record.levelno == logging.WARNING and record.site == "jamendo_com"
        and "неизвестный параметр" in record.getMessage()
        for record in caplog.records
    )


def test_root_missing_client_id_no_html_fallback(session, monkeypatch, caplog):
    monkeypatch.setattr(jamendo, "_get_client_id", lambda: None)
    assert jamendo.get_tracks(session, BASE_URL, "/") == []
    session.get.assert_not_called()
    assert any(
        record.levelno == logging.ERROR and record.site == "jamendo_com"
        and "client_id" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("category, client_id", [("/explore", "test-client"), ("/playlist/123/example", None)])
def test_other_categories_keep_html_parser(session, monkeypatch, category, client_id):
    monkeypatch.setattr(jamendo, "_get_client_id", lambda: client_id)
    session.get.return_value.text = (
        '<div data-track-id="123456" data-artist="Исполнитель" data-title="Песня"></div>'
        '<script type="application/ld+json">'
        '{"tracks": [{"id": "123456"}, {"id": "654321", "name": "Другая песня"}]}'
        '</script>'
    )
    tracks = jamendo.get_tracks(session, BASE_URL, category)
    assert [track["id"] for track in tracks] == ["123456", "654321"]
    assert tracks[0]["artist"] == "Исполнитель"
    assert tracks[1]["title"] == "Другая песня"
    assert all(track["referer"] == BASE_URL + category for track in tracks)
    assert tracks[0]["download_url"] == jamendo._build_download_url("123456")
    session.get.assert_called_once_with(
        BASE_URL + category, timeout=30, headers={"User-Agent": "Mozilla/5.0", "Referer": BASE_URL},
    )
    session.get.return_value.json.assert_not_called()


def test_empty_html_logged(session, caplog):
    session.get.return_value.text = "<html></html>"
    assert jamendo.get_tracks(session, BASE_URL, "/explore") == []
    assert any(
        record.levelno == logging.WARNING and record.site == "jamendo_com"
        and "в HTML не найдено треков" in record.getMessage()
        for record in caplog.records
    )
