#!/usr/bin/env bash
# Диагностика системы: жёсткие диски (SMART), CPU и GPU. Опрос, запись
# состояния в SQLite, HTML-отчёт (таблицы + диаграммы + аналитическая
# справка по каждому диску).
set -uo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
# SYSTEM_MONITOR_EUID — только для тестов (подмена EUID).
RUN_EUID="${SYSTEM_MONITOR_EUID:-${EUID}}"

# Конфиг. По расписанию работает root-копия скрипта (check_mytasks.sh ставит
# её root:root в /usr/local/lib/mytasks, рядом нет conf/): она читает только
# root-конфиг (по умолчанию /etc/mytasks/system_monitor.conf, юнит передаёт
# его через --config) и никогда не выполняет файлы пользователя. Запуск из
# каталога mytasks: root-конфиг, если он есть, иначе conf/system_monitor.conf.
ROOT_CONF_FILE="/etc/mytasks/${SCRIPT_BASE}.conf"
# Чьим конфигам доверять под root (владелец root, без записи для группы и остальных)
TRUSTED_UID=0
# Подмена root-конфига — только для тестов и только не под настоящим root
if [[ -n "${SYSTEM_MONITOR_TEST_ROOT_CONF:-}" && "${EUID}" -ne 0 ]]; then
    ROOT_CONF_FILE="${SYSTEM_MONITOR_TEST_ROOT_CONF}"
    TRUSTED_UID="${EUID}"
fi
USER_CONF_FILE="${SCRIPT_DIR}/conf/${SCRIPT_BASE}.conf"
INSTALLED_COPY=0
[[ -d "${SCRIPT_DIR}/conf" ]] || INSTALLED_COPY=1
# --config ФАЙЛ разбирается до загрузки конфига (остальные ключи — в ARGS)
CONF_ARG=""
prev_arg=""
for arg in "$@"; do
    [[ "${prev_arg}" == "-c" || "${prev_arg}" == "--config" ]] && CONF_ARG="${arg}"
    prev_arg="${arg}"
done
if [[ -n "${CONF_ARG}" ]]; then
    CONF_FILE="${CONF_ARG}"
elif ((INSTALLED_COPY)) || [[ -e "${ROOT_CONF_FILE}" || -L "${ROOT_CONF_FILE}" ]]; then
    CONF_FILE="${ROOT_CONF_FILE}"
else
    CONF_FILE="${USER_CONF_FILE}"
fi

