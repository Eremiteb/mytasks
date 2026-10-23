#!/usr/bin/env python3
"""Статус локального llama.cpp одним вызовом — источник `llamactl status`.

Собирает тот же JSON, что печатает `llamactl.sh status --json`: состояние
сервисов, ответ роутера и FIM, модели роутера, VRAM и процессы на GPU, RAM,
место на диске моделей и сторонние llama-server. Только стандартная
библиотека: роутер и FIM опрашиваются по HTTP (urllib), процессы, пользователи
и память читаются из /proc, место на диске — через statvfs, видеопамять —
напрямую из библиотеки драйвера NVML (ctypes, libnvidia-ml.so.1; без неё —
`nvidia-smi`). Внешний процесс один: `systemctl is-active` на оба юнита.
Прежний вариант на bash запускал 61–73 процесса и тратил ~160 мс CPU на
вызов, а трей вызывает статус каждые 5 с. LLAMACTL_NO_NVML=1 отключает NVML.

Используется треем (`llamactl_tray.py`, импорт модуля) и `llamactl.sh status`
(запуск скрипта с параметрами из уже прочитанного conf/llamactl.conf).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Значения по умолчанию — как в llamactl.sh (переопределяются conf/llamactl.conf)
DEFAULTS = {
    "ROUTER_URL": "http://127.0.0.1:8080",
    "FIM_URL": "http://127.0.0.1:8012",
    "SERVER_UNIT": "llama-server",
    "FIM_UNIT": "llama-fim",
    "SERVICE_USER": "llama",
    "MODELS_DIR": "/var/lib/llama.cpp/models",
}

LLAMA_SERVER_RE = re.compile(r"(^|/)llama-server(\s|$)")
LLAMA_SERVE_RE = re.compile(r"(^|/)llama\s+serve(\s|$)")
PORT_RE = re.compile(r"--port[ =]([0-9]+)")
CTX_RE = re.compile(r"--ctx-size ([0-9]+)")
ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_conf(path: Path) -> dict[str, str]:
    """Простые присваивания KEY="value" из shell-конфига (без его выполнения)."""
    conf = dict(DEFAULTS)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return conf
    for line in lines:
        m = ASSIGN_RE.match(line.strip())
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        conf[m.group(1)] = os.path.expandvars(value)
    return conf


def http_get(url: str, timeout: float) -> bytes | None:
    try:
        # Только адреса роутера и FIM из conf/llamactl.conf (локальные)
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (OSError, ValueError, urllib.error.URLError):
        return None


def models(router_url: str) -> list[dict]:
    """Модели роутера: [{id, status, ctx}] (ctx — строка, как в прежнем jq)."""
    raw = http_get(f"{router_url}/models", 10)
    if raw is None:
        return []
    try:
        data = json.loads(raw).get("data", [])
    except (ValueError, AttributeError):
        return []
    out = []
    for item in data:
        status = item.get("status") or {}
        args = " ".join(status.get("args") or [])
        m = CTX_RE.search(args)
        out.append({"id": item.get("id"), "status": status.get("value") or "unknown",
                    "ctx": m.group(1) if m else None})
    return out


def unit_states(units: list[str]) -> list[str]:
    """Состояние юнитов одним вызовом systemctl; нет ответа — unknown."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return ["unknown"] * len(units)
    res = subprocess.run([systemctl, "is-active", *units], capture_output=True, text=True, timeout=10, check=False)
    states = [s.strip() or "unknown" for s in res.stdout.splitlines()]
    return (states + ["unknown"] * len(units))[: len(units)]


def proc_info(pid: int, proc_root: Path) -> tuple[str, str] | None:
    """Пользователь и командная строка процесса из /proc (как ps -o user=,args=)."""
    base = proc_root / str(pid)
    try:
        raw = (base / "cmdline").read_bytes()
        uid = base.stat().st_uid
    except OSError:
        return None
    args = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    return user, args


def is_llama_server(args: str) -> bool:
    return bool(LLAMA_SERVER_RE.search(args) or LLAMA_SERVE_RE.search(args))


def foreign(service_user: str, proc_root: Path) -> list[dict]:
    """Сторонние llama-server / llama serve (не пользователя сервиса)."""
    out = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        info = proc_info(int(entry.name), proc_root)
        if not info or not info[1]:
            continue
        user, args = info
        if user == service_user or not is_llama_server(args):
            continue
        m = PORT_RE.search(args)
        out.append({"pid": int(entry.name), "user": user, "port": m.group(1) if m else "-", "cmd": args[:160]})
    out.sort(key=lambda p: p["pid"])
    return out


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong), ("reserved", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlProc(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong),
                ("gpuInstanceId", ctypes.c_uint), ("computeInstanceId", ctypes.c_uint)]


_NVML: dict = {}
MIB = 1024 ** 2


def _nvml_handle():
    """Дескриптор первой GPU через NVML (инициализация один раз на процесс)."""
    if "handle" in _NVML:
        return _NVML["handle"]
    _NVML["handle"] = None
    if os.environ.get("LLAMACTL_NO_NVML") == "1":
        return None
    try:
        lib = ctypes.CDLL("libnvidia-ml.so.1")
        handle = ctypes.c_void_p()
        if lib.nvmlInit_v2() != 0 or lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            return None
    except (OSError, AttributeError):
        return None
    _NVML["lib"] = lib
    _NVML["handle"] = handle
    return handle


