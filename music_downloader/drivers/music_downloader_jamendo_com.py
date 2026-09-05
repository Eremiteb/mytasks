import json
import logging
import os
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.LoggerAdapter(logging.getLogger("Engine"), {"site": "jamendo_com"})

_TRACK_ID_RE = re.compile(r"\b(\d{6,})\b")
_MP3_URL_TEMPLATE = "https://mp3d.jamendo.com/?trackid={track_id}&format=mp32"
_API_FILE_TEMPLATE = "https://api.jamendo.com/v3.0/tracks/file?client_id={client_id}&id={track_id}"
_API_PLAYLIST_TRACKS = "https://api.jamendo.com/v3.0/playlists/tracks/"
_API_TRACKS = "https://api.jamendo.com/v3.0/tracks/"
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "music_downloader.json"
)
_CONFIG_CLIENT_ID = None
_CONFIG_CLIENT_ID_LOADED = False


def _coerce_track_id(value):
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        m = _TRACK_ID_RE.search(value)
        return m.group(1) if m else None
    return None


def _extract_track_from_entry(entry):
    if not isinstance(entry, dict):
        return None

    track_id = (
        _coerce_track_id(entry.get("track_id"))
        or _coerce_track_id(entry.get("trackId"))
        or _coerce_track_id(entry.get("id"))
        or _coerce_track_id(entry.get("identifier"))
    )

    if not track_id:
        return None

    artist = None
    artist_obj = entry.get("artist") or entry.get("byArtist")
    if isinstance(artist_obj, dict):
        artist = artist_obj.get("name")
    if not artist:
        artist = entry.get("artist_name") or entry.get("artistName")

    title = entry.get("name") or entry.get("title") or entry.get("track_name")

    return {
        "id": track_id,
        "artist": artist,
        "title": title,
    }


def _collect_tracks_from_json(obj, out):
    if isinstance(obj, dict):
        item = _extract_track_from_entry(obj)
        if item:
            out.append(item)

        for key, value in obj.items():
            if key in ("track", "tracks", "trackList", "itemListElement"):
                _collect_tracks_from_json(value, out)
            else:
                _collect_tracks_from_json(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _collect_tracks_from_json(value, out)


def _extract_tracks_from_scripts(soup):
    tracks = []

    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue

        if script_type == "application/ld+json":
            try:
                data = json.loads(text)
            except Exception:
                continue
            _collect_tracks_from_json(data, tracks)
            continue

        if "__NEXT_DATA__" in text or "__NUXT__" in text or "__INITIAL_STATE__" in text:
            json_blob = None
            for pattern in (
                r"__NEXT_DATA__\s*=\s*(\{.*?\})\s*;\s*$",
                r"__NUXT__\s*=\s*(\{.*?\})\s*;\s*$",
                r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*$",
            ):
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    json_blob = m.group(1)
                    break
            if not json_blob:
                continue
            try:
                data = json.loads(json_blob)
            except Exception:
                continue
            _collect_tracks_from_json(data, tracks)

    return tracks


def _extract_tracks_from_dom(soup):
    tracks = []

    for tag in soup.find_all(attrs={"data-track-id": True}):
        track_id = _coerce_track_id(tag.get("data-track-id"))
        if not track_id:
            continue
        artist = tag.get("data-artist-name") or tag.get("data-artist")
        title = tag.get("data-track-name") or tag.get("data-title")
        tracks.append({
            "id": track_id,
            "artist": artist,
            "title": title,
        })

    for tag in soup.find_all(attrs={"data-trackid": True}):
        track_id = _coerce_track_id(tag.get("data-trackid"))
        if not track_id:
            continue
        artist = tag.get("data-artist-name") or tag.get("data-artist")
        title = tag.get("data-track-name") or tag.get("data-title")
        tracks.append({
            "id": track_id,
            "artist": artist,
            "title": title,
        })

    return tracks


def _dedupe_tracks(tracks):
    seen = set()
    unique = []
    for t in tracks:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        unique.append(t)
    return unique


def _get_client_id():
    env_client_id = os.getenv("JAMENDO_CLIENT_ID")
    if isinstance(env_client_id, str):
        env_client_id = env_client_id.strip()
    if env_client_id:
        return env_client_id

    global _CONFIG_CLIENT_ID, _CONFIG_CLIENT_ID_LOADED
    if _CONFIG_CLIENT_ID_LOADED:
        return _CONFIG_CLIENT_ID

    _CONFIG_CLIENT_ID_LOADED = True
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None

    for site_cfg in cfg.get("sites", []):
        if site_cfg.get("site_name") != "jamendo_com":
            continue
        client_id = site_cfg.get("client_id")
        if isinstance(client_id, str):
            client_id = client_id.strip()
        if client_id:
            _CONFIG_CLIENT_ID = client_id
        break

    return _CONFIG_CLIENT_ID


def _build_download_url(track_id):
    client_id = _get_client_id()
    if client_id:
        return _API_FILE_TEMPLATE.format(client_id=client_id, track_id=track_id)
    return _MP3_URL_TEMPLATE.format(track_id=track_id)


def _extract_playlist_id(category_path):
    m = re.search(r"/playlist/(\d+)", category_path)
    return m.group(1) if m else None


def _fetch_tracks(session, client_id):
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": 200,
        "order": "releasedate_desc",
    }
    try:
        resp = session.get(_API_TRACKS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"JAMENDO: / — не удалось получить JSON из tracks API: {e}")
        return []

    headers = data.get("headers") if isinstance(data, dict) else None
    if not isinstance(headers, dict):
        logger.error("JAMENDO: / — некорректный ответ tracks API: отсутствует объект headers")
        return []
    if headers.get("status") != "success":
        err = headers.get("error_message") or "неизвестная ошибка API"
        logger.error(f"JAMENDO: / — ошибка tracks API: {err}")
        return []
    if headers.get("warnings"):
        logger.warning(f"JAMENDO: / — предупреждение tracks API: {headers['warnings']}")

    results = data.get("results")
    if not isinstance(results, list):
        logger.error("JAMENDO: / — некорректный ответ tracks API: results не является списком")
        return []
    tracks = []
    for entry in results:
        # audiodownload_allowed — поле ответа, а не параметр фильтрации API.
        if not isinstance(entry, dict) or entry.get("audiodownload_allowed") is not True:
            continue
        download_url = entry.get("audiodownload")
        if not isinstance(download_url, str) or not download_url.strip():
            continue
        track = _extract_track_from_entry(entry)
        if track:
            track["download_url"] = download_url
            tracks.append(track)
    return _dedupe_tracks(tracks)


