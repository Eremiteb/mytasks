#!/usr/bin/env bash
# Проверка каталогов с архивами резервных копий: наличие, свежесть, размер и
# (опционально) целостность последнего архива. Результат пишется в JSONL-лог,
# при любой ошибке дополнительно показывается уведомление в трее (notify-send).
#
# Коды возврата: 0 — все каталоги в порядке, 1 — найдены проблемы с бэкапами,
# 2 — ошибка конфигурации/зависимостей/аргументов.
set -uo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)
CONF_FILE="${SCRIPT_DIR}/conf/${SCRIPT_BASE}.conf"
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"

STATE_DIR="${SCRIPT_DIR}/state"
VERIFIED_FILE="${STATE_DIR}/${SCRIPT_BASE}.verified"

mkdir -p "${SCRIPT_DIR}/conf" "${LOG_DIR}"
DRY_RUN=0

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

# Значения по умолчанию; переопределяются в conf/check_backups.conf
BACKUP_DIRS=()
ARCHIVE_PATTERNS=()
MAX_AGE_DAYS=2
MIN_SIZE_BYTES=1
MIN_SIZE_RATIO_PCT=50
SEARCH_DEPTH=1
ACCESS_TIMEOUT_SECS=15
VERIFY_INTEGRITY="yes"
VERIFY_MIN_SPEED_MBPS=20
VERIFY_TIMEOUT_BASE_SECS=120
VERIFY_MIN_AGE_MINUTES=30
KEEP_LOGS=10
APP_NAME="BackupCheck"
ICON_NAME="drive-harddisk"
URGENCY="critical"

