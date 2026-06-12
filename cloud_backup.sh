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
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"

mkdir -p "$LOG_DIR"

if [ -r "$LOG_TEMPLATE_FILE" ]; then
  # shellcheck source=/dev/null
  source "$LOG_TEMPLATE_FILE"
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
  local rc="${5:-null}"
  local ts_val msg_esc detail_esc level_norm
  ts_val="$(ts)"
  msg_esc="$(json_escape "$msg")"
  detail_esc="$(json_escape "$detail")"
  level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"
  printf '{"@timestamp":"%s","ts":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "$ts_val" "$ts_val" "$LOG_SCHEMA_VERSION" "$LOG_COMPAT_TARGETS" "$level_norm" "$msg_esc" "$event" "$SCRIPT_BASE" "$SCRIPT_NAME" "$event" "$level_norm" "$msg_esc" "$detail_esc" "$rc" >> "$LOG_FILE"
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

SUDO_PASSWORD="${SUDO_PASSWORD:-}"

REMOTE_PATH="${REMOTE_PATH:-/opt/esimych-cloud}"
REMOTE_SSH_PORT="${REMOTE_SSH_PORT:-22}"
WG_KEEP_UP="${WG_KEEP_UP:-0}"

###############################################################################
# WireGuard
###############################################################################
WG_BROUGHT_UP=0
SERVICES_STOPPED=0

wg_down_if_needed() {
  if [ "$WG_BROUGHT_UP" -eq 1 ] && [ "${WG_KEEP_UP:-0}" -ne 1 ]; then
    log_json "INFO" "wg_down" "Останавливаем WireGuard ${WG_INTERFACE}..."
    if [ -n "$SUDO_PASSWORD" ]; then
      echo "$SUDO_PASSWORD" | sudo -S wg-quick down "$WG_INTERFACE" 2>/dev/null && \
        log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} остановлен" || \
        log_json "WARN" "wg_down_fail" "Не удалось остановить WireGuard ${WG_INTERFACE}"
    else
      wg-quick down "$WG_INTERFACE" 2>/dev/null && \
        log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} остановлен" || \
        log_json "WARN" "wg_down_fail" "Не удалось остановить WireGuard ${WG_INTERFACE}"
    fi
  fi
}

services_start_if_needed() {
  if [ "$SERVICES_STOPPED" -eq 1 ]; then
    SERVICES_STOPPED=0
    log_json "INFO" "services_start" "Запускаем сервисы на ${REMOTE_HOST}..."
    export SSHPASS="${REMOTE_PASSWORD}"
    start_err=$(sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
      "cd '${REMOTE_PATH}' && docker compose up -d" 2>&1)
    start_rc=$?
    unset SSHPASS
    if [ $start_rc -ne 0 ]; then
      log_json "WARN" "services_start_failed" "Не удалось запустить сервисы" "$start_err" $start_rc
    else
      log_json "INFO" "services_start_ok" "Сервисы запущены" "" $start_rc
    fi
  fi
}

cleanup() {
  services_start_if_needed
  wg_down_if_needed
  cleanup_logs
}
trap cleanup EXIT INT TERM

log_json "INFO" "start" "Запуск резервного копирования"

if wg show "$WG_INTERFACE" >/dev/null 2>&1; then
  log_json "INFO" "wg_status" "WireGuard ${WG_INTERFACE} уже активен"
else
  log_json "INFO" "wg_up" "Поднимаем WireGuard ${WG_INTERFACE}..."
  if [ -n "$SUDO_PASSWORD" ]; then
    wg_err=$(echo "$SUDO_PASSWORD" | sudo -S wg-quick up "$WG_INTERFACE" 2>&1)
  else
    wg_err=$(wg-quick up "$WG_INTERFACE" 2>&1)
  fi
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
REMOTE_PARENT="$(dirname "$REMOTE_PATH")"
REMOTE_DIR="$(basename "$REMOTE_PATH")"

SSH_OPTS="-p ${REMOTE_SSH_PORT} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -o BatchMode=no -o Compression=no -c aes128-gcm@openssh.com,chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-ctr"