# Владелец uid и права файла: обычный файл, не симлинк, владелец TRUSTED_UID,
# без записи для группы и остальных
trusted_root_file() {
    local st
    [[ -f "$1" && ! -L "$1" ]] || return 1
    st="$(stat -c '%u %a' -- "$1" 2>/dev/null)" || return 1
    [[ "${st%% *}" == "${TRUSTED_UID}" ]] || return 1
    (( (8#${st##* } & 8#022) == 0 ))
}

if ((INSTALLED_COPY)) || [[ "${CONF_FILE}" != "${USER_CONF_FILE}" && "${RUN_EUID}" -eq 0 ]]; then
    # Root-копия и чужой конфиг под root: только доверенный файл, иначе отказ
    if ! trusted_root_file "${CONF_FILE}"; then
        echo "Ошибка: конфиг ${CONF_FILE} отсутствует или не доверенный (нужен обычный файл root:root без записи для группы и остальных); установите его: check_mytasks.sh" >&2
        exit 2
    fi
elif ((RUN_EUID == 0)); then
    # Осознанный ручной запуск от root из каталога пользователя (sudo ./system_monitor.sh)
    script_owner="$(stat -c '%u' -- "${SCRIPT_DIR}/${SCRIPT_NAME}" 2>/dev/null)" || script_owner=""
    conf_owner="0"
    [[ -e "${CONF_FILE}" ]] && { conf_owner="$(stat -c '%u' -- "${CONF_FILE}" 2>/dev/null)" || conf_owner=""; }
    if [[ "${script_owner}" != "0" || "${conf_owner}" != "0" ]]; then
        echo "ВНИМАНИЕ: от root выполняются скрипт/конфиг пользователя (${SCRIPT_DIR}); по расписанию работает root-копия из /usr/local/lib/mytasks — ручной запуск от root только осознанно" >&2
    fi
fi

# Значения из окружения не наследуются: только конфиг или умолчания ниже
MYTASKS_DIR=""
FILE_OWNER=""
if [[ -r "${CONF_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${CONF_FILE}"
fi

# Каталог данных mytasks: у root-копии — только из конфига
if [[ -z "${MYTASKS_DIR}" ]]; then
    if ((INSTALLED_COPY)); then
        echo "Ошибка: в ${CONF_FILE} не задан MYTASKS_DIR (каталог данных mytasks)" >&2
        exit 2
    fi
    MYTASKS_DIR="${SCRIPT_DIR}"
fi
LOG_DIR="${MYTASKS_LOG_DIR:-${MYTASKS_DIR}/logs}"
STATE_DIR="${MYTASKS_DIR}/state"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${MYTASKS_DIR}/conf/log_template.conf"

# Шаблон логов — файл пользователя: под root не выполняется (значения по умолчанию)
if ((RUN_EUID != 0)) && [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

# Владелец создаваемых файлов (скрипт работает от root, см. fix_owner); пустое
# значение в конфиге = владелец и группа каталога mytasks
if [[ -z "${FILE_OWNER}" ]]; then
    FILE_OWNER="$(stat -c '%U:%G' -- "${MYTASKS_DIR}" 2>/dev/null)" || FILE_OWNER=""
fi
DB_FILE="${DB_FILE:-${STATE_DIR}/${SCRIPT_BASE}.db}"
REPORT_DIR="${REPORT_DIR:-${MYTASKS_DIR}/system_reports}"
REPORT_KEEP="${REPORT_KEEP:-5}"
KEEP_LOGS="${KEEP_LOGS:-10}"
HISTORY_POINTS="${HISTORY_POINTS:-30}"
SMARTCTL_BIN="${SMARTCTL_BIN:-smartctl}"
SENSORS_BIN="${SENSORS_BIN:-sensors}"
NVIDIA_SMI_BIN="${NVIDIA_SMI_BIN:-nvidia-smi}"
SMARTCTL_OPTS="${SMARTCTL_OPTS:--a -j -d auto}"
CPU_SAMPLE_SECS="${CPU_SAMPLE_SECS:-1}"
DISKS="${DISKS:-}"
TEMP_WARN_C="${TEMP_WARN_C:-50}"
TEMP_CRIT_C="${TEMP_CRIT_C:-60}"
# Отдельные пороги для NVMe (/dev/nvme*): им штатно теплее, чем SATA; пусто — общие TEMP_*
NVME_TEMP_WARN_C="${NVME_TEMP_WARN_C:-}"
NVME_TEMP_CRIT_C="${NVME_TEMP_CRIT_C:-}"
CPU_TEMP_WARN_C="${CPU_TEMP_WARN_C:-80}"
CPU_TEMP_CRIT_C="${CPU_TEMP_CRIT_C:-90}"
GPU_TEMP_WARN_C="${GPU_TEMP_WARN_C:-75}"
GPU_TEMP_CRIT_C="${GPU_TEMP_CRIT_C:-85}"
APP_NAME="${APP_NAME:-SystemMonitor}"
ICON_NAME="${ICON_NAME:-utilities-system-monitor}"
URGENCY="${URGENCY:-critical}"

# Есть ли в пути симлинк (сравнение пути с разыменованием и без него)
path_has_symlink() {
    local resolved literal
    resolved="$(realpath -m -- "$1")" || return 0
    literal="$(realpath -m -s -- "$1")" || return 0
    [[ "${resolved}" != "${literal}" ]]
}

# Под root каталоги данных принадлежат пользователю: запись — без следования
# симлинкам (иначе подменённая ссылка увела бы запись root в чужой файл)
if ((RUN_EUID == 0)); then
    db_dir="$(dirname -- "${DB_FILE}")"
    for root_path in "${db_dir}" "${LOG_DIR}" "${REPORT_DIR}"; do
        if path_has_symlink "${root_path}"; then
            echo "Ошибка: путь ${root_path} содержит симлинк — запись от root отклонена" >&2
            exit 2
        fi
    done
    for root_path in "${DB_FILE}" "${DB_FILE}-wal" "${DB_FILE}-shm" "${DB_FILE}-journal"; do
        if [[ -L "${root_path}" ]]; then
            echo "Ошибка: ${root_path} — симлинк, база от root не открывается" >&2
            exit 2
        fi
    done
fi

mkdir -p "${LOG_DIR}"
# Лог под root создаётся эксклюзивно (noclobber: O_EXCL, симлинк не годится);
# уже существующий обычный файл (повторный запуск в ту же секунду) допустим
if ((RUN_EUID == 0)) && ! (set -C; : > "${LOG_FILE}") 2>/dev/null; then
    if [[ -L "${LOG_FILE}" || ! -f "${LOG_FILE}" ]]; then
        echo "Ошибка: не удалось безопасно создать лог ${LOG_FILE}" >&2
        exit 2
    fi
fi

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
}

html_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    s="${s//\"/&quot;}"
    printf '%s' "${s}"
}

sql_escape() {
    printf '%s' "$1" | sed "s/'/''/g"
}

sql_num() {
    if [[ -z "$1" || ! "$1" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
        printf 'NULL'
    else
        printf '%s' "$1"
    fi
}

log_json() {
    local level="$1"
    local event="$2"
    local msg="$3"
    local detail="${4:-}"
    local msg_esc detail_esc level_norm timestamp
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    timestamp="$(ts)"

    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
        "${timestamp}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

# Различается ли владелец пути с FILE_OWNER. Без разыменования симлинков (stat без -L).
# Если FILE_OWNER без группы — сравнивается только пользователь.
owner_differs() {
    local current
    current="$(stat -c '%U:%G' -- "$1" 2>/dev/null)" || return 1
    [[ "${FILE_OWNER}" == *:* ]] || current="${current%%:*}"
    [[ "${current}" != "${FILE_OWNER}" ]]
}

# Передаёт перечисленные файлы во владение FILE_OWNER. Не от root — ничего не делает
# (chown обычному пользователю не нужен и не разрешён). Каталоги не трогаются.
# chown -h: симлинк не разыменовывается, меняется владелец самой ссылки, а не цели
# (иначе ссылка могла бы увести chown на чужой файл). Ошибка chown — только WARN.
fix_owner() {
    local path
    ((DRY_RUN == 0)) || return 0
    ((RUN_EUID == 0)) || return 0
    [[ -n "${FILE_OWNER}" ]] || return 0
    for path in "$@"; do
        [[ -e "${path}" || -L "${path}" ]] || continue
        [[ -d "${path}" && ! -L "${path}" ]] && continue
        owner_differs "${path}" || continue
        if ! chown -h -- "${FILE_OWNER}" "${path}"; then
            log_json "WARN" "chown_failed" "Не удалось сменить владельца" "${FILE_OWNER} ${path}"
        fi
    done
    return 0
}

# Собственные файлы скрипта (только по его маскам, чужие файлы в каталогах не трогаются):
# БД (+ -wal/-shm), логи, HTML-отчёты, latest.html. NUL-разделитель.
owner_candidates() {
    local file
    for file in "${DB_FILE}" "${DB_FILE}-wal" "${DB_FILE}-shm" "${REPORT_DIR}/latest.html"; do
        if [[ -e "${file}" || -L "${file}" ]]; then
            printf '%s\0' "${file}"
        fi
    done
    if [[ -e "${LOG_DIR}" || -L "${LOG_DIR}" ]]; then
        find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -print0 || return 1
    fi
    if [[ -e "${REPORT_DIR}" || -L "${REPORT_DIR}" ]]; then
        find "${REPORT_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.html" -print0 || return 1
    fi
}

# Приводит к FILE_OWNER уже существующие файлы скрипта (однократная починка прошлых запусков).
fix_existing_owner() {
    local file candidates_file
    ((RUN_EUID == 0)) || return 0
    candidates_file="$(mktemp)" || return 1
    if ! owner_candidates > "${candidates_file}"; then
        rm -f -- "${candidates_file}"
        return 1
    fi
    while IFS= read -r -d '' file; do
        fix_owner "${file}"
    done < "${candidates_file}"
    rm -f -- "${candidates_file}"
}

# Для --dry-run: сколько файлов получит другого владельца (только чтение).
count_owner_changes() {
    local file count=0 candidates_file
    if ((RUN_EUID == 0)) && [[ -n "${FILE_OWNER}" ]]; then
        candidates_file="$(mktemp)" || return 1
        if ! owner_candidates > "${candidates_file}"; then
            rm -f -- "${candidates_file}"
            return 1
        fi
        while IFS= read -r -d '' file; do
            if owner_differs "${file}"; then
                count=$((count + 1))
            fi
        done < "${candidates_file}"
        rm -f -- "${candidates_file}"
    fi
    echo "${count}"
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
    fix_owner "${LOG_FILE}"
}

cleanup_reports() {
    local old_reports=()
    local old_report old_reports_file

    old_reports_file="$(mktemp)"
    if find "${REPORT_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.html" -printf '%T@|%p\n' 2>/dev/null \
        | sort -nr \
        | awk -F'|' -v keep="${REPORT_KEEP}" 'NR > keep { print $2 }' > "${old_reports_file}"; then
        while IFS= read -r old_report; do
            [[ -n "${old_report}" ]] && old_reports+=("${old_report}")
        done < "${old_reports_file}"
    fi
    rm -f -- "${old_reports_file}"

    if ((${#old_reports[@]} > 0)); then
        rm -f -- "${old_reports[@]}"
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

usage() {
    cat <<EOF
Использование:
  ${SCRIPT_NAME} [-r|--report] [-n|--dry-run] [--migrate-db] [-c|--config ФАЙЛ] [-h|--help]

Без аргументов:
  Опрашивает диски (smartctl), CPU (lm_sensors, /proc) и GPU (nvidia-smi,
  если есть), сохраняет состояние в SQLite (${DB_FILE}) и формирует
  HTML-отчёт в ${REPORT_DIR}. Предназначен для запуска по расписанию
  (таймер systemd) и/или при загрузке системы.

Опции:
  -r, --report   Только пересобрать HTML-отчёт из уже накопленных в
                 SQLite данных, без повторного опроса.
  -n, --dry-run  Опросить систему и показать, что было бы записано в SQLite и
                 отчёт, не меняя БД, отчёты и не показывая уведомлений; печатает
                 проверку схемы БД (только чтение).
      --migrate-db  Только создать базу или привести её схему к текущей и выйти (без опроса и отчёта);
                    с -n — только план. Используется check_mytasks.sh при развёртывании.
  -c, --config ФАЙЛ
                 Конфиг явно (юнит system-monitor передаёт root-конфиг
                 /etc/mytasks/system_monitor.conf). Без ключа: root-копия —
                 только root-конфиг, запуск из mytasks — root-конфиг, если он
                 есть, иначе conf/system_monitor.conf. Под root чужой конфиг
                 должен принадлежать root и быть недоступен на запись другим.
  -h, --help     Показать эту справку и выйти.
EOF
}

###############################################################################
# ARGS
###############################################################################
REPORT_ONLY=0
DRY_RUN=0
MIGRATE_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run)
            DRY_RUN=1
            shift
            ;;
        -r|--report)
            REPORT_ONLY=1
            shift
            ;;
        --migrate-db)
            MIGRATE_ONLY=1
            shift
            ;;
        -c|--config)
            # Уже учтён до загрузки конфига
            if [[ $# -lt 2 || -z "$2" ]]; then
                echo "Ключ $1 требует путь к конфигу" >&2
                exit 1
            fi
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if ((MIGRATE_ONLY && REPORT_ONLY)); then
    echo "Ключи --migrate-db и --report несовместимы" >&2
    exit 1
fi

###############################################################################
# DB
###############################################################################
# Версия схемы — PRAGMA user_version. Схема меняется ТОЛЬКО миграцией: новая миграция —
# следующий номер DB_LATEST и пересборка таблиц в текущий DDL (образец — миграция 1 в истории git
# до её удаления); DDL каждой таблицы — в одной функции ddl_*; таблицы вручную ALTER не правим.
DB_LATEST=1
DB_TABLES=(disk_stats cpu_stats gpu_stats)
DB_BUSY_TIMEOUT_MS="${DB_BUSY_TIMEOUT_MS:-30000}"

# DDL таблиц: ddl_<таблица> — CREATE TABLE в текущей схеме.
ddl_disk_stats() {
    local name="disk_stats"
    cat <<SQL
CREATE TABLE IF NOT EXISTS ${name} (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT NOT NULL,
    device                TEXT NOT NULL,
    device_alias          TEXT,
    mountpoints           TEXT,
    model                 TEXT,
    serial                TEXT,
    size_bytes            INTEGER,
    health                TEXT NOT NULL,
    temperature_c         INTEGER,
    power_on_hours        INTEGER,
    reallocated_sectors   INTEGER,
    pending_sectors       INTEGER,
    uncorrectable_errors  INTEGER
);
SQL
}

ddl_cpu_stats() {
    local name="cpu_stats"
    cat <<SQL
CREATE TABLE IF NOT EXISTS ${name} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    model          TEXT,
    temperature_c  INTEGER,
    usage_percent  REAL,
    load1          REAL,
    load5          REAL,
    load15         REAL
);
SQL
}

ddl_gpu_stats() {
    local name="gpu_stats"
    cat <<SQL
CREATE TABLE IF NOT EXISTS ${name} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    device         TEXT NOT NULL,
    model          TEXT,
    temperature_c  INTEGER,
    usage_percent  REAL,
    mem_used_mb    INTEGER,
    mem_total_mb   INTEGER,
    power_draw_w   REAL
);
SQL
}

# DDL индексов таблицы (только имя таблицы).
ddl_disk_stats_indexes() {
    echo "CREATE INDEX IF NOT EXISTS idx_disk_stats_device_ts ON disk_stats(device, ts);"
}

ddl_cpu_stats_indexes() {
    echo "CREATE INDEX IF NOT EXISTS idx_cpu_stats_ts ON cpu_stats(ts);"
}

ddl_gpu_stats_indexes() {
    echo "CREATE INDEX IF NOT EXISTS idx_gpu_stats_device_ts ON gpu_stats(device, ts);"
}

table_ddl() {
    case "$1" in
        disk_stats) ddl_disk_stats ;;
        cpu_stats) ddl_cpu_stats ;;
        gpu_stats) ddl_gpu_stats ;;
        *) return 1 ;;
    esac
}

table_index_ddl() {
    case "$1" in
        disk_stats) ddl_disk_stats_indexes ;;
        cpu_stats) ddl_cpu_stats_indexes ;;
        gpu_stats) ddl_gpu_stats_indexes ;;
        *) return 1 ;;
    esac
}

# Чтение существующей БД — только sqlite3 -readonly.
db_read() {
    sqlite3 -readonly -noheader "${DB_FILE}" "$1"
}

db_user_version() {
    db_read "PRAGMA user_version;"
}

db_has_table() {
    local found
    found="$(db_read "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = '$1';")"
    [[ "${found}" == "1" ]]
}

# Новая база: файла нет или в нём нет ни одной таблицы статистики.
db_is_new() {
    local table
    [[ -s "${DB_FILE}" ]] || return 0
    for table in "${DB_TABLES[@]}"; do
        if db_has_table "${table}"; then
            return 1
        fi
    done
    return 0
}

# SQL новой базы: текущая схема и user_version = DB_LATEST одной транзакцией.
db_create_sql() {
    local table ddl idx
    echo "BEGIN IMMEDIATE;"
    for table in "${DB_TABLES[@]}"; do
        ddl="$(table_ddl "${table}")"
        idx="$(table_index_ddl "${table}")"
        printf '%s\n%s\n' "${ddl}" "${idx}"
    done
    echo "PRAGMA user_version = ${DB_LATEST};"
    echo "COMMIT;"
}

