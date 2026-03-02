#!/usr/bin/env bash
# Не используем set -e, чтобы скрипт не падал, если mountpoint вернет ошибку
set -uo pipefail

APP_NAME="MountCheck"
ICON_NAME="drive-harddisk"
URGENCY="critical"

FAILED_MOUNTS=()

# Читаем fstab построчно напрямую через bash
# Это исключит ошибки awk и правильно обработает пробелы
while read -r device mount_point type options dump pass; do
    # Пропускаем комментарии, пустые строки и виртуальные ФС
    [[ "$device" =~ ^(#|$) ]] && continue
    [[ "$mount_point" =~ ^/(proc|sys|dev|run|tmp|boot) ]] && continue
    [[ "$type" == "swap" ]] && continue
    [[ "$type" == "none" ]] && continue

    # Декодируем пробелы (\040 -> " ")
    MP_CLEAN=$(printf '%b' "${mount_point//\\/\\\\}")

    # Проверяем, смонтирована ли точка
    if ! mountpoint -q -- "$MP_CLEAN"; then
        FAILED_MOUNTS+=("$MP_CLEAN")
    fi
done < /etc/fstab

# Если есть несмонтированные диски, отправляем уведомление
if [ ${#FAILED_MOUNTS[@]} -gt 0 ]; then
    # Собираем строку через запятую
    FAILED_STR=$(IFS=', '; echo "${FAILED_MOUNTS[*]}")

    notify-send \
        -a "$APP_NAME" \
        -i "$ICON_NAME" \
        -u "$URGENCY" \
        "Ошибка монтирования" \
        "Не активны: $FAILED_STR"

    # Дублируем в консоль для отладки
    echo "Ошибка: не смонтированы $FAILED_STR"
else
    echo "Все диски на месте."
fi
