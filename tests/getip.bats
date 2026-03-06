#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  WORK_DIR="$TMP_DIR/work"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$WORK_DIR" "$STUB_DIR" "$TMP_DIR/data" "$WORK_DIR/conf"
  cp "$REPO_ROOT/getip.sh" "$WORK_DIR/getip.sh"
  chmod +x "$WORK_DIR/getip.sh"

  cat > "$WORK_DIR/conf/getip.conf" <<EOF
IP_SERVICE_URL="https://example.invalid/ip"
IP_FILE="$TMP_DIR/data/ip.txt"
IP_HISTORY_FILE="$TMP_DIR/data/ip_history.txt"
EOF

  cat > "$STUB_DIR/curl" <<'EOF'
#!/usr/bin/env bash
printf '203.0.113.10\n'
EOF

  chmod +x "$STUB_DIR/curl"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "writes current IP and appends one history entry for same IP" {
  run env PATH="$STUB_DIR:$PATH" bash -c "cd \"$WORK_DIR\" && ./getip.sh"
  [ "$status" -eq 0 ]

  run env PATH="$STUB_DIR:$PATH" bash -c "cd \"$WORK_DIR\" && ./getip.sh"
  [ "$status" -eq 0 ]

  run bash -c "wc -l < \"$TMP_DIR/data/ip_history.txt\""
  [ "$status" -eq 0 ]
  [ "$output" -eq 1 ]

  run cat "$TMP_DIR/data/ip.txt"
  [ "$status" -eq 0 ]
  [ "$output" = "203.0.113.10" ]
}
