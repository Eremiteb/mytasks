#!/usr/bin/env bash
# Стартовая настройка и регулярная проверка mytasks на машине.
#
# Первый запуск на новой машине — из каталога, куда склонирован репозиторий:
#   0) обязательно обновляет систему штатным средством её семейства
#      (garuda-update/pacman, apt-get, dnf/yum/rpm-ostree, zypper,
#      transactional-update, apk, xbps-install, emerge, eopkg, nixos-rebuild);
#   1) прописывает себя в автозагрузку (user-юнит check-mytasks.service);
#   2) создаёт conf/check_mytasks.conf: каталог mytasks = каталог запуска,
#      затем проходит мастер остальных конфигов по conf/*.conf.example — окна
#      kdialog/zenity (или терминал) с описанием и значениями по умолчанию;
#   3) проверяет зависимости и ставит пакеты менеджером своего семейства ОС:
#      arch — pacman, debian — apt-get, redhat — dnf;
#   4) создаёт юниты systemd, задания cron, ссылки и автозапуск из шаблонов
#      каталога systemd/ с подстановкой @MYTASKS_DIR@.
# Для каждого отсутствующего или неработающего задания выводится
# предупреждение с описанием и вопрос «создать?»: «да» — создаётся, «нет» —
# ничего не меняется. Таблица состояния выводится всегда (только таблица —
# --status).
#
# Коды возврата: 0 — всё на месте, 1 — есть незапущенные или не прописанные
# задания, конфиги или обязательные зависимости, 2 — ошибка аргументов.
set -uo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)
SCRIPT_PATH="${SCRIPT_DIR}/${SCRIPT_NAME}"
CONF_DIR="${SCRIPT_DIR}/conf"
CONF_REL="conf/${SCRIPT_BASE}.conf"
CONF_FILE="${SCRIPT_DIR}/${CONF_REL}"
TEMPLATE_DIR="${SCRIPT_DIR}/systemd"
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${CONF_DIR}/log_template.conf"
SELF_UNIT="${SCRIPT_BASE//_/-}.service"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

# Значения по умолчанию; переопределяются в conf/check_mytasks.conf
MYTASKS_DIR=""
OS_FAMILY=""
OS_RELEASE_FILE="/etc/os-release"
OSTREE_FLAG="/run/ostree-booted"
SYSTEMD_RUN_DIR="/run/systemd/system"
# Признаки «нужна перезагрузка» после обновления: флаг Debian/Ubuntu и
# отсутствие модулей работающего ядра (ядро обновлено, старое удалено)
REBOOT_FLAG="/run/reboot-required"
# Garuda: обновлять через garuda-update (auto) или чистым pacman -Syu (no)
GARUDA_UPDATE="auto"
KERNEL_MODULES_DIR="/usr/lib/modules"
SUDO_CMD="auto"
UI="auto"
SYSTEM_UNIT_DIR="/etc/systemd/system"
USER_UNIT_DIR=""
AUTOSTART_DIR=""
BIN_DIR=""
# Системный Python для проверки модулей (py:…); пусто — /usr/bin/python3, а
# где его нет (NixOS) — python3 из PATH
SYSTEM_PYTHON=""
KEEP_LOGS=10
APP_NAME="MytasksCheck"
ICON_NAME="dialog-warning"
URGENCY="normal"

# Задания: «вид|имя|параметр|условие|описание».
#   вид: user-unit / system-unit — юнит из шаблонов systemd/user/ или systemd/
#        (ставятся все файлы с тем же базовым именем: .service и .timer);
#        user-ext / system-ext — юнит, который ставится вне mytasks (только
#        проверка и включение); cron — строка crontab пользователя (параметр —
#        расписание); link — ссылка в BIN_DIR (параметр — цель в mytasks);
#        autostart — файл из systemd/autostart/ в AUTOSTART_DIR; conf — конфиг
#        (параметр — пример); venv — окружение Python (параметр — модули).
#   условие: пусто — всегда; path:ПУТЬ, cmd:КОМАНДА, fs:ТОЧКА=ТИП,
#        family:СЕМЕЙСТВО через запятую; при невыполненном — «не требуется».
#   имя @CRON_UNIT@ — демон cron своего семейства ОС (cronie/cron/crond).
JOBS=(
    "system-unit|system-monitor.timer|||Опрос SMART дисков, CPU и GPU каждые 6 ч (00, 06, 12, 18 ч; пропущенный запуск догоняется после загрузки), запись в SQLite и HTML-отчёт system_reports/latest.html. Скрипт system_monitor.sh, запуск от root."
    "user-unit|check-backups.timer|||Ежедневная проверка каталогов с архивами резервных копий (наличие, возраст, размер, целостность) с уведомлением в трее. Скрипт check_backups.sh, настройки — conf/check_backups.conf."
    "user-unit|check-store-mount.service|||При входе в графический сеанс проверяет, смонтированы ли точки из /etc/fstab, и показывает уведомление о несмонтированных. Скрипт check-store-mount.sh."
    "system-ext|@CRON_UNIT@||cmd:crontab|Демон cron: запускает задания из crontab пользователя (скачивание музыки, синхронизация каталогов, мониторинг Btrfs)."
    "cron|music_downloader.sh|0 21 * * *||Ежедневно в 21:00: скачивание новой музыки с сайтов из music_downloader/music_downloader.json и раскладка по папкам исполнителей (split_by_dash.sh)."
    "cron|sources.sh|0 23 * * *||Ежедневно в 23:00: синхронизация каталогов через rsync по списку conf/sources.conf."
    "cron|btrfs_monitor.sh|0 */6 * * *|fs:/=btrfs|Каждые 6 ч: проверка счётчиков ошибок Btrfs корневого раздела, уведомление при их росте."
    "conf|conf/check_backups.conf|conf/check_backups.conf.example||Настройки check_backups.sh: каталоги с архивами (BACKUP_DIRS), допустимый возраст и размер архивов."
    "conf|conf/sources.conf|conf/sources.conf.example||Список каталогов для sources.sh: строки «источник, назначение, опции rsync» через вертикальную черту."
    "conf|music_downloader/music_downloader.json|music_downloader/music_downloader.json.example||Настройки загрузчика музыки: сайты, общий список каталогов download_paths, лимит размера."
    "venv|music_downloader/venv|requests,bs4,cloudscraper||Виртуальное окружение Python загрузчика музыки: music_downloader/setup_venv.sh ставит зависимости из requirements.txt."
    "link|llamactl|llamactl.sh|path:/opt/llama.cpp|Команда llamactl в ~/.local/bin: управление локальным llama.cpp (модели, VRAM, сервисы, пресеты, обновление)."
    "link|llamactl-tray|llamactl_tray.py|path:/opt/llama.cpp|Команда llamactl-tray в ~/.local/bin: значок llama.cpp в трее."
    "autostart|llamactl-tray.desktop||path:/opt/llama.cpp|Автозапуск значка llama.cpp в трее при входе в графический сеанс."
    "system-ext|llama-server.service||path:/opt/llama.cpp|Роутер llama.cpp на 127.0.0.1:8080: веб-интерфейс и OpenAI-совместимый API. Юнит ставится вне mytasks."
    "system-ext|llama-fim.service||path:/opt/llama.cpp|FIM-автодополнение llama.cpp на 127.0.0.1:8012 для VS Code и Zed. Юнит ставится вне mytasks."
)

# Зависимости: «команда|пакеты|условие|описание».
#   команда: имя в PATH, несколько вариантов через запятую (достаточно одного),
#        py:МОДУЛЬ — модуль системного Python (SYSTEM_PYTHON) или gnu:КОМАНДА —
#        GNU-версия утилиты (не busybox);
#   пакеты: имя пакета по умолчанию и отличия через «;» в виде
#        семейство=пакет (arch, debian, redhat, suse, alpine, void, gentoo,
#        solus), «-» — нет в репозиториях, ставить вручную;
#   условие: как у заданий, плюс opt — необязательная зависимость (не
#        считается проблемой, с --yes не ставится, но предлагается).
DEPS=(
    "python3|python3;arch=python;gentoo=dev-lang/python||Python 3: music_downloader, mht_to_fb2.py, llamactl status (llamactl_status.py) и трей"
    "py:ensurepip|python3;arch=python;debian=python3-venv;gentoo=dev-lang/python||Модуль venv/ensurepip: виртуальное окружение music_downloader"
    "crontab|cronie;debian=cron;solus=-||cron: задания music_downloader.sh, sources.sh, btrfs_monitor.sh"
    "notify-send|libnotify;debian=libnotify-bin;suse=libnotify-tools||Уведомления в трее: check_backups, check-store-mount, system_monitor, btrfs_monitor, этот скрипт"
    "gawk|gawk||split_by_dash.sh: имена файлов и папок, допустимые в Windows"
    "jq|jq||system_monitor.sh, llamactl.sh"
    "sqlite3|sqlite3;arch=sqlite;redhat=sqlite;alpine=sqlite;void=sqlite;gentoo=dev-db/sqlite||system_monitor.sh: база истории опросов"
    "smartctl|smartmontools||system_monitor.sh: SMART-атрибуты дисков"
    "curl|curl||getip.sh, llamactl.sh"
    "rsync|rsync||sources.sh: синхронизация каталогов"
    "zstd|zstd||check_backups.sh (проверка .tar.zst), cloud_backup.sh"
    "unzip|unzip||unpack-fb2zip.sh, check_backups.sh (проверка .zip)"
    "file|file||cp1251_to_utf8.sh: определение кодировки файлов"
    "gnu:find|findutils||GNU find (-printf): ротация логов во всех скриптах; в busybox (Alpine) его нет"
    "gnu:stat|coreutils||GNU coreutils (stat -c, date -d, sort -V, readlink -f, install -t); в busybox (Alpine) их нет"
    "btrfs|btrfs-progs|fs:/=btrfs|btrfs_monitor.sh: счётчики ошибок Btrfs"
    "zenity,kdialog|zenity|opt|Окна вопросов этого скрипта (кнопка «Стоп» во всех окнах — только у zenity) и графический askpass для sudo"
    "sensors|lm_sensors;debian=lm-sensors;suse=sensors;alpine=lm-sensors;gentoo=sys-apps/lm-sensors|opt|system_monitor.sh: температура CPU"
    "ffmpeg|ffmpeg;redhat=ffmpeg-free|opt|all_to_mp3.sh, music_downloader (YouTube Music): перекодирование в MP3"
    "yt-dlp|yt-dlp;gentoo=net-misc/yt-dlp|opt|music_downloader: последний источник MP3 — песни YouTube Music"
    "pandoc|pandoc;arch=pandoc-cli;alpine=pandoc-cli;gentoo=app-text/pandoc-bin|opt|doc2fb2.sh, mht_to_fb2.py: преобразование документов"
    "sshpass|sshpass|opt|cloud_backup.sh: вход на сервер по паролю"
    "wg-quick|wireguard-tools|opt|cloud_backup.sh: туннель WireGuard"
    "py:PyQt6|python3-pyqt6;arch=python-pyqt6;suse=python3-PyQt6;alpine=py3-pyqt6;void=python3-PyQt6;gentoo=dev-python/pyqt6|path:/opt/llama.cpp|llamactl_tray.py: значок в трее (PyQt6 в системном Python)"
    "shellcheck|shellcheck;redhat=ShellCheck;suse=ShellCheck;gentoo=dev-util/shellcheck-bin|opt|Разработка: проверка shell-скриптов"
    "bats|bats|opt|Разработка: тесты tests/*.bats"
)

# Примеры, по которым при первом запуске проходит мастер конфигов
# (пусто — все conf/*.conf.example и music_downloader/*.json.example)
CONF_EXAMPLES=()
# Конфиги, которые мастер не предлагает: cloud_backup_qnap.sh работает на
# самом QNAP (Entware), а не на этой машине
CONF_WIZARD_SKIP=("conf/cloud_backup_qnap.conf")

