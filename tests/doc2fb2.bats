#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "help exits successfully" {
  run bash "$REPO_ROOT/doc2fb2.sh" --help

  [ "$status" -eq 0 ]
  [[ "$output" == *"doc2fb2.sh"* ]]
}

@test "invalid --only value fails fast" {
  mkdir -p "$TMP_DIR/in"

  run bash "$REPO_ROOT/doc2fb2.sh" -d "$TMP_DIR/in" --only bad

  [ "$status" -eq 2 ]
  [[ "$output" == *"--only all|doc|docx|rtf|odt"* ]]
}

@test "TOC_DEPTH and LOG_LEVEL from the config are applied, flags win" {
  mkdir -p "$TMP_DIR/app/conf"
  cp "$REPO_ROOT/doc2fb2.sh" "$TMP_DIR/app/doc2fb2.sh"
  printf 'TOC_DEPTH=5\nLOG_LEVEL=error\n' > "$TMP_DIR/app/conf/doc2fb2.conf"

  run bash "$TMP_DIR/app/doc2fb2.sh" --debug -d "$TMP_DIR"

  [ "$status" -eq 0 ]
  [[ "$output" == *"TOC_DEPTH=5"* ]]
  [[ "$output" == *"LOG_LEVEL=error"* ]]

  run bash "$TMP_DIR/app/doc2fb2.sh" --debug --toc-depth 2 -d "$TMP_DIR"

  [[ "$output" == *"TOC_DEPTH=2"* ]]
}
