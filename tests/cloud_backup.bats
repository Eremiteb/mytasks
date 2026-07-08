#!/usr/bin/env bats

# Тесты для cloud_backup.sh
# Все внешние команды (wg, wg-quick, ping, sshpass) заменяются стабами

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"
  BACKUP_DIR="$TMP_DIR/backups"

  mkdir -p "$STUB_DIR" "$TMP_DIR/conf" "$TMP_DIR/logs" "$BACKUP_DIR"

  cp "$REPO_ROOT/cloud_backup.sh" "$TMP_DIR/cloud_backup.sh"
  chmod +x "$TMP_DIR/cloud_backup.sh"

  # Базовый корректный конфиг
  cat > "$TMP_DIR/conf/cloud_backup.conf" <<EOF
WG_INTERFACE="wg-test"
REMOTE_HOST="10.0.0.1"
REMOTE_USER="testuser"
REMOTE_PASSWORD="testpass"
REMOTE_SSH_PORT="22"
REMOTE_PATH="/opt/esimych-cloud"
BACKUP_DIR="$BACKUP_DIR"
WG_KEEP_UP="1"
EOF

  # Стаб wg: по умолчанию интерфейс уже активен
  cat > "$STUB_DIR/wg" <<'EOF'
#!/usr/bin/env bash
# wg show <iface> -> выход 0 (интерфейс активен)
exit 0
EOF
  chmod +x "$STUB_DIR/wg"

  # Стаб wg-quick: успешный up/down
  cat > "$STUB_DIR/wg-quick" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/wg-quick"

  # Стаб ping: успешный
  cat > "$STUB_DIR/ping" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/ping"

  # Стаб sshpass: запускает остаток аргументов, передавая ssh-команду
  # Перехватывает "tar" и создаёт минимальный tar.gz
  cat > "$STUB_DIR/sshpass" <<EOF
#!/usr/bin/env bash
# Игнорируем флаг -e и запускаем команду
shift  # убираем -e
exec "\$@"
EOF
  chmod +x "$STUB_DIR/sshpass"

  # Стаб ssh: притворяется, что tar вернул корректный gz-архив
  cat > "$STUB_DIR/ssh" <<'EOF'
#!/usr/bin/env bash
# Создаём минимальный gzip-файл (валидный пустой tar.gz)
printf '\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00'
exit 0
EOF
  chmod +x "$STUB_DIR/ssh"
}

teardown() {
  rm -rf "$TMP_DIR"
}

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@test "завершается с ошибкой если конфиг не найден" {
  rm "$TMP_DIR/conf/cloud_backup.conf"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  [[ "$output" == *"конфигурационный файл не найден"* ]]
}

@test "завершается с ошибкой если WG_INTERFACE не задан" {
  sed -i '/^WG_INTERFACE/d' "$TMP_DIR/conf/cloud_backup.conf"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  [[ "$output" == *"WG_INTERFACE"* ]]
}

@test "завершается с ошибкой если REMOTE_HOST не задан" {
  sed -i '/^REMOTE_HOST/d' "$TMP_DIR/conf/cloud_backup.conf"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  [[ "$output" == *"REMOTE_HOST"* ]]
}

@test "завершается с ошибкой если REMOTE_PASSWORD не задан" {
  sed -i '/^REMOTE_PASSWORD/d' "$TMP_DIR/conf/cloud_backup.conf"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  [[ "$output" == *"REMOTE_PASSWORD"* ]]
}

@test "завершается с ошибкой если BACKUP_DIR не задан" {
  sed -i '/^BACKUP_DIR/d' "$TMP_DIR/conf/cloud_backup.conf"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKUP_DIR"* ]]
}

# ---------------------------------------------------------------------------
# WireGuard
# ---------------------------------------------------------------------------

