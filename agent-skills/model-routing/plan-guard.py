#!/usr/bin/env python3
"""Хук PreToolUse для update_plan: каждый шаг плана должен иметь метку класса модели.

Правило «Выбор модели под шаг» (навык model-routing) требует при составлении плана
отнести каждый шаг к классу. Хук отклоняет план (код 2, причина — агенту в stderr),
если у пункта нет префикса [Haiku], [Sonnet] или [Opus]. Вызовы без списка шагов
(advance/done/start) пропускаются. Любая собственная ошибка хука — код 0: сломанный
хук не должен блокировать работу.

Ставится install_agent_skills.sh в ~/.claude/hooks/model-routing-plan-guard.py.
"""

import json
import re
import sys

TAG_RE = re.compile(r"^\[(Haiku|Sonnet|Opus)\]\s+\S")

REASON = (
    "План не принят: у каждого шага update_plan должна быть метка класса модели в начале — "
    "[Haiku] механика по готовому образцу (документация, граф, коммит/пуш, чтение), "
    "[Sonnet] типовая разработка с тестами, "
    "[Opus] решения, сложная диагностика и необратимое (службы, удаление, миграции, боевые запуски). "
    "Таблица и способы переключения — навык model-routing (~/.claude/skills/model-routing/SKILL.md). "
    "Повтори update_plan с метками; самодостаточные шаги [Haiku]/[Sonnet] отдай подагенту Agent с model, "
    "для серии таких шагов в основной сессии один раз предложи пользователю /model."
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
        items = (data.get("tool_input") or {}).get("items")
    except (ValueError, AttributeError):
        return 0
    if not isinstance(items, list):
        return 0
    bad = [str(it.get("content", "")) for it in items
           if isinstance(it, dict) and not TAG_RE.match(str(it.get("content", "")))]
    if not bad:
        return 0
    print(REASON + " Шаги без метки: " + "; ".join(f"«{c[:60]}»" for c in bad[:6]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
