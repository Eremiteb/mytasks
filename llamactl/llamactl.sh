#!/usr/bin/env bash
# llamactl.sh — управление локальным llama.cpp независимо от редакторов и
# других приложений: статус, модели, сервисы, логи, пресеты, добавление
# моделей с Hugging Face, замер скорости и обновление сборки llama.cpp.
#
# Работает с роутером llama-server (llama-server.service, :8080), FIM-сервером
# автодополнения (llama-fim.service, :8012) и пресетами /etc/llama.cpp/models.ini.
# Root-шаги выполняются через `sudo -A` (графическое окно пароля), не больше
# одного запроса пароля на действие.
#
# Без аргументов в терминале открывает интерактивное меню.
# Не используем set -e: ошибки отдельных проверок обрабатываются явно.
set -uo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="llamactl"
SCRIPT_PATH="$(readlink -f -- "$0")"
SCRIPT_DIR="$(dirname -- "${SCRIPT_PATH}")"
# Код llamactl — в подкаталоге llamactl/, а conf/, logs/ и state/ общие для
# всех скриптов и лежат в корне репозитория
REPO_DIR="$(dirname -- "${SCRIPT_DIR}")"
CONF_DIR="${REPO_DIR}/conf"
CONF_FILE="${CONF_DIR}/llamactl.conf"
LOG_DIR="${MYTASKS_LOG_DIR:-${REPO_DIR}/logs}"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${CONF_DIR}/log_template.conf"

# Значения по умолчанию (переопределяются conf/llamactl.conf)
ROUTER_URL="http://127.0.0.1:8080"
FIM_URL="http://127.0.0.1:8012"
SERVER_UNIT="llama-server"
FIM_UNIT="llama-fim"
SERVICE_USER="llama"
PRESET_FILE="/etc/llama.cpp/models.ini"
MODELS_DIR="/var/lib/llama.cpp/models"
LLAMA_PREFIX="/opt/llama.cpp"
LLAMA_SRC="${HOME}/.local/src/llama.cpp"
CUDA_ARCH="86"
BUILD_JOBS="14"
BENCH_MODEL="qwen3.5-9b"
HF_BASE="https://huggingface.co"
LOAD_TIMEOUT_SECS="900"
HEALTH_TIMEOUT_SECS="90"
LOG_KEEP="10"
SUDO_CMD="sudo -A"
# Блокировка записи models.ini (flock): параллельные add/remove/preset ждут
# друг друга и не затирают чужие правки
PRESET_LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/llamactl-models-ini.lock"
PRESET_LOCK_TIMEOUT_SECS="600"