if [[ -r "${CONF_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${CONF_FILE}"
fi

if [[ ${#ARCHIVE_PATTERNS[@]} -eq 0 ]]; then
    ARCHIVE_PATTERNS=("*.tar.zst" "*.tar.gz" "*.tgz" "*.tar" "*.zip" "*.7z")
fi

NOTIFY_ENABLED="yes"
ARCHIVES=()
PROBLEMS=()
SUMMARY=()

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
    local msg_esc detail_esc level_norm timestamp
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    timestamp="$(ts)"

    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
        "${timestamp}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" "${rc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    local old_logs=()
    local old_log old_logs_file

    old_logs_file="$(mktemp)"
    if find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
        | sort -nr \
        | awk -F'|' -v keep="${KEEP_LOGS}" 'NR > keep { print $2 }' > "${old_logs_file}"; then
        while IFS= read -r old_log; do
            [[ -n "${old_log}" ]] && old_logs+=("${old_log}")
        done < "${old_logs_file}"
    fi
    rm -f -- "${old_logs_file}"

    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

# Уведомление в трее. Из systemd user-сервиса и cron переменные сессии могут
# отсутствовать, поэтому при необходимости указываем шину D-Bus пользователя.
notify_alert() {
    local title="$1"
    local body="$2"
    local uid runtime_dir

    [[ "${NOTIFY_ENABLED}" == "yes" ]] || return 0
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "[dry-run] будет: уведомление «${title}»"
        return 0
    fi
    if ! command -v notify-send >/dev/null 2>&1; then
        log_json "WARN" "notify_unavailable" "notify-send не найден, уведомление не показано" "${title}"
        return 0
    fi

    if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
        uid="$(id -u)"
        runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
        if [[ -S "${runtime_dir}/bus" ]]; then
            export DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_dir}/bus"
        fi
    fi

    if ! notify-send -a "${APP_NAME}" -i "${ICON_NAME}" -u "${URGENCY}" "${title}" "${body}"; then
        log_json "WARN" "notify_failed" "Не удалось показать уведомление" "${title}"
    fi
}

# Завершение с ошибкой конфигурации/зависимостей: лог + уведомление + код 2
fatal() {
    local event="$1"
    local msg="$2"
    local detail="${3:-}"

    echo "Ошибка: ${msg}${detail:+ (${detail})}"
    log_json "ERROR" "${event}" "${msg}" "${detail}" 2
    notify_alert "Проверка бэкапов: ошибка" "${msg}${detail:+ (${detail})}"
    cleanup_logs
    exit 2
}

require_cmd() {
    local cmd="$1"
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        fatal "dependency_missing" "Не найдена зависимость" "${cmd}"
    fi
}

add_problem() {
    local dir="$1"
    local event="$2"
    local msg="$3"
    local detail="${4:-}"

    PROBLEMS+=("${dir}: ${msg}")
    log_json "ERROR" "${event}" "${msg}" "${dir}${detail:+ | ${detail}}"
}

human_size() {
    local bytes="$1"
    awk -v b="${bytes}" 'BEGIN {
        split("Б КиБ МиБ ГиБ ТиБ", u, " ")
        i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf "%.1f %s", b, u[i]
    }'
}

# Заполняет массив ARCHIVES строками «mtime|размер|путь», новые первыми
list_archives() {
    local dir="$1"
    local find_args=()
    local pattern tmp_file line

    ARCHIVES=()

    for pattern in "${ARCHIVE_PATTERNS[@]}"; do
        if [[ ${#find_args[@]} -gt 0 ]]; then
            find_args+=(-o)
        fi
        find_args+=(-name "${pattern}")
    done

    tmp_file="$(mktemp)"
    find "${dir}" -maxdepth "${SEARCH_DEPTH}" -type f \( "${find_args[@]}" \) \
        -printf '%T@|%s|%p\n' 2>/dev/null > "${tmp_file}"
    sort -t'|' -k1,1nr -o "${tmp_file}" "${tmp_file}"
    while IFS= read -r line; do
        [[ -n "${line}" ]] && ARCHIVES+=("${line}")
    done < "${tmp_file}"
    rm -f -- "${tmp_file}"
}

# Тайм-аут проверки зависит от размера: архив читается целиком, поэтому на
# VERIFY_MIN_SPEED_MBPS МиБ/с (нижняя оценка скорости NFS/диска) плюс запас.
verify_timeout_for() {
    local size="$1"
    echo $(( size / (VERIFY_MIN_SPEED_MBPS * 1048576) + VERIFY_TIMEOUT_BASE_SECS ))
}

# Проверка целостности архива штатной утилитой формата.
# Возврат: 0 — цел, 1 — повреждён, 2 — проверка невозможна/не завершилась.
verify_archive() {
    local file="$1"
    local size="$2"
    local rc=0
    local tool=()
    local prefix=()
    local limit

    case "${file}" in
        *.tar.zst|*.zst) tool=(zstd -t -q) ;;
        *.tar.gz|*.tgz|*.gz) tool=(gzip -t) ;;
        *.zip) tool=(unzip -tq) ;;
        *.7z) tool=(7z t -bd) ;;
        *.tar) tool=(tar -tf) ;;
        *) return 2 ;;
    esac

    if ! command -v "${tool[0]}" >/dev/null 2>&1; then
        log_json "WARN" "verify_skipped" "Нет утилиты для проверки целостности" "${tool[0]}"
        return 2
    fi

    # Чтение десятков ГБ не должно мешать работе пользователя после загрузки
    if command -v nice >/dev/null 2>&1; then
        prefix+=(nice -n 19)
    fi
    if command -v ionice >/dev/null 2>&1; then
        prefix+=(ionice -c3)
    fi

    limit="$(verify_timeout_for "${size}")"
    timeout "${limit}" "${prefix[@]}" "${tool[@]}" "${file}" >/dev/null 2>&1
    rc=$?
    case "${rc}" in
        0) return 0 ;;
        124)
            log_json "WARN" "verify_timeout" "Проверка целостности не уложилась в тайм-аут" "${file} | ${limit} с"
            return 2
            ;;
        *) return 1 ;;
    esac
}

