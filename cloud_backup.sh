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
PROGRESS_METRICS_FILE=""

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
  # Удаляем/нормализуем управляющие символы, чтобы каждая запись оставалась
  # корректной одной строкой JSONL даже при шумном stderr внешних команд.
  printf '%s' "$1" \
    | tr '\n\t' '  ' \
    | tr -d '\000-\010\013\014\016-\037' \
    | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
}

log_json() {
  local level="$1"
  local event="$2"
  local msg="$3"
  local detail="${4:-}"
  local rc="${5:-null}"
  local ts_val msg_esc detail_esc level_norm
  ts_val="$(ts)"
  msg_esc="$(json_escape "${msg}")"
  detail_esc="$(json_escape "${detail}")"
  level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
  printf '{"@timestamp":"%s","ts":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "${ts_val}" "${ts_val}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" "${rc}" >> "${LOG_FILE}"
}

# shellcheck disable=SC2329
cleanup_logs() {
  local old_logs=() old_log old_logs_file
  old_logs_file="$(mktemp)"
  if find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
      | sort -nr \
      | awk -F'|' 'NR > 5 { print $2 }' > "${old_logs_file}"; then
    while IFS= read -r old_log; do
      [[ -n "${old_log}" ]] && old_logs+=("${old_log}")
    done < "${old_logs_file}"
  fi
  rm -f -- "${old_logs_file}"
  if ((${#old_logs[@]} > 0)); then
    rm -f -- "${old_logs[@]}"
  fi
}

VALIDATE_BACKUP_DETAIL=""

validate_backup_file() {
  local backup_path="$1"
  local validate_out rc

  VALIDATE_BACKUP_DETAIL=""

  if [[ ! -s "${backup_path}" ]]; then
    VALIDATE_BACKUP_DETAIL="file is empty"
    return 1
  fi

  case "${backup_path}" in
    *.tar.gz)
      if ! command -v gzip >/dev/null 2>&1; then
        VALIDATE_BACKUP_DETAIL="gzip not found"
        return 2
      fi
      validate_out=$(gzip -t "${backup_path}" 2>&1)
      rc=$?
      VALIDATE_BACKUP_DETAIL="${validate_out}"
      return "${rc}"
      ;;
    *.tar.zst)
      if ! command -v zstd >/dev/null 2>&1; then
        VALIDATE_BACKUP_DETAIL="zstd not found"
        return 2
      fi
      validate_out=$(zstd -t "${backup_path}" 2>&1)
      rc=$?
      VALIDATE_BACKUP_DETAIL="${validate_out}"
      return "${rc}"
      ;;
    *)
      VALIDATE_BACKUP_DETAIL="unsupported backup extension"
      return 2
      ;;
  esac
}

###############################################################################
# CONFIG
###############################################################################
if [[ ! -r "${CONFIG_FILE}" ]]; then
  echo "Ошибка: конфигурационный файл не найден: ${CONFIG_FILE}" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"

# Значения по умолчанию нужны для IDE-анализатора: фактически переменные
# должны приходить из conf и валидируются ниже.
WG_INTERFACE="${WG_INTERFACE:-}"
REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_USER="${REMOTE_USER:-}"
REMOTE_PASSWORD="${REMOTE_PASSWORD:-}"
BACKUP_DIR="${BACKUP_DIR:-}"

for var in WG_INTERFACE REMOTE_HOST REMOTE_USER REMOTE_PASSWORD BACKUP_DIR; do
  if [[ -z "${!var:-}" ]]; then
    echo "Ошибка: переменная ${var} не задана в ${CONFIG_FILE}" >&2
    exit 1
  fi
done

SUDO_PASSWORD="${SUDO_PASSWORD:-}"

