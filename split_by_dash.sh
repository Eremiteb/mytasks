#!/bin/sh

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)

LOG_DIR="${SCRIPT_DIR}/logs"
CONFIG_DIR="${SCRIPT_DIR}/conf"
CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
mkdir -p "${LOG_DIR}" "${CONFIG_DIR}"

CREATED_DIRS_FILE="$(mktemp)"
trap 'rm -f "${CREATED_DIRS_FILE}"' EXIT

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
  level="$1"
  event="$2"
  msg="$3"
  detail="${4:-}"
  level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
  msg_esc="$(json_escape "${msg}")"
  detail_esc="$(json_escape "${detail}")"
  _ts="$(ts)"
  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
    "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
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

process_directory() {
  current_dir="$1"
  if [ ! -d "${current_dir}" ]; then
    log_json "ERROR" "dir_missing" "Каталог не существует" "${current_dir}"
    return
  fi

  log_json "INFO" "dir_start" "Обработка директории" "${current_dir}"
  find "${current_dir}" -maxdepth 1 -type f | while IFS= read -r file; do
    name=$(basename -- "${file}")
    folder=$(printf '%s\n' "${name}" | sed -n 's/^\(.*\)[[:space:]][-–—][[:space:]].*/\1/p' | sed 's/[[:space:]]*$//' | sed -n '1p')
    [ -z "${folder}" ] && continue

    target_dir="${current_dir}/${folder}"
    mkdir -p "${target_dir}"
    echo "${target_dir}" >> "${CREATED_DIRS_FILE}"

    dst="${target_dir}/${name}"
    if [ -e "${dst}" ]; then
      i=1
      while [ -e "${target_dir}/${name}.${i}" ]; do i=$((i+1)); done
      dst="${target_dir}/${name}.${i}"
    fi

    if mv -f -- "${file}" "${dst}"; then
      log_json "INFO" "moved" "Файл перемещен" "${name} -> ${dst}"
    fi
  done
}

move_created_dirs() {
  dest_dir="$1"
  mkdir -p "${dest_dir}" 2>/dev/null || true
  if [ ! -d "${dest_dir}" ]; then
    log_json "ERROR" "dest_missing" "Целевой каталог для переноса недоступен" "${dest_dir}"
    return
  fi

  log_json "INFO" "move_start" "Перенос получившихся папок в целевой каталог" "${dest_dir}"
  sort -u "${CREATED_DIRS_FILE}" | while IFS= read -r dir; do
    [ -d "${dir}" ] || continue
    dir_name=$(basename -- "${dir}")
    dir_dst="${dest_dir}/${dir_name}"

    if [ "${dir}" = "${dir_dst}" ]; then
      continue
    fi

    if [ -e "${dir_dst}" ]; then
      find "${dir}" -mindepth 1 -maxdepth 1 | while IFS= read -r item; do
        item_name=$(basename -- "${item}")
        item_dst="${dir_dst}/${item_name}"
        if [ -e "${item_dst}" ]; then
          i=1
          while [ -e "${dir_dst}/${item_name}.${i}" ]; do i=$((i+1)); done
          item_dst="${dir_dst}/${item_name}.${i}"
        fi
        if mv -f -- "${item}" "${item_dst}"; then
          log_json "INFO" "moved_item" "Файл перемещен при объединении папок" "${item} -> ${item_dst}"
        fi
      done
      rmdir "${dir}" 2>/dev/null || true
      log_json "INFO" "merged_dir" "Папка объединена с существующей в целевом каталоге" "${dir} -> ${dir_dst}"
    else
      if mv -f -- "${dir}" "${dir_dst}"; then
        log_json "INFO" "moved_dir" "Папка перемещена в целевой каталог" "${dir} -> ${dir_dst}"
      else
        log_json "ERROR" "move_dir_failed" "Не удалось переместить папку в целевой каталог" "${dir} -> ${dir_dst}"
      fi
    fi
  done
}

###############################################################################
# MAIN
###############################################################################
DEST_DIR=""
if [ -r "${CONFIG_FILE}" ]; then
  DEST_DIR=$(sed -n 's/^[[:space:]]*DEST_DIR[[:space:]]*=[[:space:]]*//p' "${CONFIG_FILE}" | sed 's/#.*//' | tail -n1)
  DEST_DIR=$(trim "${DEST_DIR}")
fi

if [ -n "${1:-}" ] && [ -d "$1" ]; then
  process_directory "$1"
elif [ -r "${CONFIG_FILE}" ]; then
  log_json "INFO" "config_used" "Использование конфига" "${CONFIG_FILE}"
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ""|\#*) continue ;; *) ;; esac
    case "${line}" in *DEST_DIR=*) continue ;; *) ;; esac
    target=$(trim "$({ printf '%s' "${line}" | sed 's/#.*//'; } || true)")
    [ -n "${target}" ] && process_directory "${target}"
  done < "${CONFIG_FILE}"
else
  err="Ошибка: каталог не указан и конфиг ${CONFIG_FILE} не найден."
  echo "${err}" >&2
  log_json "ERROR" "config_missing" "${err}"
  cleanup_logs
  exit 1
fi

if [ -n "${DEST_DIR}" ]; then
  move_created_dirs "${DEST_DIR}"
fi

cleanup_logs
exit 0