#!/bin/sh

# Автоудаление старых файлов по конфигу

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)

CONFIG_DIR="${SCRIPT_DIR}/conf"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${CONFIG_DIR}" "${LOG_DIR}"

CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"

if [ -r "${LOG_TEMPLATE_FILE}" ]; then
  # shellcheck source=/dev/null
  . "${LOG_TEMPLATE_FILE}"
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
  _level="$1"
  _event="$2"
  _msg="$3"
  _detail="${4:-}"
  _rc="${5:-null}"
  _level_norm="$(printf '%s' "${_level}" | tr '[:upper:]' '[:lower:]')"
  _msg_esc="$(json_escape "${_msg}")"
  _detail_esc="$(json_escape "${_detail}")"

  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "$(ts)" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${_level_norm}" "${_msg_esc}" "${_event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${_event}" "${_level_norm}" "${_msg_esc}" "${_detail_esc}" "${_rc}" >> "${LOG_FILE}"
}

cleanup_logs() {
  find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
    | sort -nr \
    | awk -F'|' 'NR > 10 { print $2 }' \
    | while IFS= read -r old_log; do
        [ -n "${old_log}" ] && rm -f -- "${old_log}"
      done
}

trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

###############################################################################
# CONFIG
###############################################################################
if [ ! -r "${CONFIG_FILE}" ]; then
  log_json "ERROR" "config_missing" "Конфигурационный файл не найден" "${CONFIG_FILE}" 1
  echo "Ошибка: конфигурационный файл не найден: ${CONFIG_FILE}" >&2
  exit 1
fi

# shellcheck source=/dev/null
. "${CONFIG_FILE}"

TARGET_DIR="$(trim "${TARGET_DIR:-}")"
FILE_TYPES_RAW="$(trim "${FILE_TYPES:-all}")"
KEEP_DAYS="$(trim "${KEEP_DAYS:-30}")"

if [ -z "${TARGET_DIR}" ]; then
  log_json "ERROR" "config_invalid" "Не задан TARGET_DIR" "TARGET_DIR is empty" 2
  echo "Ошибка: в ${CONFIG_FILE} должен быть указан TARGET_DIR" >&2
  exit 2
fi

if [ ! -d "${TARGET_DIR}" ]; then
  log_json "ERROR" "target_missing" "Каталог не существует" "${TARGET_DIR}" 2
  echo "Ошибка: каталог не существует: ${TARGET_DIR}" >&2
  exit 2
fi

case "${KEEP_DAYS}" in
  ''|*[!0-9]*)
    log_json "ERROR" "config_invalid" "KEEP_DAYS должен быть целым числом" "${KEEP_DAYS}" 2
    echo "Ошибка: KEEP_DAYS должен быть целым неотрицательным числом" >&2
    exit 2
    ;;
esac

log_json "INFO" "start" "Запуск автоудаления" "target=${TARGET_DIR}; types=${FILE_TYPES_RAW}; keep_days=${KEEP_DAYS}" 0

DELETED_COUNT=0
FOUND_COUNT=0

delete_one_file() {
  _file="$1"
  if rm -f -- "${_file}"; then
    DELETED_COUNT=$((DELETED_COUNT + 1))
    log_json "INFO" "deleted" "Удален файл" "${_file}" 0
  else
    log_json "ERROR" "delete_failed" "Не удалось удалить файл" "${_file}" 1
  fi
}

process_list_file() {
  _list_file="$1"

  while IFS= read -r old_file; do
    [ -z "${old_file}" ] && continue
    FOUND_COUNT=$((FOUND_COUNT + 1))
    delete_one_file "${old_file}"
  done < "${_list_file}"
}

delete_all_types() {
  _list_file=$(mktemp)
  find "${TARGET_DIR}" -type f -mtime "+${KEEP_DAYS}" -print > "${_list_file}"
  process_list_file "${_list_file}"
  rm -f -- "${_list_file}"
}

delete_selected_types() {
  _types=$(printf '%s' "${FILE_TYPES_RAW}" | tr ',' ' ')
  _list_file=$(mktemp)
  : > "${_list_file}"

  for t in ${_types}; do
    _type="$(trim "${t}")"
    [ -z "${_type}" ] && continue
    _type="${_type#.}"

    find "${TARGET_DIR}" -type f -name "*.${_type}" -mtime "+${KEEP_DAYS}" -print >> "${_list_file}"
  done

  sort -u "${_list_file}" -o "${_list_file}"
  process_list_file "${_list_file}"
  rm -f -- "${_list_file}"
}

case "${FILE_TYPES_RAW}" in
  all|ALL|"*"|"")
    delete_all_types
    ;;
  *)
    delete_selected_types
    ;;
esac

log_json "INFO" "done" "Автоудаление завершено" "matched=${FOUND_COUNT}; deleted=${DELETED_COUNT}" 0
cleanup_logs
exit 0
