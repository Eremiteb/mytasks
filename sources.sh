#!/bin/sh
# POSIX sh script

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
[ -z "${SCRIPT_DIR}" ] && SCRIPT_DIR="."

CONFIG_DIR="${SCRIPT_DIR}/conf"
mkdir -p "${CONFIG_DIR}" 2>/dev/null
DEFAULT_CONFIG="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
TODAY="$(date '+%Y-%m-%d')"
# Указываем расширение .jsonl для соответствия формату
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}_${TODAY}-report.jsonl"

# Создаем папку для логов, если её нет
mkdir -p "${LOG_DIR}"

if [ -r "${LOG_TEMPLATE_FILE}" ]; then
  # shellcheck source=/dev/null
  . "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"
# Архивация в 7z (строки «7z | ...» в конфиге): программа и опции по умолчанию
# (-snl — хранить симлинки как ссылки, а не обходить их).
SEVENZIP_BIN="${SEVENZIP_BIN:-7z}"
SEVENZIP_DEFAULT_OPTS="${SEVENZIP_DEFAULT_OPTS:--t7z -mx=5 -snl}"
# Сколько последних архивов хранить, если поле в строке конфига пустое.
SEVENZIP_KEEP="${SEVENZIP_KEEP:-3}"

# Автоочистка старых логов (старше 10 дней)
find "${LOG_DIR}" -maxdepth 1 -type f \
  -name "${SCRIPT_BASE}_????-??-??-report.jsonl" -mtime +10 \
  -exec rm -f -- {} \; 2>/dev/null

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# Разделитель полей в --out-format rsync.
TAB="$(printf '\t')"

# Экранирование строки для JSON: \ и ", табуляция как \t, перевод строки как \n,
# прочие управляющие символы (в т.ч. CR) — \u00XX. Иначе табуляция или CR в
# имени файла дают невалидную строку JSONL.
json_escape() {
  # Замена циклом, а не gsub: в строке замены gsub обратная косая черта
  # трактуется по-разному в gawk и mawk.
  awk 'BEGIN { ORS = ""; for (i = 1; i < 32; i++) code[sprintf("%c", i)] = i }
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

trim() {
  sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

join_path() {
  case "$1" in
    */) printf '%s%s\n' "$1" "$2" ;;
     *) printf '%s/%s\n' "$1" "$2" ;;
  esac
}

extract_quoted_path() {
  sed -n 's/.*"\([^"]\{1,\}\)".*/\1/p' | sed -n '1p'
}

log_json() {
  _ts="$(ts)"
  _dry_run="$( [ "${DRY_RUN}" -eq 1 ] && echo true || echo false )"
  _rc="${9:-null}"
  _itemize="${10-}"
  _level_norm="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"

  _msg="$(printf '%s' "$5" | json_escape)"
  _detail="$(printf '%s' "$6" | json_escape)"
  _itemize_esc="$(printf '%s' "${_itemize}" | json_escape)"
  # Пути — данные с диска и из конфига, экранируются так же, как сообщения.
  _file="$(printf '%s' "$3" | json_escape)"
  _dst_file="$(printf '%s' "$4" | json_escape)"
  _src="$(printf '%s' "$7" | json_escape)"
  _dst="$(printf '%s' "$8" | json_escape)"

  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","file":"%s","dst_file":"%s","src":"%s","dst":"%s","dry_run":%s,"rc":%s,"itemize":"%s","msg":"%s","detail":"%s"}\n' \
    "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${_level_norm}" "${_msg}" "$2" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "$2" "${_file}" "${_dst_file}" "${_src}" "${_dst}" "${_dry_run}" "${_rc}" "${_itemize_esc}" "${_msg}" "${_detail}" >> "${LOG_FILE}"
}

# Список архивов каталога, которые нужно удалить при хранении N последних:
# $1 — каталог, $2 — шаблон имени (в кавычках), $3 — сколько оставить.
list_old_archives() {
  find "$1" -maxdepth 1 -type f -name "$2" -printf '%T@|%p\n' 2>/dev/null \
    | sort -nr \
    | awk -F'|' -v keep="$3" 'NR > keep { sub(/^[^|]*\|/, ""); print }'
}