# Выбираем быстрейший доступный компрессор на удалённом сервере:
# zstd --fast=1 --threads=0 > pigz -1 > gzip -1
export SSHPASS="${REMOTE_PASSWORD}"
REMOTE_COMP=$(sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
  "if command -v zstd >/dev/null 2>&1; then echo zstd; elif command -v pigz >/dev/null 2>&1; then echo pigz; else echo gzip; fi" 2>/dev/null)
unset SSHPASS
case "$REMOTE_COMP" in
  zstd) COMP_CMD="zstd --fast=1 --threads=0 -c"; BACKUP_EXT="tar.zst" ;;
  pigz) COMP_CMD="pigz -1";                       BACKUP_EXT="tar.gz"  ;;
  *)    COMP_CMD="gzip -1";                        BACKUP_EXT="tar.gz"  ;;
esac

BACKUP_FILENAME="${SCRIPT_BASE}-${BACKUP_DATE}.${BACKUP_EXT}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

log_json "INFO" "backup_start" "Начало резервного копирования" \
  "remote=${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH} -> ${BACKUP_PATH}, comp=${REMOTE_COMP:-gzip}"

###############################################################################
# NEXTCLOUD CLEANUP (occ)
###############################################################################
log_json "INFO" "occ_cleanup_start" "Очистка корзин пользователей (occ trashbin:cleanup)..."
export SSHPASS="${REMOTE_PASSWORD}"
occ_trash_err=$(sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
  "docker exec -u www-data esimych-cloud-app php occ trashbin:cleanup --all-users" 2>&1)
occ_trash_rc=$?
unset SSHPASS
if [ $occ_trash_rc -ne 0 ]; then
  log_json "WARN" "occ_cleanup_trash_failed" "Не удалось очистить корзины" "$occ_trash_err" $occ_trash_rc
else
  log_json "INFO" "occ_cleanup_trash_ok" "Корзины очищены" "$occ_trash_err" $occ_trash_rc
fi

log_json "INFO" "occ_versions_start" "Очистка версий файлов (occ versions:cleanup)..."
export SSHPASS="${REMOTE_PASSWORD}"
occ_ver_err=$(sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
  "docker exec -u www-data esimych-cloud-app php occ versions:cleanup" 2>&1)
occ_ver_rc=$?
unset SSHPASS
if [ $occ_ver_rc -ne 0 ]; then
  log_json "WARN" "occ_cleanup_versions_failed" "Не удалось очистить версии файлов" "$occ_ver_err" $occ_ver_rc
else
  log_json "INFO" "occ_cleanup_versions_ok" "Версии файлов очищены" "$occ_ver_err" $occ_ver_rc
fi

log_json "INFO" "services_stop" "Останавливаем сервисы на ${REMOTE_HOST}..."
export SSHPASS="${REMOTE_PASSWORD}"
stop_err=$(sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
  "cd '${REMOTE_PATH}' && docker compose down" 2>&1)
stop_rc=$?
unset SSHPASS
if [ $stop_rc -ne 0 ]; then
  log_json "WARN" "services_stop_failed" "Не удалось остановить сервисы" "$stop_err" $stop_rc
  echo "Предупреждение: не удалось остановить сервисы (код $stop_rc): $stop_err" >&2
else
  SERVICES_STOPPED=1
  log_json "INFO" "services_stop_ok" "Сервисы остановлены" "" $stop_rc
fi

export SSHPASS="${REMOTE_PASSWORD}"

# Фоновый монитор: пишет размер файла в лог каждые 60 сек
_progress_monitor() {
  while sleep 60; do
    [ -f "$BACKUP_PATH" ] || break
    local sz
    sz=$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1)
    [ -n "$sz" ] && log_json "INFO" "backup_progress" "Прогресс архивирования" "size=${sz}"
  done
}
_progress_monitor >/dev/null 2>&1 &
_PROGRESS_PID=$!

err_tmp=$(mktemp)
sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
  "set -o pipefail; tar --create --file=- --sparse --directory='${REMOTE_PARENT}' '${REMOTE_DIR}' | ${COMP_CMD}" \
  > "$BACKUP_PATH" 2>"$err_tmp"
backup_rc=$?
err_out=$(cat "$err_tmp"); rm -f "$err_tmp"
unset SSHPASS

kill "$_PROGRESS_PID" 2>/dev/null
wait "$_PROGRESS_PID" 2>/dev/null

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