REMOTE_PATH="${REMOTE_PATH:-/opt/esimych-cloud}"
REMOTE_SSH_PORT="${REMOTE_SSH_PORT:-22}"
WG_KEEP_UP="${WG_KEEP_UP:-0}"
BACKUP_DEGRADATION_MIBS_THRESHOLD="${BACKUP_DEGRADATION_MIBS_THRESHOLD:-6}"
OPTIMIZE_SQLITE_BEFORE_BACKUP="${OPTIMIZE_SQLITE_BEFORE_BACKUP:-0}"
SQLITE_OPTIMIZE_TIMEOUT_SEC="${SQLITE_OPTIMIZE_TIMEOUT_SEC:-1800}"
OPTIMIZE_MARIADB_BEFORE_BACKUP="${OPTIMIZE_MARIADB_BEFORE_BACKUP:-0}"
MARIADB_SERVICE_NAME="${MARIADB_SERVICE_NAME:-mariadb}"
MARIADB_PURGE_BINLOGS="${MARIADB_PURGE_BINLOGS:-0}"
MARIADB_TRUNCATE_GENERAL_LOG="${MARIADB_TRUNCATE_GENERAL_LOG:-1}"
OPTIMIZE_REDIS_BEFORE_BACKUP="${OPTIMIZE_REDIS_BEFORE_BACKUP:-0}"
REDIS_SERVICE_NAME="${REDIS_SERVICE_NAME:-redis}"
REDIS_REWRITE_WAIT_SEC="${REDIS_REWRITE_WAIT_SEC:-180}"

###############################################################################
# WireGuard
###############################################################################
WG_BROUGHT_UP=0
SERVICES_STOPPED=0

# shellcheck disable=SC2329
wg_down_if_needed() {
  if [[ "${WG_BROUGHT_UP}" -eq 1 && "${WG_KEEP_UP:-0}" -ne 1 ]]; then
    log_json "INFO" "wg_down" "Останавливаем WireGuard ${WG_INTERFACE}..."
    if [[ -n "${SUDO_PASSWORD}" ]]; then
      if echo "${SUDO_PASSWORD}" | sudo -S wg-quick down "${WG_INTERFACE}" 2>/dev/null; then
        log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} остановлен"
      else
        log_json "WARN" "wg_down_fail" "Не удалось остановить WireGuard ${WG_INTERFACE}"
      fi
    else
      if wg-quick down "${WG_INTERFACE}" 2>/dev/null; then
        log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} остановлен"
      else
        log_json "WARN" "wg_down_fail" "Не удалось остановить WireGuard ${WG_INTERFACE}"
      fi
    fi
  fi
}

# shellcheck disable=SC2329
services_start_if_needed() {
  if [[ "${SERVICES_STOPPED}" -eq 1 ]]; then
    SERVICES_STOPPED=0
    log_json "INFO" "services_start" "Запускаем сервисы на ${REMOTE_HOST}..."
    export SSHPASS="${REMOTE_PASSWORD}"
    start_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
      "cd '${REMOTE_PATH}' && docker compose up -d" 2>&1)
    start_rc=$?
    unset SSHPASS
    if [[ "${start_rc}" -ne 0 ]]; then
      log_json "WARN" "services_start_failed" "Не удалось запустить сервисы" "${start_err}" "${start_rc}"
    else
      log_json "INFO" "services_start_ok" "Сервисы запущены" "" "${start_rc}"
    fi
  fi
}

# shellcheck disable=SC2329
cleanup() {
  services_start_if_needed
  wg_down_if_needed
  [[ -n "${PROGRESS_METRICS_FILE}" ]] && rm -f "${PROGRESS_METRICS_FILE}" 2>/dev/null
  cleanup_logs
}
trap cleanup EXIT INT TERM

log_json "INFO" "start" "Запуск резервного копирования"

if wg show "${WG_INTERFACE}" >/dev/null 2>&1; then
  log_json "INFO" "wg_status" "WireGuard ${WG_INTERFACE} уже активен"
else
  log_json "INFO" "wg_up" "Поднимаем WireGuard ${WG_INTERFACE}..."
  if [[ -n "${SUDO_PASSWORD}" ]]; then
    wg_err=$(echo "${SUDO_PASSWORD}" | sudo -S wg-quick up "${WG_INTERFACE}" 2>&1)
  else
    wg_err=$(wg-quick up "${WG_INTERFACE}" 2>&1)
  fi
  wg_rc=$?
  if [[ "${wg_rc}" -ne 0 ]]; then
    log_json "ERROR" "wg_up_failed" "Не удалось поднять WireGuard ${WG_INTERFACE}" "${wg_err}" "${wg_rc}"
    echo "Ошибка: wg-quick up ${WG_INTERFACE} завершился с кодом ${wg_rc}" >&2
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
  if ping -c 1 -W 5 "${REMOTE_HOST}" >/dev/null 2>&1; then
    _wg_ok=1
    break
  fi
  log_json "WARN" "wg_ping_retry" "Попытка ${_attempt}/3: хост ${REMOTE_HOST} не отвечает"
  [[ "${_attempt}" -lt 3 ]] && sleep 5