if [[ -r "${CONF_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${CONF_FILE}"
fi
if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

read -r -a SUDO <<<"${SUDO_CMD}"
# Литерал «$ORIGIN/../lib» для RPATH сборки: библиотеки ищутся рядом с бинарником.
RPATH_LIT="$(printf '%sORIGIN/../lib' '$')"
DRY_RUN=0
JSON_OUT=0
NO_LOG=0
PRESET_LOCK_FD=""

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g' | tr '\n' ' '
}

log_json() {
    local level="$1" event="$2" msg="$3" detail="${4:-}"
    [[ "${NO_LOG}" -eq 1 ]] && return 0
    local msg_esc detail_esc level_norm timestamp
    msg_esc="$(json_escape "${msg}")"
    detail_esc="$(json_escape "${detail}")"
    level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
    timestamp="$(ts)"
    mkdir -p "${LOG_DIR}" 2>/dev/null || return 0
    printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","dry_run":%s}\n' \
        "${timestamp}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" "${DRY_RUN}" >> "${LOG_FILE}" 2>/dev/null || true
}

cleanup_logs() {
    [[ "${NO_LOG}" -eq 1 ]] && return 0
    [[ -d "${LOG_DIR}" ]] || return 0
    local old_logs=() listing old
    listing="$(find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null)"
    old="$(sort -nr <<<"${listing}" | awk -F'|' -v keep="${LOG_KEEP}" 'NR > keep { print $2 }')"
    [[ -n "${old}" ]] && mapfile -t old_logs <<<"${old}"
    if ((${#old_logs[@]} > 0)); then
        rm -f -- "${old_logs[@]}"
    fi
}

info() { printf '%s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
die() {
    local rc="${2:-1}"
    printf 'Ошибка: %s\n' "$1" >&2
    log_json "ERROR" "fail" "$1"
    exit "${rc}"
}

# Действие, меняющее систему: в dry-run печатает план и пишет dry_<event>.
# Возвращает 0, если действие нужно выполнить, и 1 в dry-run.
act() {
    local event="$1" desc="$2"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        printf '[dry-run] будет: %s\n' "${desc}"
        log_json "INFO" "dry_${event}" "${desc}"
        return 1
    fi
    log_json "INFO" "${event}" "${desc}"
    return 0
}

need_cmd() {
    local c
    for c in "$@"; do
        command -v "${c}" >/dev/null 2>&1 || die "не найдена программа «${c}»" 2
    done
}

human_bytes() {
    local b="${1:-0}"
    awk -v b="${b}" 'BEGIN {
        split("Б КиБ МиБ ГиБ ТиБ", u, " "); i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i]
    }'
}

api_get() {
    curl -fsS --max-time "${2:-10}" "${ROUTER_URL}$1"
}

api_post() {
    curl -fsS --max-time "${3:-30}" -H 'Content-Type: application/json' -d "$2" "${ROUTER_URL}$1"
}

router_up() { curl -fsS --max-time 3 "${ROUTER_URL}/health" >/dev/null 2>&1; }
fim_up() { curl -fsS --max-time 3 "${FIM_URL}/health" >/dev/null 2>&1; }

unit_state() {
    local st
    st="$(systemctl is-active "$1" 2>/dev/null)"
    printf '%s' "${st:-unknown}"
}

# JSON со списком моделей роутера: [{id,status,ctx}]
models_json() {
    local raw
    if ! raw="$(api_get /models 2>/dev/null)"; then
        printf '[]'
        return 1
    fi
    jq -c '[.data[] | {id, status: (.status.value // "unknown"),
            ctx: ((.status.args // []) | join(" ") | capture("--ctx-size (?<c>[0-9]+)").c // null)}]' <<<"${raw}"
}

model_status() {
    local mj
    mj="$(models_json)"
    M="$1" jq -r '(.[] | select(.id == env.M) | .status) // "absent"' <<<"${mj}"
}

require_model() {
    local st
    st="$(model_status "$1")"
    [[ "${st}" == "absent" ]] && die "модели «$1» нет в роутере (см. llamactl models)" 2
    return 0
}

# Сторонние llama-server (не сервисные): pid|user|port|cmd
foreign_procs() {
    local list pid user args port
    list="$(ps -eo pid=,user=,args= 2>/dev/null)"
    while read -r pid user args; do
        [[ -z "${pid}" || "${user}" == "${SERVICE_USER}" ]] && continue
        [[ "${args}" =~ (^|/)llama-server([[:space:]]|$) || "${args}" =~ (^|/)llama[[:space:]]+serve([[:space:]]|$) ]] || continue
        port="$(sed -nE 's/.*--port[ =]([0-9]+).*/\1/p' <<<"${args}")"
        printf '%s|%s|%s|%s\n' "${pid}" "${user}" "${port:--}" "${args:0:160}"
    done <<<"${list}"
}

gpu_mem() {
    command -v nvidia-smi >/dev/null 2>&1 || { printf '0|0|0'; return 0; }
    local q
    q="$(nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)"
    q="${q// /}"
    printf '%s' "${q//,/|}"
}

wait_health() {
    local what="$1" url="$2" t=0
    while ((t < HEALTH_TIMEOUT_SECS)); do
        curl -fsS --max-time 3 "${url}/health" >/dev/null 2>&1 && { info "  ${what}: отвечает (${t} с)"; return 0; }
        sleep 2
        t=$((t + 2))
        ((t % 10 == 0)) && info "  ${what}: ждём ответа… ${t} с"
    done
    warn "${what} не ответил за ${HEALTH_TIMEOUT_SECS} с"
    return 1
}

units_for() {
    case "${1:-all}" in
        server) printf '%s\n' "${SERVER_UNIT}" ;;
        fim) printf '%s\n' "${FIM_UNIT}" ;;
        all) printf '%s\n%s\n' "${SERVER_UNIT}" "${FIM_UNIT}" ;;
        *) die "неизвестная цель «$1» (server|fim|all)" 2 ;;
    esac
}

confirm() {
    local ans
    [[ -t 0 ]] || return 0
    read -r -p "$1 [y/N] " ans
    [[ "${ans}" =~ ^[yYдД]$ ]]
}

###############################################################################
# КОМАНДЫ: статус
###############################################################################
# Статус собирает llamactl_status.py (HTTP, /proc, NVML — без десятков
# процессов curl/jq/ps на вызов); здесь — только вывод
status_json() {
    local py
    py="$(command -v python3)" || die "для статуса нужен python3" 2
    "${py}" "${SCRIPT_DIR}/llamactl_status.py" --router-url "${ROUTER_URL}" --fim-url "${FIM_URL}" \
        --server-unit "${SERVER_UNIT}" --fim-unit "${FIM_UNIT}" --service-user "${SERVICE_USER}" \
        --models-dir "${MODELS_DIR}"
}

cmd_status() {
    local sj lines nums foreign_lines
    local srv fim rup fup used total util ram_total ram_used ram_avail swap_used disk_avail
    sj="$(status_json)" || die "не удалось получить статус" 1
    if [[ "${JSON_OUT}" -eq 1 ]]; then
        printf '%s\n' "${sj}"
        return 0
    fi

    nums="$(jq -r '[.services.server, .services.fim, .router.up, .fim.up, .gpu.used_mib, .gpu.total_mib, .gpu.util,
        .ram.total, .ram.used, .ram.available, .ram.swap_used, .disk.models_avail] | map(tostring) | join("|")' <<<"${sj}")"
    IFS='|' read -r srv fim rup fup used total util ram_total ram_used ram_avail swap_used disk_avail <<<"${nums}"
    info "Сервисы: ${SERVER_UNIT}=${srv} (API ${rup}), ${FIM_UNIT}=${fim} (API ${fup})"
    info "Модели роутера (${ROUTER_URL}):"
    lines="$(jq -r '.models[] | "  \(if .status == "loaded" then "●" elif .status == "sleeping" then "◐" elif .status == "loading" then "…" else "○" end) \(.id)  [\(.status)\(if .ctx then ", окно \(.ctx)" else "" end)]"' <<<"${sj}")"
    if [[ -z "${lines}" ]]; then
        info "  — нет данных (роутер недоступен?)"
    else
        printf '%s\n' "${lines}"
    fi
    info "GPU: ${used}/${total} МиБ VRAM, загрузка ${util}%"
    lines="$(jq -r '.gpu.procs[] | "  \(.mib) МиБ  \(.kind)  pid \(.pid) (\(.user)): \(.cmd[0:90])"' <<<"${sj}")"
    [[ -n "${lines}" ]] && printf '%s\n' "${lines}"
    local ram_u ram_t ram_a sw da
    ram_u="$(human_bytes "${ram_used}")"; ram_t="$(human_bytes "${ram_total}")"; ram_a="$(human_bytes "${ram_avail}")"
    sw="$(human_bytes "${swap_used}")"; da="$(human_bytes "${disk_avail}")"
    info "RAM: занято ${ram_u} из ${ram_t}, доступно ${ram_a}; swap ${sw}"
    info "Диск моделей ${MODELS_DIR}: свободно ${da}"
    foreign_lines="$(jq -r '.foreign[] | "  pid \(.pid) (\(.user)) порт \(.port): \(.cmd[0:100])"' <<<"${sj}")"
    if [[ -n "${foreign_lines}" ]]; then
        warn "сторонние llama-server (не сервисы, мешают VRAM):"
        printf '%s\n' "${foreign_lines}" >&2
        info "  убрать: llamactl kill-foreign"
    fi
    return 0
}

cmd_models() {
    local mj
    mj="$(models_json)" || die "роутер ${ROUTER_URL} недоступен"
    if [[ "${JSON_OUT}" -eq 1 ]]; then
        printf '%s\n' "${mj}"
        return 0
    fi
    jq -r '.[] | "\(.id)\t\(.status)\t\(.ctx // "-")"' <<<"${mj}" | column -t -s $'\t' -N 'МОДЕЛЬ,СОСТОЯНИЕ,ОКНО'
}

###############################################################################
# КОМАНДЫ: загрузка/выгрузка
###############################################################################
cmd_load() {
    local model="${1:-}"
    [[ -n "${model}" ]] || die "укажите модель: llamactl load МОДЕЛЬ" 2
    router_up || die "роутер ${ROUTER_URL} недоступен (llamactl start server)"
    require_model "${model}"
    local st
    st="$(model_status "${model}")"
    if [[ "${st}" == "loaded" ]]; then
        info "${model} уже загружена"
        return 0
    fi
    act "load" "загрузить модель ${model} в роутер" || return 0
    local body resp
    body="$(M="${model}" jq -n -c '{model: env.M}')"
    resp="$(api_post /models/load "${body}" 30 2>&1)" || die "роутер отказал: ${resp}"
    local t=0 gm used total
    while ((t < LOAD_TIMEOUT_SECS)); do
        st="$(model_status "${model}")"
        case "${st}" in
            loaded)
                info "${model}: загружена за ${t} с"
                log_json "INFO" "loaded" "${model}" "${t}s"
                return 0
                ;;
            loading|unloaded|sleeping) ;;
            *) ;;
        esac
        sleep 3
        t=$((t + 3))
        gm="$(gpu_mem)"
        IFS='|' read -r used total _ <<<"${gm}"
        info "  загрузка ${model}: ${t} с, состояние ${st}, VRAM ${used}/${total} МиБ"
        if [[ "${st}" == "unloaded" && "${t}" -ge 15 ]]; then
            die "${model} не загрузилась (см. llamactl logs server)"
        fi
    done
    die "${model} не загрузилась за ${LOAD_TIMEOUT_SECS} с"
}

