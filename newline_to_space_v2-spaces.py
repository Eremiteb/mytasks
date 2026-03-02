#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newline_to_space_v2-spaces.py

===============================================================================
ОПИСАНИЕ РАБОТЫ СКРИПТА
===============================================================================

Скрипт обрабатывает текстовые файлы и склеивает строки в абзацы по признаку
фиксированного количества ведущих пробелов (--spaces).

Ключевая идея:
- строки с ровно N ведущими пробелами считаются "началом абзаца" (base)
- все последующие строки без этого отступа приклеиваются к base
- строки вне base пишутся отдельно

Поддерживается атомарная запись, строгая проверка текстовости,
JSON-логирование и режим dry-run.

===============================================================================
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

ENCODINGS_CANDIDATES = ["utf-8", "utf-8-sig", "cp1251", "koi8-r", "latin-1"]
OFFSETS_SAMPLE_LIMIT_DEFAULT = 50
RE_MULTI_SPACES = re.compile(r" {2,}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="microseconds")


def safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except Exception:
        pass


def count_leading_spaces(s: str) -> int:
    i = 0
    while i < len(s) and s[i] == " ":
        i += 1
    return i


def normalize_line(s: str) -> str:
    s = s.lstrip(" \t")
    return RE_MULTI_SPACES.sub(" ", s)


def detect_newline_bytes(src: bytes) -> bytes:
    crlf = src.count(b"\r\n")
    lf = src.count(b"\n")
    return b"\r\n" if crlf and crlf >= (lf - crlf) else b"\n"


def ends_with_newline(src: bytes) -> bool:
    return src.endswith(b"\n")


# ---------------------------------------------------------------------------
# Text checks
# ---------------------------------------------------------------------------

def nul_report(data: bytes) -> Dict:
    offsets = []
    count = 0
    first = None
    for i, b in enumerate(data):
        if b == 0:
            count += 1
            if first is None:
                first = i
            if len(offsets) < OFFSETS_SAMPLE_LIMIT_DEFAULT:
                offsets.append(i)
    return {
        "count": count,
        "first_offset": first if first is not None else -1,
        "offsets_sample": offsets,
        "offsets_sample_limit": OFFSETS_SAMPLE_LIMIT_DEFAULT,
        "offsets_truncated": count > OFFSETS_SAMPLE_LIMIT_DEFAULT,
    }


def score_text(txt: str) -> Tuple[int, int]:
    ctrl = 0
    printable = 0
    for ch in txt:
        o = ord(ch)
        if ch in ("\n", "\r", "\t"):
            continue
        if o < 32 or o == 127:
            ctrl += 1
        else:
            printable += 1
    return (-ctrl, printable)


def auto_decode(data: bytes) -> Tuple[Optional[str], Optional[str], List[str]]:
    tried = []
    best = None
    best_enc = None
    best_score = None

    for enc in ENCODINGS_CANDIDATES:
        tried.append(enc)
        try:
            txt = data.decode(enc, errors="strict")
        except UnicodeDecodeError:
            continue
        score = score_text(txt)
        if best_score is None or score > best_score:
            best = txt
            best_enc = enc
            best_score = score

    return best, best_enc, tried


# ---------------------------------------------------------------------------
# Glue logic (ИЗМЕНЁННАЯ)
# ---------------------------------------------------------------------------

def spaces_filter_count(lines: List[str], spaces: int) -> int:
    return sum(
        1 for s in lines
        if s.strip(" ") and count_leading_spaces(s) == spaces
    )


def glue_lines(lines: List[str], spaces: int, events: List[Dict], explain: bool) -> List[str]:
    out: List[str] = []
    base: Optional[str] = None
    base_index = 0

    def flush(reason: str) -> None:
        nonlocal base, base_index
        if base is not None:
            out.append(normalize_line(base))
            if explain:
                events.append({
                    "ts": now_iso(),
                    "level": "info",
                    "event": "base_flushed",
                    "reason": reason,
                    "base_index": base_index,
                })
            base = None
            base_index += 1

    for content in lines:

        # --- blank (ИЗМЕНЕНО) ---
        if content.strip(" ") == "":
            flush("blank")
            base = None
            out.append("")
            continue

        lead = count_leading_spaces(content)

        # dash_no_indent
        if lead == 0 and content.startswith("-"):
            flush("dash_no_indent")
            out.append(normalize_line(content))
            continue

        # --- ОСНОВНОЕ ПРАВИЛО (ИЗМЕНЕНО) ---
        if lead == spaces:
            if base is None:
                base = content
            else:
                flush("new_base")
                base = content
        else:
            if base is None:
                out.append(normalize_line(content))
            else:
                base = base + " " + content

    flush("eof")
    return out


# ---------------------------------------------------------------------------
# File IO
# ---------------------------------------------------------------------------

def read_bytes(path: str) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        with open(path, "rb") as f:
            return f.read(), None
    except Exception as e:
        return None, str(e)


def write_bytes(path: str, data: bytes) -> Optional[str]:
    try:
        with open(path, "wb") as f:
            f.write(data)
        return None
    except Exception as e:
        return str(e)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process_one_file(path: str, spaces: int, dry_run: bool, explain: bool, events: List[Dict]) -> bool:
    tmp = path + ".tmp"
    work = path + ".tmp.work"

    safe_unlink(tmp)
    safe_unlink(work)

    src, err = read_bytes(path)
    if src is None:
        events.append({"ts": now_iso(), "event": "work_read_failed", "file": path, "detail": err})
        return False

    if b"\x00" in src:
        events.append({
            "ts": now_iso(),
            "event": "skipped_non_text",
            "file": path,
            "reason": "nul_byte_found",
            "rule": "text_check:nul_byte_forbidden",
            "nul": nul_report(src),
        })
        return False

    text, enc, tried = auto_decode(src)
    if text is None:
        events.append({
            "ts": now_iso(),
            "event": "skipped_non_text",
            "file": path,
            "reason": "decode_failed",
            "rule": "text_check:decode_auto_failed",
            "encodings_tried": tried,
        })
        return False

    lines = text.splitlines()
    cnt = spaces_filter_count(lines, spaces)
    ok = cnt > 5

    events.append({
        "ts": now_iso(),
        "event": "checked_spaces",
        "file": path,
        "ok": ok,
        "spaces": spaces,
        "str_col": cnt,
        "encoding": enc,
    })

    if not ok:
        return False

    out_lines = glue_lines(lines, spaces, events, explain)

    nl = detect_newline_bytes(src)
    keep_nl = ends_with_newline(src)

    out_text = "\n".join(out_lines)
    if nl == b"\r\n":
        out_text = out_text.replace("\n", "\r\n")
    if keep_nl and not out_text.endswith(nl.decode()):
        out_text += nl.decode()

    out_bytes = out_text.encode(enc)

    write_bytes(work, out_bytes)

    if out_bytes == src:
        safe_unlink(work)
        events.append({"ts": now_iso(), "event": "no_changes", "file": path})
        return True

    os.replace(work, tmp)
    events.append({"ts": now_iso(), "event": "tmp_written", "file": path})

    if dry_run:
        events.append({"ts": now_iso(), "event": "dry_run", "file": path})
        return True

    os.replace(tmp, path)
    events.append({"ts": now_iso(), "event": "file_updated", "file": path})
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="newline_to_space_v2-spaces.py",
        description="Склейка строк по числу ведущих пробелов (--spaces)",
    )
    p.add_argument("--spaces", type=int, help="количество ведущих пробелов")
    p.add_argument("--file")
    p.add_argument("--dir")
    p.add_argument("--ext", action="append")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--explain", action="store_true")
    return p


def main(argv: List[str]) -> int:
    if not argv:
        build_arg_parser().print_help()
        return 0

    args = build_arg_parser().parse_args(argv)

    if args.spaces is None or (args.file is None) == (args.dir is None):
        build_arg_parser().print_help()
        return 2

    events: List[Dict] = []
    log_path = os.path.join(
        script_dir(),
        f"newline_to_space_v2-spaces_{_dt.datetime.now():%Y-%m-%d_%H%M%S}.json",
    )

    events.append({"ts": now_iso(), "event": "start", "params": vars(args)})

    if args.file:
        process_one_file(args.file, args.spaces, args.dry_run, args.explain, events)
    else:
        exts = [".txt"] if not args.ext else [
            (e if e.startswith(".") else "." + e).lower()
            for x in args.ext for e in x.split(",")
        ]
        for root, _, files in os.walk(args.dir):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in exts:
                    process_one_file(os.path.join(root, fn), args.spaces, args.dry_run, args.explain, events)
            if args.no_recursive:
                break

    events.append({"ts": now_iso(), "event": "finish"})

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    if args.explain:
        print(f"[LOG] {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
