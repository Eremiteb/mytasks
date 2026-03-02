#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
newline_to_space_v1-fix-width.py

НАЗНАЧЕНИЕ
---------
Скрипт обрабатывает текстовые файлы формата hard-wrap / fixed-width: он склеивает
строки фиксированной длины (параметр --str-len) в более длинные строки (блоки/абзацы),
используя правила разделителей, и сохраняет результат через временные файлы .tmp/.work
(атомарно). Скрипт умеет работать как с одним файлом (--file), так и с каталогом (--dir)
с фильтром по расширениям (--ext), с выбором рекурсивного обхода.

КЛЮЧЕВЫЕ ПРИНЦИПЫ
-----------------
1) Вход читается как БАЙТЫ. До любых преобразований выполняется проверка текстовости:
   1.1) RULE: text_check:nul_byte_forbidden
        Если в файле встречается хотя бы один байт 0x00 (NUL), файл считается не текстовым
        и пропускается. В лог пишутся:
        - reason = "nul_byte_found"
        - rule   = "text_check:nul_byte_forbidden"
        - nul.count: общее число NUL
        - nul.first_offset: первый байтовый offset
        - nul.offsets_sample: первые N offsets (выборка)
        - nul.offsets_sample_limit: N
        - nul.offsets_truncated: True/False

   1.2) RULE: text_check:decode_auto_failed
        Если NUL не найден, файл пытаемся декодировать строго (errors="strict") одной из кодировок:
        ["utf-8", "utf-8-sig", "cp1251", "koi8-r", "latin-1"].
        Выбирается «лучшая» версия по эвристике (меньше управляющих символов, больше печатных).
        Если ни одна кодировка не подошла, файл пропускается и в лог пишутся:
        - reason = "decode_failed"
        - rule   = "text_check:decode_auto_failed"
        - encodings_tried = список кодировок

2) FIXED-WIDTH ФИЛЬТР (перед склейкой)
--------------------------------------
После успешного декодирования применяется фильтр “fixed-width”:
- Рассматриваются только строки, у которых в исходнике есть EOL (splitlines(True) и eol != "").
- Длина строки считается как len(content) без EOL.
- Пустые строки (len(content) == 0) НЕ учитываются в статистике.
- Считаются частоты длин строк и выбирается ТОП-5 наиболее частых длин, сортировка:
    count DESC, затем len ASC.
Файл проходит фильтр, если значение --str-len входит в ТОП-5.
В лог пишется событие checked_fixed_width с meta.top_lengths (ТОП-5).

3) СКЛЕЙКА ПО --str-len
-----------------------
Текст обрабатывается построчно (splitlines(True)), работает с content (без EOL).
Ведётся накопительная строка base (“база”), которая записывается в выход при flush.

Правила:
- Если len(content) == str_len:
    - если base отсутствует: base = content
    - иначе: base = base + " " + content
  ДОП. ПРАВИЛО ДЕФИСА:
    - если content заканчивается на "-":
        * "-" удаляется
        * перенос строки после content считается удалённым:
          следующая строка присоединяется вплотную (без пробела-разделителя).

- Разделители / условия сброса базы:
  A) blank:
     если content.strip(" ") == "" (пусто или только пробелы),
     то: flush базы (если есть), затем записать пустую строку.

  B) dash_no_indent:
     если нет ведущих пробелов (leading_spaces == 0) и строка начинается с "-",
     то: flush базы (если есть), затем записать строку отдельно.

  C) len_mismatch:
     если len(content) != str_len,
     то:
       - если base есть: content присоединяется (с учётом возможного “без пробела” после дефиса)
         и затем немедленно flush (строка завершает базу)
       - если base нет: записать строку отдельно

- В конце файла: flush базы.

4) НОРМАЛИЗАЦИЯ ПЕРЕД ЗАПИСЬЮ
-----------------------------
Перед записью в выход (и базы, и одиночных строк) применяется нормализация:
- удалить все начальные пробелы и табы: lstrip(" \t")
- заменить любые последовательности пробелов ' ' > 1 на один пробел
- табы внутри строки не преобразуются (кроме удаления ведущих)

5) TMP/WORK И АТОМАРНОСТЬ
-------------------------
На каждый входной файл используются:
- tmp_path  = <file>.tmp
- work_path = <file>.tmp.work

Перед обработкой молча (без логирования) удаляются <file>.tmp и <file>.tmp.work (если есть).

