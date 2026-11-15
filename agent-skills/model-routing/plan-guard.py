#!/usr/bin/env python3
"""Отклоняет план без меток модели; ошибки самого хука не блокируют агента."""

import json
import re
import sys

CLAUDE_TAG = re.compile(r"^\[(?:Haiku|Opus)\]\s+\S")
MODEL_TAG = re.compile(r"^\[[A-Za-z][A-Za-z0-9._/-]+\]\s+\S")
REASON = ("План не принят: каждый шаг должен начинаться меткой модели. "
          "Claude: [Haiku] или [Opus]; Codex и Gemini: точный идентификатор "
          "доступной модели в квадратных скобках. Сверь каталог и повтори план.")
# План Claude из стольких и более шагов, где все шаги [Opus], без причины — отклоняется.
ALL_SENIOR_MIN_STEPS = 3
SENIOR_ONLY_TAG = re.compile(r"^\[Opus\]")
SENIOR_REASON = re.compile(r"\(Opus:\s*\S")
REASON_ALL_SENIOR = ("План не принят: все шаги помечены [Opus]. Рутину (сбор данных, проверки, "
                     "правки по готовому плану, документацию, коммит) пометь [Haiku] и отдай "
                     "подагенту Agent с model: haiku. Если каждый шаг действительно требует Opus "
                     "(анализ, диагностика, необратимое), добавь причину хотя бы в один шаг: "
                     "«[Opus] Шаг (Opus: причина)».")


def main() -> int:
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return 0
    if not isinstance(tool_input, dict):
        return 0
    if isinstance(tool_input.get("items"), list):
        steps, fields, tag = tool_input["items"], ("content",), CLAUDE_TAG
    elif isinstance(tool_input.get("plan"), list):
        steps, fields, tag = tool_input["plan"], ("step",), MODEL_TAG
    elif isinstance(tool_input.get("todos"), list):
        steps, fields, tag = tool_input["todos"], ("description", "content", "task"), MODEL_TAG
    else:
        return 0
    bad = []
    contents = []
    for item in steps:
        if not isinstance(item, dict):
            continue
        content = next((str(item[field]) for field in fields if field in item), "")
        contents.append(content)
        if not tag.match(content):
            bad.append(content)
    if not bad:
        if tag is CLAUDE_TAG and len(contents) >= ALL_SENIOR_MIN_STEPS \
                and all(SENIOR_ONLY_TAG.match(c) for c in contents) \
                and not any(SENIOR_REASON.search(c) for c in contents):
            print(REASON_ALL_SENIOR, file=sys.stderr)
            return 2
        return 0
    print(REASON + " Шаги без метки: " + "; ".join(f"«{step[:60]}»" for step in bad[:6]),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
