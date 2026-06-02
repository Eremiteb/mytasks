#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  TMP_CONF_DIR="$REPO_ROOT/conf"
  CONF_FILE="$TMP_CONF_DIR/auto_delete_old_files.conf"
}

teardown() {
  rm -rf "$TMP_DIR"
  rm -f "$CONF_FILE"
}

@test "deletes only selected old extension" {
  touch -d '10 days ago' "$TMP_DIR/old.log"
  touch -d '10 days ago' "$TMP_DIR/old.txt"
  touch "$TMP_DIR/new.log"

  cat > "$CONF_FILE" <<EOF
TARGET_DIR="$TMP_DIR"
FILE_TYPES="log"
KEEP_DAYS="7"
EOF

  run sh "$REPO_ROOT/auto_delete_old_files.sh"

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
TARGET_DIR="$TMP_DIR"
FILE_TYPES="all"
KEEP_DAYS="7"
EOF

  run sh "$REPO_ROOT/auto_delete_old_files.sh"

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/old1.tmp" ]
  [ ! -e "$TMP_DIR/old2.log" ]
  [ -f "$TMP_DIR/new.txt" ]
}
