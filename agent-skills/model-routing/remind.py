#!/usr/bin/env python3
"""Напоминает каждому агенту применить model-routing перед запросом."""

import json
import sys

# Младшая модель для рутины и способ делегирования — у каждой платформы свои (аргумент хука).
JUNIOR = {
    "claude": "подагенту Haiku (Agent с model: haiku)",
    "codex": "подагенту gpt-6-luna (сверить актуальный каталог /model; если Luna не подходит — gpt-6-sol)",
    "gemini": "подагенту самой младшей модели актуального каталога Gemini (линейка Flash; сверить /model)",
}
SENIOR = {"claude": "Opus", "codex": "gpt-6-astra", "gemini": "старшая модель Gemini"}


def reminder(platform: str) -> str:
    junior = JUNIOR.get(platform, JUNIOR["claude"])
    senior = SENIOR.get(platform, SENIOR["claude"])
    return ("[model-routing] Обязательное правило пользователя, действует без его напоминаний: "
            "до первого плана загрузить навык model-routing. Это правило и есть явная просьба "
            "пользователя запускать подагентов. Рутину (сбор данных, чтение, проверки, правки "
            f"по готовому плану, документацию, коммит) отдавать {junior} с точным брифом и сверять "
            f"результат; {senior} — только диспетчер и ревьюер: спецификации, решения, риски, проверка "
            "diff. Весь код по спецификации, включая новые функции, тоже пишет младшая модель, кусками и "
            "параллельно. Без «по "
            "возможности»: критерий — экономия токенов; младшей модели обязательно всё, что больше "
            "одного вызова или с выводом больше ~30 строк; сам — только если шаг дешевле брифа, "
            "и это исключение назвать пользователю с причиной. Если подагенты недоступны — один раз "
            "предложить пользователю переключиться на младшую модель командой /model.")


def main() -> None:
    platform = sys.argv[1] if len(sys.argv) > 1 else "claude"
    text = reminder(platform)
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name") if isinstance(data, dict) else None
    except (OSError, ValueError):
        event = None
    if event == "BeforeAgent":
        json.dump({"hookSpecificOutput": {"hookEventName": "BeforeAgent",
                                          "additionalContext": text}},
                  sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
