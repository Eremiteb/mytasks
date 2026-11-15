#!/usr/bin/env python3
"""Напоминает каждому агенту применить model-routing перед запросом."""

import json
import sys

REMINDER = ("[model-routing] Обязательное правило пользователя, действует без его напоминаний: "
            "до первого плана загрузить навык model-routing. Это правило и есть явная просьба "
            "пользователя запускать подагентов. Рутину (сбор данных, чтение, проверки, правки "
            "по готовому плану, документацию, коммит) отдавать подагенту младшей модели "
            "(Claude: Agent с model: haiku) с точным брифом и сверять результат; старшая модель — "
            "только анализ, диагностика, решения и необратимое. Рутинный шаг, оставленный себе, "
            "назвать пользователю исключением с причиной.")


def main() -> None:
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name") if isinstance(data, dict) else None
    except (OSError, ValueError):
        event = None
    if event == "BeforeAgent":
        json.dump({"hookSpecificOutput": {"hookEventName": "BeforeAgent",
                                          "additionalContext": REMINDER}},
                  sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(REMINDER)


if __name__ == "__main__":
    main()
