#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$STUB_DIR" "$TMP_DIR/books"
  cp "$REPO_ROOT/unpack-fb2zip.sh" "$TMP_DIR/unpack-fb2zip.sh"
  chmod +x "$TMP_DIR/unpack-fb2zip.sh"

  : > "$TMP_DIR/books/sample.fb2.zip"

  cat > "$STUB_DIR/unzip" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-Z1" ]; then
  echo "inside/book.fb2"
  exit 0
fi

if [ "$1" = "-p" ]; then
  echo "<FictionBook/>"
  exit 0
fi

exit 1
EOF

  chmod +x "$STUB_DIR/unzip"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "dry-run does not create extracted files" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/unpack-fb2zip.sh" -p "$TMP_DIR/books" --dry-run

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/books/book.fb2" ]
}

@test "extract mode writes fb2 file next to archive" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/unpack-fb2zip.sh" -p "$TMP_DIR/books"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/books/book.fb2" ]

  run grep -q '<FictionBook/>' "$TMP_DIR/books/book.fb2"
  [ "$status" -eq 0 ]
}