@test "использует существующий WG-интерфейс без wg-quick up" {
  # wg show возвращает 0 (уже активен), wg-quick должен НЕ вызываться
  cat > "$STUB_DIR/wg-quick" <<'EOF'
#!/usr/bin/env bash
echo "wg-quick был вызван: $*" >&2
exit 1
EOF
  chmod +x "$STUB_DIR/wg-quick"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  # Раз wg-quick вернул бы ошибку — успех значит, что он не вызывался
  [ "$status" -eq 0 ]

  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"wg_status"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "поднимает WG если интерфейс не активен" {
  # wg show возвращает ошибку (интерфейс не активен)
  cat > "$STUB_DIR/wg" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$STUB_DIR/wg"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -eq 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"wg_up_ok"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "завершается с ошибкой если wg-quick up провалился" {
  cat > "$STUB_DIR/wg" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$STUB_DIR/wg"

  cat > "$STUB_DIR/wg-quick" <<'EOF'
#!/usr/bin/env bash
echo "RTNETLINK answers: Operation not permitted" >&2
exit 1
EOF
  chmod +x "$STUB_DIR/wg-quick"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"wg_up_failed"' "$log_file"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Проверка VPN-соединения (ping)
# ---------------------------------------------------------------------------

@test "завершается с ошибкой если хост недоступен через VPN" {
  cat > "$STUB_DIR/ping" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$STUB_DIR/ping"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"wg_no_connection"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "логирует retry-предупреждения при недоступности хоста" {
  cat > "$STUB_DIR/ping" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
  chmod +x "$STUB_DIR/ping"

  # Убираем sleep чтобы тест не ждал 10 секунд
  cat > "$STUB_DIR/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/sleep"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  retry_count=$(grep -c '"event":"wg_ping_retry"' "$log_file" || true)
  [ "$retry_count" -ge 2 ]
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

@test "завершается с ошибкой если sshpass не установлен" {
  # Строим изолированный PATH: только нужные системные утилиты + стабы, без sshpass.
  # Нельзя просто удалить стаб — системный sshpass может быть установлен или
  # стать видимым при параллельном запуске всего набора тестов.
  local iso="$TMP_DIR/iso_no_sshpass"
  mkdir -p "$iso"

  # Копируем стабы (wg, wg-quick, ping), sshpass намеренно пропускаем
  for _s in wg wg-quick ping; do cp "$STUB_DIR/$_s" "$iso/$_s"; done

  # Симлинки на системные утилиты, нужные скрипту до/после sshpass-проверки.
  # bash и env нужны, чтобы стабы со своим шебангом #!/usr/bin/env bash запустились.
  for _cmd in bash env date mkdir ls tail rm sed grep sleep basename dirname; do
    _bin="$(command -v "$_cmd" 2>/dev/null)"
    [ -n "$_bin" ] && ln -sf "$_bin" "$iso/$_cmd" || true
  done

  run env PATH="$iso" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  log_file=$(ls -1t "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"sshpass_missing"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "завершается с ошибкой если BACKUP_DIR недоступна" {
  sed -i "s|BACKUP_DIR=.*|BACKUP_DIR=\"/nonexistent/path/$(date +%s)\"|" \
    "$TMP_DIR/conf/cloud_backup.conf"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"backup_dir_missing"' "$log_file"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Основной сценарий
# ---------------------------------------------------------------------------

@test "успешное резервное копирование создаёт архив" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -eq 0 ]
  # Файл архива должен существовать
  backup_file=$(ls "$BACKUP_DIR"/cloud_backup-*.tar.gz 2>/dev/null | head -1)
  [ -n "$backup_file" ]
  [ -f "$backup_file" ]
}

@test "успешное копирование логирует backup_done" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -eq 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"backup_done"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "имя архива содержит дату формата YYYY-MM-DD" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -eq 0 ]
  backup_file=$(ls "$BACKUP_DIR"/cloud_backup-*.tar.gz 2>/dev/null | head -1)
  [[ "$backup_file" =~ cloud_backup-[0-9]{4}-[0-9]{2}-[0-9]{2}\.tar\.gz ]]
}

@test "дозаписывает в существующий валидный архив за текущую дату" {
  backup_file="$BACKUP_DIR/cloud_backup-$(date '+%Y-%m-%d').tar.gz"
  printf '\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00' > "$backup_file"
  size_before=$(wc -c < "$backup_file")

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -eq 0 ]
  size_after=$(wc -c < "$backup_file")
  [ "$size_after" -gt "$size_before" ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"backup_append_mode"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "удаляет битый архив за текущую дату и создаёт новый" {
  backup_file="$BACKUP_DIR/cloud_backup-$(date '+%Y-%m-%d').tar.gz"
  printf 'broken-backup' > "$backup_file"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -eq 0 ]
  run gzip -t "$backup_file"
  [ "$status" -eq 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"backup_existing_invalid"' "$log_file"
  [ "$status" -eq 0 ]
}

@test "при ошибке SSH архив не остаётся на диске" {
  cat > "$STUB_DIR/ssh" <<'EOF'
#!/usr/bin/env bash
echo "Connection refused" >&2
exit 255
EOF
  chmod +x "$STUB_DIR/ssh"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  leftover=$(ls "$BACKUP_DIR"/cloud_backup-*.tar.gz 2>/dev/null || true)
  [ -z "$leftover" ]
}

@test "при ошибке SSH логирует backup_failed" {
  cat > "$STUB_DIR/ssh" <<'EOF'
#!/usr/bin/env bash
echo "Connection refused" >&2
exit 255
EOF
  chmod +x "$STUB_DIR/ssh"

  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  [ "$status" -ne 0 ]
  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"backup_failed"' "$log_file"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

@test "лог-файл создаётся в папке logs" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  [ -n "$log_file" ]
  [ -f "$log_file" ]
}

@test "каждая строка лога является валидным JSON" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  [ -n "$log_file" ]

  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # Проверяем наличие обязательных полей JSON
    [[ "$line" == *'"ts"'* ]]
    [[ "$line" == *'"level"'* ]]
    [[ "$line" == *'"event"'* ]]
  done < "$log_file"
}

@test "лог содержит событие start" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/cloud_backup.sh"

  log_file=$(ls "$TMP_DIR/logs"/cloud_backup-*.jsonl 2>/dev/null | head -1)
  run grep -q '"event":"start"' "$log_file"
  [ "$status" -eq 0 ]
}