CONF_PRESENT=0
if [[ -r "${CONF_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${CONF_FILE}"
    CONF_PRESENT=1
fi

DRY_RUN=0
STATUS_ONLY=0
NO_CREATE=0
ASSUME_YES=0
FORCE_ASK=0
NOTIFY=0
CONFIGURE=0
USE_GUI=1

UI_MODE="none"
SUDO=()
SUDO_OK=1
OS_PRETTY=""
OS_ID=""
OS_LIKE=""
OS_NOTE=""
PKG_MGR=""
UPDATE_MGR=""
UPDATE_MODE=""
UPDATE_DESC=""
UPDATE_OK=1
REBOOT_NEEDED=0
CRON_UNIT=""
FIRST_RUN=0
CHANGED=0
SKIPPED_CONFS=""
HANDLED=""
STOP=0
STOP_RC=10
STOP_HINT="«Отмена» — только это окно; «Стоп» — прервать весь процесс (дальше ничего не спрашивается и не меняется)."

# Состояние строк таблицы (индекс = номер задания в ALL_JOBS)
ALL_JOBS=()
R_KIND=()
R_NAME=()
R_INST=()
R_ENABLED=()
R_STATE=()
R_RESULT=()
R_PROBLEM=()
R_FIX=()
R_ACTION=()
R_DESC=()

# Разбор примера конфига (заполняется parse_example)
CE_NAME=()
CE_KIND=()
CE_VAL=()
CE_DESC=()
CE_QUOTE=()
CE_NEW=()
CE_HEADER=""

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g' | tr '\n' ' '
}

log_json() {
    local level="$1"
    local event="$2"
    local msg="$3"
    local detail="${4:-}"
    local rc="${5:-null}"
    local msg_esc detail_esc level_norm timestamp

    [[ "${DRY_RUN}" -eq 1 && "${event}" != "start" && "${event}" != "done" ]] && event="dry_${event}"
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    timestamp="$(ts)"

    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
        "${timestamp}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" "${rc}" >> "${LOG_FILE}"
}

cleanup_logs() {
    local old_logs=()
    local old_log old_logs_file

    old_logs_file="$(mktemp)"
    if find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
        | sort -nr \
        | awk -F'|' -v keep="${KEEP_LOGS}" 'NR > keep { print $2 }' > "${old_logs_file}"; then
        while IFS= read -r old_log; do
            [[ -n "${old_log}" ]] && old_logs+=("${old_log}")
        done < "${old_logs_file}"
    fi
    rm -f -- "${old_logs_file}"

    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

usage() {
    cat <<EOF
Использование: ${SCRIPT_NAME} [опции]

Стартовая настройка mytasks на машине и проверка его сервисов и заданий.
Без опций: таблица состояния, обязательное полное обновление системы
штатным средством её семейства, затем для каждого отсутствующего задания —
предупреждение с описанием и вопрос «создать?», в конце итоговая таблица.

  -s, --status      только таблица состояния (задания и все зависимости)
  -n, --dry-run     план без изменений: «[dry-run] будет: …»
  -y, --yes         создавать всё отсутствующее без вопросов (конфиги — со
                    значениями по умолчанию, только обязательные пакеты)
      --no-create   без вопросов и изменений: таблица и предупреждения
      --notify      уведомление в трее, если остались проблемы
  -c, --configure   мастер для всех отсутствующих конфигов (как при первом
                    запуске)
      --ask         задавать вопросы в терминале, даже если stdin не терминал
      --no-gui      не использовать окна kdialog/zenity
  -h, --help        эта справка

Коды возврата: 0 — всё на месте, 1 — есть проблемы, 2 — ошибка аргументов.
EOF
}

# Подстановка плейсхолдеров шаблонов и примеров конфигов
subst() {
    local text="$1"
    text="${text//@MYTASKS_DIR@/${MYTASKS_DIR}}"
    text="${text//@HOME@/${HOME}}"
    printf '%s' "${text}"
}

# Отрисовка шаблона в файл с подстановкой плейсхолдеров
render_file() {
    local src="$1"
    local dst="$2"
    local content rendered
    content="$(<"${src}")"
    rendered="$(subst "${content}")"
    printf '%s\n' "${rendered}" > "${dst}"
}

# Совпадение содержимого двух файлов (без cmp: diffutils есть не везде)
same_file() {
    local a b
    [[ -f "$1" && -f "$2" ]] || return 1
    a="$(<"$1")"
    b="$(<"$2")"
    [[ "${a}" == "${b}" ]]
}

say() { printf '%s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*"; }

# Экранирование текста для разметки Pango в zenity
zenity_escape() {
    local text="$1"
    text="${text//&/&amp;}"
    text="${text//</&lt;}"
    text="${text//>/&gt;}"
    printf '%s' "${text}"
}

# Вопрос «да/нет/стоп»: 0 — да, 1 — нет (закрытие окна — тоже «нет»),
# STOP_RC — «Стоп» (прервать весь процесс). Enter в терминале — да.
ui_yesno() {
    local title="$1"
    local text="$2"
    local answer etext rc

    case "${UI_MODE}" in
        zenity)
            etext="$(zenity_escape "${text}"$'\n\n'"${STOP_HINT}")"
            answer="$(zenity --question --title="${title}" --text="${etext}" --width=680 \
                --ok-label="Да" --cancel-label="Нет" --extra-button="Стоп" 2>/dev/null)"
            rc=$?
            [[ "${answer}" == "Стоп" ]] && return "${STOP_RC}"
            [[ "${rc}" -eq 0 ]] && return 0
            return 1
            ;;
        kdialog)
            kdialog --title "${title}" --yes-label "Да" --no-label "Нет" --cancel-label "Стоп" \
                --yesnocancel "${text}"$'\n\n'"${STOP_HINT}" 2>/dev/null
            rc=$?
            case "${rc}" in
                0|1) return "${rc}" ;;
                *) return "${STOP_RC}" ;;
            esac
            ;;
        tty)
            printf '\n=== %s ===\n%s\nДа/нет/стоп [Д/н/с]: ' "${title}" "${text}" >&2
            IFS= read -r answer || answer="н"
            case "${answer,,}" in
                ""|д|да|y|yes) return 0 ;;
                с|стоп|s|stop) return "${STOP_RC}" ;;
                *) return 1 ;;
            esac
            ;;
        *)
            return 1
            ;;
    esac
}

# Ввод строки: печатает значение. «Отмена» (или пустой ввод в терминале) —
# значение по умолчанию, «Стоп» — код STOP_RC.
ui_input() {
    local title="$1"
    local text="$2"
    local default="$3"
    local answer etext rc

    case "${UI_MODE}" in
        zenity)
            etext="$(zenity_escape "${text}"$'\n\n'"${STOP_HINT}")"
            answer="$(zenity --entry --title="${title}" --text="${etext}" --entry-text="${default}" --width=680 \
                --cancel-label="Отмена" --extra-button="Стоп" 2>/dev/null)"
            rc=$?
            if [[ "${rc}" -ne 0 ]]; then
                [[ "${answer}" == "Стоп" ]] && return "${STOP_RC}"
                answer="${default}"
            fi
            ;;
        kdialog)
            # У поля ввода kdialog нет третьей кнопки: «Отмена» — значение по умолчанию
            answer="$(kdialog --title "${title}" --inputbox "${text}" "${default}" 2>/dev/null)" || answer="${default}"
            ;;
        tty)
            printf '\n=== %s ===\n%s\n(Enter — по умолчанию, «стоп» — прервать всё) [%s]: ' "${title}" "${text}" "${default}" >&2
            IFS= read -r answer || answer=""
            case "${answer,,}" in
                стоп|stop) return "${STOP_RC}" ;;
                *) ;;
            esac
            [[ -n "${answer}" ]] || answer="${default}"
            ;;
        *)
            answer="${default}"
            ;;
    esac
    printf '%s' "${answer}"
}

# Многострочный ввод (элементы массива, текст конфига): «Отмена» — значение
# по умолчанию, «Стоп» — код STOP_RC
ui_text() {
    local title="$1"
    local text="$2"
    local default="$3"
    local answer etext tmp line rc

    case "${UI_MODE}" in
        zenity)
            etext="$(zenity_escape "${text}")"
            tmp="$(mktemp)"
            printf '%s\n' "${default}" > "${tmp}"
            answer="$(zenity --text-info --editable --title="${title}" --filename="${tmp}" --width=820 --height=520 \
                --cancel-label="Отмена" --extra-button="Стоп" 2>/dev/null)"
            rc=$?
            rm -f -- "${tmp}"
            if [[ "${rc}" -ne 0 ]]; then
                [[ "${answer}" == "Стоп" ]] && return "${STOP_RC}"
                answer="${default}"
            fi
            ;;
        kdialog)
            answer="$(kdialog --title "${title}" --textinputbox "${text}" "${default}" 2>/dev/null)" || answer="${default}"
            ;;
        tty)
            printf '\n=== %s ===\n%s\n--- по умолчанию ---\n%s\n--------------------\n' "${title}" "${text}" "${default}" >&2
            printf 'Enter — принять, «и» — ввести заново (строка «.» завершает ввод), «стоп» — прервать всё: ' >&2
            IFS= read -r line || line=""
            case "${line,,}" in
                с|стоп|s|stop) return "${STOP_RC}" ;;
                *) ;;
            esac
            if [[ "${line,,}" == "и" || "${line,,}" == "e" ]]; then
                answer=""
                while IFS= read -r line && [[ "${line}" != "." ]]; do
                    answer+="${line}"$'\n'
                done
                answer="${answer%$'\n'}"
            else
                answer="${default}"
            fi
            ;;
        *)
            answer="${default}"
            ;;
    esac
    printf '%s' "${answer}"
}

