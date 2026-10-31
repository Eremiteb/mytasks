#!/usr/bin/env bats

# Тесты для check_backups.sh
# Скрипт копируется во временную папку, поэтому conf/ и logs/ создаются в
# TMP_DIR, а не в репозитории. notify-send заменяется стабом.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"
  BACKUP_DIR="$TMP_DIR/backups"
  NOTIFY_LOG="$TMP_DIR/notify.log"

  mkdir -p "$STUB_DIR" "$TMP_DIR/conf" "$BACKUP_DIR"
  cp "$REPO_ROOT/check_backups.sh" "$TMP_DIR/check_backups.sh"
  chmod +x "$TMP_DIR/check_backups.sh"

  cat > "$STUB_DIR/notify-send" <<EOT
#!/usr/bin/env bash
echo "\$*" >> "$NOTIFY_LOG"
exit 0
EOT
  chmod +x "$STUB_DIR/notify-send"
}

teardown() {
  rm -rf "$TMP_DIR"
}

write_conf() {
  cat > "$TMP_DIR/conf/check_backups.conf" <<EOT
BACKUP_DIRS=("$BACKUP_DIR${1:-}")
VERIFY_INTEGRITY="no"
WRITING_CHECK_SECS=0
${2:-}
EOT
}

grow_on_sleep() {
  # Стаб sleep дописывает файл — так в тесте имитируется идущая запись архива
  cat > "$STUB_DIR/sleep" <<EOT
#!/usr/bin/env bash
printf 'x' >> "$1"
EOT
  chmod +x "$STUB_DIR/sleep"
}

make_archive() {
  # make_archive имя размер_в_байтах [возраст_в_часах]
  head -c "$2" /dev/zero > "$BACKUP_DIR/$1"
  touch -d "${3:-1} hours ago" "$BACKUP_DIR/$1"
}

run_check() {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/check_backups.sh" "$@"
}

last_log() {
  cat "$TMP_DIR"/logs/check_backups-*.jsonl
}

@test "свежий архив: код 0, уведомления нет, лог записан" {
  write_conf
  make_archive "b-1.tar.zst" 1000 1
  run_check

  [ "$status" -eq 0 ]
  [[ "$output" == *"Бэкапы в порядке"* ]]
  [ ! -e "$NOTIFY_LOG" ]
  [[ "$(last_log)" == *'"event":"dir_ok"'* ]]
  [[ "$(last_log)" == *'"event":"done"'* ]]
}

@test "устаревший архив: код 1, ERROR в логе и уведомление" {
  write_conf
  make_archive "b-1.tar.zst" 1000 100
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"archive_stale"'* ]]
  [ -s "$NOTIFY_LOG" ]
  [[ "$(cat "$NOTIFY_LOG")" == *"устарел"* ]]
}

@test "индивидуальный лимит возраста каталога переопределяет общий" {
  write_conf "|10"
  make_archive "b-1.tar.zst" 1000 100
  run_check

  [ "$status" -eq 0 ]
}

@test "в каталоге нет архивов: код 1 и уведомление" {
  write_conf
  : > "$BACKUP_DIR/readme.txt"
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"no_archives"'* ]]
  [ -s "$NOTIFY_LOG" ]
}

@test "несуществующий каталог: код 1 и уведомление" {
  write_conf
  rm -rf "$BACKUP_DIR"
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"dir_missing"'* ]]
  [ -s "$NOTIFY_LOG" ]
}

@test "пустой последний архив считается ошибкой" {
  write_conf
  make_archive "b-1.tar.zst" 0 1
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"archive_too_small"'* ]]
}

@test "резкое уменьшение относительно предыдущего архива" {
  write_conf
  make_archive "b-1.tar.zst" 10000 30
  make_archive "b-2.tar.zst" 1000 1
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"archive_shrunk"'* ]]
}

@test "проверка целостности: битый gzip даёт ошибку" {
  write_conf "" 'VERIFY_INTEGRITY="yes"'
  head -c 500 /dev/zero | tr '\0' 'x' > "$BACKUP_DIR/b-1.tar.gz"
  touch -d "2 hours ago" "$BACKUP_DIR/b-1.tar.gz"
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"archive_corrupt"'* ]]
}

@test "проверка целостности: корректный gzip проходит и запоминается" {
  write_conf "" 'VERIFY_INTEGRITY="yes"'
  echo data | gzip > "$BACKUP_DIR/b-1.tar.gz"
  touch -d "2 hours ago" "$BACKUP_DIR/b-1.tar.gz"
  run_check

  [ "$status" -eq 0 ]
  [[ "$(last_log)" == *'"event":"verify_ok"'* ]]
  [ -s "$TMP_DIR/state/check_backups.verified" ]
}