# Архивация каталога в 7z. Строка конфига:
#   7z | КАТАЛОГ | КАТАЛОГ_АРХИВОВ | СКОЛЬКО_ХРАНИТЬ | ОПЦИИ_7Z
# Архив называется «<имя каталога>-ГГГГ-ММ-ДД.7z», пишется во временный
# «.part» и переименовывается только после успеха. СКОЛЬКО_ХРАНИТЬ — число
# последних архивов (0 — не удалять старые, пусто — SEVENZIP_KEEP, по
# умолчанию 3). Пустые ОПЦИИ_7Z — SEVENZIP_DEFAULT_OPTS. Код 7z 1 (файл
# изменился при чтении) — не ошибка.
run_archive() {
  A_SRC=$(printf '%s' "$1" | cut -d'|' -f2 | trim)
  A_DST=$(printf '%s' "$1" | cut -d'|' -f3 | trim)
  A_KEEP=$(printf '%s' "$1" | cut -d'|' -f4 | trim)
  A_OPTS=$(printf '%s' "$1" | cut -d'|' -f5- | trim)

  # Пустое поле — значение по умолчанию; не число — ошибка конфига.
  [ -z "${A_KEEP}" ] && A_KEEP="${SEVENZIP_KEEP}"
  case "${A_KEEP}" in
    ""|*[!0-9]*) A_KEEP="" ;;
    *) ;;
  esac
  if [ -z "${A_SRC}" ] || [ -z "${A_DST}" ] || [ -z "${A_KEEP}" ]; then
    log_json "error" "config_error" "" "" "invalid 7z config line" "$1" "" "" null ""
    FAIL_EXIT=2
    return 0
  fi
  if [ ! -d "${A_SRC}" ]; then
    log_json "error" "archive_error" "${A_SRC}" "" "7z source is not a directory" "" "${A_SRC}" "${A_DST}" null ""
    FAIL_EXIT=2
    return 0
  fi
  if ! command -v "${SEVENZIP_BIN}" >/dev/null 2>&1; then
    log_json "error" "archive_error" "${A_SRC}" "" "7z not found: ${SEVENZIP_BIN}" "" "${A_SRC}" "${A_DST}" null ""
    FAIL_EXIT=2
    return 0
  fi
  [ -z "${A_OPTS}" ] && A_OPTS="${SEVENZIP_DEFAULT_OPTS}"

  A_NAME=$(basename -- "${A_SRC%/}")
  [ -z "${A_NAME}" ] && A_NAME="root"
  A_FINAL="${A_DST%/}/${A_NAME}-${TODAY}.7z"
  A_PART="${A_FINAL}.part"
  A_PATTERN="${A_NAME}-????-??-??.7z"

  log_json "info" "task_start" "${A_SRC}" "${A_FINAL}" "7z task started" "" "${A_SRC}" "${A_DST}" null ""

  # В dry-run новый архив ещё не создан: чтобы план ротации совпал с боевым
  # запуском, из квоты вычитается он сам (если за сегодня его ещё нет).
  A_RETAIN="${A_KEEP}"
  if [ "${DRY_RUN}" -eq 1 ] && [ "${A_KEEP}" -gt 0 ] && [ ! -e "${A_FINAL}" ]; then
    A_RETAIN=$((A_KEEP - 1))
  fi

  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[dry-run] будет: ${SEVENZIP_BIN} a ${A_OPTS} ${A_FINAL} ${A_SRC}"
    log_json "info" "would_archive" "${A_SRC}" "${A_FINAL}" "file action: would_archive" "" "${A_SRC}" "${A_DST}" null ""
  else
    mkdir -p "${A_DST}"
    rm -f -- "${A_PART}"
    A_ERR="${SCRIPT_DIR}/.${SCRIPT_BASE}.7z.err.$$"
    ERR_TMP="${A_ERR}"
    # Опции содержат «!» и «*» — разбиваем без раскрытия шаблонов.
    set -f
    # shellcheck disable=SC2086
    set -- ${A_OPTS}
    set +f
    "${SEVENZIP_BIN}" a -bd -bso0 -bsp0 "$@" -- "${A_PART}" "${A_SRC}" 2>"${A_ERR}"
    A_RC=$?

    while IFS= read -r err; do
      [ -z "${err}" ] && continue
      lvl="error"; [ "${A_RC}" -le 1 ] && lvl="warn"
      log_json "${lvl}" "7z_stderr" "${A_SRC}" "" "7z stderr" "${err}" "${A_SRC}" "${A_DST}" "${A_RC}" ""
    done < "${A_ERR}"

    if [ "${A_RC}" -gt 1 ]; then
      rm -f -- "${A_PART}"
      log_json "error" "task_end" "${A_SRC}" "${A_FINAL}" "7z task failed" "" "${A_SRC}" "${A_DST}" "${A_RC}" ""
      FAIL_EXIT=2
      cleanup
      return 0
    fi
    mv -f -- "${A_PART}" "${A_FINAL}"
    log_json "info" "archived" "${A_SRC}" "${A_FINAL}" "file action: archived" "" "${A_SRC}" "${A_DST}" "${A_RC}" ""
  fi

  # Ротация: оставляем A_KEEP последних архивов этого каталога.
  if [ "${A_KEEP}" -gt 0 ]; then
    list_old_archives "${A_DST}" "${A_PATTERN}" "${A_RETAIN}" | while IFS= read -r old; do
      [ -z "${old}" ] && continue
      if [ "${DRY_RUN}" -eq 1 ]; then
        echo "[dry-run] будет: удалить старый архив ${old}"
        log_json "info" "would_delete" "" "${old}" "file action: would_delete" "" "${A_SRC}" "${A_DST}" null ""
      else
        rm -f -- "${old}"
        log_json "info" "deleted" "" "${old}" "file action: deleted" "" "${A_SRC}" "${A_DST}" null ""
      fi
    done
  fi

  log_json "info" "task_end" "${A_SRC}" "${A_FINAL}" "7z task completed successfully" "" "${A_SRC}" "${A_DST}" 0 ""
  cleanup
}

