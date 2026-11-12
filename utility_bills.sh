#!/usr/bin/env bash

set -euo pipefail
# Логи и вывод движка содержат персональные данные (суммы, имена файлов квитанций): только владельцу.
umask 077

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)

PROJECT_PATH="${SCRIPT_DIR}/utility_bills"
VENV_PATH="${PROJECT_PATH}/venv"
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
# Значения по умолчанию; conf/utility_bills.conf их переопределяет
CRON_LOG="${LOG_DIR}/cron_execution.log"
PYTHON_BIN="python3"
CONFIG_FILE="${SCRIPT_DIR}/conf/${SCRIPT_BASE}.conf"
LOCK_FILE="${PROJECT_PATH}/.${SCRIPT_BASE}.lock"
mkdir -p "${LOG_DIR}" "${SCRIPT_DIR}/conf"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
fi
if [[ -r "${CONFIG_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${CONFIG_FILE}"
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
        { find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
            | sort -nr \
            | awk -F'|' -v keep="${KEEP_LOGS:-10}" 'NR > keep { print $2 }'; } || true
    )
    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

usage() {
    echo "Использование: ${SCRIPT_NAME} [-n|--dry-run] [ключи utility_bills.py]"
    echo "  -n, --dry-run        ничего не менять (БД, почтовый ящик), показать план"
    echo "  --file PDF ...       импортировать уже скачанные PDF вместо почты"
    echo "  --reparse            разобрать заново все PDF из базы текущей версией парсера"
    echo "  --check              проверить конфигурацию, tesseract и pdfimages"
    echo "  --migrate-db         только миграции схемы БД"
    echo "Настройки: utility_bills/utility_bills.json (пример — utility_bills.json.example)."
}

# Один запуск за раз: таймер и ручной запуск пишут в одну БД.
acquire_lock() {
    exec {LOCK_FD}>"${LOCK_FILE}"
    if ! flock -n "${LOCK_FD}"; then
        log_json "ERROR" "already_running" "utility_bills уже выполняется" "${LOCK_FILE}"
        echo "utility_bills уже выполняется (блокировка ${LOCK_FILE})" >&2
        cleanup_logs
        exit 3
    fi
}

###############################################################################
# ARGS
###############################################################################
DRY_RUN=0
PY_ARGS=()
for arg in "$@"; do
    case "${arg}" in
        -n|--dry-run)
            DRY_RUN=1
            PY_ARGS+=("--dry-run")
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            PY_ARGS+=("${arg}")
            ;;
    esac
done

###############################################################################
# MAIN
###############################################################################
log_json "INFO" "start" "Запуск utility_bills" "${PY_ARGS[*]}"

if [[ ! -f "${PROJECT_PATH}/utility_bills.py" ]]; then
    log_json "ERROR" "project_missing" "Проект не найден" "${PROJECT_PATH}"
    echo "Проект не найден: ${PROJECT_PATH}" >&2
    cleanup_logs
    exit 1
fi

acquire_lock

PYTHON="${PYTHON_BIN}"
if [[ -x "${VENV_PATH}/bin/python" ]]; then
    PYTHON="${VENV_PATH}/bin/python"
    log_json "INFO" "venv_used" "Используется виртуальное окружение" "${VENV_PATH}"
else
    log_json "WARN" "venv_missing" "Нет виртуального окружения: запустите utility_bills/setup_venv.sh" "${VENV_PATH}"
fi

PYTHON_EXIT_CODE=0
if [[ "${DRY_RUN}" -eq 1 ]]; then
    # План dry-run выводится в терминал.
    "${PYTHON}" "${PROJECT_PATH}/utility_bills.py" "${PY_ARGS[@]}" || PYTHON_EXIT_CODE=$?
else
    "${PYTHON}" "${PROJECT_PATH}/utility_bills.py" "${PY_ARGS[@]}" >> "${CRON_LOG}" 2>&1 || PYTHON_EXIT_CODE=$?
fi

case "${PYTHON_EXIT_CODE}" in
    0) log_json "INFO" "done" "Завершение работы" "rc=${PYTHON_EXIT_CODE}" ;;
    2) log_json "ERROR" "config_error" "Ошибка конфигурации или нет tesseract/pdfimages" "rc=${PYTHON_EXIT_CODE}" ;;
    *) log_json "ERROR" "engine_failed" "utility_bills.py завершился с ошибкой" "rc=${PYTHON_EXIT_CODE}" ;;
esac

cleanup_logs
exit "${PYTHON_EXIT_CODE}"
