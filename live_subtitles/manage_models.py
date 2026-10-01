#!/usr/bin/env python3
"""Менеджер моделей live_subtitles: обновляет кандидатов, замеряет их на этом железе и выбирает лучших.

Команды:
  status     — профиль железа, текущий выбор и версии моделей;
  due        — код 0, если пора обновляться (срок вышел или изменилась конфигурация) и ничего не играет;
  update     — проверить обновления моделей распознавания на Hugging Face и сконвертировать новые;
  benchmark  — замерить кандидатов (FLEURS kk->ru) и записать лучший выбор в selected.json;
  prune      — удалить то, что не нужно: проигравших кандидатов и необработанные веса (после выбора);
  run        — update + benchmark (для таймера).

Кандидаты — только из models.toml. Выбор учитывает фактическую конфигурацию: замеры идут на этой GPU при загруженном
Whisper (как в работе), а смена железа, драйвера, сборки llama.cpp или пресетов запускает пересчёт.
"""
import argparse
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.expanduser("~/.local/state/live_subtitles")
CT2_DIR = "/var/lib/llama.cpp/whisper-ct2"
MODELS_INI = "/etc/llama.cpp/models.ini"
LLAMA_SERVER_BIN = "/opt/llama.cpp/bin/llama-server"
os.environ.setdefault("HF_HOME", "/var/lib/llama.cpp/hf-cache")
N_PHRASES = 60


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_spec():
    with open(os.path.join(HERE, "models.toml"), "rb") as fh:
        return tomllib.load(fh)