cmd_unload() {
    local target="${1:-}" mj ids=() id
    [[ -n "${target}" ]] || die "укажите модель или all: llamactl unload МОДЕЛЬ|all" 2
    router_up || die "роутер ${ROUTER_URL} недоступен"
    if [[ "${target}" == "all" ]]; then
        mj="$(models_json)"
        local busy
        busy="$(jq -r '.[] | select(.status != "unloaded") | .id' <<<"${mj}")"
        [[ -n "${busy}" ]] && mapfile -t ids <<<"${busy}"
    else
        require_model "${target}"
        ids=("${target}")
    fi
    if ((${#ids[@]} == 0)); then
        info "загруженных моделей нет"
        return 0
    fi
    local body
    for id in "${ids[@]}"; do
        act "unload" "выгрузить модель ${id}" || continue
        body="$(M="${id}" jq -n -c '{model: env.M}')"
        if ! api_post /models/unload "${body}" 30 >/dev/null 2>&1; then
            warn "${id}: роутер не выгрузил модель"
            continue
        fi
        # Выгрузка асинхронная: ждём, пока роутер освободит память.
        local t=0 st
        st="$(model_status "${id}")"
        while [[ "${st}" != "unloaded" && "${t}" -lt 30 ]]; do
            sleep 1
            t=$((t + 1))
            st="$(model_status "${id}")"
        done
        if [[ "${st}" == "unloaded" ]]; then
            info "${id}: выгружена (${t} с)"
        else
            warn "${id}: за 30 с не выгрузилась (состояние ${st})"
        fi
    done
    return 0
}

cmd_free_vram() {
    local before after
    before="$(gpu_mem)"
    info "VRAM до: ${before%%|*} МиБ"
    cmd_unload all
    local fst
    fst="$(unit_state "${FIM_UNIT}")"
    if [[ "${fst}" == "active" ]]; then
        if act "stop_fim" "остановить ${FIM_UNIT} (освободит ~1.3 ГБ VRAM; вернуть: llamactl start fim)"; then
            "${SUDO[@]}" systemctl stop "${FIM_UNIT}" || warn "не удалось остановить ${FIM_UNIT}"
        fi
    fi
    local fp
    fp="$(foreign_procs)"
    [[ -n "${fp}" ]] && warn "есть сторонние llama-server — убрать их: llamactl kill-foreign"
    [[ "${DRY_RUN}" -eq 1 ]] && return 0
    sleep 2
    after="$(gpu_mem)"
    info "VRAM после: ${after%%|*} МиБ"
}

###############################################################################
# КОМАНДЫ: сервисы и логи
###############################################################################
cmd_service() {
    local action="$1" target="${2:-all}" units=() ulist
    ulist="$(units_for "${target}")" || exit 2
    mapfile -t units <<<"${ulist}"
    act "service_${action}" "systemctl ${action} ${units[*]}" || return 0
    "${SUDO[@]}" systemctl "${action}" "${units[@]}" || die "systemctl ${action} ${units[*]} завершился ошибкой"
    [[ "${action}" == "stop" ]] && { info "остановлено: ${units[*]}"; return 0; }
    local rc=0 u
    for u in "${units[@]}"; do
        if [[ "${u}" == "${SERVER_UNIT}" ]]; then
            wait_health "роутер" "${ROUTER_URL}" || rc=1
        elif [[ "${u}" == "${FIM_UNIT}" ]]; then
            wait_health "FIM" "${FIM_URL}" || rc=1
        fi
    done
    return "${rc}"
}

cmd_logs() {
    local target="server" follow=0 lines=60 unit
    while (($# > 0)); do
        case "$1" in
            server|fim) target="$1" ;;
            -f|--follow) follow=1 ;;
            -n) shift; lines="${1:-60}" ;;
            *) die "неизвестный аргумент logs: $1" 2 ;;
        esac
        shift
    done
    unit="$(units_for "${target}")"
    if [[ "${follow}" -eq 1 ]]; then
        journalctl -u "${unit}" -n "${lines}" -f
    else
        journalctl -u "${unit}" -n "${lines}" --no-pager
    fi
}

cmd_kill_foreign() {
    local fp pid user port cmd rc=0
    fp="$(foreign_procs)"
    if [[ -z "${fp}" ]]; then
        info "сторонних llama-server нет"
        return 0
    fi
    while IFS='|' read -r pid user port cmd; do
        if [[ "${user}" != "${USER}" ]]; then
            warn "pid ${pid} принадлежит ${user} — пропущен (нужны права root)"
            rc=1
            continue
        fi
        act "kill_foreign" "завершить pid ${pid} (порт ${port}): ${cmd:0:80}" || continue
        kill -TERM "${pid}" 2>/dev/null || true
        local t=0
        while kill -0 "${pid}" 2>/dev/null && ((t < 10)); do sleep 1; t=$((t + 1)); done
        kill -0 "${pid}" 2>/dev/null && kill -KILL "${pid}" 2>/dev/null
        info "pid ${pid}: завершён"
    done <<<"${fp}"
    return "${rc}"
}

###############################################################################
# КОМАНДЫ: проверка (doctor)
###############################################################################
cmd_doctor() {
    local problems=0 st
    info "Проверка llama.cpp:"
    for u in "${SERVER_UNIT}" "${FIM_UNIT}"; do
        st="$(unit_state "${u}")"
        if [[ "${st}" == "active" ]]; then info "  ✓ ${u}: active"; else info "  ✗ ${u}: ${st}"; problems=$((problems + 1)); fi
    done
    if router_up; then info "  ✓ роутер отвечает (${ROUTER_URL})"; else info "  ✗ роутер не отвечает"; problems=$((problems + 1)); fi
    if fim_up; then info "  ✓ FIM отвечает (${FIM_URL})"; else info "  ✗ FIM не отвечает"; problems=$((problems + 1)); fi
    if [[ -r "${PRESET_FILE}" ]]; then
        info "  ✓ пресеты читаются: ${PRESET_FILE}"
        local paths p
        paths="$(awk -F'=' '/^[[:space:]]*(model|mmproj)[[:space:]]*=/ { v = $2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v }' "${PRESET_FILE}")"
        while IFS= read -r p; do
            [[ -z "${p}" ]] && continue
            if [[ -r "${p}" ]]; then info "  ✓ файл модели: ${p}"; else info "  ✗ нет файла модели: ${p}"; problems=$((problems + 1)); fi
        done <<<"${paths}"
    else
        info "  ✗ нет доступа к ${PRESET_FILE}"; problems=$((problems + 1))
    fi
    local fp
    fp="$(foreign_procs)"
    if [[ -n "${fp}" ]]; then
        info "  ✗ сторонние llama-server (llama-vscode env и т.п.) — llamactl kill-foreign:"
        local pid user port cmd
        while IFS='|' read -r pid user port cmd; do info "      pid ${pid} порт ${port}: ${cmd:0:90}"; done <<<"${fp}"
        problems=$((problems + 1))
    else
        info "  ✓ сторонних llama-server нет"
    fi
    local d sz
    for d in "${HOME}/.cache/huggingface/hub" "${HOME}/.cache/llama.cpp"; do
        if [[ -d "${d}" ]]; then
            sz="$(du -sb "${d}" 2>/dev/null | cut -f1)"
            if [[ "${sz:-0}" -gt 104857600 ]]; then
                local hs
                hs="$(human_bytes "${sz}")"
                info "  ! кеш сторонних загрузок ${d}: ${hs} (можно удалить)"
            fi
        fi
    done
    local da
    da="$(df -B1 --output=avail "${MODELS_DIR}" 2>/dev/null | tail -1 | tr -d ' ')"
    if [[ "${da:-0}" -lt 21474836480 ]]; then
        info "  ! на диске моделей меньше 20 ГиБ свободно"
    fi
    if [[ -x "${LLAMA_PREFIX}/bin/llama-server" ]]; then
        local ver
        ver="$("${LLAMA_PREFIX}/bin/llama-server" --version 2>&1 | grep -m1 -E '^version')"
        info "  ✓ ${LLAMA_PREFIX}: ${ver}"
    else
        info "  ✗ нет ${LLAMA_PREFIX}/bin/llama-server"; problems=$((problems + 1))
    fi
    log_json "INFO" "doctor" "проблем: ${problems}"
    if ((problems > 0)); then
        info "Итог: проблем ${problems}"
        return 1
    fi
    info "Итог: всё в порядке"
    return 0
}

###############################################################################
# КОМАНДЫ: пресеты
###############################################################################
preset_section() {
    awk -v s="[$1]" '
        $0 == s { on = 1; print; next }
        on && /^\[/ { exit }
        on { print }' "${PRESET_FILE}"
}

preset_has_section() {
    grep -qxF "[$1]" "${PRESET_FILE}"
}

# Пишет в stdout пресет с установленным/удалённым ключом в секции.
preset_render_set() {
    local section="$1" key="$2" value="$3" mode="$4"
    awk -v s="[${section}]" -v k="${key}" -v v="${value}" -v mode="${mode}" '
        function flush() {
            if (on && !done && mode == "set") {
                for (i = 1; i <= nb; i++) if (buf[i] != "") last = i
                for (i = 1; i <= last; i++) print buf[i]
                print k " = " v
                for (i = last + 1; i <= nb; i++) print buf[i]
            } else {
                for (i = 1; i <= nb; i++) print buf[i]
            }
            nb = 0; last = 0
        }
        $0 == s { on = 1; print; next }
        on && /^\[/ { flush(); on = 0; print; next }
        on {
            line = $0
            if (match(line, "^[[:space:]]*" k "[[:space:]]*=")) {
                done = 1
                if (mode == "set") { buf[++nb] = k " = " v }
                next
            }
            buf[++nb] = line
            next
        }
        { print }
        END { if (on) flush() }' "${PRESET_FILE}"
}

# Устанавливает новый пресет (один запрос пароля), перечитывает роутер,
# при ошибке откатывает из резервной копии.
preset_apply() {
    local new_file="$1" what="$2"
    if cmp -s "${new_file}" "${PRESET_FILE}"; then
        info "изменений нет"
        return 0
    fi
    diff -u --label "${PRESET_FILE}" --label "новый" "${PRESET_FILE}" "${new_file}"
    act "preset_apply" "установить ${PRESET_FILE} (${what}) с резервной копией и перечитать роутер" || return 0
    local backup
    backup="${PRESET_FILE}.bak-$(date '+%Y%m%d-%H%M%S')"
    "${SUDO[@]}" "${SCRIPT_PATH}" __root-install-preset "${new_file}" "${PRESET_FILE}" "${backup}" \
        || die "не удалось установить пресет"
    info "резервная копия: ${backup}"
    if router_up && api_get '/models?reload=1' >/dev/null 2>&1; then
        info "роутер перечитал пресеты"
        return 0
    fi
    warn "роутер не принял пресет — перезапускаю ${SERVER_UNIT}"
    if "${SUDO[@]}" systemctl restart "${SERVER_UNIT}" && wait_health "роутер" "${ROUTER_URL}"; then
        return 0
    fi
    warn "роутер не поднялся — откатываю пресет"
    "${SUDO[@]}" "${SCRIPT_PATH}" __root-restore-preset "${backup}" "${PRESET_FILE}" "${SERVER_UNIT}"
    die "пресет откатан из ${backup}"
}

# Эксклюзивная блокировка записи пресетов. Чтение models.ini для новой версии
# и её установка должны идти между preset_lock и preset_unlock, иначе
# параллельный llamactl затрёт правку. При die блокировка снимается с выходом
# процесса. В dry-run файл не меняется, поэтому блокировка не берётся.
preset_lock() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        return 0
    fi
    need_cmd flock
    exec {PRESET_LOCK_FD}>>"${PRESET_LOCK_FILE}" || die "не удалось открыть ${PRESET_LOCK_FILE}"
    flock -w "${PRESET_LOCK_TIMEOUT_SECS}" "${PRESET_LOCK_FD}" \
        || die "пресеты заняты другим llamactl дольше ${PRESET_LOCK_TIMEOUT_SECS} с"
}

preset_unlock() {
    if [[ -n "${PRESET_LOCK_FD}" ]]; then
        exec {PRESET_LOCK_FD}>&-
        PRESET_LOCK_FD=""
    fi
}

# Секция проверяется и пресет строится по актуальному файлу под блокировкой
preset_set_key() {
    local section="$1" key="$2" value="$3" mode="$4" tmp rc=0
    preset_lock
    preset_has_section "${section}" || die "секции [${section}] нет в ${PRESET_FILE}" 2
    tmp="$(mktemp)"
    preset_render_set "${section}" "${key}" "${value}" "${mode}" > "${tmp}"
    preset_apply "${tmp}" "${mode} [${section}] ${key}" || rc=$?
    rm -f -- "${tmp}"
    preset_unlock
    return "${rc}"
}

# Правка в редакторе длится сколько угодно, поэтому блокировка берётся только
# на установку. Если за это время models.ini изменил кто-то другой, правка не
# применяется (иначе затёрла бы чужие изменения) и остаётся во временном файле.
preset_apply_edit() {
    local tmp="$1" before="$2" now rc=0
    preset_lock
    now="$(sha256sum < "${PRESET_FILE}")"
    [[ "${now}" == "${before}" ]] \
        || die "${PRESET_FILE} изменился во время правки — она не применена и сохранена в ${tmp}; повторите preset edit"
    preset_apply "${tmp}" "ручная правка" || rc=$?
    preset_unlock
    return "${rc}"
}

cmd_preset() {
    local sub="${1:-list}"
    shift || true
    [[ -r "${PRESET_FILE}" ]] || die "нет доступа к ${PRESET_FILE}" 2
    case "${sub}" in
        list)
            grep -E '^\[' "${PRESET_FILE}" | tr -d '[]'
            ;;
        show)
            [[ -n "${1:-}" ]] || die "llamactl preset show МОДЕЛЬ" 2
            preset_has_section "$1" || die "секции [$1] нет в ${PRESET_FILE}" 2
            preset_section "$1"
            ;;
        set|unset)
            local section="${1:-}" key="${2:-}" value="${3:-}"
            [[ -n "${section}" && -n "${key}" ]] || die "llamactl preset ${sub} МОДЕЛЬ КЛЮЧ${sub/unset/}${sub/set/ ЗНАЧЕНИЕ}" 2
            [[ "${sub}" == "set" && -z "${value}" ]] && die "llamactl preset set МОДЕЛЬ КЛЮЧ ЗНАЧЕНИЕ" 2
            [[ "${key}" =~ ^[A-Za-z0-9_-]+$ ]] || die "недопустимый ключ «${key}»" 2
            [[ "${value}" != *$'\n'* ]] || die "значение не может содержать перевод строки" 2
            preset_set_key "${section}" "${key}" "${value}" "${sub}"
            ;;
        edit)
            local tmp before
            tmp="$(mktemp --suffix=.ini)"
            cp -- "${PRESET_FILE}" "${tmp}"
            before="$(sha256sum < "${PRESET_FILE}")"
            "${EDITOR:-nano}" "${tmp}"
            preset_apply_edit "${tmp}" "${before}"
            rm -f -- "${tmp}"
            ;;
        *)
            die "неизвестная подкоманда preset: ${sub} (list|show|set|unset|edit)" 2
            ;;
    esac
}

