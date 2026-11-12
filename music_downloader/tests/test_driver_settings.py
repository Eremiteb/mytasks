import importlib.util
import json
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# Лог движка при импорте модуля — во временный каталог, а не в logs/ репозитория
os.environ.setdefault("MYTASKS_LOG_DIR", tempfile.mkdtemp(prefix="music_downloader_test_logs_"))

ROOT = Path(__file__).resolve().parents[1]
DRIVERS = ROOT / "drivers"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings_module = _load(DRIVERS / "music_downloader_driver_settings.py", "driver_settings_under_test")
apply_driver_settings = settings_module.apply_driver_settings
audio_search = _load(DRIVERS / "music_downloader_audio_search.py", "audio_search_settings_test")
engine = _load(ROOT / "music_downloader.py", "music_downloader_engine_settings")

log = logging.getLogger("driver_settings_test")


def _module(**settings):
    return SimpleNamespace(__name__="fake_driver", SETTINGS=settings)


def test_applies_values_of_the_same_type(caplog):
    module = _module(timeout_seconds=15, pause_seconds=1.0, user_agent="UA", enabled=True)
    apply_driver_settings(module, {"timeout_seconds": 30, "pause_seconds": 2, "user_agent": "Other", "enabled": False}, log)
    assert module.SETTINGS == {"timeout_seconds": 30, "pause_seconds": 2, "user_agent": "Other", "enabled": False}
    assert not caplog.records


def test_unknown_key_is_ignored_with_warning(caplog):
    module = _module(timeout_seconds=15)
    apply_driver_settings(module, {"timeout": 1, "timeout_seconds": 20}, log)
    assert module.SETTINGS == {"timeout_seconds": 20}
    assert any("неизвестный ключ 'timeout'" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("default", "value"),
    [(15, "30"), (15, True), (1.5, None), ("UA", 5), (True, 1), ([1, 2], "1,2"), ({"a": "x"}, [1])],
)
def test_wrong_type_keeps_default_with_warning(default, value, caplog):
    module = _module(key=default)
    apply_driver_settings(module, {"key": value}, log)
    assert module.SETTINGS["key"] == default
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_negative_number_is_rejected(caplog):
    module = _module(timeout_seconds=15, pause_range_seconds=[1.0, 2.0])
    apply_driver_settings(module, {"timeout_seconds": -1, "pause_range_seconds": [1.0, -2.0]}, log)
    assert module.SETTINGS == {"timeout_seconds": 15, "pause_range_seconds": [1.0, 2.0]}
    assert len([r for r in caplog.records if "отрицательным" in r.getMessage()]) == 2


def test_int_and_float_are_interchangeable():
    module = _module(a=1, b=1.5)
    apply_driver_settings(module, {"a": 2.5, "b": 3}, log)
    assert module.SETTINGS == {"a": 2.5, "b": 3}


def test_list_default_checks_length_and_element_types(caplog):
    module = _module(pause_range_seconds=[0.5, 1.3])
    apply_driver_settings(module, {"pause_range_seconds": [1, 2, 3]}, log)
    apply_driver_settings(module, {"pause_range_seconds": [1, "2"]}, log)
    assert module.SETTINGS["pause_range_seconds"] == [0.5, 1.3]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    apply_driver_settings(module, {"pause_range_seconds": [2, 4]}, log)
    assert module.SETTINGS["pause_range_seconds"] == [2, 4]


def test_tuple_default_accepts_json_list_and_stays_tuple():
    module = _module(retry_delays_seconds=(15, 45, 90))
    apply_driver_settings(module, {"retry_delays_seconds": [1, 2, 3]}, log)
    assert module.SETTINGS["retry_delays_seconds"] == (1, 2, 3)
    assert isinstance(module.SETTINGS["retry_delays_seconds"], tuple)


def test_range_min_must_not_exceed_max():
    module = _module(pause_range_seconds=[30, 60])
    apply_driver_settings(module, {"pause_range_seconds": [60, 30]}, log)
    assert module.SETTINGS["pause_range_seconds"] == [30, 60]


