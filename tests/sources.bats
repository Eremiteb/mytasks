#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$STUB_DIR" "$TMP_DIR/src" "$TMP_DIR/dst" "$TMP_DIR/conf"
  cp "$REPO_ROOT/sources.sh" "$TMP_DIR/sources.sh"
  chmod +x "$TMP_DIR/sources.sh"

  printf 'example\n' > "$TMP_DIR/src/file1.txt"

  cat > "$TMP_DIR/conf/custom.conf" <<EOF
$TMP_DIR/src/ | $TMP_DIR/dst/ | -a
EOF

  cat > "$STUB_DIR/rsync" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$TMP_DIR/rsync_args.txt"
printf '>f+++++++++\tfile1.txt\n'
exit 0
EOF
  chmod +x "$STUB_DIR/rsync"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "prints usage with -h" {
  run sh "$TMP_DIR/sources.sh" -h

  [ "$status" -eq 0 ]
  [[ "$output" == *"Использование"* ]]
}

@test "dry-run logs would_write and forwards --dry-run to rsync" {
  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/custom.conf" -n

  [ "$status" -eq 0 ]
  run grep -q -- "--dry-run" "$TMP_DIR/rsync_args.txt"
  [ "$status" -eq 0 ]

  run find "$TMP_DIR/logs" -maxdepth 1 -type f -name 'sources_*-report.jsonl'
  [ "$status" -eq 0 ]
  [ -n "$output" ]

  log_file="$output"
  run grep -q '"event":"would_write"' "$log_file"
  [ "$status" -eq 0 ]
}
