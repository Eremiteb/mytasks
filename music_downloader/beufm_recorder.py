#!/usr/bin/env python3
"""Запись эфира Beu FM для песен, которых нет в источниках MP3.

Служба `systemd/user/beufm-recorder.service` постоянно читает поток `stream_url`
(Icecast, MP3 192 кбит/с — байты сохраняются без перекодирования) в кольцевой
буфер и раз в `poll_seconds` опрашивает `now_playing_url` (`1.txt`, «Исполнитель -
Название»).

`1.txt` переписывается раз в минуту (в :01, замер 2026-10-24), поэтому смена
названия отстаёт от реального стыка песен на 0–60 с, и по нему одному песню
точно не вырезать. Каждый выход песни в эфир сохраняется как «дубль» с запасом:
`window_seconds` до смены названия на эту песню и `tail_seconds` после смены на
следующую. Когда набирается `takes` дублей (по умолчанию 3), дубли сравниваются
по акустическим отпечаткам (`fpcalc`, Chromaprint): песня — участок, общий для
всех дублей, а соседние треки от выхода к выходу разные. Из дублей выбирается
лучший (без ошибок декодирования, с наименьшим расхождением с остальными) и
обрезается по общему участку — это готовая запись `<id>.mp3` + `<id>.json`.
Если дубли не сходятся или запись плохая, худший дубль удаляется и песня
дописывается при следующем выходе. Песни, уже скачанные в библиотеку (есть в
`music_downloader.db`), не пишутся.

Готовые записи забирает загрузчик (`drivers/music_downloader_beufm_kz.py`):
сначала ищет песню в `search_sources` (качество лучше) и только если её нигде
нет, сохраняет запись эфира. Кэш чистится по `keep_days` и `max_mb`.

Настройки — блок `recorder` у сайта `beufm_kz` в `music_downloader.json`.
`--dry-run` проверяет настройки, поток, `1.txt` и инструменты и печатает план,
ничего не записывая.
"""
import argparse
import collections
import contextlib
import email.utils
import importlib.util
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "music_downloader.json")
DB_PATH = os.path.join(BASE_DIR, "music_downloader.db")
SCRIPT_NAME = "beufm_recorder"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
# Длительность одного элемента отпечатка Chromaprint: 4096/3 отсчёта при 11025 Гц.
FINGERPRINT_STEP = 4096 / 3 / 11025

DEFAULTS = {
    "enabled": True,
    "stream_url": "https://stream.beufm.kz/beufm",
    "now_playing_url": "https://beufm.kz/1.txt",
    # Корневой state/ игнорируется Git (music_downloader/state — нет).
    "dir": "../state/beufm_recordings",
    "bitrate_kbps": 192,
    "poll_seconds": 2,
    "window_seconds": 65,
    "tail_seconds": 10,
    # Элемент отпечатка Chromaprint смотрит на ~2 с вперёд, поэтому общий участок
    # кончается на ~1.3 с раньше песни (замер на имитации трёх выходов 2026-10-24).
    "pad_start_seconds": 0.3,
    "pad_end_seconds": 1.5,
    "takes": 3,
    "min_takes": 2,
    "max_wait_days": 7,
    "match_ber": 0.2,
    "min_seconds": 90,
    "max_seconds": 900,
    "keep_days": 14,
    "max_mb": 2048,
    "log_max_files": 10,
}

logger = logging.getLogger("BeuFMRecorder")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Разбор «Исполнитель - Название» и ID трека — те же, что у драйвера, иначе
# загрузчик не сопоставит запись с треком из эфира и с БД.
beufm = _load_module("music_downloader_beufm_kz", os.path.join(BASE_DIR, "drivers", "music_downloader_beufm_kz.py"))


