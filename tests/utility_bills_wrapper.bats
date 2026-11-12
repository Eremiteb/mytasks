#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  export MYTASKS_LOG_DIR="$TMP_DIR/logs"

  cp "$REPO_ROOT/utility_bills.sh" "$TMP_DIR/utility_bills.sh"
  chmod +x "$TMP_DIR/utility_bills.sh"
  mkdir -p "$TMP_DIR/utility_bills"

  # Подменный движок: записывает аргументы и печатает строку плана.
  cat > "$TMP_DIR/utility_bills/utility_bills.py" <<'EOF2'
import pathlib, sys
pathlib.Path(__file__).with_name("args").write_text(" ".join(sys.argv[1:]))
print("[dry-run] будет: план" if "--dry-run" in sys.argv else "обычный запуск")
sys.exit(int(pathlib.Path(__file__).with_name("rc").read_text()) if pathlib.Path(__file__).with_name("rc").exists() else 0)
EOF2
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "dry-run передаёт аргументы движку, печатает план и не создаёт БД" {
  # Короткий ключ -n обёртка передаёт движку как --dry-run.
  run bash "$TMP_DIR/utility_bills.sh" -n --file /nonexistent.pdf

  [ "$status" -eq 0 ]
  [ "$(cat "$TMP_DIR/utility_bills/args")" = "--dry-run --file /nonexistent.pdf" ]
  [[ "$output" == *"[dry-run] будет: план"* ]]
  [ ! -e "$TMP_DIR/logs/cron_execution.log" ]
  [ ! -e "$TMP_DIR/utility_bills/utility_bills.db" ]
}

@test "обычный запуск пишет вывод в CRON_LOG и возвращает код движка" {
  echo 1 > "$TMP_DIR/utility_bills/rc"

  run bash "$TMP_DIR/utility_bills.sh"

  [ "$status" -eq 1 ]
  grep -q "обычный запуск" "$TMP_DIR/logs/cron_execution.log"
  grep -q '"event":"engine_failed"' "$TMP_DIR"/logs/utility_bills-*.jsonl
}

@test "PYTHON_BIN и CRON_LOG берутся из conf/utility_bills.conf" {
  mkdir -p "$TMP_DIR/conf" "$TMP_DIR/bin"
  cat > "$TMP_DIR/bin/mypython" <<'EOF2'
#!/usr/bin/env bash
echo "mypython called $*"
EOF2
  chmod +x "$TMP_DIR/bin/mypython"
  printf 'PYTHON_BIN=%s\nCRON_LOG=%s\n' "$TMP_DIR/bin/mypython" "$TMP_DIR/my_cron.log" > "$TMP_DIR/conf/utility_bills.conf"

  run bash "$TMP_DIR/utility_bills.sh"

  [ "$status" -eq 0 ]
  grep -q "mypython called .*utility_bills.py" "$TMP_DIR/my_cron.log"
}

@test "используется python из venv, если он есть" {
  mkdir -p "$TMP_DIR/utility_bills/venv/bin"
  cat > "$TMP_DIR/utility_bills/venv/bin/python" <<'EOF2'
#!/usr/bin/env bash
echo "venv python"
EOF2
  chmod +x "$TMP_DIR/utility_bills/venv/bin/python"

  run bash "$TMP_DIR/utility_bills.sh" --dry-run

  [ "$status" -eq 0 ]
  [[ "$output" == *"venv python"* ]]
}

@test "нет проекта — код 1" {
  rm -rf "$TMP_DIR/utility_bills"

  run bash "$TMP_DIR/utility_bills.sh"

  [ "$status" -eq 1 ]
  [[ "$output" == *"Проект не найден"* ]]
}

@test "--help печатает справку и не запускает движок" {
  run bash "$TMP_DIR/utility_bills.sh" --help

  [ "$status" -eq 0 ]
  [[ "$output" == *"--dry-run"* ]]
  [ ! -e "$TMP_DIR/utility_bills/args" ]
}

@test "параллельный запуск при занятой блокировке — код 3, already_running, движок не вызван" {
  exec {lock_fd}>"$TMP_DIR/utility_bills/.utility_bills.lock"
  flock -n "$lock_fd"

  run bash "$TMP_DIR/utility_bills.sh"
  exec {lock_fd}>&-

  [ "$status" -eq 3 ]
  [[ "$output" == *"уже выполняется"* ]]
  grep -q '"event":"already_running"' "$TMP_DIR"/logs/utility_bills-*.jsonl
  [ ! -e "$TMP_DIR/utility_bills/args" ]
}

@test "лог JSONL создаётся с правами 600" {
  run bash "$TMP_DIR/utility_bills.sh"

  [ "$status" -eq 0 ]
  local log_file
  for log_file in "$TMP_DIR"/logs/utility_bills-*.jsonl; do
    [ "$(stat -c '%a' "$log_file")" = "600" ]
  done
}
