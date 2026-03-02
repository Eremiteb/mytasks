#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage:
  $(basename "$0") --dry-run DIR
  $(basename "$0") --apply   DIR

Options:
  --dry-run   показать, что будет переименовано
  --apply     реально переименовать файлы
EOF
  exit 1
}

[[ $# -ne 2 ]] && usage

MODE="$1"
DIR="$2"

[[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]] && usage
[[ ! -d "$DIR" ]] && { echo "Error: directory not found"; exit 1; }

# считаем количество файлов с расширением
total=$(find "$DIR" -type f -name '*.*' -print0 | tr -cd '\0' | wc -c)
(( total == 0 )) && { echo "No files with extensions found."; exit 0; }

i=0

find "$DIR" -type f -name '*.*' -print0 |
while IFS= read -r -d '' file; do
  i=$((i+1))
  percent=$((i * 100 / total))

  ext="${file##*.}"
  lowext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"

  # если расширение уже в нижнем регистре — пропускаем
  [[ "$ext" == "$lowext" ]] && continue

  new="${file%.*}.$lowext"

  printf "\r[%3d%%] %s" "$percent" "$file"

  if [[ "$MODE" == "--dry-run" ]]; then
    printf "\nWOULD RENAME: %s -> %s\n" "$file" "$new"
  else
    mv -n -- "$file" "$new"
  fi
done

echo -e "\nDone."
