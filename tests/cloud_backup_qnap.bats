#!/usr/bin/env bats

# Тесты для новой логики RAW_TRANSFER_* в cloud_backup_qnap.sh (передача
# потока бэкапа через nc/ncat в обход SSH-шифрования). Всё, что происходит
# ДО этого блока (подъём WireGuard, occ cleanup, optimize БД), обходится
# стабами по минимуму, необходимому чтобы дойти до передачи:
#   - WG_SOCK_OVERRIDE указывает wg_is_up() на заранее созданный AF_UNIX
#     сокет в $TMP_DIR (реальный /var/run/wireguard недоступен без root),
#     а стаб ip печатает "state UP" — весь путь wg_up_userspace (wireguard-go,
#     UAPI-обмен через ncat -U) целиком пропускается.
#   - REMOTE_PASSWORD пуст (как рекомендовано для QNAP) — ssh_remote() зовёт
#     обычный ssh напрямую, без sshpass.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"
  BACKUP_DIR="$TMP_DIR/backups"
  WG_SOCK_PATH="$TMP_DIR/wg0-qnap.sock"

  mkdir -p "$STUB_DIR" "$TMP_DIR/conf" "$TMP_DIR/logs" "$BACKUP_DIR"

  cp "$REPO_ROOT/cloud_backup_qnap.sh" "$TMP_DIR/cloud_backup_qnap.sh"
  chmod +x "$TMP_DIR/cloud_backup_qnap.sh"

  python3 -c "import socket; s=socket.socket(socket.AF_UNIX); s.bind('$WG_SOCK_PATH')"

  cat > "$TMP_DIR/conf/cloud_backup_qnap.conf" <<EOF
WG_INTERFACE="wg-test"
REMOTE_HOST="10.0.0.1"
REMOTE_USER="testuser"
REMOTE_PASSWORD=""
REMOTE_SSH_KEY=""
REMOTE_PATH="/opt/esimych-cloud"
BACKUP_DIR="$BACKUP_DIR"
WG_KEEP_UP="1"
EOF

  cat > "$STUB_DIR/ip" <<'EOF'
#!/usr/bin/env bash
echo "state UP"
exit 0
EOF
  chmod +x "$STUB_DIR/ip"

  cat > "$STUB_DIR/ping" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/ping"

  # Не используются на этом пути (wg_is_up закорачивает wg_up_userspace),
  # но preflight-проверка зависимостей требует, чтобы команды существовали.
  cat > "$STUB_DIR/wireguard-go" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/wireguard-go"

  cat > "$STUB_DIR/xxd" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/xxd"

  # ncat проверяется как обязательная зависимость даже при выключенном
  # RAW_TRANSFER_ENABLED. По умолчанию имитируем недоступный сетевой замер:
  # это best-effort путь и он не должен зависеть от установленного в CI ncat.
  # Тесты raw-transfer ниже заменяют этот стаб своей реализацией.
  cat > "$STUB_DIR/ncat" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$STUB_DIR/ncat"

  export TMP_DIR STUB_DIR BACKUP_DIR WG_SOCK_PATH
}

teardown() {
  rm -rf "$TMP_DIR"
}

# Минимальный валидный (пустой) gzip-фиксчер — как в tests/cloud_backup.bats
GZIP_FIXTURE='\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00'

run_script() {
  run env PATH="$STUB_DIR:$PATH" WG_SOCK_OVERRIDE="$WG_SOCK_PATH" \
    bash "$TMP_DIR/cloud_backup_qnap.sh"
}

# ---------------------------------------------------------------------------

@test "RAW_TRANSFER_ENABLED=0 — поведение как раньше, ncat не вызывается" {
  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"tar --create"*) printf '$GZIP_FIXTURE'; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  cat > "$STUB_DIR/ncat" <<EOF
#!/usr/bin/env bash
echo "ncat was called: \$*" > "$TMP_DIR/ncat_called_marker"
exit 1
EOF
  chmod +x "$STUB_DIR/ncat"

  run_script

  [ "$status" -eq 0 ]
  [ ! -f "$TMP_DIR/ncat_called_marker" ]
  backup_file=$(ls "$BACKUP_DIR"/cloud_backup_qnap-*.tar.gz 2>/dev/null | head -1)
  [ -n "$backup_file" ]
  run gzip -t "$backup_file"
  [ "$status" -eq 0 ]
}

@test "raw transfer: несколько неудачных подключений, потом успех" {
  cat >> "$TMP_DIR/conf/cloud_backup_qnap.conf" <<'EOF'
RAW_TRANSFER_ENABLED="1"
EOF

  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"command -v nc >/dev/null 2>&1 && echo yes"*) echo yes; exit 0 ;;
  *"nohup bash -c"*) exit 0 ;;
  *"rm -f '"*) exit 0 ;;
  *"cat '"*"raw-status-"*) echo 0; exit 0 ;;
  *"cat '"*"raw-err-"*) exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  cat > "$STUB_DIR/ncat" <<EOF
#!/usr/bin/env bash
counter_file="$TMP_DIR/ncat_attempts"
n=0
[ -f "\$counter_file" ] && n=\$(cat "\$counter_file")
n=\$((n + 1))
echo "\$n" > "\$counter_file"
if [ "\$n" -lt 3 ]; then
  echo "ncat: Connection refused." >&2
  exit 1
