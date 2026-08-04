#!/bin/sh

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)

CONFIG_DIR="${SCRIPT_DIR}/conf"
CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"

mkdir -p "$CONFIG_DIR" "$LOG_DIR"

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

log_json() {
    level="$1"
    event="$2"
    msg="$3"
    detail="${4:-}"
    rc="${5:-null}"
    level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"

    msg_esc="$(json_escape "$msg")"
    detail_esc="$(json_escape "$detail")"

    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
        "$(ts)" "$LOG_SCHEMA_VERSION" "$LOG_COMPAT_TARGETS" "$level_norm" "$msg_esc" "$event" "$SCRIPT_BASE" "$SCRIPT_NAME" "$event" "$level_norm" "$msg_esc" "$detail_esc" "$rc" >> "$LOG_FILE"
}

cleanup_logs() {
    find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
        | sort -nr \
        | awk -F'|' 'NR > 10 { print $2 }' \
        | while IFS= read -r old_log; do
                [ -n "$old_log" ] && rm -f -- "$old_log"
            done
}

###############################################################################
# DEFAULT CONFIG
###############################################################################
IP_SERVICE_URL="https://icanhazip.com"
IP_FILE="/var/downloads/clouddata/nextcloud/work/ip.txt"
IP_HISTORY_FILE="/var/downloads/clouddata/nextcloud/work/ip_history.txt"

###############################################################################
# LOAD CONFIG
###############################################################################
if [ -r "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    . "$CONFIG_FILE"
else
    warn_msg="Конфиг не найден (${CONFIG_FILE}), используются значения по умолчанию"
    echo "Внимание: ${warn_msg}" >&2
    log_json "WARN" "config_missing" "$warn_msg" "$CONFIG_FILE" 0
fi

###############################################################################
# VALIDATION
###############################################################################
if ! command -v curl >/dev/null 2>&1; then
    err_msg="curl не установлен"
    echo "Ошибка: ${err_msg}" >&2
    log_json "ERROR" "dependency_missing" "$err_msg" "install curl" 1
    exit 1
fi

mkdir -p "$(dirname "$IP_FILE")"
mkdir -p "$(dirname "$IP_HISTORY_FILE")"

###############################################################################
# MAIN
###############################################################################
log_json "INFO" "start" "Запуск проверки внешнего IP" "service=${IP_SERVICE_URL}; ip_file=${IP_FILE}; history=${IP_HISTORY_FILE}" 0

IP=$(curl -fsS --max-time 10 "$IP_SERVICE_URL" 2>/dev/null | tr -d '[:space:]' || true)

if [ -z "${IP:-}" ]; then
    err_msg="Не удалось получить IP от ${IP_SERVICE_URL}"
    echo "Ошибка: ${err_msg}" >&2
    log_json "ERROR" "ip_fetch_failed" "$err_msg" "empty response" 1
    cleanup_logs
    exit 1
fi

printf '%s\n' "$IP" > "$IP_FILE"
log_json "INFO" "ip_saved" "Текущий IP сохранен" "$IP_FILE" 0

if ! grep -qF "$IP" "$IP_HISTORY_FILE" 2>/dev/null; then
    printf '%s - %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$IP" >> "$IP_HISTORY_FILE"
    log_json "INFO" "history_appended" "IP добавлен в историю" "$IP" 0
else
    log_json "INFO" "history_skip" "IP уже присутствует в истории" "$IP" 0
fi

log_json "INFO" "done" "Скрипт завершен успешно" "ip=${IP}" 0
cleanup_logs
exit 0
