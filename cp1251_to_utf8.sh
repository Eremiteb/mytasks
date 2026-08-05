#!/bin/sh

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
mkdir -p "${LOG_DIR}"

if [ -r "${LOG_TEMPLATE_FILE}" ]; then
    # shellcheck source=/dev/null
    . "${LOG_TEMPLATE_FILE}"
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
    dry_run_json="$( [ "${DRY_RUN}" -eq 1 ] && echo true || echo false )"
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    _ts="$(ts)"
    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","dry_run":%s,"msg":"%s","detail":"%s"}\n' \
        "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${dry_run_json}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
      | sort -nr \
      | awk -F'|' 'NR > 10 { print $2 }' \
      | while IFS= read -r old_log; do
          [ -n "${old_log}" ] && rm -f -- "${old_log}"
        done
}

usage() {
    cat <<EOF
Использование: ${SCRIPT_NAME} <каталог> [--dry-run] [-h|--help]

Рекурсивно перекодирует текстовые файлы из Windows-1251 в UTF-8.

Аргументы:
    <каталог>   Путь к каталогу для обработки
    --dry-run   Только показать файлы, которые будут перекодированы (без изменений)
    -h, --help  Показать эту справку и выйти
EOF
}

###############################################################################
# ARGS
###############################################################################
DIR="${1:-}"
MODE="${2:-}"
DRY_RUN=0

if [ "${DIR}" = "-h" ] || [ "${DIR}" = "--help" ]; then
    usage
    exit 0
fi

if [ -z "${DIR}" ]; then
    usage >&2
    exit 1
fi

if [ ! -d "${DIR}" ]; then
    echo "Ошибка: каталог не найден: ${DIR}" >&2
    exit 1
fi

[ "${MODE}" = "--dry-run" ] && DRY_RUN=1

###############################################################################
# MAIN
###############################################################################
echo "Каталог: ${DIR}"
[ "${DRY_RUN}" -eq 1 ] && echo "Режим: DRY-RUN"
log_json "INFO" "start" "Запуск перекодировки" "${DIR}"

find "${DIR}" -type f | while IFS= read -r file; do
    if ! file -b --mime "${file}" | grep -q 'text'; then
        continue
    fi

    if iconv -f utf-8 -t utf-8 "${file}" >/dev/null 2>&1; then
        continue
    fi

    if iconv -f windows-1251 -t utf-8 "${file}" >/dev/null 2>&1; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            echo "[DRY-RUN] перекодировать: ${file}"
            log_json "INFO" "would_convert" "Файл требует перекодировки" "${file}"
        else
            tmp="$(mktemp)"
            iconv -f windows-1251 -t utf-8 "${file}" > "${tmp}"
            chmod --reference="${file}" "${tmp}"
            chown --reference="${file}" "${tmp}" 2>/dev/null || true
            mv "${tmp}" "${file}"
            echo "[OK] перекодирован: ${file}"
            log_json "INFO" "converted" "Файл перекодирован" "${file}"
        fi
    fi
done

echo "Готово."
log_json "INFO" "done" "Обработка завершена"
cleanup_logs
exit 0
