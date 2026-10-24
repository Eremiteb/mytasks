import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("beufm_recorder", ROOT / "beufm_recorder.py")
assert SPEC is not None and SPEC.loader is not None
recorder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recorder)

# Кадр MPEG-1 Layer III, 192 кбит/с, 48 кГц, без padding: 144*192000/48000 = 576 байт.
FRAME = bytes([0xFF, 0xFB, 0xB4, 0x00]) + bytes(572)


def settings(tmp_path, **overrides):
    return recorder.load_settings({"sites": [{"site_name": "beufm_kz", "recorder": {"dir": str(tmp_path), **overrides}}]})


def test_load_settings_defaults_and_validation(tmp_path):
    loaded = settings(tmp_path)
    assert loaded["takes"] == 3 and loaded["min_takes"] == 2 and loaded["dir"] == str(tmp_path)
    with pytest.raises(ValueError):
        settings(tmp_path, min_takes=1)
    with pytest.raises(ValueError):
        settings(tmp_path, takes=0)
    with pytest.raises(ValueError):
        recorder.load_settings({"sites": []})


def test_whole_frames_drops_garbage_and_partial_last_frame():
    assert recorder.frame_length(FRAME, 0) == 576
    data = b"\x00\xff\xfb" + FRAME * 3 + FRAME[:100]
    assert recorder.whole_frames(data) == FRAME * 3
    # одиночное «синхрослово» без следующих кадров не принимается за начало
    assert recorder.whole_frames(b"\xff\xfb\xb4\x00junk" + FRAME * 3) == FRAME * 3


def test_air_buffer_slices_by_time_and_detects_gaps():
    buffer = recorder.AirBuffer(bytes_per_second=576, keep_seconds=100)
    buffer.connect(1000.0)
    for _ in range(10):
        buffer.append(FRAME)  # по одной секунде эфира
    assert buffer.slice(1002.0, 1005.0) == FRAME * 3
    assert buffer.covers(1001.0, 1009.0)
    assert not buffer.covers(999.0, 1005.0)  # до подключения
    buffer.connect(1020.0)
    assert not buffer.covers(1001.0, 1021.0)  # разрыв потока


def test_tracker_reports_only_songs_with_observed_start():
    tracker = recorder.TrackTracker()
    assert tracker.update(("A", "1"), None) is None  # начало первой песни не видно
    assert tracker.update(("B", "2"), 100.0) is None  # «A — 1» записана не с начала
    assert tracker.update(("B", "2"), 150.0) is None
    assert tracker.update(None, 300.0) == ("B", "2", 100.0, 300.0)  # джингл после песни
    assert tracker.update(("C", "3"), 330.0) is None  # джингл не песня


def test_longest_run_bridges_short_gaps():
    mask = [True] * 5 + [False] * 2 + [True] * 5 + [False] * 10 + [True] * 3
    assert recorder.longest_run(mask, max_gap=2) == (0, 11)
    assert recorder.longest_run(mask, max_gap=1) == (0, 4)
    assert recorder.longest_run([False, False], max_gap=1) is None


def _song(seed, length):
    """Псевдослучайный «отпечаток» песни: разные seed — несвязанные записи (~0.5 различий)."""
    value, items = seed, []
    for _ in range(length):
        value = (value * 1103515245 + 12345) & 0xFFFFFFFF
        items.append(value)
    return items


def test_choose_take_finds_common_part_and_best_take(tmp_path):
    step = recorder.FINGERPRINT_STEP
    song = _song(1, int(200 / step))
    takes = [
        _song(2, int(40 / step)) + song + _song(3, 80),
        _song(4, int(10 / step)) + song + _song(5, 80),
        _song(6, int(55 / step)) + [x ^ 0x1 for x in song] + _song(7, 80),  # чуть «шумнее»
    ]
    index, start, end, quality = recorder.choose_take(takes, settings(tmp_path))
    assert index in (0, 1)
    lead = (40, 10)[index]
    assert abs(start - lead) < 1.0 and abs(end - start - 200) < 1.5
    assert quality < 0.05
    # дубли разных песен не сходятся
    assert recorder.choose_take([_song(8, 3000), _song(9, 3000)], settings(tmp_path)) is None