###############################################################################
# ARGS & CONFIG
###############################################################################
CONFIG_FILE=""
EVAL_MODE=0
DRY_RUN=0

while getopts "c:Enh" opt; do
  case "${opt}" in
    c) CONFIG_FILE="${OPTARG}" ;;
    E) EVAL_MODE=1 ;;
    n) DRY_RUN=1 ;;
    h) echo "Использование: ${SCRIPT_NAME} [-c конфиг] [-E] [-n] [-h]"; exit 0 ;;
    *) echo "Неизвестная опция: -${OPTARG}" >&2; exit 2 ;;
  esac
done

[ -z "${CONFIG_FILE}" ] && CONFIG_FILE="${DEFAULT_CONFIG}"

if [ ! -r "${CONFIG_FILE}" ]; then
  echo "Ошибка: конфигурационный файл не найден: ${CONFIG_FILE}" >&2
  exit 1
fi

###############################################################################
# MAIN
###############################################################################
OUT_TMP=""
ERR_TMP=""
OPTS_TMP=""
cleanup() {
  [ -n "${OUT_TMP}" ] && rm -f -- "${OUT_TMP}" 2>/dev/null
  [ -n "${ERR_TMP}" ] && rm -f -- "${ERR_TMP}" 2>/dev/null
  [ -n "${OPTS_TMP}" ] && rm -f -- "${OPTS_TMP}" 2>/dev/null
}
trap cleanup EXIT INT TERM

FAIL_EXIT=0

