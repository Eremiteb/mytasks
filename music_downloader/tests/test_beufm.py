import importlib.util
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "drivers" / "music_downloader_beufm_kz.py"
SPEC = importlib.util.spec_from_file_location("beufm", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
beufm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(beufm)


@pytest.fixture(autouse=True)
def _no_real_recordings(monkeypatch):
    """Тесты не читают рабочий каталог записей эфира."""
    monkeypatch.setattr(beufm, "_recorded_tracks", lambda skip_ids: [])
    monkeypatch.setattr(beufm, "_recorder_settings", lambda: (_ for _ in ()).throw(FileNotFoundError("нет")))


def _patch_sources(monkeypatch, module, sefon, lmusic):
    """Подменяет поиск в общих источниках (`search_sources` по умолчанию: Sefon, затем LMusic)."""
    by_name = {"sefon.pro": sefon, "lmusic_kz": lmusic}
    monkeypatch.setattr(module.audio_search.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        module.audio_search, "_query_source", lambda session, source, query: by_name[source["name"]](session, query)
    )

# Страница в исходном виде: спан пуст, его наполняет Player.js из 1.txt.
EMPTY_PAGE = '<div class="tags"><span id="track_org"></span></div>'
FILLED_PAGE = '<div class="tags"><span id="track_org">Ellaya - Среди Алматы</span></div>'
# Настоящий ответ 1.txt: кириллица числовыми HTML-сущностями («Сүйем мен»).
NOW_PLAYING = "Abik Jeksen - &#1057;&#1199;&#1081;&#1077;&#1084; &#1084;&#1077;&#1085;"


def _response(text, url="https://beufm.kz/"):
    return Mock(text=text, url=url)


def test_parse_now_playing_reads_span_text_and_entities():
    assert beufm.parse_now_playing(FILLED_PAGE) == ("Ellaya", "Среди Алматы")
    assert beufm.parse_now_playing(NOW_PLAYING) == ("Abik Jeksen", "Сүйем мен")
    # Разделитель — первый « - »: остальные остаются в названии.
    assert beufm.parse_now_playing("A - B - C") == ("A", "B - C")


@pytest.mark.parametrize("markup", ["", None, EMPTY_PAGE, "Beu FM", "Реклама - ", " - Без исполнителя", "Дефис-без-пробелов"])
def test_parse_now_playing_ignores_non_tracks(markup):
    assert beufm.parse_now_playing(markup) is None


def test_site_root_replaces_www_with_certificate_host():
    assert beufm._site_root("https://www.beufm.kz") == "https://beufm.kz"
    assert beufm._site_root("https://www.beufm.kz/x") == "https://beufm.kz/x"
    assert beufm._site_root("https://beufm.kz") == "https://beufm.kz"


def test_get_tracks_uses_span_when_filled():
    session = Mock()
    session.get.return_value = _response(FILLED_PAGE)
    tracks = beufm.get_tracks(session, "https://www.beufm.kz", "/")
    assert tracks == [{
        "id": beufm._track_id("Ellaya", "Среди Алматы"),
        "artist": "Ellaya",
        "title": "Среди Алматы",
        "src": "https://beufm.kz/",
        "search_translit": True,
    }]
    assert tracks[0]["id"].startswith("fm_")
    session.get.assert_called_once()
    assert session.get.call_args.args[0] == "https://beufm.kz/"


def test_get_tracks_falls_back_to_now_playing_file_of_the_player():
    session = Mock()
    session.get.side_effect = [_response(EMPTY_PAGE), _response(NOW_PLAYING, "https://beufm.kz/1.txt")]
    tracks = beufm.get_tracks(session, "https://beufm.kz", "/")
    assert [(t["artist"], t["title"]) for t in tracks] == [("Abik Jeksen", "Сүйем мен")]
    assert session.get.call_args_list[1].args[0] == "https://beufm.kz/1.txt"


def test_track_id_is_stable_and_ignores_case_and_punctuation():
    assert beufm._track_id("Ellaya", "Среди Алматы") == beufm._track_id("ELLAYA", "среди  алматы!")
    assert beufm._track_id("Ellaya", "Среди Алматы") != beufm._track_id("Ellaya", "Среди Алматы (Remix)")


def test_get_tracks_logs_jingle_and_failures(caplog):
    caplog.set_level(logging.INFO)
    session = Mock()
    session.get.side_effect = [_response(EMPTY_PAGE), _response("Beu FM", "https://beufm.kz/1.txt")]
    assert beufm.get_tracks(session, "https://beufm.kz", "/") == []
    session.get.side_effect = RuntimeError("отказ")
    assert beufm.get_tracks(session, "https://beufm.kz", "/") == []
    records = [r for r in caplog.records if r.site == "beufm_kz"]
    assert [r.levelno for r in records] == [logging.INFO, logging.ERROR]


def test_resolve_track_requires_exact_match_and_reachable_link(monkeypatch):
    sefon_results = [
        {"artist": "Ellaya", "title": "Среди Алматы (Remix)", "download_url": "https://sefon.pro/remix.mp3"},
        {"artist": "ellaya", "title": "среди алматы", "download_url": "https://sefon.pro/ok.mp3", "referer": "r"},
    ]
    _patch_sources(
        monkeypatch,
        beufm,
        lambda session, query: (sefon_results, "https://sefon.pro/search/?q=x"),
        lambda session, query: pytest.fail("Sefon уже дал совпадение"),
    )
    session = Mock()
    session.get.return_value.__enter__ = lambda self: Mock(status_code=200)
    session.get.return_value.__exit__ = lambda self, *args: None
    track = {"id": "fm_1", "artist": "Ellaya", "title": "Среди Алматы"}
    assert beufm.resolve_track(session, track) == {
        "download_url": "https://sefon.pro/ok.mp3",
        "referer": "r",
        "src": "https://sefon.pro/search/?q=x",
    }
    session.get.assert_called_once_with(
        "https://sefon.pro/ok.mp3", headers={"Referer": "r"}, stream=True, timeout=(5, 15)
    )


def test_resolve_track_falls_back_to_lmusic_and_reports_missing(monkeypatch, caplog):
    calls = []
    _patch_sources(
        monkeypatch,
        beufm,
        lambda session, query: (_ for _ in ()).throw(RuntimeError("503")),
        lambda session, query: calls.append(query) or ([], "https://lmusic.kz/search?q=x"),
    )
    assert beufm.resolve_track(Mock(), {"id": "fm_2", "artist": "Abik Jeksen", "title": "Сүйем мен"}) is None
    assert calls == ["Abik Jeksen Сүйем мен"]
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.site == "beufm_kz"]
    assert any("sefon.pro" in message for message in messages)
    assert any("совпадение с доступной ссылкой не найдено" in message for message in messages)


