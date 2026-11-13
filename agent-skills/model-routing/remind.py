#!/usr/bin/env python3
# Хук UserPromptSubmit: на любой модели, кроме Haiku, напоминает применить навык
# model-routing к каждому запросу: младшая модель для подагентов — только Haiku.
# Модель берётся из последнего ответа ассистента в транскрипте; на Haiku хук молчит,
# при неизвестной модели — напоминает. Любая ошибка — молчание, хук не блокирует запрос.
# Ставится install_agent_skills.sh в ~/.claude/skills/model-routing/remind.py.
import json
import os
import sys

TAIL = 512 * 1024  # хвоста транскрипта хватает, чтобы найти последний ответ
REMINDER = ("[model-routing] Сессия на старшей модели: до первого шага этого запроса примени "
            "навык model-routing (Skill) — отнеси шаги плана к классам и самодостаточную "
            "механику (документация, проверки, карта, коммит после «да») отдай подагенту "
            "Haiku (Sonnet не использовать) без напоминания пользователя. Короткий ответ без плана — навык не нужен.")


def last_model(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"model"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        msg = record.get("message") if isinstance(record, dict) else None
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("model"):
            return str(msg["model"])
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        data = {}
    path = data.get("transcript_path") if isinstance(data, dict) else None
    model = (last_model(path) if isinstance(path, str) and path else None) or ""
    if "haiku" not in model:
        print(REMINDER)


if __name__ == "__main__":
    main()