db_run_sql() {
    local sql="$1"
    sqlite3 -bail -cmd ".timeout ${DB_BUSY_TIMEOUT_MS}" "${DB_FILE}" <<<"${sql}"
}

init_db() {
    local version sql
    if db_is_new; then
        sql="$(db_create_sql)"
        if ! db_run_sql "${sql}"; then
            echo "Ошибка: не удалось создать базу ${DB_FILE}" >&2
            log_json "ERROR" "db_create_failed" "Не удалось создать базу" "${DB_FILE}"
            return 1
        fi
        log_json "INFO" "db_created" "Создана новая база" "v${DB_LATEST} ${DB_FILE}"
        return 0
    fi

    version="$(db_user_version)"
    if [[ ! "${version}" =~ ^[0-9]+$ ]]; then
        echo "Ошибка: не удалось прочитать версию схемы базы ${DB_FILE}" >&2
        log_json "ERROR" "db_version_unreadable" "Не удалось прочитать версию схемы" "${DB_FILE}"
        return 1
    fi
    if ((version >= DB_LATEST)); then
        return 0
    fi

    # Миграции со схем старше v1 удалены (единственная база переведена на v1 в ноябре 2026);
    # следующая смена схемы — новая миграция v2 (пересборку таблиц можно взять из истории git).
    echo "Ошибка: схема базы v${version} старше поддерживаемой v${DB_LATEST}: ${DB_FILE}" >&2
    log_json "ERROR" "db_version_unsupported" "Схема базы старше поддерживаемой" "v${version} ${DB_FILE}"
    return 1
}

# Проверка схемы для --dry-run: только чтение (sqlite3 -readonly), ничего не создаёт и не меняет.
plan_db_migration() {
    local version
    if db_is_new; then
        echo "[dry-run] будет: создана новая база v${DB_LATEST}: ${DB_FILE}"
        log_json "INFO" "dry_db_plan" "[dry-run] будет создана новая база" "v${DB_LATEST} ${DB_FILE}"
        return 0
    fi
    version="$(db_user_version)"
    if [[ ! "${version}" =~ ^[0-9]+$ ]]; then
        echo "[dry-run] не удалось прочитать версию схемы базы ${DB_FILE} (нет прав?)"
        return 0
    fi
    if ((version >= DB_LATEST)); then
        echo "[dry-run] схема БД уже v${version} (последняя v${DB_LATEST}), изменений нет"
        return 0
    fi
    echo "[dry-run] схема базы v${version} старше поддерживаемой v${DB_LATEST}: обычный запуск завершится ошибкой (${DB_FILE})"
    log_json "WARN" "dry_db_unsupported" "[dry-run] схема базы старше поддерживаемой" "v${version} ${DB_FILE}"
    return 0
}

insert_row() {
    local device="$1" alias="$2" mountpoints="$3" model="$4" serial="$5" size="$6" health="$7" temp="$8" poh="$9" realloc="${10}" pending="${11}" uncorrect="${12}" row_ts="${13}"
    local q_ts q_device q_alias q_mountpoints q_model q_serial q_health
    local n_size n_temp n_poh n_realloc n_pending n_uncorrect

    if ((DRY_RUN)); then
        echo "[dry-run] будет: запись в ${DB_FILE}: disk_stats ${device} (${model}, health=${health}, temp=${temp})"
        return 0
    fi

    q_ts="$(sql_escape "${row_ts}")"
    q_device="$(sql_escape "${device}")"
    q_alias="$(sql_escape "${alias}")"
    q_mountpoints="$(sql_escape "${mountpoints}")"
    q_model="$(sql_escape "${model}")"
    q_serial="$(sql_escape "${serial}")"
    q_health="$(sql_escape "${health}")"
    n_size="$(sql_num "${size}")"
    n_temp="$(sql_num "${temp}")"
    n_poh="$(sql_num "${poh}")"
    n_realloc="$(sql_num "${realloc}")"
    n_pending="$(sql_num "${pending}")"
    n_uncorrect="$(sql_num "${uncorrect}")"

    sqlite3 "${DB_FILE}" "INSERT INTO disk_stats (ts, device, device_alias, mountpoints, model, serial, size_bytes, health, temperature_c, power_on_hours, reallocated_sectors, pending_sectors, uncorrectable_errors) VALUES ('${q_ts}', '${q_device}', '${q_alias}', '${q_mountpoints}', '${q_model}', '${q_serial}', ${n_size}, '${q_health}', ${n_temp}, ${n_poh}, ${n_realloc}, ${n_pending}, ${n_uncorrect});"
}

insert_cpu_row() {
    local model="$1" temp="$2" usage="$3" load1="$4" load5="$5" load15="$6" row_ts="$7"
    local q_ts q_model n_temp n_usage n_load1 n_load5 n_load15

    if ((DRY_RUN)); then
        echo "[dry-run] будет: запись в ${DB_FILE}: cpu_stats ${model} (temp=${temp}, usage=${usage}%, load1=${load1})"
        return 0
    fi

    q_ts="$(sql_escape "${row_ts}")"
    q_model="$(sql_escape "${model}")"
    n_temp="$(sql_num "${temp}")"
    n_usage="$(sql_num "${usage}")"
    n_load1="$(sql_num "${load1}")"
    n_load5="$(sql_num "${load5}")"
    n_load15="$(sql_num "${load15}")"

    sqlite3 "${DB_FILE}" "INSERT INTO cpu_stats (ts, model, temperature_c, usage_percent, load1, load5, load15) VALUES ('${q_ts}', '${q_model}', ${n_temp}, ${n_usage}, ${n_load1}, ${n_load5}, ${n_load15});"
}

insert_gpu_row() {
    local device="$1" model="$2" temp="$3" usage="$4" mem_used="$5" mem_total="$6" power="$7" row_ts="$8"
    local q_ts q_device q_model n_temp n_usage n_mem_used n_mem_total n_power

    if ((DRY_RUN)); then
        echo "[dry-run] будет: запись в ${DB_FILE}: gpu_stats ${device} ${model} (temp=${temp}, usage=${usage}%)"
        return 0
    fi

    q_ts="$(sql_escape "${row_ts}")"
    q_device="$(sql_escape "${device}")"
    q_model="$(sql_escape "${model}")"
    n_temp="$(sql_num "${temp}")"
    n_usage="$(sql_num "${usage}")"
    n_mem_used="$(sql_num "${mem_used}")"
    n_mem_total="$(sql_num "${mem_total}")"
    n_power="$(sql_num "${power}")"

    sqlite3 "${DB_FILE}" "INSERT INTO gpu_stats (ts, device, model, temperature_c, usage_percent, mem_used_mb, mem_total_mb, power_draw_w) VALUES ('${q_ts}', '${q_device}', '${q_model}', ${n_temp}, ${n_usage}, ${n_mem_used}, ${n_mem_total}, ${n_power});"
}

