#!/usr/bin/env bats

setup() {
  command -v iconv >/dev/null || skip "iconv is required"
  command -v file >/dev/null || skip "file is required"
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "dry-run reports cp1251 files" {
  printf 'Привет мир\n' | iconv -f UTF-8 -t WINDOWS-1251 > "$TMP_DIR/book.txt"

  run bash "$REPO_ROOT/cp1251_to_utf8.sh" "$TMP_DIR" --dry-run

  [ "$status" -eq 0 ]
  [[ "$output" == *"[DRY-RUN] перекодировать: $TMP_DIR/book.txt"* ]]

  run iconv -f UTF-8 -t UTF-8 "$TMP_DIR/book.txt"
  [ "$status" -ne 0 ]
}

@test "apply mode converts cp1251 file to utf-8" {
  printf 'Привет мир\n' | iconv -f UTF-8 -t WINDOWS-1251 > "$TMP_DIR/book.txt"

  run bash "$REPO_ROOT/cp1251_to_utf8.sh" "$TMP_DIR"

  [ "$status" -eq 0 ]
  run iconv -f UTF-8 -t UTF-8 "$TMP_DIR/book.txt"
  [ "$status" -eq 0 ]
}