###############################################################################
# КОМАНДЫ: fit, bench
###############################################################################
cmd_fit() {
    local model="${1:-}" ctx="${2:-}"
    [[ -n "${model}" ]] || die "llamactl fit МОДЕЛЬ [ОКНО]" 2
    preset_has_section "${model}" || die "секции [${model}] нет в ${PRESET_FILE}" 2
    local sect path pctx ctk ctv fitt
    sect="$(preset_section "${model}")"
    path="$(awk -F'=' '/^[[:space:]]*model[[:space:]]*=/ { v = $2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v }' <<<"${sect}")"
    pctx="$(awk -F'=' '/^[[:space:]]*ctx-size[[:space:]]*=/ { v = $2; gsub(/[[:space:]]/, "", v); print v }' <<<"${sect}")"
    ctk="$(awk -F'=' '/^[[:space:]]*cache-type-k[[:space:]]*=/ { v = $2; gsub(/[[:space:]]/, "", v); print v }' "${PRESET_FILE}" | head -1)"
    ctv="$(awk -F'=' '/^[[:space:]]*cache-type-v[[:space:]]*=/ { v = $2; gsub(/[[:space:]]/, "", v); print v }' "${PRESET_FILE}" | head -1)"
    fitt="$(awk -F'=' '/^[[:space:]]*fit-target[[:space:]]*=/ { v = $2; gsub(/[[:space:]]/, "", v); print v }' "${PRESET_FILE}" | head -1)"
    [[ -n "${pctx}" ]] || pctx="$(awk -F'=' '/^[[:space:]]*ctx-size[[:space:]]*=/ { v = $2; gsub(/[[:space:]]/, "", v); print v; exit }' "${PRESET_FILE}")"
    ctx="${ctx:-${pctx:-32768}}"
    [[ -r "${path}" ]] || die "нет файла модели ${path}"
    local gm used total out ngl ot
    gm="$(gpu_mem)"
    IFS='|' read -r used total _ <<<"${gm}"
    info "Раскладка ${model} при окне ${ctx} (сейчас занято VRAM ${used}/${total} МиБ — расчёт учитывает это):"
    out="$("${LLAMA_PREFIX}/bin/llama-fit-params" -m "${path}" -c "${ctx}" -ctk "${ctk:-q8_0}" -ctv "${ctv:-q8_0}" -fa on -fitt "${fitt:-512}" 2>/dev/null | tail -1)"
    [[ -n "${out}" ]] || die "llama-fit-params не дал результата"
    ngl="$(sed -nE 's/.*-ngl (-?[0-9]+).*/\1/p' <<<"${out}")"
    ot="$(grep -oE 'blk\\\.[0-9]+\\\.ffn' <<<"${out}" | sort -u | wc -l)"
    if [[ "${ngl}" == "-1" ]]; then
        info "  все слои на GPU"
    else
        info "  слоёв на GPU: ${ngl}"
    fi
    ((ot > 0)) && info "  слоёв с экспертами MoE на CPU: ${ot}"
    log_json "INFO" "fit" "${model} ctx=${ctx}" "${out:0:200}"
}