# Уже проверенный и неизменившийся архив (путь|размер|mtime) повторно не читаем
is_verified() {
    [[ -r "${VERIFIED_FILE}" ]] && grep -qxF -- "$1" "${VERIFIED_FILE}"
}

mark_verified() {
    local tmp_file
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "[dry-run] будет: архив отмечен как проверенный в ${VERIFIED_FILE}"
        return 0
    fi
    mkdir -p "${STATE_DIR}"
    tmp_file="$(mktemp)"
    { [[ -r "${VERIFIED_FILE}" ]] && cat -- "${VERIFIED_FILE}"; echo "$1"; } | tail -n 50 > "${tmp_file}"
    mv -- "${tmp_file}" "${VERIFIED_FILE}"
}

# Проверяет один каталог. spec: «каталог» или «каталог|лимит_возраста_в_днях»
check_dir() {
    local spec="$1"
    local dir="${spec%%|*}"
    local max_age_days="${MAX_AGE_DAYS}"
    local mtime size path prev_size
    local count age_hours limit_hours ratio name verify_rc size_h verify_key verify_start verify_end now age_secs

    if [[ "${spec}" == *"|"* ]]; then
        max_age_days="${spec#*|}"
    fi
    if [[ -z "${dir}" || ! "${max_age_days}" =~ ^[0-9]+$ ]]; then
        add_problem "${spec}" "config_invalid" "Неверная запись в BACKUP_DIRS" "${spec}"
        return 0
    fi

    # Обращение к каталогу запускает автомонтирование (NFS x-systemd.automount);
    # timeout защищает от зависания недоступного сетевого ресурса.
    if ! timeout "${ACCESS_TIMEOUT_SECS}" test -d "${dir}" >/dev/null 2>&1; then
        add_problem "${dir}" "dir_missing" "Каталог недоступен или не существует" ""
        return 0
    fi
    if ! timeout "${ACCESS_TIMEOUT_SECS}" test -r "${dir}" >/dev/null 2>&1; then
        add_problem "${dir}" "dir_unreadable" "Нет прав на чтение каталога" ""
        return 0
    fi

    list_archives "${dir}"

    count=${#ARCHIVES[@]}
    if [[ "${count}" -eq 0 ]]; then
        add_problem "${dir}" "no_archives" "Архивы не найдены" "маски: ${ARCHIVE_PATTERNS[*]}"
        return 0
    fi

    IFS='|' read -r mtime size path <<< "${ARCHIVES[0]}"
    mtime="${mtime%.*}"
    name="$(basename -- "${path}")"
    size_h="$(human_size "${size}")"
    now="$(date +%s)"
    age_secs=$(( now - mtime ))
    age_hours=$(( age_secs / 3600 ))
    limit_hours=$(( max_age_days * 24 ))

    if [[ "${age_hours}" -gt "${limit_hours}" ]]; then
        add_problem "${dir}" "archive_stale" "Последний архив устарел (${age_hours} ч при лимите ${limit_hours} ч)" "${path}"
        return 0
    fi

    if [[ "${size}" -lt "${MIN_SIZE_BYTES}" ]]; then
        add_problem "${dir}" "archive_too_small" "Последний архив пуст или слишком мал (${size_h})" "${path}"
        return 0
    fi

    # Резкое уменьшение относительно предыдущего архива — признак обрыва записи
    if [[ "${MIN_SIZE_RATIO_PCT}" -gt 0 && "${count}" -ge 2 ]]; then
        IFS='|' read -r _ prev_size _ <<< "${ARCHIVES[1]}"
        if [[ "${prev_size}" -gt 0 ]]; then
            ratio=$(( size * 100 / prev_size ))
            if [[ "${ratio}" -lt "${MIN_SIZE_RATIO_PCT}" ]]; then
                add_problem "${dir}" "archive_shrunk" "Последний архив составляет ${ratio}% от предыдущего (порог ${MIN_SIZE_RATIO_PCT}%)" "${path}"
                return 0
            fi
        fi
    fi

    if [[ "${VERIFY_INTEGRITY}" == "yes" ]]; then
        verify_key="${path}|${size}|${mtime}"
        if [[ "${age_secs}" -lt $(( VERIFY_MIN_AGE_MINUTES * 60 )) ]]; then
            log_json "WARN" "verify_deferred" "Архив изменён менее ${VERIFY_MIN_AGE_MINUTES} мин назад, возможно ещё пишется: проверка целостности пропущена" "${path}"
        elif is_verified "${verify_key}"; then
            log_json "INFO" "verify_cached" "Архив уже проверен ранее, повторное чтение не нужно" "${path}"
        else
            verify_start="$(date +%s)"
            verify_archive "${path}" "${size}"
            verify_rc=$?
            if [[ "${verify_rc}" -eq 1 ]]; then
                add_problem "${dir}" "archive_corrupt" "Последний архив повреждён" "${path}"
                return 0
            elif [[ "${verify_rc}" -eq 0 ]]; then
                mark_verified "${verify_key}"
                verify_end="$(date +%s)"
                log_json "INFO" "verify_ok" "Целостность архива подтверждена" "${path} | $(( verify_end - verify_start )) с"
            fi
        fi
    fi

    SUMMARY+=("${dir}: ${name}, ${size_h}, ${age_hours} ч назад, архивов: ${count}")
    log_json "INFO" "dir_ok" "Каталог с бэкапами в порядке" "${dir} | ${name} | ${size_h} | ${age_hours} ч | архивов: ${count}"
}

usage() {
    cat <<EOF
Использование: ${SCRIPT_NAME} [--no-notify] [-h|--help]

Проверяет каталоги из BACKUP_DIRS (conf/${SCRIPT_BASE}.conf): наличие архивов,
свежесть, размер и при VERIFY_INTEGRITY=yes целостность последнего архива.
Пишет JSONL-лог в logs/, при ошибках показывает уведомление в трее.

  --no-notify   не показывать уведомление (только лог и код возврата)
  -n, --dry-run не менять кэш проверенных архивов и не показывать уведомления
  -h, --help    эта справка

Коды возврата: 0 — всё в порядке, 1 — проблемы с бэкапами, 2 — ошибка запуска.
EOF
}

###############################################################################
# ARGS
###############################################################################
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-notify) NOTIFY_ENABLED="no" ;;
        -n|--dry-run) DRY_RUN=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

