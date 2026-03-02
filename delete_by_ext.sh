#!/usr/bin/env bash
set -u

###############################################################################
# CONFIG
###############################################################################
SCRIPT_NAME="$(basename "$0")"
TODAY="$(date '+%Y-%m-%d')"
LOG_FILE="./${SCRIPT_NAME%.sh}_${TODAY}.log"

DRY_RUN=0
TARGET_DIR=""
EXTENSION=""

###############################################################################
# HELP
###############################################################################
usage() {
cat <<EOF
Использование:
  $SCRIPT_NAME -d DIR -e EXT [--dry-run]

Опции:
  -d DIR       каталог для обработки
  -e EXT       расширение файлов (без точки), например: tmp, log, bak
  --dry-run    только показать, что будет удалено (без удаления)
  -h           справка

Примеры:
  $SCRIPT_NAME -d /tmp -e log --dry-run
  $SCRIPT_NAME -d ./downloads -e tmp
EOF
exit 1
}

###############################################################################
# ARGUMENTS
###############################################################################
while [ $# -gt 0 ]; do
  case "$1" in
    -d) TARGET_DIR="$2"; shift 2 ;;
    -e) EXTENSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "Неизвестный аргумент: $1"; usage ;;
  esac
done

[ -z "$TARGET_DIR" ] && usage
[ -z "$EXTENSION" ] && usage
[ ! -d "$TARGET_DIR" ] && { echo "Каталог не найден: $TARGET_DIR"; exit 2; }

###############################################################################
# INIT
###############################################################################
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "dir=$TARGET_DIR ext=.$EXTENSION dry_run=$DRY_RUN" >> "$LOG_FILE"

###############################################################################
# PROCESS
###############################################################################
find "$TARGET_DIR" -type f -name "*.${EXTENSION}" | while IFS= read -r file; do
  ts="$(date '+%Y-%m-%d %H:%M:%S')"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] $file"
    echo "$ts DRY_RUN $file" >> "$LOG_FILE"
  else
    if rm -f -- "$file"; then
      echo "[DELETED] $file"
      echo "$ts DELETED $file" >> "$LOG_FILE"
    else
      echo "[ERROR]   $file"
      echo "$ts ERROR $file" >> "$LOG_FILE"
    fi
  fi
done
