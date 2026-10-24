import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

DRIVERS = Path(__file__).resolve().parents[1] / "drivers"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, DRIVERS / f"music_downloader_{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hitmotop = _load("hitmotop")
muzofond = _load("muzofond")
drivemusic = _load("drivemusic")

PAGE = "https://ru.hitmoz.org/search?q=Stromae"

HITMOTOP = """
<li class="tracks__item track" data-musid="track-id-1"
 data-musmeta='{"artist":"Stromae","title":"Alors on Danse","url":"https:\\/\\/ru.hitmoz.org\\/get\\/cuts\\/1\\/a.mp3","id":"track-id-1"}'>
</li>
<li class="tracks__item track" data-musmeta='не json'></li>
<li class="tracks__item track" data-musmeta='{"artist":"Без ссылки","title":"X"}'></li>
"""

MUZOFOND = """
<li class="item" data-id="2618664"><div class="actions"><ul>
<li class="play" data-id="2618664" data-url="https://dl3s4.muzofond.fm/aHR0cA=="></li></ul></div>
<div class="desc"><div class="trackTitle"><a href="/a"><span class="artist">Stromae</span></a> —
<a href="/track/1"><span class="track">Alors on Danse</span></a></div></div></li>
<li class="item"><div class="trackTitle"><span class="artist">Нет кнопки</span><span class="track">X</span></div></li>
"""

DRIVEMUSIC = """
<div class="music-popular__item"><div class="popular-play"><div class="btn_player">
<button data-url="https://cdn5.drivemusic.me/dl/online/t/1/stromae.mp3" class="popular-play__item"></button></div>
<div class="popular-play-name"><a href="/pop_music/32507-stromae.html" class="popular-play-author">Alors On Danse</a>
<div class='popular-play-composition'><a href="/artist/stromae.html">Stromae</a></div></div></div></div>
<div class="music-popular__item"><a class="popular-play-author">Без кнопки</a></div>
"""


def test_hitmotop_reads_musmeta_json_and_skips_broken_cards():
    assert hitmotop.parse_search(HITMOTOP, PAGE) == [{
        "artist": "Stromae",
        "title": "Alors on Danse",
        "download_url": "https://ru.hitmoz.org/get/cuts/1/a.mp3",
        "referer": PAGE,
    }]


def test_muzofond_reads_play_button_artist_and_track():
    assert muzofond.parse_search(MUZOFOND, "https://muzofond.fm/music/stromae") == [{
        "artist": "Stromae",
        "title": "Alors on Danse",
        "download_url": "https://dl3s4.muzofond.fm/aHR0cA==",
        "referer": "https://muzofond.fm/music/stromae",
    }]


def test_drivemusic_reads_button_title_and_artist():
    assert drivemusic.parse_search(DRIVEMUSIC, "https://drivemusic.me/?do=search") == [{
        "artist": "Stromae",
        "title": "Alors On Danse",
        "download_url": "https://cdn5.drivemusic.me/dl/online/t/1/stromae.mp3",
        "referer": "https://drivemusic.me/?do=search",
        "src": "https://drivemusic.me/pop_music/32507-stromae.html",
    }]


def test_search_tracks_uses_final_url_after_redirect_as_referer():
    session = Mock()
    session.get.return_value = Mock(text=HITMOTOP, url=PAGE)
    tracks = hitmotop.search_tracks(session, "https://rus.hitmotop.com", "/search?q=Stromae")
    session.get.assert_called_once_with("https://rus.hitmotop.com/search?q=Stromae", timeout=(5, 20))
    assert tracks[0]["referer"] == PAGE


def test_muzofond_treats_404_as_empty_result():
    session = Mock()
    session.get.return_value = Mock(status_code=404, text="", url="https://muzofond.fm/music/x")
    assert muzofond.search_tracks(session, "https://muzofond.fm", "/search/x") == []


youtube_music = _load("youtube_music")

SONG_INFO = {
    "track": "Setembro",
    "artists": ["Cynthia Luz", "2B", "OGBEATZZ"],
    "webpage_url": "https://music.youtube.com/watch?v=M9DAsczLQtc",
}


def test_youtube_music_parse_info_requires_song_metadata():
    candidate = youtube_music.parse_info(SONG_INFO)
    assert {key: candidate[key] for key in ("artist", "title", "download_url")} == {
        "artist": "Cynthia Luz, 2B, OGBEATZZ",
        "title": "Setembro",
        "download_url": "https://music.youtube.com/watch?v=M9DAsczLQtc",
    }
    assert callable(candidate["fetch"])
    # обычное видео без полей песни не принимается
    assert youtube_music.parse_info({"title": "Setembro (clipe)", "webpage_url": "https://youtube.com/x"}) is None


def test_youtube_music_search_reads_full_metadata_of_top_results(monkeypatch):
    monkeypatch.setattr(youtube_music, "available", lambda: True)
    calls = []

    def run_json(args, timeout):
        calls.append(args)
        if "--flat-playlist" in args:
            return {"entries": [{"url": "https://music.youtube.com/watch?v=a"}, {"url": None}]}
        return SONG_INFO

    monkeypatch.setattr(youtube_music, "_run_json", run_json)
    tracks = youtube_music.search_tracks(None, "https://music.youtube.com", "/search?q=Cynthia+Luz#songs")
    assert [track["title"] for track in tracks] == ["Setembro"]
    assert calls[0][-1] == "https://music.youtube.com/search?q=Cynthia+Luz#songs"
    assert calls[1] == ["--skip-download", "-J", "https://music.youtube.com/watch?v=a"]


def test_youtube_music_is_empty_without_tools(monkeypatch):
    monkeypatch.setattr(youtube_music, "available", lambda: False)
    monkeypatch.setattr(youtube_music, "_run_json", lambda *args: pytest.fail("yt-dlp не должен вызываться"))
    assert youtube_music.search_tracks(None, "https://music.youtube.com", "/search?q=x#songs") == []


def test_youtube_music_file_response_streams_and_removes_workdir(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "track.mp3").write_bytes(b"ID3data")
    remove = lambda: __import__("shutil").rmtree(workdir)  # noqa: E731
    with youtube_music.audio_search.FileResponse(str(workdir / "track.mp3"), on_close=remove) as response:
        assert response.status_code == 200
        assert response.headers["content-length"] == "7"
        assert b"".join(response.iter_content(3)) == b"ID3data"
    assert not workdir.exists()
