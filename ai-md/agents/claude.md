# Claude Code (Anthropic)

Ведёт Claude. Общие требования — [../AI.md](../AI.md); реестр навыков и хуков — [../skills.md](../skills.md).

- Старшая модель — Opus; младшая — Haiku. Делегирование — подагент с `model: haiku` (инструмент Agent).
- Метки шагов плана — `[Haiku]` или `[Opus]`. Хук `PreToolUse` на `mcp__ccd_turn__update_plan` (`model-routing-plan-guard.py`) отклоняет план без метки. План из 3+ шагов, где все шаги `[Opus]`, отклоняется, если ни в одном шаге нет причины «(Opus: …)».
- Коммит и пуш — подагентом Haiku. Трейлер `Co-Authored-By` называет реально коммитившую модель.
- Каталог навыков: `~/.claude/skills`. Напоминание о `model-routing` — хук `UserPromptSubmit` (`remind.py claude`) в `~/.claude/settings.json`.
