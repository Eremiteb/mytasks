#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import datetime as dt
import email
from email import policy
from email.utils import parseaddr
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from typing import Optional, Tuple


# ======================================================================
# LOG
# ======================================================================

def now_ts() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_log_path() -> Path:
    d = Path(__file__).resolve().parent
    log_dir = d / "logs"
    log_dir.mkdir(exist_ok=True)
    return log_dir / f"mht_to_fb2_{dt.datetime.now():%Y-%m-%d_%H%M%S}.jsonl"


def write_log(fp, *, level, event, src="", out="", msg="", detail=""):
    fp.write(json.dumps({
        "ts": now_ts(),
        "level": level,
        "event": event,
        "src": src,
        "out": out,
        "msg": msg,
        "detail": detail,
        "pid": os.getpid(),
    }, ensure_ascii=False) + "\n")
    fp.flush()


# ======================================================================
# SMART DECODER (КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ)
# ======================================================================

def decode_best(data: bytes, declared: Optional[str]) -> str:
    encs = []
    if declared:
        encs.append(declared)
    encs += ["utf-8", "cp1251", "windows-1251", "koi8-r", "latin-1"]

    best = None
    best_bad = None

    for enc in encs:
        try:
            txt = data.decode(enc, errors="replace")
        except Exception:
            continue
        bad = txt.count("�")
        if best_bad is None or bad < best_bad:
            best_bad = bad
            best = txt
            if bad == 0:
                break

    return best if best is not None else data.decode("utf-8", errors="replace")


def part_text_smart(part) -> Optional[str]:
    raw = part.get_payload(decode=True)
    if raw is None:
        return None
    return decode_best(raw, part.get_content_charset())


# ======================================================================
# TITLE FROM HTML
# ======================================================================

class TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.data = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, d):
        if self.in_title:
            self.data.append(d)

    def get(self):
        t = " ".join("".join(self.data).split())
        return t or None


def extract_title(html: str) -> Optional[str]:
    try:
        p = TitleParser()
        p.feed(html)
        return p.get()
    except Exception:
        return None


# ======================================================================
# MHT EXTRACTION
# ======================================================================

def extract_mht(src: Path) -> Tuple[str, str, Optional[str]]:
    msg = email.message_from_bytes(src.read_bytes(), policy=policy.default)

    subj = (msg.get("Subject") or "").strip()
    name, addr = parseaddr(msg.get("From") or "")
    author = (name or addr).strip() or None

    html = None
    text = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = (part.get_content_type() or "").lower()
            if ct == "text/html" and html is None:
                html = part_text_smart(part)
            elif ct == "text/plain" and text is None:
                text = part_text_smart(part)
    else:
        html = part_text_smart(msg)

    if not html and text:
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"<pre>{esc}</pre>"

    if not html:
        raise ValueError("HTML не найден в MHT")

    title = extract_title(html) or subj or src.stem
    return html, title.strip(), author


# ======================================================================
# PANDOC
# ======================================================================

def run_pandoc(html: Path, out: Path, title: str, author: Optional[str]):
    cmd = ["pandoc", "-f", "html", "-t", "fb2", "--metadata", f"title={title}"]
    if author:
        cmd += ["--metadata", f"author={author}"]
    cmd += ["-o", str(out), str(html)]

    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())


# ======================================================================
# WALK (ONLINE OUTPUT)
# ======================================================================

def walk_files(root: Path, log_fp):
    def onerror(e):
        write_log(log_fp, level="error", event="walk_error", src=str(e.filename), detail=str(e))

    for d, _, files in os.walk(root, onerror=onerror):
        for f in files:
            lf = f.lower()
            if lf.endswith((".mht", ".mhtml", ".chm")):
                yield Path(d) / f


# ======================================================================
# MAIN
# ======================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print("Каталог не найден", file=sys.stderr)
        return 1

    if shutil.which("pandoc") is None:
        print("pandoc не найден", file=sys.stderr)
        return 2

    log_path = default_log_path()

    with log_path.open("w", encoding="utf-8") as log_fp:
        print("Найденные файлы (онлайн):")

        with tempfile.TemporaryDirectory(prefix="mht_to_fb2_") as td:
            td = Path(td)

            for src in walk_files(root, log_fp):
                ext = src.suffix.lower()

                # ---------- CHM rule ----------
                if ext == ".chm":
                    fb2 = src.with_suffix(".fb2")
                    print(f"[CHM] {src}")
                    if fb2.exists():
                        if args.dry_run:
                            print(f"  DRY-RUN: would delete {fb2}")
                        else:
                            fb2.unlink()
                            print(f"  deleted {fb2}")
                    continue

                # ---------- MHT ----------
                print(f"[MHT] {src}")
                out = src.with_suffix(".fb2")

                if out.exists() and not args.overwrite:
                    print("  skip (exists)")
                    continue

                try:
                    html, title, author = extract_mht(src)
                    tmp = td / (src.stem + ".html")
                    tmp.write_text(html, encoding="utf-8", errors="replace")

                    run_pandoc(tmp, out, title, author)

                    if not args.dry_run:
                        src.unlink()

                except Exception as e:
                    print(f"  ERROR: {e}", file=sys.stderr)
                    write_log(log_fp, level="error", event="failed", src=str(src), detail=str(e))

    print(f"\nLOG: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