done
if [[ "${_wg_ok}" -eq 0 ]]; then
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

mkdir -p "${BACKUP_DIR}" 2>/dev/null
if [[ ! -d "${BACKUP_DIR}" ]]; then
  log_json "ERROR" "backup_dir_missing" "Папка назначения недоступна" "${BACKUP_DIR}"
  echo "Ошибка: папка ${BACKUP_DIR} недоступна" >&2
  exit 1
fi

###############################################################################
# BACKUP
###############################################################################
BACKUP_DATE="$(date '+%Y-%m-%d')"
REMOTE_PARENT="$(dirname "${REMOTE_PATH}")"
REMOTE_DIR="$(basename "${REMOTE_PATH}")"

# Встроенные исключения для явно восстановимых данных (кэши/preview/tmp).
# Эти пути исключаются всегда и не настраиваются через conf.
REMOTE_EXCLUDES=(
  "${REMOTE_DIR}/tmp/*"
  "${REMOTE_DIR}/cache/*"
  "${REMOTE_DIR}/.cache/*"
  "${REMOTE_DIR}/data/*/uploads/*"
  "${REMOTE_DIR}/data/*/files_trashbin/uploads/*"
  "${REMOTE_DIR}/data/*/cache/*"
  "${REMOTE_DIR}/data/appdata_*/preview/*"
  "${REMOTE_DIR}/data/appdata_*/thumbnails/*"
  "${REMOTE_DIR}/data/appdata_*/css/*"
  "${REMOTE_DIR}/data/appdata_*/js/*"
)
REMOTE_TAR_EXCLUDE_ARGS=""
for _ex in "${REMOTE_EXCLUDES[@]}"; do
  REMOTE_TAR_EXCLUDE_ARGS+=" --exclude='${_ex}'"
done

SSH_OPTS=(
  -p "${REMOTE_SSH_PORT}"
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=30
  -o BatchMode=no
  -o Compression=no
  -c "aes128-gcm@openssh.com,chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-ctr"
)

# Выбираем быстрейший доступный компрессор на удалённом сервере:
# zstd --fast=1 --threads=0 > pigz -1 > gzip -1
export SSHPASS="${REMOTE_PASSWORD}"
REMOTE_COMP=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "if command -v zstd >/dev/null 2>&1; then echo zstd; elif command -v pigz >/dev/null 2>&1; then echo pigz; else echo gzip; fi" 2>/dev/null)
unset SSHPASS
case "${REMOTE_COMP}" in
  zstd) COMP_CMD="zstd --fast=1 --threads=0 -c"; BACKUP_EXT="tar.zst" ;;
  pigz) COMP_CMD="pigz -1";                       BACKUP_EXT="tar.gz"  ;;
  *)    COMP_CMD="gzip -1";                        BACKUP_EXT="tar.gz"  ;;
esac

BACKUP_FILENAME="${SCRIPT_BASE}-${BACKUP_DATE}.${BACKUP_EXT}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

if [[ -f "${BACKUP_PATH}" ]]; then
  if validate_backup_file "${BACKUP_PATH}"; then
    BACKUP_APPEND=1
    log_json "INFO" "backup_append_mode" "Файл бэкапа за ${BACKUP_DATE} уже существует и прошёл проверку — дозапись" \
      "file=${BACKUP_PATH}"
  else
    validate_rc=$?
    BACKUP_APPEND=0
    validate_detail="file=${BACKUP_PATH}"
    if [[ -n "${VALIDATE_BACKUP_DETAIL}" ]]; then
      validate_detail="${validate_detail}, reason=${VALIDATE_BACKUP_DETAIL}"
    fi
    if [[ "${validate_rc}" -eq 1 ]]; then
      log_json "WARN" "backup_existing_invalid" "Существующий файл бэкапа повреждён — удаляем и создаём заново" \
        "${validate_detail}" "${validate_rc}"
      rm -f "${BACKUP_PATH}"
    else
      log_json "WARN" "backup_existing_unchecked" "Не удалось проверить существующий файл бэкапа — создаём заново без дозаписи" \
        "${validate_detail}" "${validate_rc}"
    fi
  fi
else
  BACKUP_APPEND=0
fi

log_json "INFO" "backup_start" "Начало резервного копирования" \
  "remote=${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH} -> ${BACKUP_PATH}, comp=${REMOTE_COMP:-gzip}, append=${BACKUP_APPEND}"
