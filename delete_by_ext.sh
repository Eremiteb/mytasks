#!/bin/sh

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)

LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
mkdir -p "$LOG_DIR"

if [ -r "$LOG_TEMPLATE_FILE" ]; then
  # shellcheck source=/dev/null
  . "$LOG_TEMPLATE_FILE"
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

cleanup_logs() {
  old_logs=$(ls -1t "${LOG_DIR}/${SCRIPT_BASE}-"*.jsonl 2>/dev/null | tail -n +11 || true)
  if [ -n "${old_logs:-}" ]; then
    rm -f $old_logs
  fi
}

log_json() {
  level="$1"
  event="$2"
  msg="$3"
  detail="${4:-}"
  rc="${5:-null}"
  dry_run_json="$( [ "$DRY_RUN" -eq 1 ] && echo true || echo false )"
  level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"

  msg_esc="$(json_escape "$msg")"
  detail_esc="$(json_escape "$detail")"

  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","dry_run":%s,"msg":"%s","detail":"%s","rc":%s}\n' \
    "$(ts)" "$LOG_SCHEMA_VERSION" "$LOG_COMPAT_TARGETS" "$level_norm" "$msg_esc" "$event" "$SCRIPT_BASE" "$SCRIPT_NAME" "$event" "$level_norm" "$dry_run_json" "$msg_esc" "$detail_esc" "$rc" >> "$LOG_FILE"
}

usage() {
  cat <<EOF
Использование:
  $SCRIPT_NAME -d DIR -e EXT [--dry-run]

Опции:
  -d DIR       каталог для обработки
  -e EXT       расширение файлов (без точки), например: tmp, log, bak
  --dry-run    только показать, что будет удалено (без удаления)
  -h, --help   справка

Примеры:
  $SCRIPT_NAME -d /tmp -e log --dry-run
  $SCRIPT_NAME -d ./downloads -e tmp
EOF
}

###############################################################################
# ARGS & VALIDATION
###############################################################################
DRY_RUN=0
TARGET_DIR=""
EXTENSION=""

while [ $# -gt 0 ]; do
  case "$1" in
    -d)
      [ $# -lt 2 ] && { echo "Ошибка: для -d требуется значение" >&2; exit 1; }
      TARGET_DIR="$2"
      shift 2
      ;;
    -e)
      [ $# -lt 2 ] && { echo "Ошибка: для -e требуется значение" >&2; exit 1; }
      EXTENSION="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Неизвестный аргумент: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "$TARGET_DIR" ] || [ -z "$EXTENSION" ]; then
  usage >&2
  exit 1
fi

if [ ! -d "$TARGET_DIR" ]; then
  echo "Каталог не найден: $TARGET_DIR" >&2
  exit 2
fi

EXTENSION=${EXTENSION#.}
if [ -z "$EXTENSION" ]; then
  echo "Ошибка: расширение не может быть пустым" >&2
  exit 2
fi

###############################################################################
# MAIN
###############################################################################
log_json "INFO" "start" "Запуск удаления файлов по расширению" "dir=${TARGET_DIR}; ext=.${EXTENSION}" 0

DELETED_COUNT=0
ERROR_COUNT=0
MATCHED_COUNT=0

LIST_FILE=$(mktemp)
trap 'rm -f -- "$LIST_FILE"' EXIT INT TERM

find "$TARGET_DIR" -type f -name "*.${EXTENSION}" -print > "$LIST_FILE"

while IFS= read -r file; do
  [ -z "$file" ] && continue
  MATCHED_COUNT=$((MATCHED_COUNT + 1))

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] $file"
    log_json "INFO" "would_delete" "Найден файл для удаления" "$file" 0
    continue
  fi

  if rm -f -- "$file"; then
    DELETED_COUNT=$((DELETED_COUNT + 1))
    echo "[DELETED] $file"
    log_json "INFO" "deleted" "Файл удален" "$file" 0
  else
    ERROR_COUNT=$((ERROR_COUNT + 1))
    echo "[ERROR]   $file"
    log_json "ERROR" "delete_failed" "Не удалось удалить файл" "$file" 1
  fi
done < "$LIST_FILE"

if [ "$ERROR_COUNT" -gt 0 ]; then
  log_json "ERROR" "done" "Завершено с ошибками" "matched=${MATCHED_COUNT}; deleted=${DELETED_COUNT}; errors=${ERROR_COUNT}" 1
  cleanup_logs
  exit 1
fi

log_json "INFO" "done" "Завершено успешно" "matched=${MATCHED_COUNT}; deleted=${DELETED_COUNT}; errors=${ERROR_COUNT}" 0
cleanup_logs
exit 0
