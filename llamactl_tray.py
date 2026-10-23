#!/usr/bin/python3
"""Иконка llama.cpp в системном трее — графическая обёртка над llamactl.sh.

Каждые несколько секунд опрашивает `llamactl.sh status --json` и показывает
состояние цветом иконки, VRAM/RAM в подсказке и меню действий: загрузка и
выгрузка моделей, освобождение VRAM, сервисы, логи, проверка, пресеты,
добавление моделей, замер скорости и обновление llama.cpp.

Быстрые действия выполняются в фоне с уведомлением о результате; долгие и
интерактивные (обновление, добавление модели, редактор пресетов, логи)
открываются в терминале, где виден их прогресс.

Используется системный Python (`/usr/bin/python3`) с пакетом python-pyqt6.
`--print-menu` печатает меню без графики (для тестов и отладки).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LLAMACTL = SCRIPT_DIR / "llamactl.sh"
DEFAULT_TERMINAL = "konsole --hold -e"
GIB = 1024 ** 3

# Цвета иконки по состоянию
STATE_COLORS = {
    "down": "#8a8a8a",     # сервис остановлен или роутер не отвечает
    "idle": "#3d8ee8",     # всё работает, модели не загружены
    "loading": "#e8b33d",  # модель загружается
    "busy": "#3dbb5a",     # модель загружена
    "warn": "#e8743d",     # FIM не работает, сторонние llama-server и т.п.
}
STATE_TITLES = {
    "down": "остановлен",
    "idle": "ожидает",
    "loading": "загружает модель",
    "busy": "модель загружена",
    "warn": "есть проблемы",
}


@dataclass
class Item:
    """Пункт меню. action — аргументы llamactl; mode: bg | term | open | quit."""

    label: str
    action: list[str] | None = None
    mode: str = "bg"
    enabled: bool = True
    checked: bool | None = None
    children: list[Item] = field(default_factory=list)
    separator: bool = False


def classify(status: dict | None) -> str:
    """Определяет состояние для цвета иконки."""
    if not status:
        return "down"
    services = status.get("services", {})
    if services.get("server") != "active" or not status.get("router", {}).get("up"):
        return "down"
    if status.get("foreign") or services.get("fim") != "active" or not status.get("fim", {}).get("up"):
        return "warn"
    states = {m.get("status") for m in status.get("models", [])}
    if "loading" in states:
        return "loading"
    if states & {"loaded", "sleeping"}:
        return "busy"
    return "idle"


def tooltip(status: dict | None) -> str:
    """Текст подсказки иконки."""
    state = classify(status)
    lines = [f"llama.cpp: {STATE_TITLES[state]}"]
    if not status:
        lines.append("llamactl status не ответил")
        return "\n".join(lines)
    active = [f"{m['id']} ({m['status']})" for m in status.get("models", [])
              if m.get("status") in ("loaded", "sleeping", "loading")]
    lines.append("Модель: " + (", ".join(active) if active else "не загружена"))
    gpu = status.get("gpu", {})
    lines.append(f"VRAM: {gpu.get('used_mib', 0) / 1024:.1f} / {gpu.get('total_mib', 0) / 1024:.1f} ГиБ")
    ram = status.get("ram", {})
    lines.append(f"RAM свободно: {ram.get('available', 0) / GIB:.1f} ГиБ")
    foreign = status.get("foreign", [])
    if foreign:
        lines.append(f"Сторонние llama-server: {len(foreign)}")
    fim = status.get("services", {}).get("fim")
    if fim != "active":
        lines.append(f"FIM: {fim}")
    return "\n".join(lines)


def _ctx_label(ctx: str | None) -> str:
    if not ctx:
        return ""
    n = int(ctx)
    return f" — {n // 1024}k" if n >= 1024 else f" — {n}"


def build_menu(status: dict | None, dry_run: bool = False) -> list[Item]:
    """Строит дерево меню по статусу (без Qt, тестируемо)."""
    items: list[Item] = []
    state = classify(status)
    gpu = (status or {}).get("gpu", {})
    head = f"llama.cpp: {STATE_TITLES[state]}"
    if status:
        head += f" | VRAM {gpu.get('used_mib', 0) / 1024:.1f}/{gpu.get('total_mib', 0) / 1024:.1f} ГиБ"
    if dry_run:
        head += " | DRY-RUN"
    items.append(Item(head, enabled=False))
    items.append(Item("Открыть веб-чат", mode="open"))
    items.append(Item("", separator=True))

    router_up = bool(status and status.get("router", {}).get("up"))
    models_menu = Item("Модели", enabled=router_up)
    for m in (status or {}).get("models", []):
        loaded = m.get("status") in ("loaded", "sleeping", "loading")
        label = f"{m['id']}{_ctx_label(m.get('ctx'))} [{m.get('status')}]"
        action = ["unload", m["id"]] if loaded else ["load", m["id"]]
        models_menu.children.append(Item(label, action=action, checked=loaded))
    models_menu.children.append(Item("", separator=True))
    models_menu.children.append(Item("Выгрузить все", action=["unload", "all"]))
    items.append(models_menu)
    items.append(Item("Освободить VRAM (выгрузить всё, остановить FIM)", action=["free-vram"]))

    foreign = (status or {}).get("foreign", [])
    if foreign:
        items.append(Item(f"Завершить сторонние llama-server ({len(foreign)})", action=["kill-foreign"]))
    items.append(Item("", separator=True))

    svc = Item("Сервисы")
    services = (status or {}).get("services", {})
    svc.children.append(Item(f"роутер: {services.get('server', '?')}, FIM: {services.get('fim', '?')}", enabled=False))
    svc.children.append(Item("Перезапустить всё", action=["restart", "all"]))
    svc.children.append(Item("Перезапустить роутер", action=["restart", "server"]))
    svc.children.append(Item("Перезапустить FIM", action=["restart", "fim"]))
    svc.children.append(Item("Запустить всё", action=["start", "all"]))
    svc.children.append(Item("Остановить всё", action=["stop", "all"]))
    items.append(svc)

    logs = Item("Логи")
    logs.children.append(Item("Роутер (в терминале)", action=["logs", "server", "-f"], mode="term"))
    logs.children.append(Item("FIM (в терминале)", action=["logs", "fim", "-f"], mode="term"))
    items.append(logs)
    items.append(Item("Проверка (doctor)", action=["doctor"]))
    items.append(Item("", separator=True))

    presets = Item("Пресеты")
    presets.children.append(Item("Редактировать пресеты (в терминале)", action=["preset", "edit"], mode="term"))
    fit = Item("Раскладка по GPU (fit)")
    bench = Item("Замер скорости")
    for m in (status or {}).get("models", []):
        fit.children.append(Item(m["id"], action=["fit", m["id"]]))
        bench.children.append(Item(m["id"], action=["bench", m["id"]]))
    presets.children.extend([fit, bench])
    items.append(presets)
    items.append(Item("Добавить модель с Hugging Face…", action=["add"], mode="term"))
    items.append(Item("Обновить llama.cpp (с замером)", action=["update", "--bench"], mode="term"))
    items.append(Item("Консольное меню llamactl", action=[], mode="term"))
    items.append(Item("", separator=True))
    items.append(Item("Выход", mode="quit"))
    return items


def render_text(items: list[Item], depth: int = 0) -> str:
    """Текстовое представление меню для --print-menu."""
    out = []
    for it in items:
        pad = "  " * depth
        if it.separator:
            out.append(pad + "---")
            continue
        mark = "" if it.checked is None else ("[x] " if it.checked else "[ ] ")
        extra = []
        if not it.enabled:
            extra.append("disabled")
        if it.action is not None and it.mode in ("bg", "term"):
            extra.append(f"{it.mode}: llamactl {' '.join(it.action)}".rstrip())
        elif it.mode in ("open", "quit"):
            extra.append(it.mode)
        suffix = f"  <{'; '.join(extra)}>" if extra else ""
        out.append(f"{pad}{mark}{it.label}{suffix}")
        if it.children:
            out.append(render_text(it.children, depth + 1))
    return "\n".join(out)


def llamactl_cmd(args: list[str], dry_run: bool) -> list[str]:
    return [str(LLAMACTL), *(["--dry-run"] if dry_run else []), *args]


def read_status(status_file: str | None) -> dict | None:
    """Статус из файла (тесты) или из llamactl status --json."""
    try:
        if status_file:
            return json.loads(Path(status_file).read_text(encoding="utf-8"))
        res = subprocess.run([str(LLAMACTL), "status", "--json"], capture_output=True, text=True, timeout=20, check=False)
        return json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run_gui(opts: argparse.Namespace) -> int:
    """Графический режим (PyQt6 импортируется только здесь)."""
    from PyQt6.QtCore import QProcess, Qt, QTimer
    from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication, QInputDialog, QMenu, QSystemTrayIcon

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("llamactl-tray")
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Системный трей недоступен", file=sys.stderr)
        return 1

    def make_icon(state: str) -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(STATE_COLORS[state]))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        p.setPen(QColor("white"))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(38)
        p.setFont(font)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "L")
        p.end()
        return QIcon(pm)

    icons = {s: make_icon(s) for s in STATE_COLORS}
    tray = QSystemTrayIcon(icons["down"])
    menu = QMenu()
    tray.setContextMenu(menu)
    st: dict = {"status": None, "running": set(), "poll": None}
    terminal = shlex.split(opts.terminal)

    def notify(title: str, text: str, error: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
        tray.showMessage(title, text[-600:] or "готово", icon, 8000)

    def router_url() -> str:
        return ((st["status"] or {}).get("router", {}) or {}).get("url", "http://127.0.0.1:8080")

    def run_bg(args: list[str], label: str) -> None:
        key = " ".join(args)
        if key in st["running"]:
            notify("llamactl", f"уже выполняется: {label}")
            return
        st["running"].add(key)
        proc = QProcess(app)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        def done(code: int, _status) -> None:
            out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace").strip()
            st["running"].discard(key)
            tail = "\n".join(out.splitlines()[-8:])
            notify(f"llamactl: {label}" + (" — ошибка" if code else ""), tail, error=bool(code))
            refresh()
            proc.deleteLater()

        proc.finished.connect(done)
        cmd = llamactl_cmd(args, opts.dry_run)
        proc.start(cmd[0], cmd[1:])
        tray.setToolTip(f"{tray.toolTip()}\nВыполняется: {label}")

    def run_term(args: list[str]) -> None:
        if args == ["add"]:
            repo, ok = QInputDialog.getText(None, "Добавить модель", "Репозиторий Hugging Face (user/repo):")
            if not ok or not repo.strip():
                return
            fname, ok = QInputDialog.getText(None, "Добавить модель", "Файл .gguf в репозитории:")
            if not ok or not fname.strip():
                return
            mmproj, ok = QInputDialog.getText(None, "Добавить модель", "mmproj для картинок (пусто — без него):")
            args = ["add", repo.strip(), fname.strip()]
            if ok and mmproj.strip():
                args += ["--mmproj", mmproj.strip()]
        subprocess.Popen([*terminal, *llamactl_cmd(args, opts.dry_run)], start_new_session=True)

    def trigger(item: Item) -> None:
        if item.mode == "quit":
            app.quit()
        elif item.mode == "open":
            subprocess.Popen(["xdg-open", router_url()], start_new_session=True)
        elif item.mode == "term":
            run_term(item.action or [])
        elif item.action is not None:
            run_bg(item.action, item.label)

    def fill(qmenu: QMenu, items: list[Item]) -> None:
        for it in items:
            if it.separator:
                qmenu.addSeparator()
            elif it.children:
                sub = qmenu.addMenu(it.label)
                sub.setEnabled(it.enabled)
                fill(sub, it.children)
            else:
                act = QAction(it.label, qmenu)
                act.setEnabled(it.enabled)
                if it.checked is not None:
                    act.setCheckable(True)
                    act.setChecked(it.checked)
                act.triggered.connect(lambda _=False, i=it: trigger(i))
                qmenu.addAction(act)

    def apply_status(status: dict | None) -> None:
        st["status"] = status
        state = classify(status)
        tray.setIcon(icons[state])
        tip = tooltip(status)
        if st["running"]:
            tip += "\nВыполняется: " + ", ".join(sorted(st["running"]))
        tray.setToolTip(tip)
        if not menu.isVisible():
            menu.clear()
            fill(menu, build_menu(status, opts.dry_run))

    def refresh() -> None:
        if st["poll"] is not None:
            return
        proc = QProcess(app)
        st["poll"] = proc

        def done(code: int, _status) -> None:
            raw = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            st["poll"] = None
            try:
                status = json.loads(raw) if code == 0 and raw.strip() else None
            except ValueError:
                status = None
            apply_status(status)
            proc.deleteLater()

        proc.finished.connect(done)
        proc.start(str(LLAMACTL), ["status", "--json"])

    def on_activated(reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            subprocess.Popen(["xdg-open", router_url()], start_new_session=True)

    tray.activated.connect(on_activated)
    apply_status(None)
    tray.show()
    timer = QTimer(app)
    timer.timeout.connect(refresh)
    timer.start(max(2, opts.interval) * 1000)
    refresh()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Иконка llama.cpp в трее (обёртка над llamactl.sh)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="действия выполнять как llamactl --dry-run (только план)")
    parser.add_argument("--interval", type=int, default=5, help="период опроса статуса, с (по умолчанию 5)")
    parser.add_argument("--terminal", default=os.environ.get("LLAMACTL_TERMINAL", DEFAULT_TERMINAL),
                        help=f"команда терминала для долгих действий (по умолчанию «{DEFAULT_TERMINAL}»)")
    parser.add_argument("--print-menu", action="store_true", help="напечатать состояние и меню без графики")
    parser.add_argument("--status-file", help="взять статус из JSON-файла вместо llamactl (для тестов)")
    opts = parser.parse_args(argv)
    if opts.print_menu:
        status = read_status(opts.status_file)
        print(f"state: {classify(status)}")
        print(tooltip(status))
        print(render_text(build_menu(status, opts.dry_run)))
        return 0
    return run_gui(opts)


if __name__ == "__main__":
    sys.exit(main())
