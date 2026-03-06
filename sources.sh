#!/bin/sh
# POSIX sh script

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
[ -z "$SCRIPT_DIR" ] && SCRIPT_DIR="."

CONFIG_DIR="${SCRIPT_DIR}/conf"
mkdir -p "$CONFIG_DIR" 2>/dev/null
DEFAULT_CONFIG="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
TODAY="$(date '+%Y-%m-%d')"
# Указываем расширение .jsonl для соответствия формату
LOG_DIR="${SCRIPT_DIR}/logs"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}_${TODAY}-report.jsonl"

# Создаем папку для логов, если её нет
mkdir -p "$LOG_DIR"

# Автоочистка старых логов (старше 10 дней)
find "$LOG_DIR" -maxdepth 1 -type f \
  -name "${SCRIPT_BASE}_????-??-??-report.jsonl" -mtime +10 \
  -exec rm -f -- {} \; 2>/dev/null

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
  sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
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
  _dry_run="$( [ "$DRY_RUN" -eq 1 ] && echo true || echo false )"
  _rc="${9:-null}"
  _itemize="${10-}"

  _msg="$(printf '%s' "$5" | json_escape)"
  _detail="$(printf '%s' "$6" | json_escape)"
  _itemize_esc="$(printf '%s' "$_itemize" | json_escape)"

  printf '{"ts":"%s","level":"%s","script":"%s","event":"%s","file":"%s","dst_file":"%s","src":"%s","dst":"%s","dry_run":%s,"rc":%s,"itemize":"%s","msg":"%s","detail":"%s"}\n' \
    "$_ts" "$1" "$SCRIPT_NAME" "$2" "$3" "$4" "$7" "$8" "$_dry_run" "$_rc" "$_itemize_esc" "$_msg" "$_detail" >> "$LOG_FILE"
}

###############################################################################
# ARGS & CONFIG
###############################################################################
CONFIG_FILE=""
EVAL_MODE=0
DRY_RUN=0

while getopts "c:Enh" opt; do
  case "$opt" in
    c) CONFIG_FILE="$OPTARG" ;;
    E) EVAL_MODE=1 ;;
    n) DRY_RUN=1 ;;
    h) echo "Использование: $SCRIPT_NAME [-c конфиг] [-E] [-n] [-h]"; exit 0 ;;
  esac
done

[ -z "$CONFIG_FILE" ] && CONFIG_FILE="$DEFAULT_CONFIG"

if [ ! -r "$CONFIG_FILE" ]; then
  echo "Ошибка: конфигурационный файл не найден: $CONFIG_FILE" >&2
  exit 1
fi

###############################################################################
# MAIN
###############################################################################
OUT_TMP=""
ERR_TMP=""
cleanup() {
  [ -n "$OUT_TMP" ] && rm -f -- "$OUT_TMP" 2>/dev/null
  [ -n "$ERR_TMP" ] && rm -f -- "$ERR_TMP" 2>/dev/null
}
trap cleanup EXIT INT TERM

FAIL_EXIT=0

while IFS= read -r LINE || [ -n "$LINE" ]; do
  case "$LINE" in
    ""|\#*) continue ;;
  esac

  SRC=$(printf '%s' "$LINE" | cut -d'|' -f1 | trim)
  DST=$(printf '%s' "$LINE" | cut -d'|' -f2 | trim)
  OPTS=$(printf '%s' "$LINE" | cut -d'|' -f3- | trim)

  if [ -z "$SRC" ] || [ -z "$DST" ] || [ -z "$OPTS" ]; then
    log_json "error" "config_error" "" "" "invalid config line" "$LINE" "" "" null ""
    FAIL_EXIT=2
    continue
  fi

  # СОЗДАНИЕ ПАПКИ НАЗНАЧЕНИЯ
  # Если это не dry-run, создаем целевую папку перед запуском rsync
  if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$DST"
  fi

  log_json "info" "task_start" "$SRC" "$DST" "task started" "" "$SRC" "$DST" null ""

  if [ "$EVAL_MODE" -eq 1 ]; then
    eval "set -- $OPTS"
  else
    set -- $OPTS
  fi

  [ "$DRY_RUN" -eq 1 ] && set -- "$@" --dry-run

  OUT_TMP="${SCRIPT_DIR}/.${SCRIPT_BASE}.out.$$"
  ERR_TMP="${SCRIPT_DIR}/.${SCRIPT_BASE}.err.$$"
  : >"$OUT_TMP"
  : >"$ERR_TMP"

  rsync "$@" --itemize-changes --out-format="%i	%n" "$SRC" "$DST" >"$OUT_TMP" 2>"$ERR_TMP"
  RC=$?

  # Обработка вывода (события written/deleted)
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in
      \*deleting\ *)
        name=${line#*deleting }
        case "$name" in */) continue ;; esac
        dstf="$(join_path "$DST" "$name")"
        ev="deleted"; [ "$DRY_RUN" -eq 1 ] && ev="would_delete"
        log_json "info" "$ev" "" "$dstf" "file action: $ev" "" "$SRC" "$DST" null ""
        continue
        ;;
    esac
    item=$(printf '%s' "$line" | cut -f1)
    name=$(printf '%s' "$line" | cut -f2-)
    case "$item" in
      ">f"*)
        srcf="$(join_path "$SRC" "$name")"
        dstf="$(join_path "$DST" "$name")"
        ev="written"; [ "$DRY_RUN" -eq 1 ] && ev="would_write"
        log_json "info" "$ev" "$srcf" "$dstf" "file action: $ev" "" "$SRC" "$DST" null "$item"
        ;;
    esac
  done < "$OUT_TMP"

  # Обработка ошибок rsync
  while IFS= read -r err; do
    [ -z "$err" ] && continue
    f="$(printf '%s' "$err" | extract_quoted_path)"
    [ -z "$f" ] && f="$SRC"
    lvl="error"; [ "$RC" -eq 0 ] && lvl="warn"
    log_json "$lvl" "rsync_stderr" "$f" "" "rsync stderr" "$err" "$SRC" "$DST" "$RC" ""
  done < "$ERR_TMP"

  if [ "$RC" -ne 0 ]; then
    log_json "error" "task_end" "$SRC" "$DST" "task failed" "" "$SRC" "$DST" "$RC" ""
    FAIL_EXIT=2
  else
    log_json "info" "task_end" "$SRC" "$DST" "task completed successfully" "" "$SRC" "$DST" "$RC" ""
  fi

  cleanup
done < "$CONFIG_FILE"

exit "$FAIL_EXIT"
