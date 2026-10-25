#!/usr/bin/env bats

# Тесты для check_mytasks.sh
# Скрипт и шаблоны копируются во временный каталог (WORK), HOME — временный,
# systemctl, crontab, apt-get, pacman, dnf и notify-send заменены стабами,
# SUDO_CMD="" — root-шаги выполняются напрямую в каталоги внутри TMP_DIR.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  WORK="$TMP_DIR/mytasks"
  mkdir -p "$WORK/conf" "$WORK/systemd/user" "$WORK/systemd/autostart"
  WORK="$(cd "$WORK" && pwd -P)"
  STUB_DIR="$TMP_DIR/stubs"
  STATE="$TMP_DIR/state"
  CALLS="$TMP_DIR/calls.log"
  export HOME="$TMP_DIR/home"
  export XDG_CONFIG_HOME="$HOME/.config"
  export MYTASKS_LOG_DIR="$TMP_DIR/logs"
  unset DISPLAY WAYLAND_DISPLAY SUDO_ASKPASS
  USER_UNITS="$HOME/.config/systemd/user"
  SYS_UNITS="$TMP_DIR/etc-systemd"
  mkdir -p "$STUB_DIR" "$STATE/enabled" "$STATE/ext" "$HOME" "$SYS_UNITS" "$USER_UNITS"
  export STATE CALLS USER_UNITS SYS_UNITS

  cp "$REPO_ROOT/check_mytasks.sh" "$WORK/"
  cp "$REPO_ROOT/systemd/user/check-mytasks.service" "$WORK/systemd/user/"
  cp "$REPO_ROOT/conf/check_mytasks.conf.example" "$WORK/conf/"
  chmod +x "$WORK/check_mytasks.sh"
  printf '#!/bin/sh\necho demo\n' > "$WORK/demo.sh"
  chmod +x "$WORK/demo.sh"

  # Шаблоны демо-юнитов и автозапуска с плейсхолдером
  for u in "$WORK/systemd/user/demo" "$WORK/systemd/sysdemo"; do
    printf '[Unit]\nDescription=demo\n\n[Service]\nType=oneshot\nExecStart=@MYTASKS_DIR@/demo.sh\n' > "$u.service"
    printf '[Timer]\nOnCalendar=daily\n\n[Install]\nWantedBy=timers.target\n' > "$u.timer"
  done
  printf '[Desktop Entry]\nType=Application\nExec=@MYTASKS_DIR@/demo.sh\n' > "$WORK/systemd/autostart/demo.desktop"

  # Пример конфига для мастера
  cat > "$WORK/conf/demo.conf.example" <<'EOT'
# Демо-конфиг
# Шапка примера

# Каталог данных
DATA_DIR="@MYTASKS_DIR@/state"

# Число
COUNT=5

# Путь с переменной окружения
SRC="${HOME}/src"

# Список каталогов
ITEMS=(
    "@HOME@/a"
    # "закомментированный"
)
EOT
  printf '# Список строк\n@HOME@/music/|/mnt/x/|-a\n' > "$WORK/conf/lines.conf.example"

  cat > "$STATE/os-release" <<'EOT'
ID=garuda
ID_LIKE=arch
PRETTY_NAME="Garuda Linux"
EOT

  # systemctl: состояние в $STATE (enabled/<юнит>, ext/<юнит> — внешние юниты)
  cat > "$STUB_DIR/systemctl" <<'EOT'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
args=()
for a in "$@"; do [[ "$a" == "--user" || "$a" == "--" ]] || args+=("$a"); done
case "${args[0]}" in
  show)
    unit="${args[-1]}"
    if [[ -f "$USER_UNITS/$unit" || -f "$SYS_UNITS/$unit" || -f "$STATE/ext/$unit" ]]; then
      echo "LoadState=loaded"
    else
      echo "LoadState=not-found"
    fi
    if [[ -f "$STATE/enabled/$unit" ]]; then
      echo "UnitFileState=enabled"
      if [[ "$unit" == *.timer || -f "$STATE/ext/$unit" ]]; then echo "ActiveState=active"; else echo "ActiveState=inactive"; fi
    else
      echo "UnitFileState=disabled"
      echo "ActiveState=inactive"
    fi
    echo "Result=success"
    if [[ -f "$STATE/ext/$unit" ]]; then echo "Type=simple"; else echo "Type=oneshot"; fi
    ;;
  enable)
    for u in "${args[@]:1}"; do [[ "$u" == --now ]] || touch "$STATE/enabled/$u"; done
    ;;
  *) ;;