cmd_bench() {
    local model="${1:-${BENCH_MODEL}}"
    router_up || die "роутер ${ROUTER_URL} недоступен"
    require_model "${model}"
    act "bench" "загрузить ${model} (если не загружена) и сгенерировать 2×256 токенов" || return 0
    cmd_load "${model}"
    local body r tg pp acc
    body="$(M="${model}" jq -n -c '{model: env.M, max_tokens: 256, temperature: 0,
        messages: [{role: "user", content: "Напиши на Bash функцию, которая проверяет доступность списка хостов через ping и печатает итог. Только код с русскими комментариями."}]}')"
    api_post /v1/chat/completions "${body}" 600 >/dev/null 2>&1 || die "прогрев не удался"
    r="$(api_post /v1/chat/completions "${body}" 600 2>/dev/null)" || die "замер не удался"
    tg="$(jq -r '.timings.predicted_per_second | . * 10 | round / 10' <<<"${r}")"
    pp="$(jq -r '.timings.prompt_per_second | round' <<<"${r}")"
    acc="$(jq -r 'if .timings.draft_n then "\(.timings.draft_n_accepted)/\(.timings.draft_n)" else "-" end' <<<"${r}")"
    info "Замер ${model}: генерация ${tg} ток/с, промпт ${pp} ток/с, черновики принято ${acc}"
    log_json "INFO" "bench" "${model}" "tg=${tg} pp=${pp} draft=${acc}"
    BENCH_LAST="${tg}"
}

###############################################################################
# КОМАНДЫ: добавление/удаление моделей
###############################################################################
hf_file_info() {
    # stdout: size|sha256 для файла репозитория
    local repo="$1" file="$2" tree
    tree="$(curl -fsS --max-time 30 "${HF_BASE}/api/models/${repo}/tree/main?recursive=1")" || return 1
    F="${file}" jq -r '.[] | select(.path == env.F) | "\(.size)|\(.lfs.oid // "")"' <<<"${tree}" | head -1
}

hf_download() {
    local repo="$1" file="$2" dest="$3" sha="$4" i
    mkdir -p -- "$(dirname -- "${dest}")"
    info "Загрузка ${repo}/${file} → ${dest}"
    for i in 1 2 3 4 5 6; do
        curl -fL --http1.1 --retry 5 -C - --progress-bar -o "${dest}.part" "${HF_BASE}/${repo}/resolve/main/${file}" && break
        warn "обрыв загрузки, попытка ${i}/6"
        sleep 3
    done
    if [[ -n "${sha}" ]]; then
        info "Проверка sha256…"
        echo "${sha}  ${dest}.part" | sha256sum -c --status || { rm -f -- "${dest}.part"; die "sha256 не совпал: ${file}"; }
    fi
    mv -f -- "${dest}.part" "${dest}"
    info "Готово: ${dest}"
}

