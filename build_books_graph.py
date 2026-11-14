#!/usr/bin/env python3
"""Строит локальный Graphify-граф файловой библиотеки без чтения содержимого файлов."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from graphify.export import to_json
from graphify.exporters.html import to_html

SKIP_DIRS = {".stfolder", ".stversions", ".sync", ".thumbnails", "graphify-out"}
SKIP_EXTENSIONS = {"htm", "html"}
HTML_SIDECAR_SUFFIXES = ("_files", ".files", " files")
BOOK_EXTENSIONS = {
    "azw",
    "azw3",
    "chm",
    "djvu",
    "doc",
    "docx",
    "epub",
    "fb2",
    "mobi",
    "odt",
    "pdf",
    "rtf",
    "txt",
    "zip",
}
IMAGE_EXTENSIONS = {"bmp", "gif", "jpeg", "jpg", "png", "tif", "tiff", "webp"}
PAPER_EXTENSIONS = {"djvu", "pdf"}
GENERIC_TITLES = {
    "book",
    "cover",
    "index",
    "readme",
    "архив",
    "книга",
    "обложка",
    "содержание",
}


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha1(value.encode('utf-8', 'surrogatepass')).hexdigest()}"  # noqa: S324


def normalize_title(stem: str) -> str:
    text = unicodedata.normalize("NFKC", stem).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("Недостижимая ветвь")


def is_html_sidecar_directory(name: str) -> bool:
    return name.casefold().endswith(HTML_SIDECAR_SUFFIXES)


def file_type(extension: str) -> str:
    if extension in PAPER_EXTENSIONS:
        return "paper"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    return "document"


def community_key(relative_path: Path) -> str:
    if not relative_path.parts:
        return "Корень библиотеки"
    first = relative_path.parts[0]
    if first.startswith("_"):
        return "Разделы с подчёркиванием"
    initial = first[0].upper()
    if "А" <= initial <= "Я" or initial == "Ё":
        return f"Кириллица: {initial}"
    if "A" <= initial <= "Z":
        return f"Латиница: {initial}"
    if initial.isdigit():
        return "Названия с цифры"
    return "Прочие названия"


def add_node(graph: nx.Graph, node_id: str, **attributes: object) -> None:
    graph.add_node(node_id, **attributes)


def add_edge(graph: nx.Graph, source: str, target: str, relation: str, _source_file: str, *, weight: float = 1.0, confidence: str = "EXTRACTED", confidence_score: float = 1.0) -> None:
    graph.add_edge(
        source,
        target,
        relation=relation,
        confidence=confidence,
        confidence_score=confidence_score,
        source_file="filesystem",
        weight=weight,
        _src=source,
        _tgt=target,
    )


def build(source: Path, output: Path) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise SystemExit(f"Каталог не найден: {source}")
    if output == source or source in output.parents:
        raise SystemExit("Каталог результата должен находиться вне индексируемой библиотеки")

    output.mkdir(parents=True, exist_ok=True)
    graph = nx.Graph()
    root_id = "library_root"
    add_node(
        graph,
        root_id,
        label=source.name,
        file_type="concept",
        source_file=str(source),
        source_location=None,
        kind="library",
        relative_path=".",
    )

    extension_nodes: dict[str, str] = {}
    title_files: dict[str, list[str]] = defaultdict(list)
    title_paths: dict[str, list[str]] = defaultdict(list)
    extension_counts: Counter[str] = Counter()
    extension_sizes: Counter[str] = Counter()
    top_counts: Counter[str] = Counter()
    top_sizes: Counter[str] = Counter()
    node_communities: dict[str, str] = {root_id: "Корень библиотеки"}
    errors: list[str] = []
    file_count = 0
    directory_count = 1
    total_size = 0

    stack: list[tuple[Path, Path, str]] = [(source, Path(), root_id)]
    while stack:
        directory, relative_directory, directory_id = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
            continue

        child_directories: list[tuple[Path, Path, str]] = []
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and (
                entry.name in SKIP_DIRS or is_html_sidecar_directory(entry.name)
            ):
                continue
            relative = relative_directory / entry.name
            absolute = Path(entry.path)
            community = community_key(relative)
            if entry.is_dir(follow_symlinks=False):
                child_id = stable_id("dir", relative.as_posix())
                add_node(
                    graph,
                    child_id,
                    label=entry.name,
                    file_type="concept",
                    source_file=str(absolute),
                    source_location=None,
                    kind="directory",
                    relative_path=relative.as_posix(),
                )
                add_edge(graph, directory_id, child_id, "contains", str(directory))
                node_communities[child_id] = community
                child_directories.append((absolute, relative, child_id))
                directory_count += 1
                continue
            if not entry.is_file(follow_symlinks=False):
                continue

            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as exc:
                errors.append(f"{absolute}: {exc}")
                continue

            extension = absolute.suffix.lower().lstrip(".") or "без расширения"
            if extension in SKIP_EXTENSIONS:
                continue
            file_id = stable_id("file", relative.as_posix())
            add_node(
                graph,
                file_id,
                label=entry.name,
                file_type=file_type(extension),
                source_file=str(absolute),
                source_location=None,
                kind="file",
                relative_path=relative.as_posix(),
                extension=extension,
                size_bytes=size,
            )
            add_edge(graph, directory_id, file_id, "contains", str(directory))
            node_communities[file_id] = community

            format_id = extension_nodes.get(extension)
            if format_id is None:
                format_id = stable_id("format", extension)
                extension_nodes[extension] = format_id
                add_node(
                    graph,
                    format_id,
                    label=f"Формат .{extension}" if extension != "без расширения" else "Без расширения",
                    file_type="concept",
                    source_file=str(source),
                    source_location=None,
                    kind="format",
                    extension=extension,
                )
                node_communities[format_id] = "Форматы файлов"
            add_edge(graph, file_id, format_id, "has_format", str(absolute))

            if extension in BOOK_EXTENSIONS:
                normalized = normalize_title(absolute.stem)
                if len(normalized) >= 4 and normalized not in GENERIC_TITLES:
                    title_files[normalized].append(file_id)
                    title_paths[normalized].append(relative.as_posix())

            top = relative.parts[0] if relative.parts else "(корень)"
            extension_counts[extension] += 1
            extension_sizes[extension] += size
            top_counts[top] += 1
            top_sizes[top] += size
            file_count += 1
            total_size += size

        stack.extend(reversed(child_directories))

    duplicate_groups = 0
    duplicate_files = 0
    largest_groups: list[dict[str, object]] = []
    for normalized, file_ids in title_files.items():
        if not 2 <= len(file_ids) <= 100:
            continue
        title_id = stable_id("title", normalized)
        paths = title_paths[normalized]
        add_node(
            graph,
            title_id,
            label=f"Одинаковое название: {normalized}",
            file_type="concept",
            source_file=str(source),
            source_location=None,
            kind="normalized_title",
            normalized_title=normalized,
            candidates=len(file_ids),
        )
        node_communities[title_id] = "Возможные дубликаты названий"
        for file_id, relative_path in zip(file_ids, paths, strict=True):
            add_edge(
                graph,
                file_id,
                title_id,
                "shares_normalized_name",
                str(source / relative_path),
                confidence="INFERRED",
                confidence_score=0.75,
            )
        duplicate_groups += 1
        duplicate_files += len(file_ids)
        largest_groups.append({"title": normalized, "files": len(file_ids), "paths": paths[:10]})

    community_names = sorted(set(node_communities.values()))
    community_ids = {name: number for number, name in enumerate(community_names)}
    communities: dict[int, list[str]] = {number: [] for number in community_ids.values()}
    for node_id in graph.nodes:
        communities[community_ids[node_communities[node_id]]].append(node_id)
    labels = {number: name for name, number in community_ids.items()}

    graph_path = output / "graph.json"
    html_path = output / "graph.html"
    if not to_json(graph, communities, str(graph_path), force=True, community_labels=labels):
        raise SystemExit("Graphify отказался записать graph.json")
    if not to_html(graph, communities, str(html_path), community_labels=labels, node_limit=5000):
        raise SystemExit("Graphify не создал graph.html")

    summary: dict[str, object] = {
        "source": str(source),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": file_count,
        "directories": directory_count,
        "bytes": total_size,
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "communities": len(communities),
        "possible_duplicate_title_groups": duplicate_groups,
        "files_in_possible_duplicate_title_groups": duplicate_files,
        "skipped_directory_names": sorted(SKIP_DIRS),
        "skipped_directory_suffixes": list(HTML_SIDECAR_SUFFIXES),
        "skipped_extensions": sorted(SKIP_EXTENSIONS),
        "scan_errors": errors,
        "extensions": [
            {"extension": extension, "files": count, "bytes": extension_sizes[extension]}
            for extension, count in extension_counts.most_common()
        ],
        "top_level": [
            {"name": name, "files": count, "bytes": top_sizes[name]}
            for name, count in top_counts.most_common()
        ],
        "largest_possible_duplicate_title_groups": sorted(
            largest_groups, key=lambda item: (-int(item["files"]), str(item["title"]))
        )[:100],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    report_lines = [
        "# Граф библиотеки Books",
        "",
        f"Создан: `{summary['created_at']}`",
        f"Источник: `{source}`",
        "",
        "## Сводка",
        "",
        f"- Файлов: **{file_count:,}**",
        f"- Каталогов: **{directory_count:,}**",
        f"- Общий размер: **{human_size(total_size)}**",
        f"- Узлов графа: **{graph.number_of_nodes():,}**",
        f"- Связей: **{graph.number_of_edges():,}**",
        f"- Сообществ: **{len(communities):,}**",
        f"- Групп с одинаковым нормализованным названием: **{duplicate_groups:,}** ({duplicate_files:,} файлов)",
        f"- Ошибок чтения метаданных: **{len(errors):,}**",
        "",
        "## Крупнейшие форматы",
        "",
        "| Расширение | Файлов | Размер |",
        "|---|---:|---:|",
    ]
    for extension, count in extension_counts.most_common(20):
        report_lines.append(f"| `{extension}` | {count:,} | {human_size(extension_sizes[extension])} |")
    report_lines += [
        "",
        "## Крупнейшие разделы верхнего уровня",
        "",
        "| Раздел | Файлов | Размер |",
        "|---|---:|---:|",
    ]
    for name, count in top_counts.most_common(20):
        safe_name = name.replace("|", "\\|")
        report_lines.append(f"| `{safe_name}` | {count:,} | {human_size(top_sizes[name])} |")
    report_lines += [
        "",
        "## Достоверность",
        "",
        "- Связи каталогов и форматов извлечены из файловой системы (`EXTRACTED`, 1.0).",
        "- Связи одинаковых названий являются кандидатами (`INFERRED`, 0.75), а не доказанными дубликатами.",
        "- Содержимое книг не читалось, контрольные суммы не вычислялись, файлы библиотеки не изменялись.",
        "- HTML показывает агрегированные сообщества; полный список узлов и связей находится в `graph.json`.",
        "",
    ]
    (output / "GRAPH_REPORT.md").write_text("\n".join(report_lines), encoding="utf-8")
    (output / ".graphify_root").write_text(str(source), encoding="utf-8")
    return summary


def self_test() -> None:
    assert normalize_title("Ёжик_в-тумане") == "ежик в тумане"
    assert community_key(Path("Адамов/Книга.fb2")) == "Кириллица: А"
    assert is_html_sidecar_directory("Книга_files")
    assert is_html_sidecar_directory("Книга.files")
    assert is_html_sidecar_directory("Книга files")
    assert not is_html_sidecar_directory("files")
    assert file_type("pdf") == "paper"
    assert file_type("jpg") == "image"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    self_test()
    summary = build(args.source, args.output)
    print(
        f"Готово: {summary['files']:,} файлов, {summary['nodes']:,} узлов, "
        f"{summary['edges']:,} связей"
    )


if __name__ == "__main__":
    main()
