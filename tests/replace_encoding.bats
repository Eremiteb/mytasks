#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "dry-run keeps files unchanged" {
  printf 'charset=windows-1251\n' > "$TMP_DIR/test.txt"

  run bash "$REPO_ROOT/replace_encoding.sh" -d "$TMP_DIR"

  [ "$status" -eq 0 ]
  run grep -q "windows-1251" "$TMP_DIR/test.txt"
  [ "$status" -eq 0 ]
}

@test "no-recursive mode changes only top-level files" {
  mkdir -p "$TMP_DIR/sub"
  printf 'windows-1251\n' > "$TMP_DIR/root.txt"
  printf 'windows-1251\n' > "$TMP_DIR/sub/nested.txt"

  run bash "$REPO_ROOT/replace_encoding.sh" -n "$TMP_DIR"

  [ "$status" -eq 0 ]
  run grep -q "utf-8" "$TMP_DIR/root.txt"
  [ "$status" -eq 0 ]
  run grep -q "windows-1251" "$TMP_DIR/sub/nested.txt"
  [ "$status" -eq 0 ]
}
