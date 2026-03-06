#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$STUB_DIR"
  cp "$REPO_ROOT/hp_p1005_garuda.sh" "$TMP_DIR/hp_p1005_garuda.sh"
  chmod +x "$TMP_DIR/hp_p1005_garuda.sh"

  cat > "$STUB_DIR/pacman" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF

  cat > "$STUB_DIR/lsusb" <<'EOF'
#!/usr/bin/env bash
echo 'Bus 001 Device 002: ID 03f0:3d17 HP LaserJet P1005'
EOF

  cat > "$STUB_DIR/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/lp" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/system-config-printer" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/sudo" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/lsmod" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  chmod +x "$STUB_DIR/pacman" "$STUB_DIR/lsusb" "$STUB_DIR/systemctl" \
           "$STUB_DIR/lp" "$STUB_DIR/system-config-printer" "$STUB_DIR/sudo" \
           "$STUB_DIR/lsmod" "$STUB_DIR/sleep"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "--all mode reaches completion path with mocked system commands" {
  run env PATH="$STUB_DIR:$PATH" bash -c "cd \"$TMP_DIR\" && ./hp_p1005_garuda.sh --all"

  [[ "$output" == *"Done. Log:"* ]]
  run find "$TMP_DIR" -maxdepth 1 -type f -name 'hp_p1005_garuda_*.jsonl'
  [ "$status" -eq 0 ]
  [ -n "$output" ]
}