def test_schedule_list_length_is_free_but_items_checked():
    module = _module(retry_delays_seconds=(15, 45, 90))
    apply_driver_settings(module, {"retry_delays_seconds": [5, 10, 20, 40]}, log)
    assert module.SETTINGS["retry_delays_seconds"] == (5, 10, 20, 40)
    apply_driver_settings(module, {"retry_delays_seconds": []}, log)
    assert module.SETTINGS["retry_delays_seconds"] == ()
    apply_driver_settings(module, {"retry_delays_seconds": [5, "x"]}, log)
    assert module.SETTINGS["retry_delays_seconds"] == ()


def test_dict_default_merges_known_keys(caplog):
    module = _module(chart_headers={"User-Agent": "old", "Accept": "x"})
    apply_driver_settings(module, {"chart_headers": {"User-Agent": "new"}}, log)
    assert module.SETTINGS["chart_headers"] == {"User-Agent": "new", "Accept": "x"}
    apply_driver_settings(module, {"chart_headers": {"Other": "y"}}, log)
    assert module.SETTINGS["chart_headers"] == {"User-Agent": "new", "Accept": "x"}
    assert caplog.records


def test_no_settings_attribute_or_empty_settings_do_nothing(caplog):
    apply_driver_settings(SimpleNamespace(__name__="plain"), {"a": 1}, log)
    module = _module(a=1)
    apply_driver_settings(module, {}, log)
    apply_driver_settings(module, None, log)
    assert module.SETTINGS == {"a": 1}
    assert not caplog.records


def test_non_object_settings_are_ignored_with_warning(caplog):
    module = _module(a=1)
    apply_driver_settings(module, [1, 2], log)
    assert module.SETTINGS == {"a": 1}
    assert caplog.records


def test_engine_loads_settings_module_by_path_and_builds_key():
    assert engine.load_driver_settings_module().apply_driver_settings.__name__ == "apply_driver_settings"
    assert engine.driver_settings_key("drivers/music_downloader_shazam.py") == "music_downloader_shazam"


def _write_driver(path, timeout=5):
    path.write_text(
        f"SETTINGS = {{'timeout_seconds': {timeout}}}\n"
        "def search_tracks(session, base_url, search_path):\n"
        "    return [{'artist': 'A', 'title': 'B', 'download_url': 'https://d.test/b.mp3', 'timeout': SETTINGS['timeout_seconds']}]\n",
        encoding="utf-8",
    )


def test_audio_search_applies_driver_settings_once_on_load(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_search, "_DRIVERS", {})
    monkeypatch.setattr(audio_search, "_DRIVERS_DIR", str(tmp_path))
    driver = tmp_path / "music_downloader_fake.py"
    _write_driver(driver)
    source = {"name": "fake", "driver": str(driver), "base_url": "https://d.test", "search_path": "/?q={query}"}
    settings = {"music_downloader_fake": {"timeout_seconds": 42}}
    candidates, _ = audio_search._query_source(Mock(), source, "A B", settings)
    assert candidates[0]["timeout"] == 42
    # Второй вызов берёт модуль из кэша; настройки уже применены.
    candidates, _ = audio_search._query_source(Mock(), source, "A B", None)
    assert candidates[0]["timeout"] == 42


def test_find_audio_passes_engine_driver_settings_to_source_driver_and_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_search, "_DRIVERS", {})
    monkeypatch.setattr(audio_search, "_DRIVERS_DIR", str(tmp_path))
    monkeypatch.setattr(audio_search, "_OWN_SETTINGS_APPLIED", False)
    monkeypatch.setattr(audio_search, "SETTINGS", dict(audio_search.SETTINGS))
    pauses = []
    monkeypatch.setattr(audio_search.time, "sleep", pauses.append)
    driver = tmp_path / "music_downloader_fake.py"
    _write_driver(driver)
    loaded = {}
    real_query = audio_search._query_source

    def query(session, source, text, driver_settings=None):
        result = real_query(session, source, text, driver_settings)
        loaded["timeout"] = result[0][0]["timeout"]
        return result

    monkeypatch.setattr(audio_search, "_query_source", query)
    source = {"name": "fake", "driver": str(driver), "base_url": "https://d.test", "search_path": "/?q={query}"}
    session = Mock()
    session.get.return_value.__enter__ = lambda self: Mock(status_code=200)
    session.get.return_value.__exit__ = lambda self, *args: None
    track = {
        "artist": "A", "title": "B", "_search_sources": [source],
        "_driver_settings": {
            "music_downloader_fake": {"timeout_seconds": 7},
            "music_downloader_audio_search": {"query_pause_seconds": 3, "link_check_read_timeout_seconds": 9},
        },
    }
    assert audio_search.find_audio(session, track, "Тест")["download_url"] == "https://d.test/b.mp3"
    assert loaded["timeout"] == 7
    assert pauses == [3]
    assert session.get.call_args.kwargs["timeout"] == (5, 9)


