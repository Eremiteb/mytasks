#!/usr/bin/env bats

# Тесты для install_agent_skills.sh
# Скрипт и источники копируются во временную папку, HOME — временный, хост только local.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  export HOME="$TMP_DIR/home"
  export MYTASKS_LOG_DIR="$TMP_DIR/logs"
  mkdir -p "$HOME/.claude" "$HOME/.codex" "$TMP_DIR/conf"
  cp "$REPO_ROOT/install_agent_skills.sh" "$TMP_DIR/"
  cp -r "$REPO_ROOT/agent-skills" "$TMP_DIR/"
  chmod +x "$TMP_DIR/install_agent_skills.sh"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "без аргументов печатает usage и завершается с ошибкой" {
  run "$TMP_DIR/install_agent_skills.sh"
  [ "$status" -eq 1 ]
  [[ "$output" == *"--dry-run"* ]]
}

@test "--dry-run ничего не записывает" {
  run "$TMP_DIR/install_agent_skills.sh" --dry-run local
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: записать"* ]]
  [ ! -e "$HOME/.claude/skills" ]
  [ ! -e "$HOME/.claude/CLAUDE.md" ]
  [[ "$output" == *"[dry-run] будет: подключить хук плана"* ]]
  [ ! -e "$HOME/.claude/settings.json" ]
  [ ! -e "$HOME/.claude/hooks" ]
}

@test "--apply подключает хук плана, не трогая прочие настройки и хуки, без дублей" {
  cat > "$HOME/.claude/settings.json" <<'EOF'
{
  "theme": "dark",
  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo свой"}]}]}
}
EOF
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [[ "$output" == *"[готово] подключён хук плана"* ]]
  [ -f "$HOME/.claude/hooks/model-routing-plan-guard.py" ]
  [ -f "$HOME/.claude/settings.json.bak-model-routing" ]
  run python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); p=d["hooks"]["PreToolUse"]; print(d["theme"], len(p), p[0]["matcher"], p[1]["matcher"], p[1]["hooks"][0]["command"])' "$HOME/.claude/settings.json"
  [ "$output" = "dark 2 Bash mcp__ccd_turn__update_plan python3 -I $HOME/.claude/hooks/model-routing-plan-guard.py" ]
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [[ "$output" == *"[ok] хук плана подключён"* ]]
  run python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["hooks"]["PreToolUse"]))' "$HOME/.claude/settings.json"
  [ "$output" = "2" ]
}

@test "хук плана: шаг без метки модели отклоняется, с метками и advance — пропускаются" {
  guard="$TMP_DIR/agent-skills/model-routing/plan-guard.py"
  run bash -c "printf '%s' '{\"tool_input\":{\"items\":[{\"content\":\"[Haiku] Коммит\"},{\"content\":\"Написать код\"}]}}' | python3 -I '$guard'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"«Написать код»"* ]]
  run bash -c "printf '%s' '{\"tool_input\":{\"items\":[{\"content\":\"[Haiku] Коммит\"},{\"content\":\"[Opus] Таймер\"}]}}' | python3 -I '$guard'"
  [ "$status" -eq 0 ]
  run bash -c "printf '%s' '{\"tool_input\":{\"advance\":true}}' | python3 -I '$guard'"
  [ "$status" -eq 0 ]
}

@test "--apply ставит навыки и раздел, повторный запуск идемпотентен" {
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [ -f "$HOME/.claude/skills/model-routing/SKILL.md" ]
  [ -f "$HOME/.agents/skills/model-routing/SKILL.md" ]
  grep -q 'model-routing:begin' "$HOME/.claude/CLAUDE.md"
  grep -q 'model-routing:begin' "$HOME/.codex/AGENTS.md"
  [ ! -e "$HOME/.gemini" ]
  cp "$HOME/.claude/CLAUDE.md" "$TMP_DIR/before.md"
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [[ "$output" != *"[готово]"* ]]
  cmp "$HOME/.claude/CLAUDE.md" "$TMP_DIR/before.md"
}

@test "существующий раздел без маркеров не дублируется" {
  printf '# Мои правила\n\n## Выбор модели под шаг\n\nтекст\n' > "$HOME/.claude/CLAUDE.md"
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [ "$(grep -c 'Выбор модели под шаг' "$HOME/.claude/CLAUDE.md")" -eq 1 ]
}