def load_settings(config):
    """Блок `recorder` сайта `beufm_kz` поверх значений по умолчанию; путь `dir` — от каталога загрузчика."""
    site = next((s for s in config.get("sites", []) if s.get("site_name") == "beufm_kz"), None)
    if site is None:
        raise ValueError("В music_downloader.json нет сайта beufm_kz")
    settings = {**DEFAULTS, **(site.get("recorder") or {})}
    if not os.path.isabs(settings["dir"]):
        settings["dir"] = os.path.normpath(os.path.join(BASE_DIR, settings["dir"]))
    for key in ("bitrate_kbps", "poll_seconds", "window_seconds", "tail_seconds", "takes", "min_takes",
                "max_wait_days", "match_ber", "min_seconds", "max_seconds", "keep_days", "max_mb"):
        if not isinstance(settings[key], (int, float)) or isinstance(settings[key], bool) or settings[key] <= 0:
            raise ValueError(f"recorder.{key} должно быть положительным числом")
    if not 2 <= settings["min_takes"] <= settings["takes"]:
        raise ValueError("Нужно 2 ≤ recorder.min_takes ≤ recorder.takes: песня вырезается сравнением дублей")
    return settings


# --- MP3 и буфер эфира ---------------------------------------------------


def is_frame_header(data, index):
    """Заголовок кадра MPEG-аудио: синхрослово, допустимые версия, слой, битрейт и частота."""
    if index + 3 >= len(data) or data[index] != 0xFF or data[index + 1] & 0xE0 != 0xE0:
        return False
    version, layer = (data[index + 1] >> 3) & 0x03, (data[index + 1] >> 1) & 0x03
    bitrate, sample_rate = data[index + 2] >> 4, (data[index + 2] >> 2) & 0x03
    return version != 1 and layer != 0 and bitrate not in (0, 15) and sample_rate != 3


