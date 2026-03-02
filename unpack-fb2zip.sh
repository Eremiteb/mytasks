#!/usr/bin/env bash
# unpack-fb2zip.sh — распаковывает *.fb2.zip рядом с архивом (в тот же каталог)
# Лог: один JSONL-файл (по одной JSON-записи на строку)
# Требования: bash, find, unzip

set -u

DRY_RUN=0
ROOT_DIR=""
LOG_FILE=""

ts_now() { date '+%Y-%m-%d %H:%M:%S'; }

json_escape() {
  # экранирование строки для JSON
  local s="${1-}"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

log_json() {
  # level event msg src dst rc extra_json
  local level="${1-}" event="${2-}" msg="${3-}" src="${4-}" dst="${5-}" rc="${6-}" extra="${7-}"
  local t; t="$(ts_now)"
  local j="{\"ts\":\"$(json_escape "$t")\",\"level\":\"$(json_escape "$level")\",\"event\":\"$(json_escape "$event")\""
  [ -n "$msg" ] && j+=",\"msg\":\"$(json_escape "$msg")\""
  [ -n "$src" ] && j+=",\"src\":\"$(json_escape "$src")\""
  [ -n "$dst" ] && j+=",\"dst\":\"$(json_escape "$dst")\""
  j+=",\"rc\":$rc"
  [ -n "$extra" ] && j+=",$extra"
  j+="}"
  printf '%s\n' "$j" >>"$LOG_FILE"
}

usage() {
  cat <<'EOF'
Использование:
  unpack-fb2zip.sh -p DIR [--dry-run] [--log FILE]

Описание:
  Ищет файлы *.fb2.zip в указанной папке DIR и подкаталогах и распаковывает
  содержащиеся .fb2 файлы рядом с архивом (в тот же каталог, без внутренних путей).
  Если внутри архива несколько .fb2 или есть конфликты имён — добавляет суффиксы _1, _2 ...

Опции:
  -p, --path DIR     корневая папка для поиска (обязательно)
  --dry-run          ничего не извлекать, только показать/залогировать действия
  --log FILE         путь к JSONL-логу (по умолчанию: logs/unpack-fb2zip_YYYY-MM-DD.jsonl)
  -h, --help         помощь

Формат лога:
  JSON Lines (каждая строка — отдельный JSON-объект)
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Ошибка: не найдено: $1" >&2
    exit 127
  }
}

unique_path() {
  # $1 = dir, $2 = filename
  local dir="$1" name="$2"
  local base ext cand n
  base="$name"
  ext=""
  if [[ "$name" == *.* ]]; then
    ext=".${name##*.}"
    base="${name%.*}"
  fi
  cand="$dir/$name"
  n=1
  while [ -e "$cand" ]; do
    cand="$dir/${base}_$n$ext"
    n=$((n+1))
  done
  printf '%s' "$cand"
}

# --- args ---
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--path) ROOT_DIR="${2-}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --log) LOG_FILE="${2-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Неизвестный аргумент: $1" >&2; usage; exit 2;;
  esac
done

[ -z "${ROOT_DIR}" ] && { echo "Ошибка: укажите -p DIR" >&2; usage; exit 2; }
[ -d "$ROOT_DIR" ] || { echo "Ошибка: нет такой папки: $ROOT_DIR" >&2; exit 2; }

if [ -z "${LOG_FILE}" ]; then
  SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
  LOG_DIR="${SCRIPT_DIR}/logs"
  mkdir -p "$LOG_DIR"
  LOG_FILE="${LOG_DIR}/unpack-fb2zip_$(date '+%Y-%m-%d').jsonl"
fi

need_cmd find
need_cmd unzip

log_json "info" "start" "Начало обработки" "$ROOT_DIR" "" 0 "\"dry_run\":$DRY_RUN"

# --- main ---
# Ищем именно *.fb2.zip
find "$ROOT_DIR" -type f -name '*.fb2.zip' -print0 2>/dev/null | \
while IFS= read -r -d '' zipfile; do
  dir="$(dirname -- "$zipfile")"

  # Список .fb2 внутри архива
  if ! entries="$(unzip -Z1 -- "$zipfile" 2>/dev/null | awk 'tolower($0) ~ /\.fb2$/ {print}')" ; then
    log_json "error" "zip_list_failed" "Не удалось прочитать список файлов в архиве" "$zipfile" "" 1 ""
    continue
  fi

  if [ -z "$entries" ]; then
    log_json "info" "no_fb2_inside" "В архиве нет .fb2" "$zipfile" "" 0 ""
    continue
  fi

  # Извлекаем каждую .fb2 “рядом” (basename), избегая конфликтов имён
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    out_name="$(basename -- "$entry")"
    out_path="$(unique_path "$dir" "$out_name")"

    if [ "$DRY_RUN" -eq 1 ]; then
      log_json "info" "would_extract" "DRY-RUN: извлёк бы файл" "$zipfile" "$out_path" 0 "\"entry\":\"$(json_escape "$entry")\""
      continue
    fi

    # unzip -p: печатает файл на stdout; пишем в итоговый файл
    if unzip -p -- "$zipfile" "$entry" >"$out_path" 2>/dev/null; then
      log_json "info" "extracted" "Файл извлечён" "$zipfile" "$out_path" 0 "\"entry\":\"$(json_escape "$entry")\""
    else
      rc=$?
      # на случай частично созданного файла
      [ -f "$out_path" ] && rm -f -- "$out_path"
      log_json "error" "extract_failed" "Ошибка извлечения" "$zipfile" "$out_path" "$rc" "\"entry\":\"$(json_escape "$entry")\""
    fi
  done <<<"$entries"

done

log_json "info" "finish" "Готово" "$ROOT_DIR" "" 0 "\"log\":\"$(json_escape "$LOG_FILE")\""

echo "OK. Лог: $LOG_FILE"
exit 0
