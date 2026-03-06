#!/usr/bin/env bats

setup() {
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
