#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}" .sh)"
CONFIG_DIR="${SCRIPT_DIR}/conf"
mkdir -p "$CONFIG_DIR"
CONF_FILE="${CONFIG_DIR}/${SCRIPT_NAME}.conf"

# Значения по умолчанию
IP_SERVICE_URL="https://icanhazip.com"
IP_FILE="/var/downloads/clouddata/nextcloud/work/ip.txt"
IP_HISTORY_FILE="/var/downloads/clouddata/nextcloud/work/ip_history.txt"

# Загрузка конфига
if [ -f "$CONF_FILE" ]; then
    # shellcheck source=/dev/null
    . "$CONF_FILE"
else
    echo "Внимание: конфиг не найден ($CONF_FILE), используются значения по умолчанию" >&2
fi

# Создаём папку для файлов, если её нет
mkdir -p "$(dirname "$IP_FILE")"
mkdir -p "$(dirname "$IP_HISTORY_FILE")"

# Получаем IP
IP=$(curl -s --max-time 10 "$IP_SERVICE_URL" | tr -d '[:space:]')

if [ -z "$IP" ]; then
    echo "Ошибка: не удалось получить IP от $IP_SERVICE_URL" >&2
    exit 1
fi

# Сохраняем текущий IP
printf '%s\n' "$IP" > "$IP_FILE"

# Добавляем в историю только при изменении
if ! grep -qF "$IP" "$IP_HISTORY_FILE" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $IP" >> "$IP_HISTORY_FILE"
fi