def test_resolve_track_skips_unreachable_link(monkeypatch):
    candidates = [{"artist": "A", "title": "B", "download_url": "https://x.test/a.mp3"}]
    _patch_sources(
        monkeypatch,
        beufm,
        lambda session, query: (candidates, "https://sefon.pro/search/?q=x"),
        lambda session, query: ([], "https://lmusic.kz/search?q=x"),
    )
    session = Mock()
    session.get.return_value.__enter__ = lambda self: Mock(status_code=404)
    session.get.return_value.__exit__ = lambda self, *args: None
    assert beufm.resolve_track(session, {"id": "fm_3", "artist": "A", "title": "B"}) is None


def test_get_tracks_adds_ready_air_recordings(monkeypatch):
    recorded = [{"id": "fm_x", "artist": "Сара Амангелді", "title": "Көздерің", "_recording": "/r/fm_x.mp3"}]
    monkeypatch.setattr(beufm, "_recorded_tracks", lambda skip_ids: recorded)
    session = Mock()
    session.get.return_value = _response(FILLED_PAGE)
    tracks = beufm.get_tracks(session, "https://beufm.kz", "/")
    assert [track["artist"] for track in tracks] == ["Ellaya", "Сара Амангелді"]


def test_recorded_tracks_skip_current_and_mark_path(monkeypatch, tmp_path):
    recorder = Mock()
    recorder.list_recordings.return_value = [
        {"artist": "Ellaya", "title": "Среди Алматы", "path": str(tmp_path / "a.mp3")},
        {"artist": "Сара Амангелді", "title": "Көздерің", "path": str(tmp_path / "b.mp3")},
    ]
    recorder.list_aired.return_value = []
    monkeypatch.undo()
    settings = {"dir": str(tmp_path), "aired_file": "aired.jsonl", "aired_lookback_days": 2}
    monkeypatch.setattr(beufm, "_recorder_settings", lambda: (settings, recorder))
    skip = {beufm._track_id("Ellaya", "Среди Алматы")}
    tracks = beufm._recorded_tracks(skip)
    assert [(t["artist"], t["_recording"]) for t in tracks] == [("Сара Амангелді", str(tmp_path / "b.mp3"))]
    assert tracks[0]["id"] == beufm._track_id("Сара Амангелді", "Көздерің")


def test_resolve_track_prefers_sources_then_air_recording(monkeypatch, tmp_path):
    recording = tmp_path / "fm.mp3"
    recording.write_bytes(b"ID3air")
    track = {"id": "fm_1", "artist": "Сара Амангелді", "title": "Көздерің", "_recording": str(recording)}
    found = {"download_url": "https://sefon.pro/ok.mp3", "referer": "r", "src": "s"}
    monkeypatch.setattr(beufm.audio_search, "find_audio", lambda *args: found)
    assert beufm.resolve_track(Mock(), track) is found
    monkeypatch.setattr(beufm.audio_search, "find_audio", lambda *args: None)
    resolved = beufm.resolve_track(Mock(), track)
    assert resolved["download_url"] == f"file://{recording}"
    with resolved["fetch"]() as response:
        assert b"".join(response.iter_content()) == b"ID3air"
    assert recording.exists()  # запись остаётся до очистки кэша рекордером
    assert beufm.resolve_track(Mock(), {"id": "fm_2", "artist": "A", "title": "B"}) is None


def test_recorded_tracks_add_aired_journal_without_duplicates(monkeypatch, tmp_path):
    recorder = Mock()
    recorder.list_recordings.return_value = [
        {"artist": "Сара Амангелді", "title": "Көздерің", "path": str(tmp_path / "b.mp3")},
    ]
    recorder.list_aired.return_value = [
        {"artist": "Сара Амангелді", "title": "Көздерің", "ts": 1},  # уже есть готовая запись
        {"artist": "FAVOR1TE", "title": "Qyzganshaq", "ts": 2},
        {"artist": "Ellaya", "title": "Среди Алматы", "ts": 3},  # звучит сейчас
    ]
    monkeypatch.undo()
    settings = {"dir": str(tmp_path), "aired_file": "aired.jsonl", "aired_lookback_days": 2}
    monkeypatch.setattr(beufm, "_recorder_settings", lambda: (settings, recorder))
    tracks = beufm._recorded_tracks({beufm._track_id("Ellaya", "Среди Алматы")})
    assert [(t["artist"], "_recording" in t) for t in tracks] == [("Сара Амангелді", True), ("FAVOR1TE", False)]
    since = recorder.list_aired.call_args.args[2]
    assert abs(since - (beufm.time.time() - 2 * 86400)) < 5
