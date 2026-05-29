#!/bin/bash
# Резервное копирование /opt/esimych-cloud с удалённого сервера через WireGuard VPN

set -uo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_DIR="${SCRIPT_DIR}/conf"
CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"

mkdir -p "$LOG_DIR"

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
  local rc="${5:-null}"
  local ts_val msg_esc detail_esc
  ts_val="$(ts)"
  msg_esc="$(json_escape "$msg")"
  detail_esc="$(json_escape "$detail")"
  printf '{"ts":"%s","level":"%s","script":"%s","event":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "$ts_val" "$level" "$SCRIPT_NAME" "$event" "$msg_esc" "$detail_esc" "$rc" >> "$LOG_FILE"
}

cleanup_logs() {
  local old_logs
  old_logs=$(ls -1t "${LOG_DIR}/${SCRIPT_BASE}-"*.jsonl 2>/dev/null | tail -n +6)
  [ -n "$old_logs" ] && rm -f $old_logs
}

###############################################################################
# CONFIG
###############################################################################
if [ ! -r "$CONFIG_FILE" ]; then
  echo "Ошибка: конфигурационный файл не найден: $CONFIG_FILE" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

for var in WG_INTERFACE REMOTE_HOST REMOTE_USER REMOTE_PASSWORD BACKUP_DIR; do
  if [ -z "${!var:-}" ]; then
    echo "Ошибка: переменная $var не задана в $CONFIG_FILE" >&2
    exit 1
  fi
done

REMOTE_PATH="${REMOTE_PATH:-/opt/esimych-cloud}"
REMOTE_SSH_PORT="${REMOTE_SSH_PORT:-22}"
WG_KEEP_UP="${WG_KEEP_UP:-0}"

###############################################################################
# WireGuard
###############################################################################
WG_BROUGHT_UP=0

wg_down_if_needed() {
  if [ "$WG_BROUGHT_UP" -eq 1 ] && [ "${WG_KEEP_UP:-0}" -ne 1 ]; then
    log_json "INFO" "wg_down" "Опускаем WireGuard ${WG_INTERFACE}..."
    sudo wg-quick down "$WG_INTERFACE" 2>/dev/null && \
      log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} опущен" || \
      log_json "WARN" "wg_down_fail" "Не удалось опустить WireGuard ${WG_INTERFACE}"
  fi
}

cleanup() {
  wg_down_if_needed
  cleanup_logs
}
trap cleanup EXIT INT TERM

log_json "INFO" "start" "Запуск резервного копирования"

if ip link show "$WG_INTERFACE" >/dev/null 2>&1; then
  log_json "INFO" "wg_status" "WireGuard ${WG_INTERFACE} уже активен"
else
  log_json "INFO" "wg_up" "Поднимаем WireGuard ${WG_INTERFACE}..."
  wg_err=$(sudo wg-quick up "$WG_INTERFACE" 2>&1)
  wg_rc=$?
  if [ $wg_rc -ne 0 ]; then
    log_json "ERROR" "wg_up_failed" "Не удалось поднять WireGuard ${WG_INTERFACE}" "$wg_err" $wg_rc
    echo "Ошибка: wg-quick up ${WG_INTERFACE} завершился с кодом $wg_rc" >&2
    exit 1
  fi
  WG_BROUGHT_UP=1
  sleep 2
  log_json "INFO" "wg_up_ok" "WireGuard ${WG_INTERFACE} успешно поднят" "" 0
fi

# Проверяем реальную связь с удалённым хостом через VPN
log_json "INFO" "wg_check" "Проверяем доступность ${REMOTE_HOST} через VPN..."
_wg_ok=0
for _attempt in 1 2 3; do
  if ping -c 1 -W 5 "$REMOTE_HOST" >/dev/null 2>&1; then
    _wg_ok=1
    break
  fi
  log_json "WARN" "wg_ping_retry" "Попытка ${_attempt}/3: хост ${REMOTE_HOST} не отвечает"
  [ "$_attempt" -lt 3 ] && sleep 5
done
if [ "$_wg_ok" -eq 0 ]; then
  log_json "ERROR" "wg_no_connection" "Хост ${REMOTE_HOST} недоступен через VPN после 3 попыток" "" 1
  echo "Ошибка: ${REMOTE_HOST} недоступен через VPN" >&2
  exit 1
fi
log_json "INFO" "wg_connected" "Соединение с ${REMOTE_HOST} подтверждено"

###############################################################################
# PREFLIGHT
###############################################################################
if ! command -v sshpass >/dev/null 2>&1; then
  log_json "ERROR" "sshpass_missing" "sshpass не установлен" "sudo pacman -S sshpass"
  echo "Ошибка: sshpass не найден. Установите: sudo pacman -S sshpass" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR" 2>/dev/null
if [ ! -d "$BACKUP_DIR" ]; then
  log_json "ERROR" "backup_dir_missing" "Папка назначения недоступна" "$BACKUP_DIR"
  echo "Ошибка: папка $BACKUP_DIR недоступна" >&2
  exit 1
fi

###############################################################################
# BACKUP
###############################################################################
BACKUP_DATE="$(date '+%Y-%m-%d')"
BACKUP_FILENAME="${SCRIPT_BASE}-${BACKUP_DATE}.tar.gz"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

REMOTE_PARENT="$(dirname "$REMOTE_PATH")"
REMOTE_DIR="$(basename "$REMOTE_PATH")"

log_json "INFO" "backup_start" "Начало резервного копирования" \
  "remote=${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH} -> ${BACKUP_PATH}"

SSH_OPTS="-p ${REMOTE_SSH_PORT} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -o BatchMode=no -o Compression=no -c aes128-gcm@openssh.com,chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-ctr"
export SSHPASS="${REMOTE_PASSWORD}"

err_tmp=$(mktemp)
sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
  "set -o pipefail; tar --create --file=- --directory='${REMOTE_PARENT}' '${REMOTE_DIR}' | gzip -1" \
  > "$BACKUP_PATH" 2>"$err_tmp"
backup_rc=$?
err_out=$(cat "$err_tmp"); rm -f "$err_tmp"
unset SSHPASS

if [ $backup_rc -eq 0 ]; then
  backup_size=$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1)
  log_json "INFO" "backup_done" "Резервная копия создана успешно" \
    "file=${BACKUP_PATH}, size=${backup_size}" $backup_rc
  echo "Готово: ${BACKUP_PATH} (${backup_size})"
else
  log_json "ERROR" "backup_failed" "Ошибка при создании резервной копии" "$err_out" $backup_rc
  echo "Ошибка резервного копирования (код $backup_rc): $err_out" >&2
  rm -f "$BACKUP_PATH" 2>/dev/null
  exit $backup_rc
fi

exit 0
