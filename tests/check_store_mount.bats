#!/usr/bin/env bats

setup() {
  # Логи скриптов — во временный каталог теста, а не в logs/ репозитория
  export MYTASKS_LOG_DIR="$BATS_TEST_TMPDIR/logs"
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$STUB_DIR"

  cat > "$STUB_DIR/mountpoint" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/notify-send" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  chmod +x "$STUB_DIR/mountpoint" "$STUB_DIR/notify-send"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "reports all mounts are present when mountpoint returns success" {
  run env PATH="$STUB_DIR:$PATH" bash "$REPO_ROOT/check-store-mount.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Все диски на месте."* ]]
}

@test "dry-run runs the check and reports success" {
  run env PATH="$STUB_DIR:$PATH" bash "$REPO_ROOT/check-store-mount.sh" --dry-run

  [ "$status" -eq 0 ]
  [[ "$output" == *"Все диски на месте."* ]]
}

@test "rejects unknown argument" {
  run env PATH="$STUB_DIR:$PATH" bash "$REPO_ROOT/check-store-mount.sh" --bogus

  [ "$status" -eq 2 ]
}

@test "MOUNT_IGNORE_REGEX: исключённые точки не проверяются, даже если mountpoint их не видит" {
  printf '#!/usr/bin/env bash\nexit 1\n' > "$STUB_DIR/mountpoint"
  chmod +x "$STUB_DIR/mountpoint"

  run env PATH="$STUB_DIR:$PATH" MOUNT_WAIT_SECS=0 MOUNT_IGNORE_REGEX='.*' bash "$REPO_ROOT/check-store-mount.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Все диски на месте."* ]]
}

@test "MOUNT_WAIT_SECS=0: несмонтированная точка обнаруживается сразу, без ожидания" {
  printf '#!/usr/bin/env bash\nexit 1\n' > "$STUB_DIR/mountpoint"
  chmod +x "$STUB_DIR/mountpoint"

  run env PATH="$STUB_DIR:$PATH" MOUNT_WAIT_SECS=0 MOUNT_IGNORE_REGEX='^/(proc|sys|dev|run|tmp)' bash "$REPO_ROOT/check-store-mount.sh"

  # В /etc/fstab этой машины есть точки вне исключений; при отсутствии таких записей тест неинформативен
  if grep -qE '^[^#[:space:]]+[[:space:]]+/' /etc/fstab; then
    [ "$status" -eq 1 ]
    [[ "$output" == *"Ошибка: не смонтированы"* ]]
  fi
}

@test "KEEP_LOGS: лишние JSONL-логи удаляются по заданному числу" {
  mkdir -p "$MYTASKS_LOG_DIR"
  for i in 1 2 3 4 5 6; do
    : > "$MYTASKS_LOG_DIR/check-store-mount-2020-01-0${i}-00-00-00.jsonl"
    touch -d "2020-01-0${i} 00:00:00" "$MYTASKS_LOG_DIR/check-store-mount-2020-01-0${i}-00-00-00.jsonl"
  done

  run env PATH="$STUB_DIR:$PATH" KEEP_LOGS=2 bash "$REPO_ROOT/check-store-mount.sh"

  [ "$status" -eq 0 ]
  run bash -c "ls \"$MYTASKS_LOG_DIR\"/check-store-mount-*.jsonl | wc -l"
  [ "$output" -eq 2 ]
}
