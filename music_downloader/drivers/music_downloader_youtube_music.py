"""Последний источник `search_sources`: песни YouTube Music через системный `yt-dlp`.

Нужен для релизов, которых нет на MP3-сайтах (нишевые южноафриканские, турецкие,
бразильские, казахские треки). Поиск — `https://music.youtube.com/search?q=…#songs`
(только песни, не клипы); у первых `MAX_CANDIDATES` результатов читаются полные
метаданные, где YouTube Music отдаёт точные `artists` и `track` (каналы «… - Topic»),
поэтому сверка в `find_audio` такая же строгая, как у остальных источников.

Аудио — opus/m4a, поэтому кандидат несёт `fetch`: скачивание `bestaudio` и
перекодирование в MP3 через `ffmpeg` во временный каталог только для одного
выбранного трека. Ответ `fetch()` имитирует потоковый HTTP-ответ (status_code,
headers, iter_content), так что лимиты каталогов, очистка тегов и `filehash`
в движке работают без изменений. Нет `yt-dlp` или `ffmpeg` — источник молча пуст.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urljoin

logger = logging.getLogger("Engine")

MAX_CANDIDATES = 3
SEARCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 600
# VBR ~190 кбит/с: исходник opus ~130–160 кбит/с, больший битрейт MP3 качества не добавит.
MP3_QUALITY = "2"


def available():
    return shutil.which("yt-dlp") is not None and shutil.which("ffmpeg") is not None


def _run_json(args, timeout):
    result = subprocess.run(
        ["yt-dlp", "--no-warnings", "--ignore-config", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp завершился с кодом {result.returncode}: {result.stderr.strip()[-200:]}")
    return json.loads(result.stdout)


class FileResponse:
    """Готовый MP3 на диске в виде потокового ответа; временный каталог удаляется при закрытии."""

    status_code = 200

    def __init__(self, path, workdir):
        self._path, self._workdir = path, workdir
        self.headers = {"content-length": str(os.path.getsize(path)), "content-type": "audio/mpeg"}

    def iter_content(self, chunk_size=131072):
        with open(self._path, "rb") as source:
            while chunk := source.read(chunk_size):
                yield chunk

    def close(self):
        shutil.rmtree(self._workdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def download_mp3(url):
    """Скачивает `bestaudio` и перекодирует в MP3; возвращает `FileResponse`."""
    workdir = tempfile.mkdtemp(prefix="music_downloader_ytm_")
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--no-warnings", "--ignore-config", "--no-playlist", "--quiet",
                "-f", "bestaudio", "-x", "--audio-format", "mp3", "--audio-quality", MP3_QUALITY,
                "-o", os.path.join(workdir, "track.%(ext)s"), url,
            ],
            capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT, check=False,
        )
        path = os.path.join(workdir, "track.mp3")
        if result.returncode != 0 or not os.path.isfile(path):
            raise RuntimeError(f"yt-dlp не скачал {url}: {result.stderr.strip()[-200:]}")
        return FileResponse(path, workdir)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def parse_info(info):
    """Кандидат из полных метаданных песни; без `track`/`artists` (обычное видео) — None."""
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    track = info.get("track")
    url = info.get("webpage_url") or info.get("original_url")
    if not artists or not track or not url:
        return None
    return {
        "artist": ", ".join(artists),
        "title": track,
        "download_url": url,
        "referer": "https://music.youtube.com/",
        "src": url,
        "fetch": lambda: download_mp3(url),
    }


def search_tracks(session, base_url, search_path):
    """Поиск песен YouTube Music; `session` не нужен — запросы делает `yt-dlp`."""
    if not available():
        return []
    search_url = urljoin(base_url, search_path)
    found = _run_json(["--flat-playlist", "--playlist-end", str(MAX_CANDIDATES), "-J", search_url], SEARCH_TIMEOUT)
    tracks = []
    for entry in found.get("entries") or []:
        url = entry.get("url")
        if not url:
            continue
        try:
            candidate = parse_info(_run_json(["--skip-download", "-J", url], SEARCH_TIMEOUT))
        except Exception as error:
            logger.warning(f"YouTube Music: не удалось прочитать {url}: {error}")
            continue
        if candidate is not None:
            tracks.append(candidate)
    return tracks