log_json "INFO" "backup_excludes" "Применены встроенные исключения tar" "count=${#REMOTE_EXCLUDES[@]}, list=${REMOTE_EXCLUDES[*]}"

###############################################################################
# NEXTCLOUD CLEANUP (occ)
###############################################################################
log_json "INFO" "occ_cleanup_start" "Очистка корзин пользователей (occ trashbin:cleanup)..."
export SSHPASS="${REMOTE_PASSWORD}"
occ_trash_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "docker exec -u www-data esimych-cloud-app php occ trashbin:cleanup --all-users" 2>&1)
occ_trash_rc=$?
unset SSHPASS
if [[ "${occ_trash_rc}" -ne 0 ]]; then
  log_json "WARN" "occ_cleanup_trash_failed" "Не удалось очистить корзины" "${occ_trash_err}" "${occ_trash_rc}"
else
  log_json "INFO" "occ_cleanup_trash_ok" "Корзины очищены" "${occ_trash_err}" "${occ_trash_rc}"
fi

# "occ trashbin:cleanup --all-users" при полной очистке физически удаляет
# саму папку data/<user>/files_trashbin (не только её содержимое), если она
# опустела. Из-за этого встроенный фоновый джоб Nextcloud ExpireTrash (он
# запускается через cron.php независимо от расписания этого скрипта) затем
# падает с "NotFoundException.../files_trashbin" при каждом своём запуске,
# пока папка не появится снова. Пересоздаём её для каждого пользователя сразу
# после очистки — mkdir -p идемпотентен и безопасен.
occ_trash_repair_detail=""
occ_trash_repair_rc=0
export SSHPASS="${REMOTE_PASSWORD}"
remote_datadir=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "docker exec -u www-data esimych-cloud-app php occ config:system:get datadirectory" 2>/dev/null | tr -d '\r\n')
remote_users=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "docker exec -u www-data esimych-cloud-app php occ user:list" 2>/dev/null)
unset SSHPASS
log_json "INFO" "occ_user_list" "Получен список пользователей Nextcloud (occ user:list)" "${remote_users:-<пусто>}"
if [[ -z "${remote_datadir}" || -z "${remote_users}" ]]; then
  occ_trash_repair_rc=1
  occ_trash_repair_detail="не удалось получить datadirectory или список пользователей"
else
  while IFS= read -r _uid; do
    [[ -z "${_uid}" ]] && continue
    export SSHPASS="${REMOTE_PASSWORD}"
    _repair_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
      "docker exec -u www-data esimych-cloud-app mkdir -p '${remote_datadir}/${_uid}/files_trashbin'" 2>&1)
    _repair_rc=$?
    unset SSHPASS
    if [[ "${_repair_rc}" -ne 0 ]]; then
      occ_trash_repair_rc=1
      occ_trash_repair_detail="${occ_trash_repair_detail}${_uid}: ${_repair_err}; "
      continue
    fi
    # Проверяем, что папка реально существует после mkdir -p (а не просто
    # команда молча ничего не сделала из-за проблем с docker exec/SSH) —
    # результат логируется отдельно для каждого пользователя.
    export SSHPASS="${REMOTE_PASSWORD}"
    sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
      "docker exec -u www-data esimych-cloud-app test -d '${remote_datadir}/${_uid}/files_trashbin'" >/dev/null 2>&1
    _verify_rc=$?
    unset SSHPASS
    if [[ "${_verify_rc}" -eq 0 ]]; then
      log_json "INFO" "occ_trashbin_verify_ok" "Папка files_trashbin подтверждена после пересоздания" "user=${_uid}, path=${remote_datadir}/${_uid}/files_trashbin" 0
    else
      occ_trash_repair_rc=1
      occ_trash_repair_detail="${occ_trash_repair_detail}${_uid}: папка не найдена после mkdir -p; "
      log_json "ERROR" "occ_trashbin_verify_failed" "Папка files_trashbin отсутствует после попытки пересоздания" "user=${_uid}, path=${remote_datadir}/${_uid}/files_trashbin" "${_verify_rc}"
    fi
  done < <(printf '%s\n' "${remote_users}" | sed -nE 's/^[[:space:]]*-[[:space:]]*([^:]+):.*/\1/p')
fi
if [[ "${occ_trash_repair_rc}" -ne 0 ]]; then
  log_json "WARN" "occ_trashbin_repair_failed" "Не удалось пересоздать/подтвердить папки files_trashbin" "${occ_trash_repair_detail}" "${occ_trash_repair_rc}"
