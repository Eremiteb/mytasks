#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<EOF
Использование: $(basename "$0") <каталог> [--dry-run] [-h|--help]

Рекурсивно перекодирует текстовые файлы из Windows-1251 в UTF-8.

Аргументы:
  <каталог>   Путь к каталогу для обработки
  --dry-run   Только показать файлы, которые будут перекодированы (без изменений)
  -h, --help  Показать эту справку и выйти

Примеры:
  $(basename "$0") /path/to/dir
  $(basename "$0") /path/to/dir --dry-run
EOF
}

DIR="${1:-}"
MODE="${2:-}"

if [ "$DIR" = "-h" ] || [ "$DIR" = "--help" ]; then
    usage
    exit 0
fi

if [ -z "$DIR" ]; then
    usage
    exit 1
fi

if [ ! -d "$DIR" ]; then
    echo "Ошибка: каталог не найден: $DIR" >&2
    exit 1
fi

DRY_RUN=0
[ "$MODE" = "--dry-run" ] && DRY_RUN=1

echo "Каталог: $DIR"
[ "$DRY_RUN" -eq 1 ] && echo "Режим: DRY-RUN"

find "$DIR" -type f | while IFS= read -r file; do
    # пропускаем бинарные файлы
    if ! file -b --mime "$file" | grep -q 'text'; then
        continue
    fi

    # уже UTF-8?
    if iconv -f utf-8 -t utf-8 "$file" >/dev/null 2>&1; then
        continue
    fi

    # проверяем, что это windows-1251
    if iconv -f windows-1251 -t utf-8 "$file" >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[DRY-RUN] перекодировать: $file"
        else
            tmp="$(mktemp)"
            iconv -f windows-1251 -t utf-8 "$file" > "$tmp"
            chmod --reference="$file" "$tmp"
            chown --reference="$file" "$tmp" 2>/dev/null || true
            mv "$tmp" "$file"
            echo "[OK] перекодирован: $file"
        fi
    fi
done

echo "Готово."
