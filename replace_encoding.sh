#!/usr/bin/env bash

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
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
}

log_json() {
    local level="$1"
    local event="$2"
    local msg="$3"
    local detail="${4:-}"
    local level_norm msg_esc detail_esc _ts
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    _ts="$(ts)"
    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
        "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    local old_logs
    mapfile -t old_logs < <(
        { find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
            | sort -nr \
            | awk -F'|' 'NR > 10 { print $2 }'; } || true
    )
    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

###############################################################################
# USAGE
###############################################################################

usage() {
    cat << EOF
Использование: ${SCRIPT_NAME} [опции] [путь_к_каталогу] [исходная_строка] [новая_строка]

Опции:
  -h, --help              Показать справку
  -d, --dry-run           Сухой запуск (не менять файлы)
  -n, --no-recursive      Только в указанном каталоге (без подкаталогов)

По умолчанию: "windows-1251" → "utf-8"
EOF
    exit 0
}

command -v grep >/dev/null 2>&1 || { echo "Ошибка: grep не установлен."; exit 1; }
command -v sed >/dev/null 2>&1 || { echo "Ошибка: sed не установлен."; exit 1; }

DRY_RUN=false
RECURSIVE=true
TARGET_DIR="."
FROM_STR="windows-1251"
TO_STR="utf-8"

while getopts ":hdn-:" opt; do
    case ${opt} in
        h) usage ;;
        d) DRY_RUN=true ;;
        n) RECURSIVE=false ;;
        -)
            case "${OPTARG}" in
                help) usage ;;
                dry-run) DRY_RUN=true ;;
                no-recursive) RECURSIVE=false ;;
                *) echo "Ошибка: неизвестная опция --${OPTARG}" >&2; exit 1 ;;
            esac ;;
        \?) echo "Ошибка: неизвестная опция -${OPTARG}" >&2; exit 1 ;;
        *) ;;
    esac
done
shift $((OPTIND - 1))

[[ -n "${1:-}" ]] && [[ -d "$1" ]] && { TARGET_DIR="$1"; shift; }
[[ -n "${1:-}" ]] && { FROM_STR="$1"; shift; }
[[ -n "${1:-}" ]] && { TO_STR="$1"; shift; }

[[ ! -d "${TARGET_DIR}" ]] && { echo "Ошибка: каталог '${TARGET_DIR}' не существует." >&2; exit 1; }
[[ -z "${FROM_STR}" ]] && { echo "Ошибка: исходная строка пустая." >&2; exit 1; }

log_json "INFO" "start" "Запуск замены строк" "dir=${TARGET_DIR}; from=${FROM_STR}; to=${TO_STR}; dry_run=${DRY_RUN}; recursive=${RECURSIVE}"

echo "Каталог: ${TARGET_DIR}"
echo "Замена: \"${FROM_STR}\" → \"${TO_STR}\""
${RECURSIVE} && echo "Режим: рекурсивный" || echo "Режим: только текущий каталог"
${DRY_RUN} && echo "=== СУХОЙ ЗАПУСК ===" || echo "=== РЕАЛЬНАЯ ЗАМЕНА ==="
echo

# Сначала собираем все файлы в массив и считаем общее количество
if ${RECURSIVE}; then
    mapfile -t files < <(grep -rlI -- "${FROM_STR}" "${TARGET_DIR}" 2>/dev/null || true)
else
    mapfile -t files < <(grep -lI -- "${FROM_STR}" "${TARGET_DIR}"/* 2>/dev/null || true)
fi
total=${#files[@]}

if (( total == 0 )); then
    echo "Файлы с строкой \"${FROM_STR}\" не найдены."
    log_json "INFO" "done" "Совпадения не найдены"
    cleanup_logs
    exit 0
fi

echo "Найдено файлов для обработки: ${total}"
echo

# Функция прогресс-бара
progress_bar() {
    local current=$1
    local total=$2
    local width=50
    local percent=$(( current * 100 / total ))
    local filled=$(( current * width / total ))
    local empty=$(( width - filled ))

    printf "\r["
    printf "%${filled}s" | tr ' ' "#"
    printf "%${empty}s" | tr ' ' "░"
    printf "] %3d%% (%d/%d)" "${percent}" "${current}" "${total}"
}

# Экранирование для sed
SED_FROM=$(printf '%s\n' "${FROM_STR}" | sed 's/[\/&]/\\&/g')
SED_TO=$(printf '%s\n' "${TO_STR}" | sed 's/[\/&]/\\&/g')

# Обработка файлов с прогресс-баром
counter=0
for file in "${files[@]}"; do
    ((counter++))
    progress_bar "${counter}" "${total}"

    [[ -f "${file}" ]] || continue

    if ${DRY_RUN}; then
        echo -e "\nНайдено в: ${file}"
        grep -n -- "${FROM_STR}" "${file}" | sed "s/${SED_FROM}/[31m&[0m/g; s/$/  ← заменится на \"${TO_STR}\"/"
        log_json "INFO" "would_replace" "Найдено совпадение" "${file}"
    else
        echo -e "\nОбрабатываем: ${file}"
        if sed -i.bak "s/${SED_FROM}/${SED_TO}/g" "${file}" 2>/dev/null; then
            rm -f "${file}.bak"
            log_json "INFO" "replaced" "Замена выполнена" "${file}"
        else
            sed -i "s/${SED_FROM}/${SED_TO}/g" "${file}"
            log_json "INFO" "replaced" "Замена выполнена (fallback sed -i)" "${file}"
        fi
    fi
done

echo -e "\n"
if ${DRY_RUN}; then
    echo "Сухой запуск завершён. Обработано файлов: ${total} (ничего не изменено)."
    log_json "INFO" "done" "Сухой запуск завершен" "files=${total}"
else
    echo "Замена завершена. Обработано файлов: ${total}."
    log_json "INFO" "done" "Замена завершена" "files=${total}"
fi

cleanup_logs