# Выбор варианта: печатает номер (с 0), по умолчанию — первый. «Отмена» —
# код 1 (только это окно), «Стоп» — код STOP_RC (весь процесс).
ui_choice() {
    local title="$1"
    local text="$2"
    shift 2
    local options=("$@")
    local args=() i answer etext state rc

    case "${UI_MODE}" in
        zenity)
            for i in "${!options[@]}"; do
                state="FALSE"
                [[ "${i}" -eq 0 ]] && state="TRUE"
                args+=("${state}" "${i}" "${options[${i}]}")
            done
            etext="$(zenity_escape "${text}"$'\n\n'"${STOP_HINT}")"
            answer="$(zenity --list --radiolist --title="${title}" --text="${etext}" --width=760 --height=560 \
                --cancel-label="Отмена" --extra-button="Стоп" \
                --column="" --column="№" --column="Вариант" --hide-column=2 --print-column=2 "${args[@]}" 2>/dev/null)"
            rc=$?
            if [[ "${rc}" -ne 0 ]]; then
                [[ "${answer}" == "Стоп" ]] && return "${STOP_RC}"
                return 1
            fi
            ;;
        kdialog)
            # У списка kdialog нет третьей кнопки: «Стоп» — отдельный пункт списка
            for i in "${!options[@]}"; do
                state="off"
                [[ "${i}" -eq 0 ]] && state="on"
                args+=("${i}" "${options[${i}]}" "${state}")
            done
            args+=("stop" "■ Стоп — прервать всю настройку" "off")
            answer="$(kdialog --title "${title}" --radiolist "${text}" "${args[@]}" 2>/dev/null)" || return 1
            [[ "${answer}" == "stop" ]] && return "${STOP_RC}"
            ;;
        tty)
            printf '\n=== %s ===\n%s\n' "${title}" "${text}" >&2
            for i in "${!options[@]}"; do
                printf '  %d) %s\n' "$((i + 1))" "${options[${i}]}" >&2
            done
            printf 'Номер [1], «стоп» — прервать всё: ' >&2
            IFS= read -r answer || answer=""
            case "${answer,,}" in
                с|стоп|s|stop) return "${STOP_RC}" ;;
                *) ;;
            esac
            if [[ "${answer}" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= ${#options[@]} )); then
                answer="$((answer - 1))"
            else
                answer=""
            fi
            ;;
        *)
            answer=""
            ;;
    esac
    [[ -n "${answer}" ]] || answer=0
    printf '%s' "${answer}"
}

# «Стоп»: прервать весь запуск — дальше ничего не спрашивать и не менять
request_stop() {
    [[ "${STOP}" -eq 1 ]] && return 0
    STOP=1
    say ""
    say "■ Остановлено кнопкой «Стоп»: весь процесс прерван, дальше ничего не спрашивается и не меняется."
    log_json "WARN" "stopped" "Остановлено пользователем (кнопка «Стоп»)" ""
}

select_ui() {
    UI_MODE="none"
    [[ "${NO_CREATE}" -eq 1 || "${STATUS_ONLY}" -eq 1 || "${UI}" == "none" ]] && return 0

    # zenity предпочтительнее: кнопка «Стоп» есть во всех его окнах; у kdialog —
    # только в вопросах «да/нет» (в списке это отдельный пункт, в полях её нет)
    if [[ "${USE_GUI}" -eq 1 && "${UI}" != "tty" && ( -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ) ]]; then
        if [[ "${UI}" == "kdialog" ]] && command -v kdialog >/dev/null 2>&1; then
            UI_MODE="kdialog"
        elif command -v zenity >/dev/null 2>&1; then
            UI_MODE="zenity"
        elif command -v kdialog >/dev/null 2>&1; then
            UI_MODE="kdialog"
        fi
    fi
    if [[ "${UI_MODE}" == "none" ]] && [[ -t 0 || "${FORCE_ASK}" -eq 1 ]]; then
        UI_MODE="tty"
    fi
    return 0
}

select_sudo() {
    SUDO=()
    SUDO_OK=1
    if [[ "${SUDO_CMD}" != "auto" ]]; then
        read -r -a SUDO <<<"${SUDO_CMD}"
        return 0
    fi
    if [[ "${EUID}" -eq 0 ]]; then
        return 0
    fi
    if [[ -n "${SUDO_ASKPASS:-}" && -x "${SUDO_ASKPASS}" ]] && command -v sudo >/dev/null 2>&1; then
        SUDO=(sudo -A)
    elif [[ "${UI_MODE}" == "kdialog" || "${UI_MODE}" == "zenity" ]] && command -v pkexec >/dev/null 2>&1; then
        SUDO=(pkexec)
    elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1; then
        SUDO=(sudo)
    else
        SUDO_OK=0
    fi
    return 0
}

# Семейство ОС по /etc/os-release (ID, затем ID_LIKE): менеджер пакетов,
# юнит cron и наличие systemd. Нераспознанная ОС — unknown с предупреждением.
detect_family() {
    local key val id="" like="" variant="" word words=()

    if [[ -r "${OS_RELEASE_FILE}" ]]; then
        while IFS='=' read -r key val; do
            val="${val%\"}"
            val="${val#\"}"
            case "${key}" in
                ID) id="${val,,}" ;;
                ID_LIKE) like="${val,,}" ;;
                VARIANT_ID) variant="${val,,}" ;;
                PRETTY_NAME) OS_PRETTY="${val}" ;;
                *) ;;
            esac
        done < "${OS_RELEASE_FILE}"
    fi
    OS_ID="${id:-?}"
    OS_LIKE="${like}"
    [[ -n "${OS_PRETTY}" ]] || OS_PRETTY="${id:-неизвестная ОС}"

    case "${OS_FAMILY}" in
        arch|debian|redhat|suse|alpine|void|gentoo|solus|nixos) ;;
        "")
            OS_FAMILY="unknown"
            read -r -a words <<<"${id} ${like}"
            for word in "${words[@]}"; do
                case "${word}" in
                    arch|archarm|manjaro|manjaro-arm|endeavouros|garuda|cachyos|artix|arcolinux|archcraft|rebornos|blendos|steamos|parabola|hyperbola|xerolinux|mabox|biglinux|crystal)
                        OS_FAMILY="arch" ;;
                    debian|ubuntu|linuxmint|lmde|pop|raspbian|elementary|kali|neon|zorin|mx|antix|deepin|devuan|pureos|parrot|tuxedo|peppermint|bodhi|sparky|q4os|siduction|armbian|trisquel|kubuntu|xubuntu|lubuntu)
                        OS_FAMILY="debian" ;;
                    rhel|fedora|centos|rocky|almalinux|ol|amzn|nobara|ultramarine|eurolinux|navy|circle|cloudlinux|scientific|mageia|openmandriva|bazzite|aurora|bluefin)
                        OS_FAMILY="redhat" ;;
                    opensuse|opensuse-leap|opensuse-tumbleweed|opensuse-slowroll|opensuse-microos|sles|sled|sle-micro|suse)
                        OS_FAMILY="suse" ;;
                    alpine|postmarketos) OS_FAMILY="alpine" ;;
                    void) OS_FAMILY="void" ;;
                    gentoo|funtoo|calculate) OS_FAMILY="gentoo" ;;
                    solus) OS_FAMILY="solus" ;;
                    nixos) OS_FAMILY="nixos" ;;
                    *) continue ;;
                esac
                break
            done
            ;;
        *)
            OS_NOTE="OS_FAMILY=«${OS_FAMILY}» в ${CONF_REL} не поддерживается"
            OS_FAMILY="unknown"
            ;;
    esac

    CRON_UNIT=""
    PKG_MGR=""
    case "${OS_FAMILY}" in
        arch)
            PKG_MGR="pacman"; CRON_UNIT="cronie.service"
            if [[ "${GARUDA_UPDATE}" != "no" ]] && command -v garuda-update >/dev/null 2>&1; then
                UPDATE_MODE="garuda"; UPDATE_DESC="garuda-update --noconfirm (зеркала, keyring, hotfix Garuda, pacman -Syu)"
            else
                UPDATE_DESC="pacman -Syu --noconfirm"
            fi
            ;;
        debian) PKG_MGR="apt-get"; CRON_UNIT="cron.service"; UPDATE_DESC="apt-get update && apt-get -y full-upgrade" ;;
        redhat)
            CRON_UNIT="crond.service"
            if [[ -e "${OSTREE_FLAG}" ]]; then
                # Fedora Atomic (Silverblue, Kinoite, Bazzite…): пакеты — слоями rpm-ostree
                PKG_MGR="rpm-ostree"; UPDATE_DESC="rpm-ostree upgrade (новая версия — после перезагрузки)"
                OS_NOTE="неизменяемая система (${variant:-ostree}): пакеты ставятся слоями rpm-ostree и появятся после перезагрузки"
            elif command -v dnf >/dev/null 2>&1; then
                PKG_MGR="dnf"; UPDATE_DESC="dnf -y upgrade --refresh"
            else
                PKG_MGR="yum"; UPDATE_DESC="yum -y update"
            fi
            ;;
        suse)
            PKG_MGR="zypper"; CRON_UNIT="cron.service"
            if [[ "${id}" == *microos* || "${id}" == sle-micro* ]]; then
                PKG_MGR="transactional-update"; UPDATE_DESC="transactional-update --non-interactive dup (после перезагрузки)"
                OS_NOTE="транзакционная система: обновление и пакеты применяются после перезагрузки"
            elif [[ "${id}" == *tumbleweed* || "${id}" == *slowroll* ]]; then
                UPDATE_MODE="dup"; UPDATE_DESC="zypper refresh && zypper dup (Tumbleweed обновляется только dup)"
            else
                UPDATE_MODE="update"; UPDATE_DESC="zypper refresh && zypper update"
            fi
            ;;
        alpine) PKG_MGR="apk"; UPDATE_DESC="apk update && apk upgrade" ;;
        void) PKG_MGR="xbps-install"; UPDATE_DESC="xbps-install -Syu xbps && xbps-install -Syu" ;;
        gentoo)
            PKG_MGR="emerge"; CRON_UNIT="cronie.service"
            UPDATE_DESC="emaint sync -a && emerge -uDN @world (сборка из исходников может занять часы)"
            ;;
        solus) PKG_MGR="eopkg"; UPDATE_DESC="eopkg upgrade -y" ;;
        nixos)
            CRON_UNIT="cron.service"
            UPDATE_MGR="nixos-rebuild"; UPDATE_DESC="nixos-rebuild switch --upgrade"
            OS_NOTE="NixOS: пакеты и службы объявляются в configuration.nix — автоустановка пакетов отключена"
            ;;
        *)
            OS_NOTE="ОС «${OS_PRETTY}» (ID=${OS_ID}${like:+, ID_LIKE=${like}}) не распознана: пакеты автоматически не ставятся — установите их вручную или задайте OS_FAMILY в ${CONF_REL} (arch, debian, redhat, suse, alpine, void, gentoo, solus, nixos)"
            ;;
    esac
    [[ -n "${UPDATE_MGR}" ]] || UPDATE_MGR="${PKG_MGR}"
    has_systemd || OS_NOTE="${OS_NOTE:+${OS_NOTE}; }systemd не запущен (${OS_FAMILY}): юниты не создаются, службы включайте средствами своего init"
    return 0
}

# systemd установлен и является init этой системы
has_systemd() {
    command -v systemctl >/dev/null 2>&1 && [[ -d "${SYSTEMD_RUN_DIR}" ]]
}

# Проверка условия «path:…,cmd:…,fs:ТОЧКА=ТИП,family:…» (opt пропускается)
check_when() {
    local when="$1"
    local token mnt want fstype
    local tokens=()

    [[ -z "${when}" ]] && return 0
    IFS=',' read -r -a tokens <<<"${when}"
    for token in "${tokens[@]}"; do
        case "${token}" in
            opt|"") ;;
            path:*) [[ -e "${token#path:}" ]] || return 1 ;;
            cmd:*) command -v "${token#cmd:}" >/dev/null 2>&1 || return 1 ;;
            family:*) [[ "${OS_FAMILY}" == "${token#family:}" ]] || return 1 ;;
            fs:*)
                mnt="${token#fs:}"
                want="${mnt#*=}"
                mnt="${mnt%%=*}"
                fstype="$(findmnt -no FSTYPE -- "${mnt}" 2>/dev/null)" || return 1
                [[ "${fstype}" == "${want}" ]] || return 1
                ;;
            *) return 1 ;;
        esac
    done
    return 0
}

when_label() {
    local when="$1"
    local token tokens=() out=""
    IFS=',' read -r -a tokens <<<"${when}"
    for token in "${tokens[@]}"; do
        [[ "${token}" == "opt" || -z "${token}" ]] && continue
        out+="${out:+, }${token}"
    done
    printf '%s' "${out}"
}

###############################################################################
# HELPERS: root-шаги (вызываются через sudo/pkexec, логи не пишут)
###############################################################################
# Аргументы включения юнита в ENABLE_ARGS: таймер — сразу запустить,
# сервис по событию (вход в сеанс) — только включить
unit_enable_args() {
    if [[ "$1" == *.timer ]]; then
        ENABLE_ARGS=(enable --now)
    else
        ENABLE_ARGS=(enable)
    fi
}

root_install_units() {
    local dest="$1"
    local primary="$2"
    shift 2
    install -d -m 0755 -- "${dest}" || return 1
    install -m 0644 -t "${dest}" -- "$@" || return 1
    systemctl daemon-reload || return 1
    unit_enable_args "${primary}"
    systemctl "${ENABLE_ARGS[@]}" -- "${primary}"
}

root_enable() {
    systemctl enable --now -- "$1"
}

root_pkg() {
    local mgr="$1"
    shift
    case "${mgr}" in
        pacman) pacman -S --needed --noconfirm -- "$@" ;;
        apt-get) apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y -- "$@" ;;
        dnf) dnf install -y -- "$@" ;;
        yum) yum install -y -- "$@" ;;
        rpm-ostree) rpm-ostree install --idempotent --assumeyes -- "$@" ;;
        zypper) zypper --non-interactive install -- "$@" ;;
        apk) apk add -- "$@" ;;
        xbps-install) xbps-install -Sy -- "$@" ;;
        emerge) emerge --noreplace --ask=n -- "$@" ;;
        eopkg) eopkg install -y -- "$@" ;;
        transactional-update) transactional-update --non-interactive pkg install -- "$@" ;;
        *) echo "Неизвестный менеджер пакетов: ${mgr}" >&2; return 2 ;;
    esac
}

