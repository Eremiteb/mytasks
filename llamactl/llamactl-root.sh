#!/usr/bin/env bash
# llamactl-root.sh — root-хелпер llamactl: единственная программа, которую
# llamactl запускает через `sudo -A`. check_mytasks.sh ставит его копией
# root:root 0755 в /usr/local/lib/mytasks/llamactl-root (вне домашнего
# каталога), поэтому процессы пользователя не могут подменить код, который
# выполняется от root.
#
# Принципы:
#   * пользовательские конфиги (conf/llamactl.conf и т.п.) не читаются;
#     пути и юниты — зашитые значения по умолчанию или root-конфиг
#     /etc/mytasks/llamactl-root.conf (владелец root, без записи для группы и
#     остальных; разбирается без выполнения, только ключи PRESET_FILE,
#     LLAMA_PREFIX, SERVER_UNIT, FIM_UNIT, ROUTER_URL, FIM_URL,
#     HEALTH_TIMEOUT_SECS в виде КЛЮЧ="значение"; нужен, только если значения
#     отличаются от зашитых ниже);
#   * аргументы только сверяются с этими путями: записать можно лишь
#     models.ini, его резервные копии рядом и PREFIX/PREFIX.new/PREFIX.prev;
#   * файлы пользователя читаются с правами вызвавшего пользователя
#     (setpriv), поэтому через симлинк или жёсткую ссылку нельзя прочитать
#     чужой файл; из каталога сборки ничего не исполняется.
#
# Команды:
#   install-preset ИСТОЧНИК НАЗНАЧЕНИЕ   установить новый models.ini (НАЗНАЧЕНИЕ
#                                        должен совпасть с PRESET_FILE); печатает
#                                        путь резервной копии
#   restore-preset КОПИЯ                 вернуть models.ini из КОПИИ (PRESET_FILE.bak-*)
#                                        и перезапустить роутер
#   swap-install КАТАЛОГ ПРЕФИКС         поставить bin/ и lib/ из КАТАЛОГА (готовая
#                                        установка сборки) вместо ПРЕФИКСА (должен
#                                        совпасть с LLAMA_PREFIX), перезапустить
#                                        сервисы, при сбое — откат
#
# Тесты: LLAMACTL_ROOT_TEST=1 (только не под root) разрешает задать путь
# root-конфига в LLAMACTL_ROOT_CONF и доверять файлам текущего пользователя.
# sudo по умолчанию сбрасывает окружение (env_reset), а под root тестовый
# режим запрещён, поэтому подсунуть пути через окружение нельзя.
set -uo pipefail
umask 022

PROG="llamactl-root"

PRESET_FILE="/etc/llama.cpp/models.ini"
LLAMA_PREFIX="/opt/llama.cpp"
SERVER_UNIT="llama-server"
FIM_UNIT="llama-fim"
ROUTER_URL="http://127.0.0.1:8080"
FIM_URL="http://127.0.0.1:8012"
HEALTH_TIMEOUT_SECS="90"
# Предел размера models.ini: пресеты — текст в десятки КБ
MAX_PRESET_BYTES=1048576
ROOT_CONF="/etc/mytasks/llamactl-root.conf"
# Чьим файлам (root-конфиг) можно доверять
TRUSTED_UID=0

die() {
    printf '%s: %s\n' "${PROG}" "$1" >&2
    exit "${2:-1}"
}

if [[ "${LLAMACTL_ROOT_TEST:-}" == "1" ]]; then
    [[ "${EUID}" -ne 0 ]] || die "тестовый режим LLAMACTL_ROOT_TEST под root запрещён" 2
    ROOT_CONF="${LLAMACTL_ROOT_CONF:-${ROOT_CONF}}"
    TRUSTED_UID="${EUID}"
fi

# Пользователь, вызвавший sudo/pkexec (его файлы читаются с его правами)
CALLER_UID="${SUDO_UID:-${PKEXEC_UID:-${EUID}}}"
[[ "${CALLER_UID}" =~ ^[0-9]+$ ]] || die "некорректный UID вызвавшего пользователя" 2

