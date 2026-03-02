#!/bin/bash

SCRIPT_PATH="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR_NAME="music_downloader"
PROJECT_PATH="$SCRIPT_PATH/$PROJECT_DIR_NAME"
VENV_PATH="$PROJECT_PATH/venv"

SCRIPT_NAME_BASE=$(basename "$0" .sh)
TIMESTAMP=$(date +"%Y-%m-%d-%H-%M-%S")
LOG_DIR="${SCRIPT_PATH}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/${SCRIPT_NAME_BASE}-${TIMESTAMP}.jsonl"

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

log_json "INFO" "Запуск music_downloader"

if [ ! -d "$PROJECT_PATH" ]; then
    log_json "ERROR" "Проект не найден."
    cleanup_logs; exit 1
fi

cd "$PROJECT_PATH"
[ -f "$VENV_PATH/bin/activate" ] && source "$VENV_PATH/bin/activate"

# 1. Запуск Python
python3 music_downloader.py >> cron_execution.log 2>&1
PYTHON_EXIT_CODE=$?

[ -f "$VENV_PATH/bin/activate" ] && deactivate

# 2. Запуск сортировки при успехе
if [ $PYTHON_EXIT_CODE -eq 0 ]; then
    SPLIT_SCRIPT="$SCRIPT_PATH/split_by_dash.sh"
    if [ -f "$SPLIT_SCRIPT" ]; then
        log_json "INFO" "Запуск сортировщика (авто-поиск конфига)."
        /bin/sh "$SPLIT_SCRIPT" >> cron_execution.log 2>&1
    else
        log_json "ERROR" "Сортировщик не найден."
    fi
else
    log_json "ERROR" "Загрузчик завершился с ошибкой $PYTHON_EXIT_CODE"
fi

cleanup_logs
exit $PYTHON_EXIT_CODE