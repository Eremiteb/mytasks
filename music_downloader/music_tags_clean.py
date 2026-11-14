#!/usr/bin/env python3
"""Очистка тегов ID3 в уже лежащих библиотеках движком music_downloader (``clean_mp3_tags``).

Без ``--apply`` — dry-run: каждый файл копируется во временный каталог, чистится
там и проверяется, оригиналы не меняются, план пишется строками ``[dry-run] будет:``.
С ``--apply`` перед заменой файла в резервный каталог пишется исходный блок ID3v2
и хвост ID3v1, затем очищенная копия атомарно заменяет оригинал (права и время
изменения сохраняются). ``--restore`` возвращает исходные теги из резервного каталога.

Проверки каждого файла: аудиоданные (без ID3v2/ID3v1) побайтово те же, повторная
очистка ничего не меняет, оригинал не менялся с момента копирования. Любая
неудача оставляет оригинал нетронутым. Повторный запуск безопасен (уже чистые
файлы пропускаются).

Примеры:
  music_tags_clean.py /mnt/copy/Music                      # dry-run
  music_tags_clean.py /mnt/copy/Music --apply              # применить
  music_tags_clean.py --restore state/music-tags-backup-… # откат
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE_PATH = REPO / "music_downloader" / "music_downloader.py"
MAGIC = b"TAGBAK1\n"


def load_engine():
    os.environ.setdefault("MYTASKS_LOG_DIR", tempfile.mkdtemp(prefix="music_tags_clean_logs_"))
    spec = importlib.util.spec_from_file_location("music_downloader_engine", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def id3v2_len(data):
    """Длина блока ID3v2 в начале файла (0, если его нет)."""
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size + (10 if data[5] & 0x10 else 0)


def layout(path):
    """``(длина_ID3v2, длина_ID3v1, размер_файла)``; ID3v1 — 128 байт ``TAG`` в конце."""
    size = os.path.getsize(path)
    with open(path, "rb") as file_obj:
        head = id3v2_len(file_obj.read(10))
        tail = 0
        if size - head >= 128:
            file_obj.seek(size - 128)
            tail = 128 if file_obj.read(3) == b"TAG" else 0
    return head, tail, size


def audio_digest(path):
    """SHA-256 аудиоданных без блока ID3v2 в начале и ID3v1 в конце."""
    head, tail, size = layout(path)
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        file_obj.seek(head)
        remaining = size - head - tail
        while remaining > 0:
            chunk = file_obj.read(min(1 << 20, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def find_mp3(roots):
    for root in roots:
        for directory, dirs, names in os.walk(root):
            dirs.sort()
            for name in sorted(names):
                if name.lower().endswith(".mp3"):
                    yield root, os.path.join(directory, name)


def backup_path(backup_dir, root, path):
    label = hashlib.sha1(os.path.abspath(root).encode()).hexdigest()[:8]
    rel = Path(os.path.relpath(path, root))
    # Имя с суффиксом длиннее 255 байт ФС не примет: такая копия хранится под хешем
    # пути (исходный путь всё равно записан в метаданных копии, --restore его читает).
    parts = [part if len(part.encode()) <= 254 else hashlib.sha1(part.encode()).hexdigest() for part in rel.parts[:-1]]
    name = rel.name + ".tagbak"
    if len(name.encode()) > 255:
        name = hashlib.sha1(str(rel).encode()).hexdigest() + ".tagbak"
    return Path(backup_dir, label, *parts, name)


def write_backup(target, original, head, tail, size, stat, digest):
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "path": original, "size": size, "mtime_ns": stat.st_mtime_ns, "mode": stat.st_mode & 0o7777,
        "head_len": head, "tail_len": tail, "audio_sha256": digest,
    }
    with open(original, "rb") as src:
        head_bytes = src.read(head)
        src.seek(size - tail)
        tail_bytes = src.read(tail) if tail else b""
    tmp = target.with_suffix(".tmp")
    with open(tmp, "wb") as out:
        out.write(MAGIC + json.dumps(meta, ensure_ascii=False).encode() + b"\n" + head_bytes + tail_bytes)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, target)


def clean_one(engine, path, work_dir, backup_dir, root, apply):
    """Возвращает ``(статус, размер_до, размер_после)``; статусы: clean, cleaned, would_clean, error:<причина>."""
    stat = os.stat(path)
    work = os.path.join(work_dir, "work.mp3")
    shutil.copyfile(path, work)
    try:
        head, tail, size = layout(work)
        before = audio_digest(work)
        if not engine.clean_mp3_tags(work):
            return "clean", size, size
        if audio_digest(work) != before:
            return "error:аудиоданные изменились", size, size
        if engine.clean_mp3_tags(work):
            return "error:повторная очистка снова меняет файл", size, size
        new_size = os.path.getsize(work)
        if not apply:
            return "would_clean", size, new_size
        now = os.stat(path)
        if (now.st_size, now.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            return "error:оригинал изменился во время очистки", size, size
        write_backup(backup_path(backup_dir, root, path), path, head, tail, size, stat, before)
        os.chmod(work, stat.st_mode & 0o7777)
        os.utime(work, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(work, path)
        return "cleaned", size, new_size
    finally:
        if os.path.exists(work):
            os.remove(work)


def restore(backup_dir, apply):
    files = sorted(Path(backup_dir).rglob("*.tagbak"))
    stats = {"restored": 0, "error": 0}
    print(f"СТАРТ откат: резервных записей {len(files)}", flush=True)
    for index, bak in enumerate(files, 1):
        try:
            raw = bak.read_bytes()
            assert raw.startswith(MAGIC)
            meta_line, rest = raw[len(MAGIC):].split(b"\n", 1)
            meta = json.loads(meta_line)
            head_bytes, tail_bytes = rest[:meta["head_len"]], rest[meta["head_len"]:]
            path = meta["path"]
            head, tail, size = layout(path)
            if audio_digest(path) != meta["audio_sha256"]:
                raise ValueError("аудиоданные не совпадают с резервной записью")
            if not apply:
                print(f"[dry-run] будет: вернуть теги {path}")
                stats["restored"] += 1
                continue
            # Короткое имя: суффикс к длинному имени может превысить лимит 255 байт.
            work = os.path.join(os.path.dirname(path), f".tagrestore-{os.getpid()}.tmp")
            with open(path, "rb") as src, open(work, "wb") as out:
                out.write(head_bytes)
                src.seek(head)
                remaining = size - head - tail
                while remaining > 0:
                    chunk = src.read(min(1 << 20, remaining))
                    out.write(chunk)
                    remaining -= len(chunk)
                out.write(tail_bytes)
            os.chmod(work, meta["mode"])
            os.utime(work, ns=(meta["mtime_ns"], meta["mtime_ns"]))
            os.replace(work, path)
            stats["restored"] += 1
        except Exception as error:
            stats["error"] += 1
            print(f"ОШИБКА отката {bak}: {error}", flush=True)
        if index % 1000 == 0:
            print(f"ПРОГРЕСС откат {index}/{len(files)}", flush=True)
    print(f"ИТОГ откат: {stats}", flush=True)
    return 1 if stats["error"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="*", help="каталоги библиотек")
    parser.add_argument("--apply", action="store_true", help="менять файлы (по умолчанию dry-run)")
    parser.add_argument("--restore", metavar="BACKUP_DIR", help="вернуть исходные теги из резервного каталога")
    parser.add_argument("--backup-dir", default=str(REPO / "state" / f"music-tags-backup-{time.strftime('%Y-%m-%d')}"))
    parser.add_argument("--report", default=None, help="JSONL-отчёт (по умолчанию рядом с резервным каталогом)")
    parser.add_argument("--limit", type=int, default=0, help="обработать не более N файлов (для проб)")
    parser.add_argument("--work-dir", default=None,
                        help="временный каталог на ТОЙ ЖЕ ФС, что и библиотека, вне синхронизируемых папок")
    args = parser.parse_args()
    if args.restore:
        return restore(args.restore, args.apply)
    if not args.roots:
        parser.error("нужен хотя бы один каталог библиотеки")
    engine = load_engine()
    roots = [os.path.abspath(root) for root in args.roots]
    total = sum(1 for _ in find_mp3(roots))
    report_path = args.report or f"{args.backup_dir}.report.jsonl"
    mode = "ПРИМЕНЕНИЕ" if args.apply else "dry-run"
    print(f"СТАРТ {mode}: файлов {total}, резервные копии тегов: {args.backup_dir}", flush=True)
    stats = {}
    saved = 0
    t0 = time.time()
    current_work = None
    # Отчёт — в with: закрывается и при сбое посреди обработки
    write_report = args.apply or args.limit == 0
    with (
        open(report_path, "a", encoding="utf-8") if write_report else contextlib.nullcontext()
    ) as report:
        for index, (root, path) in enumerate(find_mp3(roots), 1):
            if args.limit and index > args.limit:
                break
            work_dir = args.work_dir or os.path.join(os.path.dirname(root.rstrip("/")), ".tagclean-work")
            if current_work != work_dir:
                os.makedirs(work_dir, exist_ok=True)
                current_work = work_dir
            try:
                status, before, after = clean_one(engine, path, work_dir, args.backup_dir, root, args.apply)
            except Exception as error:
                status, before, after = f"error:{error!r}", 0, 0
            key = status.split(":", 1)[0]
            stats[key] = stats.get(key, 0) + 1
            saved += before - after
            if status.startswith("would_clean") and index <= 20:
                print(f"[dry-run] будет: очистить теги {path} ({before} → {after} байт)")
            if status.startswith("error"):
                print(f"ОШИБКА {path}: {status}", flush=True)
            if report and status != "clean":
                report.write(json.dumps({"path": path, "status": status, "before": before, "after": after},
                                        ensure_ascii=False) + "\n")
            if index % 500 == 0 or index == total:
                spent = time.time() - t0
                print(f"ПРОГРЕСС {index}/{total} ({index * 100 // total}%), {index / spent:.1f} файл/с, "
                      f"осталось ~{spent / index * (total - index) / 60:.0f} мин; {stats}", flush=True)
    if current_work and os.path.isdir(current_work) and not os.listdir(current_work):
        os.rmdir(current_work)
    print(f"ИТОГ {mode}: {stats}, высвобождено {saved} байт, за {(time.time() - t0) / 60:.1f} мин", flush=True)
    return 1 if stats.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