# Пишет в stdout актуальный models.ini с новой секцией модели
add_render_preset() {
    local name="$1" repo="$2" ctx="$3" dest="$4" mm_dest="$5" today
    today="$(date '+%F')"
    cat -- "${PRESET_FILE}"
    printf '\n[%s]\n; Добавлено llamactl %s из %s\n' "${name}" "${today}" "${repo}"
    [[ -n "${ctx}" ]] && printf 'ctx-size = %s\n' "${ctx}"
    printf 'model = %s\n' "${dest}"
    if [[ -n "${mm_dest}" ]]; then
        printf 'mmproj = %s\n' "${mm_dest}"
        printf '; Проектор изображений на CPU: не отнимает VRAM у весов.\nno-mmproj-offload = true\n'
    fi
}

# Под блокировкой и уже после загрузки: секция дописывается к текущему
# models.ini, а не к копии, прочитанной до загрузки, — иначе пропали бы
# правки, сделанные за время загрузки.
add_apply_preset() {
    local name="$1" tmp rc=0
    preset_lock
    preset_has_section "${name}" && die "модель [${name}] уже есть в ${PRESET_FILE}" 2
    tmp="$(mktemp)"
    add_render_preset "$@" > "${tmp}"
    preset_apply "${tmp}" "добавление [${name}]" || rc=$?
    rm -f -- "${tmp}"
    preset_unlock
    return "${rc}"
}

# Под блокировкой: секция (с пустой строкой перед ней) вырезается из текущего models.ini
remove_apply_preset() {
    local name="$1" tmp rc=0
    preset_lock
    preset_has_section "${name}" || die "секции [${name}] нет в ${PRESET_FILE}" 2
    tmp="$(mktemp)"
    awk -v s="[${name}]" '
        $0 == s { skip = 1; next }
        skip && /^\[/ { skip = 0 }
        skip { next }
        { lines[++n] = $0 }
        END { while (n > 0 && lines[n] == "") n--; for (i = 1; i <= n; i++) print lines[i] }' "${PRESET_FILE}" > "${tmp}"
    preset_apply "${tmp}" "удаление [${name}]" || rc=$?
    rm -f -- "${tmp}"
    preset_unlock
    return "${rc}"
}

cmd_add() {
    local repo="" file="" name="" mmproj="" ctx=""
    while (($# > 0)); do
        case "$1" in
            --name) shift; name="${1:-}" ;;
            --mmproj) shift; mmproj="${1:-}" ;;
            --ctx) shift; ctx="${1:-}" ;;
            -*) die "неизвестный параметр add: $1" 2 ;;
            *) if [[ -z "${repo}" ]]; then repo="$1"; elif [[ -z "${file}" ]]; then file="$1"; else die "лишний аргумент: $1" 2; fi ;;
        esac
        shift
    done
    [[ -n "${repo}" && -n "${file}" ]] || die "llamactl add РЕПОЗИТОРИЙ ФАЙЛ.gguf [--name ИМЯ] [--mmproj ФАЙЛ] [--ctx N]" 2
    [[ -n "${name}" ]] || { name="$(basename -- "${file}" .gguf)"; name="${name,,}"; }
    [[ "${name}" =~ ^[A-Za-z0-9._-]+$ ]] || die "недопустимое имя модели «${name}»" 2
    [[ -z "${ctx}" || "${ctx}" =~ ^[0-9]+$ ]] || die "окно должно быть числом" 2
    preset_has_section "${name}" && die "модель [${name}] уже есть в ${PRESET_FILE}" 2
    local fi_main size sha fi_mm mm_size=0 mm_sha="" avail need
    fi_main="$(hf_file_info "${repo}" "${file}")" || die "репозиторий ${repo} недоступен"
    [[ -n "${fi_main}" ]] || die "в ${repo} нет файла ${file}"
    IFS='|' read -r size sha <<<"${fi_main}"
    if [[ -n "${mmproj}" ]]; then
        fi_mm="$(hf_file_info "${repo}" "${mmproj}")"
        [[ -n "${fi_mm}" ]] || die "в ${repo} нет файла ${mmproj}"
        IFS='|' read -r mm_size mm_sha <<<"${fi_mm}"
    fi
    avail="$(df -B1 --output=avail "${MODELS_DIR}" | tail -1 | tr -d ' ')"
    need=$((size + mm_size + 1073741824))
    local hs ha
    hs="$(human_bytes "$((size + mm_size))")"; ha="$(human_bytes "${avail}")"
    info "Модель: ${name} = ${repo}/${file} (${hs}), на диске свободно ${ha}"
    ((avail > need)) || die "не хватает места в ${MODELS_DIR}"
    local dir="${MODELS_DIR}/${name}" dest mm_dest=""
    dest="${dir}/$(basename -- "${file}")"
    [[ -n "${mmproj}" ]] && mm_dest="${dir}/$(basename -- "${mmproj}")"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        act "download" "скачать ${repo}/${file} → ${dest} (sha256 ${sha:0:12}…)" || true
        [[ -n "${mm_dest}" ]] && { act "download" "скачать ${repo}/${mmproj} → ${mm_dest}" || true; }
        add_apply_preset "${name}" "${repo}" "${ctx}" "${dest}" "${mm_dest}"
        return 0
    fi
    act "download" "скачать ${repo}/${file} → ${dest}" && hf_download "${repo}" "${file}" "${dest}" "${sha}"
    [[ -n "${mm_dest}" ]] && hf_download "${repo}" "${mmproj}" "${mm_dest}" "${mm_sha}"
    add_apply_preset "${name}" "${repo}" "${ctx}" "${dest}" "${mm_dest}"
    cmd_fit "${name}" || true
    info "Модель ${name} добавлена: llamactl load ${name}"
}

cmd_remove() {
    local name="" del=0
    while (($# > 0)); do
        case "$1" in
            --delete-files) del=1 ;;
            -*) die "неизвестный параметр remove: $1" 2 ;;
            *) name="$1" ;;
        esac
        shift
    done
    [[ -n "${name}" ]] || die "llamactl remove МОДЕЛЬ [--delete-files]" 2
    preset_has_section "${name}" || die "секции [${name}] нет в ${PRESET_FILE}" 2
    local sect files=() f flist mst
    sect="$(preset_section "${name}")"
    flist="$(awk -F'=' '/^[[:space:]]*(model|mmproj)[[:space:]]*=/ { v = $2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v }' <<<"${sect}")"
    [[ -n "${flist}" ]] && mapfile -t files <<<"${flist}"
    mst="$(model_status "${name}")"
    if [[ "${mst}" != "unloaded" && "${mst}" != "absent" ]]; then
        cmd_unload "${name}"
    fi
    remove_apply_preset "${name}"
    ((del == 1)) || { info "файлы модели оставлены (удалить: --delete-files)"; return 0; }
    for f in "${files[@]}"; do
        case "${f}" in
            "${MODELS_DIR}"/*)
                act "delete_file" "удалить ${f}" && rm -f -- "${f}"
                ;;
            *)
                warn "пропущен файл вне ${MODELS_DIR}: ${f}"
                ;;
        esac
    done
    local d
    d="${MODELS_DIR}/${name}"
    if [[ -d "${d}" ]] && act "delete_dir" "удалить пустой каталог ${d}"; then
        rmdir -- "${d}" 2>/dev/null || true
    fi
    return 0
}

###############################################################################
# КОМАНДЫ: обновление llama.cpp
###############################################################################
installed_commit() {
    local v
    v="$("${LLAMA_PREFIX}/bin/llama-server" --version 2>&1 | sed -nE 's/.*commit ([0-9a-f]+).*/\1/p' | head -1)"
    printf '%s' "${v}"
}