# Полное обновление системы штатным средством семейства (MODE уточняет вариант)
root_update() {
    local mgr="$1"
    local mode="${2:-}"
    case "${mgr}" in
        pacman)
            if [[ "${mode}" == "garuda" ]]; then
                garuda-update --noconfirm
            else
                pacman -Syu --noconfirm
            fi
            ;;
        apt-get) apt-get update && DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade ;;
        dnf) dnf -y upgrade --refresh ;;
        yum) yum -y update ;;
        rpm-ostree) rpm-ostree upgrade ;;
        zypper)
            zypper --non-interactive refresh || return 1
            if [[ "${mode}" == "dup" ]]; then
                zypper --non-interactive dup
            else
                zypper --non-interactive update
            fi
            ;;
        transactional-update) transactional-update --non-interactive dup ;;
        apk) apk update && apk upgrade ;;
        xbps-install) xbps-install -Syu xbps && xbps-install -Syu ;;
        emerge) emaint sync -a && emerge --update --deep --newuse --ask=n @world ;;
        eopkg) eopkg upgrade -y ;;
        nixos-rebuild) nixos-rebuild switch --upgrade ;;
        *) echo "Неизвестный способ обновления: ${mgr}" >&2; return 2 ;;
    esac
}

# Запуск root-шага через выбранную команду повышения прав
as_root() {
    if [[ "${SUDO_OK}" -ne 1 ]]; then
        warn "Нет способа повысить права (sudo -A с SUDO_ASKPASS, pkexec или sudo в терминале) — шаг пропущен."
        return 1
    fi
    "${SUDO[@]}" "${SCRIPT_PATH}" "$@"
}

###############################################################################
# HELPERS: проверка заданий
###############################################################################
kind_label() {
    case "$1" in
        user-unit) printf 'юнит (польз.)' ;;
        system-unit) printf 'юнит (система)' ;;
        user-ext) printf 'сервис (польз.)' ;;
        system-ext) printf 'сервис (система)' ;;
        cron) printf 'cron' ;;
        link) printf 'ссылка' ;;
        autostart) printf 'автозапуск' ;;
        conf) printf 'конфиг' ;;
        venv) printf 'venv' ;;
        *) printf '%s' "$1" ;;
    esac
}

# Свойства юнита: P_LOAD, P_FILE, P_ACTIVE, P_RESULT, P_TYPE
unit_props() {
    local scope="$1"
    local unit="$2"
    local line out
    local sc=(systemctl)
    [[ "${scope}" == "user" ]] && sc+=(--user)

    P_LOAD=""; P_FILE=""; P_ACTIVE=""; P_RESULT=""; P_TYPE=""
    out="$("${sc[@]}" show -p LoadState -p UnitFileState -p ActiveState -p Result -p Type -- "${unit}" 2>/dev/null)" || return 1
    while IFS= read -r line; do
        case "${line}" in
            LoadState=*) P_LOAD="${line#*=}" ;;
            UnitFileState=*) P_FILE="${line#*=}" ;;
            ActiveState=*) P_ACTIVE="${line#*=}" ;;
            Result=*) P_RESULT="${line#*=}" ;;
            Type=*) P_TYPE="${line#*=}" ;;
            *) ;;
        esac
    done <<<"${out}"
    return 0
}

# Состояние юнита словами и признак проблемы (STATE_TXT, STATE_BAD)
unit_state_text() {
    local unit="$1"
    STATE_BAD=0
    if [[ "${P_ACTIVE}" == "failed" || ( -n "${P_RESULT}" && "${P_RESULT}" != "success" ) ]]; then
        STATE_TXT="ошибка (${P_RESULT:-${P_ACTIVE}})"
        STATE_BAD=1
    elif [[ "${unit}" == *.timer ]]; then
        if [[ "${P_ACTIVE}" == "active" ]]; then
            STATE_TXT="ожидает"
        else
            STATE_TXT="не активен"
            STATE_BAD=1
        fi
    elif [[ "${P_ACTIVE}" == "active" || "${P_ACTIVE}" == "activating" || "${P_ACTIVE}" == "reloading" ]]; then
        STATE_TXT="работает"
    elif [[ "${P_TYPE}" == "oneshot" ]]; then
        STATE_TXT="при событии"
    else
        STATE_TXT="не запущен"
        STATE_BAD=1
    fi
}

# Шаблоны юнита в UNIT_TPLS: все файлы с тем же базовым именем
unit_templates() {
    local kind="$1"
    local unit="$2"
    local dir="${TEMPLATE_DIR}"
    local base="${unit%.*}"
    local f
    UNIT_TPLS=()
    [[ "${kind}" == "user-unit" ]] && dir="${TEMPLATE_DIR}/user"
    for f in "${dir}/${base}".service "${dir}/${base}".timer; do
        [[ -f "${f}" ]] && UNIT_TPLS+=("${f}")
    done
    return 0
}

set_row() {
    local i="$1"
    R_INST[i]="$2"
    R_ENABLED[i]="$3"
    R_STATE[i]="$4"
    R_RESULT[i]="$5"
    R_PROBLEM[i]="$6"
    R_FIX[i]="$7"
    R_ACTION[i]="$8"
}

eval_unit() {
    local i="$1" kind="$2" name="$3"
    local scope="user" dest="${USER_UNIT_DIR}" templates=() tpl tmp inst="есть" missing=0 differs=0 enabled
    [[ "${kind}" == system-* ]] && { scope="system"; dest="${SYSTEM_UNIT_DIR}"; }

    if ! has_systemd; then
        set_row "${i}" "—" "—" "нет systemd" "ВНИМАНИЕ" "systemd не запущен: юнит создать нельзя, настройте службу средствами своего init" "none" ""
        return 0
    fi

    if [[ "${kind}" == *-unit ]]; then
        unit_templates "${kind}" "${name}"
        templates=("${UNIT_TPLS[@]}")
        if [[ ${#templates[@]} -eq 0 ]]; then
            set_row "${i}" "—" "—" "—" "ВНИМАНИЕ" "нет шаблона ${name} в ${TEMPLATE_DIR}" "none" ""
            return 0
        fi
        tmp="$(mktemp)"
        for tpl in "${templates[@]}"; do
            if [[ ! -f "${dest}/${tpl##*/}" ]]; then
                missing=1
            else
                render_file "${tpl}" "${tmp}"
                same_file "${tmp}" "${dest}/${tpl##*/}" || differs=1
            fi
        done
        rm -f -- "${tmp}"
        if [[ "${missing}" -eq 1 ]]; then
            set_row "${i}" "нет" "нет" "—" "ВНИМАНИЕ" "юнит не установлен в ${dest}" "install" "установить ${templates[*]##*/} в ${dest} и включить"
            return 0
        fi
        [[ "${differs}" -eq 1 ]] && inst="отличается"
    fi

    unit_props "${scope}" "${name}" || P_LOAD="not-found"
    if [[ "${P_LOAD}" != "loaded" ]]; then
        if [[ "${kind}" == *-ext ]]; then
            set_row "${i}" "нет" "—" "—" "ВНИМАНИЕ" "юнит ${name} не установлен (ставится вне mytasks)" "none" ""
        else
            set_row "${i}" "${inst}" "?" "не загружен" "ВНИМАНИЕ" "systemd не видит юнит" "install" "переустановить и включить ${name}"
        fi
        return 0
    fi

    enabled="нет"
    case "${P_FILE}" in
        enabled|enabled-runtime|static|alias|indirect|generated) enabled="да" ;;
        *) ;;
    esac
    unit_state_text "${name}"

    if [[ "${differs}" -eq 1 ]]; then
        set_row "${i}" "${inst}" "${enabled}" "${STATE_TXT}" "ВНИМАНИЕ" "установленный юнит отличается от шаблона (другой путь mytasks или старая версия)" "install" "обновить ${templates[*]##*/} из шаблона и включить"
    elif [[ "${enabled}" != "да" ]]; then
        set_row "${i}" "${inst}" "${enabled}" "${STATE_TXT}" "ВНИМАНИЕ" "юнит не включён в автозапуск" "enable" "включить ${name}"
    elif [[ "${STATE_BAD}" -eq 1 ]]; then
        set_row "${i}" "${inst}" "${enabled}" "${STATE_TXT}" "ВНИМАНИЕ" "юнит включён, но ${STATE_TXT}" "enable" "запустить ${name}"
    else
        set_row "${i}" "${inst}" "${enabled}" "${STATE_TXT}" "OK" "" "none" ""
    fi
}

# Поиск строки crontab с mytasks-скриптом: печатает exact / other / пусто
cron_match() {
    local lines="$1"
    local name="$2"
    printf '%s\n' "${lines}" | awk -v want="${MYTASKS_DIR}/${name}" -v base="/${name}" '
        /^[[:space:]]*(#|$)/ { next }
        {
            for (i = 6; i <= NF; i++) {
                if ($(i) == want) { found = "exact"; exit }
                if (length($(i)) >= length(base) && substr($(i), length($(i)) - length(base) + 1) == base) found = "other"
            }
        }
        END { print found }'
}

eval_cron() {
    local i="$1" name="$2" schedule="$3"
    local lines match

    if ! command -v crontab >/dev/null 2>&1; then
        set_row "${i}" "—" "—" "нет crontab" "ВНИМАНИЕ" "команда crontab не найдена (установите cron в зависимостях)" "none" ""
        return 0
    fi
    lines="$(crontab -l 2>/dev/null)" || lines=""
    match="$(cron_match "${lines}" "${name}")"
    case "${match}" in
        exact) set_row "${i}" "есть" "${schedule}" "в crontab" "OK" "" "none" "" ;;
        other) set_row "${i}" "другой путь" "—" "в crontab" "ВНИМАНИЕ" "в crontab ${name} вызывается не из ${MYTASKS_DIR}" "cron-rewrite" "заменить путь к ${name} в crontab на ${MYTASKS_DIR}/${name}" ;;
        *) set_row "${i}" "нет" "—" "—" "ВНИМАНИЕ" "задания нет в crontab" "cron-add" "добавить в crontab: ${schedule} ${MYTASKS_DIR}/${name}" ;;
    esac
}

eval_link() {
    local i="$1" name="$2" target="${MYTASKS_DIR}/$3"
    local link="${BIN_DIR}/${name}" cur

    if [[ ! -e "${target}" ]]; then
        set_row "${i}" "—" "—" "—" "ВНИМАНИЕ" "нет цели ${target}" "none" ""
    elif [[ -L "${link}" ]]; then
        cur="$(readlink -- "${link}")"
        if [[ "${cur}" == "${target}" ]]; then
            set_row "${i}" "есть" "—" "—" "OK" "" "none" ""
        else
            set_row "${i}" "другая цель" "—" "—" "ВНИМАНИЕ" "${link} указывает на ${cur}" "link" "перенаправить ${link} → ${target}"
        fi
    elif [[ -e "${link}" ]]; then
        set_row "${i}" "файл" "—" "—" "ВНИМАНИЕ" "${link} — обычный файл, не ссылка; уберите его вручную" "none" ""
    else
        set_row "${i}" "нет" "—" "—" "ВНИМАНИЕ" "ссылки ${link} нет" "link" "создать ссылку ${link} → ${target}"
    fi
}