else
  log_json "INFO" "occ_trashbin_repair_ok" "Папки files_trashbin пересозданы и проверены для всех пользователей" "" 0
fi

log_json "INFO" "occ_versions_start" "Очистка версий файлов (occ versions:cleanup)..."
export SSHPASS="${REMOTE_PASSWORD}"
occ_ver_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "docker exec -u www-data esimych-cloud-app php occ versions:cleanup" 2>&1)
occ_ver_rc=$?
unset SSHPASS
if [[ "${occ_ver_rc}" -ne 0 ]]; then
  log_json "WARN" "occ_cleanup_versions_failed" "Не удалось очистить версии файлов" "${occ_ver_err}" "${occ_ver_rc}"
else
  log_json "INFO" "occ_cleanup_versions_ok" "Версии файлов очищены" "${occ_ver_err}" "${occ_ver_rc}"
fi

###############################################################################
# DATABASE OPTIMIZATION (before services down)
###############################################################################
if [[ "${OPTIMIZE_MARIADB_BEFORE_BACKUP}" -eq 1 ]]; then
  log_json "INFO" "mariadb_optimize_start" "Оптимизация MariaDB перед архивированием"
  export SSHPASS="${REMOTE_PASSWORD}"
  mariadb_opt_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
    "cd '${REMOTE_PATH}' && docker compose exec -T \
      -e PURGE_BINLOGS='${MARIADB_PURGE_BINLOGS}' \
      -e TRUNCATE_GENERAL_LOG='${MARIADB_TRUNCATE_GENERAL_LOG}' \
      '${MARIADB_SERVICE_NAME}' sh -lc 'set -e; \
        mariadb-check -uroot -p\"\${MARIADB_ROOT_PASSWORD}\" --optimize --all-databases --skip-database=information_schema --skip-database=performance_schema --skip-database=mysql --skip-database=sys; \
        if [ \"\${PURGE_BINLOGS}\" = \"1\" ]; then \
          mariadb -uroot -p\"\${MARIADB_ROOT_PASSWORD}\" -e \"PURGE BINARY LOGS BEFORE NOW();\"; \
        fi; \
        if [ \"\${TRUNCATE_GENERAL_LOG}\" = \"1\" ]; then \
          : > /var/lib/mysql/general.log || true; \
        fi'" 2>&1)
  mariadb_opt_rc=$?
  unset SSHPASS
  if [[ "${mariadb_opt_rc}" -ne 0 ]]; then
    log_json "WARN" "mariadb_optimize_failed" "Оптимизация MariaDB завершилась с предупреждениями" "${mariadb_opt_err}" "${mariadb_opt_rc}"
  else
    log_json "INFO" "mariadb_optimize_ok" "Оптимизация MariaDB завершена" "${mariadb_opt_err}" "${mariadb_opt_rc}"
  fi
else
  log_json "INFO" "mariadb_optimize_skip" "Оптимизация MariaDB отключена (OPTIMIZE_MARIADB_BEFORE_BACKUP=0)"
fi

if [[ "${OPTIMIZE_REDIS_BEFORE_BACKUP}" -eq 1 ]]; then
  log_json "INFO" "redis_optimize_start" "Оптимизация Redis AOF перед архивированием"
  export SSHPASS="${REMOTE_PASSWORD}"
  redis_opt_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
    "cd '${REMOTE_PATH}' && docker compose exec -T \
      -e REDIS_WAIT='${REDIS_REWRITE_WAIT_SEC}' \
      '${REDIS_SERVICE_NAME}' sh -lc 'set -e; \
        redis-cli BGREWRITEAOF >/dev/null; \
        i=0; \
        while [ \${i} -lt \${REDIS_WAIT} ]; do \
          in_progress=\$(redis-cli INFO persistence | tr -d '\\r' | sed -n \"s/^aof_rewrite_in_progress:\\([0-9]\\+\\)$/\\1/p\"); \
          [ \"\${in_progress}\" = \"0\" ] && exit 0; \
          i=\$((i + 1)); \
          sleep 1; \
        done; \
        echo \"AOF rewrite did not finish within \${REDIS_WAIT} seconds\"; \
        exit 1'" 2>&1)
  redis_opt_rc=$?
  unset SSHPASS
  if [[ "${redis_opt_rc}" -ne 0 ]]; then
    log_json "WARN" "redis_optimize_failed" "Оптимизация Redis завершилась с предупреждениями" "${redis_opt_err}" "${redis_opt_rc}"
  else
    log_json "INFO" "redis_optimize_ok" "Оптимизация Redis завершена" "${redis_opt_err}" "${redis_opt_rc}"
  fi