cmd_update() {
    local ref="" bench=0 force=0
    while (($# > 0)); do
        case "$1" in
            --ref) shift; ref="${1:-}" ;;
            --bench) bench=1 ;;
            --force) force=1 ;;
            *) die "неизвестный параметр update: $1" 2 ;;
        esac
        shift
    done
    need_cmd git cmake
    [[ -d "${LLAMA_SRC}/.git" ]] || die "нет исходников ${LLAMA_SRC} (git clone https://github.com/ggml-org/llama.cpp)" 2
    local dirty
    dirty="$(git -C "${LLAMA_SRC}" status --porcelain --untracked-files=no)"
    [[ -z "${dirty}" ]] || die "в ${LLAMA_SRC} есть незакоммиченные изменения — обновление прервано"
    local cur remote target
    cur="$(installed_commit)"
    remote="$(git -C "${LLAMA_SRC}" ls-remote origin "${ref:-HEAD}" | awk 'NR == 1 { print $1 }')"
    [[ -n "${remote}" ]] || die "не удалось узнать ${ref:-HEAD} в origin"
    target="${remote:0:7}"
    info "Установлено: ${cur:-?}; в origin ${ref:-HEAD}: ${target}"
    if [[ -n "${cur}" && "${remote}" == "${cur}"* && "${force}" -eq 0 ]]; then
        info "llama.cpp уже актуален (повторить сборку: --force)"
        return 0
    fi
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        act "fetch" "git fetch ${ref:-HEAD} и переключить ${LLAMA_SRC} на ${target}" || true
        act "build" "cmake (CUDA, sm_${CUDA_ARCH}, RPATH ${RPATH_LIT}) и сборка в ${BUILD_JOBS} потоков" || true
        act "install" "установить в ${LLAMA_PREFIX}.new, проверить, заменить ${LLAMA_PREFIX} (старая версия → ${LLAMA_PREFIX}.prev), перезапустить ${SERVER_UNIT} ${FIM_UNIT}; при сбое — откат" || true
        ((bench == 1)) && { act "bench" "замер ${BENCH_MODEL} до и после" || true; }
        return 0
    fi
    local before=""
    if ((bench == 1)); then
        info "== Замер до обновления"
        cmd_bench "${BENCH_MODEL}" && before="${BENCH_LAST}"
    fi
    info "== [1/4] Получение исходников ${target}"
    log_json "INFO" "fetch" "${ref:-HEAD} ${target}"
    git -C "${LLAMA_SRC}" fetch --depth 1 origin "${ref:-HEAD}" || die "git fetch не удался"
    git -C "${LLAMA_SRC}" checkout --detach FETCH_HEAD || die "git checkout не удался"
    info "== [2/4] Сборка (прогресс — проценты cmake)"
    local rpath="${RPATH_LIT}"
    log_json "INFO" "build" "sm_${CUDA_ARCH} -j${BUILD_JOBS}"
    cmake -S "${LLAMA_SRC}" -B "${LLAMA_SRC}/build" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
        -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
        -DCMAKE_INSTALL_PREFIX="${LLAMA_PREFIX}" "-DCMAKE_INSTALL_RPATH=${rpath}" >/dev/null || die "cmake configure не удался"
    cmake --build "${LLAMA_SRC}/build" -j "${BUILD_JOBS}" 2>&1 | grep --line-buffered -E '^\[ *[0-9]+%\]' | awk '{ p = $1 " " $2; if (p != last) { print "  " p; last = p } }' | uniq
    [[ -x "${LLAMA_SRC}/build/bin/llama-server" ]] || die "сборка не удалась (cmake --build ${LLAMA_SRC}/build)"
    info "== [3/4] Установка с заменой и проверкой (один запрос пароля)"
    "${SUDO[@]}" "${SCRIPT_PATH}" __root-swap-install "${LLAMA_SRC}/build" "${LLAMA_PREFIX}" "${SERVER_UNIT}" "${FIM_UNIT}" "${ROUTER_URL}" "${FIM_URL}" \
        || die "установка не удалась, прежняя версия возвращена"
    local now
    now="$(installed_commit)"
    info "== [4/4] Установлено: ${now}"
    log_json "INFO" "updated" "${cur} -> ${now}"
    if ((bench == 1)); then
        info "== Замер после обновления"
        cmd_bench "${BENCH_MODEL}"
        info "Скорость ${BENCH_MODEL}: было ${before:-?} → стало ${BENCH_LAST:-?} ток/с"
    fi
    info "Откат при необходимости: прежняя версия в ${LLAMA_PREFIX}.prev"
}

###############################################################################
# ВНУТРЕННИЕ root-КОМАНДЫ (запускаются самим скриптом через sudo -A)
###############################################################################
root_install_preset() {
    local src="$1" dst="$2" backup="$3"
    cp -p -- "${dst}" "${backup}" || exit 1
    install -m 0644 -- "${src}" "${dst}" || exit 1
}

root_restore_preset() {
    local backup="$1" dst="$2" unit="$3"
    install -m 0644 -- "${backup}" "${dst}" && systemctl restart "${unit}"
}

root_swap_install() {
    local build="$1" prefix="$2" su="$3" fu="$4" rurl="$5" furl="$6"
    local new="${prefix}.new" prev="${prefix}.prev"
    rm -rf -- "${new}"
    cmake --install "${build}" --prefix "${new}" >/dev/null || { echo "cmake --install не удался" >&2; exit 1; }
    "${new}/bin/llama-server" --version >/dev/null 2>&1 || { echo "новая сборка не запускается" >&2; rm -rf -- "${new}"; exit 1; }
    if ldd "${new}/bin/llama-server" | grep -q 'not found'; then
        echo "у новой сборки не найдены библиотеки" >&2; rm -rf -- "${new}"; exit 1
    fi
    rm -rf -- "${prev}"
    mv -- "${prefix}" "${prev}" && mv -- "${new}" "${prefix}" || exit 1
    systemctl restart "${su}" "${fu}"
    local t=0 ok=0
    while ((t < 90)); do
        if curl -fsS --max-time 3 "${rurl}/health" >/dev/null 2>&1 && curl -fsS --max-time 3 "${furl}/health" >/dev/null 2>&1; then
            ok=1; break
        fi
        sleep 3; t=$((t + 3))
    done
    if ((ok == 1)); then
        echo "  сервисы отвечают (${t} с)"
        exit 0
    fi
    echo "сервисы не поднялись — откат на ${prev}" >&2
    rm -rf -- "${new}"
    mv -- "${prefix}" "${new}" && mv -- "${prev}" "${prefix}"
    systemctl restart "${su}" "${fu}"
    exit 1
}