###############################################################################
# ПРОВЕРКИ
###############################################################################
# Абсолютный путь из безопасных символов, без «.», «..» и пустых компонентов
safe_path() {
    local p="$1"
    [[ "${p}" =~ ^/[A-Za-z0-9._+/-]+$ ]] || return 1
    [[ "${p}" != */ && "${p}" != *//* ]] || return 1
    [[ "/${p}/" != */../* && "/${p}/" != */./* ]] || return 1
    return 0
}

# Файл можно считать доверенным: обычный, не симлинк, владелец TRUSTED_UID,
# без записи для группы и остальных
trusted_file() {
    local f="$1" st owner mode
    [[ -f "${f}" && ! -L "${f}" ]] || return 1
    st="$(stat -c '%u %a' -- "${f}")" || return 1
    owner="${st%% *}"
    mode="${st##* }"
    [[ "${owner}" == "${TRUSTED_UID}" ]] || return 1
    (( (8#${mode} & 8#022) == 0 ))
}

# Root-конфиг: KEY="VALUE" только для известных ключей, без выполнения
load_root_conf() {
    local line key val re='^([A-Z_]+)=(.*)$'
    [[ -e "${ROOT_CONF}" || -L "${ROOT_CONF}" ]] || return 0
    trusted_file "${ROOT_CONF}" \
        || die "${ROOT_CONF} не доверенный (нужен обычный файл root:root без записи для группы и остальных)" 2
    while IFS= read -r line || [[ -n "${line}" ]]; do
        [[ "${line}" =~ ${re} ]] || continue
        key="${BASH_REMATCH[1]}"
        val="${BASH_REMATCH[2]}"
        val="${val%\"}"
        val="${val#\"}"
        case "${key}" in
            PRESET_FILE) PRESET_FILE="${val}" ;;
            LLAMA_PREFIX) LLAMA_PREFIX="${val}" ;;
            SERVER_UNIT) SERVER_UNIT="${val}" ;;
            FIM_UNIT) FIM_UNIT="${val}" ;;
            ROUTER_URL) ROUTER_URL="${val}" ;;
            FIM_URL) FIM_URL="${val}" ;;
            HEALTH_TIMEOUT_SECS) HEALTH_TIMEOUT_SECS="${val}" ;;
            *) ;;
        esac
    done < "${ROOT_CONF}"
}

validate_settings() {
    local u re_url='^https?://[A-Za-z0-9.-]+(:[0-9]+)?$'
    safe_path "${PRESET_FILE}" || die "недопустимый PRESET_FILE в ${ROOT_CONF}" 2
    safe_path "${LLAMA_PREFIX}" || die "недопустимый LLAMA_PREFIX в ${ROOT_CONF}" 2
    [[ "${LLAMA_PREFIX}" != "/" ]] || die "LLAMA_PREFIX не может быть /" 2
    for u in "${SERVER_UNIT}" "${FIM_UNIT}"; do
        [[ "${u}" =~ ^[A-Za-z0-9@._-]+$ ]] || die "недопустимое имя юнита «${u}» в ${ROOT_CONF}" 2
    done
    for u in "${ROUTER_URL}" "${FIM_URL}"; do
        [[ "${u}" =~ ${re_url} ]] || die "недопустимый адрес «${u}» в ${ROOT_CONF}" 2
    done
    [[ "${HEALTH_TIMEOUT_SECS}" =~ ^[0-9]{1,4}$ ]] || die "недопустимый HEALTH_TIMEOUT_SECS в ${ROOT_CONF}" 2
}

# Чтение файла пользователя с его правами: под root через setpriv, иначе
# (тесты, запуск самим root) — напрямую
read_as_caller() {
    local f="$1" gid
    if [[ "${EUID}" -eq 0 && "${CALLER_UID}" != "0" ]]; then
        command -v setpriv >/dev/null 2>&1 || die "нет setpriv (util-linux): нельзя безопасно прочитать файл пользователя" 2
        gid="$(id -g -- "${CALLER_UID}")" || die "неизвестный пользователь UID ${CALLER_UID}" 2
        setpriv --reuid="${CALLER_UID}" --regid="${gid}" --clear-groups -- cat -- "${f}"
    else
        cat -- "${f}"
    fi
}

###############################################################################
# КОМАНДЫ
###############################################################################
TMP_FILE=""
cleanup() {
    [[ -n "${TMP_FILE}" ]] && rm -f -- "${TMP_FILE}"
    return 0
}
trap cleanup EXIT

# Атомарная замена models.ini содержимым файла $1 (уже под root, рядом с целью)
put_preset() {
    chmod 0644 -- "$1" || return 1
    mv -f -- "$1" "${PRESET_FILE}"
}

cmd_install_preset() {
    local src="${1:-}" dst="${2:-}" dir backup size rcs=()
    [[ $# -eq 2 ]] || die "install-preset ИСТОЧНИК НАЗНАЧЕНИЕ" 2
    [[ "${dst}" == "${PRESET_FILE}" ]] || die "путь «${dst}» вне разрешённых (разрешён только ${PRESET_FILE})" 2
    safe_path "${src}" || die "недопустимый путь источника «${src}»" 2
    [[ -f "${src}" && ! -L "${src}" ]] || die "источник ${src} — не обычный файл" 2
    dir="$(dirname -- "${PRESET_FILE}")"
    [[ -d "${dir}" && ! -L "${dir}" ]] || die "нет каталога ${dir}" 2
    TMP_FILE="$(mktemp -- "${dir}/.models.ini.XXXXXX")" || die "не удалось создать временный файл в ${dir}"
    # Копия под root с лимитом: дальше проверяется и ставится только она
    read_as_caller "${src}" | head -c "$((MAX_PRESET_BYTES + 1))" > "${TMP_FILE}"
    rcs=("${PIPESTATUS[@]}")
    ((rcs[1] == 0)) || die "не удалось записать копию ${TMP_FILE}"
    size="$(stat -c '%s' -- "${TMP_FILE}")" || die "не удалось проверить копию"
    # Чтение, прерванное лимитом head (SIGPIPE), — это «слишком большой», а не сбой
    ((size <= MAX_PRESET_BYTES)) || die "источник больше ${MAX_PRESET_BYTES} байт" 2
    ((rcs[0] == 0)) || die "не удалось прочитать ${src}"
    ((size > 0)) || die "источник пуст" 2
    backup="${PRESET_FILE}.bak-$(date '+%Y%m%d-%H%M%S')"
    if [[ -e "${PRESET_FILE}" ]]; then
        cp -p -- "${PRESET_FILE}" "${backup}" || die "не удалось сделать резервную копию ${backup}"
    fi
    put_preset "${TMP_FILE}" || die "не удалось установить ${PRESET_FILE}"
    TMP_FILE=""
    printf '%s\n' "${backup}"
}

cmd_restore_preset() {
    local backup="${1:-}" stamp dir
    [[ $# -eq 1 ]] || die "restore-preset КОПИЯ" 2
    stamp="${backup#"${PRESET_FILE}.bak-"}"
    [[ "${backup}" == "${PRESET_FILE}.bak-"* && "${stamp}" =~ ^[0-9]{8}-[0-9]{6}$ ]] \
        || die "путь «${backup}» вне разрешённых (только ${PRESET_FILE}.bak-ГГГГММДД-ЧЧММСС)" 2
    [[ -f "${backup}" && ! -L "${backup}" ]] || die "нет резервной копии ${backup}" 2
    dir="$(dirname -- "${PRESET_FILE}")"
    TMP_FILE="$(mktemp -- "${dir}/.models.ini.XXXXXX")" || die "не удалось создать временный файл в ${dir}"
    cat -- "${backup}" > "${TMP_FILE}" || die "не удалось прочитать ${backup}"
    put_preset "${TMP_FILE}" || die "не удалось восстановить ${PRESET_FILE}"
    TMP_FILE=""
    systemctl restart -- "${SERVER_UNIT}"
}

# Копия одного подкаталога (bin или lib) готовой установки: обычные файлы —
# чтением с правами пользователя, симлинки — только на соседний файл (без «/»),
# подкаталоги (cmake, pkgconfig) не нужны для работы и пропускаются
copy_tree_part() {
    local stage="$1" new="$2" sub="$3" mode="$4" listing entry type name target
    local re_name='^[A-Za-z0-9._+-]+$'
    mkdir -m 0755 -- "${new}/${sub}" || return 1
    [[ -d "${stage}/${sub}" && ! -L "${stage}/${sub}" ]] || return 0
    listing="$(find -P "${stage}/${sub}" -mindepth 1 -maxdepth 1 -printf '%y %f\n')" || return 1
    while IFS= read -r entry; do
        [[ -n "${entry}" ]] || continue
        type="${entry%% *}"
        name="${entry#* }"
        [[ "${name}" =~ ${re_name} ]] || { printf '%s: недопустимое имя «%s» в %s\n' "${PROG}" "${name}" "${sub}" >&2; return 1; }
        case "${type}" in
            f)
                read_as_caller "${stage}/${sub}/${name}" > "${new}/${sub}/${name}" || return 1
                chmod "${mode}" -- "${new}/${sub}/${name}" || return 1
                ;;
            l)
                target="$(readlink -- "${stage}/${sub}/${name}")" || return 1
                [[ "${target}" =~ ${re_name} ]] \
                    || { printf '%s: симлинк %s/%s ведёт за пределы каталога (%s)\n' "${PROG}" "${sub}" "${name}" "${target}" >&2; return 1; }
                ln -s -- "${target}" "${new}/${sub}/${name}" || return 1
                ;;
            d) ;;
            *) printf '%s: %s/%s — не файл и не симлинк\n' "${PROG}" "${sub}" "${name}" >&2; return 1 ;;
        esac
    done <<<"${listing}"
    # Все симлинки копии должны указывать на файлы этой же копии
    for entry in "${new}/${sub}"/*; do
        if [[ -L "${entry}" && ! -f "${entry}" ]]; then
            printf '%s: симлинк %s указывает на отсутствующий файл\n' "${PROG}" "${entry}" >&2
            return 1
        fi
    done
    return 0
}

services_up() {
    curl -fsS --max-time 3 -- "${ROUTER_URL}/health" >/dev/null 2>&1 \
        && curl -fsS --max-time 3 -- "${FIM_URL}/health" >/dev/null 2>&1
}

cmd_swap_install() {
    local stage="${1:-}" prefix="${2:-}" new prev t=0
    [[ $# -eq 2 ]] || die "swap-install КАТАЛОГ ПРЕФИКС" 2
    [[ "${prefix}" == "${LLAMA_PREFIX}" ]] || die "путь «${prefix}» вне разрешённых (разрешён только ${LLAMA_PREFIX})" 2
    safe_path "${stage}" || die "недопустимый путь установки сборки «${stage}»" 2
    [[ -d "${stage}" && ! -L "${stage}" ]] || die "нет каталога ${stage}" 2
    new="${LLAMA_PREFIX}.new"
    prev="${LLAMA_PREFIX}.prev"
    rm -rf -- "${new}"
    mkdir -m 0755 -- "${new}" || die "не удалось создать ${new}"
    if ! copy_tree_part "${stage}" "${new}" bin 0755 || ! copy_tree_part "${stage}" "${new}" lib 0644; then
        rm -rf -- "${new}"
        die "копирование сборки не удалось"
    fi
    if [[ ! -f "${new}/bin/llama-server" || -L "${new}/bin/llama-server" ]]; then
        rm -rf -- "${new}"
        die "в ${stage}/bin нет llama-server"
    fi
    rm -rf -- "${prev}"
    if [[ -e "${LLAMA_PREFIX}" ]]; then
        mv -- "${LLAMA_PREFIX}" "${prev}" || die "не удалось убрать ${LLAMA_PREFIX}"
    fi
    mv -- "${new}" "${LLAMA_PREFIX}" || die "не удалось поставить ${LLAMA_PREFIX}"
    systemctl restart -- "${SERVER_UNIT}" "${FIM_UNIT}"
    while ((t < HEALTH_TIMEOUT_SECS)); do
        if services_up; then
            printf '  сервисы отвечают (%s с)\n' "${t}"
            return 0
        fi
        sleep 3
        t=$((t + 3))
    done
    printf '%s: сервисы не поднялись — откат на %s\n' "${PROG}" "${prev}" >&2
    if [[ -e "${prev}" ]]; then
        rm -rf -- "${new}"
        mv -- "${LLAMA_PREFIX}" "${new}" && mv -- "${prev}" "${LLAMA_PREFIX}"
        systemctl restart -- "${SERVER_UNIT}" "${FIM_UNIT}"
    fi
    return 1
}

###############################################################################
# MAIN
###############################################################################
load_root_conf
validate_settings

CMD="${1:-}"
(($# > 0)) && shift
case "${CMD}" in
    install-preset) cmd_install_preset "$@" ;;
    restore-preset) cmd_restore_preset "$@" ;;
    swap-install) cmd_swap_install "$@" ;;
    *) die "неизвестная команда «${CMD}» (install-preset|restore-preset|swap-install)" 2 ;;
esac