else
  log_json "INFO" "redis_optimize_skip" "Оптимизация Redis отключена (OPTIMIZE_REDIS_BEFORE_BACKUP=0)"
fi

log_json "INFO" "services_stop" "Останавливаем сервисы на ${REMOTE_HOST}..."
export SSHPASS="${REMOTE_PASSWORD}"
stop_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
  "cd '${REMOTE_PATH}' && docker compose down" 2>&1)
stop_rc=$?
unset SSHPASS
if [[ "${stop_rc}" -ne 0 ]]; then
  log_json "WARN" "services_stop_failed" "Не удалось остановить сервисы" "${stop_err}" "${stop_rc}"
  echo "Предупреждение: не удалось остановить сервисы (код ${stop_rc}): ${stop_err}" >&2
else
  SERVICES_STOPPED=1
  log_json "INFO" "services_stop_ok" "Сервисы остановлены" "" "${stop_rc}"
fi

###############################################################################
# SQLITE OPTIMIZATION (optional)
###############################################################################
if [[ "${OPTIMIZE_SQLITE_BEFORE_BACKUP}" -eq 1 ]]; then
  log_json "INFO" "sqlite_optimize_start" "Оптимизация SQLite-баз перед архивированием"
  export SSHPASS="${REMOTE_PASSWORD}"
  sqlite_opt_err=$(sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
    "set -o pipefail; find '${REMOTE_PATH}' -type f -name '*.db' -print0 \
      | xargs -0 -r -I{} timeout ${SQLITE_OPTIMIZE_TIMEOUT_SEC}s sqlite3 \"{}\" 'PRAGMA optimize; VACUUM;'" 2>&1)
  sqlite_opt_rc=$?
  unset SSHPASS
  if [[ "${sqlite_opt_rc}" -ne 0 ]]; then
    log_json "WARN" "sqlite_optimize_failed" "Оптимизация SQLite завершилась с предупреждениями" "${sqlite_opt_err}" "${sqlite_opt_rc}"
  else
    log_json "INFO" "sqlite_optimize_ok" "Оптимизация SQLite завершена" "${sqlite_opt_err}" "${sqlite_opt_rc}"
  fi
else
  log_json "INFO" "sqlite_optimize_skip" "Оптимизация SQLite отключена (OPTIMIZE_SQLITE_BEFORE_BACKUP=0)"
fi

export SSHPASS="${REMOTE_PASSWORD}"
PROGRESS_METRICS_FILE="$(mktemp)"

# Фоновый монитор: пишет размер файла в лог каждые 60 сек
_progress_monitor() {
  while sleep 60; do
    [[ -f "${BACKUP_PATH}" ]] || break
    local sz size_bytes now_epoch
    sz=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
    size_bytes=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || printf '0')
    now_epoch=$(date +%s)
    printf '%s\t%s\n' "${now_epoch}" "${size_bytes}" >> "${PROGRESS_METRICS_FILE}"
    [[ -n "${sz}" ]] && log_json "INFO" "backup_progress" "Прогресс архивирования" "size=${sz}, bytes=${size_bytes}"
  done
}
_progress_monitor >/dev/null 2>&1 &
_PROGRESS_PID=$!

ARCHIVE_START_EPOCH=$(date +%s)
err_tmp=$(mktemp)
REMOTE_TAR_CMD="set -o pipefail; tar --create --file=- --sparse${REMOTE_TAR_EXCLUDE_ARGS} --directory='${REMOTE_PARENT}' '${REMOTE_DIR}' | ${COMP_CMD}"
if [[ "${BACKUP_APPEND}" -eq 1 ]]; then
  sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
    "${REMOTE_TAR_CMD}" \
    >> "${BACKUP_PATH}" 2>"${err_tmp}"
else
  sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" \
    "${REMOTE_TAR_CMD}" \
    > "${BACKUP_PATH}" 2>"${err_tmp}"
fi
backup_rc=$?
err_out=$(cat "${err_tmp}"); rm -f "${err_tmp}"
unset SSHPASS
ARCHIVE_END_EPOCH=$(date +%s)