###############################################################################
# MAIN
###############################################################################
require_cmd find
require_cmd sort
require_cmd timeout

if [[ ${#BACKUP_DIRS[@]} -eq 0 ]]; then
    fatal "config_missing" "Не задан BACKUP_DIRS" "${CONF_FILE}"
fi

log_json "INFO" "start" "Проверка каталогов с бэкапами" "каталогов: ${#BACKUP_DIRS[@]}"

for spec in "${BACKUP_DIRS[@]}"; do
    check_dir "${spec}"
done

if [[ ${#PROBLEMS[@]} -gt 0 ]]; then
    problems_str="$(printf '%s\n' "${PROBLEMS[@]}")"
    echo "Ошибка: найдены проблемы с бэкапами:"
    echo "${problems_str}"
    log_json "ERROR" "done" "Найдены проблемы с бэкапами" "проблем: ${#PROBLEMS[@]}" 1
    notify_alert "Проблемы с резервными копиями" "${problems_str}"
    cleanup_logs
    exit 1
fi

printf 'Бэкапы в порядке:\n%s\n' "$(printf '%s\n' "${SUMMARY[@]}")"
log_json "INFO" "done" "Все каталоги с бэкапами в порядке" "каталогов: ${#BACKUP_DIRS[@]}" 0
cleanup_logs
exit 0
