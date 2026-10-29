#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME="$(basename -- "$0")"
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)
LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
LOG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/log_template.conf"
CONF_FILE="${SCRIPT_DIR}/conf/${SCRIPT_BASE}.conf"
mkdir -p "${LOG_DIR}"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

###############################################################################
# CONFIG (значения по умолчанию; переопределяются в conf/install_agent_skills.conf)
###############################################################################
# local — это устройство, остальное — имена из ~/.ssh/config или адреса (ssh по ключу).
HOSTS=(local)
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
SKILLS_SRC_DIR="${SCRIPT_DIR}/agent-skills"
KEEP_LOGS=10
if [[ -r "${CONF_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${CONF_FILE}"
fi

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
  local level_norm msg_esc detail_esc _ts
  level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
  msg_esc="$(json_escape "${msg}")"
  detail_esc="$(json_escape "${detail}")"
  _ts="$(ts)"
  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
    "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
  local old_logs=()
  mapfile -t old_logs < <(
    { find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
      | sort -nr \
      | awk -F'|' -v keep="${KEEP_LOGS}" 'NR > keep { print $2 }'; } || true
  )
  if ((${#old_logs[@]} > 0)); then
    rm -f -- "${old_logs[@]}"
  fi
}

usage() {
  cat <<USAGE
Usage:
  ${SCRIPT_NAME} --dry-run [HOST...]
  ${SCRIPT_NAME} --apply   [HOST...]

Ставит навык model-routing и раздел «Выбор модели под шаг» для Claude Code,
Codex и Gemini CLI на этом устройстве (local) и на удалённых хостах по ssh.
Для Claude Code ещё подключает хук ~/.claude/hooks/model-routing-plan-guard.py
(PreToolUse update_plan в ~/.claude/settings.json): план без меток
[Haiku]/[Sonnet]/[Opus] у шагов не принимается.
Хосты — из HOSTS в conf/${SCRIPT_BASE}.conf либо аргументами.

Options:
  --dry-run, -n  показать, что будет записано (на хостах только чтение)
  --apply        записать файлы
USAGE
  exit 1
}

# Файл источника в base64 одной строкой.
encode_file() {
  local file="${SKILLS_SRC_DIR}/model-routing/$1"
  [[ -r "${file}" ]] || { echo "Error: нет файла ${file}" >&2; exit 1; }
  base64 -w0 < "${file}"
}

# Сценарий, который выполняется на целевом устройстве (stdin у bash -s).
build_payload() {
  local dry="$1" claude_skill agents_skill claude_section agents_section plan_guard
  claude_skill="$(encode_file claude.SKILL.md)"
  agents_skill="$(encode_file agents.SKILL.md)"
  claude_section="$(encode_file section.claude.md)"
  agents_section="$(encode_file section.agents.md)"
  plan_guard="$(encode_file plan-guard.py)"
  printf 'DRY=%s\nCLAUDE_SKILL=%s\nAGENTS_SKILL=%s\nCLAUDE_SECTION=%s\nAGENTS_SECTION=%s\nPLAN_GUARD=%s\n' \
    "${dry}" "${claude_skill}" "${agents_skill}" "${claude_section}" "${agents_section}" "${plan_guard}"
  cat <<'PAYLOAD'
put() {
  local dest="$1" b64="$2"
  if [ -f "$dest" ] && printf '%s' "$b64" | base64 -d | cmp -s - "$dest"; then
    echo "[ok] актуален: $dest"; return 0
  fi
  if [ "$DRY" -eq 1 ]; then echo "[dry-run] будет: записать $dest"; return 0; fi
  mkdir -p "$(dirname "$dest")"
  printf '%s' "$b64" | base64 -d > "$dest"
  echo "[готово] записан: $dest"
}
section() {
  local file="$1" b64="$2" tmp
  if [ -f "$file" ] && grep -q '^## Выбор модели под шаг' "$file" && ! grep -q 'model-routing:begin' "$file"; then
    echo "[ok] раздел уже есть (без маркеров): $file"; return 0
  fi
  tmp="$(mktemp)"
  {
    if [ -f "$file" ]; then
      sed '/<!-- model-routing:begin -->/,/<!-- model-routing:end -->/d' "$file" \
        | awk '{l[NR]=$0} END{n=NR; while(n>0 && l[n]=="") n--; for(i=1;i<=n;i++) print l[i]}'
    fi
    printf '\n<!-- model-routing:begin -->\n'
    printf '%s' "$b64" | base64 -d
    printf '<!-- model-routing:end -->\n'
  } > "$tmp"
  if [ -f "$file" ] && cmp -s "$tmp" "$file"; then
    echo "[ok] актуален: $file"
  elif [ "$DRY" -eq 1 ]; then
    echo "[dry-run] будет: обновить раздел в $file"
  else
    mkdir -p "$(dirname "$file")"
    cat "$tmp" > "$file"
    echo "[готово] обновлён раздел: $file"
  fi
  rm -f "$tmp"
}
# Хук PreToolUse на update_plan в ~/.claude/settings.json: одна запись, без дублей,
# остальные настройки и хуки не трогаются; перед записью — копия settings.json.bak-model-routing
hook() {
  local settings="$1" guard="$2"
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[пропуск] нет python3: хук плана не подключён"; return 0
  fi
  python3 -I - "$settings" "$guard" "$DRY" <<'PY'
import json, os, shutil, sys

path, guard, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
want = {"matcher": "mcp__ccd_turn__update_plan",
        "hooks": [{"type": "command", "command": f"python3 -I {guard}", "timeout": 10,
                   "statusMessage": "Проверка меток модели в плане"}]}
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except ValueError:
    print(f"[ошибка] {path}: неверный JSON, хук плана не подключён")
    sys.exit(0)
pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
idx = [i for i, e in enumerate(pre)
       if any("model-routing-plan-guard" in str(h.get("command", "")) for h in e.get("hooks", []))]
if len(idx) == 1 and pre[idx[0]] == want:
    print(f"[ok] хук плана подключён: {path}")
    sys.exit(0)
if dry:
    print(f"[dry-run] будет: подключить хук плана в {path}")
    sys.exit(0)
pos = idx[0] if idx else len(pre)
for i in reversed(idx):
    del pre[i]
pre.insert(pos, want)
if os.path.exists(path):
    shutil.copy2(path, path + ".bak-model-routing")
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp-model-routing"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, path)
print(f"[готово] подключён хук плана: {path}")
PY
}
found=0
if [ -d "$HOME/.claude" ]; then
  found=1
  put "$HOME/.claude/skills/model-routing/SKILL.md" "$CLAUDE_SKILL"
  section "$HOME/.claude/CLAUDE.md" "$CLAUDE_SECTION"
  put "$HOME/.claude/hooks/model-routing-plan-guard.py" "$PLAN_GUARD"
  hook "$HOME/.claude/settings.json" "$HOME/.claude/hooks/model-routing-plan-guard.py"
fi
if [ -d "$HOME/.codex" ] || [ -d "$HOME/.gemini" ] || [ -d "$HOME/.agents" ]; then
  found=1
  put "$HOME/.agents/skills/model-routing/SKILL.md" "$AGENTS_SKILL"
fi
if [ -d "$HOME/.codex" ]; then section "$HOME/.codex/AGENTS.md" "$AGENTS_SECTION"; fi
if [ -d "$HOME/.gemini" ]; then section "$HOME/.gemini/GEMINI.md" "$AGENTS_SECTION"; fi
if [ "$found" -eq 0 ]; then echo "[пропуск] нет ~/.claude, ~/.codex, ~/.gemini, ~/.agents"; fi
PAYLOAD
}

# Выполняет сценарий на хосте; вывод сценария — в консоль и лог.
run_host() {
  local host="$1" payload="$2" out line rc=0
  if [[ "${host}" == "local" ]]; then
    out="$(bash -s 2>&1 <<<"${payload}")" || rc=$?
  else
    out="$(ssh "${SSH_OPTS[@]}" -- "${host}" bash -s 2>&1 <<<"${payload}")" || rc=$?
  fi
  while IFS= read -r line; do
    echo "  ${line}"
    log_json INFO "install_${host}" "${line}" "${host}"
  done <<<"${out}"
  return "${rc}"
}

###############################################################################
# ARGS
###############################################################################
(($# >= 1)) || usage
MODE="$1"
shift
case "${MODE}" in
  --dry-run | -n) DRY=1 ;;
  --apply) DRY=0 ;;
  *) usage ;;
esac
if (($# > 0)); then
  HOSTS=("$@")
fi

###############################################################################
# MAIN
###############################################################################
payload="$(build_payload "${DRY}")"
failed=0
rc=0
for host in "${HOSTS[@]}"; do
  echo "== ${host} =="
  set +e
  run_host "${host}" "${payload}"
  rc=$?
  set -e
  if ((rc != 0)); then
    log_json ERROR "install_host_failed" "ошибка на ${host}, код ${rc}" "${host}"
    failed=$((failed + 1))
  fi
done
cleanup_logs
if ((failed > 0)); then
  log_json ERROR "install_failed" "хостов с ошибкой: ${failed}"
  exit 1
fi
log_json INFO "install_done" "хостов обработано: ${#HOSTS[@]}" "dry_run=${DRY}"
