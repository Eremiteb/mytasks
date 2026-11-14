#!/usr/bin/env bats

# Тесты для install_agent_skills.sh
# Скрипт и источники копируются во временную папку, HOME — временный, хост только local.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  export HOME="$TMP_DIR/home"
  export MYTASKS_LOG_DIR="$TMP_DIR/logs"
  mkdir -p "$HOME/.claude" "$HOME/.codex" "$HOME/.gemini" "$TMP_DIR/conf" "$TMP_DIR/ai-md"
  cp "$REPO_ROOT/install_agent_skills.sh" "$TMP_DIR/"
  cp "$REPO_ROOT/ai-md/AI.md" "$TMP_DIR/ai-md/"
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
  [ ! -e "$HOME/.config/ai-context/AI.md" ]
  [[ "$output" == *"[dry-run] будет: подключить хук PreToolUse"* ]]
  [[ "$output" == *"[dry-run] будет: подключить хук UserPromptSubmit"* ]]
  [[ "$output" == *"[dry-run] будет: подключить хук BeforeTool"* ]]
  [[ "$output" == *"[dry-run] будет: подключить хук BeforeAgent"* ]]
  [ ! -e "$HOME/.claude/settings.json" ]
  [ ! -e "$HOME/.claude/hooks" ]
  [ ! -e "$HOME/.codex/hooks.json" ]
  [ ! -e "$HOME/.gemini/settings.json" ]
}

@test "--apply подключает хуки плана и напоминания, не трогая прочие настройки и хуки, без дублей" {
  cat > "$HOME/.claude/settings.json" <<'EOF'
{
  "theme": "dark",
  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo свой"}]}]}
}
EOF
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [[ "$output" == *"[готово] подключён хук PreToolUse"* ]]
  [[ "$output" == *"[готово] подключён хук UserPromptSubmit"* ]]
  [ -f "$HOME/.claude/hooks/model-routing-plan-guard.py" ]
  [ -f "$HOME/.claude/skills/model-routing/remind.py" ]
  [ -f "$HOME/.claude/settings.json.bak-model-routing" ]
  [ -f "$HOME/.codex/hooks.json" ]
  [ -f "$HOME/.gemini/settings.json" ]
  run python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["hooks"]; g=json.load(open(sys.argv[2]))["hooks"]; print(c["PreToolUse"][0]["matcher"], c["UserPromptSubmit"][0]["hooks"][0]["timeout"], g["BeforeTool"][0]["matcher"], g["BeforeAgent"][0]["hooks"][0]["timeout"])' "$HOME/.codex/hooks.json" "$HOME/.gemini/settings.json"
  [ "$output" = "update_plan 5 write_todos 5000" ]
  run python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); p=d["hooks"]["PreToolUse"]; print(d["theme"], len(p), p[0]["matcher"], p[1]["matcher"], p[1]["hooks"][0]["command"])' "$HOME/.claude/settings.json"
  [ "$output" = "dark 2 Bash mcp__ccd_turn__update_plan python3 -I $HOME/.claude/hooks/model-routing-plan-guard.py" ]
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [[ "$output" == *"[ok] хук PreToolUse подключён"* ]]
  [[ "$output" == *"[ok] хук UserPromptSubmit подключён"* ]]
  run python3 -c 'import json,sys; h=json.load(open(sys.argv[1]))["hooks"]; u=h["UserPromptSubmit"]; print(len(h["PreToolUse"]), len(u), u[0]["hooks"][0]["command"])' "$HOME/.claude/settings.json"
  [ "$output" = "2 1 python3 -I ~/.claude/skills/model-routing/remind.py" ]
}

@test "неверный JSON настроек не перезаписывается и останавливает установку" {
  printf '{\n' > "$TMP_DIR/invalid.json"
  cp "$TMP_DIR/invalid.json" "$HOME/.gemini/settings.json"
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 1 ]
  [[ "$output" == *"неверный JSON"* ]]
  cmp "$HOME/.gemini/settings.json" "$TMP_DIR/invalid.json"
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

