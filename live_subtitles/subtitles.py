#!/usr/bin/env python3
"""Живые русские субтитры к казахской речи из системного звука (PipeWire).

Конвейер: parec (monitor вывода, 16 кГц моно) -> Silero VAD (нарезка на фразы)
-> faster-whisper (kk, CUDA) -> NLLB (kaz_Cyrl -> rus_Cyrl, CUDA) -> окно PyQt6.
"""

# ============================ НАСТРОЙКИ ============================
# Любой ключ можно переопределить в config.toml рядом со скриптом
# (см. config.example.toml) или аргументами командной строки.
DEFAULTS = {
    # --- Звук ---
    "audio_source": "",             # "" = monitor вывода по умолчанию
    # --- Нарезка на фразы (VAD) ---
    "vad_threshold": 0.5,           # порог «это речь»; выше — строже
    "vad_neg_threshold": 0.35,      # ниже — кадр тишины
    "min_silence_ms": 450,          # пауза, завершающая фразу
    "soft_phrase_ms": 2500,         # после этой длины режем по короткой паузе
    "soft_silence_ms": 200,         # короткая пауза для мягкого разреза
    "max_phrase_ms": 5500,          # жёсткий предел длины фразы: субтитр появляется только после конца фразы
    "min_speech_ms": 300,           # всплески короче отбрасываются
    "preroll_ms": 300,              # пре-ролл перед началом речи
    "min_phrase_rms": 0.004,        # фразы тише (RMS, 0..1) считаются шумом и не распознаются
    "max_chars_per_sec": 30,        # больше символов в секунду — галлюцинация
    "max_compression_ratio": 2.4,   # zlib-сжатие текста выше — зацикливание
    # --- Модели ---
    # Дообученный на казахском turbo (WER на FLEURS 10,4% против 22,9% у базовой), сконвертирован в CTranslate2
    # скриптом convert_whisper.py. Если каталога нет — берётся базовая "large-v3-turbo" (или "large-v3").
    "whisper_model": "/var/lib/llama.cpp/whisper-ct2/whisper-turbo-ksc2",
    "whisper_beam_size": 5,
    "whisper_compute_type": "int8_float16",   # экономит ~1 ГБ VRAM: 9B-модели llama достаётся больше слоёв на GPU; "float16" — максимум точности
    "translator": "llama",          # "llama" (llama-server) или "nllb" (локально, ест VRAM)
    "llama_url": "http://127.0.0.1:8080/v1",
    "llama_model": "qwen3.5-9b",    # секция из /etc/llama.cpp/models.ini; точнее, но 4–8 с: qwen3.6-35b-a3b
    "llama_timeout_sec": 30,        # таймаут одного перевода
    "llama_load_timeout_sec": 300,  # первая загрузка модели роутером
    "nllb_model": "facebook/nllb-200-distilled-1.3B",  # или ...-600M
    "nllb_beams": 2,
    # --- Задержка ---
    "max_lag_sec": 6.0,             # более старые фразы пропускаются
    # --- Окно ---
    "show_original": True,
    "font_size_ru": 30,
    "font_size_kk": 26,
    "window_width_frac": 0.8,
    "max_height_frac": 0.4,         # высота окна субтитров — доля высоты окна браузера (экрана); текст подгоняется под неё
    "min_font_scale": 0.45,         # нижний предел автоуменьшения шрифта для длинных фраз
    "bottom_margin_px": 60,
    "background_alpha": 190,
    "clear_after_sec": 8.0,
    "qt_platform": "wayland",       # "wayland" или "xcb" (XWayland)
    "follow_window": True,          # Wayland/KWin: показывать субтитры на окне браузера, а не на всём экране
    "browser_classes": ["firefox", "chrom", "brave", "vivaldi", "opera", "zen", "librewolf", "floorp", "edge"],
    "show_indicator": True,         # постоянный значок состояния (слушаю / слышу речь / перевожу / ошибка)
    # Язык фразы определяет базовая large-v3-turbo: у дообученной ksc2 собственный детектор языка сломан (замер на
    # FLEURS: казахский 0,12–0,55 против 0,98–0,99 у базовой; русский/английский 0,00). Пусто — использовать ASR-модель.
    "lid_model": "large-v3-turbo",
    "lid_skip_sec": 12,             # язык не проверять повторно, если казахская фраза была не более стольких секунд назад
    "kk_min_prob": 0.5,             # минимум вероятности казахского по детектору языка Whisper; иное (рус./англ.) не переводится
    "idle_unload_sec": 600,         # выгрузить Whisper и модель перевода из VRAM после стольких секунд без речи (0 — не выгружать)
    "service": False,               # режим systemd-сервиса (--service)
    "layer_shell": True,            # Wayland: слой Overlay (LayerShellQt) — остаётся над полноэкранным видео
}
# Каталог кэша моделей Hugging Face (whisper, NLLB): рядом с моделями llama.cpp.
HF_CACHE_DIR = "/var/lib/llama.cpp/hf-cache"
# ===================================================================

import argparse
import collections
import ctypes
import glob
import os
import queue
import re
import signal
import site
import subprocess
import sys
import threading
import time
import tomllib
import zlib
import json
import urllib.request

SAMPLE_RATE = 16000
CHUNK = 512                          # кадр Silero VAD при 16 кГц (32 мс)
CHUNK_MS = CHUNK * 1000 // SAMPLE_RATE
HERE = os.path.dirname(os.path.abspath(__file__))
APP_ID = "live_subtitles"
KWIN_RULE = "live-subtitles-overlay"


def preload_cuda_libs():
    """Подгружает libcudnn/libcublas из пакетов nvidia-* в venv (обход проблемы ctranslate2)."""
    names = ("libcudart.so.12", "libcublasLt.so.12", "libcublas.so.12", "libcudnn.so.9")
    dirs = []
    for sp in site.getsitepackages():
        dirs += sorted(glob.glob(os.path.join(sp, "nvidia", "*", "lib")))
    for name in names:
        for d in dirs:
            path = os.path.join(d, name)
            if os.path.exists(path):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                except OSError as exc:
                    print(f"[cuda] не удалось загрузить {path}: {exc}", file=sys.stderr)
                break


os.environ.setdefault("HF_HOME", HF_CACHE_DIR)
preload_cuda_libs()
import torch  # noqa: E402  (до faster_whisper: подтягивает CUDA-библиотеки)
import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402


STATE_DIR = os.path.expanduser("~/.local/state/live_subtitles")
SELECTED_KEYS = ("whisper_model", "whisper_beam_size", "whisper_compute_type", "llama_model", "translator", "nllb_model")


