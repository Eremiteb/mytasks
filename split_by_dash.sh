#!/bin/sh

# Определение путей и имен
SCRIPT_PATH="$( cd "$( dirname "$0" )" && pwd )"
SCRIPT_NAME_BASE=$(basename "$0" .sh)
TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")

# Конфиг и лог именуются строго по имени скрипта
LOG_DIR="${SCRIPT_PATH}/logs"
CONFIG_DIR="${SCRIPT_PATH}/conf"
mkdir -p "$LOG_DIR"
mkdir -p "$CONFIG_DIR"
LOG_FILE="${LOG_DIR}/${SCRIPT_NAME_BASE}-${TIMESTAMP}.jsonl"
CONF_FILE="${CONFIG_DIR}/${SCRIPT_NAME_BASE}.conf"

log_json() {
  _level="$1"
  _msg="$2"
  _time=$(date +"%Y-%m-%dT%H:%M:%S%z")
  printf '{"timestamp": "%s", "level": "%s", "message": "%s"}\n' "$_time" "$_level" "$_msg" >> "$LOG_FILE"
}

cleanup_logs() {
  _old_logs=$(ls -1t "${LOG_DIR}/${SCRIPT_NAME_BASE}-"*.jsonl 2>/dev/null | tail -n +6)
  [ -n "$_old_logs" ] && rm $_old_logs
}

process_directory() {
  _current_dir="$1"
  if [ ! -d "$_current_dir" ]; then
    log_json "ERROR" "Каталог не существует: $_current_dir"
    return
  fi

  log_json "INFO" "Обработка директории: $_current_dir"
  
  find "$_current_dir" -maxdepth 1 -type f | while IFS= read -r file; do
    name=$(basename "$file")
    folder=$(printf '%s\n' "$name" | sed -n 's/^\(.*\)[[:space:]][-–—][[:space:]].*/\1/p' | sed 's/[[:space:]]*$//' | sed -n '1p')

    [ -z "$folder" ] && continue

    target_dir="$_current_dir/$folder"
    mkdir -p "$target_dir"
    
    dst="$target_dir/$name"
    if [ -e "$dst" ]; then
      i=1; while [ -e "$target_dir/$name.$i" ]; do i=$((i+1)); done
      dst="$target_dir/$name.$i"
    fi

    mv "$file" "$dst" && log_json "INFO" "Moved: $name -> $folder/"
  done
}

# --- Основная логика ---
# 1. Если передан аргумент — работаем с ним
# 2. Если нет — ищем конфиг рядом со скриптом
if [ -n "$1" ] && [ -d "$1" ]; then
  process_directory "$1"
elif [ -f "$CONF_FILE" ]; then
  log_json "INFO" "Использование конфига: $CONF_FILE"
  while IFS= read -r line || [ -n "$line" ]; do
    target=$(echo "$line" | sed 's/#.*//' | xargs)
    [ -n "$target" ] && process_directory "$target"
  done < "$CONF_FILE"
else
  _err="Ошибка: Каталог не указан и конфиг $CONF_FILE не найден."
  echo "$_err" >&2
  log_json "ERROR" "$_err"
  exit 1
fi

cleanup_logs
exit 0