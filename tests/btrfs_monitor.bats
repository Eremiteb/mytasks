#!/usr/bin/env bats

# Тесты для btrfs_monitor.sh
# Внешние команды (btrfs, findmnt, notify-send) заменяются стабами.
# Скрипт копируется во временную папку, поэтому logs/ и state/
# создаются в TMP_DIR, а не в репозитории.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"
  STATS_FILE="$TMP_DIR/stats.txt"
  NOTIFY_LOG="$TMP_DIR/notify.log"
  STATE_FILE="$TMP_DIR/state/btrfs_monitor.state"

  mkdir -p "$STUB_DIR" "$TMP_DIR/conf" "$TMP_DIR/logs" "$TMP_DIR/state"

  cp "$REPO_ROOT/btrfs_monitor.sh" "$TMP_DIR/btrfs_monitor.sh"
  chmod +x "$TMP_DIR/btrfs_monitor.sh"

  # По умолчанию все счётчики нулевые (два раздела)
  cat > "$STATS_FILE" <<'EOF'
[/dev/sda2].write_io_errs    0
[/dev/sda2].read_io_errs     0
[/dev/sda2].flush_io_errs    0
[/dev/sda2].corruption_errs  0
[/dev/sda2].generation_errs  0
EOF

  # Стаб btrfs: `btrfs device stats <path>` печатает содержимое STATS_FILE
  cat > "$STUB_DIR/btrfs" <<EOF
#!/usr/bin/env bash
cat "$STATS_FILE"
EOF
  chmod +x "$STUB_DIR/btrfs"

  # Стаб findmnt: точка монтирования — Btrfs
  cat > "$STUB_DIR/findmnt" <<'EOF'
#!/usr/bin/env bash
echo "btrfs"
EOF
  chmod +x "$STUB_DIR/findmnt"

  # Стаб notify-send: фиксирует факт вызова
  cat > "$STUB_DIR/notify-send" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$NOTIFY_LOG"
exit 0
EOF
  chmod +x "$STUB_DIR/notify-send"
}

teardown() {
  rm -rf "$TMP_DIR"
}

run_monitor() {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/btrfs_monitor.sh"
}

last_log() {
  ls -1t "$TMP_DIR/logs"/btrfs_monitor-*.jsonl 2>/dev/null | head -1
}

# ---------------------------------------------------------------------------
# Проверка зависимостей и типа ФС
# ---------------------------------------------------------------------------

