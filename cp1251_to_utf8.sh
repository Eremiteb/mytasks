#!/usr/bin/env bash

set -eu
# Функции в подстановках команд наследуют set -e (SC2311).
shopt -s inherit_errexit

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
mkdir -p "${LOG_DIR}"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    . "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# Экранирование строки для JSON: \ и ", \t, \n, прочие управляющие символы
# (в т.ч. CR) — \u00XX; иначе табуляция или перевод строки в имени файла
# дают невалидную строку JSONL. Замена циклом, а не gsub: обратная косая
# черта в строке замены gsub трактуется по-разному в gawk и mawk.
json_escape() {
  printf '%s' "${1-}" | awk 'BEGIN { ORS = ""; for (i = 1; i < 32; i++) code[sprintf("%c", i)] = i }
    {
      out = ""; s = $0
      while (match(s, /[\\"\001-\037]/)) {
        c = substr(s, RSTART, 1)
        if (c == "\\") e = "\\\\"
        else if (c == "\"") e = "\\\""
        else if (c == "\t") e = "\\t"
        else e = sprintf("\\u%04x", code[c])
        out = out substr(s, 1, RSTART - 1) e
        s = substr(s, RSTART + 1)
      }
      if (NR > 1) print "\\n"
      print out s
    }'
}

log_json() {
    level="$1"
    event="$2"
    msg="$3"
    detail="${4:-}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      dry_run_json=true
    else
      dry_run_json=false
    fi
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    _ts="$(ts)"
    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","dry_run":%s,"msg":"%s","detail":"%s"}\n' \
        "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${dry_run_json}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    local listing="" sorted old_logs old_log find_rc=0
    # Сбой find не должен прерывать запуск: чистим по тому, что удалось перечислить.
    listing="$(find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null)" || find_rc=$?
    if [[ "${find_rc}" -ne 0 ]]; then
      log_json "WARN" "cleanup_logs_partial" "find не смог перечислить журналы полностью" "${LOG_DIR}"
    fi
    sorted="$(sort -nr <<<"${listing}")"
    old_logs="$(awk -F'|' -v keep="${KEEP_LOGS:-10}" 'NR > keep { print $2 }' <<<"${sorted}")"
    while IFS= read -r old_log; do
      if [[ -n "${old_log}" ]]; then
        rm -f -- "${old_log}"
      fi
    done <<<"${old_logs}"
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

if [[ "${DIR}" = "-h" ]] || [[ "${DIR}" = "--help" ]]; then
    usage
    exit 0
fi

if [[ -z "${DIR}" ]]; then
    usage >&2
    exit 1
fi

if [[ ! -d "${DIR}" ]]; then
    echo "Ошибка: каталог не найден: ${DIR}" >&2
    exit 1
fi

if [[ "${MODE}" = "--dry-run" ]]; then
    DRY_RUN=1
fi

###############################################################################
# MAIN
###############################################################################
echo "Каталог: ${DIR}"
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Режим: DRY-RUN"
fi
log_json "INFO" "start" "Запуск перекодировки" "${DIR}"

# Кандидаты — только текстовые файлы, у которых libmagic определяет 8-битную
# кодировку (cp1251 он выдаёт как iso-8859-1 / windows-1252 / unknown-8bit).
# Валидный UTF-8, ASCII, UTF-16/32 и бинарные файлы не трогаются: их перекодировка
# из windows-1251 «всегда проходит» и испортила бы содержимое.
# Результат — в CANDIDATE (1/0), код возврата всегда 0: проверка вызывается без
# условия, чтобы set -e не отключался внутри функции (SC2310).
CANDIDATE=0
is_candidate() {
    local f="$1" mime enc
    CANDIDATE=0
    mime="$(file -b --mime "${f}")" || return 0
    case "${mime}" in
        *text*) ;;
        *) return 0 ;;
    esac
    enc="$(file -b --mime-encoding "${f}")" || return 0
    case "${enc}" in
        iso-8859-1|windows-1252|unknown-8bit) ;;
        *) return 0 ;;
    esac
    if iconv -f utf-8 -t utf-8 "${f}" >/dev/null 2>&1; then
        return 0
    fi
    if iconv -f windows-1251 -t utf-8 "${f}" >/dev/null 2>&1; then
        CANDIDATE=1
    fi
}

# Результат — в CONVERT_OK (1/0), код возврата всегда 0 (см. is_candidate).
CONVERT_OK=0
convert_file() {
    local f="$1" tmp
    CONVERT_OK=0
    tmp="$(mktemp -- "${f}.XXXXXXXX")" || return 0
    if iconv -f windows-1251 -t utf-8 "${f}" > "${tmp}" \
        && chmod --reference="${f}" -- "${tmp}" \
        && { chown --reference="${f}" -- "${tmp}" 2>/dev/null || true; } \
        && mv -- "${tmp}" "${f}"; then
        CONVERT_OK=1
        return 0
    fi
    rm -f -- "${tmp}"
}

LIST_FILE="$(mktemp)"
trap 'rm -f -- "${LIST_FILE}"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Список файлов — во временный файл: цикл читает его без подстановки процесса.
# Сбой find (например, нечитаемый подкаталог) не прерывает обработку найденного.
find_rc=0
find "${DIR}" -type f -print0 > "${LIST_FILE}" || find_rc=$?
if [[ "${find_rc}" -ne 0 ]]; then
    echo "Предупреждение: find завершился с кодом ${find_rc}, список файлов может быть неполным" >&2
    log_json "WARN" "find_partial" "find завершился с ошибкой, список файлов может быть неполным" "${DIR}"
fi

while IFS= read -r -d '' file; do
    is_candidate "${file}"
    if [[ "${CANDIDATE}" -ne 1 ]]; then
        continue
    fi

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "[DRY-RUN] перекодировать: ${file}"
        log_json "INFO" "would_convert" "Файл требует перекодировки" "${file}"
        continue
    fi

    convert_file "${file}"
    if [[ "${CONVERT_OK}" -eq 1 ]]; then
        echo "[OK] перекодирован: ${file}"
        log_json "INFO" "converted" "Файл перекодирован" "${file}"
    else
        echo "[ОШИБКА] не удалось перекодировать: ${file}" >&2
        log_json "ERROR" "convert_failed" "iconv завершился с ошибкой" "${file}"
    fi
done < "${LIST_FILE}"

echo "Готово."
log_json "INFO" "done" "Обработка завершена"
cleanup_logs
exit 0
