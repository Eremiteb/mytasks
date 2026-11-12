#!/usr/bin/env bash

# Автоудаление старых файлов по конфигу

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)

CONFIG_DIR="${SCRIPT_DIR}/conf"
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
mkdir -p "${CONFIG_DIR}" "${LOG_DIR}"

CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
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
  # Экранируем \ и ", а также \n, \r, \t и прочие управляющие символы (<0x20),
  # чтобы значение (например, имя файла) не могло разорвать запись JSONL
  # (CWE-117). Управляющие символы обрабатывает awk.
  printf '%s' "$1" | awk '
    BEGIN { for (i = 1; i < 32; i++) ctl[sprintf("%c", i)] = i }
    {
      if (NR > 1) printf "%s", "\\n"
      n = length($0)
      for (j = 1; j <= n; j++) {
        c = substr($0, j, 1)
        if (c == "\\") printf "%s", "\\\\"
        else if (c == "\"") printf "%s", "\\\""
        else if (c == "\t") printf "%s", "\\t"
        else if (c == "\r") printf "%s", "\\r"
        else if (c in ctl) printf "\\u%04x", ctl[c]
        else printf "%s", c
      }
    }'
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
  _ts="$(ts)"

  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${_level_norm}" "${_msg_esc}" "${_event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${_event}" "${_level_norm}" "${_msg_esc}" "${_detail_esc}" "${_rc}" >> "${LOG_FILE}"
}

cleanup_logs() {
  find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
    | sort -nr \
    | awk -F'|' -v keep="${KEEP_LOGS:-10}" 'NR > keep { print $2 }' \
    | while IFS= read -r old_log; do
        [[ -n "${old_log}" ]] && rm -f -- "${old_log}"
      done
}

trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

validate_target_dir() {
  _target_candidate="$1"

  if [[ ! -d "${_target_candidate}" ]]; then
    log_json "ERROR" "target_missing" "Каталог не существует" "${_target_candidate}" 2
    echo "Ошибка: каталог не существует: ${_target_candidate}" >&2
    return 1
  fi

  # Защита от катастрофического удаления: реальный путь не может быть корнем,
  # домашним каталогом или каталогом глубины < 2 (например, /mnt).
  _target_real="$(realpath -- "${_target_candidate}")" || {
    log_json "ERROR" "config_invalid" "Не удалось определить реальный путь каталога" "${_target_candidate}" 2
    echo "Ошибка: не удалось определить реальный путь: ${_target_candidate}" >&2
    return 1
  }
  _target_depth="$(printf '%s' "${_target_real}" | awk -F/ '{ n = 0; for (i = 1; i <= NF; i++) if ($i != "") n++; print n }')"
  if [[ "${_target_real}" == "/" || "${_target_real}" == "${HOME_REAL}" || "${_target_depth}" -lt 2 ]]; then
    log_json "ERROR" "target_unsafe" "Каталог слишком общий (корень, домашний каталог или глубина < 2)" "${_target_real}" 2
    echo "Ошибка: небезопасный каталог: ${_target_real}" >&2
    return 1
  fi

  printf '%s\0' "${_target_real}" >> "${TARGET_LIST_FILE}"
}

cleanup_temp_files() {
  rm -f -- "${TARGET_LIST_FILE:-}" "${MATCH_LIST_FILE:-}"
}

###############################################################################
# ARGS
###############################################################################
DRY_RUN=0
for arg in "$@"; do
  case "${arg}" in
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Использование: ${SCRIPT_NAME} [-n|--dry-run]"
      echo "  -n, --dry-run  только показать, какие файлы будут удалены, без удаления"
      exit 0
      ;;
    *)
      echo "Неизвестный аргумент: ${arg}" >&2
      exit 2
      ;;
  esac
done

###############################################################################
# CONFIG
###############################################################################
if [[ ! -r "${CONFIG_FILE}" ]]; then
  log_json "ERROR" "config_missing" "Конфигурационный файл не найден" "${CONFIG_FILE}" 1
  echo "Ошибка: конфигурационный файл не найден: ${CONFIG_FILE}" >&2
  exit 1
fi

# shellcheck source=/dev/null
. "${CONFIG_FILE}"

TARGET_DIRS_RAW="${TARGET_DIRS:-}"
FILE_TYPES_RAW="$(trim "${FILE_TYPES:-all}")"
KEEP_DAYS="$(trim "${KEEP_DAYS:-30}")"
TARGET_LIST_FILE="$(mktemp)"
trap cleanup_temp_files EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
MATCH_LIST_FILE="$(mktemp)"
HOME_REAL="$(realpath -- "${HOME:-/nonexistent}" 2>/dev/null || printf '%s' "${HOME:-}")"