###############################################################################
# ИНТЕРАКТИВНОЕ МЕНЮ
###############################################################################
pick_model() {
    local prompt="$1" filter="${2:-all}" mj ids=() i n sel
    mj="$(models_json)"
    case "${filter}" in
        loaded) sel="$(jq -r '.[] | select(.status != "unloaded") | .id' <<<"${mj}")" ;;
        *) sel="$(jq -r '.[].id' <<<"${mj}")" ;;
    esac
    [[ -n "${sel}" ]] && mapfile -t ids <<<"${sel}"
    ((${#ids[@]} > 0)) || { info "подходящих моделей нет" >&2; return 1; }
    for i in "${!ids[@]}"; do printf '  %d) %s\n' "$((i + 1))" "${ids[i]}" >&2; done
    read -r -p "${prompt} [номер]: " n
    [[ "${n}" =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#ids[@]})) || return 1
    printf '%s' "${ids[n - 1]}"
}

cmd_menu() {
    local choice m a b
    while true; do
        printf '\n'
        cmd_status
        cat <<'EOF'

llamactl — действия:
  1) загрузить модель        2) выгрузить модель     3) выгрузить все
  4) освободить VRAM         5) перезапустить сервисы 6) логи роутера (-f)
  7) проверка (doctor)       8) пресет: показать     9) пресет: изменить ключ
 10) пресеты: редактор      11) добавить модель с HF 12) удалить модель
 13) раскладка по GPU (fit) 14) замер скорости       15) обновить llama.cpp
 16) убрать сторонние llama-server                    0) выход
EOF
        read -r -p "Выбор: " choice || return 0
        case "${choice}" in
            1) m="$(pick_model "Загрузить")" && cmd_load "${m}" ;;
            2) m="$(pick_model "Выгрузить" loaded)" && cmd_unload "${m}" ;;
            3) cmd_unload all ;;
            4) cmd_free_vram ;;
            5) read -r -p "Что перезапустить (server/fim/all) [all]: " a; cmd_service restart "${a:-all}" ;;
            6) cmd_logs server -f ;;
            7) cmd_doctor ;;
            8) m="$(pick_model "Пресет")" && cmd_preset show "${m}" ;;
            9) m="$(pick_model "Модель")" && read -r -p "Ключ (например ctx-size): " a && read -r -p "Значение: " b && cmd_preset set "${m}" "${a}" "${b}" ;;
            10) cmd_preset edit ;;
            11) read -r -p "Репозиторий HF (user/repo): " a && read -r -p "Файл .gguf: " b && read -r -p "mmproj (пусто — без него): " m && \
                if [[ -n "${m}" ]]; then cmd_add "${a}" "${b}" --mmproj "${m}"; else cmd_add "${a}" "${b}"; fi ;;
            12) m="$(pick_model "Удалить")" && confirm "Удалить ${m} из пресетов?" && cmd_remove "${m}" ;;
            13) m="$(pick_model "Модель")" && cmd_fit "${m}" ;;
            14) m="$(pick_model "Замер")" && cmd_bench "${m}" ;;
            15) cmd_update --bench ;;
            16) cmd_kill_foreign ;;
            0|q|exit) return 0 ;;
            *) info "нет такого пункта" ;;
        esac
        read -r -p "Enter — продолжить" _ || return 0
    done
}

usage() {
    cat <<EOF
Использование: ${SCRIPT_NAME} [-n|--dry-run] [--json] КОМАНДА [аргументы]

Без команды в терминале — интерактивное меню.

Статус и модели:
  status [--json]              сервисы, модели, VRAM/RAM, сторонние llama-server
  models [--json]              список моделей роутера и их состояние
  load МОДЕЛЬ                  загрузить модель (с прогрессом)
  unload МОДЕЛЬ|all            выгрузить модель или все
  free-vram                    выгрузить все модели и остановить FIM (под игру)
Сервисы и логи:
  start|stop|restart [server|fim|all]
  logs [server|fim] [-f] [-n N]
  doctor                       проверка конфигурации и окружения (код 1 — есть проблемы)
  kill-foreign                 завершить сторонние llama-server (llama-vscode env и т.п.)
Пресеты и модели:
  preset list|show М|set М КЛЮЧ ЗНАЧ|unset М КЛЮЧ|edit
  fit МОДЕЛЬ [ОКНО]            раскладка слоёв по GPU/CPU (llama-fit-params)
  add РЕПО ФАЙЛ.gguf [--name ИМЯ] [--mmproj ФАЙЛ] [--ctx N]
  remove МОДЕЛЬ [--delete-files]
  bench [МОДЕЛЬ]               замер скорости генерации
Обновление:
  update [--ref REF] [--bench] [--force]   пересборка llama.cpp с CUDA, замена с откатом

  -n, --dry-run   ничего не менять, напечатать план «[dry-run] будет: …»
  --json          машиночитаемый вывод (status, models)
Конфиг: conf/llamactl.conf (пример: conf/llamactl.conf.example)
EOF
}

###############################################################################
# ARGS
###############################################################################
# Короткие -n/-h — только до имени команды (у logs свой -n N);
# длинные --dry-run/--json допускаются в любом месте.
ARGS=()
while (($# > 0)); do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --json) JSON_OUT=1 ;;
        --help) usage; exit 0 ;;
        -n) if ((${#ARGS[@]} == 0)); then DRY_RUN=1; else ARGS+=("$1"); fi ;;
        -h) if ((${#ARGS[@]} == 0)); then usage; exit 0; else ARGS+=("$1"); fi ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done
set -- "${ARGS[@]+"${ARGS[@]}"}"
CMD="${1:-}"
(($# > 0)) && shift

###############################################################################
# MAIN
###############################################################################
case "${CMD}" in
    __root-install-preset) NO_LOG=1; root_install_preset "$@"; exit 0 ;;
    __root-restore-preset) NO_LOG=1; root_restore_preset "$@"; exit "$?" ;;
    __root-swap-install) NO_LOG=1; root_swap_install "$@" ;;
    *) ;;
esac

need_cmd curl jq awk
# status/models вызываются трей-иконкой каждые несколько секунд — их не логируем.
case "${CMD}" in
    status|models|"") NO_LOG=1 ;;
    *) ;;
esac
log_json "INFO" "start" "llamactl ${CMD:-menu}" "$*"

rc=0
case "${CMD}" in
    "")
        if [[ -t 0 && -t 1 ]]; then cmd_menu; else cmd_status; fi
        rc=$?
        ;;
    status) cmd_status; rc=$? ;;
    models) cmd_models; rc=$? ;;
    load) cmd_load "$@"; rc=$? ;;
    unload) cmd_unload "$@"; rc=$? ;;
    free-vram) cmd_free_vram; rc=$? ;;
    start|stop|restart) cmd_service "${CMD}" "$@"; rc=$? ;;
    logs) cmd_logs "$@"; rc=$? ;;
    doctor) cmd_doctor; rc=$? ;;
    kill-foreign) cmd_kill_foreign; rc=$? ;;
    preset) cmd_preset "$@"; rc=$? ;;
    fit) cmd_fit "$@"; rc=$? ;;
    bench) cmd_bench "$@"; rc=$? ;;
    add) cmd_add "$@"; rc=$? ;;
    remove) cmd_remove "$@"; rc=$? ;;
    update) cmd_update "$@"; rc=$? ;;
    menu) cmd_menu; rc=$? ;;
    *) usage >&2; die "неизвестная команда «${CMD}»" 2 ;;
esac

log_json "INFO" "done" "llamactl ${CMD:-menu}" "rc=${rc}"
cleanup_logs
exit "${rc}"