def _fake_tools(monkeypatch, fingerprints, errors=None):
    by_path = {}
    monkeypatch.setattr(recorder, "fingerprint", lambda path: by_path[path])
    monkeypatch.setattr(recorder, "decode_errors", lambda path: (errors or {}).get(Path(path).name, 0))
    return by_path


def _takes(tmp_path, count, first=0, artist="Сара Амангелді", title="Көздерің"):
    return [
        recorder.save_take(str(tmp_path), artist, title, 1000, 1240, FRAME * 1000, now=1000 + index)
        for index in range(first, first + count)
    ]


def test_finalize_waits_for_takes_then_writes_best(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    step = recorder.FINGERPRINT_STEP
    song = _song(1, int(200 / step))
    paths = _takes(tmp_path, 2)
    by_path = _fake_tools(monkeypatch, None)
    track_id = recorder.recording_id("Сара Амангелді", "Көздерің")
    assert recorder.finalize(str(tmp_path), track_id, cfg) == "waiting"  # нужно 3 дубля
    paths += _takes(tmp_path, 1, first=2)
    for number, path in enumerate(paths):
        by_path[path] = _song(10 + number, int(5 / step)) + song + _song(20 + number, 80)
    assert recorder.finalize(str(tmp_path), track_id, cfg) == "ready"
    meta = json.loads((tmp_path / f"{track_id}.json").read_text(encoding="utf-8"))
    assert meta["takes"] == 3 and abs(meta["duration"] - 200) < 1.5
    assert not (tmp_path / track_id).exists()  # дубли удалены
    assert [item["title"] for item in recorder.list_recordings(str(tmp_path))] == ["Көздерің"]


def test_finalize_retries_when_takes_disagree(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    paths = _takes(tmp_path, 3)
    by_path = _fake_tools(monkeypatch, None)
    for number, path in enumerate(paths):
        by_path[path] = _song(30 + number, 2500)  # три разных звука
    track_id = recorder.recording_id("Сара Амангелді", "Көздерің")
    assert recorder.finalize(str(tmp_path), track_id, cfg) == "retry"
    assert len(recorder.list_takes(str(tmp_path / track_id))) == 2  # старый дубль удалён — ждём следующий выход
    assert not (tmp_path / f"{track_id}.mp3").exists()


def test_finalize_drops_broken_takes(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    paths = _takes(tmp_path, 3)
    by_path = _fake_tools(monkeypatch, None, errors={Path(paths[0]).name: 2, Path(paths[1]).name: 1})
    for path in paths:
        by_path[path] = _song(1, 2000)
    track_id = recorder.recording_id("Сара Амангелді", "Көздерің")
    assert recorder.finalize(str(tmp_path), track_id, cfg) == "retry"
    assert [Path(p).name for p, _ in recorder.list_takes(str(tmp_path / track_id))] == [Path(paths[2]).name]


def test_finalize_overdue_uses_two_takes(tmp_path, monkeypatch):
    cfg = settings(tmp_path, max_wait_days=1)
    paths = _takes(tmp_path, 2)
    by_path = _fake_tools(monkeypatch, None)
    step = recorder.FINGERPRINT_STEP
    song = _song(1, int(150 / step))
    for number, path in enumerate(paths):
        by_path[path] = _song(40 + number, 50) + song + _song(50 + number, 80)
    recorder.finalize_overdue(str(tmp_path), cfg, now=1000 + 3 * 86400)
    assert len(recorder.list_recordings(str(tmp_path))) == 1


def test_cleanup_by_age_and_size(tmp_path):
    for name, age_days in (("old", 30), ("new", 1)):
        (tmp_path / f"{name}.mp3").write_bytes(b"x" * 1024)
        (tmp_path / f"{name}.json").write_text("{}", encoding="utf-8")
        mtime = 2_000_000_000 - age_days * 86400
        os.utime(tmp_path / f"{name}.mp3", (mtime, mtime))
    removed = recorder.cleanup(str(tmp_path), keep_days=14, max_mb=10, now=2_000_000_000)
    assert [Path(path).name for path in removed] == ["old.mp3"]
    assert not (tmp_path / "old.json").exists() and (tmp_path / "new.mp3").exists()


def test_in_library_uses_engine_keys(tmp_path):
    db = tmp_path / "db.sqlite"
    engine = recorder._engine()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE downloads (artist TEXT, title TEXT)")
        conn.execute("INSERT INTO downloads VALUES (?, ?)",
                     (engine.normalize_db_text("Madi Syzdykov"), engine.normalize_db_text("Ai Omir ai")))
    assert recorder.in_library("madi  syzdykov", "Ai omir-ai", db_path=str(db))
    assert not recorder.in_library("Madi Syzdykov", "Другая", db_path=str(db))
    assert not recorder.in_library("A", "B", db_path=str(tmp_path / "нет.sqlite"))


def test_handle_finished_skips_library_and_gaps(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    buffer = Mock()
    monkeypatch.setattr(recorder, "in_library", lambda artist, title: artist == "В библиотеке")
    assert recorder.handle_finished(cfg, buffer, "В библиотеке", "X", 1000, 1200) == "в библиотеке"
    buffer.covers.return_value = False
    assert recorder.handle_finished(cfg, buffer, "A", "B", 1000, 1200) .startswith("нет эфира")
    buffer.covers.return_value = True
    assert recorder.handle_finished(cfg, buffer, "A", "B", 1000, 1010) == "длительность вне пределов"


def test_changed_at_from_headers_corrects_server_clock():
    headers = {"Last-Modified": "Sat, 24 Oct 2026 18:17:01 GMT", "Date": "Sat, 24 Oct 2026 18:17:11 GMT"}
    server_date = recorder.email.utils.parsedate_to_datetime(headers["Date"]).timestamp()
    # местные часы на 3 с впереди сервера
    assert recorder.changed_at_from_headers(headers, server_date + 3) == server_date - 10 + 3
    assert recorder.changed_at_from_headers({}, 0) is None


def test_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    cfg = settings(tmp_path / "rec")
    response = Mock()
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *args: None
    response.headers = {"Content-Type": "audio/mpeg"}
    response.read.return_value = FRAME * 20
    monkeypatch.setattr(recorder.urllib.request, "urlopen", lambda *args, **kwargs: response)
    monkeypatch.setattr(recorder, "read_now_playing", lambda url: (("Duman", "Бiр, eкі, үш"), None))
    monkeypatch.setattr(recorder.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert recorder.dry_run(cfg) == 0
    output = capsys.readouterr().out
    assert "[dry-run] будет:" in output and "Duman - Бiр, eкі, үш" in output
    assert not (tmp_path / "rec").exists()


def test_log_switches_to_new_day_file(tmp_path, monkeypatch):
    days = iter(["2026-10-24", "2026-10-25"])
    current = {"day": next(days)}

    class FakeDatetime(recorder.datetime):
        @classmethod
        def now(cls, tz=None):
            return recorder.datetime.fromisoformat(current["day"] + "T23:59:00")

    monkeypatch.setattr(recorder, "datetime", FakeDatetime)
    monkeypatch.setattr(recorder.logger, "handlers", [])
    recorder.setup_logging(str(tmp_path), max_files=10)
    recorder.logger.info("до полуночи")
    current["day"] = next(days)
    recorder.logger.info("после полуночи")
    for handler in recorder.logger.handlers:
        handler.close()
    assert "до полуночи" in (tmp_path / "beufm_recorder-2026-10-24.jsonl").read_text(encoding="utf-8")
    assert "после полуночи" in (tmp_path / "beufm_recorder-2026-10-25.jsonl").read_text(encoding="utf-8")