Результат пишется в .work (в той же кодировке, что выбрана при decode_auto, newline="\n"),
затем .work читается как байты и сравнивается с исходными байтами:
- Если байты совпали: удалить .work, НЕ создавать/НЕ обновлять .tmp, исходник не трогать,
  логировать event=no_changes.
- Если отличаются: os.replace(.work -> .tmp), логировать event=tmp_written.
  Далее:
  - --dry-run: исходник не менять, .tmp оставить рядом, логировать event=dry_run.
  - без --dry-run: os.replace(.tmp -> исходник), логировать event=file_updated.

6) РЕЖИМ КАТАЛОГА
-----------------
--dir требует --ext.
--ext можно указывать несколько раз и/или через запятую. Сравнение по расширению без точки, lower.
По умолчанию обход рекурсивный; --no-recursive ограничивает только текущим каталогом.
Логируются:
- dir_invalid (если каталог не существует/не каталог)
- no_matching_files (если ни одного файла с указанными расширениями не найдено)
- scan_summary (seen_files, matched_ext, processed_any, processed_ok)
- start / finish

СПРАВКА ПО АРГУМЕНТАМ И ПОВЕДЕНИЕ БЕЗ АРГУМЕНТОВ
-------------------------------------------------
- Если скрипт запущен БЕЗ аргументов, он печатает справку (--help) и завершает работу с кодом 2.
- Всегда должно быть указано ровно одно из: --file ИЛИ --dir.
- Для --dir обязателен --ext.
- --str-len обязателен всегда.

ПРИМЕРЫ
-------
1) Один файл (dry-run):
   python3 newline_to_space_v1-fix-width.py --file "/path/book.txt" --str-len 74 --dry-run --explain

2) Каталог рекурсивно:
   python3 newline_to_space_v1-fix-width.py --dir "/copy/Books/_разобрать" --ext txt --str-len 74 --dry-run --explain

3) Каталог без рекурсии:
   python3 newline_to_space_v1-fix-width.py --dir "/copy/Books/_разобрать" --ext txt --no-recursive --str-len 74 --dry-run
