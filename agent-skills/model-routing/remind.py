#!/usr/bin/env python3
"""Напоминает каждому агенту применить model-routing перед запросом."""

import json
import sys

REMINDER = ("[model-routing] В начале запроса применить навык model-routing: "
            "сверить доступные модели, выбрать модель для каждого шага и "
            "передать самостоятельную рутину подходящему агенту, если это доступно.")


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