eval_autostart() {
    local i="$1" name="$2"
    local tpl="${TEMPLATE_DIR}/autostart/${name}" dest="${AUTOSTART_DIR}/${name}" tmp exec_line state="не запущен"

    if [[ ! -f "${tpl}" ]]; then
        set_row "${i}" "—" "—" "—" "ВНИМАНИЕ" "нет шаблона ${tpl}" "none" ""
        return 0
    fi
    tmp="$(mktemp)"
    render_file "${tpl}" "${tmp}"
    exec_line="$(sed -n 's/^Exec=//p' "${tmp}" | head -n 1)"
    # Программа может быть запущена и через ссылку (llamactl_tray.py ↔ llamactl-tray),
    # поэтому ищем базовое имя без расширения, считая «_» и «-» равными
    exec_line="${exec_line%% *}"
    exec_line="${exec_line##*/}"
    exec_line="${exec_line%.*}"
    if [[ -n "${exec_line}" ]] && pgrep -f -- "${exec_line//[_-]/[_-]}" >/dev/null 2>&1; then
        state="работает"
    fi
    if [[ ! -f "${dest}" ]]; then
        set_row "${i}" "нет" "нет" "${state}" "ВНИМАНИЕ" "файла автозапуска ${dest} нет" "autostart" "создать ${dest}"
    elif ! same_file "${tmp}" "${dest}"; then
        set_row "${i}" "отличается" "да" "${state}" "ВНИМАНИЕ" "файл автозапуска отличается от шаблона" "autostart" "обновить ${dest} из шаблона"
    else
        set_row "${i}" "есть" "да" "${state}" "OK" "" "none" ""
    fi
    rm -f -- "${tmp}"
}

eval_conf() {
    local i="$1" name="$2" example="$3"
    local value
    if [[ -f "${SCRIPT_DIR}/${name}" ]]; then
        if [[ "${name}" == "${CONF_REL}" && "${CONF_PRESENT}" -eq 1 ]]; then
            value="$(conf_value "${CONF_FILE}" MYTASKS_DIR)"
            if [[ "${value}" != "${SCRIPT_DIR}" ]]; then
                set_row "${i}" "есть" "—" "другой каталог" "ВНИМАНИЕ" "в конфиге MYTASKS_DIR=${value:-(пусто)}, а скрипт запущен из ${SCRIPT_DIR}" "conf-dir" "записать MYTASKS_DIR=${SCRIPT_DIR}"
                return 0
            fi
        fi
        set_row "${i}" "есть" "—" "—" "OK" "" "none" ""
    elif [[ ! -f "${SCRIPT_DIR}/${example}" ]]; then
        set_row "${i}" "нет" "—" "—" "ВНИМАНИЕ" "нет ни конфига, ни примера ${example}" "none" ""
    else
        set_row "${i}" "нет" "—" "—" "ВНИМАНИЕ" "конфига нет" "conf" "создать ${name} по ${example} (мастер с описанием и значениями по умолчанию)"
    fi
}

eval_venv() {
    local i="$1" name="$2" modules="$3"
    local py="${MYTASKS_DIR}/${name}/bin/python"
    local setup="${MYTASKS_DIR}/${name%/*}/setup_venv.sh"
    if [[ ! -x "${py}" ]]; then
        set_row "${i}" "нет" "—" "—" "ВНИМАНИЕ" "нет окружения ${name}" "venv" "создать окружение: ${setup}"
    elif ! "${py}" -c "import ${modules}" >/dev/null 2>&1; then
        set_row "${i}" "есть" "—" "нет модулей" "ВНИМАНИЕ" "в окружении нет модулей ${modules}" "venv" "переустановить зависимости: ${setup}"
    else
        set_row "${i}" "есть" "—" "готово" "OK" "" "none" ""
    fi
}

# Проверка одного задания
eval_job() {
    local i="$1"
    local kind name arg when desc label
    IFS='|' read -r kind name arg when desc <<<"${ALL_JOBS[${i}]}"
    [[ "${name}" == "@CRON_UNIT@" ]] && name="${CRON_UNIT:-cron}"
    R_KIND[i]="${kind}"
    R_NAME[i]="${name}"
    R_DESC[i]="${desc}"

    if [[ "${ALL_JOBS[${i}]}" == *"|@CRON_UNIT@|"* ]] && { [[ -z "${CRON_UNIT}" ]] || ! has_systemd; }; then
        # Без systemd (Alpine, Void и т.п.) или с неизвестным юнитом — по процессу
        R_NAME[i]="демон cron"
        if ! command -v crontab >/dev/null 2>&1; then
            set_row "${i}" "—" "—" "—" "не требуется" "" "none" ""
            R_STATE[i]="нет crontab"
        elif pgrep -x 'crond|cron|cronie|busybox' >/dev/null 2>&1; then
            set_row "${i}" "—" "—" "работает" "OK" "" "none" ""
        else
            set_row "${i}" "—" "—" "не запущен" "ВНИМАНИЕ" "демон cron не запущен; включите его средствами init (OpenRC: rc-update add crond, runit: ln -s /etc/sv/cronie /var/service)" "none" ""
        fi
        return 0
    fi
    if ! check_when "${when}"; then
        label="$(when_label "${when}")"
        set_row "${i}" "—" "—" "—" "не требуется" "" "none" ""
        R_STATE[i]="нет: ${label}"
        return 0
    fi

    case "${kind}" in
        user-unit|system-unit|user-ext|system-ext) eval_unit "${i}" "${kind}" "${name}" ;;
        cron) eval_cron "${i}" "${name}" "${arg}" ;;
        link) eval_link "${i}" "${name}" "${arg}" ;;
        autostart) eval_autostart "${i}" "${name}" ;;
        conf) eval_conf "${i}" "${name}" "${arg}" ;;
        venv) eval_venv "${i}" "${name}" "${arg}" ;;
        *) set_row "${i}" "—" "—" "—" "ВНИМАНИЕ" "неизвестный вид задания «${kind}»" "none" "" ;;
    esac
}

eval_all() {
    local i
    for i in "${!ALL_JOBS[@]}"; do
        eval_job "${i}"
    done
}

###############################################################################
# HELPERS: исправление заданий
###############################################################################
fix_unit() {
    local i="$1"
    local kind="${R_KIND[${i}]}" name="${R_NAME[${i}]}"
    local scope="user" dest="${USER_UNIT_DIR}" templates=() tpl tmpdir files=() args=() rc=0
    [[ "${kind}" == system-* ]] && { scope="system"; dest="${SYSTEM_UNIT_DIR}"; }

    if [[ "${R_FIX[${i}]}" == "enable" ]]; then
        unit_enable_args "${name}"
        args=("${ENABLE_ARGS[@]}")
        [[ "${kind}" == *-ext || "${name}" == *.timer ]] && args=(enable --now)
        if [[ "${DRY_RUN}" -eq 1 ]]; then
            if [[ "${scope}" == "system" ]]; then
                say "[dry-run] будет: systemctl ${args[*]} ${name} (с повышением прав)"
            else
                say "[dry-run] будет: systemctl --user ${args[*]} ${name}"
            fi
        elif [[ "${scope}" == "system" ]]; then
            as_root __root-enable "${name}" || rc=$?
        else
            systemctl --user "${args[@]}" -- "${name}" || rc=$?
        fi
        return "${rc}"
    fi

    unit_templates "${kind}" "${name}"
    templates=("${UNIT_TPLS[@]}")
    tmpdir="$(mktemp -d)"
    for tpl in "${templates[@]}"; do
        render_file "${tpl}" "${tmpdir}/${tpl##*/}"
        chmod 0644 -- "${tmpdir}/${tpl##*/}"
        files+=("${tmpdir}/${tpl##*/}")
    done
    chmod 0755 -- "${tmpdir}"
    unit_enable_args "${name}"
    args=("${ENABLE_ARGS[@]}")
    if [[ "${scope}" == "system" ]]; then
        if [[ "${DRY_RUN}" -eq 1 ]]; then
            say "[dry-run] будет: установить ${templates[*]##*/} в ${dest}, systemctl daemon-reload, systemctl ${args[*]} ${name} (с повышением прав)"
        else
            as_root __root-install-units "${dest}" "${name}" "${files[@]}" || rc=$?
        fi
    else
        if [[ "${DRY_RUN}" -eq 1 ]]; then
            say "[dry-run] будет: установить ${templates[*]##*/} в ${dest}, systemctl --user daemon-reload, systemctl --user ${args[*]} ${name}"
        else
            mkdir -p -- "${dest}" \
                && install -m 0644 -t "${dest}" -- "${files[@]}" \
                && systemctl --user daemon-reload \
                && systemctl --user "${args[@]}" -- "${name}" || rc=$?
        fi
    fi
    rm -rf -- "${tmpdir}"
    return "${rc}"
}

fix_cron() {
    local i="$1"
    local name="${R_NAME[${i}]}" schedule desc lines tmp rc
    IFS='|' read -r _ _ schedule _ desc <<<"${ALL_JOBS[${i}]}"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: ${R_ACTION[${i}]}"
        return 0
    fi
    lines="$(crontab -l 2>/dev/null)" || lines=""
    tmp="$(mktemp)"
    if [[ "${R_FIX[${i}]}" == "cron-rewrite" ]]; then
        printf '%s\n' "${lines}" | awk -v want="${MYTASKS_DIR}/${name}" -v base="/${name}" '
            /^[[:space:]]*(#|$)/ { print; next }
            {
                for (i = 6; i <= NF; i++)
                    if (length($(i)) >= length(base) && substr($(i), length($(i)) - length(base) + 1) == base) $(i) = want
                print
            }' > "${tmp}"
    else
        [[ -n "${lines}" ]] && printf '%s\n' "${lines}" > "${tmp}"
        printf '# mytasks: %s\n%s %s/%s\n' "${desc}" "${schedule}" "${MYTASKS_DIR}" "${name}" >> "${tmp}"
    fi
    crontab "${tmp}"
    rc=$?
    rm -f -- "${tmp}"
    return "${rc}"
}

fix_link() {
    local i="$1"
    local name arg
    IFS='|' read -r _ name arg _ <<<"${ALL_JOBS[${i}]}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: ${R_ACTION[${i}]}"
        return 0
    fi
    mkdir -p -- "${BIN_DIR}" && ln -sfn -- "${MYTASKS_DIR}/${arg}" "${BIN_DIR}/${name}"
}

fix_autostart() {
    local i="$1"
    local name="${R_NAME[${i}]}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: ${R_ACTION[${i}]}"
        return 0
    fi
    mkdir -p -- "${AUTOSTART_DIR}" && render_file "${TEMPLATE_DIR}/autostart/${name}" "${AUTOSTART_DIR}/${name}"
}

fix_venv() {
    local i="$1"
    local name="${R_NAME[${i}]}"
    local setup="${MYTASKS_DIR}/${name%/*}/setup_venv.sh"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: ${R_ACTION[${i}]}"
        return 0
    fi
    say "Создаётся окружение ${name} (pip ставит зависимости, это может занять несколько минут)…"
    bash "${setup}"
}

# Замена MYTASKS_DIR в собственном конфиге на каталог запуска
fix_conf_dir() {
    local tmp line re='^MYTASKS_DIR='
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: записать MYTASKS_DIR=\"${SCRIPT_DIR}\" в ${CONF_REL}"
        MYTASKS_DIR="${SCRIPT_DIR}"
        return 0
    fi
    tmp="$(mktemp)"
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" =~ ${re} ]]; then
            printf 'MYTASKS_DIR="%s"\n' "${SCRIPT_DIR}"
        else
            printf '%s\n' "${line}"
        fi
    done < "${CONF_FILE}" > "${tmp}"
    cat -- "${tmp}" > "${CONF_FILE}"
    rm -f -- "${tmp}"
    MYTASKS_DIR="${SCRIPT_DIR}"
}

fix_job() {
    local i="$1"
    local name arg
    case "${R_FIX[${i}]}" in
        install|enable) fix_unit "${i}" ;;
        cron-add|cron-rewrite) fix_cron "${i}" ;;
        link) fix_link "${i}" ;;
        autostart) fix_autostart "${i}" ;;
        venv) fix_venv "${i}" ;;
        conf-dir) fix_conf_dir ;;
        conf)
            IFS='|' read -r _ name arg _ <<<"${ALL_JOBS[${i}]}"
            conf_wizard "${name}" "${arg}" || return 3
            ;;
        *) return 1 ;;
    esac
}