def load_selected():
    """Выбор менеджера моделей (manage_models.py): лучшие по замерам модели; перекрывается config.toml."""
    path = os.path.join(STATE_DIR, "selected.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {k: v for k, v in json.load(fh).items() if k in SELECTED_KEYS}
    except (OSError, ValueError):
        return {}


def load_config(args):
    cfg = dict(DEFAULTS)
    cfg.update(load_selected())
    path = args.config or os.path.join(HERE, "config.toml")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            cfg.update(tomllib.load(fh))
    if args.whisper_model:
        cfg["whisper_model"] = args.whisper_model
    if args.nllb_model:
        cfg["nllb_model"] = args.nllb_model
    if args.translator:
        cfg["translator"] = args.translator
    if args.llama_model:
        cfg["llama_model"] = args.llama_model
    if args.service:
        cfg["service"] = True           # не грузить модели заранее; индикатор — только пока что-то играет
    if args.no_original:
        cfg["show_original"] = False
    if args.source:
        cfg["audio_source"] = args.source
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        print(f"[config] неизвестные ключи: {sorted(unknown)}", file=sys.stderr)
    return cfg


def resolve_source(cfg):
    if cfg["audio_source"]:
        return cfg["audio_source"]
    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
    return sink + ".monitor"


# ------------------------------ захват + VAD ------------------------------
class Capture(threading.Thread):
    """Читает звук из parec, режет на фразы Silero VAD, кладёт в очередь (audio, t_end).

    Захват переживает смену устройства вывода и падение parec: поток переоткрывает источник.
    """

    def __init__(self, cfg, out_q, on_state=lambda state: None):
        super().__init__(daemon=True)
        self.cfg, self.out_q, self.on_state = cfg, out_q, on_state
        self.proc = None
        self.stopping = False

    def _open(self):
        source = resolve_source(self.cfg)
        print(f"[audio] источник: {source}", flush=True)
        self.proc = subprocess.Popen(
            ["parec", "-d", source, "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1",
             "--latency-msec=50"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return source

    def _watch_default_sink(self, source):
        """Если устройство вывода по умолчанию сменилось, закрываем parec — основной цикл переоткроет его."""
        while not self.stopping and self.proc and self.proc.poll() is None:
            time.sleep(3)
            try:
                if not self.cfg["audio_source"] and resolve_source(self.cfg) != source:
                    print("[audio] сменилось устройство вывода по умолчанию, переоткрываю захват", flush=True)
                    self.proc.terminate()
                    return
            except (subprocess.SubprocessError, OSError):
                pass

    def run(self):
        from silero_vad import load_silero_vad
        torch.set_num_threads(1)
        vad = load_silero_vad()
        cfg = self.cfg
        while not self.stopping:
            try:
                source = self._open()
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"[audio] не удалось открыть источник: {exc}; повтор через 2 с", file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            threading.Thread(target=self._watch_default_sink, args=(source,), daemon=True).start()
            self._capture_loop(vad, cfg)
            if not self.stopping:
                print("[audio] parec завершился, переоткрываю захват через 1 с", file=sys.stderr, flush=True)
                time.sleep(1)
                vad.reset_states()

    def _capture_loop(self, vad, cfg):
        preroll = collections.deque(maxlen=max(1, cfg["preroll_ms"] // CHUNK_MS))
        buf, in_speech, silence_ms, speech_ms = [], False, 0, 0
        nbytes = CHUNK * 2
        while True:
            raw = self.proc.stdout.read(nbytes)
            if len(raw) < nbytes:
                return
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            prob = vad(torch.from_numpy(chunk), SAMPLE_RATE).item()
            if not in_speech:
                preroll.append(chunk)
                if prob >= cfg["vad_threshold"]:
                    in_speech, buf = True, list(preroll)
                    silence_ms, speech_ms = 0, CHUNK_MS
                    self.on_state("hear")
                continue
            buf.append(chunk)
            if prob < cfg["vad_neg_threshold"]:
                silence_ms += CHUNK_MS
            else:
                silence_ms, speech_ms = 0, speech_ms + CHUNK_MS
            total_ms = len(buf) * CHUNK_MS
            need = cfg["soft_silence_ms"] if total_ms >= cfg["soft_phrase_ms"] else cfg["min_silence_ms"]
            if silence_ms >= need or total_ms >= cfg["max_phrase_ms"]:
                if speech_ms >= cfg["min_speech_ms"]:
                    self.out_q.put((np.concatenate(buf), time.monotonic()))
                else:
                    self.on_state("listen")
                buf, in_speech = [], False
                preroll.clear()
                vad.reset_states()

    def stop(self):
        self.stopping = True
        if self.proc:
            self.proc.terminate()


# ------------------------------ ASR + перевод ------------------------------
def load_asr(cfg):
    if os.path.isabs(cfg["whisper_model"]) and not os.path.exists(cfg["whisper_model"]):
        print(f"[asr] нет {cfg['whisper_model']} (запустите convert_whisper.py): беру базовую large-v3-turbo",
              file=sys.stderr, flush=True)
        cfg["whisper_model"] = "large-v3-turbo"
    print(f"[asr] загрузка {cfg['whisper_model']} (CUDA, {cfg['whisper_compute_type']})…", flush=True)
    try:
        return WhisperModel(cfg["whisper_model"], device="cuda", compute_type=cfg["whisper_compute_type"])
    except RuntimeError as exc:
        if "out of memory" not in str(exc):
            raise
        # VRAM занята моделями llama-server — выгружаем их (роутер загрузит нужную при первом запросе) и повторяем.
        print("[asr] не хватает VRAM: выгружаю модели llama-server и повторяю", flush=True)
        unload_llama_models(cfg)
        time.sleep(3)
        return WhisperModel(cfg["whisper_model"], device="cuda", compute_type=cfg["whisper_compute_type"])


def unload_llama_models(cfg):
    base = cfg["llama_url"].rstrip("/")
    try:
        with urllib.request.urlopen(base + "/models", timeout=10) as resp:
            models = json.load(resp)["data"]
        for m in models:
            if m.get("status", {}).get("value") == "loaded":
                req = urllib.request.Request(base[:-3] + "/models/unload" if base.endswith("/v1") else base + "/models/unload",
                                             json.dumps({"model": m["id"]}).encode(),
                                             {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=30).read()
                print(f"[llama] выгружена модель {m['id']}", flush=True)
    except OSError as exc:
        print(f"[llama] не удалось выгрузить модели: {exc}", file=sys.stderr, flush=True)


def unload_llama_model(cfg):
    """Выгружает из llama-server только модель перевода (чужие, например для кода, не трогаем)."""
    base = cfg["llama_url"].rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    try:
        req = urllib.request.Request(root + "/models/unload", json.dumps({"model": cfg["llama_model"]}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    except OSError as exc:
        print(f"[llama] не удалось выгрузить {cfg['llama_model']}: {exc}", file=sys.stderr, flush=True)


class NllbTranslator:
    """Локальный NLLB kaz_Cyrl -> rus_Cyrl (fp16, CUDA)."""

    def __init__(self, cfg):
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        print(f"[mt] загрузка {cfg['nllb_model']} (CUDA, fp16)…", flush=True)
        self.cfg = cfg
        self.tok = AutoTokenizer.from_pretrained(cfg["nllb_model"], src_lang="kaz_Cyrl")
        try:
            mt = AutoModelForSeq2SeqLM.from_pretrained(cfg["nllb_model"], dtype=torch.float16)
        except TypeError:  # старые версии transformers
            mt = AutoModelForSeq2SeqLM.from_pretrained(cfg["nllb_model"], torch_dtype=torch.float16)
        self.mt = mt.to("cuda").eval()

    def __call__(self, text, _context=None):
        inputs = self.tok(text, return_tensors="pt", truncation=True, max_length=256).to("cuda")
        with torch.inference_mode():
            out = self.mt.generate(**inputs, forced_bos_token_id=self.tok.convert_tokens_to_ids("rus_Cyrl"),
                                   num_beams=self.cfg["nllb_beams"], max_new_tokens=256, no_repeat_ngram_size=4)
        text = self.tok.batch_decode(out, skip_special_tokens=True)[0].strip()
        return re.sub(r"\s+([,.!?;:])", r"\1", text)      # NLLB ставит пробел перед знаками препинания


class LlamaTranslator:
    """Перевод kk -> ru через OpenAI-совместимый API llama-server (роутер сам грузит модель)."""

    SYSTEM = ("Ты переводчик живых субтитров. Переведи казахскую фразу на русский язык. "
              "Текст получен автоматическим распознаванием речи и может содержать ошибки: "
              "передай смысл естественно. Верни только перевод, без пояснений и кавычек.")

    def __init__(self, cfg):
        self.cfg = cfg
        self.url = cfg["llama_url"].rstrip("/") + "/chat/completions"

    def _post(self, messages, max_tokens, timeout):
        body = {"model": self.cfg["llama_model"], "messages": messages, "temperature": 0,
                "max_tokens": max_tokens, "stream": False,
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(self.url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)["choices"][0]["message"]["content"].strip()

    def warmup(self):
        print(f"[mt] прогрев {self.cfg['llama_model']} в llama-server…", flush=True)
        self._post([{"role": "user", "content": "Сәлем"}], 8, self.cfg["llama_load_timeout_sec"])

    KAZAKH_LETTERS = re.compile("[әғқңөұүһі]", re.IGNORECASE)

    def __call__(self, text, context=None):
        ru = self._translate(text, context)
        if len(self.KAZAKH_LETTERS.findall(ru)) >= 2:   # модель вернула казахский как есть — просим строже
            ru = self._translate(text, None, strict=True) or ru
        return ru

    def _translate(self, text, context, strict=False):
        system = self.SYSTEM + (" Ответ должен быть ТОЛЬКО на русском языке, казахские слова не оставляй." if strict else "")
        messages = [{"role": "system", "content": system}]
        if context:  # предыдущая пара — для связности и терминов
            messages += [{"role": "user", "content": context[0]}, {"role": "assistant", "content": context[1]}]
        messages.append({"role": "user", "content": text})
        return self._post(messages, 256, self.cfg["llama_timeout_sec"])


def make_translator(cfg):
    if cfg["translator"] == "nllb":
        return NllbTranslator(cfg)
    tr = LlamaTranslator(cfg)
    tr.warmup()
    return tr


def kazakh_probability(asr, audio):
    """Вероятность казахского языка фразы по детектору Whisper (рус./англ. речь переводить не нужно)."""
    _, _, probs = asr.detect_language(audio, language_detection_segments=1)
    return dict(probs).get("kk", 0.0)


def is_hallucination(cfg, text, duration):
    """Whisper на шуме зацикливается: отсекаем повторы и неправдоподобную плотность текста."""
    raw = text.encode()
    if len(text) / max(duration, 0.1) > cfg["max_chars_per_sec"]:
        return True
    return len(raw) > 40 and len(raw) / len(zlib.compress(raw)) > cfg["max_compression_ratio"]


def collapse_repeats(text):
    """«міне, міне, міне, …» -> «міне»: Whisper любит зацикливаться на одном слове."""
    return re.sub(r"\b(\w+)(?:[\s,.!?]+\1\b){2,}", r"\1", text, flags=re.IGNORECASE)


def transcribe(asr, cfg, audio):
    segments, _ = asr.transcribe(
        audio, language="kk", beam_size=cfg["whisper_beam_size"], temperature=0.0,
        condition_on_previous_text=False, vad_filter=False, without_timestamps=True,
        no_speech_threshold=0.6, compression_ratio_threshold=2.4)
    parts = [s.text.strip() for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
    text = " ".join(p for p in parts if p).strip()
    if is_hallucination(cfg, text, len(audio) / SAMPLE_RATE):
        print(f"[asr] отброшена галлюцинация: {text[:60]!r}…", flush=True)
        return ""
    return collapse_repeats(text)


class Worker(threading.Thread):
    """Распознаёт и переводит фразы. Модели грузятся при необходимости и выгружаются после простоя."""

    def __init__(self, cfg, in_q, emit, status, on_state=lambda state: None):
        super().__init__(daemon=True)
        self.cfg, self.in_q, self.emit, self.status, self.on_state = cfg, in_q, emit, status, on_state
        self.asr = self.lid = self.translate = None

    def run(self):
        try:
            self._run()
        except Exception as exc:  # ошибка загрузки/работы моделей не должна оставлять зависшее окно
            import traceback
            traceback.print_exc()
            print(f"[fatal] рабочий поток остановлен: {exc}", file=sys.stderr, flush=True)
            self.status(f"Ошибка: {exc}")
            time.sleep(5)
            os._exit(1)      # ненулевой код: systemd (Restart=on-failure) перезапустит сервис

    def _load(self):
        cfg = self.cfg
        self.on_state("load")
        self.status("Загрузка моделей…")
        t0 = time.time()
        self.asr = load_asr(cfg)
        self.lid = self.asr
        if cfg["lid_model"] and cfg["lid_model"] != cfg["whisper_model"]:
            self.lid = WhisperModel(cfg["lid_model"], device="cuda", compute_type=cfg["whisper_compute_type"])
        self.status("Загрузка модели перевода…")
        try:
            self.translate = make_translator(cfg)
        except Exception as exc:  # llama-server недоступен и т.п.
            print(f"[mt] не удалось подготовить перевод: {exc}", file=sys.stderr, flush=True)
            self.status(f"Перевод недоступен: {exc}")
            self.asr = self.lid = None
            raise
        print(f"[init] модели готовы за {time.time() - t0:.1f} с", flush=True)
        self.status("Субтитры запущены — жду казахскую речь…", 6)
        self.on_state("listen")

    def _unload(self):
        print("[idle] простой: выгружаю модели из VRAM", flush=True)
        self.asr = self.lid = self.translate = None
        import gc
        gc.collect()
        torch.cuda.empty_cache()
        if self.cfg["translator"] == "llama":
            unload_llama_model(self.cfg)
        self.on_state("sleep")

    def _run(self):
        cfg = self.cfg
        if not cfg["service"]:
            self._load()
        else:
            self.on_state("sleep")
        context, last_speech, last_kk = None, time.monotonic(), 0.0
        while True:
            try:
                audio, t_end = self.in_q.get(timeout=15)
            except queue.Empty:
                idle = cfg["idle_unload_sec"]
                if self.asr is not None and idle and time.monotonic() - last_speech > idle:
                    self._unload()
                continue
            if float(np.sqrt(np.mean(audio ** 2))) < cfg["min_phrase_rms"]:
                continue
            last_speech = time.monotonic()
            if self.asr is None:
                try:
                    self._load()
                except Exception:   # noqa: BLE001 — сообщение уже показано; повторим на следующей фразе
                    self.on_state("error")
                    time.sleep(5)
                    continue
            # Пропускаем устаревшие фразы, чтобы субтитры не отставали.
            while time.monotonic() - t_end > cfg["max_lag_sec"] and not self.in_q.empty():
                print("[lag] пропущена устаревшая фраза", flush=True)
                audio, t_end = self.in_q.get()
            t0 = time.monotonic()
            self.on_state("busy")
            try:
                # Язык стабилен в пределах разговора: проверяем только после паузы (экономит ~0,15 с на фразу).
                if time.monotonic() - last_kk < cfg["lid_skip_sec"]:
                    kk_prob = 1.0
                else:
                    kk_prob = kazakh_probability(self.lid, audio)
                if kk_prob < cfg["kk_min_prob"]:
                    print(f"[lang] не казахский (kk={kk_prob:.2f}), пропуск", flush=True)
                    self.on_state("other")
                    continue
                kk = transcribe(self.asr, cfg, audio)
            except RuntimeError as exc:
                if "out of memory" not in str(exc):
                    raise
                # Видеопамять занята чем-то ещё: сбрасываем модели и загрузим заново на следующей фразе, а не падаем.
                print("[oom] не хватило видеопамяти, перезагружу модели на следующей фразе", file=sys.stderr, flush=True)
                self._unload()
                self.on_state("error")
                time.sleep(3)
                continue
            if not kk:
                self.on_state("listen")
                continue
            last_kk = time.monotonic()
            t1 = time.monotonic()
            try:
                ru = self.translate(kk, context)
            except Exception as exc:
                print(f"[mt] ошибка перевода: {exc}", file=sys.stderr, flush=True)
                self.on_state("error")
                continue
            context = (kk, ru)
            t2 = time.monotonic()
            print(f"kk: {kk}\nru: {ru}\n    [asr {t1 - t0:.2f} с, mt {t2 - t1:.2f} с, "
                  f"задержка {t2 - t_end:.2f} с от конца фразы, фраза {len(audio) / SAMPLE_RATE:.1f} с, "
                  f"kk={kk_prob:.2f}]", flush=True)
            self.emit(kk, ru)
            self.on_state("listen")


# ------------------------------ layer-shell ------------------------------
LAYER_SHELL_LIB = "libLayerShellQtInterface.so.6"
LAYER_SHELL_PLUGIN_DIRS = ("/usr/lib/qt6/plugins", "/usr/lib64/qt6/plugins", "/usr/lib/x86_64-linux-gnu/qt6/plugins")
LAYER_OVERLAY, ANCHOR_BOTTOM, ANCHOR_LEFT, KEYBOARD_NONE = 3, 2, 4, 0


def prepare_layer_shell(cfg):
    """Включает shell-интеграцию layer-shell до создания QApplication.

    Нативный Wayland не позволяет закрепить обычное окно выше полноэкранного видео (слой Active
    в KWin выше Above), а слой Overlay протокола wlr-layer-shell — выше. Плагин берётся из
    системного layer-shell-qt, в pip-Qt подкладывается через QT_PLUGIN_PATH (нужна та же
    минорная версия Qt). Возвращает True, если слой доступен.
    """
    if not cfg["layer_shell"] or cfg["qt_platform"] != "wayland":
        return False
    plugin = next((os.path.join(d, "wayland-shell-integration", "liblayer-shell.so")
                   for d in LAYER_SHELL_PLUGIN_DIRS
                   if os.path.exists(os.path.join(d, "wayland-shell-integration", "liblayer-shell.so"))), None)
    try:
        ctypes.CDLL(LAYER_SHELL_LIB)
    except OSError:
        plugin = None
    if not plugin:
        print("[gui] layer-shell-qt не найден: окно без слоя Overlay (нужно правило KWin)", file=sys.stderr)
        return False
    shim = os.path.join(os.path.expanduser("~/.cache"), APP_ID, "qt-plugins")
    os.makedirs(os.path.join(shim, "wayland-shell-integration"), exist_ok=True)
    link = os.path.join(shim, "wayland-shell-integration", "liblayer-shell.so")
    if os.path.realpath(link) != os.path.realpath(plugin):
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(plugin, link)
    paths = [shim] + [q for q in os.environ.get("QT_PLUGIN_PATH", "").split(os.pathsep) if q]
    os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(paths)
    os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
    return True


class _QMargins(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("top", ctypes.c_int), ("right", ctypes.c_int), ("bottom", ctypes.c_int)]


class LayerShell:
    """Тонкая обёртка LayerShellQt::Window через ctypes (C++ API без привязок к Python)."""

    def __init__(self, qwindow, left, bottom):
        from PyQt6 import sip
        self.lib = ctypes.CDLL(LAYER_SHELL_LIB)
        pre = "_ZN12LayerShellQt6Window"
        get = getattr(self.lib, pre + "3getEP7QWindow")
        get.restype, get.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        self.win = get(sip.unwrapinstance(qwindow))
        self._call(pre + "8setLayerENS0_5LayerE", ctypes.c_int, LAYER_OVERLAY)
        self._call(pre + "10setAnchorsE6QFlagsINS0_6AnchorEE", ctypes.c_int, ANCHOR_BOTTOM | ANCHOR_LEFT)
        self._call(pre + "24setKeyboardInteractivityENS0_21KeyboardInteractivityE", ctypes.c_int, KEYBOARD_NONE)
        self._call(pre + "16setExclusiveZoneEi", ctypes.c_int, -1)   # не сдвигаться панелями
        self.set_margins(left, bottom)

    def _call(self, name, argtype, value):
        fn = getattr(self.lib, name)
        fn.argtypes = [ctypes.c_void_p, argtype]
        fn(self.win, value)

    def set_margins(self, left, bottom):
        self.margins = _QMargins(int(left), 0, 0, int(bottom))   # держим ссылку на время вызова
        self._call("_ZN12LayerShellQt6Window10setMarginsERK8QMargins", ctypes.POINTER(_QMargins),
                   ctypes.byref(self.margins))


# ------------------------------ окно ------------------------------
def make_overlay_class():
    from html import escape
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import QColor, QFont, QPainter, QTextDocument, QTextOption
    from PyQt6.QtWidgets import QApplication, QWidget

    PAD, GAP, HEAD = 22, 28, 26        # отступ, промежуток между колонками (разделитель), высота заголовка таблицы

    class Overlay(QWidget):
        sig_text = pyqtSignal(str, str)
        sig_status = pyqtSignal(str, int)
        sig_state = pyqtSignal(str)
        sig_windows = pyqtSignal(str)
        sig_mpris = pyqtSignal(str)

        def __init__(self, cfg, use_layer_shell=False):
            super().__init__()
            self.cfg = cfg
            self.layer = None
            self._drag = None
            self.setWindowTitle("Live Subtitles")
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            screen = QApplication.primaryScreen().geometry()
            self.follow_visible = True
            self.area_h = screen.height()
            self.base_scale = 1.0
            self.state = "load"
            self.kk_text = self.ru_text = ""
            self.box_h = 0
            self.doc_ru, self.doc_kk = QTextDocument(), QTextDocument()
            self.windows, self.active_id, self.mpris_title, self.target_id = [], "", "", None
            width, height = self.relayout(screen.width(), 1.0)
            self.pos_x = (screen.width() - width) // 2
            self.pos_bottom = cfg["bottom_margin_px"]
            if use_layer_shell:
                self.winId()    # создаёт QWindow до show(): layer-shell настраивается заранее
                self.layer = LayerShell(self.windowHandle(), self.pos_x, self.pos_bottom)
            else:
                # Нативный Wayland положение игнорирует — там работает правило KWin.
                self.move(screen.x() + self.pos_x, screen.y() + screen.height() - height - self.pos_bottom)
            self.clear_timer = QTimer(self)
            self.clear_timer.setSingleShot(True)
            self.clear_timer.timeout.connect(lambda: self.set_text("", ""))
            self.sig_text.connect(self.on_text)
            self.sig_status.connect(self.on_status)
            self.sig_state.connect(self.on_state)
            self.sig_windows.connect(self.on_windows)
            self.sig_mpris.connect(self.on_mpris)

        def relayout(self, area_width, scale):
            """Подгоняет размер окна под область показа (экран или окно браузера)."""
            cfg = self.cfg
            width = max(420, int(area_width * cfg["window_width_frac"]))
            height = max(200, int(self.area_h * cfg["max_height_frac"]))
            self.base_scale = scale
            self.setFixedSize(width, height)
            self.fit_text()
            self.update_mask()
            return width, height

        def on_windows(self, payload):
            import json as _json
            data = _json.loads(payload)
            self.windows, self.active_id = data["windows"], data["active"]
            self.reselect()

        def on_mpris(self, title):
            if title != self.mpris_title:
                self.mpris_title = title
                self.update_mask()
                self.update()
                self.reselect()

        def pick_window(self):
            """Окно браузера, где играет видео (по заголовку MPRIS); иначе последнее активное видимое."""
            visible = [w for w in self.windows if w["vis"]]
            title = self.mpris_title.casefold().strip()
            if title:
                for w in visible:
                    cap = w["cap"].casefold()
                    if title in cap or cap.startswith(title[:30]) or title.startswith(cap[:30]):
                        return w
            for w in visible:
                if w["id"] == self.active_id:
                    return w
            return visible[0] if visible else None

        def reselect(self):
            w = self.pick_window()
            new_id = w["id"] if w else None
            if new_id != self.target_id:
                self.target_id = new_id
                print(f"[follow] субтитры на окне: {w['cap'][:60] if w else '— (нет видимого окна браузера)'}", flush=True)
            if w:
                self.follow(w["x"], w["y"], w["w"], w["h"], w["ox"], w["oy"], w["ow"], w["oh"], 1)
            else:
                self.follow(0, 0, 0, 0, 0, 0, 1, 1, 0)

        def follow(self, gx, gy, gw, gh, ox, oy, ow, oh, visible):
            """Привязка к окну браузера (координаты окна и экрана в логических пикселях KWin)."""
            self.follow_visible = bool(visible) and gw > 0
            if not self.follow_visible:
                self.set_text("", "")
                return
            self.area_h = gh
            width, height = self.relayout(gw, max(0.8, min(1.0, gw / ow)))
            self.pos_x = max(0, min(ow - width, gx - ox + (gw - width) // 2))
            self.pos_bottom = max(0, (oy + oh) - (gy + gh) + self.cfg["bottom_margin_px"])
            if self.layer:
                self.layer.set_margins(self.pos_x, self.pos_bottom)
            self.update()

        # --- таблица: слева русский перевод, справа казахский оригинал ---
        def _two_columns(self):
            return self.cfg["show_original"] and bool(self.kk_text)

        def _column_width(self):
            total = self.width() - 2 * PAD
            return (total - GAP) // 2 if self._two_columns() else total

        def _fill(self, doc, text, size, color, bold):
            font = QFont()
            font.setPointSizeF(size)
            doc.setDefaultFont(font)
            opt = QTextOption()
            opt.setWrapMode(QTextOption.WrapMode.WordWrap)
            doc.setDefaultTextOption(opt)
            doc.setTextWidth(self._column_width())
            weight = "font-weight:600;" if bold else ""
            doc.setHtml(f'<div style="color:{color};{weight}">{escape(text)}</div>')
            doc.setTextWidth(self._column_width())

        def _content_h(self):
            return max(self.doc_ru.size().height(), self.doc_kk.size().height() if self._two_columns() else 0)

        def set_text(self, kk, ru):
            self.kk_text, self.ru_text = kk, ru
            self.fit_text()
            self.update_mask()
            self.update()

        def update_mask(self):
            """Окно ловит мышь только над плашкой (или значком); над остальной прозрачной частью клики идут в браузер."""
            from PyQt6.QtCore import QRect
            from PyQt6.QtGui import QRegion
            if self.ru_text or self.kk_text:
                rect = QRect(0, self.height() - self.box_h, self.width(), self.box_h)
            elif self._indicator_shown():
                rect = QRect((self.width() - 260) // 2, self.height() - 28, 260, 28)
            else:
                rect = QRect(0, self.height() - 1, 1, 1)     # пустая область ввода: окно «невидимо» для мыши
            if rect != getattr(self, "_mask_rect", None):
                # Qt не перерисовывает то, что осталось вне новой маски: сначала открываем окно целиком и перерисовываем
                # (paintEvent очищает прозрачностью), и только потом сужаем область до плашки.
                self.setMask(QRegion(self.rect()))
                self.repaint()
                self.setMask(QRegion(rect))
                self._mask_rect = rect

        def fit_text(self):
            """Подбирает размер шрифта так, чтобы обе колонки целиком поместились в окне; иначе сокращает оригинал."""
            cfg = self.cfg
            if not (self.ru_text or self.kk_text) or self.width() <= 2 * PAD:
                self.box_h = 0
                return
            head = HEAD if self._two_columns() else 0
            room = self.height() - 2 * 12 - head
            kk = self.kk_text
            factor = 1.0
            while True:
                self._fill(self.doc_ru, self.ru_text, cfg["font_size_ru"] * self.base_scale * factor, "#ffffff", True)
                self._fill(self.doc_kk, kk, cfg["font_size_kk"] * self.base_scale * factor, "#dcdcdc", False)
                if self._content_h() <= room or factor <= cfg["min_font_scale"]:
                    break
                factor = round(factor - 0.05, 2)
            while self._two_columns() and self._content_h() > room and len(kk) > 10:   # оригинал сокращаем, перевод — нет
                kk = kk[: int(len(kk) * 0.85)].rstrip() + "…"
                self._fill(self.doc_kk, kk, cfg["font_size_kk"] * self.base_scale * factor, "#dcdcdc", False)
            self.box_h = int(min(self.height(), head + self._content_h() + 2 * 12))

        def on_state(self, state):
            self.state = state
            self.update_mask()
            self.update()

        def on_status(self, text, seconds):
            self.set_text("", text)
            if seconds:
                self.clear_timer.start(seconds * 1000)

        def on_text(self, kk, ru):
            if not self.follow_visible:     # браузер свёрнут/закрыт — не показываем
                return
            self.set_text(kk, ru)
            self.clear_timer.start(int(self.cfg["clear_after_sec"] * 1000))

        INDICATORS = {"load": ("#d6a400", "загрузка…"), "listen": ("#2fbf4a", "слушаю"),
                      "hear": ("#3a8ee6", "слышу речь"), "busy": ("#3a8ee6", "перевожу…"),
                      "error": ("#e04040", "ошибка перевода"), "sleep": ("#7d8590", "спит: проснусь на речи"),
                      "other": ("#7d8590", "не казахский")}

        def _indicator_shown(self):
            """В режиме сервиса значок виден, только пока в браузере что-то играет (MPRIS Playing)."""
            if not (self.cfg["show_indicator"] and self.follow_visible):
                return False
            return bool(self.mpris_title) or not self.cfg["service"]

        def _paint_indicator(self):
            """Маленький постоянный значок состояния: по нему видно, что программа жива и что делает."""
            color, label = self.INDICATORS.get(self.state, self.INDICATORS["listen"])
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            font = QFont()
            font.setPointSize(11)
            p.setFont(font)
            w = p.fontMetrics().horizontalAdvance(label) + 40
            h = 28
            x, y = (self.width() - w) // 2, self.height() - h
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 120))
            p.drawRoundedRect(x, y, w, h, 12, 12)
            p.setBrush(QColor(color))
            p.drawEllipse(x + 12, y + h // 2 - 5, 10, 10)
            p.setPen(QColor(235, 235, 235))
            p.drawText(x + 30, y, w - 34, h, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

        def paintEvent(self, _event):
            # Прозрачное окно не очищается само: без этого плашки прежних кадров накладываются друг на друга.
            clear = QPainter(self)
            clear.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            clear.fillRect(self.rect(), Qt.GlobalColor.transparent)
            clear.end()
            if not (self.ru_text or self.kk_text):
                if self._indicator_shown():
                    self._paint_indicator()
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            top = self.height() - self.box_h
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, self.cfg["background_alpha"]))
            p.drawRoundedRect(0, top, self.width(), self.box_h, 16, 16)
            two = self._two_columns()
            head = HEAD if two else 0
            cw = self._column_width()
            y_text = top + 12 + head
            if two:
                font = QFont()
                font.setPointSize(11)
                p.setFont(font)
                p.setPen(QColor("#9aa0a6"))
                p.drawText(PAD, top + 8, cw, HEAD, Qt.AlignmentFlag.AlignVCenter, "Орысша")
                p.drawText(PAD + cw + GAP, top + 8, cw, HEAD, Qt.AlignmentFlag.AlignVCenter, "Қазақша")
                p.setPen(QColor(255, 255, 255, 60))
                p.drawLine(PAD, top + 8 + HEAD, self.width() - PAD, top + 8 + HEAD)                  # под заголовком
                mid = PAD + cw + GAP // 2
                p.drawLine(mid, top + 12, mid, top + self.box_h - 12)                                 # между колонками
            for doc, x in ((self.doc_ru, PAD), (self.doc_kk, PAD + cw + GAP)):
                if doc is self.doc_kk and not two:
                    continue
                p.save()
                p.translate(x, y_text)
                doc.drawContents(p)
                p.restore()

        def mousePressEvent(self, ev):
            if ev.button() == Qt.MouseButton.LeftButton and self.layer:
                self._drag = ev.position().toPoint()   # layer-shell: двигаем через поля
            elif ev.button() == Qt.MouseButton.LeftButton and self.windowHandle():
                self.windowHandle().startSystemMove()
            elif ev.button() == Qt.MouseButton.RightButton:
                QApplication.quit()

        def mouseMoveEvent(self, ev):
            if not (self.layer and self._drag and ev.buttons() & Qt.MouseButton.LeftButton):
                return
            # Поверхность едет под курсором, поэтому точка захвата остаётся прежней, а сдвиг — в локальных координатах.
            cur = ev.position().toPoint()
            screen = QApplication.primaryScreen().geometry()
            self.pos_x = max(0, min(screen.width() - self.width(), self.pos_x + cur.x() - self._drag.x()))
            self.pos_bottom = max(0, min(screen.height() - self.height(),
                                         self.pos_bottom - (cur.y() - self._drag.y())))
            self.layer.set_margins(self.pos_x, self.pos_bottom)
            self.update()

        def mouseReleaseEvent(self, _ev):
            self._drag = None

    return Overlay


FOLLOW_SERVICE = f"io.github.live_subtitles.p{os.getpid()}"   # своё имя на каждый процесс: копии не мешают друг другу
FOLLOW_PATH = "/Overlay"
FOLLOW_IFACE = "io.github.live_subtitles.Overlay"
KWIN_FOLLOW_SCRIPT = "live_subtitles_follow"

# Скрипт KWin: запоминает последнее активное окно браузера и шлёт его геометрию в приложение.
KWIN_FOLLOW_JS = """
var classes = %(classes)s;
var known = [], lastActive = "";
function isBrowser(w) {
  if (!w || !w.normalWindow) return false;
  var c = (w.resourceClass + " " + w.desktopFileName).toLowerCase();
  return classes.some(function (k) { return c.indexOf(k) >= 0; });
}
function info(w) {
  var g = w.frameGeometry, o = w.output.geometry;
  var here = w.onAllDesktops || w.desktops.indexOf(workspace.currentDesktop) >= 0;
  return {id: String(w.internalId), cap: String(w.caption), x: Math.round(g.x), y: Math.round(g.y),
          w: Math.round(g.width), h: Math.round(g.height), ox: Math.round(o.x), oy: Math.round(o.y),
          ow: Math.round(o.width), oh: Math.round(o.height), vis: (!w.minimized && here) ? 1 : 0};
}
function send() {
  var list = workspace.windowList().filter(isBrowser).map(info);
  callDBus("%(svc)s", "%(path)s", "%(iface)s", "update", JSON.stringify({active: lastActive, windows: list}));
}
function hook(w) {
  if (!isBrowser(w) || known.indexOf(w) >= 0) return;
  known.push(w);
  w.frameGeometryChanged.connect(send);
  w.minimizedChanged.connect(send);
  w.captionChanged.connect(send);
  w.desktopsChanged.connect(send);
  w.outputChanged.connect(send);
}
workspace.windowAdded.connect(function (w) { hook(w); send(); });
workspace.windowRemoved.connect(send);
workspace.windowActivated.connect(function (w) { if (isBrowser(w)) { hook(w); lastActive = String(w.internalId); } send(); });
workspace.currentDesktopChanged.connect(send);
workspace.windowList().forEach(hook);
if (isBrowser(workspace.activeWindow)) lastActive = String(workspace.activeWindow.internalId);
send();
"""


class MprisWatcher(threading.Thread):
    """Раз в 2 с узнаёт заголовок воспроизводимого ролика (MPRIS через busctl) и отдаёт его окну."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit

    @staticmethod
    def _busctl(*args):
        r = subprocess.run(["busctl", "--user", "--json=short", *args], capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None

    def playing_title(self):
        names = self._busctl("list", "--acquired")
        for item in names or []:
            name = item.get("name", "")
            if not name.startswith("org.mpris.MediaPlayer2."):
                continue
            base = ("get-property", name, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player")
            status = self._busctl(*base, "PlaybackStatus")
            if not status or status.get("data") != "Playing":
                continue
            meta = self._busctl(*base, "Metadata")
            title = ((meta or {}).get("data", {}).get("xesam:title") or {}).get("data", "")
            if title:
                return title
        return ""

    def run(self):
        while True:
            try:
                self.emit(self.playing_title())
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                print(f"[follow] MPRIS недоступен: {exc}", file=sys.stderr, flush=True)
            time.sleep(2)


def start_follow(cfg, overlay):
    """Регистрирует D-Bus-сервис приложения и загружает скрипт KWin. Возвращает функцию очистки."""
    import json
    from PyQt6.QtCore import QObject, pyqtClassInfo, pyqtSlot
    from PyQt6.QtDBus import QDBusConnection

    @pyqtClassInfo("D-Bus Interface", FOLLOW_IFACE)
    class Service(QObject):
        @pyqtSlot(str)
        def update(self, payload):
            try:
                overlay.sig_windows.emit(payload)
            except (ValueError, KeyError) as exc:
                print(f"[follow] неверные данные {payload[:80]!r}: {exc}", file=sys.stderr)

    bus = QDBusConnection.sessionBus()
    if not bus.registerService(FOLLOW_SERVICE):
        print("[follow] не удалось занять имя D-Bus (уже запущена другая копия?)", file=sys.stderr)
        return lambda: None
    service = Service()
    MprisWatcher(overlay.sig_mpris.emit).start()
    overlay.follow_service = service    # иначе сборщик мусора уничтожит объект и путь исчезнет из D-Bus
    bus.registerObject(FOLLOW_PATH, service, QDBusConnection.RegisterOption.ExportAllSlots)
    script = os.path.join(os.path.expanduser("~/.cache"), APP_ID, "follow.js")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(KWIN_FOLLOW_JS % {"classes": json.dumps(cfg["browser_classes"]), "svc": FOLLOW_SERVICE,
                                  "path": FOLLOW_PATH, "iface": FOLLOW_IFACE})
    qd = ["qdbus6", "org.kde.KWin"]
    subprocess.run(qd + ["/Scripting", "org.kde.kwin.Scripting.unloadScript", KWIN_FOLLOW_SCRIPT],
                   capture_output=True, check=False)
    res = subprocess.run(qd + ["/Scripting", "org.kde.kwin.Scripting.loadScript", script, KWIN_FOLLOW_SCRIPT],
                         capture_output=True, text=True, check=False)
    sid = res.stdout.strip()
    if res.returncode != 0 or not sid.lstrip("-").isdigit() or int(sid) < 0:
        print(f"[follow] скрипт KWin не загружен: {res.stderr.strip() or res.stdout.strip()}", file=sys.stderr)
    else:
        subprocess.run(qd + [f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"], check=False)
        print("[follow] субтитры привязаны к окну браузера (KWin)", flush=True)

    def cleanup():
        subprocess.run(qd + ["/Scripting", "org.kde.kwin.Scripting.unloadScript", KWIN_FOLLOW_SCRIPT],
                       capture_output=True, check=False)
        bus.unregisterService(FOLLOW_SERVICE)

    return cleanup


def run_gui(cfg, on_start, on_quit, test_text=False):
    if cfg["qt_platform"]:
        os.environ.setdefault("QT_QPA_PLATFORM", cfg["qt_platform"])
    use_layer_shell = prepare_layer_shell(cfg)
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication
    QGuiApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_ID)
    overlay = make_overlay_class()(cfg, use_layer_shell)
    overlay.show()
    if cfg["follow_window"] and use_layer_shell:
        app.aboutToQuit.connect(start_follow(cfg, overlay))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    tick = QTimer()          # даёт интерпретатору обработать сигналы
    tick.start(200)
    tick.timeout.connect(lambda: None)
    app.aboutToQuit.connect(on_quit)
    if test_text:
        def show_test():    # без автоочистки; повтор — после того, как KWin сообщит геометрию окна
            overlay.set_text("Қазақ тілінде ұзақ тест субтитрі: " + "бұл өте ұзақ сөйлем, " * 4,
                             "Тестовые субтитры на русском языке: " + "это очень длинное предложение, " * 6)
        show_test()
        QTimer.singleShot(2500, show_test)
    else:
        on_start(lambda kk, ru: overlay.sig_text.emit(kk, ru), lambda s, sec=0: overlay.sig_status.emit(s, sec),
                 lambda st: overlay.sig_state.emit(st))
    return app.exec()


# ------------------------------ KWin ------------------------------
def install_kwin_rule(cfg):
    """Правило KWin: поверх всех окон, без рамки/фокуса/панели задач, стартовая позиция внизу экрана."""
    if cfg["qt_platform"]:
        os.environ.setdefault("QT_QPA_PLATFORM", cfg["qt_platform"])
    from PyQt6.QtGui import QGuiApplication
    app = QGuiApplication([])  # noqa: F841 (объект должен жить, пока читаем экран)
    geo = QGuiApplication.primaryScreen().geometry()
    width = int(geo.width() * cfg["window_width_frac"])
    height = int((cfg["font_size_kk"] * 2 + cfg["font_size_ru"] * 3) * 1.5) + 40
    x = geo.x() + (geo.width() - width) // 2
    y = geo.y() + geo.height() - height - cfg["bottom_margin_px"]

    def kw(*a):
        subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", *a], check=True)

    def kr(*a):
        return subprocess.run(["kreadconfig6", "--file", "kwinrulesrc", *a],
                              capture_output=True, text=True, check=True).stdout.strip()

    rules = [r for r in kr("--group", "General", "--key", "rules").split(",") if r]
    if KWIN_RULE not in rules:
        rules.append(KWIN_RULE)
        kw("--group", "General", "--key", "rules", ",".join(rules))
        kw("--group", "General", "--key", "count", str(len(rules)))
    settings = {
        "Description": "Live Subtitles: поверх всех окон",
        "wmclass": APP_ID, "wmclassmatch": "1", "wmclasscomplete": "false",
        "above": "true", "aboverule": "2",
        "skiptaskbar": "true", "skiptaskbarrule": "2",
        "skippager": "true", "skippagerrule": "2",
        "skipswitcher": "true", "skipswitcherrule": "2",
        "noborder": "true", "noborderrule": "2",
        "acceptfocus": "false", "acceptfocusrule": "2",
        "position": f"{x},{y}", "positionrule": "3",
    }
    for key, value in settings.items():
        kw("--group", KWIN_RULE, "--key", key, value)
    subprocess.run(["qdbus6", "org.kde.KWin", "/KWin", "reconfigure"], check=False)
    print(f"[kwin] правило '{KWIN_RULE}' установлено (позиция {x},{y})")


# ------------------------------ проверка ------------------------------
def self_check(cfg):
    ok = True
    print(f"torch {torch.__version__}, CUDA доступна: {torch.cuda.is_available()}")
    if not torch.cuda.is_available():
        return 1
    print(f"GPU: {torch.cuda.get_device_name(0)}, cuDNN {torch.backends.cudnn.version()}")
    asr = load_asr(cfg)
    print(f"VRAM занято torch: {torch.cuda.memory_allocated() / 2**30:.2f} ГиБ")
    silence = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
    print(f"whisper на тишине -> {transcribe(asr, cfg, silence)!r} (ожидается пусто)")
    translate = make_translator(cfg)
    kk = "Сәлеметсіз бе, бүгін ауа райы өте жақсы."
    t0 = time.monotonic()
    print(f"перевод ({cfg['translator']}): {kk} -> {translate(kk)}  [{time.monotonic() - t0:.2f} с]")
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader"],
                         capture_output=True, text=True).stdout.strip()
    print(f"VRAM (nvidia-smi, всего на GPU): {out}")
    source = resolve_source(cfg)
    proc = subprocess.Popen(["parec", "-d", source, "--format=s16le", f"--rate={SAMPLE_RATE}",
                             "--channels=1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = proc.stdout.read(SAMPLE_RATE * 2)   # 1 с
    proc.terminate()
    rms = float(np.sqrt(np.mean((np.frombuffer(data, dtype=np.int16) / 32768.0) ** 2))) if data else 0.0
    print(f"parec ({source}): получено {len(data)} байт из {SAMPLE_RATE * 2}, RMS {rms:.4f}")
    ok = ok and len(data) == SAMPLE_RATE * 2
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="путь к config.toml")
    ap.add_argument("--whisper-model", help="large-v3-turbo | large-v3")
    ap.add_argument("--nllb-model", help="facebook/nllb-200-distilled-1.3B | ...-600M")
    ap.add_argument("--translator", choices=("llama", "nllb"), help="движок перевода")
    ap.add_argument("--llama-model", help="модель llama-server (секция models.ini)")
    ap.add_argument("--source", help="имя источника PulseAudio/PipeWire")
    ap.add_argument("--service", action="store_true", help="режим systemd-сервиса: модели грузятся при речи")
    ap.add_argument("--no-original", action="store_true", help="не показывать казахский оригинал")
    ap.add_argument("--list-sources", action="store_true")
    ap.add_argument("--check", action="store_true", help="проверить CUDA, модели и захват звука")
    ap.add_argument("--test-window", action="store_true", help="показать окно с тестовым текстом")
    ap.add_argument("--install-kwin-rule", action="store_true", help="установить правило KWin для окна")
    args = ap.parse_args()
    cfg = load_config(args)

    if args.list_sources:
        return subprocess.call(["pactl", "list", "short", "sources"])
    if args.check:
        return self_check(cfg)
    if args.install_kwin_rule:
        return install_kwin_rule(cfg)

    phrases = queue.Queue()
    capture = Capture(cfg, phrases)

    def on_start(emit, status, state):
        capture.on_state = state
        Worker(cfg, phrases, emit, status, state).start()
        capture.start()

    return run_gui(cfg, on_start, capture.stop, test_text=args.test_window)


if __name__ == "__main__":
    sys.exit(main())