TARGET_COUNT=0
while IFS= read -r _target_candidate || [[ -n "${_target_candidate}" ]]; do
  _target_trimmed="$(trim "${_target_candidate}")"
  [[ -z "${_target_trimmed}" ]] && continue
  TARGET_COUNT=$((TARGET_COUNT + 1))
  if ! validate_target_dir "${_target_trimmed}"; then
    exit 2
  fi
done <<EOF
${TARGET_DIRS_RAW}
EOF

if [[ "${TARGET_COUNT}" -eq 0 ]]; then
  log_json "ERROR" "config_invalid" "TARGET_DIRS не содержит каталогов" "${CONFIG_FILE}" 2
  echo "Ошибка: в ${CONFIG_FILE} должен быть указан хотя бы один каталог в TARGET_DIRS" >&2
  exit 2
fi

case "${KEEP_DAYS}" in
  ''|*[!0-9]*)
    log_json "ERROR" "config_invalid" "KEEP_DAYS должен быть целым числом" "${KEEP_DAYS}" 2
    echo "Ошибка: KEEP_DAYS должен быть целым неотрицательным числом" >&2
    exit 2
    ;;
  *)
    :
    ;;
esac


# Каждый тип файла подставляется в шаблон find -name — допускаем только
# безопасные расширения (без *, ?, [ ], / и т.п.). Проверка до любых удалений.
case "${FILE_TYPES_RAW}" in
  all|ALL|"*"|"") : ;;
  *)
    read -r -a FILE_TYPES_LIST <<< "${FILE_TYPES_RAW//,/ }"
    for _t in "${FILE_TYPES_LIST[@]}"; do
      _t="${_t#.}"
      case "${_t}" in
        ''|*[!A-Za-z0-9_-]*)
          log_json "ERROR" "config_invalid" "Недопустимый тип файла в FILE_TYPES" "${_t}" 2
          echo "Ошибка: недопустимый тип в FILE_TYPES: ${_t} (разрешено: A-Za-z0-9_-)" >&2
          exit 2
          ;;
        *) : ;;
      esac
    done
    FILE_TYPES_LIST=("${FILE_TYPES_LIST[@]/#./}")
    ;;
esac
log_json "INFO" "start" "Запуск автоудаления" "targets=${TARGET_COUNT}; types=${FILE_TYPES_RAW}; keep_days=${KEEP_DAYS}" 0

DELETED_COUNT=0
FOUND_COUNT=0

delete_one_file() {
  _file="$1"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] будет: удалён файл: %s\n' "${_file}"
    log_json "INFO" "dry_deleted" "[dry-run] Был бы удален файл" "${_file}" 0
    DELETED_COUNT=$((DELETED_COUNT + 1))
    return 0
  fi
  if rm -f -- "${_file}"; then
    DELETED_COUNT=$((DELETED_COUNT + 1))
    log_json "INFO" "deleted" "Удален файл" "${_file}" 0
  else
    log_json "ERROR" "delete_failed" "Не удалось удалить файл" "${_file}" 1
  fi
}

process_list_file() {
  _list_file="$1"

  while IFS= read -r -d '' old_file; do
    [[ -z "${old_file}" ]] && continue
    FOUND_COUNT=$((FOUND_COUNT + 1))
    delete_one_file "${old_file}"
  done < "${_list_file}"
}

delete_all_types() {
  : > "${MATCH_LIST_FILE}"
  while IFS= read -r -d '' _target_dir; do
    find "${_target_dir}" -xdev -type f -mtime "+${KEEP_DAYS}" -print0 >> "${MATCH_LIST_FILE}"
  done < "${TARGET_LIST_FILE}"
  sort -zu "${MATCH_LIST_FILE}" -o "${MATCH_LIST_FILE}"
  process_list_file "${MATCH_LIST_FILE}"
}

delete_selected_types() {
  : > "${MATCH_LIST_FILE}"

  for _type in "${FILE_TYPES_LIST[@]}"; do
    while IFS= read -r -d '' _target_dir; do
      find "${_target_dir}" -xdev -type f -name "*.${_type}" -mtime "+${KEEP_DAYS}" -print0 >> "${MATCH_LIST_FILE}"
    done < "${TARGET_LIST_FILE}"
  done

  sort -zu "${MATCH_LIST_FILE}" -o "${MATCH_LIST_FILE}"
  process_list_file "${MATCH_LIST_FILE}"
}

case "${FILE_TYPES_RAW}" in
  all|ALL|"*"|"")
    delete_all_types
    ;;
  *)
    delete_selected_types
    ;;
esac

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "[dry-run] Изменения не выполнены: подошло файлов ${FOUND_COUNT}."
fi

log_json "INFO" "done" "Автоудаление завершено" "matched=${FOUND_COUNT}; deleted=${DELETED_COUNT}" 0
cleanup_logs
exit 0
