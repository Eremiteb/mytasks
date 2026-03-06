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