esac
exit 0
EOT
  # crontab: файл $STATE/crontab
  cat > "$STUB_DIR/crontab" <<'EOT'
#!/usr/bin/env bash
echo "crontab $*" >> "$CALLS"
if [[ "${1:-}" == "-l" ]]; then
  [[ -f "$STATE/crontab" ]] || exit 1
  cat "$STATE/crontab"
else
  cat "$1" > "$STATE/crontab"
fi
EOT
  # loginctl: состояние linger — файл $STATE/linger; enable-linger может «отказать» ($STATE/linger-denied)
  cat > "$STUB_DIR/loginctl" <<'EOT'
#!/usr/bin/env bash
echo "loginctl $*" >> "$CALLS"
case "$1" in
  show-user) [[ -f "$STATE/linger" ]] && echo yes || echo no ;;
  enable-linger) [[ -f "$STATE/linger-denied" ]] && exit 1; touch "$STATE/linger" ;;
  *) ;;
esac
EOT
  mkdir -p "$STATE/modules/$(uname -r)"
  for c in apt-get pacman dnf yum rpm-ostree zypper apk xbps-install emerge emaint eopkg garuda-update nixos-rebuild transactional-update notify-send; do
    printf '#!/usr/bin/env bash\necho "%s $*" >> "$CALLS"\nexit 0\n' "$c" > "$STUB_DIR/$c"
  done
  chmod +x "$STUB_DIR"/*
  export PATH="$STUB_DIR:$PATH"

  # Самоустановка уже выполнена: юнит автозагрузки на месте и включён
  sed "s|@MYTASKS_DIR@|$WORK|g" "$WORK/systemd/user/check-mytasks.service" > "$USER_UNITS/check-mytasks.service"
  touch "$STATE/enabled/check-mytasks.service"
}

teardown() {
  rm -rf "$TMP_DIR"
}

# Конфиг check_mytasks.sh: $1 — строки JOBS, $2 — строки DEPS, $3 — MYTASKS_DIR
write_conf() {
  cat > "$WORK/conf/check_mytasks.conf" <<EOT
MYTASKS_DIR="${3:-$WORK}"
OS_RELEASE_FILE="$STATE/os-release"
SUDO_CMD=""
UI="tty"
SYSTEM_UNIT_DIR="$SYS_UNITS"
USER_UNIT_DIR="$USER_UNITS"
AUTOSTART_DIR="$HOME/.config/autostart"
BIN_DIR="$HOME/.local/bin"
SYSTEM_PYTHON="$STUB_DIR/none-python"
SYSTEMD_RUN_DIR="${SYSTEMD_DIR:-$STATE}"
REBOOT_FLAG="$STATE/reboot-required"
KERNEL_MODULES_DIR="$STATE/modules"
GARUDA_UPDATE="${GARUDA:-auto}"
OSTREE_FLAG="$STATE/ostree-booted"
JOBS=(
$1
)
DEPS=(
$2
)
EOT
}

ALL_DEMO_JOBS='"user-unit|demo.timer|||Демо-таймер пользователя"
"system-unit|sysdemo.timer|||Системный демо-таймер"
"system-ext|@CRON_UNIT@||cmd:crontab|Демон cron"
"cron|demo.sh|0 1 * * *||Демо-задание cron"
"link|demo-link|demo.sh||Демо-ссылка"
"autostart|demo.desktop|||Демо-автозапуск"'

@test "--status печатает таблицу и ничего не меняет" {
  write_conf "$ALL_DEMO_JOBS" ""
  run "$WORK/check_mytasks.sh" --status
  [ "$status" -eq 1 ]
  [[ "$output" == *"Состояние сервисов и заданий"* ]]
  [[ "$output" == *"demo.timer"*"ВНИМАНИЕ"* ]]
  [[ "$output" == *"cronie.service"* ]]
  [[ "$output" == *"check-mytasks.service"*"OK"* ]]
  [ ! -e "$USER_UNITS/demo.timer" ]
  [ ! -e "$STATE/crontab" ]
  [ ! -e "$HOME/.local/bin/demo-link" ]
}

@test "dry-run печатает план и не меняет состояние" {
  write_conf "$ALL_DEMO_JOBS" '"democmd|demo-arch;debian=demo-deb;redhat=demo-rh||Демо-зависимость"'
  run "$WORK/check_mytasks.sh" --dry-run --no-gui
  [ "$status" -eq 1 ]
  [[ "$output" == *"[dry-run] будет: установить demo.service demo.timer"* ]]
  [[ "$output" == *"[dry-run] будет: добавить в crontab: 0 1 * * * $WORK/demo.sh"* ]]
  [[ "$output" == *"[dry-run] будет: установить пакеты (arch, pacman): demo-arch"* ]]
  [[ "$output" == *"[dry-run] будет: обновить систему: "* ]]
  [ ! -e "$USER_UNITS/demo.timer" ]
  [ ! -e "$SYS_UNITS/sysdemo.timer" ]
  [ ! -e "$STATE/crontab" ]
  [ ! -e "$HOME/.local/bin/demo-link" ]
  [ ! -e "$HOME/.config/autostart/demo.desktop" ]
  run grep -q 'pacman\|enable' "$CALLS"
  [ "$status" -ne 0 ]
  grep -q '"event":"dry_job_problem"' "$MYTASKS_LOG_DIR"/check_mytasks-*.jsonl
}

@test "без терминала и --yes только предупреждает" {
  write_conf "$ALL_DEMO_JOBS" ""
  run "$WORK/check_mytasks.sh" --no-gui < /dev/null
  [ "$status" -eq 1 ]
  [[ "$output" == *"⚠ юнит (польз.) demo.timer"* ]]
  [[ "$output" == *"Пропущено (не создавать)."* ]]
  [[ "$output" == *"Обновление системы пропущено"* ]]
  run grep -qE 'garuda-update|pacman' "$CALLS"
  [ "$status" -ne 0 ]
  [ ! -e "$USER_UNITS/demo.timer" ]
  [ ! -e "$STATE/crontab" ]
}

@test "ответ «да» создаёт юнит с путём mytasks, «нет» — не создаёт" {
  write_conf '"user-unit|demo.timer|||Демо-таймер"
"link|demo-link|demo.sh||Демо-ссылка"' ""
  run bash -c "printf 'да\nнет\n' | '$WORK/check_mytasks.sh' --ask --no-gui"
  [ -f "$USER_UNITS/demo.service" ]
  [ -f "$USER_UNITS/demo.timer" ]
  grep -q "ExecStart=$WORK/demo.sh" "$USER_UNITS/demo.service"
  run grep -c '@MYTASKS_DIR@' "$USER_UNITS/demo.service"
  [ "$output" = "0" ]
  grep -q 'systemctl --user enable --now -- demo.timer' "$CALLS"
  [ ! -e "$HOME/.local/bin/demo-link" ]
}

@test "долгоживущая служба (не oneshot) включается сразу, oneshot — только enable" {
  printf '[Unit]\nDescription=daemon\n\n[Service]\nType=simple\nExecStart=@MYTASKS_DIR@/demo.sh\n\n[Install]\nWantedBy=default.target\n' \
    > "$WORK/systemd/user/daemondemo.service"
  printf '[Unit]\nDescription=login\n\n[Service]\nType=oneshot\nExecStart=@MYTASKS_DIR@/demo.sh\n\n[Install]\nWantedBy=default.target\n' \
    > "$WORK/systemd/user/logindemo.service"
  write_conf '"user-unit|daemondemo.service|||Демо-служба"
"user-unit|logindemo.service|||Демо-вход"' ""
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ -f "$USER_UNITS/daemondemo.service" ]
  grep -q 'systemctl --user enable --now -- daemondemo.service' "$CALLS"
  grep -q 'systemctl --user enable -- logindemo.service' "$CALLS"
  run grep -q 'enable --now -- logindemo.service' "$CALLS"
  [ "$status" -ne 0 ]
}

@test "linger: dry-run не включает, --yes включает, включённый — OK" {
  write_conf '"linger|enable-linger||cmd:loginctl|Демо-linger"' ""
  run "$WORK/check_mytasks.sh" --dry-run --no-gui
  [[ "$output" == *"[dry-run] будет: loginctl enable-linger"* ]]
  [ ! -e "$STATE/linger" ]
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ -e "$STATE/linger" ]
  grep -q "loginctl enable-linger -- $(id -un)" "$CALLS"
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"enable-linger"*"OK"* ]]
}

@test "linger: отказ и без прав, и с повышением — предупреждение, не сбой" {
  write_conf '"linger|enable-linger||cmd:loginctl|Демо-linger"' ""
  touch "$STATE/linger-denied"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 1 ]
  [ ! -e "$STATE/linger" ]
  [ "$(grep -c "loginctl enable-linger -- $(id -un)" "$CALLS")" -eq 2 ]  # сам и через __root-linger
  [[ "$output" == *"Не удалось: loginctl enable-linger"* ]]
}

health_log() {  # $1 — время последней записи (date -d), $2 — уровень
  mkdir -p "$MYTASKS_LOG_DIR"
  printf '{"@timestamp": "%s", "log.level": "%s", "message": "В эфире: A - B"}\n' \
    "$(date -d "$1" +%Y-%m-%dT%H:%M:%S)" "${2:-INFO}" > "$MYTASKS_LOG_DIR/demo_rec-2026-10-25.jsonl"
}

@test "health: свежий лог без ошибок — OK" {
  write_conf '"health|demo-rec|demo_rec-*.jsonl:20||Демо-проверка"' ""
  health_log "5 minutes ago"
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"demo-rec"*"работает, 5 мин назад"*"OK"* ]]
}

@test "health: молчащая служба — ВНИМАНИЕ, --dry-run только план, --yes перезапускает" {
  write_conf '"health|demo-rec|demo_rec-*.jsonl:20||Демо-проверка"' ""
  health_log "45 minutes ago"
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"молчит 45 мин"*"ВНИМАНИЕ"* ]]
  run "$WORK/check_mytasks.sh" --dry-run --no-gui
  [[ "$output" == *"[dry-run] будет: systemctl --user restart demo-rec.service"* ]]
  run grep -q 'restart' "$CALLS"
  [ "$status" -ne 0 ]
  run "$WORK/check_mytasks.sh" --yes --no-gui
  grep -q 'systemctl --user restart -- demo-rec.service' "$CALLS"
}

@test "health: ошибки в логе и отсутствие лога — ВНИМАНИЕ" {
  write_conf '"health|demo-rec|demo_rec-*.jsonl:20||Демо-проверка"' ""
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"нет лога"*"ВНИМАНИЕ"* ]]
  health_log "1 minute ago" ERROR
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"ошибки: 1"*"ВНИМАНИЕ"* ]]
}

@test "--yes создаёт юниты, cron, ссылку и автозапуск; итог OK" {
  write_conf "$ALL_DEMO_JOBS" ""
  touch "$STATE/ext/cronie.service" "$STATE/enabled/cronie.service"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 0 ]
  [ -f "$SYS_UNITS/sysdemo.timer" ]
  grep -q "ExecStart=$WORK/demo.sh" "$SYS_UNITS/sysdemo.service"
  grep -q "^0 1 \* \* \* $WORK/demo.sh$" "$STATE/crontab"
  [ "$(readlink "$HOME/.local/bin/demo-link")" = "$WORK/demo.sh" ]
  grep -q "Exec=$WORK/demo.sh" "$HOME/.config/autostart/demo.desktop"
  [[ "$output" == *"Итоговое состояние"* ]]
  [[ "$output" == *"Все сервисы и задания на месте."* ]]
}

@test "повторный запуск после --yes ничего не дублирует" {
  write_conf "$ALL_DEMO_JOBS" ""
  touch "$STATE/ext/cronie.service" "$STATE/enabled/cronie.service"
  "$WORK/check_mytasks.sh" --yes --no-gui
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 0 ]
  run grep -c "demo.sh" "$STATE/crontab"
  [ "$output" = "1" ]
}

@test "юнит, отличающийся от шаблона, обновляется" {
  write_conf '"user-unit|demo.timer|||Демо-таймер"' ""
  printf 'old\n' > "$USER_UNITS/demo.service"
  sed "s|@MYTASKS_DIR@|$WORK|g" "$WORK/systemd/user/demo.timer" > "$USER_UNITS/demo.timer"
  touch "$STATE/enabled/demo.timer"
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"demo.timer"*"отличается"* ]]
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 0 ]
  grep -q "ExecStart=$WORK/demo.sh" "$USER_UNITS/demo.service"
}

@test "cron со старым путём переписывается на каталог mytasks" {
  write_conf '"cron|demo.sh|0 1 * * *||Демо-задание cron"' ""
  printf '# свой комментарий\n0 1 * * * /old/place/demo.sh\n' > "$STATE/crontab"
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"другой путь"* ]]
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 0 ]
  grep -q "^0 1 \* \* \* $WORK/demo.sh$" "$STATE/crontab"
  grep -q '^# свой комментарий$' "$STATE/crontab"
  run grep -c 'old/place' "$STATE/crontab"
  [ "$output" = "0" ]
}

@test "другой каталог в конфиге: предупреждение и запись каталога запуска" {
  write_conf "" "" "/old/place"
  run "$WORK/check_mytasks.sh" --status
  [ "$status" -eq 1 ]
  [[ "$output" == *"другой каталог"* ]]
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 0 ]
  grep -q "^MYTASKS_DIR=\"$WORK\"$" "$WORK/conf/check_mytasks.conf"
}

@test "семейство debian: cron.service и пакеты через apt-get" {
  write_conf '"system-ext|@CRON_UNIT@||cmd:crontab|Демон cron"' '"democmd|demo-arch;debian=demo-deb;redhat=demo-rh;suse=demo-suse||Демо"
"optcmd|opt-arch;debian=opt-deb|opt|Необязательная"'
  printf 'ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu"\n' > "$STATE/os-release"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"семейство debian, пакеты — apt-get"* ]]
  [[ "$output" == *"cron.service"* ]]
  grep -q 'apt-get install -y -- demo-deb' "$CALLS"
  grep -q 'apt-get -y full-upgrade' "$CALLS"
  run grep -q 'opt-deb' "$CALLS"
  [ "$status" -ne 0 ]
}

@test "семейство redhat: crond.service и dnf" {
  write_conf '"system-ext|@CRON_UNIT@||cmd:crontab|Демон cron"' '"democmd|demo-arch;debian=demo-deb;redhat=demo-rh;suse=demo-suse||Демо"'
  printf 'ID=fedora\nPRETTY_NAME="Fedora"\n' > "$STATE/os-release"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"семейство redhat"* ]]
  [[ "$output" == *"crond.service"* ]]
  grep -q 'dnf install -y -- demo-rh' "$CALLS"
  grep -q 'dnf -y upgrade --refresh' "$CALLS"
}

@test "производные дистрибутивы: Mint → debian, Nobara → redhat, Tumbleweed → suse, Artix → arch" {
  write_conf "" ""
  for pair in "linuxmint ubuntu:debian" "nobara fedora:redhat" "opensuse-tumbleweed opensuse suse:suse" "artix arch:arch" "postmarketos alpine:alpine" "funtoo gentoo:gentoo"; do
    ids="${pair%%:*}"; fam="${pair##*:}"
    printf 'ID=%s\nID_LIKE="%s"\nPRETTY_NAME="T"\n' "${ids%% *}" "${ids#* }" > "$STATE/os-release"
    run "$WORK/check_mytasks.sh" --status
    [[ "$output" == *"(семейство $fam,"* ]] || { echo "$pair: $output" | head -3; return 1; }
  done
}

@test "семейство suse: zypper и cron.service" {
  write_conf '"system-ext|@CRON_UNIT@||cmd:crontab|Демон cron"' '"democmd|demo-arch;debian=demo-deb;redhat=demo-rh;suse=demo-suse||Демо"'
  printf 'ID="opensuse-leap"\nID_LIKE="suse opensuse"\nPRETTY_NAME="openSUSE Leap"\n' > "$STATE/os-release"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"cron.service"* ]]
  grep -q 'zypper --non-interactive install -- demo-suse' "$CALLS"
  grep -q 'zypper --non-interactive update' "$CALLS"
}

@test "Fedora Atomic: пакеты через rpm-ostree с предупреждением о перезагрузке" {
  write_conf "" '"democmd|demo-arch;redhat=demo-rh||Демо"'
  printf 'ID=fedora\nVARIANT_ID=kinoite\nPRETTY_NAME="Fedora Kinoite"\n' > "$STATE/os-release"
  touch "$STATE/ostree-booted"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"неизменяемая система (kinoite)"* ]]
  grep -q 'rpm-ostree install --idempotent --assumeyes -- demo-rh' "$CALLS"
  grep -q 'rpm-ostree upgrade' "$CALLS"
}

@test "Alpine без systemd: юниты не создаются, cron проверяется по процессу" {
  export SYSTEMD_DIR="$TMP_DIR/no-systemd"
  write_conf '"user-unit|demo.timer|||Демо-таймер"
"system-ext|@CRON_UNIT@||cmd:crontab|Демон cron"' '"democmd|demo-arch;alpine=demo-apk||Демо"'
  printf 'ID=alpine\nPRETTY_NAME="Alpine Linux"\n' > "$STATE/os-release"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"systemd не запущен"* ]]
  [[ "$output" == *"демон cron"* ]]
  [ ! -e "$USER_UNITS/demo.timer" ]
  grep -q 'apk add -- demo-apk' "$CALLS"
  grep -q 'apk upgrade' "$CALLS"
}

@test "NixOS: предупреждение, пакеты не ставятся" {
  write_conf "" '"democmd|demo-arch||Демо"'
  printf 'ID=nixos\nPRETTY_NAME="NixOS"\n' > "$STATE/os-release"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"NixOS: пакеты и службы объявляются в configuration.nix"* ]]
  grep -q 'nixos-rebuild switch --upgrade' "$CALLS"
  [[ "$output" == *"установите вручную"* ]]
  run grep -qE 'pacman|apt-get|dnf|zypper|apk' "$CALLS"
  [ "$status" -ne 0 ]
}

@test "неизвестная ОС: явное предупреждение, пакеты вручную" {
  write_conf "" '"democmd|demo-arch||Демо"'
  printf 'ID=slackware\nPRETTY_NAME="Slackware 15.0"\n' > "$STATE/os-release"
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"ОС «Slackware 15.0» (ID=slackware) не распознана"* ]]
  [[ "$output" == *"задайте OS_FAMILY"* ]]
  [[ "$output" == *"автоматически не выполняется — обновите её вручную"* ]]
  run grep -qE 'pacman|apt-get|dnf|zypper|apk' "$CALLS"
  [ "$status" -ne 0 ]
  grep -q '"event":"os_note"' "$MYTASKS_LOG_DIR"/check_mytasks-*.jsonl
}

@test "обновление системы — первый шаг: Garuda — garuda-update до автозагрузки и пакетов" {
  rm -f "$USER_UNITS/check-mytasks.service" "$STATE/enabled/check-mytasks.service"
  write_conf "" '"democmd|demo-arch||Демо"'
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"Обновление системы — обязательный первый шаг: garuda-update --noconfirm"* ]]
  [[ "$output" == *"✔ система обновлена"* ]]
  first="$(grep -nE 'garuda-update|systemctl --user enable|pacman -S --needed' "$CALLS" | head -1)"
  [[ "$first" == *"garuda-update --noconfirm"* ]]
  grep -q 'pacman -S --needed --noconfirm -- demo-arch' "$CALLS"
}

@test "обновление системы: Arch без garuda-update — pacman -Syu" {
  export GARUDA=no
  write_conf "" ""
  run "$WORK/check_mytasks.sh" --yes --no-gui
  grep -q 'pacman -Syu --noconfirm' "$CALLS"
  run grep -q 'garuda-update' "$CALLS"
  [ "$status" -ne 0 ]
}

@test "обновление системы: Tumbleweed — zypper dup, Void — xbps, Gentoo — emerge @world, Solus — eopkg" {
  write_conf "" ""
  for pair in 'opensuse-tumbleweed:zypper --non-interactive dup' 'void:xbps-install -Syu' 'gentoo:emerge --update --deep --newuse --ask=n @world' 'solus:eopkg upgrade -y' 'opensuse-microos:transactional-update --non-interactive dup'; do
    printf 'ID=%s\nPRETTY_NAME="T"\n' "${pair%%:*}" > "$STATE/os-release"
    : > "$CALLS"
    "$WORK/check_mytasks.sh" --yes --no-gui >/dev/null 2>&1 || true
    grep -qF -- "${pair#*:}" "$CALLS" || { echo "нет «${pair#*:}» для ${pair%%:*}"; cat "$CALLS"; return 1; }
  done
}

@test "обновление не удалось: пакеты не ставятся, остальное проверяется" {
  printf '#!/usr/bin/env bash\necho "garuda-update $*" >> "$CALLS"\nexit 1\n' > "$STUB_DIR/garuda-update"
  write_conf '"link|demo-link|demo.sh||Демо-ссылка"' '"democmd|demo-arch||Демо"'
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"Обновление системы не удалось (код 1)"* ]]
  [[ "$output" == *"не ставятся: система не обновлена"* ]]
  run grep -q 'pacman -S --needed' "$CALLS"
  [ "$status" -ne 0 ]
  [ "$(readlink "$HOME/.local/bin/demo-link")" = "$WORK/demo.sh" ]
}

@test "после обновления ядра — предупреждение о перезагрузке" {
  rm -rf "$STATE/modules/$(uname -r)"
  write_conf "" ""
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [[ "$output" == *"нужна перезагрузка"* ]]
  [[ "$output" == *"Перезагрузите компьютер"* ]]
}

@test "мастер конфига: значения по умолчанию с подстановкой путей" {
  write_conf '"conf|conf/demo.conf|conf/demo.conf.example||Демо-конфиг"' ""
  run "$WORK/check_mytasks.sh" --yes --no-gui
  [ "$status" -eq 0 ]
  grep -q "^DATA_DIR=\"$WORK/state\"$" "$WORK/conf/demo.conf"
  grep -q '^COUNT=5$' "$WORK/conf/demo.conf"
  grep -qF 'SRC="${HOME}/src"' "$WORK/conf/demo.conf"
  grep -qF "\"$HOME/a\"" "$WORK/conf/demo.conf"
  grep -q '# "закомментированный"' "$WORK/conf/demo.conf"
}

@test "мастер конфига: настройка каждого значения в терминале" {
  write_conf '"conf|conf/demo.conf|conf/demo.conf.example||Демо-конфиг"' ""
  run bash -c "printf '2\n/data\n\n\nи\nx\ny z\n.\n' | '$WORK/check_mytasks.sh' --ask --no-gui"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Каталог данных"* ]]
  [[ "$output" == *"По умолчанию: $WORK/state"* ]]
  grep -q '^DATA_DIR="/data"$' "$WORK/conf/demo.conf"
  grep -q '^COUNT=5$' "$WORK/conf/demo.conf"
  grep -qF 'SRC="${HOME}/src"' "$WORK/conf/demo.conf"
  grep -q '^    "y z"$' "$WORK/conf/demo.conf"
  run bash -c "source '$WORK/conf/demo.conf'; echo \"\${#ITEMS[@]}:\${ITEMS[1]}\""
  [ "$output" = "2:y z" ]
}

@test "мастер конфига: «Не создавать» не создаёт и не спрашивает повторно" {
  write_conf '"conf|conf/demo.conf|conf/demo.conf.example||Демо-конфиг"' ""
  run bash -c "printf '3\n' | '$WORK/check_mytasks.sh' --ask --no-gui"
  [ "$status" -eq 1 ]
  [ ! -e "$WORK/conf/demo.conf" ]
  run grep -c '3) Не создавать' <<<"$output"
  [ "$output" = "1" ]
}

@test "первый запуск: автозагрузка, конфиг с каталогом запуска и мастер конфигов" {
  rm -f "$USER_UNITS/check-mytasks.service" "$STATE/enabled/check-mytasks.service"
  rm -f "$WORK/conf/check_mytasks.conf"
  run bash -c "'$WORK/check_mytasks.sh' --yes --no-gui --dry-run"
  [[ "$output" == *"Нет conf/check_mytasks.conf — первый запуск"* ]]
  [[ "$output" == *"[dry-run] будет: создать conf/demo.conf по conf/demo.conf.example"* ]]
  [ ! -e "$WORK/conf/check_mytasks.conf" ]
  [ ! -e "$USER_UNITS/check-mytasks.service" ]

  run bash -c "'$WORK/check_mytasks.sh' --yes --no-gui"
  [[ "$output" == *"Сам check_mytasks.sh при входе в сеанс"* ]]
  [ -f "$USER_UNITS/check-mytasks.service" ]
  grep -q "ExecStart=$WORK/check_mytasks.sh --no-create --notify" "$USER_UNITS/check-mytasks.service"
  grep -q 'systemctl --user enable -- check-mytasks.service' "$CALLS"
  grep -q "^MYTASKS_DIR=\"$WORK\"$" "$WORK/conf/check_mytasks.conf"
  grep -qE '^OS_FAMILY="(arch|debian|redhat)"$' "$WORK/conf/check_mytasks.conf"
  grep -q "^USER_UNIT_DIR=\"$HOME/.config/systemd/user\"$" "$WORK/conf/check_mytasks.conf"
  [ -f "$WORK/conf/demo.conf" ]
  grep -qF "$HOME/music/|/mnt/x/|-a" "$WORK/conf/lines.conf"
}

@test "«стоп» прерывает весь запуск: дальше ничего не спрашивается и не создаётся" {
  write_conf '"user-unit|demo.timer|||Демо-таймер"
"link|demo-link|demo.sh||Демо-ссылка"
"conf|conf/demo.conf|conf/demo.conf.example||Демо-конфиг"' '"democmd|demo-arch;debian=demo-deb;redhat=demo-rh;suse=demo-suse||Демо"'
  run bash -c "printf 'стоп\nда\nда\n1\n' | '$WORK/check_mytasks.sh' --ask --no-gui"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Остановлено кнопкой «Стоп»"* ]]
  [[ "$output" == *"Итоговое состояние"* ]]
  [[ "$output" == *"остановлен кнопкой «Стоп»"* ]]
  run grep -c '^=== mytasks' <<<"$output"
  [ "$output" = "1" ]
  [ ! -e "$USER_UNITS/demo.timer" ]
  [ ! -e "$HOME/.local/bin/demo-link" ]
  [ ! -e "$WORK/conf/demo.conf" ]
  run grep -q 'pacman -S --needed' "$CALLS"
  [ "$status" -ne 0 ]
  grep -q '"event":"stopped"' "$MYTASKS_LOG_DIR"/check_mytasks-*.jsonl
}

@test "«стоп» в мастере конфига: конфиг не пишется, остальное не спрашивается" {
  write_conf '"conf|conf/demo.conf|conf/demo.conf.example||Демо-конфиг"
"link|demo-link|demo.sh||Демо-ссылка"' ""
  run bash -c "printf '2\n/data\nстоп\n' | '$WORK/check_mytasks.sh' --ask --no-gui"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Остановлено кнопкой «Стоп»"* ]]
  [ ! -e "$WORK/conf/demo.conf" ]
  [ ! -e "$HOME/.local/bin/demo-link" ]
}

@test "--no-create --notify: уведомление о проблемах без изменений" {
  write_conf '"user-unit|demo.timer|||Демо-таймер"' ""
  run "$WORK/check_mytasks.sh" --no-create --notify
  [ "$status" -eq 1 ]
  grep -q 'notify-send .*mytasks: требуют внимания' "$CALLS"
  [ ! -e "$USER_UNITS/demo.timer" ]
}

@test "условие задания не выполнено — «не требуется», не проблема" {
  write_conf '"link|demo-link|demo.sh|path:/nonexistent/x|Демо-ссылка"' ""
  run "$WORK/check_mytasks.sh" --status
  [ "$status" -eq 0 ]
  [[ "$output" == *"не требуется"* ]]
}

@test "LLAMA_PREFIX из конфига: условие path:@LLAMA_PREFIX@ включает задания llama.cpp" {
  write_conf '"link|demo-link|demo.sh|path:@LLAMA_PREFIX@|Демо-ссылка"' ""
  printf 'LLAMA_PREFIX="%s"\n' "$TMP_DIR/llama" >> "$WORK/conf/check_mytasks.conf"
  run "$WORK/check_mytasks.sh" --status
  [[ "$output" == *"не требуется"* ]]
  [ "$status" -eq 0 ]

  mkdir -p "$TMP_DIR/llama"
  run "$WORK/check_mytasks.sh" --status
  [ "$status" -eq 1 ]
  [[ "$output" == *"demo-link"*"ВНИМАНИЕ"* ]]
}

@test "LLAMA_PREFIX берётся из conf/llamactl.conf, если не задан в check_mytasks.conf" {
  write_conf '"link|demo-link|demo.sh|path:@LLAMA_PREFIX@|Демо-ссылка"' ""
  mkdir -p "$TMP_DIR/llama-from-llamactl"
  printf 'LLAMA_PREFIX="%s"\n' "$TMP_DIR/llama-from-llamactl" > "$WORK/conf/llamactl.conf"
  run "$WORK/check_mytasks.sh" --status
  [ "$status" -eq 1 ]
  [[ "$output" == *"demo-link"*"ВНИМАНИЕ"* ]]
}

@test "неизвестный аргумент — код 2" {
  write_conf "" ""
  run "$WORK/check_mytasks.sh" --bogus
  [ "$status" -eq 2 ]
}
