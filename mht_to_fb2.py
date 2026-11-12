#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import datetime as dt
import email
from email import policy
from email.message import Message
from email.utils import parseaddr
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from typing import IO, Iterator, Optional, Tuple

# Предельное время одного запуска pandoc, секунды (зависший разбор не блокирует обход).
PANDOC_TIMEOUT_SEC = 300


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


def write_log(fp: IO[str], *, level: str, event: str, src: str = "", out: str = "",
              msg: str = "", detail: str = "") -> None:
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
        except LookupError:
            # неизвестное имя кодировки в заголовке письма — пробуем следующую
            continue
        bad = txt.count("�")
        if best_bad is None or bad < best_bad:
            best_bad = bad
            best = txt
            if bad == 0:
                break

    return best if best is not None else data.decode("utf-8", errors="replace")


def part_text_smart(part: Message) -> Optional[str]:
    raw = part.get_payload(decode=True)
    if raw is None:
        return None
    return decode_best(raw, part.get_content_charset())


# ======================================================================
# TITLE FROM HTML
# ======================================================================

class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.data: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        _ = attrs
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.data.append(data)

    def get(self) -> Optional[str]:
        t = " ".join("".join(self.data).split())
        return t or None


def extract_title(html: str) -> Optional[str]:
    try:
        p = TitleParser()
        p.feed(html)
        return p.get()
    except (AssertionError, ValueError):
        # HTMLParser бросает AssertionError/ValueError на битой разметке
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

def run_pandoc(html: Path, out: Path, title: str, author: Optional[str],
               timeout: int = PANDOC_TIMEOUT_SEC) -> None:
    # --sandbox: pandoc не читает локальные файлы и не обращается к сети при
    # встраивании картинок из недоверенного HTML.
    cmd = ["pandoc", "--sandbox", "-f", "html", "-t", "fb2", "--metadata", f"title={title}"]
    if author:
        cmd += ["--metadata", f"author={author}"]
    cmd += ["-o", str(out), str(html)]

    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"pandoc: тайм-аут {timeout} с") from e
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())


# ======================================================================
# WALK (ONLINE OUTPUT)
# ======================================================================

def walk_files(root: Path, log_fp: IO[str]) -> Iterator[Path]:
    def onerror(e: OSError) -> None:
        write_log(log_fp, level="error", event="walk_error", src=str(e.filename), detail=str(e))

    for d, _, files in os.walk(root, onerror=onerror):
        for f in files:
            lf = f.lower()
            if lf.endswith((".mht", ".mhtml", ".chm")):
                yield Path(d) / f


def convert_one(src: Path, out: Path, tmp_dir: Path, dry_run: bool, timeout: int) -> None:
    """Конвертирует один .mht в .fb2; исходник удаляет только после успешной записи.

    В режиме dry-run ничего не пишется: ни временный HTML, ни .fb2.
    """
    html, title, author = extract_mht(src)
    if dry_run:
        print(f"  DRY-RUN: would convert {src} -> {out} (title={title!r})")
        print(f"  DRY-RUN: would delete {src}")
        return

    tmp = tmp_dir / (src.stem + ".html")
    tmp.write_text(html, encoding="utf-8", errors="replace")
    # pandoc пишет во временный файл рядом с целью: сбой не оставляет битый .fb2
    part = out.with_name(out.name + ".part")
    try:
        run_pandoc(tmp, part, title, author, timeout)
        if not part.is_file() or part.stat().st_size == 0:
            raise RuntimeError("pandoc не создал .fb2 или результат пуст")
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)

    if out.is_file() and out.stat().st_size > 0:
        src.unlink()


# ======================================================================
# MAIN
# ======================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--timeout", type=int, default=PANDOC_TIMEOUT_SEC,
                    help="тайм-аут одного запуска pandoc, секунды (по умолчанию %(default)s)")
    args = ap.parse_args()

    if args.timeout <= 0:
        ap.error("--timeout должен быть положительным")

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
                    convert_one(src, out, td, args.dry_run, args.timeout)
                except (OSError, ValueError, RuntimeError) as e:
                    print(f"  ERROR: {e}", file=sys.stderr)
                    write_log(log_fp, level="error", event="failed", src=str(src), detail=str(e))

    print(f"\nLOG: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