kill "${_PROGRESS_PID}" 2>/dev/null
wait "${_PROGRESS_PID}" 2>/dev/null

if [[ "${backup_rc}" -eq 0 ]]; then
  archive_duration=$((ARCHIVE_END_EPOCH - ARCHIVE_START_EPOCH))
  [[ "${archive_duration}" -lt 1 ]] && archive_duration=1

  backup_bytes=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || printf '0')
  backup_size=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
  avg_mib_s=$(awk -v b="${backup_bytes}" -v d="${archive_duration}" 'BEGIN { printf "%.2f", (b/1048576)/d }')

  progress_samples=$(wc -l < "${PROGRESS_METRICS_FILE}" 2>/dev/null || printf '0')
  window_mib_s="n/a"
  if [[ "${progress_samples}" -ge 2 ]]; then
    first_sample=$(head -n1 "${PROGRESS_METRICS_FILE}")
    last_sample=$(tail -n1 "${PROGRESS_METRICS_FILE}")
    first_ts=$(printf '%s' "${first_sample}" | awk -F '\t' '{print $1}')
    first_bytes=$(printf '%s' "${first_sample}" | awk -F '\t' '{print $2}')
    last_ts=$(printf '%s' "${last_sample}" | awk -F '\t' '{print $1}')
    last_bytes=$(printf '%s' "${last_sample}" | awk -F '\t' '{print $2}')
    window_dt=$((last_ts - first_ts))
    if [[ "${window_dt}" -gt 0 ]]; then
      window_mib_s=$(awk -v b1="${first_bytes}" -v b2="${last_bytes}" -v d="${window_dt}" 'BEGIN { printf "%.2f", ((b2-b1)/1048576)/d }')
    fi
  fi

  metrics_detail="file=${BACKUP_PATH}, size=${backup_size}, bytes=${backup_bytes}, duration_s=${archive_duration}, avg_mib_s=${avg_mib_s}, window_mib_s=${window_mib_s}, progress_samples=${progress_samples}, comp=${REMOTE_COMP:-gzip}, append=${BACKUP_APPEND}"
  log_json "INFO" "backup_metrics" "Метрики этапа архивирования" "${metrics_detail}" 0

  if awk -v s="${avg_mib_s}" -v t="${BACKUP_DEGRADATION_MIBS_THRESHOLD}" 'BEGIN { exit !(s < t) }'; then
    probable_cause="network_or_remote_io"
    if [[ "${REMOTE_COMP:-gzip}" == "zstd" ]]; then
      probable_cause="network_or_remote_io_or_zstd_cpu"
    fi
    if [[ "${progress_samples}" -lt 2 ]]; then
      probable_cause="insufficient_progress_samples"
    fi
    log_json "WARN" "backup_degradation" "Обнаружена деградация скорости бэкапа" \
      "threshold_mib_s=${BACKUP_DEGRADATION_MIBS_THRESHOLD}, probable_cause=${probable_cause}, ${metrics_detail}" 0
  fi

  backup_size=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
  log_json "INFO" "backup_done" "Резервная копия создана успешно" \
    "file=${BACKUP_PATH}, size=${backup_size}" "${backup_rc}"
  echo "Готово: ${BACKUP_PATH} (${backup_size})"

  # Очистка старых бэкапов, оставляем только 5 последних
  old_backups=()
  _old_bak_file="$(mktemp)"
  if find "${BACKUP_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.tar.*" -printf '%T@|%p\n' 2>/dev/null \
      | sort -nr \
      | awk -F'|' 'NR > 5 { print $2 }' > "${_old_bak_file}"; then
    while IFS= read -r _old_bak; do
      [[ -n "${_old_bak}" ]] && old_backups+=("${_old_bak}")
    done < "${_old_bak_file}"
  fi
  rm -f -- "${_old_bak_file}"
  if ((${#old_backups[@]} > 0)); then
    log_json "INFO" "backup_cleanup" "Удаление старых резервных копий (оставляем 5)"
    rm -f -- "${old_backups[@]}"
  fi
else
  log_json "ERROR" "backup_failed" "Ошибка при создании резервной копии" "${err_out}" "${backup_rc}"
  echo "Ошибка резервного копирования (код ${backup_rc}): ${err_out}" >&2
  rm -f "${BACKUP_PATH}" 2>/dev/null
  exit "${backup_rc}"
fi

rm -f "${PROGRESS_METRICS_FILE}" 2>/dev/null

exit 0