# Решение «создать?» для проблемы: 0 — делать, 1 — нет (или «Стоп»)
decide() {
    local title="$1"
    local text="$2"
    local rc
    [[ "${STOP}" -eq 1 ]] && return 1
    [[ "${DRY_RUN}" -eq 1 || "${ASSUME_YES}" -eq 1 ]] && return 0
    [[ "${NO_CREATE}" -eq 1 || "${STATUS_ONLY}" -eq 1 ]] && return 1
    ui_yesno "${title}" "${text}"
    rc=$?
    if [[ "${rc}" -eq "${STOP_RC}" ]]; then
        request_stop
        return 1
    fi
    return "${rc}"
}

# Предупреждение по заданию и (по согласию) его исправление
handle_job() {
    local i="$1"
    local name="${R_NAME[${i}]}" label text rc
    [[ "${STOP}" -eq 1 ]] && return 0
    [[ "${R_RESULT[${i}]}" == "ВНИМАНИЕ" ]] || return 0
    # Задание уже обработано в этом запуске (исправлено, отклонено или не
    # исправляется) — второй раз не предупреждать и не спрашивать
    [[ " ${HANDLED} " == *" ${name} "* ]] && return 0
    HANDLED+=" ${name}"
    label="$(kind_label "${R_KIND[${i}]}")"

    warn "${label} ${name}: ${R_PROBLEM[${i}]}"
    say "   ${R_DESC[${i}]}"
    log_json "WARN" "job_problem" "${label} ${name}: ${R_PROBLEM[${i}]}" "${R_DESC[${i}]}"

    if [[ "${R_FIX[${i}]}" == "none" ]]; then
        say "   Автоматически не исправляется."
        return 0
    fi
    if [[ "${R_FIX[${i}]}" == "conf" ]]; then
        # Мастер сам спрашивает «создать / настроить / не создавать»
        if [[ " ${SKIPPED_CONFS} " == *" ${name} "* ]]; then
            say "   Пропущено ранее в мастере конфигов."
            return 0
        fi
        if [[ "${NO_CREATE}" -eq 1 || ( "${UI_MODE}" == "none" && "${ASSUME_YES}" -eq 0 && "${DRY_RUN}" -eq 0 ) ]]; then
            say "   Пропущено (нет возможности задать вопрос)."
            return 0
        fi
    else
        text="${label}: ${name}"$'\n\n'"${R_DESC[${i}]}"$'\n\n'"Проблема: ${R_PROBLEM[${i}]}"$'\n'"Действие: ${R_ACTION[${i}]}"$'\n\n'"Создать / исправить?"
    fi
    if [[ "${R_FIX[${i}]}" != "conf" ]] && ! decide "mytasks — ${name}" "${text}"; then
        [[ "${STOP}" -eq 1 ]] && return 0
        say "   Пропущено (не создавать)."
        log_json "INFO" "job_skipped" "Пропущено: ${name}" "${R_ACTION[${i}]}"
        return 0
    fi
    CHANGED=1
    fix_job "${i}"
    rc=$?
    if [[ "${rc}" -eq 3 ]]; then
        [[ "${STOP}" -eq 1 ]] && return 0
        log_json "INFO" "job_skipped" "Пропущено: ${name}" "${R_ACTION[${i}]}"
    elif [[ "${rc}" -eq 0 ]]; then
        [[ "${DRY_RUN}" -eq 1 ]] || say "   ✔ ${R_ACTION[${i}]}"
        log_json "INFO" "job_fixed" "Исправлено: ${name}" "${R_ACTION[${i}]}" 0
    else
        warn "Не удалось: ${R_ACTION[${i}]} (код ${rc})"
        log_json "ERROR" "job_fix_failed" "Не удалось исправить: ${name}" "${R_ACTION[${i}]}" "${rc}"
    fi
    return 0
}

###############################################################################
# HELPERS: обновление системы
###############################################################################
# Нужна ли перезагрузка: флаг Debian/Ubuntu или нет модулей работающего ядра
reboot_needed() {
    local kver
    [[ -e "${REBOOT_FLAG}" ]] && return 0
    kver="$(uname -r)"
    [[ -d "${KERNEL_MODULES_DIR}/${kver}" ]] && return 1
    [[ -d "${KERNEL_MODULES_DIR}" ]]
}

# Обязательный первый шаг: полное обновление системы до любых изменений.
# Без него установка пакетов в rolling-дистрибутивах (Arch и др.) — это
# неподдерживаемое частичное обновление, поэтому при сбое пакеты не ставятся.
system_update() {
    local rc
    [[ "${STOP}" -eq 1 ]] && return 0
    if [[ -z "${UPDATE_MGR}" ]]; then
        UPDATE_OK=0
        warn "Обновление системы для «${OS_PRETTY}» (${OS_FAMILY}) автоматически не выполняется — обновите её вручную; пакеты не ставятся."
        log_json "WARN" "update_unsupported" "Обновление системы не поддерживается" "${OS_FAMILY}"
        return 0
    fi
    say "Обновление системы — обязательный первый шаг: ${UPDATE_DESC}"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: обновить систему: ${UPDATE_DESC} (с повышением прав)"
        return 0
    fi
    log_json "INFO" "update_start" "Обновление системы" "${UPDATE_DESC}"
    as_root __root-update "${UPDATE_MGR}" "${UPDATE_MODE}"
    rc=$?
    if [[ "${rc}" -ne 0 ]]; then
        UPDATE_OK=0
        warn "Обновление системы не удалось (код ${rc}): пакеты в этом запуске не ставятся, остальное проверяется."
        log_json "ERROR" "update_failed" "Обновление системы не удалось" "${UPDATE_DESC}" "${rc}"
        return 0
    fi
    say "   ✔ система обновлена"
    log_json "INFO" "update_done" "Система обновлена" "${UPDATE_DESC}" 0
    if reboot_needed; then
        REBOOT_NEEDED=1
        warn "После обновления нужна перезагрузка (обновлено ядро или системные библиотеки)."
        log_json "WARN" "reboot_needed" "Нужна перезагрузка после обновления" ""
    fi
    return 0
}

