#!/usr/bin/env bash
set -uo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)
CONF_FILE="${SCRIPT_DIR}/conf/${SCRIPT_BASE}.conf"
LOG_DIR="${SCRIPT_DIR}/logs"
STATE_DIR="${SCRIPT_DIR}/state"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
STATE_FILE_DEFAULT="${STATE_DIR}/${SCRIPT_BASE}.state"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

if [[ -r "${CONF_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${CONF_FILE}"
fi

ROOT_PATH="${ROOT_PATH:-/}"
STATE_FILE="${STATE_FILE:-${STATE_FILE_DEFAULT}}"
KEEP_LOGS="${KEEP_LOGS:-10}"
APP_NAME="${APP_NAME:-BtrfsMonitor}"
ICON_NAME="${ICON_NAME:-drive-harddisk}"
URGENCY="${URGENCY:-critical}"

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
    local msg_esc detail_esc level_norm
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"

    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
        "$(ts)" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    local old_logs=()
    mapfile -t old_logs < <(
        find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
            | sort -nr \
            | awk -F'|' -v keep="${KEEP_LOGS}" 'NR > keep { print $2 }'
    )
    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

notify_alert() {
    local title="$1"
    local body="$2"

    if command -v notify-send >/dev/null 2>&1; then
        notify-send -a "${APP_NAME}" -i "${ICON_NAME}" -u "${URGENCY}" "${title}" "${body}" || true
    fi
}

require_cmd() {
    local cmd="$1"
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "Ошибка: не найдена команда '${cmd}'"
        log_json "ERROR" "dependency_missing" "Не найдена зависимость" "${cmd}"
        cleanup_logs
        exit 2
    fi
}

collect_stats() {
    btrfs device stats "${ROOT_PATH}" | awk '
        {
            if (match($1, /^\[(.*)\]\.([A-Za-z0-9_]+)$/, m)) {
                dev=m[1]
                metric=m[2]
                value=$2+0
                print dev "\t" metric "\t" value
            }
        }
    '
}

load_prev_to_map() {
    local file="$1"
    while IFS=$'\t' read -r dev metric value; do
        [[ -z "${dev:-}" || -z "${metric:-}" || -z "${value:-}" ]] && continue
        PREV["${dev}|${metric}"]="${value}"
    done < "${file}"
}

###############################################################################
# MAIN
###############################################################################
require_cmd btrfs

if ! findmnt -no FSTYPE "${ROOT_PATH}" | grep -qi '^btrfs$'; then
    echo "Ошибка: '${ROOT_PATH}' не является Btrfs"
    log_json "ERROR" "not_btrfs" "Точка не на Btrfs" "${ROOT_PATH}"
    cleanup_logs
    exit 2
fi

log_json "INFO" "start" "Проверка счетчиков Btrfs" "${ROOT_PATH}"

CURRENT_RAW="$(collect_stats 2>/dev/null || true)"
if [[ -z "${CURRENT_RAW}" ]]; then
    echo "Ошибка: не удалось получить btrfs device stats для ${ROOT_PATH}"
    log_json "ERROR" "stats_read_failed" "Не удалось прочитать btrfs device stats" "${ROOT_PATH}"
    cleanup_logs
    exit 2
fi

TMP_CURRENT="$(mktemp)"
trap 'rm -f "${TMP_CURRENT}"' EXIT
printf '%s\n' "${CURRENT_RAW}" > "${TMP_CURRENT}"

if [[ ! -f "${STATE_FILE}" ]]; then
    cp "${TMP_CURRENT}" "${STATE_FILE}"
    echo "Инициализация: сохранено базовое состояние в ${STATE_FILE}"
    log_json "INFO" "state_initialized" "Создано базовое состояние" "${STATE_FILE}"
    cleanup_logs
    exit 0
fi

declare -A PREV=()
declare -a GROWTH_LINES=()
declare -a RESET_LINES=()

load_prev_to_map "${STATE_FILE}"

while IFS=$'\t' read -r dev metric value; do
    [[ -z "${dev:-}" || -z "${metric:-}" || -z "${value:-}" ]] && continue
    key="${dev}|${metric}"
    prev="${PREV[${key}]:-0}"

    if [[ "${value}" =~ ^[0-9]+$ && "${prev}" =~ ^[0-9]+$ ]]; then
        if (( value > prev )); then
            delta=$((value - prev))
            GROWTH_LINES+=("${dev} ${metric}: ${prev} -> ${value} (+${delta})")
        elif (( value < prev )); then
            RESET_LINES+=("${dev} ${metric}: ${prev} -> ${value}")
        fi
    fi
done < "${TMP_CURRENT}"

cp "${TMP_CURRENT}" "${STATE_FILE}"

if [[ ${#GROWTH_LINES[@]} -gt 0 ]]; then
    detail="$(printf '%s; ' "${GROWTH_LINES[@]}")"
    detail="${detail%; }"

    echo "ALERT: обнаружен рост счетчиков ошибок Btrfs"
    printf '%s\n' "${GROWTH_LINES[@]}"

    notify_alert "Btrfs: рост счетчиков ошибок" "${detail}"
    log_json "ERROR" "errors_growth" "Обнаружен рост счетчиков ошибок Btrfs" "${detail}"

    if [[ ${#RESET_LINES[@]} -gt 0 ]]; then
        reset_detail="$(printf '%s; ' "${RESET_LINES[@]}")"
        reset_detail="${reset_detail%; }"
        log_json "INFO" "counter_reset" "Обнаружено уменьшение счетчиков (reset/zero)" "${reset_detail}"
    fi

    cleanup_logs
    exit 1
fi

if [[ ${#RESET_LINES[@]} -gt 0 ]]; then
    reset_detail="$(printf '%s; ' "${RESET_LINES[@]}")"
    reset_detail="${reset_detail%; }"
    log_json "INFO" "counter_reset" "Обнаружено уменьшение счетчиков (reset/zero)" "${reset_detail}"
fi

echo "OK: роста счетчиков ошибок Btrfs не обнаружено"
log_json "INFO" "done" "Рост счетчиков ошибок Btrfs не обнаружен"
cleanup_logs
exit 0