###############################################################################
# DISCOVERY & POLLING — ДИСКИ
###############################################################################
discover_disks() {
    DEVICES=()
    if [[ -n "${DISKS}" ]]; then
        local raw name
        IFS=',' read -ra raw <<< "${DISKS}"
        for name in "${raw[@]}"; do
            name="$(printf '%s' "${name}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
            [[ -z "${name}" ]] && continue
            [[ "${name}" == /dev/* ]] || name="/dev/${name}"
            DEVICES+=("${name}")
        done
        return 0
    fi

    local name type lsblk_out
    lsblk_out="$(lsblk -dn -o NAME,TYPE 2>/dev/null)"
    while read -r name type; do
        [[ "${type}" != "disk" ]] && continue
        case "${name}" in
            zram*|loop*|ram*|sr*|fd*|dm-*) continue ;;
            *) ;;
        esac
        DEVICES+=("/dev/${name}")
    done <<< "${lsblk_out}"
}

# Точки монтирования диска (по всем его разделам/дочерним устройствам).
# Собирает точки монтирования диска, привязанные к конкретному разделу —
# "nvme0n1p1: /, /var/log; nvme0n1p2: /boot/efi", а не единым списком.
# Дерево разделов (с путями /dev/..., в т.ч. /dev/mapper/... для LUKS/LVM) —
# из lsblk -J через jq; точки монтирования — из таблицы монтирования хоста
# (findmnt --task 1, т.е. /proc/1/mountinfo), а не своей: служба работает в
# песочнице systemd (ProtectSystem, ReadWritePaths, PrivateTmp), где lsblk
# видит и служебные bind-монтирования вроде .../state или второго /var/tmp.
# findmnt -S /dev/<раздел> на btrfs отдаёт все подтома раздела (/ и /var/log
# с одного и того же раздела).
disk_mountpoints() {
    local dev="$1" json paths name path fm_json targets out=""
    json="$(lsblk -J -o NAME,PATH -- "${dev}" 2>/dev/null)"
    [[ -z "${json}" ]] && return
    paths="$(jq -r '.. | objects | select(has("name") and has("path")) | "\(.name)\t\(.path)"' 2>/dev/null <<< "${json}")"
    while IFS=$'\t' read -r name path; do
        [[ -n "${name}" && -n "${path}" ]] || continue
        # findmnt завершается с кодом 1, если раздел не смонтирован
        if ! fm_json="$(findmnt --task 1 -J -o TARGET -S "${path}" 2>/dev/null)"; then
            continue
        fi
        targets="$(jq -r '[.filesystems[]?.target] | join(", ")' 2>/dev/null <<< "${fm_json}")"
        [[ -n "${targets}" ]] && out+="${out:+; }${name}: ${targets}"
    done <<< "${paths}"
    [[ -n "${out}" ]] && printf '%s\n' "${out}"
    return 0
}

# Объём диска в байтах. Через lsblk (а не только smartctl), т.к. lsblk
# уже обязательная зависимость режима опроса и доступен даже когда smartctl
# не может прочитать диск (см. правило "все поля отчёта — из БД" в памяти).
disk_size_bytes() {
    local dev="$1" bytes
    bytes="$(lsblk -bdn -o SIZE -- "${dev}" 2>/dev/null | head -n1)"
    [[ "${bytes}" =~ ^[0-9]+$ ]] && printf '%s' "${bytes}"
    return 0
}

# Устойчивое системное имя диска (udev, /dev/disk/by-id) — в отличие от
# /dev/sdX, не меняется при перестановке дисков/перезагрузке.
device_alias() {
    local dev="$1" real link resolved name fallback=""
    real="$(readlink -f -- "${dev}" 2>/dev/null || printf '%s' "${dev}")"

    for link in /dev/disk/by-id/*; do
        [[ -e "${link}" ]] || continue
        resolved="$(readlink -f -- "${link}" 2>/dev/null || true)"
        [[ "${resolved}" == "${real}" ]] || continue
        name="$(basename -- "${link}")"
        case "${name}" in
            nvme-eui.*)
                # Менее читаемо, чем nvme-<VENDOR>_<MODEL>_<SERIAL> — держим как запасной вариант
                fallback="${name}"
                ;;
            ata-*|nvme-*|scsi-*|usb-*)
                printf '%s' "${name}"
                return
                ;;
            *)
                fallback="${name}"
                ;;
        esac
    done

    printf '%s' "${fallback}"
}

poll_disk() {
    local dev="$1" row_ts="$2" json parsed alias mountpoints size
    local health model serial temp poh realloc pending uncorrect
    local -a smartctl_opts=()
    read -r -a smartctl_opts <<< "${SMARTCTL_OPTS}"

    alias="$(device_alias "${dev}")"
    mountpoints="$(disk_mountpoints "${dev}")"
    size="$(disk_size_bytes "${dev}")"
    json="$("${SMARTCTL_BIN}" "${smartctl_opts[@]}" "${dev}" 2>/dev/null || true)"

    if [[ -z "${json}" ]] || ! jq -e . >/dev/null 2>&1 <<< "${json}"; then
        log_json "WARN" "smartctl_read_failed" "Не удалось получить данные SMART" "${dev}"
        insert_row "${dev}" "${alias}" "${mountpoints}" "" "" "${size}" "UNKNOWN" "" "" "" "" "" "${row_ts}"
        return
    fi

    parsed="$(jq -r '
        [
            (if .smart_status.passed == true then "PASSED"
             elif .smart_status.passed == false then "FAILED"
             else "UNKNOWN" end),
            (.model_name // .model_family // "n/a"),
            (.serial_number // "n/a"),
            (.temperature.current // ""),
            (.power_on_time.hours // ""),
            ([.ata_smart_attributes.table[]? | select(.name=="Reallocated_Sector_Ct") | .raw.value][0] // .nvme_smart_health_information_log.media_errors // ""),
            ([.ata_smart_attributes.table[]? | select(.name=="Current_Pending_Sector") | .raw.value][0] // ""),
            ([.ata_smart_attributes.table[]? | select(.name=="Offline_Uncorrectable") | .raw.value][0] // .nvme_smart_health_information_log.num_err_log_entries // "")
        ] | @tsv
    ' <<< "${json}")"

    IFS=$'\t' read -r health model serial temp poh realloc pending uncorrect <<< "${parsed}"

    insert_row "${dev}" "${alias}" "${mountpoints}" "${model}" "${serial}" "${size}" "${health}" "${temp}" "${poh}" "${realloc}" "${pending}" "${uncorrect}" "${row_ts}"
    log_json "INFO" "disk_polled" "Опрошен диск" "${dev}: health=${health}; temp=${temp}"
}

###############################################################################
# ОПРОС — CPU
###############################################################################
cpu_model() {
    awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null
}

# Температура CPU через lm_sensors (JSON, -j). Опционально: если sensors
# не установлен/не настроен (sensors-detect), CPU всё равно опрашивается —
# просто без температуры. Ищем главный тепловой датчик пакета: Tctl/Tdie
# (AMD, k10temp) или "Package id 0" (Intel, coretemp).
cpu_temperature() {
    command -v "${SENSORS_BIN}" >/dev/null 2>&1 || return 0
    "${SENSORS_BIN}" -j 2>/dev/null | jq -r '
        [.. | objects | to_entries[]? |
         select(.key | test("^(Tctl|Tdie|Package id 0)$")) |
         (.value | to_entries[]? | select(.key | test("_input$")) | .value)
        ][0] // empty
    ' 2>/dev/null | awk '{ if ($1 != "") printf "%.0f", $1 }'
}

# Загрузка CPU (%) — две выборки /proc/stat с интервалом CPU_SAMPLE_SECS (по умолчанию 1 с) (тот же
# принцип, что использует top/mpstat), без дополнительных зависимостей.
cpu_usage_percent() {
    local read1 read2 total1 idle1 total2 idle2 dtotal didle
    read1="$(awk '/^cpu /{total=$2+$3+$4+$5+$6+$7+$8+$9+$10+$11; print total, $5}' /proc/stat 2>/dev/null)"
    [[ -z "${read1}" ]] && return 0
    sleep "${CPU_SAMPLE_SECS}"
    read2="$(awk '/^cpu /{total=$2+$3+$4+$5+$6+$7+$8+$9+$10+$11; print total, $5}' /proc/stat 2>/dev/null)"
    [[ -z "${read2}" ]] && return 0
    read -r total1 idle1 <<< "${read1}"
    read -r total2 idle2 <<< "${read2}"
    dtotal=$((total2 - total1))
    didle=$((idle2 - idle1))
    ((dtotal <= 0)) && return 0
    awk -v dt="${dtotal}" -v di="${didle}" 'BEGIN{printf "%.1f", (dt-di)/dt*100}'
}

poll_cpu() {
    local row_ts="$1" model temp usage load1 load5 load15

    model="$(cpu_model)"
    temp="$(cpu_temperature)"
    if [[ -z "${temp}" ]] && ! command -v "${SENSORS_BIN}" >/dev/null 2>&1; then
        log_json "INFO" "sensors_unavailable" "lm_sensors не найден — температура CPU не будет записана" ""
    fi
    usage="$(cpu_usage_percent)"
    read -r load1 load5 load15 _ < /proc/loadavg

    insert_cpu_row "${model}" "${temp}" "${usage}" "${load1}" "${load5}" "${load15}" "${row_ts}"
    log_json "INFO" "cpu_polled" "Опрошен процессор" "temp=${temp}; usage=${usage}%"
}

###############################################################################
# ОПРОС — GPU
###############################################################################
gpu_available() {
    command -v "${NVIDIA_SMI_BIN}" >/dev/null 2>&1
}

# Опционально: если nvidia-smi не найден (нет NVIDIA GPU или драйвера),
# просто пропускаем опрос GPU — это нормальная ситуация, не ошибка.
poll_gpu() {
    local row_ts="$1" index model temp usage mem_used mem_total power

    if ! gpu_available; then
        log_json "INFO" "gpu_unavailable" "nvidia-smi не найден — опрос GPU пропущен" ""
        return 0
    fi

    local nvidia_out
    nvidia_out="$("${NVIDIA_SMI_BIN}" --query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw --format=csv,noheader,nounits 2>/dev/null)"

    while IFS=',' read -r index model temp usage mem_used mem_total power; do
        [[ -z "${index// /}" ]] && continue
        index="$(printf '%s' "${index}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        model="$(printf '%s' "${model}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        temp="$(printf '%s' "${temp}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        usage="$(printf '%s' "${usage}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        mem_used="$(printf '%s' "${mem_used}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        mem_total="$(printf '%s' "${mem_total}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        power="$(printf '%s' "${power}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"

        insert_gpu_row "${index}" "${model}" "${temp}" "${usage}" "${mem_used}" "${mem_total}" "${power}" "${row_ts}"
        log_json "INFO" "gpu_polled" "Опрошена видеокарта" "GPU${index}: temp=${temp}; usage=${usage}%"
    done <<< "${nvidia_out}"
}

###############################################################################
# REPORT ANALYSIS
###############################################################################
# Пороги температуры диска -> DISK_TEMP_WARN_C / DISK_TEMP_CRIT_C (NVMe — свои, если заданы)
set_disk_temp_limits() {
    DISK_TEMP_WARN_C="${TEMP_WARN_C}"
    DISK_TEMP_CRIT_C="${TEMP_CRIT_C}"
    if [[ "$1" == /dev/nvme* ]]; then
        DISK_TEMP_WARN_C="${NVME_TEMP_WARN_C:-${TEMP_WARN_C}}"
        DISK_TEMP_CRIT_C="${NVME_TEMP_CRIT_C:-${TEMP_CRIT_C}}"
    fi
}

classify_status() {
    # $1=health $2=temp $3=realloc $4=pending $5=uncorrect $6=device -> echoes "ok|warn|crit"
    local health="$1" temp="$2" realloc="$3" pending="$4" uncorrect="$5"
    set_disk_temp_limits "${6:-}"

    if [[ "${health}" == "FAILED" ]]; then
        echo "crit"
        return
    fi
    if [[ "${uncorrect}" =~ ^[0-9]+$ ]] && ((uncorrect > 0)); then
        echo "crit"
        return
    fi
    if [[ "${temp}" =~ ^-?[0-9]+$ ]] && ((temp >= DISK_TEMP_CRIT_C)); then
        echo "crit"
        return
    fi
    if [[ "${realloc}" =~ ^[0-9]+$ ]] && ((realloc > 0)); then
        echo "warn"
        return
    fi
    if [[ "${pending}" =~ ^[0-9]+$ ]] && ((pending > 0)); then
        echo "warn"
        return
    fi
    if [[ "${temp}" =~ ^-?[0-9]+$ ]] && ((temp >= DISK_TEMP_WARN_C)); then
        echo "warn"
        return
    fi
    if [[ "${health}" == "UNKNOWN" ]]; then
        echo "unknown"
        return
    fi
    echo "ok"
}

# Классификация по одному порогу температуры (CPU/GPU) -> ok|warn|crit|unknown
classify_temp_status() {
    local temp="$1" warn_c="$2" crit_c="$3"
    if [[ ! "${temp}" =~ ^-?[0-9]+$ ]]; then
        echo "unknown"
        return
    fi
    if ((temp >= crit_c)); then
        echo "crit"
    elif ((temp >= warn_c)); then
        echo "warn"
    else
        echo "ok"
    fi
}

# Формирует текст аналитической справки по одному диску (на русском).
analyze_disk() {
    local device="$1" health="$2" temp="$3" poh="$4" realloc="$5" pending="$6" uncorrect="$7" mountpoints="$8"
    local -a notes=()
    set_disk_temp_limits "${device}"

    case "${health}" in
        FAILED)
            notes+=("SMART сообщает об отказе диска (overall-health: FAILED). Диск требует немедленной замены, убедитесь в наличии актуальной резервной копии данных.")
            ;;
        UNKNOWN)
            notes+=("Не удалось получить статус SMART (нет доступа к диску или он не поддерживает SMART). Проверьте права запуска (нужен root) и подключение диска.")
            ;;
        *) ;;
    esac

    if [[ "${uncorrect}" =~ ^[0-9]+$ ]] && ((uncorrect > 0)); then
        notes+=("Обнаружены неисправимые ошибки чтения (${uncorrect}). Высокий риск потери данных — рекомендуется срочная замена накопителя.")
    fi
    if [[ "${realloc}" =~ ^[0-9]+$ ]] && ((realloc > 0)); then
        notes+=("Есть переназначенные секторы (Reallocated_Sector_Ct=${realloc}) — признак деградации поверхности носителя. Стоит планировать замену и чаще делать резервные копии.")
    fi
    if [[ "${pending}" =~ ^[0-9]+$ ]] && ((pending > 0)); then
        notes+=("Есть секторы, ожидающие переназначения (Current_Pending_Sector=${pending}). Рекомендуется внеплановая полная проверка диска (long self-test).")
    fi
    if [[ "${temp}" =~ ^-?[0-9]+$ ]]; then
        if ((temp >= DISK_TEMP_CRIT_C)); then
            notes+=("Температура ${temp}°C выше критического порога (${DISK_TEMP_CRIT_C}°C) — проверьте охлаждение и обдув корпуса.")
        elif ((temp >= DISK_TEMP_WARN_C)); then
            notes+=("Температура ${temp}°C приближается к порогу предупреждения (${DISK_TEMP_WARN_C}°C), стоит проверить охлаждение.")
        fi
    fi

    if [[ ${#notes[@]} -eq 0 ]]; then
        notes+=("Показатели в норме, признаков деградации накопителя не обнаружено.")
    fi

    if [[ "${poh}" =~ ^[0-9]+$ ]]; then
        local days=$((poh / 24))
        notes+=("Наработка: ${poh} ч (~${days} дн.).")
    fi

    if [[ -n "${mountpoints}" ]]; then
        notes+=("Точки монтирования: ${mountpoints}.")
    else
        notes+=("Диск не смонтирован ни в одну точку.")
    fi

    local out=""
    local n
    for n in "${notes[@]}"; do
        out+="${n} "
    done
    printf '%s' "${out% }"
}

status_label() {
    case "$1" in
        ok) echo "Норма" ;;
        warn) echo "Внимание" ;;
        crit) echo "Критично" ;;
        unknown) echo "Неизвестно" ;;
        *) echo "Неизвестно" ;;
    esac
}

status_color() {
    case "$1" in
        ok) echo "#0ca30c" ;;
        warn) echo "#fab219" ;;
        crit) echo "#d03b3b" ;;
        unknown) echo "#898781" ;;
        *) echo "#898781" ;;
    esac
}

# Человекочитаемый объём диска (двоичные приставки КиБ/МиБ/ГиБ/ТиБ/ПиБ)
format_size() {
    local bytes="$1"
    if [[ ! "${bytes}" =~ ^[0-9]+$ ]]; then
        printf '—'
        return
    fi
    awk -v b="${bytes}" 'BEGIN{
        split("Б КиБ МиБ ГиБ ТиБ ПиБ", units, " ")
        u=1; v=b
        while (v>=1024 && u<6) { v/=1024; u++ }
        if (u==1) printf "%d %s", v, units[u]
        else printf "%.1f %s", v, units[u]
    }'
}

###############################################################################
# REPORT — CHARTS (inline SVG, без внешних библиотек)
###############################################################################
build_temp_chart() {
    local ts_axis_file="$1" hist_file="$2"
    local awk_prog

    awk_prog=$(cat <<'EOF'
BEGIN {
    split("#2a78d6 #eb6834 #1baf7a #eda100 #e87ba4 #008300 #4a3aa7 #e34948", colors, " ")
    W=760; H=260; PADL=40; PADR=16; PADT=16; PADB=34
    YMIN=0; YMAX=90
    n_ts=0; n_dev=0
}
FNR==NR {
    n_ts++
    ts_order[n_ts]=$0
    ts_index[$0]=n_ts
    next
}
{
    n=split($0, a, "|")
    if (n<3) next
    dev=a[1]; t=a[2]; temp=a[3]
    if (!(dev in dev_seen)) { dev_seen[dev]=1; n_dev++; dev_order[n_dev]=dev }
    xi = ts_index[t]
    if (xi=="") next
    key = dev SUBSEP xi
    if (temp != "") { val[key]=temp+0; hasval[key]=1 }
}
END {
    if (n_ts < 1) { print "<p class=\"empty\">Недостаточно данных для графика температуры.</p>"; exit }
    plot_w = W - PADL - PADR
    plot_h = H - PADT - PADB

    print "<div class=\"chart-wrap\">"
    printf "<svg viewBox=\"0 0 %d %d\" preserveAspectRatio=\"xMidYMid meet\" class=\"chart-svg\" role=\"img\" aria-label=\"История температуры дисков\">\n", W, H

    # сетка и подписи оси Y
    split("0 45 90", yticks, " ")
    for (i=1;i<=3;i++) {
        yv=yticks[i]+0
        gy = PADT + (YMAX-yv)/(YMAX-YMIN)*plot_h
        printf "<line x1=\"%d\" y1=\"%.1f\" x2=\"%d\" y2=\"%.1f\" stroke=\"#e1e0d9\" stroke-width=\"1\"/>\n", PADL, gy, W-PADR, gy
        printf "<text x=\"%d\" y=\"%.1f\" font-size=\"11\" fill=\"#898781\" text-anchor=\"end\">%s°</text>\n", PADL-6, gy+4, yv
    }
    printf "<line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"#c3c2b7\" stroke-width=\"1\"/>\n", PADL, PADT+plot_h, W-PADR, PADT+plot_h

    if (n_dev > 8) n_dev_draw = 8; else n_dev_draw = n_dev

    for (d=1; d<=n_dev_draw; d++) {
        dev = dev_order[d]
        color = colors[((d-1)%8)+1]
        seg = ""
        pts_n = 0
        for (xi=1; xi<=n_ts; xi++) {
            key = dev SUBSEP xi
            if (!(key in hasval)) {
                if (pts_n >= 2) printf "<polyline points=\"%s\" fill=\"none\" stroke=\"%s\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>\n", seg, color
                seg=""; pts_n=0
                continue
            }
            if (n_ts>1) px = PADL + (xi-1)*(plot_w/(n_ts-1)); else px = PADL + plot_w/2
            py = PADT + (YMAX-val[key])/(YMAX-YMIN)*plot_h
            seg = seg sprintf("%.1f,%.1f ", px, py)
            pts_n++
            printf "<circle cx=\"%.1f\" cy=\"%.1f\" r=\"3\" fill=\"%s\"><title>%s: %s°C</title></circle>\n", px, py, color, dev, val[key]
            if (d % 2 == 0) labely = py + 13; else labely = py - 7
            printf "<text x=\"%.1f\" y=\"%.1f\" font-size=\"9\" fill=\"#52514e\" text-anchor=\"middle\">%s°</text>\n", px, labely, val[key]
        }
        if (pts_n >= 2) printf "<polyline points=\"%s\" fill=\"none\" stroke=\"%s\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>\n", seg, color
    }

    printf "<text x=\"%d\" y=\"%d\" font-size=\"11\" fill=\"#898781\" text-anchor=\"start\">%s</text>\n", PADL, H-8, ts_order[1]
    if (n_ts > 1) printf "<text x=\"%d\" y=\"%d\" font-size=\"11\" fill=\"#898781\" text-anchor=\"end\">%s</text>\n", W-PADR, H-8, ts_order[n_ts]
    print "</svg>"

    print "<div class=\"legend\">"
    for (d=1; d<=n_dev_draw; d++) {
        color = colors[((d-1)%8)+1]
        printf "<span class=\"legend-item\"><i style=\"background:%s\"></i>%s</span>\n", color, dev_order[d]
    }
    if (n_dev > n_dev_draw) printf "<span class=\"legend-item legend-more\">и ещё %d диск(ов)</span>\n", n_dev-n_dev_draw
    print "</div></div>"
}
EOF
)
    awk "${awk_prog}" "${ts_axis_file}" "${hist_file}"
}

build_sector_chart() {
    local latest_file="$1"
    local awk_prog

    awk_prog=$(cat <<'EOF'
BEGIN {
    FS="|"
    W=760; H=240; PADL=40; PADR=16; PADT=30; PADB=48
    n=0; maxv=1
}
{
    dev=$1; realloc=$10; pending=$11
    if (realloc !~ /^[0-9]+$/) realloc=0
    if (pending !~ /^[0-9]+$/) pending=0
    n++
    devs[n]=dev; rv[n]=realloc; pv[n]=pending
    if (realloc>maxv) maxv=realloc
    if (pending>maxv) maxv=pending
}
END {
    if (n < 1) { print "<p class=\"empty\">Нет данных для диаграммы секторов.</p>"; exit }
    plot_w = W - PADL - PADR
    plot_h = H - PADT - PADB
    ymax = maxv * 1.2
    group_w = plot_w / n
    bar_w = group_w * 0.28

    print "<div class=\"chart-wrap\">"
    printf "<svg viewBox=\"0 0 %d %d\" preserveAspectRatio=\"xMidYMid meet\" class=\"chart-svg\" role=\"img\" aria-label=\"Переназначенные и ожидающие секторы по дискам\">\n", W, H
    printf "<line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"#c3c2b7\" stroke-width=\"1\"/>\n", PADL, PADT+plot_h, W-PADR, PADT+plot_h

    for (i=1; i<=n; i++) {
        gx = PADL + (i-1)*group_w + group_w/2

        rh = (rv[i]/ymax)*plot_h
        rx = gx - bar_w - 2
        ry = PADT + plot_h - rh
        printf "<rect x=\"%.1f\" y=\"%.1f\" width=\"%.1f\" height=\"%.1f\" rx=\"3\" fill=\"#2a78d6\"><title>%s: Reallocated=%s</title></rect>\n", rx, ry, bar_w, (rh<1?1:rh), devs[i], rv[i]
        if (rv[i]+0 > 0) printf "<text x=\"%.1f\" y=\"%.1f\" font-size=\"10\" text-anchor=\"middle\" fill=\"#52514e\">%s</text>\n", rx+bar_w/2, ry-4, rv[i]

        ph = (pv[i]/ymax)*plot_h
        px = gx + 2
        py = PADT + plot_h - ph
        printf "<rect x=\"%.1f\" y=\"%.1f\" width=\"%.1f\" height=\"%.1f\" rx=\"3\" fill=\"#eb6834\"><title>%s: Pending=%s</title></rect>\n", px, py, bar_w, (ph<1?1:ph), devs[i], pv[i]
        if (pv[i]+0 > 0) printf "<text x=\"%.1f\" y=\"%.1f\" font-size=\"10\" text-anchor=\"middle\" fill=\"#52514e\">%s</text>\n", px+bar_w/2, py-4, pv[i]

        printf "<text x=\"%.1f\" y=\"%d\" font-size=\"11\" fill=\"#898781\" text-anchor=\"middle\">%s</text>\n", gx, PADT+plot_h+16, devs[i]
    }
    print "</svg>"
    print "<div class=\"legend\"><span class=\"legend-item\"><i style=\"background:#2a78d6\"></i>Reallocated_Sector_Ct</span><span class=\"legend-item\"><i style=\"background:#eb6834\"></i>Current_Pending_Sector</span></div>"
    print "</div>"
}
EOF
)
    awk "${awk_prog}" "${latest_file}"
}

# Общий многосерийный график для CPU/GPU: все метрики нормированы в шкалу
# 0-100 (температура, °C, и проценты естественно укладываются в одну шкалу),
# поэтому одна общая ось не нарушает правило "не смешивать разные шкалы" —
# это не dual-axis, а честно общий диапазон. У каждой точки — свой суффикс
# единицы измерения (° или %), проставленный в исходных данных.
# Формат строк hist_file: "серия|ts|значение|суффикс"
build_metric_chart() {
    local ts_axis_file="$1" hist_file="$2" aria_label="$3"
    local awk_prog

    awk_prog=$(cat <<'EOF'
BEGIN {
    split("#2a78d6 #eb6834 #1baf7a #eda100 #e87ba4 #008300 #4a3aa7 #e34948", colors, " ")
    W=760; H=260; PADL=34; PADR=16; PADT=16; PADB=34
    YMIN=0; YMAX=100
    n_ts=0; n_series=0
}
FNR==NR {
    n_ts++
    ts_order[n_ts]=$0
    ts_index[$0]=n_ts
    next
}
{
    n=split($0, a, "|")
    if (n<4) next
    series=a[1]; t=a[2]; v=a[3]; unit=a[4]
    if (!(series in seen)) { seen[series]=1; n_series++; series_order[n_series]=series }
    xi = ts_index[t]
    if (xi=="") next
    key = series SUBSEP xi
    if (v != "") { val[key]=v+0; hasval[key]=1; unitof[series]=unit }
}
END {
    if (n_ts < 1 || n_series < 1) { print "<p class=\"empty\">Недостаточно данных для графика.</p>"; exit }
    plot_w = W - PADL - PADR
    plot_h = H - PADT - PADB

    print "<div class=\"chart-wrap\">"
    printf "<svg viewBox=\"0 0 %d %d\" preserveAspectRatio=\"xMidYMid meet\" class=\"chart-svg\" role=\"img\" aria-label=\"%s\">\n", W, H, ARIA

    split("0 50 100", yticks, " ")
    for (i=1;i<=3;i++) {
        yv=yticks[i]+0
        gy = PADT + (YMAX-yv)/(YMAX-YMIN)*plot_h
        printf "<line x1=\"%d\" y1=\"%.1f\" x2=\"%d\" y2=\"%.1f\" stroke=\"#e1e0d9\" stroke-width=\"1\"/>\n", PADL, gy, W-PADR, gy
        printf "<text x=\"%d\" y=\"%.1f\" font-size=\"11\" fill=\"#898781\" text-anchor=\"end\">%s</text>\n", PADL-6, gy+4, yv
    }
    printf "<line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"#c3c2b7\" stroke-width=\"1\"/>\n", PADL, PADT+plot_h, W-PADR, PADT+plot_h

    if (n_series > 8) n_draw = 8; else n_draw = n_series

    for (d=1; d<=n_draw; d++) {
        series = series_order[d]
        color = colors[((d-1)%8)+1]
        seg = ""
        pts_n = 0
        for (xi=1; xi<=n_ts; xi++) {
            key = series SUBSEP xi
            if (!(key in hasval)) {
                if (pts_n >= 2) printf "<polyline points=\"%s\" fill=\"none\" stroke=\"%s\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>\n", seg, color
                seg=""; pts_n=0
                continue
            }
            v = val[key]
            if (v > YMAX) v = YMAX
            if (v < YMIN) v = YMIN
            if (n_ts>1) px = PADL + (xi-1)*(plot_w/(n_ts-1)); else px = PADL + plot_w/2
            py = PADT + (YMAX-v)/(YMAX-YMIN)*plot_h
            seg = seg sprintf("%.1f,%.1f ", px, py)
            pts_n++
            printf "<circle cx=\"%.1f\" cy=\"%.1f\" r=\"3\" fill=\"%s\"><title>%s: %s%s</title></circle>\n", px, py, color, series, val[key], unitof[series]
            if (d % 2 == 0) labely = py + 13; else labely = py - 7
            printf "<text x=\"%.1f\" y=\"%.1f\" font-size=\"9\" fill=\"#52514e\" text-anchor=\"middle\">%s%s</text>\n", px, labely, val[key], unitof[series]
        }
        if (pts_n >= 2) printf "<polyline points=\"%s\" fill=\"none\" stroke=\"%s\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>\n", seg, color
    }

    printf "<text x=\"%d\" y=\"%d\" font-size=\"11\" fill=\"#898781\" text-anchor=\"start\">%s</text>\n", PADL, H-8, ts_order[1]
    if (n_ts > 1) printf "<text x=\"%d\" y=\"%d\" font-size=\"11\" fill=\"#898781\" text-anchor=\"end\">%s</text>\n", W-PADR, H-8, ts_order[n_ts]
    print "</svg>"

    print "<div class=\"legend\">"
    for (d=1; d<=n_draw; d++) {
        color = colors[((d-1)%8)+1]
        printf "<span class=\"legend-item\"><i style=\"background:%s\"></i>%s</span>\n", color, series_order[d]
    }
    if (n_series > n_draw) printf "<span class=\"legend-item legend-more\">и ещё %d</span>\n", n_series-n_draw
    print "</div></div>"
}
EOF
)
    awk -v ARIA="${aria_label}" "${awk_prog}" "${ts_axis_file}" "${hist_file}"
}

###############################################################################
# REPORT — сборка HTML
###############################################################################
generate_report() {
    local report_file="$1"
    local latest_tsv ts_axis_file hist_file tmp_report
    local device alias mountpoints model serial size health temp poh realloc pending uncorrect row_ts
    local n_ok=0 n_warn=0 n_crit=0 n_unknown=0
    local rows_html="" cards_html="" first_ts="" cutoff_ts=""

    CRITICAL_LINES=()
    WARNING_LINES=()

    latest_tsv="$(sqlite3 -noheader -separator '|' "${DB_FILE}" \
        "SELECT device, device_alias, mountpoints, model, serial, size_bytes, health, temperature_c, power_on_hours, reallocated_sectors, pending_sectors, uncorrectable_errors, ts FROM disk_stats WHERE id IN (SELECT MAX(id) FROM disk_stats GROUP BY device) ORDER BY device;")"

    if [[ -z "${latest_tsv}" ]]; then
        echo "Ошибка: в базе ${DB_FILE} нет данных" >&2
        log_json "ERROR" "no_data" "В базе нет данных для отчёта" "${DB_FILE}"
        return 2
    fi

    while IFS='|' read -r device alias mountpoints model serial size health temp poh realloc pending uncorrect row_ts; do
        [[ -z "${device}" ]] && continue
        local status color label analysis
        local e_device e_mountpoints e_model e_serial e_size e_label e_row_ts e_alias e_analysis
        status="$(classify_status "${health}" "${temp}" "${realloc}" "${pending}" "${uncorrect}" "${device}")"
        color="$(status_color "${status}")"
        label="$(status_label "${status}")"
        analysis="$(analyze_disk "${device}" "${health}" "${temp}" "${poh}" "${realloc}" "${pending}" "${uncorrect}" "${mountpoints}")"

        case "${status}" in
            ok) ((n_ok++)) ;;
            warn) ((n_warn++)); WARNING_LINES+=("${device}: ${label}") ;;
            crit) ((n_crit++)); CRITICAL_LINES+=("${device}: ${label}") ;;
            *) ((n_unknown++)) ;;
        esac

        e_device="$(html_escape "${device}")"
        e_mountpoints="$(html_escape "${mountpoints}")"
        e_model="$(html_escape "${model}")"
        e_serial="$(html_escape "${serial}")"
        e_size="$(format_size "${size}")"
        e_label="$(html_escape "${label}")"
        e_row_ts="$(html_escape "${row_ts}")"
        e_alias="$(html_escape "${alias}")"
        e_analysis="$(html_escape "${analysis}")"

        rows_html+="<tr>
<td>${e_device}</td>
<td>${mountpoints:+${e_mountpoints}}</td>
<td>${e_model}</td>
<td>${e_serial}</td>
<td>${e_size}</td>
<td><span class=\"badge\" style=\"--c:${color}\"><i></i>${e_label}</span></td>
<td>${temp:-—}</td>
<td>${poh:-—}</td>
<td>${realloc:-0}</td>
<td>${pending:-0}</td>
<td>${uncorrect:-0}</td>
<td>${e_row_ts}</td>
</tr>
"

        cards_html+="<div class=\"disk-card\" style=\"--c:${color}\">
<h3><span class=\"badge\" style=\"--c:${color}\"><i></i>${e_label}</span> ${e_device} <span class=\"muted\">${e_model}${alias:+ · ${e_alias}}</span></h3>
<p>${e_analysis}</p>
</div>
"
    done <<< "${latest_tsv}"

    ts_axis_file="$(mktemp)"
    hist_file="$(mktemp)"
    sqlite3 -noheader "${DB_FILE}" \
        "SELECT ts FROM (SELECT DISTINCT ts FROM disk_stats ORDER BY ts DESC LIMIT ${HISTORY_POINTS}) ORDER BY ts ASC;" > "${ts_axis_file}"
    first_ts="$(head -n1 "${ts_axis_file}" 2>/dev/null || true)"
    if [[ -n "${first_ts}" ]]; then
        local q_cutoff_ts
        cutoff_ts="${first_ts}"
        q_cutoff_ts="$(sql_escape "${cutoff_ts}")"
        sqlite3 -noheader -separator '|' "${DB_FILE}" \
            "SELECT device, ts, COALESCE(temperature_c,'') FROM disk_stats WHERE ts >= '${q_cutoff_ts}' ORDER BY device, ts;" > "${hist_file}"
    fi

    local latest_file
    latest_file="$(mktemp)"
    printf '%s\n' "${latest_tsv}" > "${latest_file}"

    local temp_chart sector_chart
    temp_chart="$(build_temp_chart "${ts_axis_file}" "${hist_file}")"
    sector_chart="$(build_sector_chart "${latest_file}")"
    rm -f -- "${hist_file}" "${latest_file}"

    # --- CPU ---
    local cpu_model_v cpu_temp cpu_usage cpu_load1 cpu_load5 cpu_load15 cpu_ts
    local cpu_latest cpu_rows_html="" cpu_chart="<p class=\"empty\">Нет данных о CPU.</p>"
    cpu_latest="$(sqlite3 -noheader -separator '|' "${DB_FILE}" \
        "SELECT model, temperature_c, usage_percent, load1, load5, load15, ts FROM cpu_stats ORDER BY id DESC LIMIT 1;")"
    if [[ -n "${cpu_latest}" ]]; then
        IFS='|' read -r cpu_model_v cpu_temp cpu_usage cpu_load1 cpu_load5 cpu_load15 cpu_ts <<< "${cpu_latest}"
        local cstatus ccolor clabel
        cstatus="$(classify_temp_status "${cpu_temp}" "${CPU_TEMP_WARN_C}" "${CPU_TEMP_CRIT_C}")"
        ccolor="$(status_color "${cstatus}")"
        clabel="$(status_label "${cstatus}")"
        case "${cstatus}" in
            ok) ((n_ok++)) ;;
            warn) ((n_warn++)); WARNING_LINES+=("CPU: ${clabel}") ;;
            crit) ((n_crit++)); CRITICAL_LINES+=("CPU: ${clabel}") ;;
            *) ((n_unknown++)) ;;
        esac

        local e_cpu_model e_clabel e_cpu_ts
        e_cpu_model="$(html_escape "${cpu_model_v}")"
        e_clabel="$(html_escape "${clabel}")"
        e_cpu_ts="$(html_escape "${cpu_ts}")"

        cpu_rows_html="<tr>
<td>${e_cpu_model}</td>
<td><span class=\"badge\" style=\"--c:${ccolor}\"><i></i>${e_clabel}</span></td>
<td>${cpu_temp:-—}</td>
<td>${cpu_usage:-—}</td>
<td>${cpu_load1:-—} / ${cpu_load5:-—} / ${cpu_load15:-—}</td>
<td>${e_cpu_ts}</td>
</tr>
"
        if [[ -n "${cutoff_ts}" ]]; then
            local cpu_hist_file q_cutoff_ts_cpu
            cpu_hist_file="$(mktemp)"
            q_cutoff_ts_cpu="$(sql_escape "${cutoff_ts}")"
            sqlite3 -noheader -separator '|' "${DB_FILE}" \
                "SELECT 'CPU температура', ts, COALESCE(temperature_c,''), '°' FROM cpu_stats WHERE ts >= '${q_cutoff_ts_cpu}'
                 UNION ALL
                 SELECT 'CPU загрузка, %', ts, COALESCE(usage_percent,''), '%' FROM cpu_stats WHERE ts >= '${q_cutoff_ts_cpu}';" > "${cpu_hist_file}"
            cpu_chart="$(build_metric_chart "${ts_axis_file}" "${cpu_hist_file}" "История CPU: температура и загрузка")"
            rm -f -- "${cpu_hist_file}"
        fi
    fi

    # --- GPU ---
    local gpu_latest gpu_device gpu_model_v gpu_temp gpu_usage gpu_mem_used gpu_mem_total gpu_power gpu_ts
    local gpu_rows_html="" gpu_chart="<p class=\"empty\">Видеокарта NVIDIA не обнаружена (nvidia-smi недоступен) либо данных ещё нет.</p>"
    gpu_latest="$(sqlite3 -noheader -separator '|' "${DB_FILE}" \
        "SELECT device, model, temperature_c, usage_percent, mem_used_mb, mem_total_mb, power_draw_w, ts FROM gpu_stats WHERE id IN (SELECT MAX(id) FROM gpu_stats GROUP BY device) ORDER BY device;")"
    if [[ -n "${gpu_latest}" ]]; then
        while IFS='|' read -r gpu_device gpu_model_v gpu_temp gpu_usage gpu_mem_used gpu_mem_total gpu_power gpu_ts; do
            [[ -z "${gpu_device}" ]] && continue
            local gstatus gcolor glabel
            gstatus="$(classify_temp_status "${gpu_temp}" "${GPU_TEMP_WARN_C}" "${GPU_TEMP_CRIT_C}")"
            gcolor="$(status_color "${gstatus}")"
            glabel="$(status_label "${gstatus}")"
            case "${gstatus}" in
                ok) ((n_ok++)) ;;
                warn) ((n_warn++)); WARNING_LINES+=("GPU${gpu_device}: ${glabel}") ;;
                crit) ((n_crit++)); CRITICAL_LINES+=("GPU${gpu_device}: ${glabel}") ;;
                *) ((n_unknown++)) ;;
            esac

            local e_gpu_model e_glabel e_gpu_ts
            e_gpu_model="$(html_escape "${gpu_model_v}")"
            e_glabel="$(html_escape "${glabel}")"
            e_gpu_ts="$(html_escape "${gpu_ts}")"

            gpu_rows_html+="<tr>
<td>GPU${gpu_device}</td>
<td>${e_gpu_model}</td>
<td><span class=\"badge\" style=\"--c:${gcolor}\"><i></i>${e_glabel}</span></td>
<td>${gpu_temp:-—}</td>
<td>${gpu_usage:-—}</td>
<td>${gpu_mem_used:-—} / ${gpu_mem_total:-—} МиБ</td>
<td>${gpu_power:-—}</td>
<td>${e_gpu_ts}</td>
</tr>
"
        done <<< "${gpu_latest}"

        if [[ -n "${cutoff_ts}" ]]; then
            local gpu_hist_file q_cutoff_ts_gpu
            gpu_hist_file="$(mktemp)"
            q_cutoff_ts_gpu="$(sql_escape "${cutoff_ts}")"
            sqlite3 -noheader -separator '|' "${DB_FILE}" \
                "SELECT 'GPU'||device||' температура', ts, COALESCE(temperature_c,''), '°' FROM gpu_stats WHERE ts >= '${q_cutoff_ts_gpu}'
                 UNION ALL
                 SELECT 'GPU'||device||' загрузка, %', ts, COALESCE(usage_percent,''), '%' FROM gpu_stats WHERE ts >= '${q_cutoff_ts_gpu}'
                 UNION ALL
                 SELECT 'GPU'||device||' VRAM, %', ts, CASE WHEN mem_total_mb>0 THEN ROUND(100.0*mem_used_mb/mem_total_mb,1) ELSE '' END, '%' FROM gpu_stats WHERE ts >= '${q_cutoff_ts_gpu}';" > "${gpu_hist_file}"
            gpu_chart="$(build_metric_chart "${ts_axis_file}" "${gpu_hist_file}" "История GPU: температура, загрузка, VRAM")"
            rm -f -- "${gpu_hist_file}"
        fi
    fi

    rm -f -- "${ts_axis_file}"

    local e_db_file e_report_dir e_nvme_limits=""
    e_db_file="$(html_escape "${DB_FILE}")"
    e_report_dir="$(html_escape "${REPORT_DIR}")"
    if [[ -n "${NVME_TEMP_WARN_C}${NVME_TEMP_CRIT_C}" ]]; then
        set_disk_temp_limits /dev/nvme
        e_nvme_limits=" (NVMe: warn=${DISK_TEMP_WARN_C}°C, crit=${DISK_TEMP_CRIT_C}°C)"
    fi

    tmp_report="$(mktemp)"
    cat > "${tmp_report}" <<HTML
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Диагностика системы — ${TIMESTAMP}</title>
<style>
:root{
  --surface-1:#fcfcfb; --page:#f9f9f7; --text-primary:#0b0b0b; --text-secondary:#52514e;
  --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,0.10);
}
@media (prefers-color-scheme: dark){
  :root{ --surface-1:#1a1a19; --page:#0d0d0d; --text-primary:#ffffff; --text-secondary:#c3c2b7; --grid:#2c2c2a; --border:rgba(255,255,255,0.10); }
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text-primary);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--text-secondary);font-size:13px;margin-bottom:20px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px 18px;min-width:110px}
.tile .n{font-size:26px;font-weight:600}
.tile .l{font-size:12px;color:var(--text-secondary)}
section{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:20px}
section h2{font-size:15px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid var(--grid)}
th{color:var(--text-secondary);font-weight:600;font-size:12px}
td{font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:12px}
.badge i{width:9px;height:9px;border-radius:50%;background:var(--c);display:inline-block}
.disk-card{border-left:3px solid var(--c);padding:8px 12px;margin-bottom:10px;background:var(--page)}
.disk-card h3{font-size:14px;margin:0 0 4px;display:flex;gap:8px;align-items:center}
.disk-card p{font-size:13px;color:var(--text-secondary);margin:0}
.muted{color:var(--muted);font-weight:400;font-size:12px}
.chart-wrap{width:100%}
.chart-svg{width:100%;height:auto;display:block}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12px;color:var(--text-secondary)}
.legend-item{display:inline-flex;align-items:center;gap:6px}
.legend-item i{width:10px;height:10px;border-radius:2px;display:inline-block}
.empty{color:var(--muted);font-size:13px}
footer{color:var(--muted);font-size:12px;margin-top:12px}
</style>
</head>
<body>
<h1>Диагностика системы</h1>
<div class="sub">Отчёт сформирован: ${TIMESTAMP} · база: ${e_db_file}</div>

<div class="tiles">
<div class="tile"><div class="n">$((n_ok + n_warn + n_crit + n_unknown))</div><div class="l">Всего объектов</div></div>
<div class="tile"><div class="n" style="color:#0ca30c">${n_ok}</div><div class="l">В норме</div></div>
<div class="tile"><div class="n" style="color:#fab219">${n_warn}</div><div class="l">Внимание</div></div>
<div class="tile"><div class="n" style="color:#d03b3b">${n_crit}</div><div class="l">Критично</div></div>
</div>

<section>
<h2>Состояние дисков</h2>
<table>
<thead><tr><th>Устройство</th><th>Точка монтирования</th><th>Модель</th><th>Серийный №</th><th>Объём</th><th>Статус</th><th>Темп., °C</th><th>Наработка, ч</th><th>Reallocated</th><th>Pending</th><th>Uncorrectable</th><th>Обновлено</th></tr></thead>
<tbody>
${rows_html}
</tbody>
</table>
</section>

<section>
<h2>Аналитическая справка по дискам</h2>
${cards_html}
</section>

<section>
<h2>Процессор (CPU)</h2>
<table>
<thead><tr><th>Модель</th><th>Статус</th><th>Темп., °C</th><th>Загрузка, %</th><th>Load avg (1/5/15)</th><th>Обновлено</th></tr></thead>
<tbody>
${cpu_rows_html:-<tr><td colspan="6" class="empty">Нет данных</td></tr>}
</tbody>
</table>
${cpu_chart}
</section>

<section>
<h2>Видеокарта (GPU)</h2>
<table>
<thead><tr><th>GPU</th><th>Модель</th><th>Статус</th><th>Темп., °C</th><th>Загрузка, %</th><th>VRAM</th><th>Мощность, Вт</th><th>Обновлено</th></tr></thead>
<tbody>
${gpu_rows_html:-<tr><td colspan="8" class="empty">Нет данных</td></tr>}
</tbody>
</table>
${gpu_chart}
</section>

<section>
<h2>История температуры дисков (последние ${HISTORY_POINTS} опросов)</h2>
${temp_chart}
</section>

<section>
<h2>Переназначенные / ожидающие секторы по дискам</h2>
${sector_chart}
</section>

<footer>${SCRIPT_NAME} · пороги дисков: warn=${TEMP_WARN_C}°C, crit=${TEMP_CRIT_C}°C${e_nvme_limits} · CPU: warn=${CPU_TEMP_WARN_C}°C, crit=${CPU_TEMP_CRIT_C}°C · GPU: warn=${GPU_TEMP_WARN_C}°C, crit=${GPU_TEMP_CRIT_C}°C · отчёт хранится ${REPORT_KEEP} последних версий в ${e_report_dir}</footer>
</body>
</html>
HTML

    # Права — до переноса: chmod по готовому пути пошёл бы по подменённой ссылке
    chmod 644 -- "${tmp_report}" || return 1
    mv -f -- "${tmp_report}" "${report_file}"
}

###############################################################################
# MAIN
###############################################################################
require_cmd sqlite3
if ((DRY_RUN == 0)); then
    mkdir -p "${REPORT_DIR}" "$(dirname -- "${DB_FILE}")"
    if ! init_db; then
        cleanup_logs
        exit 2
    fi
    if ! fix_existing_owner; then
        echo "Ошибка: не удалось перечислить файлы для исправления владельца" >&2
        cleanup_logs
        exit 2
    fi
else
    plan_db_migration
fi

# --migrate-db: только создание или миграция базы, без опроса и отчёта (развёртывание с нуля — check_mytasks.sh)
if ((MIGRATE_ONLY)); then
    cleanup_logs
    exit 0
fi

if ((REPORT_ONLY == 0)); then
    require_cmd smartctl
    require_cmd jq
    require_cmd lsblk
    require_cmd findmnt

    declare -a DEVICES=()
    discover_disks

    if [[ ${#DEVICES[@]} -eq 0 ]]; then
        echo "Ошибка: не найдено ни одного диска для опроса" >&2
        log_json "ERROR" "no_disks_found" "Не найдено дисков для опроса" ""
        cleanup_logs
        exit 2
    fi

    log_json "INFO" "start" "Опрос системы" "${#DEVICES[@]} дисков"
    RUN_TS="$(ts)"
    for dev in "${DEVICES[@]}"; do
        poll_disk "${dev}" "${RUN_TS}"
    done
    poll_cpu "${RUN_TS}"
    poll_gpu "${RUN_TS}"
elif ((DRY_RUN == 0)); then
    ROW_COUNT="$(sqlite3 -noheader "${DB_FILE}" "SELECT COUNT(*) FROM disk_stats;" 2>/dev/null || echo 0)"
    if [[ "${ROW_COUNT}" -eq 0 ]]; then
        echo "Ошибка: в базе ${DB_FILE} ещё нет данных — сначала запустите ${SCRIPT_NAME} без ключей" >&2
        log_json "ERROR" "no_data" "Нет данных для отчёта (режим --report)" "${DB_FILE}"
        cleanup_logs
        exit 2
    fi
fi

REPORT_FILE="${REPORT_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.html"
if ((DRY_RUN)); then
    echo "[dry-run] будет: сформирован HTML-отчёт ${REPORT_FILE} и обновлён ${REPORT_DIR}/latest.html"
    if ((RUN_EUID == 0)) && [[ -n "${FILE_OWNER}" ]]; then
        if ! OWNER_CHANGES="$(count_owner_changes)"; then
            echo "Ошибка: не удалось перечислить файлы для плана смены владельца" >&2
            cleanup_logs
            exit 2
        fi
        echo "[dry-run] будет: сменить владельца на ${FILE_OWNER}: ${OWNER_CHANGES} файлов"
    fi
    echo "[dry-run] будет: уведомление, если в отчёте окажутся объекты в критическом состоянии"
    echo "[dry-run] Изменения не выполнены."
    log_json "INFO" "dry_done" "[dry-run] Опрос выполнен, БД и отчёты не изменены" "${REPORT_FILE}"
    cleanup_logs
    exit 0
fi
if ! generate_report "${REPORT_FILE}"; then
    cleanup_logs
    exit 2
fi
# latest.html — через временный файл рядом и rename: rename заменяет саму
# ссылку, а не её цель, права ставятся до rename (chmod/cp идут по симлинку)
if ! LATEST_TMP="$(mktemp -- "${REPORT_DIR}/.latest.html.XXXXXX")" \
    || ! cp -- "${REPORT_FILE}" "${LATEST_TMP}" \
    || ! chmod 644 -- "${LATEST_TMP}" \
    || ! mv -f -- "${LATEST_TMP}" "${REPORT_DIR}/latest.html"; then
    echo "Ошибка: не удалось обновить ${REPORT_DIR}/latest.html" >&2
    log_json "ERROR" "latest_failed" "Не удалось обновить latest.html" "${REPORT_DIR}"
    [[ -n "${LATEST_TMP:-}" ]] && rm -f -- "${LATEST_TMP}"
    cleanup_logs
    exit 2
fi
fix_owner "${REPORT_FILE}" "${REPORT_DIR}/latest.html" "${DB_FILE}" "${DB_FILE}-wal" "${DB_FILE}-shm"
log_json "INFO" "report_generated" "Сформирован HTML-отчёт" "${REPORT_FILE}"

FINAL_EXIT=0
if [[ ${#CRITICAL_LINES[@]} -gt 0 ]]; then
    DETAIL="$(printf '%s; ' "${CRITICAL_LINES[@]}")"
    DETAIL="${DETAIL%; }"
    echo "КРИТИЧНО: ${DETAIL}"
    notify_alert "Система: критическое состояние" "${DETAIL}"
    log_json "ERROR" "critical_status" "Обнаружены объекты в критическом состоянии" "${DETAIL}"
    FINAL_EXIT=1
fi
if [[ ${#WARNING_LINES[@]} -gt 0 ]]; then
    WDETAIL="$(printf '%s; ' "${WARNING_LINES[@]}")"
    WDETAIL="${WDETAIL%; }"
    log_json "WARN" "system_warning" "Объекты, требующие внимания" "${WDETAIL}"
fi

echo "Отчёт: ${REPORT_FILE}"
cleanup_logs
cleanup_reports
exit "${FINAL_EXIT}"
