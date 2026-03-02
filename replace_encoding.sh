#!/bin/bash

# Скрипт для замены строки на другую в текстовых файлах с прогресс-баром

usage() {
    cat << EOF
Использование: $0 [опции] [путь_к_каталогу] [исходная_строка] [новая_строка]

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
    case $opt in
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
        \?) echo "Ошибка: неизвестная опция -$OPTARG" >&2; exit 1 ;;
    esac
done
shift $((OPTIND - 1))

[ -n "$1" ] && [ -d "$1" ] && { TARGET_DIR="$1"; shift; }
[ -n "$1" ] && { FROM_STR="$1"; shift; }
[ -n "$1" ] && { TO_STR="$1"; shift; }

[ ! -d "$TARGET_DIR" ] && { echo "Ошибка: каталог '$TARGET_DIR' не существует." >&2; exit 1; }
[ -z "$FROM_STR" ] && { echo "Ошибка: исходная строка пустая." >&2; exit 1; }

echo "Каталог: $TARGET_DIR"
echo "Замена: \"$FROM_STR\" → \"$TO_STR\""
$RECURSIVE && echo "Режим: рекурсивный" || echo "Режим: только текущий каталог"
$DRY_RUN && echo "=== СУХОЙ ЗАПУСК ===" || echo "=== РЕАЛЬНАЯ ЗАМЕНА ==="
echo

# Формируем команду поиска
if $RECURSIVE; then
    SEARCH_CMD="grep -rlI -- \"$FROM_STR\" \"$TARGET_DIR\""
else
    SEARCH_CMD="grep -lI -- \"$FROM_STR\" \"$TARGET_DIR\"/* 2>/dev/null || true"
fi

# Сначала собираем все файлы в массив и считаем общее количество
mapfile -t files < <(eval "$SEARCH_CMD")
total=${#files[@]}

if (( total == 0 )); then
    echo "Файлы с строкой \"$FROM_STR\" не найдены."
    exit 0
fi

echo "Найдено файлов для обработки: $total"
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
    printf "] %3d%% (%d/%d)" "$percent" "$current" "$total"
}

# Экранирование для sed
SED_FROM=$(printf '%s\n' "$FROM_STR" | sed 's/[\/&]/\\&/g')
SED_TO=$(printf '%s\n' "$TO_STR" | sed 's/[\/&]/\\&/g')

# Обработка файлов с прогресс-баром
counter=0
for file in "${files[@]}"; do
    ((counter++))
    progress_bar "$counter" "$total"

    [ -f "$file" ] || continue

    if $DRY_RUN; then
        echo -e "\nНайдено в: $file"
        grep -n -- "$FROM_STR" "$file" | sed "s/$SED_FROM/[31m&[0m/g; s/$/  ← заменится на \"$TO_STR\"/"
    else
        echo -e "\nОбрабатываем: $file"
        if sed -i.bak "s/$SED_FROM/$SED_TO/g" "$file" 2>/dev/null; then
            rm -f "${file}.bak"
        else
            sed -i "s/$SED_FROM/$SED_TO/g" "$file"
        fi
    fi
done

echo -e "\n"
if $DRY_RUN; then
    echo "Сухой запуск завершён. Обработано файлов: $total (ничего не изменено)."
else
    echo "Замена завершена. Обработано файлов: $total."
fi