while IFS= read -r LINE || [ -n "${LINE}" ]; do
  case "${LINE}" in
    ""|\#*) continue ;;
    *) ;;
  esac

  SRC=$(printf '%s' "${LINE}" | cut -d'|' -f1 | trim)

  # Строка «7z | ...» — архивация в 7z, а не rsync.
  if [ "${SRC}" = "7z" ]; then
    run_archive "${LINE}"
    continue
  fi

  DST=$(printf '%s' "${LINE}" | cut -d'|' -f2 | trim)
  OPTS=$(printf '%s' "${LINE}" | cut -d'|' -f3- | trim)

  if [ -z "${SRC}" ] || [ -z "${DST}" ] || [ -z "${OPTS}" ]; then
    log_json "error" "config_error" "" "" "invalid config line" "${LINE}" "" "" null ""
    FAIL_EXIT=2
    continue
  fi

  # СОЗДАНИЕ ПАПКИ НАЗНАЧЕНИЯ
  # Если это не dry-run, создаем целевую папку перед запуском rsync
  if [ "${DRY_RUN}" -eq 0 ]; then
    mkdir -p "${DST}"
  fi

  log_json "info" "task_start" "${SRC}" "${DST}" "task started" "" "${SRC}" "${DST}" null ""

  if [ "${EVAL_MODE}" -eq 1 ]; then
    # Разбиваем $OPTS на позиционные параметры с учётом кавычек, но без eval:
    # xargs использует свой парсер кавычек/экранирования, а не shell, поэтому
    # $()/`` ``/;/&&/| внутри OPTS не выполняются как код, только как текст.
    # Читаем из файла, а не из пайпа — иначе POSIX sh (dash) запускает while
    # в сабшелле, и накопленные через "set --" параметры терялись бы снаружи.
    OPTS_TMP="$(mktemp)"
    printf '%s\n' "${OPTS}" | xargs -n1 -- printf '%s\n' >"${OPTS_TMP}"
    set --
    while IFS= read -r OPT; do
      set -- "$@" "${OPT}"
    done <"${OPTS_TMP}"
    rm -f -- "${OPTS_TMP}"
  else
    # shellcheck disable=SC2086
    set -- ${OPTS}
  fi

  [ "${DRY_RUN}" -eq 1 ] && set -- "$@" --dry-run

  OUT_TMP="${SCRIPT_DIR}/.${SCRIPT_BASE}.out.$$"
  ERR_TMP="${SCRIPT_DIR}/.${SCRIPT_BASE}.err.$$"
  : >"${OUT_TMP}"
  : >"${ERR_TMP}"

  rsync "$@" --itemize-changes --out-format="%i	%n" "${SRC}" "${DST}" >"${OUT_TMP}" 2>"${ERR_TMP}"
  RC=$?

  # Обработка вывода (события written/deleted)
  while IFS= read -r line; do
    [ -z "${line}" ] && continue
    case "${line}" in
      \*deleting*)
        # rsync дополняет «*deleting» пробелами до ширины %i, затем идёт
        # табуляция из --out-format: имя — всё после первой табуляции.
        name=${line#*"${TAB}"}
        case "${name}" in */) continue ;; *) ;; esac
        dstf="$(join_path "${DST}" "${name}")"
        ev="deleted"; [ "${DRY_RUN}" -eq 1 ] && ev="would_delete"
        log_json "info" "${ev}" "" "${dstf}" "file action: ${ev}" "" "${SRC}" "${DST}" null ""
        continue
        ;;
      *) ;;
    esac
    item=$(printf '%s' "${line}" | cut -f1)
    name=$(printf '%s' "${line}" | cut -f2-)
    case "${item}" in
      ">f"*)
        srcf="$(join_path "${SRC}" "${name}")"
        dstf="$(join_path "${DST}" "${name}")"
        ev="written"; [ "${DRY_RUN}" -eq 1 ] && ev="would_write"
        log_json "info" "${ev}" "${srcf}" "${dstf}" "file action: ${ev}" "" "${SRC}" "${DST}" null "${item}"
        ;;
      *) ;;
    esac
  done < "${OUT_TMP}"

  # Обработка ошибок rsync
  while IFS= read -r err; do
    [ -z "${err}" ] && continue
    f="$(printf '%s' "${err}" | extract_quoted_path)"
    [ -z "${f}" ] && f="${SRC}"
    lvl="error"; [ "${RC}" -eq 0 ] && lvl="warn"
    log_json "${lvl}" "rsync_stderr" "${f}" "" "rsync stderr" "${err}" "${SRC}" "${DST}" "${RC}" ""
  done < "${ERR_TMP}"

  if [ "${RC}" -ne 0 ]; then
    log_json "error" "task_end" "${SRC}" "${DST}" "task failed" "" "${SRC}" "${DST}" "${RC}" ""
    FAIL_EXIT=2
  else
    log_json "info" "task_end" "${SRC}" "${DST}" "task completed successfully" "" "${SRC}" "${DST}" "${RC}" ""
  fi

  cleanup
done < "${CONFIG_FILE}"

exit "${FAIL_EXIT}"
