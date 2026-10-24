import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audio_search", ROOT / "drivers" / "music_downloader_audio_search.py")
assert SPEC is not None and SPEC.loader is not None
audio_search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audio_search)


def _reachable_session(status=200):
    session = Mock()
    session.get.return_value.__enter__ = lambda self: Mock(status_code=status)
    session.get.return_value.__exit__ = lambda self, *args: None
    return session


def test_sources_come_from_config_in_order_and_skip_disabled(monkeypatch):
    monkeypatch.setattr(audio_search.time, "sleep", lambda _: None)
    sources = [
        {"name": "off", "enabled": False, "driver": "x", "base_url": "https://off.test", "search_path": "/?q={query}"},
        {"name": "first", "driver": "x", "base_url": "https://first.test", "search_path": "/s?q={query}"},
        {"name": "second", "driver": "x", "base_url": "https://second.test", "search_path": "/s?q={query}"},
    ]
    asked = []

    def query(session, source, text):
        asked.append(source["name"])
        if source["name"] == "first":
            return [], "https://first.test/s?q=x"
        return [{"artist": "A", "title": "B", "download_url": "https://second.test/b.mp3"}], "https://second.test/s?q=x"

    monkeypatch.setattr(audio_search, "_query_source", query)
    track = {"artist": "A", "title": "B", "_search_sources": sources}
    assert audio_search.find_audio(_reachable_session(), track, "Тест") == {
        "download_url": "https://second.test/b.mp3",
        "referer": "https://second.test/s?q=x",
        "src": "https://second.test/s?q=x",
    }
    assert asked == ["first", "second"]


def test_missing_config_falls_back_to_default_sources():
    assert [source["name"] for source in audio_search.enabled_sources(None)] == ["sefon.pro", "lmusic_kz"]


def test_query_source_loads_driver_and_quotes_query(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(
        "def search_tracks(session, base_url, search_path):\n"
        "    return [{'artist': base_url, 'title': search_path}]\n",
        encoding="utf-8",
    )
    source = {"name": "t", "driver": str(driver), "base_url": "https://t.test", "search_path": "/find?q={query}"}
    candidates, url = audio_search._query_source(Mock(), source, "Кино & Цой")
    assert url == "https://t.test/find?q=%D0%9A%D0%B8%D0%BD%D0%BE+%26+%D0%A6%D0%BE%D0%B9"
    assert candidates == [{"artist": "https://t.test", "title": url.removeprefix("https://t.test")}]


@pytest.mark.parametrize("name", ["music_downloader.json.example", "music_downloader.json"])
def test_config_sources_point_to_drivers_with_search(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} отсутствует (рабочий конфиг локальный)")
    sources = json.loads(path.read_text(encoding="utf-8"))["search_sources"]
    assert sources
    for source in sources:
        assert "{query}" in source["search_path"]
        assert hasattr(audio_search._load_driver(source["driver"]), "search_tracks")
