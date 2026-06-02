#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
mkdir -p "$LOG_DIR"

if [[ -r "$LOG_TEMPLATE_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$LOG_TEMPLATE_FILE"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
}

log_json() {
  local level="$1"
  local event="$2"
  local msg="$3"
  local detail="${4:-}"
  local level_norm msg_esc detail_esc
  level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"
  msg_esc="$(json_escape "$msg")"
  detail_esc="$(json_escape "$detail")"
  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
    "$(ts)" "$LOG_SCHEMA_VERSION" "$LOG_COMPAT_TARGETS" "$level_norm" "$msg_esc" "$event" "$SCRIPT_BASE" "$SCRIPT_NAME" "$event" "$level_norm" "$msg_esc" "$detail_esc" >> "$LOG_FILE"
}

cleanup_logs() {
  local old_logs
  old_logs=$(ls -1t "${LOG_DIR}/${SCRIPT_BASE}-"*.jsonl 2>/dev/null | tail -n +11 || true)
  if [[ -n "${old_logs:-}" ]]; then
    rm -f $old_logs
  fi
}

usage() {
  cat <<EOF
Usage:
  $SCRIPT_NAME --dry-run DIR
  $SCRIPT_NAME --apply   DIR

Options:
  --dry-run   показать, что будет переименовано
  --apply     реально переименовать файлы
EOF
  exit 1
}

###############################################################################
# ARGS
###############################################################################
[[ $# -ne 2 ]] && usage
MODE="$1"
DIR="$2"

[[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]] && usage
[[ ! -d "$DIR" ]] && { echo "Error: directory not found"; exit 1; }

###############################################################################
# MAIN
###############################################################################
log_json "INFO" "start" "Запуск изменения регистра расширений" "mode=${MODE}; dir=${DIR}"

total=$(find "$DIR" -type f -name '*.*' -print0 | tr -cd '\0' | wc -c)
(( total == 0 )) && { echo "No files with extensions found."; log_json "INFO" "done" "Файлов с расширением не найдено"; cleanup_logs; exit 0; }

i=0
renamed=0

find "$DIR" -type f -name '*.*' -print0 |
while IFS= read -r -d '' file; do
  i=$((i+1))
  percent=$((i * 100 / total))

  ext="${file##*.}"
  lowext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"
  [[ "$ext" == "$lowext" ]] && continue

  new="${file%.*}.$lowext"
  printf "\r[%3d%%] %s" "$percent" "$file"

  if [[ "$MODE" == "--dry-run" ]]; then
    printf "\nWOULD RENAME: %s -> %s\n" "$file" "$new"
    log_json "INFO" "would_rename" "Файл будет переименован" "$file -> $new"
  else
    mv -n -- "$file" "$new"
    renamed=$((renamed + 1))
    log_json "INFO" "renamed" "Файл переименован" "$file -> $new"
  fi
done

echo
echo "Done."
log_json "INFO" "done" "Обработка завершена" "renamed=${renamed}; total=${total}"
cleanup_logs
