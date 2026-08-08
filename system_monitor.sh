#!/usr/bin/env bash
# Диагностика жёстких дисков (SMART): опрос, запись состояния в SQLite,
# HTML-отчёт (таблица + диаграммы + аналитическая справка по каждому диску).
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

DB_FILE="${DB_FILE:-${STATE_DIR}/disk_monitor.db}"
REPORT_DIR="${REPORT_DIR:-${SCRIPT_DIR}/disk_reports}"
REPORT_KEEP="${REPORT_KEEP:-5}"
KEEP_LOGS="${KEEP_LOGS:-10}"
HISTORY_POINTS="${HISTORY_POINTS:-30}"
SMARTCTL_BIN="${SMARTCTL_BIN:-smartctl}"
DISKS="${DISKS:-}"
TEMP_WARN_C="${TEMP_WARN_C:-50}"
TEMP_CRIT_C="${TEMP_CRIT_C:-60}"
APP_NAME="${APP_NAME:-DiskMonitor}"
ICON_NAME="${ICON_NAME:-drive-harddisk}"
URGENCY="${URGENCY:-critical}"

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
  ${SCRIPT_NAME} [-r|--report] [-h|--help]

Без аргументов:
  Опрашивает диски (smartctl), сохраняет состояние в SQLite (${DB_FILE})
  и формирует HTML-отчёт в ${REPORT_DIR}. Предназначен для запуска при
  загрузке системы (cron @reboot / systemd) и по расписанию.

Опции:
  -r, --report   Только пересобрать HTML-отчёт из уже накопленных в
                 SQLite данных, без повторного опроса дисков.
  -h, --help     Показать эту справку и выйти.
EOF
}

###############################################################################
# ARGS
###############################################################################
REPORT_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--report)
            REPORT_ONLY=1
            shift
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

###############################################################################
# DB
###############################################################################
init_db() {
    sqlite3 "${DB_FILE}" <<'SQL'
CREATE TABLE IF NOT EXISTS disk_stats (
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
CREATE INDEX IF NOT EXISTS idx_disk_stats_device_ts ON disk_stats(device, ts);
SQL
    # Миграции для баз, созданных до появления этих колонок (ошибку "duplicate column" игнорируем)
    sqlite3 "${DB_FILE}" "ALTER TABLE disk_stats ADD COLUMN device_alias TEXT;" 2>/dev/null || true
    sqlite3 "${DB_FILE}" "ALTER TABLE disk_stats ADD COLUMN mountpoints TEXT;" 2>/dev/null || true
    sqlite3 "${DB_FILE}" "ALTER TABLE disk_stats ADD COLUMN size_bytes INTEGER;" 2>/dev/null || true
}

insert_row() {
    local device="$1" alias="$2" mountpoints="$3" model="$4" serial="$5" size="$6" health="$7" temp="$8" poh="$9" realloc="${10}" pending="${11}" uncorrect="${12}" row_ts="${13}"
    sqlite3 "${DB_FILE}" "INSERT INTO disk_stats (ts, device, device_alias, mountpoints, model, serial, size_bytes, health, temperature_c, power_on_hours, reallocated_sectors, pending_sectors, uncorrectable_errors) VALUES ('$(sql_escape "${row_ts}")', '$(sql_escape "${device}")', '$(sql_escape "${alias}")', '$(sql_escape "${mountpoints}")', '$(sql_escape "${model}")', '$(sql_escape "${serial}")', $(sql_num "${size}"), '$(sql_escape "${health}")', $(sql_num "${temp}"), $(sql_num "${poh}"), $(sql_num "${realloc}"), $(sql_num "${pending}"), $(sql_num "${uncorrect}"));"
}

###############################################################################
# DISCOVERY & POLLING
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

    local name type
    while read -r name type; do
        [[ "${type}" != "disk" ]] && continue
        case "${name}" in
            zram*|loop*|ram*|sr*|fd*|dm-*) continue ;;
            *) ;;
        esac
        DEVICES+=("/dev/${name}")
    done < <(lsblk -dn -o NAME,TYPE 2>/dev/null)
}