def _fetch_playlist_tracks(session, playlist_id, client_id):
    tracks = []
    offset = 0
    limit = 200

    while True:
        params = {
            "client_id": client_id,
            "id": playlist_id,
            "limit": limit,
            "offset": offset,
        }
        try:
            resp = session.get(_API_PLAYLIST_TRACKS, params=params, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"JAMENDO: плейлист {playlist_id} — ошибка запроса API: {e}")
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"JAMENDO: плейлист {playlist_id} — ответ API не является JSON: {e}")
            return []

        headers = data.get("headers", {}) if isinstance(data, dict) else {}
        if headers.get("status") != "success":
            err = headers.get("error_message") or "неизвестная ошибка API"
            logger.error(f"JAMENDO: плейлист {playlist_id} — ошибка API: {err}")
            return []
        if headers.get("warnings"):
            logger.warning(f"JAMENDO: плейлист {playlist_id} — предупреждение API: {headers['warnings']}")

        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            break

        for item in results:
            for t in item.get("tracks", []) or []:
                if t.get("audiodownload_allowed") is not True:
                    continue
                download_url = t.get("audiodownload")
                if not isinstance(download_url, str) or not download_url.strip():
                    continue
                track_id = _coerce_track_id(t.get("id") or t.get("track_id"))
                if not track_id:
                    continue
                artist = t.get("artist_name")
                if not artist and isinstance(t.get("artist"), dict):
                    artist = t.get("artist", {}).get("name")
                title = t.get("name") or t.get("title")

                tracks.append({
                    "id": track_id,
                    "artist": artist,
                    "title": title,
                    "download_url": download_url,
                })

        total = headers.get("results_count")
        if total and isinstance(total, int) and offset + limit >= total:
            break
        if len(results) < 1:
            break
        offset += limit

    return tracks


def get_tracks(session, base_url, category_path):
    playlist_url = urljoin(base_url, category_path)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": base_url,
    }

    playlist_id = _extract_playlist_id(category_path)
    client_id = _get_client_id()
    if not client_id:
        logger.error(
            "JAMENDO: отсутствует client_id. Задайте JAMENDO_CLIENT_ID или client_id для jamendo_com "
            "в music_downloader.json. Получить идентификатор: https://developer.jamendo.com/"
        )
        if category_path == "/":
            return []
    if client_id and (category_path == "/" or playlist_id):
        tracks = (
            _fetch_tracks(session, client_id)
            if category_path == "/"
            else _fetch_playlist_tracks(session, playlist_id, client_id)
        )
        for t in tracks:
            t["referer"] = playlist_url
        if tracks:
            logger.info(f"JAMENDO: {category_path} — получено треков через API: {len(tracks)}")
        else:
            logger.warning(f"JAMENDO: {category_path} — API не вернул доступных для скачивания треков")
        time.sleep(1.0)
        return tracks

    try:
        resp = session.get(playlist_url, timeout=30, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"JAMENDO: не удалось получить страницу {category_path}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    tracks = []
    tracks.extend(_extract_tracks_from_dom(soup))
    tracks.extend(_extract_tracks_from_scripts(soup))
    tracks = _dedupe_tracks(tracks)

    for t in tracks:
        t["download_url"] = _build_download_url(t["id"])
        t["referer"] = playlist_url

    if tracks:
        logger.info(f"JAMENDO: {category_path} — найдено треков в HTML: {len(tracks)}")
    else:
        logger.warning(f"JAMENDO: {category_path} — в HTML не найдено треков")
    time.sleep(1.0)
    return tracks