"""

from __future__ import print_function

import argparse
import datetime
import io
import json
import os
import sys


###############################################################################
# DEFAULTS
###############################################################################

NOW_TAG = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(SCRIPT_DIR, "newline_to_space_v1-fix-width_{0}.json".format(NOW_TAG))

AUTO_ENCODINGS = ["utf-8", "utf-8-sig", "cp1251", "koi8-r", "latin-1"]


###############################################################################
# UTIL
###############################################################################

def iso_now():
    return datetime.datetime.now().isoformat()


def decode_strict(data, enc):
    try:
        return data.decode(enc, errors="strict")
    except Exception:
        return None


def score_decoded_text(text):
    ctrl = 0
    printable = 0
    for ch in text:
        o = ord(ch)
        if ch in "\n\r\t":
            continue
        if o < 32 or o == 127:
            ctrl += 1
        else:
            printable += 1
    return ctrl * 50 - min(printable, 2000)


def decode_auto(data):
    best_text = None
    best_enc = None
    best_score = None
    for enc in AUTO_ENCODINGS:
        t = decode_strict(data, enc)
        if t is None:
            continue
        sc = score_decoded_text(t)
        if best_score is None or sc < best_score:
            best_score = sc
            best_text = t
            best_enc = enc
        if enc in ("utf-8", "utf-8-sig") and sc <= -1000:
            break
    return best_text, best_enc


def line_content_and_eol(line):
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def count_leading_spaces(s):
    i = 0
    while i < len(s) and s[i] == " ":
        i += 1
    return i


def silent_remove_if_exists(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def normalize_output_line(s):
    """
    - удалить все начальные пробелы и табы
    - любые последовательности пробелов ' ' > 1 заменить на 1 пробел
    """
    s = s.lstrip(" \t")
    out = []
    prev_space = False
    for ch in s:
        if ch == " ":
            if not prev_space:
                out.append(" ")
            prev_space = True
        else:
            out.append(ch)
            prev_space = False
    return "".join(out)


###############################################################################
# FIXED-WIDTH FILTER
###############################################################################

def compute_top_lengths(text, top_n):
    """
    ТОП длин строк по частоте среди строк с EOL и len(content) > 0.
    Сортировка: count DESC, len ASC.
    """
    hist = {}
    for line in text.splitlines(True):
        content, eol = line_content_and_eol(line)
        if not eol:
            continue
        L = len(content)
        if L <= 0:
            continue  # пустые строки исключаем
        hist[L] = hist.get(L, 0) + 1
    items = sorted(hist.items(), key=lambda x: (-x[1], x[0]))
    return [{"len": int(L), "count": int(c)} for (L, c) in items[:top_n]]


def check_fixed_width(text, str_len):
    top5 = compute_top_lengths(text, 5)
    top5_lens = [int(x["len"]) for x in top5]
    in_top5 = int(str_len) in top5_lens
    return bool(in_top5), {
        "str_len": int(str_len),
        "top_lengths": top5,
        "str_len_in_top5": bool(in_top5)
    }


###############################################################################
# CORE PROCESSING
###############################################################################

def str_len_process_to_work(text, used_enc, work_path, events, explain, str_len):
    base = None
    base_index = 0
    joined = 0
    flushes = 0
    blank_lines = 0
    dash_lines = 0
    len_mismatch_joined = 0

    # если предыдущая добавленная строка (len==str_len) завершалась дефисом '-',
    # то следующую строку склеиваем ВПЛОТНУЮ (без пробела)
    join_no_space = False

    def flush(reason, out_fh):
        nonlocal base, base_index, flushes, join_no_space
        if base is not None:
            out_fh.write(normalize_output_line(base))
            out_fh.write("\n")
            base_index += 1
            flushes += 1
            events.append({
                "ts": iso_now(),
                "level": "info",
                "event": "base_flushed",
                "base_index": base_index,
                "reason": reason
            })
            if explain:
                print("[FLUSH] base #{0} ({1})".format(base_index, reason))
            base = None
        join_no_space = False

    with io.open(work_path, "w", encoding=used_enc or "utf-8", newline="\n") as out_fh:
        for line in text.splitlines(True):
            content, _eol = line_content_and_eol(line)

            # blank separator
            if content.strip(" ") == "":
                flush("blank", out_fh)
                out_fh.write("\n")
                blank_lines += 1
                continue

            # dash without indent
            if count_leading_spaces(content) == 0 and content.startswith("-"):
                flush("dash_no_indent", out_fh)
                out_fh.write(normalize_output_line(content))
                out_fh.write("\n")
                dash_lines += 1
                continue

            # len mismatch
            if len(content) != int(str_len):
                if base is not None:
                    if join_no_space:
                        base += content
                    else:
                        base += " " + content
                    joined += 1
                    len_mismatch_joined += 1
                    flush("len_mismatch_joined", out_fh)
                else:
                    out_fh.write(normalize_output_line(content))
                    out_fh.write("\n")
                    flushes += 1
                continue

            # len == str_len
            cur = content
            cur_hyphen = False
            if cur.endswith("-"):
                # "-" -> "" и перенос строки после content -> "" (следующая строка без пробела)
                cur = cur[:-1]
                cur_hyphen = True

            if base is None:
                base = cur
            else:
                if join_no_space:
                    base += cur
                else:
                    base += " " + cur
                joined += 1

            join_no_space = cur_hyphen

        flush("eof", out_fh)

    return {
        "bases_written": int(base_index),
        "joined_lines": int(joined),
        "flushes": int(flushes),
        "blank_lines": int(blank_lines),
        "dash_lines": int(dash_lines),
        "len_mismatch_joined": int(len_mismatch_joined)
    }


###############################################################################
# PROCESS FILE
###############################################################################

def process_file(path, args, events):
    tmp_path = path + ".tmp"
    work_path = tmp_path + ".work"

    # всегда удаляем старые .tmp/.work (без логирования)
    silent_remove_if_exists(tmp_path)
    silent_remove_if_exists(work_path)

    if not os.path.isfile(path):
        events.append({"ts": iso_now(), "level": "info", "event": "skipped_not_file", "file": path})
        return False

    with io.open(path, "rb") as f:
        src_bytes = f.read()

    # RULE A: NUL forbidden
    if b"\x00" in src_bytes:
        nul_count = src_bytes.count(b"\x00")
        first_offset = src_bytes.find(b"\x00")

        sample_limit = 50
        offsets_sample = []
        if nul_count > 0:
            for i, b in enumerate(src_bytes):
                if b == 0:
                    offsets_sample.append(i)
                    if len(offsets_sample) >= sample_limit:
                        break

        events.append({
            "ts": iso_now(),
            "level": "info",
            "event": "skipped_non_text",
            "file": path,
            "reason": "nul_byte_found",
            "rule": "text_check:nul_byte_forbidden",
            "nul": {
                "count": int(nul_count),
                "first_offset": int(first_offset) if first_offset >= 0 else None,
                "offsets_sample": offsets_sample,
                "offsets_sample_limit": int(sample_limit),
                "offsets_truncated": bool(nul_count > len(offsets_sample))
            }
        })
        if args.explain:
            print("[SKIP] non-text (NUL forbidden): {0} count={1} first_offset={2}".format(
                path, int(nul_count), int(first_offset) if first_offset >= 0 else -1
            ))
        return False

    text, used_enc = decode_auto(src_bytes)

    # RULE B: decode_auto must succeed
    if text is None:
        events.append({
            "ts": iso_now(),
            "level": "info",
            "event": "skipped_non_text",
            "file": path,
            "reason": "decode_failed",
            "rule": "text_check:decode_auto_failed",
            "encodings_tried": list(AUTO_ENCODINGS)
        })
        if args.explain:
            print("[SKIP] non-text (decode failed): {0}".format(path))
        return False

    # fixed-width filter
    ok_fw, meta = check_fixed_width(text, args.str_len)
    events.append({
        "ts": iso_now(),
        "level": "info",
        "event": "checked_fixed_width",
        "file": path,
        "ok": bool(ok_fw),
        "encoding": used_enc,
        "meta": meta
    })

    if args.explain:
        print("[FIXED-WIDTH] {0} ok={1} str_len={2} in_top5={3}".format(
            path, bool(ok_fw), int(args.str_len), bool(meta.get("str_len_in_top5"))
        ))
        try:
            tops = meta.get("top_lengths") or []
            if tops:
                top_str = ", ".join(["{0}:{1}".format(int(x["len"]), int(x["count"])) for x in tops])
                print("[TOP-5] {0}".format(top_str))
        except Exception:
            pass

    if not ok_fw:
        events.append({
            "ts": iso_now(),
            "level": "info",
            "event": "skipped_fixed_width",
            "file": path,
            "meta": meta
        })
        return False

    # process to .work
    stats = str_len_process_to_work(text, used_enc, work_path, events, args.explain, args.str_len)

    # compare bytes (no_changes)
    try:
        with io.open(work_path, "rb") as wf:
            out_bytes = wf.read()
    except Exception as e:
        events.append({
            "ts": iso_now(),
            "level": "error",
            "event": "work_read_failed",
            "file": path,
            "err": str(e)
        })
        silent_remove_if_exists(work_path)
        return False

    if out_bytes == src_bytes:
        silent_remove_if_exists(work_path)
        events.append({
            "ts": iso_now(),
            "level": "info",
            "event": "no_changes",
            "file": path,
            "encoding": used_enc,
            "stats": {"process": stats, "fixed_width": meta}
        })
        if args.explain:
            print("[NO-CHANGES] {0}".format(path))
        return True

    # publish .tmp atomically
    os.replace(work_path, tmp_path)

    events.append({
        "ts": iso_now(),
        "level": "info",
        "event": "tmp_written",
        "file": path,
        "tmp": tmp_path,
        "encoding": used_enc,
        "stats": {"process": stats, "fixed_width": meta}
    })

    if args.dry_run:
        events.append({"ts": iso_now(), "level": "info", "event": "dry_run", "file": path, "tmp": tmp_path})
        if args.explain:
            print("[DRY-RUN] {0} -> {1}".format(path, tmp_path))
        return True

    os.replace(tmp_path, path)
    events.append({"ts": iso_now(), "level": "info", "event": "file_updated", "file": path, "encoding": used_enc})
    if args.explain:
        print("OK {0}".format(path))
    return True


###############################################################################
# ARGPARSE / HELP
###############################################################################

def build_parser():
    p = argparse.ArgumentParser(
        prog="newline_to_space_v1-fix-width.py",
        description=(
            "Склейка hard-wrap / fixed-width текста по длине строк.\n\n"
            "Ключевые правила:\n"
            "  - Запрещён NUL (0x00): такие файлы пропускаются (skipped_non_text).\n"
            "  - decode_auto обязателен (utf-8/utf-8-sig/cp1251/koi8-r/latin-1).\n"
            "  - fixed-width фильтр: --str-len должен входить в ТОП-5 длин строк (пустые строки не считаем).\n"
            "  - склейка: строки длины == --str-len накапливаются в базу, дефис '-' в конце строки\n"
            "    удаляется, и следующая строка склеивается вплотную (без пробела).\n"
            "  - запись результата: .tmp.work -> сравнение -> no_changes или .tmp -> (dry-run или file_updated).\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=True
    )

    p.add_argument("--file", default="", help="Обработать один файл")
    p.add_argument("--dir", default="", help="Обработать каталог (по умолчанию рекурсивно)")
    p.add_argument("--ext", action="append", default=[],
                   help="Расширения для --dir (txt,html...). Можно несколько раз или через запятую")
    p.add_argument("--no-recursive", action="store_true",
                   help="Не заходить в подкаталоги при --dir (только текущий каталог)")
    p.add_argument("--str-len", type=int, required=True,
                   help="Целевая длина строки (len(content) без EOL) для fixed-width фильтра и склейки")
    p.add_argument("--dry-run", action="store_true",
                   help="dry-run: исходник не менять, оставить .tmp рядом")
    p.add_argument("--explain", action="store_true",
                   help="Печатать детали в консоль")
    p.add_argument("--log", default=DEFAULT_LOG,
                   help="JSON лог (по умолчанию рядом со скриптом)")

    return p


def parse_args(argv, parser):
    # режим "без аргументов": печатаем справку и выходим с кодом 2
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)

    args = parser.parse_args(argv)

    # ровно одно из --file / --dir
    if bool(args.file) == bool(args.dir):
        raise SystemExit("ERROR: укажите либо --file, либо --dir")

    # для --dir обязателен --ext
    if args.dir and not args.ext:
        raise SystemExit("ERROR: для --dir нужен --ext")

    if args.str_len < 0:
        raise SystemExit("ERROR: --str-len должен быть >= 0")

    return args


###############################################################################
# MAIN
###############################################################################

def main(argv):
    parser = build_parser()

    try:
        args = parse_args(argv, parser)
    except SystemExit as e:
        if getattr(e, "code", None) == 2:
            return 2
        if str(e):
            print(str(e), file=sys.stderr)
        return 2

    # normalize exts
    exts = []
    for x in args.ext:
        for part in x.split(","):
            part = part.strip().lower()
            if part:
                exts.append(part.lstrip("."))

    events = [{
        "ts": iso_now(),
        "level": "info",
        "event": "start",
        "dry_run": bool(args.dry_run),
        "file": args.file,
        "dir": args.dir,
        "exts": exts,
        "str_len": int(args.str_len),
        "no_recursive": bool(args.no_recursive),
        "explain": bool(args.explain)
    }]

    seen_files = 0
    matched_ext = 0
    processed_any = 0
    processed_ok = 0

    if args.file:
        processed_any = 1
        processed_ok = 1 if process_file(args.file, args, events) else 0
    else:
        if not os.path.isdir(args.dir):
            events.append({"ts": iso_now(), "level": "error", "event": "dir_invalid", "dir": args.dir})
            print("ERROR: каталог не существует или не каталог:", args.dir, file=sys.stderr)
        else:
            recursive = not args.no_recursive

            for root, _dirs, files in os.walk(args.dir):
                for name in files:
                    seen_files += 1
                    ext = os.path.splitext(name)[1].lstrip(".").lower()
                    if ext in exts:
                        matched_ext += 1
                        processed_any += 1
                        if process_file(os.path.join(root, name), args, events):
                            processed_ok += 1
                if not recursive:
                    break

            if matched_ext == 0:
                events.append({
                    "ts": iso_now(),
                    "level": "info",
                    "event": "no_matching_files",
                    "dir": args.dir,
                    "exts": exts,
                    "seen_files": seen_files
                })
                print("INFO: не найдено файлов с расширениями:", ",".join(exts), "в", args.dir)

    events.append({
        "ts": iso_now(),
        "level": "info",
        "event": "scan_summary",
        "dir": args.dir,
        "seen_files": int(seen_files),
        "matched_ext": int(matched_ext),
        "processed_any": int(processed_any),
        "processed_ok": int(processed_ok)
    })

    events.append({"ts": iso_now(), "level": "info", "event": "finish"})

    try:
        with io.open(args.log, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("ERROR: не удалось записать лог:", str(e), file=sys.stderr)
        return 2

    print("== Готово. Лог:", args.log)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
