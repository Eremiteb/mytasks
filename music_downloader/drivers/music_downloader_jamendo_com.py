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

# Значения по умолчанию; переопределяются блоком drivers.music_downloader_jamendo_com в music_downloader.json.
SETTINGS = {
    "api_retry_delays_seconds": (1, 3, 5),
    "api_timeout_seconds": 30,
    "api_page_limit": 200,
    "page_timeout_seconds": 30,
    "user_agent": "Mozilla/5.0",
    "final_pause_seconds": 1.0,
    # Только песни с вокалом: инструментальные треки и треки без пометки
    # musicinfo.vocalinstrumental отбрасываются (замер 2026-11-08: из 200 последних
    # релизов 126 инструментальных, 48 без пометки, 26 с вокалом).
    "vocal_only": True,
}
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
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        logger.warning(f"Не удалось прочитать client_id из {_CONFIG_PATH}: {error}")
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


def _is_rejected_instrumental(entry):
    """Трек без вокала, который нужно пропустить при включённом vocal_only.

    Решение принимается только по полю musicinfo.vocalinstrumental (его даёт
    include=musicinfo): всё, кроме «vocal», — инструментальная или неразмеченная
    запись, как и при серверном фильтре vocalinstrumental=vocal. Если musicinfo в
    ответе нет (эндпоинт его не вернул), трек не отбрасывается.
    """
    if not SETTINGS["vocal_only"] or not isinstance(entry, dict):
        return False
    musicinfo = entry.get("musicinfo")
    if not isinstance(musicinfo, dict):
        return False
    return musicinfo.get("vocalinstrumental") != "vocal"


def _vocal_params():
    if not SETTINGS["vocal_only"]:
        return {}
    return {"include": "musicinfo", "vocalinstrumental": "vocal"}


def _log_skipped_instrumental(label, count):
    if count:
        logger.info(f"JAMENDO: {label} — пропущено треков без вокала: {count}")


def _extract_playlist_id(category_path):
    m = re.search(r"/playlist/(\d+)", category_path)
    return m.group(1) if m else None


def _fetch_tracks(session, client_id):
    params = {
        "client_id": client_id,
        "format": "json",
        "limit": SETTINGS["api_page_limit"],
        "order": "releasedate_desc",
        **_vocal_params(),
    }
    last_error = None
    last_error_level = logging.ERROR
    attempt = 0
    for delay in (0, *SETTINGS["api_retry_delays_seconds"]):
        if delay:
            time.sleep(delay)
        attempt += 1
        try:
            resp = session.get(_API_TRACKS, params=params, timeout=SETTINGS["api_timeout_seconds"])
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            last_error = f"не удалось получить JSON из tracks API: {e}"
            last_error_level = logging.ERROR
            continue

        headers = data.get("headers") if isinstance(data, dict) else None
        if not isinstance(headers, dict):
            last_error = "некорректный ответ tracks API: отсутствует объект headers"
            last_error_level = logging.ERROR
            continue
        if headers.get("status") != "success":
            err = headers.get("error_message") or "неизвестная ошибка API"
            last_error = f"ошибка tracks API: {err}"
            last_error_level = logging.ERROR
            continue
        if headers.get("warnings"):
            logger.warning(f"JAMENDO: / — предупреждение tracks API: {headers['warnings']}")

        results = data.get("results")
        if not isinstance(results, list):
            last_error = "некорректный ответ tracks API: results не является списком"
            last_error_level = logging.ERROR
            continue
        if not results:
            last_error = "API вернул пустой список results"
            last_error_level = logging.WARNING
            continue

        tracks = []
        skipped_instrumental = 0
        for entry in results:
            if _is_rejected_instrumental(entry):
                skipped_instrumental += 1
                continue
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

        _log_skipped_instrumental("/", skipped_instrumental)
        deduped = _dedupe_tracks(tracks)
        if not deduped:
            last_error = "в ответе API нет треков с разрешённым audiodownload"
            last_error_level = logging.WARNING
            continue

        if attempt > 1:
            logger.info(f"JAMENDO: / — список получен после попытки {attempt}")
        return deduped

    if last_error_level == logging.ERROR:
        logger.error(f"JAMENDO: / — не удалось получить треки за {attempt} попыток ({last_error})")
    else:
        logger.warning(f"JAMENDO: / — не удалось получить треки за {attempt} попыток ({last_error})")
    return []


def _fetch_playlist_tracks(session, playlist_id, client_id):
    tracks = []
    offset = 0
    limit = SETTINGS["api_page_limit"]

    while True:
        params = {
            "client_id": client_id,
            "id": playlist_id,
            "limit": limit,
            "offset": offset,
            # Серверный фильтр vocalinstrumental для плейлистов не используется:
            # просим musicinfo и, если он пришёл, отсеиваем треки без вокала на месте.
            **({"include": "musicinfo"} if SETTINGS["vocal_only"] else {}),
        }
        results = None
        headers = {}
        last_error = None
        attempt = 0
        for delay in (0, *SETTINGS["api_retry_delays_seconds"]):
            if delay:
                time.sleep(delay)
            attempt += 1
            try:
                resp = session.get(_API_PLAYLIST_TRACKS, params=params, timeout=SETTINGS["api_timeout_seconds"])
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                last_error = f"ошибка запроса API: {e}"
                continue

            headers = data.get("headers", {}) if isinstance(data, dict) else {}
            if headers.get("status") != "success":
                err = headers.get("error_message") or "неизвестная ошибка API"
                last_error = f"ошибка API: {err}"
                continue
            if headers.get("warnings"):
                logger.warning(f"JAMENDO: плейлист {playlist_id} — предупреждение API: {headers['warnings']}")

            raw_results = data.get("results")
            if isinstance(raw_results, list):
                results = raw_results
                break
            last_error = "results не является списком"

        if results is None:
            logger.error(
                f"JAMENDO: плейлист {playlist_id} — не удалось получить страницу (offset {offset}) "
                f"за {attempt} попыток: {last_error}"
            )
            return []

        if not results:
            break

        skipped_instrumental = 0
        for item in results:
            for t in item.get("tracks", []) or []:
                if _is_rejected_instrumental(t):
                    skipped_instrumental += 1
                    continue
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

        _log_skipped_instrumental(f"плейлист {playlist_id}", skipped_instrumental)
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
        "User-Agent": SETTINGS["user_agent"],
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
        time.sleep(SETTINGS["final_pause_seconds"])
        return tracks

    try:
        resp = session.get(playlist_url, timeout=SETTINGS["page_timeout_seconds"], headers=headers)
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
    time.sleep(SETTINGS["final_pause_seconds"])
    return tracks