fi
printf '$GZIP_FIXTURE'
exit 0
EOF
  chmod +x "$STUB_DIR/ncat"

  run_script

  [ "$status" -eq 0 ]
  [ "$(cat "$TMP_DIR/ncat_attempts")" -eq 3 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup_qnap-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"raw_transfer_connect_retry"' "$log_file"
  [ "$status" -eq 0 ]
  run grep -q '"event":"raw_transfer_status_fetch"' "$log_file"
  [ "$status" -eq 0 ]
  backup_file=$(ls "$BACKUP_DIR"/cloud_backup_qnap-*.tar.gz 2>/dev/null | head -1)
  [ -n "$backup_file" ]
  run gzip -t "$backup_file"
  [ "$status" -eq 0 ]
}

@test "raw transfer: чистый локальный EOF, но удалённый статус — ошибка" {
  cat >> "$TMP_DIR/conf/cloud_backup_qnap.conf" <<'EOF'
RAW_TRANSFER_ENABLED="1"
EOF

  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"command -v nc >/dev/null 2>&1 && echo yes"*) echo yes; exit 0 ;;
  *"nohup bash -c"*) exit 0 ;;
  *"rm -f '"*) exit 0 ;;
  *"cat '"*"raw-status-"*) echo 1; exit 0 ;;
  *"cat '"*"raw-err-"*) echo "remote tar failed: disk full"; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  cat > "$STUB_DIR/ncat" <<EOF
#!/usr/bin/env bash
printf '$GZIP_FIXTURE'
exit 0
EOF
  chmod +x "$STUB_DIR/ncat"

  run_script

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup_qnap-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"backup_failed"' "$log_file"
  [ "$status" -eq 0 ]
  leftover=$(ls "$BACKUP_DIR"/cloud_backup_qnap-*.tar.gz 2>/dev/null || true)
  [ -z "$leftover" ]
}

@test "raw transfer: nc отсутствует на удалённой стороне — фолбэк на SSH" {
  cat >> "$TMP_DIR/conf/cloud_backup_qnap.conf" <<'EOF'
RAW_TRANSFER_ENABLED="1"
EOF

  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"command -v nc >/dev/null 2>&1 && echo yes"*) exit 1 ;;
  *"tar --create"*) printf '$GZIP_FIXTURE'; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  cat > "$STUB_DIR/ncat" <<EOF
#!/usr/bin/env bash
echo "ncat was called: \$*" > "$TMP_DIR/ncat_called_marker"
exit 1
EOF
  chmod +x "$STUB_DIR/ncat"

  run_script

  [ "$status" -eq 0 ]
  [ ! -f "$TMP_DIR/ncat_called_marker" ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup_qnap-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"raw_transfer_nc_missing"' "$log_file"
  [ "$status" -eq 0 ]
  backup_file=$(ls "$BACKUP_DIR"/cloud_backup_qnap-*.tar.gz 2>/dev/null | head -1)
  [ -n "$backup_file" ]
  run gzip -t "$backup_file"
  [ "$status" -eq 0 ]
}

@test "JSONL сохраняет дефисы в путях и именах контейнеров" {
  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"tar --create"*) printf '$GZIP_FIXTURE'; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  run_script

  [ "$status" -eq 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup_qnap-*.jsonl 2>/dev/null | head -1)
  run grep -q '/opt/esimych-cloud' "$log_file"
  [ "$status" -eq 0 ]
}

@test "OCC использует настраиваемое имя сервиса docker compose" {
  cat >> "$TMP_DIR/conf/cloud_backup_qnap.conf" <<'EOF'
NEXTCLOUD_SERVICE_NAME="cloud-app"
EOF

  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$TMP_DIR/ssh_commands"
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"tar --create"*) printf '$GZIP_FIXTURE'; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  run_script

  [ "$status" -eq 0 ]
  run grep -q "docker compose exec -T -u www-data 'cloud-app' php occ trashbin:cleanup" "$TMP_DIR/ssh_commands"
  [ "$status" -eq 0 ]
}

@test "запуск сервисов повторяется и завершается успехом на третьей попытке" {
  cat >> "$TMP_DIR/conf/cloud_backup_qnap.conf" <<'EOF'
SERVICES_START_RETRIES="3"
SERVICES_START_RETRY_DELAY_SEC="0"
EOF

  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"tar --create"*) printf '$GZIP_FIXTURE'; exit 0 ;;
  *"docker compose up -d"*)
    count=0
    [ -f "$TMP_DIR/start_attempts" ] && count=\$(cat "$TMP_DIR/start_attempts")
    count=\$((count + 1))
    printf '%s\n' "\$count" > "$TMP_DIR/start_attempts"
    [ "\$count" -ge 3 ]
    ;;
  *"docker compose ps -a"*) echo 'nextcloud running healthy'; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  run_script

  [ "$status" -eq 0 ]
  [ "$(cat "$TMP_DIR/start_attempts")" -eq 3 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup_qnap-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"services_start_retry"' "$log_file"
  [ "$status" -eq 0 ]
  run grep -q '"event":"services_start_ok"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "окончательная ошибка запуска сервисов делает задание неуспешным" {
  cat >> "$TMP_DIR/conf/cloud_backup_qnap.conf" <<'EOF'
SERVICES_START_RETRIES="2"
SERVICES_START_RETRY_DELAY_SEC="0"
EOF

  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"command -v zstd >/dev/null 2>&1"*) echo gzip; exit 0 ;;
  *"tar --create"*) printf '$GZIP_FIXTURE'; exit 0 ;;
  *"docker compose up -d"*) exit 1 ;;
  *"docker compose ps -a"*) echo 'valkey exited unhealthy'; exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$STUB_DIR/ssh"

  run_script

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup_qnap-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"services_start_failed".*"level":"error"' "$log_file"
  [ "$status" -eq 0 ]
}