# Точки монтирования диска (по всем его разделам/дочерним устройствам).
# Собирает точки монтирования диска, привязанные к конкретному разделу —
# "nvme0n1p1: /, /var/log; nvme0n1p2: /boot/efi", а не единым списком.
# Через JSON (lsblk -J + jq), а не построчный разбор: на btrfs один и тот же
# раздел обычно смонтирован сразу в несколько мест через подтома (subvolumes,
# например / и /var/log с одного и того же раздела), и лёгкий способ узнать,
# какому именно разделу принадлежит каждая точка — рекурсивно обойти дерево
# lsblk -J по каждому "name" с непустым mountpoints. MOUNTPOINT (ед. число)
# в построчном режиме отдаёт только одну точку на раздел и может молча
# потерять корень "/", а раздельный построчный разбор MOUNTPOINTS (мн. число)
# неоднозначен на продолжающихся строках без имени раздела.
disk_mountpoints() {
    local dev="$1" json
    json="$(lsblk -J -o NAME,MOUNTPOINTS -- "${dev}" 2>/dev/null)"
    [[ -z "${json}" ]] && return
    jq -r '
        [ .. | objects | select(has("name")) |
          select((.mountpoints // []) | length > 0) |
          "\(.name): " + ((.mountpoints // []) | join(", "))
        ] | join("; ")
    ' 2>/dev/null <<< "${json}"
}

# Устойчивое системное имя диска (udev, /dev/disk/by-id) — в отличие от
# /dev/sdX, не меняется при перестановке дисков/перезагрузке.
# Объём диска в байтах. Через lsblk (а не только smartctl), т.к. lsblk
# уже обязательная зависимость режима опроса и доступен даже когда smartctl
# не может прочитать диск (см. правило "все поля отчёта — из БД" в памяти).
disk_size_bytes() {
    local dev="$1" bytes
    bytes="$(lsblk -bdn -o SIZE -- "${dev}" 2>/dev/null | head -n1)"
    [[ "${bytes}" =~ ^[0-9]+$ ]] && printf '%s' "${bytes}"
    return 0
}

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

    alias="$(device_alias "${dev}")"
    mountpoints="$(disk_mountpoints "${dev}")"
    size="$(disk_size_bytes "${dev}")"
    json="$("${SMARTCTL_BIN}" -a -j -d auto "${dev}" 2>/dev/null || true)"

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
# REPORT ANALYSIS
###############################################################################
classify_status() {
    # $1=health $2=temp $3=realloc $4=pending $5=uncorrect -> echoes "ok|warn|crit"
    local health="$1" temp="$2" realloc="$3" pending="$4" uncorrect="$5"

    if [[ "${health}" == "FAILED" ]]; then
        echo "crit"
        return
    fi
    if [[ "${uncorrect}" =~ ^[0-9]+$ ]] && ((uncorrect > 0)); then
        echo "crit"
        return
    fi
    if [[ "${temp}" =~ ^-?[0-9]+$ ]] && ((temp >= TEMP_CRIT_C)); then
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
    if [[ "${temp}" =~ ^-?[0-9]+$ ]] && ((temp >= TEMP_WARN_C)); then
        echo "warn"
        return
    fi
    if [[ "${health}" == "UNKNOWN" ]]; then
        echo "unknown"
        return
    fi
    echo "ok"
}

# Формирует текст аналитической справки по одному диску (на русском).
analyze_disk() {
    local device="$1" health="$2" temp="$3" poh="$4" realloc="$5" pending="$6" uncorrect="$7" mountpoints="$8"
    local -a notes=()

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
        if ((temp >= TEMP_CRIT_C)); then
            notes+=("Температура ${temp}°C выше критического порога (${TEMP_CRIT_C}°C) — проверьте охлаждение и обдув корпуса.")
        elif ((temp >= TEMP_WARN_C)); then
            notes+=("Температура ${temp}°C приближается к порогу предупреждения (${TEMP_WARN_C}°C), стоит проверить охлаждение.")
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
        status="$(classify_status "${health}" "${temp}" "${realloc}" "${pending}" "${uncorrect}")"
        color="$(status_color "${status}")"
        label="$(status_label "${status}")"
        analysis="$(analyze_disk "${device}" "${health}" "${temp}" "${poh}" "${realloc}" "${pending}" "${uncorrect}" "${mountpoints}")"

        case "${status}" in
            ok) ((n_ok++)) ;;
            warn) ((n_warn++)); WARNING_LINES+=("${device}: ${label}") ;;
            crit) ((n_crit++)); CRITICAL_LINES+=("${device}: ${label}") ;;
            *) ((n_unknown++)) ;;
        esac

        rows_html+="<tr>
<td>$(html_escape "${device}")</td>
<td>${mountpoints:+$(html_escape "${mountpoints}")}</td>
<td>$(html_escape "${model}")</td>
<td>$(html_escape "${serial}")</td>
<td>$(format_size "${size}")</td>
<td><span class=\"badge\" style=\"--c:${color}\"><i></i>$(html_escape "${label}")</span></td>
<td>${temp:-—}</td>
<td>${poh:-—}</td>
<td>${realloc:-0}</td>
<td>${pending:-0}</td>
<td>${uncorrect:-0}</td>
<td>$(html_escape "${row_ts}")</td>
</tr>
"

        cards_html+="<div class=\"disk-card\" style=\"--c:${color}\">
<h3><span class=\"badge\" style=\"--c:${color}\"><i></i>$(html_escape "${label}")</span> $(html_escape "${device}") <span class=\"muted\">$(html_escape "${model}")${alias:+ · ${alias}}</span></h3>
<p>$(html_escape "${analysis}")</p>
</div>
"
    done <<< "${latest_tsv}"

    ts_axis_file="$(mktemp)"
    hist_file="$(mktemp)"
    sqlite3 -noheader "${DB_FILE}" \
        "SELECT ts FROM (SELECT DISTINCT ts FROM disk_stats ORDER BY ts DESC LIMIT ${HISTORY_POINTS}) ORDER BY ts ASC;" > "${ts_axis_file}"
    first_ts="$(head -n1 "${ts_axis_file}" 2>/dev/null || true)"
    if [[ -n "${first_ts}" ]]; then
        cutoff_ts="${first_ts}"
        sqlite3 -noheader -separator '|' "${DB_FILE}" \
            "SELECT device, ts, COALESCE(temperature_c,'') FROM disk_stats WHERE ts >= '$(sql_escape "${cutoff_ts}")' ORDER BY device, ts;" > "${hist_file}"
    fi

    local latest_file
    latest_file="$(mktemp)"
    printf '%s\n' "${latest_tsv}" > "${latest_file}"

    local temp_chart sector_chart
    temp_chart="$(build_temp_chart "${ts_axis_file}" "${hist_file}")"
    sector_chart="$(build_sector_chart "${latest_file}")"
    rm -f -- "${ts_axis_file}" "${hist_file}" "${latest_file}"

    tmp_report="$(mktemp)"
    cat > "${tmp_report}" <<HTML
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Диагностика дисков — ${TIMESTAMP}</title>
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
<h1>Диагностика жёстких дисков</h1>
<div class="sub">Отчёт сформирован: ${TIMESTAMP} · база: $(html_escape "${DB_FILE}")</div>

<div class="tiles">
<div class="tile"><div class="n">$((n_ok + n_warn + n_crit + n_unknown))</div><div class="l">Всего дисков</div></div>
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
<h2>Аналитическая справка</h2>
${cards_html}
</section>

<section>
<h2>История температуры (последние ${HISTORY_POINTS} опросов)</h2>
${temp_chart}
</section>

<section>
<h2>Переназначенные / ожидающие секторы по дискам</h2>
${sector_chart}
</section>

<footer>${SCRIPT_NAME} · пороги: warn=${TEMP_WARN_C}°C, crit=${TEMP_CRIT_C}°C · отчёт хранится ${REPORT_KEEP} последних версий в $(html_escape "${REPORT_DIR}")</footer>
</body>
</html>
HTML

    mv -f -- "${tmp_report}" "${report_file}"
    chmod 644 -- "${report_file}"
}

