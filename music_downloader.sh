#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)

PROJECT_PATH="${SCRIPT_DIR}/music_downloader"
VENV_PATH="${PROJECT_PATH}/venv"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
CRON_LOG="${LOG_DIR}/cron_execution.log"
mkdir -p "${LOG_DIR}"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
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
    local level_norm msg_esc detail_esc _ts
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    _ts="$(ts)"
    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
        "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    local old_logs=()
    mapfile -t old_logs < <(
        find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
            | sort -nr \
            | awk -F'|' 'NR > 10 { print $2 }'
    )
    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

###############################################################################
# MAIN
###############################################################################
log_json "INFO" "start" "Запуск music_downloader"

if [[ ! -d "${PROJECT_PATH}" ]]; then
    log_json "ERROR" "project_missing" "Проект не найден" "${PROJECT_PATH}"
    cleanup_logs
    exit 1
fi

cd "${PROJECT_PATH}"
VENV_ACTIVE=0
if [[ -f "${VENV_PATH}/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "${VENV_PATH}/bin/activate"
    VENV_ACTIVE=1
    log_json "INFO" "venv_activated" "Активировано виртуальное окружение" "${VENV_PATH}"
fi

set +e
python3 music_downloader.py >> "${CRON_LOG}" 2>&1
PYTHON_EXIT_CODE=$?
set -e

if [[ "${VENV_ACTIVE}" -eq 1 ]]; then
    deactivate
fi

if [[ ${PYTHON_EXIT_CODE} -eq 0 ]]; then
    SPLIT_SCRIPT="${SCRIPT_DIR}/split_by_dash.sh"
    if [[ -f "${SPLIT_SCRIPT}" ]]; then
        log_json "INFO" "split_start" "Запуск сортировщика" "${SPLIT_SCRIPT}"
        /bin/sh "${SPLIT_SCRIPT}" >> "${CRON_LOG}" 2>&1 || log_json "WARN" "split_failed" "Сортировщик завершился с ошибкой"
    else
        log_json "ERROR" "split_missing" "Сортировщик не найден" "${SPLIT_SCRIPT}"
    fi
else
    log_json "ERROR" "downloader_failed" "Загрузчик завершился с ошибкой" "rc=${PYTHON_EXIT_CODE}"
fi

log_json "INFO" "done" "Завершение работы" "rc=${PYTHON_EXIT_CODE}"
cleanup_logs
exit "${PYTHON_EXIT_CODE}"