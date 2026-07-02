#!/usr/bin/env bash
# Не используем set -e, чтобы скрипт не падал при одиночной ошибке mountpoint
set -uo pipefail

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

APP_NAME="MountCheck"
ICON_NAME="drive-harddisk"
URGENCY="critical"

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
    local msg_esc detail_esc
    msg_esc="$(json_escape "$msg")"
    detail_esc="$(json_escape "$detail")"
    level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"
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

###############################################################################
# MAIN
###############################################################################
# Максимальное время ожидания монтирования (должно совпадать с
# x-systemd.mount-timeout в /etc/fstab)
MOUNT_WAIT_SECS=30

FAILED_MOUNTS=()
log_json "INFO" "start" "Проверка точек монтирования" "/etc/fstab"

while read -r device mount_point type options dump pass; do
    [[ "$device" =~ ^(#|$) ]] && continue
    [[ "$mount_point" =~ ^/(proc|sys|dev|run|tmp|boot) ]] && continue
    [[ "$type" == "swap" || "$type" == "none" ]] && continue

    MP_CLEAN=$(printf '%b' "${mount_point//\\/\\\\}")

    # Для точек с x-systemd.automount: доступ к директории инициирует монтирование.
    # Повторяем до MOUNT_WAIT_SECS секунд — сеть может стать доступной чуть позже.
    ELAPSED=0
    while true; do
        timeout 5 ls -- "$MP_CLEAN" >/dev/null 2>&1 || true
        mountpoint -q -- "$MP_CLEAN" && break
        [[ $ELAPSED -ge $MOUNT_WAIT_SECS ]] && break
        sleep 3
        ELAPSED=$(( ELAPSED + 3 ))
    done

    if ! mountpoint -q -- "$MP_CLEAN"; then
        FAILED_MOUNTS+=("$MP_CLEAN")
        log_json "WARN" "mount_missing" "Точка не смонтирована" "$MP_CLEAN"
    fi
done < /etc/fstab

if [[ ${#FAILED_MOUNTS[@]} -gt 0 ]]; then
    FAILED_STR=$(IFS=', '; echo "${FAILED_MOUNTS[*]}")
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -a "$APP_NAME" -i "$ICON_NAME" -u "$URGENCY" \
            "Ошибка монтирования" "Не активны: $FAILED_STR"
    fi
    echo "Ошибка: не смонтированы $FAILED_STR"
    log_json "ERROR" "done" "Найдены несмонтированные точки" "$FAILED_STR"
    cleanup_logs
    exit 1
fi

echo "Все диски на месте."
log_json "INFO" "done" "Все точки смонтированы"
cleanup_logs
exit 0
