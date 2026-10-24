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


def test_query_path_placeholder_quotes_query_for_url_path(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text("def search_tracks(session, base_url, search_path):\n    return []\n", encoding="utf-8")
    source = {"name": "t", "driver": str(driver), "base_url": "https://t.test", "search_path": "/search/{query_path}"}
    assert audio_search._query_source(Mock(), source, "AC/DC Цой")[1] == "https://t.test/search/AC%2FDC%20%D0%A6%D0%BE%D0%B9"


@pytest.mark.parametrize("name",["music_downloader.json.example", "music_downloader.json"])
def test_config_sources_point_to_drivers_with_search(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} отсутствует (рабочий конфиг локальный)")
    sources = json.loads(path.read_text(encoding="utf-8"))["search_sources"]
    assert sources
    for source in sources:
        assert "{query}" in source["search_path"] or "{query_path}" in source["search_path"]
        assert hasattr(audio_search._load_driver(source["driver"]), "search_tracks")


@pytest.mark.parametrize(
    ("artist", "title", "candidate_artist", "candidate_title", "expected"),
    [
        ("Blaqbonez", "IKEBE 3000 (feat. Asake)", "Blaqbonez & Asake", "IKEBE 3000", True),
        ("Keane", "Somewhere Only We Know (Remastered 2024)", "Keane", "Somewhere Only We Know", True),
        ("Scorpions", "Still Loving You (2015 - Remaster)", "Scorpions", "Still Loving You", True),
        ("JAMALA, ONUKA", "Людина", "Jamala", "Людина", True),
        ("Tame Impala", "Dracula (with JENNIE)", "Tame Impala", "Dracula", False),
        ("Tame Impala", "Dracula (with JENNIE)", "Tame Impala, JENNIE", "Dracula", True),
        ("Tame Impala", "Dracula (with JENNIE)", "Tame Impala", "Dracula (feat. JENNIE)", True),
        ("Tame Impala", "Dracula", "Tame Impala", "Dracula (with JENNIE)", False),
        ("Tame Impala", "Dracula (with JENNIE)", "Tame Impala, Boys Noize", "Dracula", False),
        ("Lykke Li", "I Follow Rivers (The Magician Remix)", "Lykke Li", "I Follow Rivers", False),
        ("Stromae", "Alors on danse", "Другой", "Alors on danse", False),
        ("JAŸ-Z", "Empire State Of Mind (feat. Alicia Keys)", "JAY-Z", "Empire State Of Mind", True),
    ],
)
def test_relaxed_match_ignores_feat_and_remaster_only(artist, title, candidate_artist, candidate_title, expected):
    assert audio_search.relaxed_match(artist, title, candidate_artist, candidate_title) is expected


def test_search_queries_add_first_artist_and_clean_title():
    assert audio_search.search_queries("Akon & Snoop Dogg", "I Wanna Love You (feat. Snoop Dogg)") == [
        "Akon & Snoop Dogg I Wanna Love You (feat. Snoop Dogg)",
        "Akon I Wanna Love You",
    ]
    assert audio_search.search_queries("Stromae", "Alors on danse") == ["Stromae Alors on danse"]
    assert audio_search.search_queries("JAŸ-Z", "Encore")[-1] == "JAY-Z Encore"
    assert audio_search.search_queries("Abik Jeksen", "Сүйем мён") == ["Abik Jeksen Сүйем мён"]
    assert audio_search.match_key("JAŸ-Z") != audio_search.match_key("JAY-Z")  # строгий проход не ослаблен


def test_strict_match_in_later_source_beats_relaxed_in_earlier(monkeypatch):
    monkeypatch.setattr(audio_search.time, "sleep", lambda _: None)
    sources = [
        {"name": "a", "driver": "x", "base_url": "https://a.test", "search_path": "/?q={query}"},
        {"name": "b", "driver": "x", "base_url": "https://b.test", "search_path": "/?q={query}"},
    ]
    found = {
        "a": [{"artist": "Keane", "title": "Somewhere Only We Know", "download_url": "https://a.test/1.mp3"}],
        "b": [{"artist": "Keane", "title": "Somewhere Only We Know (Remastered 2024)",
               "download_url": "https://b.test/2.mp3"}],
    }
    monkeypatch.setattr(
        audio_search, "_query_source", lambda session, source, query: (found[source["name"]], f"{source['base_url']}/s")
    )
    track = {"artist": "Keane", "title": "Somewhere Only We Know (Remastered 2024)", "_search_sources": sources}
    assert audio_search.find_audio(_reachable_session(), track, "Тест")["download_url"] == "https://b.test/2.mp3"
    track["_search_sources"] = sources[:1]
    assert audio_search.find_audio(_reachable_session(), track, "Тест")["download_url"] == "https://a.test/1.mp3"


def test_fetch_candidate_skips_http_check_and_reaches_engine(monkeypatch):
    monkeypatch.setattr(audio_search.time, "sleep", lambda _: None)
    fetch = Mock()
    source = {"name": "ytm", "driver": "x", "base_url": "https://music.youtube.com", "search_path": "/search?q={query}"}
    monkeypatch.setattr(audio_search, "_query_source", lambda session, src, query: (
        [{"artist": "A", "title": "B", "download_url": "https://music.youtube.com/watch?v=1", "fetch": fetch}],
        "https://music.youtube.com/search?q=x",
    ))
    session = Mock()
    result = audio_search.find_audio(session, {"artist": "A", "title": "B", "_search_sources": [source]}, "Тест")
    assert result["fetch"] is fetch
    session.get.assert_not_called()
    fetch.assert_not_called()


MADI = [["Madi Syzdykov", "Мәди Сыздықов", "Мади Сыздыков", "Madi Syzdyq", "Madistation"]]


def test_fold_key_ignores_script_for_kazakh_names():
    assert audio_search.fold_key("Мәди Сыздықов") == audio_search.fold_key("Madi Syzdykov")
    assert audio_search.fold_key("аi ómir-ai") == audio_search.fold_key("Ai Omir ai")  # кириллическая «а»
    assert audio_search.fold_key("Қайрат") == audio_search.fold_key("Qairat")
    assert audio_search.match_key("Мәди Сыздықов") != audio_search.match_key("Madi Syzdykov")  # строгий не ослаблен


@pytest.mark.parametrize(
    ("latin", "cyrillic"),
    [
        ("Madi Syzdykov Ai Omir ai", "Мади Сыздыков Ай Омир ай"),
        ("WinL Keshire almadyn", "Винл Кешире алмадын"),
        ("Mona Songz onay kelmeydi", "Мона Сонгз онай келмейди"),
    ],
)
def test_to_cyrillic_for_search_queries(latin, cyrillic):
    assert audio_search.to_cyrillic(latin) == cyrillic


def test_to_latin_for_search_queries():
    assert audio_search.to_latin("Сара Амангелді Көздерің") == "Sara Amangeldi Kozderin"


def test_translit_and_alias_queries():
    assert audio_search.search_queries("Madi Syzdykov", "Ai Omir ai", MADI, translit=True) == [
        "Madi Syzdykov Ai Omir ai",
        "Мади Сыздыков Ай Омир ай",
        "Madi Syzdyq Ai Omir ai",
        "Madistation Ai Omir ai",
    ]
    assert audio_search.search_queries("Сара Амангелді", "Тыңда", translit=True)[-1] == "Sara Amangeldi Tynda"
    # без пометки translit и без синонимов лишних запросов нет
    assert audio_search.search_queries("Madi Syzdykov", "Ai Omir ai") == ["Madi Syzdykov Ai Omir ai"]


def test_relaxed_match_uses_artist_aliases_only_from_config():
    assert audio_search.relaxed_match("Madi Syzdykov", "Ai Omir ai", "Madistation", "аi ómir-ai", MADI)
    assert not audio_search.relaxed_match("Madi Syzdykov", "Ai Omir ai", "Madistation", "аi ómir-ai")
    # тот же текст в исполнении другого певца — другая запись
    assert not audio_search.relaxed_match(
        "Сара Амангелді", "Көздерің сенің сондай әп-әдемі", "Кайрат Нуртас, Argonya",
        "Көздерің Сенің Сондай Әп Әдемі (feat. Argonya)",
    )
