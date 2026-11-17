# `install_agent_skills.sh`: распространение AI.md, навыков и хуков

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md).

## `install_agent_skills.sh`

- `ai-md/AI.md` — индекс общих требований; установщик доставляет копии `AI.md`, `skills.md`, `projects.md` и `agents/{claude,codex,gemini}.md` в `~/.config/ai-context/` (та же структура каталогов) вместе с навыками и хуками. `mytasks.md` и `topics/` на другие устройства не доставляются. `AGENTS.md`/`CLAUDE.md`/`GEMINI.md` после однократной очистки — указатели; установщик их не меняет.
- Источники навыков и хуков — `agent-skills/`. Сначала `./install_agent_skills.sh --dry-run`, затем `--apply` по правилу подтверждения боевого запуска.
- Устройства — `HOSTS` в `conf/install_agent_skills.conf` (`local` + имена из `~/.ssh/config`). Ставится в `~/.claude`, `~/.codex`, `~/.gemini`, `~/.agents` там, где каталог есть. Постоянно — `agent-skills-sync.timer` (задание `check_mytasks.sh`). Новое устройство — в `HOSTS`, не копировать файлы вручную.
- `plan-guard.py` — в каталог хуков каждой платформы: Claude — `PreToolUse` на `update_plan`, проверка меток `[Haiku]`/`[Opus]`; Codex — `PreToolUse` на `update_plan`, метка с актуальным идентификатором модели у каждого шага; Gemini — `BeforeTool` на `write_todos`. Ошибка самого хука не блокирует работу; отсутствие метки — код 2 и причина агенту.
- `remind.py` — напоминание применять `model-routing` в начале каждого запроса: Claude и Codex — `UserPromptSubmit`, Gemini — `BeforeAgent` (JSON `additionalContext`); для остальных — текст. Напоминание не заменяет самостоятельную проверку агентом.
- Codex загружает `~/.codex/hooks.json` только после просмотра и доверия в `/hooks` или Settings → Hooks; установщик этот шаг не обходит. Gemini — `~/.gemini/settings.json`, исполнит хуки, когда CLI установлен. Тайм-ауты: Gemini — миллисекунды, Claude/Codex — секунды.
- Навыки `best-practices`, `infosec` (с `reference.md`) и `music-library-merge` ставятся в `~/.claude/skills` и `~/.agents/skills` на всех хостах из `HOSTS`. Источники — `agent-skills/best-practices/`, `agent-skills/infosec/`, `agent-skills/music-library-merge/`; правятся только там.