###############################################################################
# HELPERS: зависимости
###############################################################################
dep_present() {
    local cmd="$1"
    local alt alts=() ver
    if [[ "${cmd}" == gnu:* ]]; then
        # GNU-версия утилиты (в busybox нет find -printf, stat -c, date -d и т.п.)
        ver="$("${cmd#gnu:}" --version 2>/dev/null)" || return 1
        [[ "${ver}" == *GNU* ]]
        return
    fi
    if [[ "${cmd}" == py:* ]]; then
        [[ -x "${SYSTEM_PYTHON}" ]] || return 1
        "${SYSTEM_PYTHON}" -c "import ${cmd#py:}" >/dev/null 2>&1
        return
    fi
    IFS=',' read -r -a alts <<<"${cmd}"
    for alt in "${alts[@]}"; do
        command -v "${alt}" >/dev/null 2>&1 && return 0
    done
    return 1
}

# Пакет для текущего семейства из «пакет;семейство=пакет;…»
dep_package() {
    local spec="$1"
    local item items=() default="-"
    IFS=';' read -r -a items <<<"${spec}"
    for item in "${items[@]}"; do
        if [[ "${item}" == "${OS_FAMILY}="* ]]; then
            printf '%s' "${item#*=}"
            return 0
        fi
        [[ "${item}" == *=* ]] || default="${item}"
    done
    [[ "${OS_FAMILY}" == "unknown" ]] && default="-"
    printf '%s' "${default}"
}

# Таблица зависимостей: all — все, missing — только отсутствующие
deps_rows() {
    local mode="$1"
    local entry cmd spec when desc pkg have need
    DEP_ROWS=()
    DEP_MISSING_REQ=0
    for entry in "${DEPS[@]}"; do
        IFS='|' read -r cmd spec when desc <<<"${entry}"
        check_when "${when}" || continue
        pkg="$(dep_package "${spec}")"
        need="обязательно"
        [[ ",${when}," == *",opt,"* ]] && need="опционально"
        if dep_present "${cmd}"; then
            have="есть"
            [[ "${mode}" == "all" ]] || continue
        else
            have="нет"
            [[ "${need}" == "обязательно" ]] && DEP_MISSING_REQ=$((DEP_MISSING_REQ + 1))
        fi
        DEP_ROWS+=("${cmd}"$'\t'"${pkg}"$'\t'"${have}"$'\t'"${need}"$'\t'"${desc}")
    done
}

handle_deps() {
    local entry cmd spec when desc pkg optional text pkgs=() rc
    [[ "${STOP}" -eq 1 ]] && return 0
    for entry in "${DEPS[@]}"; do
        IFS='|' read -r cmd spec when desc <<<"${entry}"
        check_when "${when}" || continue
        dep_present "${cmd}" && continue
        pkg="$(dep_package "${spec}")"
        optional=0
        [[ ",${when}," == *",opt,"* ]] && optional=1

        if [[ "${optional}" -eq 1 ]]; then
            say "· Нет необязательной зависимости ${cmd} (пакет ${pkg}): ${desc}"
        else
            warn "Нет зависимости ${cmd} (пакет ${pkg}): ${desc}"
            log_json "WARN" "dep_missing" "Нет зависимости ${cmd}" "${pkg}: ${desc}"
        fi
        if [[ "${pkg}" == "-" || -z "${PKG_MGR}" ]]; then
            say "   Автоматически не ставится (семейство ${OS_FAMILY}${PKG_MGR:+, нет пакета}) — установите вручную."
            continue
        fi
        [[ "${optional}" -eq 1 && "${ASSUME_YES}" -eq 1 ]] && continue
        if [[ "${optional}" -eq 1 && "${DRY_RUN}" -eq 1 ]]; then
            say "[dry-run] будет: вопрос об установке ${pkg} (необязательно)"
            continue
        fi
        text="Зависимость: ${cmd}"$'\n\n'"${desc}"$'\n\n'"Пакет (${OS_FAMILY}, ${PKG_MGR}): ${pkg}"
        [[ "${optional}" -eq 1 ]] && text+=$'\n'"Необязательная: без неё работает всё, кроме указанного."
        text+=$'\n\n'"Установить?"
        if decide "mytasks — пакет ${pkg}" "${text}"; then
            [[ " ${pkgs[*]} " == *" ${pkg} "* ]] || pkgs+=("${pkg}")
        fi
        [[ "${STOP}" -eq 1 ]] && return 0
    done

    [[ ${#pkgs[@]} -gt 0 ]] || return 0
    if [[ "${UPDATE_OK}" -ne 1 ]]; then
        warn "Пакеты ${pkgs[*]} не ставятся: система не обновлена (частичное обновление не поддерживается). Обновите систему и запустите скрипт снова."
        return 0
    fi
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: установить пакеты (${OS_FAMILY}, ${PKG_MGR}): ${pkgs[*]} (с повышением прав)"
        return 0
    fi
    as_root __root-pkg "${PKG_MGR}" "${pkgs[@]}"
    rc=$?
    if [[ "${rc}" -eq 0 ]]; then
        log_json "INFO" "pkg_install" "Установлены пакеты" "${pkgs[*]}" 0
    else
        warn "Не удалось установить пакеты: ${pkgs[*]} (код ${rc})"
        log_json "ERROR" "pkg_install_failed" "Не удалось установить пакеты" "${pkgs[*]}" "${rc}"
    fi
}

###############################################################################
# HELPERS: мастер конфигов
###############################################################################
# Значение переменной из конфига без его выполнения
conf_value() {
    local file="$1"
    local var="$2"
    local line val re="^${var}=(.*)$"
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${line}" =~ ${re} ]]; then
            val="${BASH_REMATCH[1]}"
            val="${val%\"}"
            val="${val#\"}"
            printf '%s' "${val}"
            return 0
        fi
    done < "${file}"
    return 1
}

# Разбор примера: блоки «комментарии над переменной + присваивание»
parse_example() {
    local file="$1"
    local line text desc="" header="" header_done=0 in_array=0 vals="" val quote
    local re_comment='^[[:space:]]*#' re_blank='^[[:space:]]*$'
    local re_arr_start='^([A-Za-z_][A-Za-z0-9_]*)=\($' re_arr_end='^[[:space:]]*\)[[:space:]]*$'
    local re_arr_item='^[[:space:]]*"(.*)"[[:space:]]*$'
    local re_assign='^([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
    local re_dq='^"(.*)"$' re_sq="^'(.*)'$"

    CE_NAME=(); CE_KIND=(); CE_VAL=(); CE_DESC=(); CE_QUOTE=(); CE_NEW=()
    CE_HEADER=""
    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${in_array}" -eq 1 ]]; then
            if [[ "${line}" =~ ${re_arr_end} ]]; then
                CE_VAL+=("${vals%$'\n'}")
                in_array=0
            elif [[ "${line}" =~ ${re_arr_item} ]]; then
                vals+="${BASH_REMATCH[1]}"$'\n'
            fi
            continue
        fi
        if [[ "${line}" =~ ${re_comment} ]]; then
            text="${line#*#}"
            text="${text# }"
            if [[ "${header_done}" -eq 0 ]]; then
                # Подсказку «Скопируйте этот файл…» мастер не показывает: копирует сам
                [[ "${text}" == Скопируйте* ]] || header+="${text}"$'\n'
            else
                desc+="${text}"$'\n'
            fi
            continue
        fi
        if [[ "${line}" =~ ${re_blank} ]]; then
            header_done=1
            desc=""
            continue
        fi
        if [[ "${header_done}" -eq 0 ]]; then
            # Комментарий сразу над первой переменной — её описание, а не шапка
            desc="${header}"
            header=""
            header_done=1
        fi
        if [[ "${line}" =~ ${re_arr_start} ]]; then
            CE_NAME+=("${BASH_REMATCH[1]}"); CE_KIND+=("array"); CE_DESC+=("${desc%$'\n'}"); CE_QUOTE+=("dq")
            in_array=1
            vals=""
            desc=""
            continue
        fi
        if [[ "${line}" =~ ${re_assign} ]]; then
            CE_NAME+=("${BASH_REMATCH[1]}")
            val="${BASH_REMATCH[2]}"
            quote="none"
            if [[ "${val}" =~ ${re_dq} ]]; then
                val="${BASH_REMATCH[1]}"; quote="dq"
            elif [[ "${val}" =~ ${re_sq} ]]; then
                val="${BASH_REMATCH[1]}"; quote="sq"
            fi
            CE_KIND+=("scalar"); CE_VAL+=("${val}"); CE_DESC+=("${desc%$'\n'}"); CE_QUOTE+=("${quote}")
            desc=""
        fi
    done < "${file}"
    CE_HEADER="${header%$'\n'}"
}

# Значение в синтаксисе shell с исходным видом кавычек
quote_value() {
    local value="$1"
    local quote="$2"
    local re_plain='^[A-Za-z0-9_./:,+@%=-]*$'
    if [[ "${quote}" == "none" && "${value}" =~ ${re_plain} ]]; then
        printf '%s' "${value}"
        return 0
    fi
    if [[ "${quote}" == "sq" && "${value}" != *"'"* ]]; then
        printf "'%s'" "${value}"
        return 0
    fi
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    value="${value//\`/\\\`}"
    printf '"%s"' "${value}"
}

# Запись конфига по примеру: подстановка во всех строках, новые значения
# переменных из CE_NEW (неизменённые массивы остаются как в примере)
write_conf() {
    local example="$1"
    local out="$2"
    local line in_array=0 skip=0 j k name elem quoted items=()
    local re_arr_start='^([A-Za-z_][A-Za-z0-9_]*)=\($' re_arr_end='^[[:space:]]*\)[[:space:]]*$'
    local re_assign='^([A-Za-z_][A-Za-z0-9_]*)=(.*)$'

    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ "${in_array}" -eq 1 ]]; then
            if [[ "${line}" =~ ${re_arr_end} ]]; then
                in_array=0
                [[ "${skip}" -eq 1 ]] && { printf ')\n'; continue; }
            fi
            [[ "${skip}" -eq 1 ]] || subst "${line}"$'\n'
            continue
        fi
        if [[ "${line}" =~ ${re_arr_start} || "${line}" =~ ${re_assign} ]]; then
            name="${BASH_REMATCH[1]}"
            j=-1
            for k in "${!CE_NAME[@]}"; do
                [[ "${CE_NAME[${k}]}" == "${name}" ]] && { j="${k}"; break; }
            done
            if [[ "${j}" -ge 0 && "${CE_KIND[${j}]}" == "array" ]]; then
                in_array=1
                skip=0
                subst "${line}"$'\n'
                elem="$(subst "${CE_VAL[${j}]}")"
                if [[ "${CE_NEW[${j}]}" != "${elem}" ]]; then
                    skip=1
                    mapfile -t items <<<"${CE_NEW[${j}]}"
                    for elem in "${items[@]}"; do
                        [[ -n "${elem}" ]] || continue
                        quoted="$(quote_value "${elem}" dq)"
                        printf '    %s\n' "${quoted}"
                    done
                fi
                continue
            fi
            elem="$(subst "${CE_VAL[${j}]}")"
            if [[ "${j}" -ge 0 && "${CE_NEW[${j}]}" != "${elem}" ]]; then
                quoted="$(quote_value "${CE_NEW[${j}]}" "${CE_QUOTE[${j}]}")"
                printf '%s=%s\n' "${name}" "${quoted}"
                continue
            fi
        fi
        subst "${line}"$'\n'
    done < "${example}" > "${out}"
}

# Значение по умолчанию, определённое на этой машине (вместо значения примера):
# семейство ОС, архитектура CUDA и число потоков сборки llama.cpp
smart_default() {
    local rel="$1"
    local j="$2"
    local cap cores
    case "${rel}|${CE_NAME[${j}]}" in
        "${CONF_REL}|OS_FAMILY")
            [[ "${OS_FAMILY}" != "unknown" ]] && CE_NEW[j]="${OS_FAMILY}"
            ;;
        "conf/llamactl.conf|CUDA_ARCH")
            if command -v nvidia-smi >/dev/null 2>&1; then
                cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n 1)"
                cap="${cap//[^0-9]/}"
                [[ -n "${cap}" ]] && CE_NEW[j]="${cap}"
            fi
            ;;
        "conf/llamactl.conf|BUILD_JOBS")
            cores="$(nproc 2>/dev/null)" || cores=""
            if [[ "${cores}" =~ ^[0-9]+$ ]]; then
                # Два ядра оставляем системе и работающему llama-server
                (( cores > 2 )) && cores=$((cores - 2))
                CE_NEW[j]="${cores}"
            fi
            ;;
        *) ;;
    esac
    return 0
}

# Мастер одного конфига: 0 — создан, 1 — пропущен
conf_wizard() {
    local rel="$1"
    local example_rel="$2"
    local example="${SCRIPT_DIR}/${example_rel}" target="${SCRIPT_DIR}/${rel}"
    local text title choice j k n defaults="" content tmp answer rc

    [[ -f "${example}" ]] || return 1
    parse_example "${example}"
    n="${#CE_NAME[@]}"
    for j in "${!CE_NAME[@]}"; do
        CE_NEW[j]="$(subst "${CE_VAL[${j}]}")"
        smart_default "${rel}" "${j}"
        if [[ "${CE_KIND[${j}]}" == "array" ]]; then
            defaults+="  ${CE_NAME[${j}]} = (${CE_NEW[${j}]//$'\n'/; })"$'\n'
        else
            defaults+="  ${CE_NAME[${j}]} = ${CE_NEW[${j}]:-(пусто)}"$'\n'
        fi
    done
    title="mytasks — ${rel}"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: создать ${rel} по ${example_rel}"
        [[ -n "${defaults}" ]] && printf '%s' "${defaults}" | sed 's/^/[dry-run]   /'
        return 0
    fi

    if [[ "${n}" -eq 0 ]]; then
        # Конфиг-список (строки) или JSON: редактируется текстом целиком
        content="$(<"${example}")"
        content="$(subst "${content}")"
        text="Конфиг ${rel} не найден. Пример: ${example_rel}"$'\n\n'"${CE_HEADER}"
        if [[ "${ASSUME_YES}" -eq 1 ]]; then
            choice=0
        else
            choice="$(ui_choice "${title}" "${text}" "Создать по примеру (пути подставлены)" "Отредактировать текст" "Не создавать")"
            rc=$?
            [[ "${rc}" -eq "${STOP_RC}" ]] && { request_stop; return 1; }
            [[ "${rc}" -ne 0 ]] && choice=2
        fi
        case "${choice}" in
            0) ;;
            1)
                content="$(ui_text "${title}" "Содержимое ${rel}:"$'\n'"${CE_HEADER}" "${content}")" \
                    || { request_stop; return 1; }
                ;;
            *) say "   ${rel}: не создан."; SKIPPED_CONFS+=" ${rel}"; return 1 ;;
        esac
        mkdir -p -- "${target%/*}"
        ( umask 077 && printf '%s\n' "${content}" > "${target}" )
        say "   ✔ создан ${rel}"
        log_json "INFO" "conf_created" "Создан конфиг ${rel}" "${example_rel}"
        return 0
    fi

    text="Конфиг ${rel} не найден. Пример: ${example_rel}"
    [[ -n "${CE_HEADER}" ]] && text+=$'\n\n'"${CE_HEADER}"
    text+=$'\n\n'"Значения по умолчанию:"$'\n'"${defaults}"
    if [[ "${ASSUME_YES}" -eq 1 ]]; then
        choice=0
    else
        choice="$(ui_choice "${title}" "${text}" "Создать со значениями по умолчанию" "Настроить каждое значение" "Не создавать")"
        rc=$?
        [[ "${rc}" -eq "${STOP_RC}" ]] && { request_stop; return 1; }
        [[ "${rc}" -ne 0 ]] && choice=2
    fi
    case "${choice}" in
        0) ;;
        1)
            k=0
            for j in "${!CE_NAME[@]}"; do
                k=$((k + 1))
                text="${CE_DESC[${j}]:-(без описания)}"
                if [[ "${CE_KIND[${j}]}" == "array" ]]; then
                    text+=$'\n\n'"Список: по одному значению на строку."$'\n'"По умолчанию:"$'\n'"${CE_NEW[${j}]}"
                    answer="$(ui_text "${title} — ${CE_NAME[${j}]} (${k} из ${n})" "${text}" "${CE_NEW[${j}]}")" \
                        || { request_stop; return 1; }
                    CE_NEW[j]="${answer}"
                else
                    text+=$'\n\n'"По умолчанию: ${CE_NEW[${j}]}"
                    answer="$(ui_input "${title} — ${CE_NAME[${j}]} (${k} из ${n})" "${text}" "${CE_NEW[${j}]}")" \
                        || { request_stop; return 1; }
                    CE_NEW[j]="${answer}"
                fi
            done
            ;;
        *) say "   ${rel}: не создан."; SKIPPED_CONFS+=" ${rel}"; return 1 ;;
    esac
    mkdir -p -- "${target%/*}"
    tmp="$(mktemp)"
    write_conf "${example}" "${tmp}"
    ( umask 077 && cat -- "${tmp}" > "${target}" )
    rm -f -- "${tmp}"
    say "   ✔ создан ${rel}"
    log_json "INFO" "conf_created" "Создан конфиг ${rel}" "${example_rel}"
    return 0
}

# Перечитать собственный конфиг после создания или правки
reload_conf() {
    [[ -r "${CONF_FILE}" ]] || return 0
    # shellcheck source=/dev/null
    source "${CONF_FILE}"
    CONF_PRESENT=1
    apply_defaults
}

