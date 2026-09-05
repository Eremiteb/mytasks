import importlib.util
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "drivers" / "music_downloader_shazam.py"
SPEC = importlib.util.spec_from_file_location("shazam", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
shazam = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shazam)

CANONICAL = '<link rel="canonical" href="https://www.shazam.com/charts/top-200/world">'


def _card(rank, song_id, artist, title, artist_html=None):
    artist_html = artist_html if artist_html is not None else f'<a data-test-id="charts_userevent_list_artistName">{artist}</a>'
    return (
        f'<div data-test-id="songItem"><span class="SongItem-module_rankingNumber__x">{rank}</span>'
        f'<a data-test-id="charts_userevent_list_songTitle" href="/ru-ru/song/{song_id}/x"></a>'
        f'<a data-test-id="charts_userevent_list_songTitle" href="/ru-ru/song/{song_id}/x">{title}</a>'
        f"{artist_html}</div>"
    )


def _chart_html(count=200):
    cards = [_card(1, 1000, None, "Bad Times", artist_html='<template id="P:5"></template>')]
    cards += [_card(rank, 1000 + rank - 1, f"Artist {rank}", f"Title {rank}") for rank in range(2, count + 1)]
    hidden = '<div hidden id="S:5"><a data-test-id="charts_userevent_list_artistName">Imael &amp; Angel</a></div>'
    return CANONICAL + "".join(cards) + hidden + '<script>$RS("S:5","P:5")</script>'


def test_parse_chart_restores_inserts_and_returns_full_top_200():
    tracks = shazam.parse_chart(_chart_html())
    assert len(tracks) == 200
    assert tracks[0] == {
        "id": "adam_1000",
        "rank": 1,
        "artist": "Imael & Angel",
        "title": "Bad Times",
        "src": "https://www.shazam.com/ru-ru/song/1000/x",
    }
    assert [track["rank"] for track in tracks] == list(range(1, 201))
    assert all("download_url" not in track for track in tracks)


@pytest.mark.parametrize(
    "html",
    [
        '<link rel="canonical" href="https://www.shazam.com/song/1/x">' + _chart_html()[len(CANONICAL):],
        _chart_html(199),
        _chart_html().replace('href="/ru-ru/song/1199/x"', 'href="/ru-ru/song/1198/x"'),
        _chart_html().replace('<script>$RS("S:5","P:5")</script>', ""),
    ],
)
def test_parse_chart_rejects_foreign_or_incomplete_pages(html):
    with pytest.raises(ValueError):
        shazam.parse_chart(html)


def test_get_tracks_logs_and_returns_empty_on_bad_page(caplog):
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.text = "<html></html>"
    assert shazam.get_tracks(session, "https://www.shazam.com", "/ru-ru/charts/top-200/world") == []
    assert any(record.levelno == logging.ERROR and record.site == "shazam_com" for record in caplog.records)


def test_resolve_track_requires_exact_match_and_reachable_link(monkeypatch, caplog):
    monkeypatch.setattr(shazam.time, "sleep", lambda _: None)
    sefon_results = [
        {"artist": "Bruno Mars", "title": "I Just Might (Remix)", "download_url": "https://sefon.pro/remix.mp3"},
        {"artist": "bruno mars", "title": "I just might!", "download_url": "https://sefon.pro/ok.mp3", "referer": "r"},
    ]
    monkeypatch.setattr(shazam, "_search_sefon", lambda session, query: (sefon_results, "https://sefon.pro/search/?q=x"))
    monkeypatch.setattr(shazam, "_search_lmusic", lambda session, query: pytest.fail("Sefon уже дал совпадение"))
    session = Mock()
    session.get.return_value.__enter__ = lambda self: Mock(status_code=200)
    session.get.return_value.__exit__ = lambda self, *args: None
    track = {"id": "adam_1", "artist": "Bruno Mars", "title": "I Just Might"}
    assert shazam.resolve_track(session, track) == {
        "download_url": "https://sefon.pro/ok.mp3",
        "referer": "r",
        "src": "https://sefon.pro/search/?q=x",
    }
    session.get.assert_called_once_with(
        "https://sefon.pro/ok.mp3", headers={"Referer": "r"}, stream=True, timeout=(5, 15)
    )


def test_resolve_track_falls_back_to_lmusic_and_reports_missing(monkeypatch, caplog):
    monkeypatch.setattr(shazam.time, "sleep", lambda _: None)
    monkeypatch.setattr(shazam, "_search_sefon", lambda session, query: (_ for _ in ()).throw(RuntimeError("503")))
    calls = []
    monkeypatch.setattr(shazam, "_search_lmusic", lambda session, query: calls.append(query) or ([], "https://lmusic.kz/search?q=x"))
    assert shazam.resolve_track(Mock(), {"id": "adam_2", "artist": "Yung Miami", "title": "Spend Dat"}) is None
    assert calls == ["Yung Miami Spend Dat"]
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("sefon.pro" in message for message in messages)
    assert any("совпадение с доступной ссылкой не найдено" in message for message in messages)