def _nvml_query() -> tuple[int, int, int, list[tuple[int, int]]] | None:
    """VRAM used/total (МиБ), загрузка (%), процессы [(pid, МиБ)] или None."""
    handle = _nvml_handle()
    if handle is None:
        return None
    lib = _NVML["lib"]
    mem = _NvmlMemory()
    mem.version = ctypes.sizeof(_NvmlMemory) | (2 << 24)
    util = _NvmlUtil()
    count = ctypes.c_uint(64)
    procs = (_NvmlProc * 64)()
    try:
        if lib.nvmlDeviceGetMemoryInfo_v2(handle, ctypes.byref(mem)) != 0:
            return None
        if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) != 0:
            util.gpu = 0
        if lib.nvmlDeviceGetComputeRunningProcesses_v3(handle, ctypes.byref(count), procs) != 0:
            count.value = 0
    except AttributeError:
        return None
    apps = [(procs[i].pid, procs[i].usedGpuMemory // MIB) for i in range(count.value)]
    return mem.used // MIB, mem.total // MIB, util.gpu, apps


def gpu(service_user: str, proc_root: Path) -> dict:
    """VRAM, загрузка и процессы на GPU: NVML, иначе nvidia-smi, иначе нули."""
    result = {"used_mib": 0, "total_mib": 0, "util": 0, "procs": []}
    nvml = _nvml_query()
    if nvml is not None:
        result["used_mib"], result["total_mib"], result["util"], apps = nvml
        for pid, mib in apps:
            result["procs"].append(_gpu_proc(pid, mib, service_user, proc_root))
        return result
    smi = shutil.which("nvidia-smi")
    if not smi:
        return result
    q = subprocess.run([smi, "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=10, check=False)
    first = q.stdout.splitlines()[0] if q.stdout.strip() else ""
    nums = [p.strip() for p in first.split(",")]
    for key, val in zip(("used_mib", "total_mib", "util"), nums, strict=False):
        result[key] = int(val) if val.isdigit() else 0
    apps = subprocess.run([smi, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=10, check=False)
    for line in apps.stdout.splitlines():
        if "," not in line:
            continue
        pid_s, mib_s = (p.strip() for p in line.split(",", 1))
        if not pid_s.isdigit():
            continue
        result["procs"].append(_gpu_proc(int(pid_s), int(mib_s) if mib_s.isdigit() else 0, service_user, proc_root))
    return result


def _gpu_proc(pid: int, mib: int, service_user: str, proc_root: Path) -> dict:
    user, args = proc_info(pid, proc_root) or ("", "")
    kind = "service" if user == service_user else ("foreign" if is_llama_server(args) or "llama serve" in args else "other")
    return {"pid": pid, "user": user, "mib": mib, "kind": kind, "cmd": args[:160]}


def ram(proc_root: Path) -> dict:
    """RAM и swap в байтах из /proc/meminfo (used = total − available, как free)."""
    info: dict[str, int] = {}
    try:
        for line in (proc_root / "meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                info[key] = int(parts[0]) * 1024
    except OSError:
        pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {"total": total, "used": max(total - avail, 0), "available": avail,
            "swap_used": max(info.get("SwapTotal", 0) - info.get("SwapFree", 0), 0)}


def disk_avail(path: str) -> int:
    try:
        st = os.statvfs(path)
    except OSError:
        return 0
    return st.f_bavail * st.f_frsize


def collect(conf: dict[str, str], proc_root: Path = Path("/proc")) -> dict:
    """Полный статус в формате `llamactl.sh status --json`."""
    server_state, fim_state = unit_states([conf["SERVER_UNIT"], conf["FIM_UNIT"]])
    router_url, fim_url = conf["ROUTER_URL"], conf["FIM_URL"]
    return {
        "services": {"server": server_state, "fim": fim_state},
        "router": {"up": http_get(f"{router_url}/health", 3) is not None, "url": router_url},
        "fim": {"up": http_get(f"{fim_url}/health", 3) is not None, "url": fim_url},
        "models": models(router_url),
        "gpu": gpu(conf["SERVICE_USER"], proc_root),
        "ram": ram(proc_root),
        "disk": {"models_avail": disk_avail(conf["MODELS_DIR"])},
        "foreign": foreign(conf["SERVICE_USER"], proc_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Статус llama.cpp в JSON (как llamactl status --json)")
    parser.add_argument("--conf", type=Path, default=Path(__file__).resolve().parent / "conf" / "llamactl.conf",
                        help="conf/llamactl.conf (по умолчанию рядом со скриптом)")
    for key in DEFAULTS:
        parser.add_argument("--" + key.lower().replace("_", "-"), dest=key, help=f"переопределить {key}")
    parser.add_argument("--proc", type=Path, default=Path("/proc"), help="корень procfs (для тестов)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="принимается для единообразия: скрипт и так ничего не меняет, только читает")
    opts = parser.parse_args(argv)
    conf = load_conf(opts.conf)
    for key in DEFAULTS:
        if getattr(opts, key):
            conf[key] = getattr(opts, key)
    print(json.dumps(collect(conf, opts.proc), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
