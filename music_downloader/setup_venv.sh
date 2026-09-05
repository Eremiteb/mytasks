#!/usr/bin/env bash

set -euo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/../logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/../conf/log_template.conf"
mkdir -p "$LOG_DIR"

if [[ -r "$LOG_TEMPLATE_FILE" ]]; then
	# shellcheck source=/dev/null
	source "$LOG_TEMPLATE_FILE"
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
	local level_norm msg_esc detail_esc
	level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"
	msg_esc="$(json_escape "$msg")"
	detail_esc="$(json_escape "$detail")"
	printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
		"$(ts)" "$LOG_SCHEMA_VERSION" "$LOG_COMPAT_TARGETS" "$level_norm" "$msg_esc" "$event" "$SCRIPT_BASE" "$SCRIPT_NAME" "$event" "$level_norm" "$msg_esc" "$detail_esc" >> "$LOG_FILE"
}

###############################################################################
# MAIN
###############################################################################
cd "$SCRIPT_DIR"

log_json "INFO" "start" "Инициализация виртуального окружения" "$SCRIPT_DIR"

python3 -m venv venv

# shellcheck source=/dev/null
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

log_json "INFO" "done" "Виртуальное окружение настроено и зависимости установлены"
echo "Виртуальное окружение настроено и библиотеки установлены."