@test "завершается с ошибкой если btrfs не установлен" {
  # Изолированный PATH без стаба btrfs (системный btrfs может присутствовать)
  local iso="$TMP_DIR/iso_no_btrfs"
  mkdir -p "$iso"
  cp "$STUB_DIR/findmnt" "$iso/findmnt"
  cp "$STUB_DIR/notify-send" "$iso/notify-send"
  for _cmd in bash env date mkdir ls tail rm sed grep tr awk mktemp basename dirname cat printf cp; do
    _bin="$(command -v "$_cmd" 2>/dev/null)"
    [ -n "$_bin" ] && ln -sf "$_bin" "$iso/$_cmd" || true
  done

  run env PATH="$iso" bash "$TMP_DIR/btrfs_monitor.sh"

  [ "$status" -eq 2 ]
  run grep -q '"event":"dependency_missing"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "завершается с ошибкой если точка не на Btrfs" {
  cat > "$STUB_DIR/findmnt" <<'EOF'
#!/usr/bin/env bash
echo "ext4"
EOF
  chmod +x "$STUB_DIR/findmnt"

  run_monitor

  [ "$status" -eq 2 ]
  [[ "$output" == *"не является Btrfs"* ]]
  run grep -q '"event":"not_btrfs"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "завершается с ошибкой если btrfs device stats пуст" {
  : > "$STATS_FILE"

  run_monitor

  [ "$status" -eq 2 ]
  run grep -q '"event":"stats_read_failed"' "$(last_log)"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Инициализация состояния
# ---------------------------------------------------------------------------

@test "первый запуск создаёт базовое состояние и завершается с 0" {
  [ ! -f "$STATE_FILE" ]

  run_monitor

  [ "$status" -eq 0 ]
  [ -f "$STATE_FILE" ]
  [[ "$output" == *"Инициализация"* ]]
  run grep -q '"event":"state_initialized"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "сохранённое состояние соответствует выводу stats" {
  run_monitor
  [ "$status" -eq 0 ]

  run grep -P '^/dev/sda2\twrite_io_errs\t0$' "$STATE_FILE"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Сравнение счётчиков
# ---------------------------------------------------------------------------

@test "нет изменений счётчиков → OK и выход 0" {
  # Предзаполняем состояние теми же значениями, что и в stats
  printf '/dev/sda2\twrite_io_errs\t0\n' > "$STATE_FILE"

  run_monitor

  [ "$status" -eq 0 ]
  [[ "$output" == *"OK"* ]]
  run grep -q '"event":"done"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "рост счётчиков → ALERT и выход 1" {
  printf '/dev/sda2\twrite_io_errs\t0\n' > "$STATE_FILE"
  cat > "$STATS_FILE" <<'EOF'
[/dev/sda2].write_io_errs    5
EOF

  run_monitor

  [ "$status" -eq 1 ]
  [[ "$output" == *"ALERT"* ]]
  [[ "$output" == *"write_io_errs"* ]]
  run grep -q '"event":"errors_growth"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "при росте счётчиков вызывается notify-send" {
  printf '/dev/sda2\twrite_io_errs\t0\n' > "$STATE_FILE"
  cat > "$STATS_FILE" <<'EOF'
[/dev/sda2].write_io_errs    3
EOF

  run_monitor

  [ "$status" -eq 1 ]
  [ -f "$NOTIFY_LOG" ]
  run grep -q "рост счетчиков" "$NOTIFY_LOG"
  [ "$status" -eq 0 ]
}

@test "уменьшение счётчиков (reset) → выход 0 и counter_reset" {
  printf '/dev/sda2\twrite_io_errs\t9\n' > "$STATE_FILE"
  cat > "$STATS_FILE" <<'EOF'
[/dev/sda2].write_io_errs    0
EOF

  run_monitor

  [ "$status" -eq 0 ]
  run grep -q '"event":"counter_reset"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "reset не вызывает notify-send" {
  printf '/dev/sda2\twrite_io_errs\t9\n' > "$STATE_FILE"
  cat > "$STATS_FILE" <<'EOF'
[/dev/sda2].write_io_errs    0
EOF

  run_monitor

  [ "$status" -eq 0 ]
  [ ! -f "$NOTIFY_LOG" ]
}

@test "состояние обновляется после обнаружения роста" {
  printf '/dev/sda2\twrite_io_errs\t0\n' > "$STATE_FILE"
  cat > "$STATS_FILE" <<'EOF'
[/dev/sda2].write_io_errs    7
EOF

  run_monitor
  [ "$status" -eq 1 ]

  # Повторный запуск с тем же stats: роста больше нет
  run_monitor
  [ "$status" -eq 0 ]
  [[ "$output" == *"OK"* ]]
}

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

@test "лог-файл создаётся в папке logs" {
  run_monitor

  log_file="$(last_log)"
  [ -n "$log_file" ]
  [ -f "$log_file" ]
}

@test "каждая строка лога содержит обязательные поля" {
  run_monitor

  log_file="$(last_log)"
  [ -n "$log_file" ]
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    [[ "$line" == *'"@timestamp"'* ]]
    [[ "$line" == *'"level"'* ]]
    [[ "$line" == *'"event"'* ]]
  done < "$log_file"
}

@test "лог содержит событие start" {
  run_monitor

  run grep -q '"event":"start"' "$(last_log)"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Ротация логов
# ---------------------------------------------------------------------------

@test "старые логи удаляются согласно KEEP_LOGS" {
  cat > "$TMP_DIR/conf/btrfs_monitor.conf" <<'EOF'
KEEP_LOGS=2
EOF

  # Создаём 5 старых лог-файлов
  for i in 1 2 3 4 5; do
    touch "$TMP_DIR/logs/btrfs_monitor-2020-01-0${i}-00-00-00.jsonl"
  done

  run_monitor
  [ "$status" -eq 0 ]

  # После запуска остаётся не более KEEP_LOGS файлов
  count=$(ls -1 "$TMP_DIR/logs"/btrfs_monitor-*.jsonl 2>/dev/null | wc -l)
  [ "$count" -le 2 ]
}