def test_driver_uses_settings_in_http_timeout(monkeypatch):
    hitmotop = _load(DRIVERS / "music_downloader_hitmotop.py", "hitmotop_settings_test")
    apply_driver_settings(hitmotop, {"connect_timeout_seconds": 2, "read_timeout_seconds": 3}, log)
    session = Mock()
    session.get.return_value.text = "<html></html>"
    session.get.return_value.url = "https://h.test/search"
    hitmotop.search_tracks(session, "https://h.test", "/search?q=x")
    assert session.get.call_args.kwargs["timeout"] == (2, 3)


def test_driver_reads_settings_at_call_time_not_import(monkeypatch):
    sefon = _load(DRIVERS / "music_downloader_sefon_pro.py", "sefon_settings_test")
    session = Mock()
    session.get.return_value.text = "<html></html>"
    sefon.get_tracks(session, "https://s.test", "/news/")
    assert session.get.call_args.kwargs["timeout"] == 15
    monkeypatch.setitem(sefon.SETTINGS, "timeout_seconds", 99)
    sefon.get_tracks(session, "https://s.test", "/news/")
    assert session.get.call_args.kwargs["timeout"] == 99


def test_shazam_uses_retry_schedule_headers_and_timeout_from_settings(monkeypatch):
    shazam = _load(DRIVERS / "music_downloader_shazam.py", "shazam_settings_test")
    sleeps = []
    monkeypatch.setattr(shazam.time, "sleep", sleeps.append)
    apply_driver_settings(
        shazam,
        {
            "retry_delays_seconds": [1, 2, 3],
            "read_timeout_seconds": 8,
            "chart_headers": {"User-Agent": "TestUA"},
        },
        log,
    )
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.text = "<html></html>"
    assert shazam.get_tracks(session, "https://www.shazam.com", "/charts") == []
    assert sleeps == [1, 2, 3]
    kwargs = session.get.call_args.kwargs
    assert kwargs["timeout"] == (5, 8)
    assert kwargs["headers"]["User-Agent"] == "TestUA"
    assert kwargs["headers"]["Accept-Language"] == "en-US,en;q=0.5"


def test_youtube_music_uses_max_candidates_and_timeouts_from_settings(monkeypatch):
    youtube_music = _load(DRIVERS / "music_downloader_youtube_music.py", "ytm_settings_test")
    apply_driver_settings(youtube_music, {"max_candidates": 5, "search_timeout_seconds": 11}, log)
    monkeypatch.setattr(youtube_music, "available", lambda: True)
    calls = []

    def run_json(args, timeout):
        calls.append((args, timeout))
        return {"entries": []}

    monkeypatch.setattr(youtube_music, "_run_json", run_json)
    youtube_music.search_tracks(None, "https://music.youtube.com", "/search?q=x")
    args, timeout = calls[0]
    assert args[args.index("--playlist-end") + 1] == "5"
    assert timeout == 11


def test_every_driver_with_settings_is_documented_in_json_example():
    example = json.loads((ROOT / "music_downloader.json.example").read_text(encoding="utf-8"))["drivers"]
    for path in sorted(DRIVERS.glob("music_downloader_*.py")):
        if path.stem == "music_downloader_driver_settings":
            continue
        module = _load(path, f"documented_{path.stem}")
        assert path.stem in example, f"{path.stem} отсутствует в блоке drivers примера конфига"
        shipped = {key: (list(value) if isinstance(value, tuple) else value) for key, value in module.SETTINGS.items()}
        assert example[path.stem] == shipped, f"{path.stem}: пример конфига расходится с SETTINGS"