def read_json(name, default):
    try:
        with open(os.path.join(STATE_DIR, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(STATE_DIR, name))      # атомарно: сервис никогда не прочтёт половину файла


# ------------------------------ профиль железа ------------------------------
def run_out(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def hardware_profile():
    """Всё, от чего зависит выбор: смена любого поля означает «конфигурация изменилась — пересчитать»."""
    gpu = run_out("nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader")
    mem = re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read())
    cpu = re.search(r"Model name:\s*(.+)", run_out("lscpu"))
    ini = ""
    try:
        with open(MODELS_INI, "rb") as fh:
            ini = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        pass
    llama = run_out(LLAMA_SERVER_BIN, "--version")
    import importlib.metadata as md
    return {"gpu": gpu, "ram_mb": int(mem.group(1)) // 1024 if mem else 0, "cpu": cpu.group(1) if cpu else "",
            "models_ini": ini, "llama_cpp": hashlib.sha256(llama.encode()).hexdigest()[:12] if llama else "",
            "ctranslate2": md.version("ctranslate2"), "faster_whisper": md.version("faster-whisper")}


def fingerprint(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------ обновление ------------------------------
def repo_info(repo):
    from huggingface_hub import HfApi
    return HfApi().model_info(repo)


def safe_to_convert(info):
    """Конвертируем только безопасные полные модели: safetensors, без чистых LoRA-адаптеров и pickle-весов."""
    files = {s.rfilename for s in info.siblings}
    has_full = any(f.endswith(".safetensors") and "adapter" not in f for f in files)
    return has_full and "adapter_config.json" not in files


def convert_repo(entry, sha):
    target = os.path.join(CT2_DIR, f"{entry['name']}-{sha[:8]}")
    if os.path.exists(os.path.join(target, "model.bin")):
        return target
    log(f"конвертация {entry['repo']}@{sha[:8]} -> {target}")
    subprocess.run([os.path.join(HERE, "venv/bin/ct2-transformers-converter"), "--model", entry["repo"], "--revision", sha,
                    "--output_dir", target, "--quantization", "float16", "--force"], check=True)
    from huggingface_hub import hf_hub_download
    for name in ("preprocessor_config.json", "tokenizer.json"):    # нужны turbo (128 мел-каналов) и токенайзеру
        if not os.path.exists(os.path.join(target, name)):
            shutil.copy(hf_hub_download("openai/whisper-large-v3-turbo", name), os.path.join(target, name))
    return target


def prune_old(entry, keep_dir):
    dirs = sorted(glob.glob(os.path.join(CT2_DIR, entry["name"] + "-*")), key=os.path.getmtime, reverse=True)
    for old in dirs[2:]:                 # оставляем две последние версии
        if old != keep_dir:
            log(f"удаляю старую версию {old}")
            shutil.rmtree(old, ignore_errors=True)


def cmd_update(spec):
    versions = read_json("versions.json", {})
    for entry in spec.get("asr", []):
        name = entry["name"]
        try:
            if entry["kind"] == "faster-whisper":
                from faster_whisper.utils import download_model
                path = download_model(entry["repo"])
                versions[name] = {"path": entry["repo"], "cache": path, "checked": int(time.time())}
                log(f"{name}: кэш faster-whisper актуален")
                continue
            info = repo_info(entry["repo"])
            known = versions.get(name, {})
            if known.get("sha") == info.sha and os.path.exists(os.path.join(known.get("path", ""), "model.bin")):
                log(f"{name}: без изменений ({info.sha[:8]})")
                continue
            if not safe_to_convert(info):
                log(f"{name}: пропуск — нет полной safetensors-модели (LoRA/pickle), конвертация небезопасна")
                continue
            target = convert_repo(entry, info.sha)
            link = os.path.join(CT2_DIR, name)
            if os.path.islink(link) or os.path.exists(link):
                (os.remove if os.path.islink(link) else shutil.rmtree)(link)
            os.symlink(target, link)
            versions[name] = {"repo": entry["repo"], "sha": info.sha, "path": target, "checked": int(time.time())}
            prune_old(entry, target)
            log(f"{name}: обновлено до {info.sha[:8]}")
        except Exception as exc:   # noqa: BLE001 — сбой одного кандидата не должен ронять остальные
            log(f"{name}: ошибка обновления: {exc}")
    write_json("versions.json", versions)


# ------------------------------ данные FLEURS и метрики ------------------------------
def load_fleurs():
    import numpy as np
    import pyarrow.parquet as pq
    import soundfile as sf
    from huggingface_hub import hf_hub_download
    kk = pq.read_table(hf_hub_download("google/fleurs", "parquet-data/kk_kz/test-00000-of-00001.parquet",
                                       repo_type="dataset")).to_pylist()
    ru = {r["id"]: r["raw_transcription"] for r in pq.read_table(
        hf_hub_download("google/fleurs", "parquet-data/ru_ru/test-00000-of-00001.parquet",
                        repo_type="dataset")).to_pylist()}
    audio, ref_kk, ref_ru = [], [], []
    for r in kk[::max(1, len(kk) // N_PHRASES)][:N_PHRASES]:
        x, _ = sf.read(io.BytesIO(r["audio"]["bytes"]), dtype="float32")
        audio.append(x.mean(1) if x.ndim > 1 else x)
        ref_kk.append(r["transcription"])
        ref_ru.append(ru.get(r["id"]))
    return audio, ref_kk, ref_ru


def norm(text):
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def gpu_used_mb():
    out = run_out("nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits")
    return int(out.splitlines()[0]) if out else 0


def asr_model_path(entry, versions):
    if entry["kind"] == "faster-whisper":
        return entry["repo"]
    path = versions.get(entry["name"], {}).get("path", "")
    return path if os.path.exists(os.path.join(path, "model.bin")) else None


# ------------------------------ замеры ------------------------------
def bench_asr(spec, versions, audio, ref_kk):
    import jiwer
    from faster_whisper import WhisperModel
    results = []
    for entry in spec.get("asr", []):
        path = asr_model_path(entry, versions)
        if not path:
            log(f"ASR {entry['name']}: нет локальной модели, пропуск")
            continue
        base = gpu_used_mb()
        model = WhisperModel(path, device="cuda", compute_type="int8_float16")
        vram = gpu_used_mb() - base
        hyps, t0 = [], time.time()
        for a in audio:
            segs, _ = model.transcribe(a, language="kk", beam_size=5, temperature=0.0, condition_on_previous_text=False,
                                       vad_filter=False, without_timestamps=True)
            hyps.append(" ".join(s.text.strip() for s in segs).strip())
        dt = time.time() - t0
        wer = 100 * jiwer.wer([norm(r) for r in ref_kk], [norm(h) for h in hyps])
        res = {"name": entry["name"], "path": path, "WER": round(wer, 2),
               "RTF": round(dt / (sum(len(a) for a in audio) / 16000), 4), "vram_mb": vram, "hyps": hyps}
        log(f"ASR {res['name']}: WER {res['WER']}%  RTF {res['RTF']}  VRAM +{vram} МБ")
        results.append(res)
        del model
    return results


def llama_sections(url):
    with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=15) as resp:
        return {m["id"] for m in json.load(resp)["data"]}


def bench_mt(spec, asr_best, pairs, cfg):
    import numpy as np
    import sacrebleu
    import subtitles as S
    from faster_whisper import WhisperModel
    resident = WhisperModel(asr_best["path"], device="cuda", compute_type="int8_float16")   # как в работе: Whisper в VRAM
    try:
        available = llama_sections(cfg["llama_url"])
    except OSError as exc:
        log(f"llama-server недоступен ({exc}): замеряю только локальные модели")
        available = set()
    refs = [r for _, r in pairs]
    results = []
    for entry in spec.get("mt", []):
        try:
            if entry["kind"] == "llama":
                if entry["name"] not in available:
                    log(f"MT {entry['name']}: нет в llama-server (models.ini), пропуск")
                    continue
                tr = S.LlamaTranslator(dict(cfg, llama_model=entry["name"], llama_timeout_sec=120, llama_load_timeout_sec=600))
                tr.warmup()
            else:
                tr = S.NllbTranslator(dict(cfg, nllb_model=entry["repo"], nllb_beams=2))
            out, times = [], []
            for h, _ in pairs:
                t0 = time.time()
                out.append(tr(h))
                times.append(time.time() - t0)
            chrf = sacrebleu.corpus_chrf(out, [refs]).score
            res = {"name": entry["name"], "kind": entry["kind"], "chrF": round(chrf, 2),
                   "mean_s": round(float(np.mean(times)), 2), "p90_s": round(float(np.percentile(times, 90)), 2)}
            log(f"MT {res['name']}: chrF {res['chrF']}  среднее {res['mean_s']} с  p90 {res['p90_s']} с")
            results.append(res)
            if entry["kind"] == "llama":
                S.unload_llama_model(dict(cfg, llama_model=entry["name"]))
            else:
                import torch
                del tr
                torch.cuda.empty_cache()
        except Exception as exc:   # noqa: BLE001
            log(f"MT {entry['name']}: ошибка замера: {exc}")
    del resident
    return results


def choose(results, key_good, key_speed, limit_key, limit, tie):
    """Лучший по качеству среди укладывающихся в лимит; при ничьей (в пределах tie) — более быстрый."""
    ok = [r for r in results if r[limit_key] <= limit]
    if not ok:
        return None
    best = max(ok, key=key_good)
    close = [r for r in ok if abs(key_good(r) - key_good(best)) <= tie]
    return min(close, key=key_speed)


def cmd_benchmark(spec):
    import subtitles as S
    sel = spec["selection"]
    versions = read_json("versions.json", {})
    profile = hardware_profile()
    log("загрузка тестового набора FLEURS")
    audio, ref_kk, ref_ru = load_fleurs()
    asr_results = bench_asr(spec, versions, audio, ref_kk)
    # качество: меньше WER лучше -> инвертируем; ничья — до 0,3 п.п. WER
    best_asr = choose(asr_results, key_good=lambda r: -r["WER"], key_speed=lambda r: r["RTF"],
                      limit_key="RTF", limit=sel["asr_max_rtf"], tie=0.3)
    if not best_asr:
        log("ни один вариант распознавания не подошёл — выбор не меняю")
        return 1
    pairs = [(h, r) for h, r in zip(best_asr["hyps"], ref_ru) if r]
    cfg = dict(S.DEFAULTS)
    mt_results = bench_mt(spec, best_asr, pairs, cfg)
    best_mt = choose(mt_results, key_good=lambda r: r["chrF"], key_speed=lambda r: r["p90_s"],
                     limit_key="p90_s", limit=sel["mt_max_p90_sec"], tie=sel["mt_tie_chrf"])
    link = os.path.join(CT2_DIR, best_asr["name"])     # стабильный путь: обновления подхватываются без правки выбора
    selected = {"whisper_model": link if os.path.exists(link) else best_asr["path"],
                "whisper_beam_size": 5, "whisper_compute_type": "int8_float16"}
    if best_mt:
        if best_mt["kind"] == "llama":
            selected.update(translator="llama", llama_model=best_mt["name"])
        else:
            entry = next(e for e in spec["mt"] if e["name"] == best_mt["name"])
            selected.update(translator="nllb", nllb_model=entry["repo"])
    else:
        log("ни один переводчик не уложился в лимит задержки — перевод не меняю")
    selected["_meta"] = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "fingerprint": fingerprint(profile),
                         "asr": {k: v for k, v in best_asr.items() if k != "hyps"},
                         "mt": best_mt}
    write_json("selected.json", {k: v for k, v in selected.items()})
    write_json("last_run.json", {"time": int(time.time()), "fingerprint": fingerprint(profile), "profile": profile})
    write_json(f"bench-{time.strftime('%Y%m%d-%H%M')}.json", {
        "profile": profile, "asr": [{k: v for k, v in r.items() if k != "hyps"} for r in asr_results], "mt": mt_results,
        "selected": selected})
    log(f"выбрано: ASR {best_asr['name']} (WER {best_asr['WER']}%), MT {best_mt['name'] if best_mt else '—'}")
    return 0


# ------------------------------ очистка ------------------------------
KEEP_HF_REPOS = {"mobiuslabsgmbh/faster-whisper-large-v3-turbo", "openai/whisper-large-v3-turbo"}   # LID и конфиги конвертации


def dir_size_mb(path):
    return int(sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs
                   if os.path.exists(os.path.join(d, f))) / 1048576)


def cmd_prune(spec):
    """Удаляет то, что не нужно в работе: проигравших кандидатов и необработанные веса. Общий blobs трогает только HF."""
    if not spec["selection"].get("prune_unused", False):
        log("очистка отключена (prune_unused = false)")
        return 0
    selected = read_json("selected.json", {})
    if not selected.get("whisper_model"):
        log("очистка пропущена: выбора ещё нет")
        return 0
    keep_asr = os.path.basename(selected["whisper_model"].rstrip("/"))
    keep_asr = re.sub(r"-[0-9a-f]{8}$", "", keep_asr)
    cands = [e["name"] for e in spec.get("asr", []) if e["kind"] == "ct2-convert"]
    freed = 0
    # 1) готовые CTranslate2-модели: оставляем только выбранную (и её версионные каталоги)
    for entry in sorted(os.listdir(CT2_DIR)) if os.path.isdir(CT2_DIR) else []:
        base = re.sub(r"-[0-9a-f]{8}$", "", entry)
        if base == keep_asr:
            continue
        path = os.path.join(CT2_DIR, entry)
        size = 0 if os.path.islink(path) else dir_size_mb(path)
        (os.remove if os.path.islink(path) else shutil.rmtree)(path)
        freed += size
        log(f"удалено {path} ({size} МБ)")
    versions = {k: v for k, v in read_json("versions.json", {}).items()
                if k == keep_asr or k not in cands}
    write_json("versions.json", versions)
    # 2) кэш Hugging Face: необработанные веса конвертированных моделей, лишние репозитории, NLLB если не выбран
    drop = {e["repo"] for e in spec.get("asr", []) if e["kind"] == "ct2-convert"} | set(spec["selection"].get("purge_repos", []))
    drop |= {e["repo"] for e in spec.get("mt", []) if e["kind"] == "nllb" and selected.get("nllb_model") != e["repo"]}
    drop -= KEEP_HF_REPOS
    from huggingface_hub import scan_cache_dir
    info = scan_cache_dir()
    hashes = [rev.commit_hash for repo in info.repos if repo.repo_type == "model" and repo.repo_id in drop
              for rev in repo.revisions]
    if hashes:
        strategy = info.delete_revisions(*hashes)
        log(f"кэш Hugging Face: удаляю {len(hashes)} ревизий, освободится {strategy.expected_freed_size_str}")
        strategy.execute()
    else:
        log("кэш Hugging Face: удалять нечего")
    freed += prune_hf_leftovers()
    log(f"очистка завершена, освобождено ещё около {freed} МБ (без учёта кэша HF)")
    return 0


FLEURS_KEEP = {"kk_kz", "ru_ru"}       # для замеров нужны только казахский (речь) и русский (эталон перевода)


def prune_hf_leftovers():
    """Остатки прерванных загрузок (*.incomplete) и лишние языки FLEURS в кэше Hugging Face."""
    hub = os.path.join(os.environ["HF_HOME"], "hub")
    freed = 0
    for path in glob.glob(os.path.join(hub, "**", "*.incomplete"), recursive=True):
        freed += os.path.getsize(path) // 1048576
        os.remove(path)
        log(f"удалён остаток загрузки {os.path.basename(path)[:24]}…")
    for link in glob.glob(os.path.join(hub, "datasets--google--fleurs", "snapshots", "*", "parquet-data", "*", "*")):
        lang = os.path.basename(os.path.dirname(link))
        if lang in FLEURS_KEEP:
            continue
        target = os.path.realpath(link)
        if os.path.isfile(target):
            freed += os.path.getsize(target) // 1048576
            os.remove(target)
        os.remove(link)
        log(f"удалён лишний язык FLEURS {lang}")
    return freed


# ------------------------------ прочие команды ------------------------------
def cmd_due(spec):
    last = read_json("last_run.json", {})
    profile = hardware_profile()
    age_days = (time.time() - last.get("time", 0)) / 86400
    changed = last.get("fingerprint") != fingerprint(profile)
    if not changed and age_days < spec["selection"]["update_every_days"]:
        log(f"не пора: последний расчёт {age_days:.1f} дн. назад, конфигурация та же")
        return 1
    import subtitles as S
    if S.MprisWatcher(lambda _t: None).playing_title():
        log("сейчас что-то играет — откладываю, чтобы не прерывать перевод")
        return 1
    log("пора: " + ("конфигурация изменилась" if changed else f"прошло {age_days:.1f} дн."))
    return 0


def cmd_status(spec):
    print(json.dumps({"profile": hardware_profile(), "selected": read_json("selected.json", {}),
                      "versions": read_json("versions.json", {})}, ensure_ascii=False, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("status", "due", "update", "benchmark", "prune", "run"))
    args = ap.parse_args()
    spec = load_spec()
    sys.path.insert(0, HERE)
    if args.command == "status":
        return cmd_status(spec)
    if args.command == "due":
        return cmd_due(spec)
    if args.command == "prune":
        return cmd_prune(spec)
    if args.command in ("update", "run"):
        cmd_update(spec)
    if args.command in ("benchmark", "run"):
        rc = cmd_benchmark(spec)
        if rc == 0 and args.command == "run":
            cmd_prune(spec)      # убрать проигравших и необработанные веса
        return rc
    return 0


if __name__ == "__main__":
    code = main()
    # После замеров CUDA/ctranslate2 иногда зависают на завершении интерпретатора — выходим сразу (выбор уже записан).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(code or 0))