@test "проверка целостности: уже проверенный архив повторно не читается" {
  write_conf "" 'VERIFY_INTEGRITY="yes"'
  echo data | gzip > "$BACKUP_DIR/b-1.tar.gz"
  touch -d "2 hours ago" "$BACKUP_DIR/b-1.tar.gz"
  run_check
  [ "$status" -eq 0 ]
  rm -f "$TMP_DIR"/logs/*.jsonl

  # Повторный запуск: утилиты проверки нет в PATH-стабе — вызов был бы виден по verify_ok
  run_check
  [ "$status" -eq 0 ]
  [[ "$(last_log)" == *'"event":"verify_cached"'* ]]
  [[ "$(last_log)" != *'"event":"verify_ok"'* ]]
}

@test "проверка целостности: свежий (ещё пишущийся) архив откладывается" {
  write_conf "" 'VERIFY_INTEGRITY="yes"'
  head -c 500 /dev/zero | tr '\0' 'x' > "$BACKUP_DIR/b-1.tar.gz"
  run_check

  [ "$status" -eq 0 ]
  [[ "$(last_log)" == *'"event":"verify_deferred"'* ]]
}

@test "пишущийся последний архив пропускается, проверяется предыдущий" {
  write_conf
  make_archive "b-1.tar.zst" 10000 30
  head -c 100 /dev/zero > "$BACKUP_DIR/b-2.tar.zst"
  grow_on_sleep "$BACKUP_DIR/b-2.tar.zst"
  run_check

  [ "$status" -eq 0 ]
  [ ! -e "$NOTIFY_LOG" ]
  [[ "$output" == *"b-1.tar.zst"*"ещё пишется: b-2.tar.zst"* ]]
  [[ "$(last_log)" == *'"event":"archive_writing"'* ]]
  [[ "$(last_log)" != *'"event":"archive_shrunk"'* ]]
}

@test "единственный архив ещё пишется: не ошибка" {
  write_conf
  : > "$BACKUP_DIR/b-1.tar.zst"
  grow_on_sleep "$BACKUP_DIR/b-1.tar.zst"
  run_check

  [ "$status" -eq 0 ]
  [[ "$output" == *"готовых архивов пока нет"* ]]
  [[ "$(last_log)" == *'"event":"dir_writing"'* ]]
}

@test "свежий, но неизменный архив проверяется как обычно" {
  write_conf
  make_archive "b-1.tar.zst" 10000 30
  head -c 100 /dev/zero > "$BACKUP_DIR/b-2.tar.zst"
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"archive_shrunk"'* ]]
  [[ "$(last_log)" != *'"event":"archive_writing"'* ]]
}

@test "архив, открытый на запись локальным процессом, считается пишущимся" {
  command -v lsof >/dev/null 2>&1 || skip "нет lsof"
  write_conf
  make_archive "b-1.tar.zst" 10000 30
  head -c 100 /dev/zero > "$BACKUP_DIR/b-2.tar.zst"
  exec 7>>"$BACKUP_DIR/b-2.tar.zst"
  run_check
  exec 7>&-

  [ "$status" -eq 0 ]
  [[ "$(last_log)" == *'"event":"archive_writing"'*"открыт на запись"* ]]
}

@test "растущий .part снимает ошибку устаревшего архива" {
  write_conf
  make_archive "b-1.7z" 1000 100
  : > "$BACKUP_DIR/b-2.7z.part"
  grow_on_sleep "$BACKUP_DIR/b-2.7z.part"
  run_check

  [ "$status" -eq 0 ]
  [[ "$(last_log)" == *'"event":"archive_stale_writing"'* ]]
  [[ "$(last_log)" != *'"event":"archive_stale"'* ]]
}

@test "заброшенный .part не скрывает устаревший архив" {
  write_conf
  make_archive "b-1.7z" 1000 100
  make_archive "b-2.7z.part" 100 50
  grow_on_sleep "$BACKUP_DIR/b-2.7z.part"
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"archive_stale"'* ]]
}

@test "проверка целостности: архив, изменившийся во время проверки, не повреждён" {
  write_conf "" 'VERIFY_INTEGRITY="yes"'
  make_archive "b-1.tar.gz" 500 2
  cat > "$STUB_DIR/gzip" <<'EOT'
#!/usr/bin/env bash
for f in "$@"; do :; done
printf 'x' >> "$f"
exit 1
EOT
  chmod +x "$STUB_DIR/gzip"
  run_check

  [ "$status" -eq 0 ]
  [[ "$(last_log)" == *"изменился во время проверки"* ]]
  [[ "$(last_log)" != *'"event":"archive_corrupt"'* ]]
}

@test "тайм-аут проверки пропорционален размеру архива" {
  run bash -c 'VERIFY_MIN_SPEED_MBPS=20; VERIFY_TIMEOUT_BASE_SECS=120
    eval "$(sed -n "/^verify_timeout_for()/,/^}/p" "$1")"
    verify_timeout_for $(( 20 * 1048576 * 100 ))' _ "$TMP_DIR/check_backups.sh"

  [ "$status" -eq 0 ]
  [ "$output" -eq 220 ]
}

@test "--no-notify подавляет уведомление, но не меняет код возврата" {
  write_conf
  make_archive "b-1.tar.zst" 1000 100
  run_check --no-notify

  [ "$status" -eq 1 ]
  [ ! -e "$NOTIFY_LOG" ]
}

@test "без BACKUP_DIRS: код 2 и уведомление" {
  : > "$TMP_DIR/conf/check_backups.conf"
  run_check

  [ "$status" -eq 2 ]
  [[ "$(last_log)" == *'"event":"config_missing"'* ]]
  [ -s "$NOTIFY_LOG" ]
}

@test "неверный лимит возраста в записи BACKUP_DIRS" {
  write_conf "|abc"
  make_archive "b-1.tar.zst" 1000 1
  run_check

  [ "$status" -eq 1 ]
  [[ "$(last_log)" == *'"event":"config_invalid"'* ]]
}

@test "неизвестный аргумент: код 2" {
  write_conf
  run_check --bogus

  [ "$status" -eq 2 ]
}

@test "dry-run does not notify nor create state directory" {
  write_conf

  run_check --dry-run

  [ "$status" -eq 1 ]
  [ ! -e "$NOTIFY_LOG" ]
  [ ! -e "$TMP_DIR/state" ]
  [[ "$output" == *"[dry-run] будет: уведомление"* ]]
}
