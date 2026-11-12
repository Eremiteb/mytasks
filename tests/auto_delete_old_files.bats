#!/usr/bin/env bats

setup() {
  # Логи скриптов — во временный каталог теста, а не в logs/ репозитория
  export MYTASKS_LOG_DIR="$BATS_TEST_TMPDIR/logs"
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  TMP_CONF_DIR="$REPO_ROOT/conf"
  CONF_FILE="$TMP_CONF_DIR/auto_delete_old_files.conf"
  CONF_BACKUP="$TMP_DIR/auto_delete_old_files.conf"
  if [ -e "$CONF_FILE" ]; then
    cp -- "$CONF_FILE" "$CONF_BACKUP"
  else
    CONF_BACKUP=""
  fi
}

teardown() {
  if [ -n "$CONF_BACKUP" ]; then
    mv -f -- "$CONF_BACKUP" "$CONF_FILE"
  else
    rm -f "$CONF_FILE"
  fi
  rm -rf "$TMP_DIR"
}

@test "deletes only selected old extension" {
  touch -d '10 days ago' "$TMP_DIR/old.log"
  touch -d '10 days ago' "$TMP_DIR/old.txt"
  touch "$TMP_DIR/new.log"

  cat > "$CONF_FILE" <<EOF
TARGET_DIRS="$TMP_DIR"
FILE_TYPES="log"
KEEP_DAYS="7"
EOF

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/old.log" ]
  [ -f "$TMP_DIR/old.txt" ]
  [ -f "$TMP_DIR/new.log" ]
}

@test "all types removes all old files" {
  touch -d '20 days ago' "$TMP_DIR/old1.tmp"
  touch -d '20 days ago' "$TMP_DIR/old2.log"
  touch "$TMP_DIR/new.txt"

  cat > "$CONF_FILE" <<EOF
TARGET_DIRS="$TMP_DIR"
FILE_TYPES="all"
KEEP_DAYS="7"
EOF

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/old1.tmp" ]
  [ ! -e "$TMP_DIR/old2.log" ]
  [ -f "$TMP_DIR/new.txt" ]
}

@test "dry-run lists old files but does not delete them" {
  touch -d '10 days ago' "$TMP_DIR/old.log"
  touch "$TMP_DIR/new.log"

  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="$TMP_DIR"
FILE_TYPES="log"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh" --dry-run

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/old.log" ]
  [ -f "$TMP_DIR/new.log" ]
  [[ "$output" == *"[dry-run] будет: удалён файл: $TMP_DIR/old.log"* ]]
  [[ "$output" != *"new.log"* ]]
}

@test "удаляет старые файлы из TARGET_DIRS, включая путь с пробелом" {
  first_dir="$TMP_DIR/first"
  second_dir="$TMP_DIR/with space"
  mkdir -p "$first_dir" "$second_dir"
  touch -d '10 days ago' "$first_dir/old.log" "$second_dir/old.log"

  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="$first_dir
$second_dir"
FILE_TYPES="log"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 0 ]
  [ ! -e "$first_dir/old.log" ]
  [ ! -e "$second_dir/old.log" ]
}

@test "невалидный второй TARGET_DIRS не удаляет файлы из первого" {
  first_dir="$TMP_DIR/first"
  missing_dir="$TMP_DIR/missing"
  mkdir -p "$first_dir"
  touch -d '10 days ago' "$first_dir/old.log"

  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="$first_dir
$missing_dir"
FILE_TYPES="log"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 2 ]
  [ -f "$first_dir/old.log" ]
}

@test "отказ при каталоге верхнего уровня (глубина < 2)" {
  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="/tmp"
FILE_TYPES="all"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 2 ]
  [[ "$output" == *"небезопасный каталог"* ]]
}

@test "отказ при каталоге, равном корню" {
  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="/"
FILE_TYPES="all"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 2 ]
}

@test "отказ при каталоге, равном HOME" {
  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="$TMP_DIR"
FILE_TYPES="all"
KEEP_DAYS="7"
EOF2

  run env HOME="$TMP_DIR" bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 2 ]
  [[ "$output" == *"небезопасный каталог"* ]]
}

@test "недопустимый тип в FILE_TYPES отвергается до удаления" {
  touch -d '20 days ago' "$TMP_DIR/old.log"
  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="$TMP_DIR"
FILE_TYPES="log,*"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 2 ]
  [ -f "$TMP_DIR/old.log" ]
}

@test "перевод строки в имени файла не позволяет удалить файл вне каталога" {
  target_dir="$TMP_DIR/target"
  outside_file="$TMP_DIR/outside.log"
  injected_file="$target_dir/prefix"$'\n'"$outside_file"
  mkdir -p "$(dirname -- "$injected_file")"
  touch -d '20 days ago' "$injected_file" "$outside_file"

  cat > "$CONF_FILE" <<EOF2
TARGET_DIRS="$target_dir"
FILE_TYPES="all"
KEEP_DAYS="7"
EOF2

  run bash "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 0 ]
  [ ! -e "$injected_file" ]
  [ -f "$outside_file" ]
}

@test "json_escape экранирует перевод строки в имени файла" {
  sed -n '/^json_escape() {/,/^}/p' "$REPO_ROOT/auto_delete_old_files.sh" > "$TMP_DIR/je.sh"
  # shellcheck source=/dev/null
  . "$TMP_DIR/je.sh"
  out="$(json_escape 'a
b"c\d')"
  [ "$out" = 'a\nb\"c\\d' ]
}