_BITRATES = {  # кбит/с для Layer III: MPEG-1 и MPEG-2/2.5
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def frame_length(data, index):
    """Длина кадра MPEG Layer III по заголовку; не Layer III или не заголовок — None."""
    if not is_frame_header(data, index) or (data[index + 1] >> 1) & 0x03 != 1:
        return None
    version = (data[index + 1] >> 3) & 0x03  # 3 — MPEG-1, 2 — MPEG-2, 0 — MPEG-2.5
    bitrate = _BITRATES[1 if version == 3 else 2][data[index + 2] >> 4] * 1000
    sample_rate = _SAMPLE_RATES[version][(data[index + 2] >> 2) & 0x03]
    padding = (data[index + 2] >> 1) & 0x01
    return (144 if version == 3 else 72) * bitrate // sample_rate + padding


def _frames_follow(data, index, count):
    """С `index` подряд идут `count` кадров: защита от случайного «синхрослова» в данных."""
    for _ in range(count):
        length = frame_length(data, index)
        if length is None:
            return False
        index += length
    return True


def whole_frames(data):
    """Только целые кадры MP3: срезает байты до первого кадра и неполный последний.

    Иначе ffmpeg видит битый кадр, и проверка качества отбраковала бы каждый дубль.
    """
    start = next((i for i in range(min(len(data), 65536)) if _frames_follow(data, i, 3)), None)
    if start is None:
        return data
    end = start
    while True:
        length = frame_length(data, end)
        if length is None or end + length > len(data):
            break
        end += length
    return data[start:end]


def align_to_frame(data):
    """Совместимое имя: нарезка по целым кадрам."""
    return whole_frames(data)


class AirBuffer:
    """Кольцевой буфер эфира: куски с временем начала по счёту байтов (поток идёт в реальном времени)."""

    def __init__(self, bytes_per_second, keep_seconds):
        self.bytes_per_second = bytes_per_second
        self.keep_seconds = keep_seconds
        self.chunks = collections.deque()  # (время начала куска, байты)
        self.lock = threading.Lock()
        self.connected_at = None  # начало непрерывного потока
        self._position = None

    def connect(self, audio_start):
        """Новое соединение: песни, начатые до него, записаны не целиком."""
        with self.lock:
            self.connected_at = audio_start
            self._position = audio_start

    def append(self, data):
        with self.lock:
            self.chunks.append((self._position, data))
            self._position += len(data) / self.bytes_per_second
            while self.chunks and self.chunks[0][0] < self._position - self.keep_seconds:
                self.chunks.popleft()

    def slice(self, start, end):
        """Байты эфира между моментами start и end, выровненные по кадру MP3."""
        with self.lock:
            parts = []
            for chunk_start, data in self.chunks:
                chunk_end = chunk_start + len(data) / self.bytes_per_second
                if chunk_end <= start or chunk_start >= end:
                    continue
                first = max(0, int((start - chunk_start) * self.bytes_per_second))
                last = min(len(data), int((end - chunk_start) * self.bytes_per_second))
                parts.append(data[first:last])
            return align_to_frame(b"".join(parts))

    def covers(self, start, end):
        """Буфер непрерывно (одним соединением) содержит весь промежуток [start, end]."""
        with self.lock:
            return (
                self.connected_at is not None and self.connected_at <= start
                and bool(self.chunks) and self.chunks[0][0] <= start and self._position >= end
            )


class TrackTracker:
    """Отслеживает смену названия и отдаёт песни, у которых видны начало и конец."""

    def __init__(self):
        self.current = None  # (исполнитель, название) или None для джингла
        self.started = None  # момент смены на текущую песню; None — начало не наблюдалось

    def update(self, parsed, changed_at):
        """Новое название; `changed_at=None` — первый опрос (начало текущей песни не видно).

        Возвращает (исполнитель, название, начало, конец) завершившейся песни либо None.
        """
        if changed_at is None:
            self.current, self.started = parsed, None
            return None
        if parsed == self.current:
            return None
        finished = None
        if self.current is not None and self.started is not None:
            finished = (*self.current, self.started, changed_at)
        self.current, self.started = parsed, changed_at
        return finished


# --- Дубли, отпечатки и выбор лучшей записи -------------------------------


def recording_id(artist, title):
    return beufm._track_id(artist, title)


def _write_json(path, data):
    temp = f"{path}.part"
    with open(temp, "w", encoding="utf-8") as target:
        json.dump(data, target, ensure_ascii=False, indent=1)
    os.replace(temp, path)


def save_take(directory, artist, title, started, ended, data, now=None):
    """Сохраняет дубль `<id>/take-<время>.mp3` + `.json`; возвращает путь к MP3."""
    now = time.time() if now is None else now
    track_dir = os.path.join(directory, recording_id(artist, title))
    os.makedirs(track_dir, exist_ok=True)
    base = os.path.join(track_dir, f"take-{now:.0f}")
    with open(f"{base}.mp3.part", "wb") as target:
        target.write(data)
    os.replace(f"{base}.mp3.part", f"{base}.mp3")
    _write_json(f"{base}.json", {
        "artist": artist, "title": title, "recorded": now,
        "title_started": started, "title_ended": ended, "bytes": len(data),
    })
    return f"{base}.mp3"


def list_takes(track_dir):
    """Дубли песни, старые первыми: [(путь к MP3, метаданные)]."""
    takes = []
    if not os.path.isdir(track_dir):
        return takes
    for name in sorted(os.listdir(track_dir)):
        if name.startswith("take-") and name.endswith(".json"):
            mp3 = os.path.join(track_dir, name[:-5] + ".mp3")
            try:
                with open(os.path.join(track_dir, name), encoding="utf-8") as source:
                    meta = json.load(source)
            except (OSError, ValueError):
                continue
            if os.path.isfile(mp3):
                takes.append((mp3, meta))
    return takes


def remove_take(mp3_path):
    for path in (mp3_path, mp3_path[:-4] + ".json"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def fingerprint(path):
    """Сырой отпечаток Chromaprint всего файла (список 32-битных чисел); нет `fpcalc` — None."""
    if shutil.which("fpcalc") is None:
        return None
    result = subprocess.run(
        ["fpcalc", "-raw", "-length", "0", "-json", path], capture_output=True, text=True, timeout=120, check=False
    )
    # При битом кадре fpcalc завершается с кодом 3, но отпечаток выдаёт: битые дубли
    # отсеивает decode_errors, здесь достаточно, чтобы отпечаток был.
    try:
        return json.loads(result.stdout).get("fingerprint") or None
    except ValueError:
        return None


def decode_errors(path):
    """Число сообщений об ошибках при полном декодировании (битые кадры, обрывы); нет ffmpeg — 0."""
    if shutil.which("ffmpeg") is None:
        return 0
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True, timeout=300,
        check=False,
    )
    return len([line for line in result.stderr.splitlines() if line.strip()]) + (result.returncode != 0)


def _ber(a, b, offset, start=0, end=None):
    """Доля различающихся бит отпечатков при сдвиге b относительно a на `offset` элементов."""
    end = len(a) if end is None else end
    total = count = 0
    for index in range(max(start, -offset), min(end, len(b) - offset)):
        total += (a[index] ^ b[index + offset]).bit_count()
        count += 1
    return (total / (32 * count), count) if count else (1.0, 0)


def best_offset(a, b, max_shift, min_overlap):
    """Сдвиг b относительно a (в элементах) с наименьшей долей различий."""
    best = (1.0, 0)
    for offset in range(-max_shift, max_shift + 1):
        ber, count = _ber(a, b, offset)
        if count >= min_overlap and ber < best[0]:
            best = (ber, offset)
    return best[1], best[0]


def item_ber(a, b, offset):
    """Доля различий каждого элемента a с элементом b при сдвиге (вне b — 1.0)."""
    return [
        (a[index] ^ b[index + offset]).bit_count() / 32 if 0 <= index + offset < len(b) else 1.0
        for index in range(len(a))
    ]


def matched_mask(a, b, offset, threshold, smooth=8):
    """Для каждого элемента a: совпадает ли окрестность (`smooth` элементов) с b при сдвиге."""
    raw = item_ber(a, b, offset)
    half = smooth // 2
    mask = []
    for index in range(len(a)):
        window = raw[max(0, index - half):index + half + 1]
        mask.append(sum(window) / len(window) < threshold)
    return mask


def extend_edges(run, raw_lists, threshold, limit):
    """Сглаживание срезает край участка на ~1 с: расширяем, пока элемент совпадает со всеми дублями."""
    start, end = run
    size = len(raw_lists[0])
    for _ in range(limit):
        if start > 0 and all(raw[start - 1] < threshold for raw in raw_lists):
            start -= 1
        else:
            break
    for _ in range(limit):
        if end + 1 < size and all(raw[end + 1] < threshold for raw in raw_lists):
            end += 1
        else:
            break
    return start, end


def longest_run(mask, max_gap):
    """Самый длинный участок True с пропусками не длиннее `max_gap`: (начало, конец) или None."""
    best = None
    start = last = None
    for index, value in enumerate(mask):
        if not value:
            continue
        if start is None or index - last - 1 > max_gap:
            if start is not None and (best is None or last - start > best[1] - best[0]):
                best = (start, last)
            start = index
        last = index
    if start is not None and (best is None or last - start > best[1] - best[0]):
        best = (start, last)
    return best


def choose_take(fingerprints, settings):
    """Общий для всех дублей участок и лучший дубль.

    Возвращает (индекс лучшего дубля, начало, конец в секундах его файла, качество)
    либо None, если дубли не сходятся. Качество — средняя доля различий участка
    с остальными дублями (0 — один и тот же звук, ~0.5 — несвязанный).
    """
    max_shift = int((settings["window_seconds"] + 30) / FINGERPRINT_STEP)
    min_overlap = int(settings["min_seconds"] / FINGERPRINT_STEP)
    max_gap = int(3 / FINGERPRINT_STEP)
    candidates = []
    for index, base in enumerate(fingerprints):
        common = [True] * len(base)
        bers, raw_lists = [], []
        for other_index, other in enumerate(fingerprints):
            if other_index == index:
                continue
            offset, ber = best_offset(base, other, max_shift, min_overlap)
            mask = matched_mask(base, other, offset, settings["match_ber"])
            common = [x and y for x, y in zip(common, mask, strict=True)]
            bers.append((offset, ber))
            raw_lists.append(item_ber(base, other, offset))
        run = longest_run(common, max_gap)
        if run is None:
            continue
        start, end = extend_edges(run, raw_lists, settings["match_ber"] + 0.1, int(3 / FINGERPRINT_STEP))
        duration = (end - start + 1) * FINGERPRINT_STEP
        if not settings["min_seconds"] <= duration <= settings["max_seconds"]:
            continue
        quality = sum(
            _ber(base, other, offset, start, end + 1)[0]
            for other, (offset, _) in zip((f for i, f in enumerate(fingerprints) if i != index), bers, strict=True)
        ) / len(bers)
        candidates.append((quality, index, start * FINGERPRINT_STEP, (end + 1) * FINGERPRINT_STEP))
    if not candidates:
        return None
    quality, index, start, end = min(candidates)
    return index, start, end, quality


def finalize(directory, track_id, settings, force=False):
    """Выбирает лучший дубль и пишет готовую запись `<id>.mp3`/`.json`.

    Возвращает 'ready', 'waiting' (мало дублей), 'retry' (дубли не сошлись или
    лучший битый — худший удалён, ждём следующий выход) или 'no-tools'.
    """
    track_dir = os.path.join(directory, track_id)
    takes = list_takes(track_dir)
    if len(takes) < settings["min_takes"] or (len(takes) < settings["takes"] and not force):
        return "waiting"
    fingerprints = [fingerprint(path) for path, _ in takes]
    if any(fp is None for fp in fingerprints):
        return "no-tools"
    errors = [decode_errors(path) for path, _ in takes]
    usable = [index for index, count in enumerate(errors) if count == 0]
    for index in [i for i in range(len(takes)) if i not in usable]:
        logger.info(f"Дубль {takes[index][0]} с ошибками декодирования ({errors[index]}) — удалён")
        remove_take(takes[index][0])
    takes = [takes[i] for i in usable]
    fingerprints = [fingerprints[i] for i in usable]
    if len(takes) < settings["min_takes"]:
        return "retry"
    choice = choose_take(fingerprints, settings)
    if choice is None:
        # Дубли не сходятся: скорее всего один из них — не та песня или запись с
        # провалом. Удаляем самый старый и дописываем при следующем выходе.
        logger.info(f"Дубли {track_id} не сошлись — самый старый удалён, ждём следующий выход в эфир")
        remove_take(takes[0][0])
        return "retry"
    index, start, end, quality = choice
    path, meta = takes[index]
    bytes_per_second = settings["bitrate_kbps"] * 1000 / 8
    with open(path, "rb") as source:
        data = source.read()
    first = max(0, int((start - settings["pad_start_seconds"]) * bytes_per_second))
    last = min(len(data), int((end + settings["pad_end_seconds"]) * bytes_per_second))
    audio = align_to_frame(data[first:last])
    final = os.path.join(directory, f"{track_id}.mp3")
    with open(f"{final}.part", "wb") as target:
        target.write(audio)
    _tag(f"{final}.part", meta["artist"], meta["title"])
    os.replace(f"{final}.part", final)
    _write_json(os.path.join(directory, f"{track_id}.json"), {
        "id": track_id,
        "artist": meta["artist"],
        "title": meta["title"],
        "duration": round(end - start, 1),
        "takes": len(takes),
        "quality": round(quality, 3),
        "recorded": datetime.fromtimestamp(meta["recorded"]).isoformat(timespec="seconds"),
        "source": "Beu FM, запись эфира",
    })
    shutil.rmtree(track_dir, ignore_errors=True)
    logger.info(
        f"Готова запись из эфира: {meta['artist']} - {meta['title']} ({end - start:.0f} с, "
        f"лучший из {len(takes)} дублей, расхождение {quality:.3f})"
    )
    return "ready"


def _tag(path, artist, title):
    """ID3 исполнителя и названия; без mutagen файл остаётся без тегов (движок их не требует)."""
    try:
        from mutagen.id3 import COMM, ID3, TIT2, TPE1, ID3NoHeaderError
    except ImportError:
        return
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(COMM(encoding=3, lang="rus", desc="", text=["Запись эфира Beu FM"]))
    tags.save(path)


def finalize_overdue(directory, settings, now=None):
    """Песни с ≥ min_takes дублями, ждущие больше `max_wait_days`, собираются из того, что есть."""
    now = time.time() if now is None else now
    if not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        takes = list_takes(os.path.join(directory, name))
        if takes and takes[0][1].get("recorded", now) < now - settings["max_wait_days"] * 86400:
            finalize(directory, name, settings, force=True)


def cleanup(directory, keep_days, max_mb, now=None):
    """Удаляет записи и дубли старше keep_days и самые старые сверх max_mb; возвращает удалённое."""
    now = time.time() if now is None else now
    if not os.path.isdir(directory):
        return []
    items = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.endswith(".mp3"):
                path = os.path.join(root, name)
                items.append((os.path.getmtime(path), os.path.getsize(path), path))
    items.sort()
    total = sum(size for _, size, _ in items)
    removed = []
    for mtime, size, path in items:
        if mtime >= now - keep_days * 86400 and total <= max_mb * 1048576:
            continue
        for target in (path, path[:-4] + ".json"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(target)
        total -= size
        removed.append(path)
    for name in os.listdir(directory):
        track_dir = os.path.join(directory, name)
        if os.path.isdir(track_dir) and not os.listdir(track_dir):
            os.rmdir(track_dir)
    return removed


def list_recordings(directory):
    """Готовые записи (для драйвера Beu FM), старые первыми: метаданные + `path`."""
    if not os.path.isdir(directory):
        return []
    found = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        meta_path = os.path.join(directory, name)
        mp3_path = meta_path[:-5] + ".mp3"
        try:
            with open(meta_path, encoding="utf-8") as source:
                meta = json.load(source)
        except (OSError, ValueError):
            continue
        if os.path.isfile(mp3_path) and meta.get("artist") and meta.get("title"):
            found.append({**meta, "path": mp3_path})
    return sorted(found, key=lambda meta: meta.get("recorded", ""))


def in_library(artist, title, db_path=DB_PATH):
    """Песня уже скачана загрузчиком (тот же ключ artist/title, что у движка)?"""
    if not os.path.isfile(db_path):
        return False
    engine = _engine()
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE artist = ? AND title = ? LIMIT 1",
                (engine.normalize_db_text(artist), engine.normalize_db_text(title)),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# --- Сеть и служба -------------------------------------------------------


def changed_at_from_headers(headers, received_at):
    """Момент изменения 1.txt по `Last-Modified` в местных часах (поправка по `Date`); иначе None."""
    try:
        modified = email.utils.parsedate_to_datetime(headers["Last-Modified"]).timestamp()
        server_now = email.utils.parsedate_to_datetime(headers["Date"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    return modified + (received_at - server_now)


def read_now_playing(url, timeout=10):
    """((исполнитель, название) или None для джингла, момент изменения 1.txt или None)."""
    request = urllib.request.Request(
        f"{url}?{time.time():.0f}", headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
        changed_at = changed_at_from_headers(response.headers, time.time())
    return beufm.parse_now_playing(text), changed_at


def stream_worker(settings, buffer, stop):
    """Читает поток с переподключением; время эфира считается по числу байтов."""
    while not stop.is_set():
        try:
            request = urllib.request.Request(settings["stream_url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=20) as response:
                connected = time.time()
                # Icecast при подключении отдаёт «запас» из прошлого (замер: ~2.4 с):
                # начало буфера сдвигается назад на то, что пришло быстрее реального времени.
                data = b""
                while time.time() - connected < 1.0:
                    data += response.read(8192)
                ahead = max(0.0, len(data) / buffer.bytes_per_second - (time.time() - connected))
                buffer.connect(connected - ahead)
                buffer.append(data)
                logger.info(f"Подключён к эфиру {settings['stream_url']} (запас {ahead:.1f} с)")
                while not stop.is_set():
                    chunk = response.read(8192)
                    if not chunk:
                        raise ConnectionError("поток закрыт сервером")
                    buffer.append(chunk)
        except Exception as error:
            if stop.is_set():
                break
            logger.warning(f"Разрыв эфира, переподключение через 10 с: {error}")
            stop.wait(10)


def handle_finished(settings, buffer, artist, title, started, ended):
    """Сохраняет дубль завершившейся песни и при достаточном числе дублей собирает запись."""
    directory = settings["dir"]
    track_id = recording_id(artist, title)
    if in_library(artist, title):
        return "в библиотеке"
    if os.path.isfile(os.path.join(directory, f"{track_id}.mp3")):
        return "уже записана"
    start, end = started - settings["window_seconds"], ended + settings["tail_seconds"]
    if not buffer.covers(start, end):
        return "нет эфира за весь запас (подключение позже начала или разрыв)"
    # Название держится столько же, сколько песня, ± минута из-за поминутного 1.txt.
    title_seconds = ended - started
    if not settings["min_seconds"] - 60 <= title_seconds <= settings["max_seconds"] + 60:
        return "длительность вне пределов"
    save_take(directory, artist, title, started, ended, buffer.slice(start, end))
    count = len(list_takes(os.path.join(directory, track_id)))
    logger.info(f"Дубль {count}/{settings['takes']}: {artist} - {title}")
    status = finalize(directory, track_id, settings)
    if status == "no-tools":
        logger.warning("Нет fpcalc (chromaprint) или ffmpeg: дубли копятся, но запись не собирается")
    for path in cleanup(directory, settings["keep_days"], settings["max_mb"]):
        logger.info(f"Удалена старая запись {path}")
    return status


def run_service(settings):
    bytes_per_second = settings["bitrate_kbps"] * 1000 / 8
    keep = settings["max_seconds"] + settings["window_seconds"] + settings["tail_seconds"] + 120
    buffer = AirBuffer(bytes_per_second, keep)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(target=stream_worker, args=(settings, buffer, stop), daemon=True).start()
    tracker = TrackTracker()
    logger.info(f"Запись эфира Beu FM запущена, записи — {settings['dir']}")
    pending = []
    first_poll = True
    last_overdue = 0.0
    while not stop.is_set():
        try:
            parsed, modified_at = read_now_playing(settings["now_playing_url"])
        except Exception as error:
            logger.warning(f"Не удалось прочитать название трека: {error}")
            stop.wait(settings["poll_seconds"])
            continue
        now = time.time()
        if modified_at is None or not now - 2 * settings["poll_seconds"] - 60 <= modified_at <= now + 1:
            modified_at = now - settings["poll_seconds"] / 2
        previous = tracker.current
        finished = tracker.update(parsed, None if first_poll else modified_at)
        first_poll = False
        if finished:
            pending.append(finished)
        if parsed and parsed != previous:
            logger.info(f"В эфире: {parsed[0]} - {parsed[1]}")
        # Дубль режем, когда в буфер пришёл и хвост после смены названия.
        for item in list(pending):
            if time.time() < item[3] + settings["tail_seconds"] + 1:
                continue
            pending.remove(item)
            status = handle_finished(settings, buffer, *item)
            logger.info(f"{item[0]} - {item[1]}: {status}")
        if now - last_overdue > 3600:
            finalize_overdue(settings["dir"], settings)
            last_overdue = now
        stop.wait(settings["poll_seconds"])
    logger.info("Запись эфира Beu FM остановлена")


def dry_run(settings):
    """Проверки без записи: настройки, поток, название, инструменты; план — `[dry-run] будет: …`."""
    request = urllib.request.Request(settings["stream_url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = response.headers.get("Content-Type")
        sample = response.read(16384)
    if not any(is_frame_header(sample, index) for index in range(len(sample))):
        raise RuntimeError(f"В потоке {settings['stream_url']} не найдено кадров MP3 ({content_type})")
    parsed, _ = read_now_playing(settings["now_playing_url"])
    now = f"{parsed[0]} - {parsed[1]}" if parsed else "джингл/реклама (без « - »)"
    tools = {name: shutil.which(name) is not None for name in ("fpcalc", "ffmpeg")}
    plan = [
        f"чтение эфира {settings['stream_url']} ({content_type}, {settings['bitrate_kbps']} кбит/с)",
        f"опрос {settings['now_playing_url']} раз в {settings['poll_seconds']} с; сейчас в эфире: {now}",
        f"дубли каждого выхода песни (запас {settings['window_seconds']} с до названия, "
        f"{settings['tail_seconds']} с после) в {settings['dir']}; песни из библиотеки не пишутся",
        f"сборка записи из лучшего из {settings['takes']} дублей по общему участку отпечатков "
        f"(fpcalc: {'есть' if tools['fpcalc'] else 'НЕТ'}, ffmpeg: {'есть' if tools['ffmpeg'] else 'НЕТ'})",
        f"готовых записей: {len(list_recordings(settings['dir']))}; очистка старше {settings['keep_days']} дн. "
        f"и сверх {settings['max_mb']} МиБ",
    ]
    for line in plan:
        print(f"[dry-run] будет: {line}")
        logger.info(f"[dry-run] {line}")
    return 0 if all(tools.values()) else 1


_ENGINE = None


def _engine():
    """Движок загрузчика по пути — ради JSONLHandler и normalize_db_text (одинаковые ключи БД)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _load_module("music_downloader_engine", os.path.join(BASE_DIR, "music_downloader.py"))
    return _ENGINE


def _with_site(record):
    """JSONLHandler берёт `service.name` из поля `site` записи."""
    record.site = getattr(record, "site", SCRIPT_NAME)
    return True


def _daily_path(log_dir, day=None):
    return os.path.join(log_dir, f"{SCRIPT_NAME}-{day or datetime.now():%Y-%m-%d}.jsonl")


def _rotate_logs(log_dir, max_files):
    logs = sorted(name for name in os.listdir(log_dir) if name.startswith(f"{SCRIPT_NAME}-") and name.endswith(".jsonl"))
    for name in logs[:-max_files]:
        os.remove(os.path.join(log_dir, name))


def setup_logging(log_dir, max_files):
    """Суточный JSONL-лог в формате движка (ECS-поля из conf/log_template.conf).

    Служба работает сутками: при смене даты запись переходит в файл нового дня,
    старые файлы сверх `max_files` удаляются.
    """
    os.makedirs(log_dir, exist_ok=True)
    base = _engine().JSONLHandler

    class DailyJSONLHandler(base):
        def emit(self, record):
            path = _daily_path(log_dir)
            if self.baseFilename != os.path.abspath(path):
                self.close()
                self.baseFilename = os.path.abspath(path)
                self.stream = None
                _rotate_logs(log_dir, max_files)
            super().emit(record)

    handler = DailyJSONLHandler(_daily_path(log_dir), encoding="utf-8", delay=True)
    handler.addFilter(_with_site)
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.setLevel(logging.INFO)
    _rotate_logs(log_dir, max_files)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Запись эфира Beu FM для песен, которых нет в источниках MP3")
    parser.add_argument("-n", "--dry-run", action="store_true", help="только проверить и показать план")
    args = parser.parse_args(argv)
    with open(CONFIG_PATH, encoding="utf-8") as source:
        config = json.load(source)
    settings = load_settings(config)
    log_dir = os.environ.get("MYTASKS_LOG_DIR") or os.path.join(os.path.dirname(BASE_DIR), "logs")
    setup_logging(log_dir, settings["log_max_files"])
    if not settings["enabled"]:
        logger.info("Запись эфира отключена (recorder.enabled = false)")
        return 0
    if args.dry_run:
        return dry_run(settings)
    run_service(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