###############################################################################
# MAIN
###############################################################################
require_cmd sqlite3
mkdir -p "${REPORT_DIR}" "$(dirname -- "${DB_FILE}")"
init_db

if ((REPORT_ONLY == 0)); then
    require_cmd smartctl
    require_cmd jq
    require_cmd lsblk

    declare -a DEVICES=()
    discover_disks

    if [[ ${#DEVICES[@]} -eq 0 ]]; then
        echo "Ошибка: не найдено ни одного диска для опроса" >&2
        log_json "ERROR" "no_disks_found" "Не найдено дисков для опроса" ""
        cleanup_logs
        exit 2
    fi

    log_json "INFO" "start" "Опрос дисков" "${#DEVICES[@]} устройств"
    RUN_TS="$(ts)"
    for dev in "${DEVICES[@]}"; do
        poll_disk "${dev}" "${RUN_TS}"
    done
else
    ROW_COUNT="$(sqlite3 -noheader "${DB_FILE}" "SELECT COUNT(*) FROM disk_stats;" 2>/dev/null || echo 0)"
    if [[ "${ROW_COUNT}" -eq 0 ]]; then
        echo "Ошибка: в базе ${DB_FILE} ещё нет данных — сначала запустите ${SCRIPT_NAME} без ключей" >&2
        log_json "ERROR" "no_data" "Нет данных для отчёта (режим --report)" "${DB_FILE}"
        cleanup_logs
        exit 2
    fi
fi

REPORT_FILE="${REPORT_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.html"
if ! generate_report "${REPORT_FILE}"; then
    cleanup_logs
    exit 2
fi
cp -f -- "${REPORT_FILE}" "${REPORT_DIR}/latest.html"
chmod 644 -- "${REPORT_DIR}/latest.html"
log_json "INFO" "report_generated" "Сформирован HTML-отчёт" "${REPORT_FILE}"

FINAL_EXIT=0
if [[ ${#CRITICAL_LINES[@]} -gt 0 ]]; then
    DETAIL="$(printf '%s; ' "${CRITICAL_LINES[@]}")"
    DETAIL="${DETAIL%; }"
    echo "КРИТИЧНО: ${DETAIL}"
    notify_alert "Диски: критическое состояние" "${DETAIL}"
    log_json "ERROR" "critical_disk_status" "Обнаружены диски в критическом состоянии" "${DETAIL}"
    FINAL_EXIT=1
fi
if [[ ${#WARNING_LINES[@]} -gt 0 ]]; then
    WDETAIL="$(printf '%s; ' "${WARNING_LINES[@]}")"
    WDETAIL="${WDETAIL%; }"
    log_json "WARN" "disk_warning" "Диски, требующие внимания" "${WDETAIL}"
fi

echo "Отчёт: ${REPORT_FILE}"
cleanup_logs
cleanup_reports
exit "${FINAL_EXIT}"