apply_defaults() {
    MYTASKS_DIR="${MYTASKS_DIR:-${SCRIPT_DIR}}"
    if [[ -z "${SYSTEM_PYTHON}" ]]; then
        SYSTEM_PYTHON="/usr/bin/python3"
        [[ -x "${SYSTEM_PYTHON}" ]] || SYSTEM_PYTHON="$(command -v python3 || printf '%s' "${SYSTEM_PYTHON}")"
    fi
    USER_UNIT_DIR="${USER_UNIT_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user}"
    AUTOSTART_DIR="${AUTOSTART_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/autostart}"
    BIN_DIR="${BIN_DIR:-${HOME}/.local/bin}"
    ALL_JOBS=(
        "user-unit|${SELF_UNIT}|||Сам ${SCRIPT_NAME} при входе в сеанс: таблица состояния в журнале и уведомление, если сервисы не запущены или задания не прописаны."
        "conf|${CONF_REL}|${CONF_REL}.example||Каталог mytasks (берётся из каталога запуска) для юнитов, cron, ссылок и автозапуска; семейство ОС; способ повышения прав."
        "${JOBS[@]}"
    )
}

# Мастер всех отсутствующих конфигов по примерам
configure_all() {
    local example rel examples=()
    if [[ ${#CONF_EXAMPLES[@]} -gt 0 ]]; then
        examples=("${CONF_EXAMPLES[@]}")
    else
        for example in "${CONF_DIR}"/*.conf.example "${SCRIPT_DIR}"/music_downloader/*.json.example; do
            [[ -f "${example}" ]] && examples+=("${example#"${SCRIPT_DIR}"/}")
        done
    fi
    for example in "${examples[@]}"; do
        rel="${example%.example}"
        [[ "${rel}" == "${CONF_REL}" ]] && continue
        [[ -f "${SCRIPT_DIR}/${rel}" ]] && continue
        [[ " ${CONF_WIZARD_SKIP[*]} " == *" ${rel} "* ]] && continue
        [[ "${STOP}" -eq 1 ]] && break
        say "· Конфиг ${rel}: нет, мастер по ${example}"
        conf_wizard "${rel}" "${example}"
    done
}

###############################################################################
# HELPERS: таблицы
###############################################################################
# Вывод таблицы из строк с полями через табуляцию; ширина — в символах
print_table() {
    local color_col="$1"
    shift
    local rows=("$@")
    local row cells=() widths=() c len out pad cell color spaces reset="" use_color=0
    [[ -t 1 ]] && { use_color=1; reset=$'\033[0m'; }

    for row in "${rows[@]}"; do
        IFS=$'\t' read -r -a cells <<<"${row}"
        for c in "${!cells[@]}"; do
            len="${#cells[${c}]}"
            (( len > ${widths[${c}]:-0} )) && widths[c]="${len}"
        done
    done
    for row in "${rows[@]}"; do
        IFS=$'\t' read -r -a cells <<<"${row}"
        out=""
        for c in "${!cells[@]}"; do
            cell="${cells[${c}]}"
            pad=$(( ${widths[${c}]} - ${#cell} ))
            color=""
            if [[ "${use_color}" -eq 1 && "${c}" -eq "${color_col}" ]]; then
                case "${cell}" in
                    OK|есть) color=$'\033[32m' ;;
                    ВНИМАНИЕ|нет) color=$'\033[31m' ;;
                    *) color=$'\033[2m' ;;
                esac
            fi
            if [[ -n "${color}" ]]; then
                out+="${color}${cell}${reset}"
            else
                out+="${cell}"
            fi
            if [[ "${c}" -lt $(( ${#cells[@]} - 1 )) ]]; then
                printf -v spaces '%*s' "$(( pad + 2 ))" ''
                out+="${spaces}"
            fi
        done
        printf '%s\n' "${out}"
    done
}

print_jobs_table() {
    local i rows=() desc label
    rows+=("Вид"$'\t'"Задание"$'\t'"Установлено"$'\t'"Автозапуск"$'\t'"Состояние"$'\t'"Итог"$'\t'"Описание")
    for i in "${!ALL_JOBS[@]}"; do
        desc="${R_DESC[${i}]}"
        (( ${#desc} > 70 )) && desc="${desc:0:69}…"
        label="$(kind_label "${R_KIND[${i}]}")"
        rows+=("${label}"$'\t'"${R_NAME[${i}]}"$'\t'"${R_INST[${i}]}"$'\t'"${R_ENABLED[${i}]}"$'\t'"${R_STATE[${i}]}"$'\t'"${R_RESULT[${i}]}"$'\t'"${desc}")
    done
    print_table 5 "${rows[@]}"
}

print_deps_table() {
    local mode="$1"
    local rows=()
    deps_rows "${mode}"
    if [[ ${#DEP_ROWS[@]} -eq 0 ]]; then
        say "Зависимости: все на месте."
        return 0
    fi
    rows+=("Команда"$'\t'"Пакет (${OS_FAMILY})"$'\t'"Есть"$'\t'"Нужна"$'\t'"Для чего")
    rows+=("${DEP_ROWS[@]}")
    print_table 2 "${rows[@]}"
}

count_problems() {
    local i n=0
    for i in "${!ALL_JOBS[@]}"; do
        [[ "${R_RESULT[${i}]}" == "ВНИМАНИЕ" ]] && n=$((n + 1))
    done
    PROBLEMS="${n}"
}

problem_names() {
    local i out=""
    for i in "${!ALL_JOBS[@]}"; do
        [[ "${R_RESULT[${i}]}" == "ВНИМАНИЕ" ]] && out+="${out:+, }${R_NAME[${i}]}"
    done
    printf '%s' "${out}"
}

# Уведомление в трее; из systemd user-сервиса указываем шину D-Bus явно
notify_alert() {
    local title="$1"
    local body="$2"
    local uid runtime_dir

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        say "[dry-run] будет: уведомление «${title}»"
        return 0
    fi
    command -v notify-send >/dev/null 2>&1 || return 0
    if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
        uid="$(id -u)"
        runtime_dir="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
        [[ -S "${runtime_dir}/bus" ]] && export DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime_dir}/bus"
    fi
    notify-send -a "${APP_NAME}" -i "${ICON_NAME}" -u "${URGENCY}" "${title}" "${body}" || true
}

###############################################################################
# ARGS
###############################################################################
# Внутренние root-шаги (запускаются этим же скриптом через sudo/pkexec)
case "${1:-}" in
    __root-install-units) shift; root_install_units "$@"; rc=$?; exit "${rc}" ;;
    __root-enable) shift; root_enable "$@"; rc=$?; exit "${rc}" ;;
    __root-pkg) shift; root_pkg "$@"; rc=$?; exit "${rc}" ;;
    __root-update) shift; root_update "$@"; rc=$?; exit "${rc}" ;;
    *) ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--status) STATUS_ONLY=1 ;;
        -n|--dry-run) DRY_RUN=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        --no-create) NO_CREATE=1 ;;
        --notify) NOTIFY=1 ;;
        -c|--configure) CONFIGURE=1 ;;
        --ask) FORCE_ASK=1 ;;
        --no-gui) USE_GUI=0 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "Неизвестный аргумент: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

###############################################################################
# MAIN
###############################################################################
mkdir -p -- "${LOG_DIR}"
apply_defaults
detect_family
select_ui
select_sudo

log_json "INFO" "start" "Проверка mytasks" "ОС: ${OS_PRETTY} (${OS_FAMILY}, ${PKG_MGR:-нет менеджера пакетов}), каталог: ${MYTASKS_DIR}, окна: ${UI_MODE}"
say "Система: ${OS_PRETTY} (семейство ${OS_FAMILY}${PKG_MGR:+, пакеты — ${PKG_MGR}}); каталог mytasks: ${MYTASKS_DIR}"
if [[ -n "${OS_NOTE}" ]]; then
    warn "${OS_NOTE}"
    log_json "WARN" "os_note" "${OS_NOTE}" "ID=${OS_ID}, ID_LIKE=${OS_LIKE}"
fi
if [[ "${CONF_PRESENT}" -eq 0 ]]; then
    FIRST_RUN=1
    warn "Нет ${CONF_REL} — первый запуск: используется каталог запуска ${SCRIPT_DIR}"
    log_json "WARN" "config_missing" "Нет конфига, первый запуск" "${CONF_FILE}"
fi
[[ "${SUDO_OK}" -eq 1 ]] || warn "Повышение прав недоступно (нет SUDO_ASKPASS, pkexec или терминала): системные юниты и пакеты не будут созданы."

eval_all
say ""
say "Состояние сервисов и заданий:"
print_jobs_table
say ""

if [[ "${STATUS_ONLY}" -eq 1 ]]; then
    print_deps_table all
else
    print_deps_table missing
    say ""

    # 0) Обязательное обновление системы — до любых изменений (кроме режима
    # только предупреждений: без терминала и без --yes ничего не меняется)
    if [[ "${UI_MODE}" != "none" || "${ASSUME_YES}" -eq 1 || "${DRY_RUN}" -eq 1 ]]; then
        system_update
    else
        say "Обновление системы пропущено: нет терминала или окон для подтверждения (запустите вручную или с --yes)."
    fi
    say ""

    # 1) Автозагрузка самого скрипта и 2) собственный конфиг с каталогом mytasks.
    # Если репозиторий перенесён (каталог в конфиге другой), сначала каталог.
    FIRST_STEPS=(0 1)
    [[ "${R_FIX[1]}" == "conf-dir" ]] && FIRST_STEPS=(1 0)
    for i in "${FIRST_STEPS[@]}"; do
        eval_job "${i}"
        handle_job "${i}"
        reload_conf
    done

    # Первый запуск — мастер всех конфигов по примерам
    if [[ "${STOP}" -eq 0 ]] && { [[ "${CONFIGURE}" -eq 1 ]] || [[ "${FIRST_RUN}" -eq 1 && "${NO_CREATE}" -eq 0 ]]; }; then
        if [[ "${UI_MODE}" != "none" || "${ASSUME_YES}" -eq 1 || "${DRY_RUN}" -eq 1 ]]; then
            say ""
            say "Мастер конфигов (значения по умолчанию подставлены из примеров):"
            configure_all
        fi
    fi

    # 3) Зависимости, 4) остальные задания
    say ""
    handle_deps
    eval_all
    for i in "${!ALL_JOBS[@]}"; do
        handle_job "${i}"
    done

    if [[ ( "${CHANGED}" -eq 1 || "${STOP}" -eq 1 ) && "${DRY_RUN}" -eq 0 ]]; then
        eval_all
        say ""
        say "Итоговое состояние:"
        print_jobs_table
    fi
fi

count_problems
deps_rows missing
TOTAL=$((PROBLEMS + DEP_MISSING_REQ))
say ""
if [[ "${TOTAL}" -eq 0 ]]; then
    say "Все сервисы и задания на месте."
    [[ "${REBOOT_NEEDED}" -eq 1 ]] && say "Перезагрузите компьютер: обновление системы требует перезапуска."
    log_json "INFO" "done" "Все сервисы и задания на месте" "" 0
    rc=0
else
    NAMES="$(problem_names)"
    say "Требуют внимания — заданий: ${PROBLEMS}${NAMES:+ (${NAMES})}; не хватает обязательных зависимостей: ${DEP_MISSING_REQ}."
    [[ "${STOP}" -eq 1 ]] && say "Запуск остановлен кнопкой «Стоп»; продолжить — запустить скрипт снова."
    [[ "${REBOOT_NEEDED}" -eq 1 ]] && say "Перезагрузите компьютер: обновление системы требует перезапуска."
    say "Создать недостающее: ${SCRIPT_PATH} (вопросы в окне), без вопросов: ${SCRIPT_PATH} --yes"
    log_json "WARN" "done" "Есть проблемы" "задания: ${NAMES}; зависимостей: ${DEP_MISSING_REQ}" 1
    if [[ "${NOTIFY}" -eq 1 ]]; then
        BODY="${NAMES}"
        [[ "${DEP_MISSING_REQ}" -gt 0 ]] && BODY+="${BODY:+; }нет обязательных зависимостей: ${DEP_MISSING_REQ}"
        BODY+=$'\n'"Создать недостающее: ${SCRIPT_NAME}"
        notify_alert "mytasks: требуют внимания" "${BODY}"
    fi
    rc=1
fi
cleanup_logs
exit "${rc}"