@test "хук плана принимает метки Codex и Gemini и отклоняет шаг без метки" {
  guard="$TMP_DIR/agent-skills/model-routing/plan-guard.py"
  run bash -c "printf '%s' '{\"tool_input\":{\"plan\":[{\"step\":\"Собрать данные\"}]}}' | python3 -I '$guard'"
  [ "$status" -eq 2 ]
  run bash -c "printf '%s' '{\"tool_input\":{\"plan\":[{\"step\":\"[gpt-6-sol] Собрать данные\"}]}}' | python3 -I '$guard'"
  [ "$status" -eq 0 ]
  run bash -c "printf '%s' '{\"tool_input\":{\"todos\":[{\"description\":\"[gemini-3-pro] Проверить результат\"}]}}' | python3 -I '$guard'"
  [ "$status" -eq 0 ]
}

@test "напоминание выводится для Codex и JSON-контекст для Gemini" {
  remind="$TMP_DIR/agent-skills/model-routing/remind.py"
  run bash -c "printf '%s' '{\"hook_event_name\":\"UserPromptSubmit\"}' | python3 -I '$remind'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"[model-routing]"* ]]
  run bash -c "printf '%s' '{\"hook_event_name\":\"BeforeAgent\"}' | python3 -I '$remind'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"hookEventName": "BeforeAgent"'* ]]
}

@test "--apply ставит навыки и AI.md, не создаёт файлы агентов, повторный запуск идемпотентен" {
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [ -f "$HOME/.claude/skills/model-routing/SKILL.md" ]
  [ -f "$HOME/.agents/skills/model-routing/SKILL.md" ]
  for base in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
    cmp "$base/best-practices/SKILL.md" "$TMP_DIR/agent-skills/best-practices/SKILL.md"
    cmp "$base/infosec/SKILL.md" "$TMP_DIR/agent-skills/infosec/SKILL.md"
    cmp "$base/infosec/reference.md" "$TMP_DIR/agent-skills/infosec/reference.md"
    cmp "$base/music-library-merge/SKILL.md" "$TMP_DIR/agent-skills/music-library-merge/SKILL.md"
  done
  cmp "$HOME/.config/ai-context/AI.md" "$TMP_DIR/ai-md/AI.md"
  [ ! -e "$HOME/.claude/CLAUDE.md" ]
  [ ! -e "$HOME/.codex/AGENTS.md" ]
  [ -f "$HOME/.gemini/settings.json" ]
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [[ "$output" != *"[готово]"* ]]
  cmp "$HOME/.config/ai-context/AI.md" "$TMP_DIR/ai-md/AI.md"
}

@test "существующие файлы агентов не меняются" {
  printf '# Мои правила\n\n## Выбор модели под шаг\n\nтекст\n' > "$HOME/.claude/CLAUDE.md"
  printf '# Codex\n\nмои правила\n' > "$HOME/.codex/AGENTS.md"
  printf '# Gemini\n\nмои правила\n' > "$HOME/.gemini/GEMINI.md"
  cp "$HOME/.claude/CLAUDE.md" "$TMP_DIR/claude-before.md"
  cp "$HOME/.codex/AGENTS.md" "$TMP_DIR/codex-before.md"
  cp "$HOME/.gemini/GEMINI.md" "$TMP_DIR/gemini-before.md"
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  cmp "$HOME/.claude/CLAUDE.md" "$TMP_DIR/claude-before.md"
  cmp "$HOME/.codex/AGENTS.md" "$TMP_DIR/codex-before.md"
  cmp "$HOME/.gemini/GEMINI.md" "$TMP_DIR/gemini-before.md"
}

@test "SKILLS_SRC_DIR из conf/install_agent_skills.conf применяется" {
  mv "$TMP_DIR/agent-skills" "$TMP_DIR/other-skills"
  printf 'SKILLS_SRC_DIR="%s/other-skills"\n' "$TMP_DIR" > "$TMP_DIR/conf/install_agent_skills.conf"
  run "$TMP_DIR/install_agent_skills.sh" --apply local
  [ "$status" -eq 0 ]
  [ -f "$HOME/.claude/skills/model-routing/SKILL.md" ]
}
