#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "dry-run reports matching files without deleting" {
  touch "$TMP_DIR/remove.tmp"
  touch "$TMP_DIR/keep.txt"

  run bash "$REPO_ROOT/delete_by_ext.sh" -d "$TMP_DIR" -e tmp --dry-run

  [ "$status" -eq 0 ]
  [[ "$output" == *"[DRY-RUN] $TMP_DIR/remove.tmp"* ]]
  [ -f "$TMP_DIR/remove.tmp" ]
  [ -f "$TMP_DIR/keep.txt" ]
}

@test "apply mode deletes only target extension" {
  touch "$TMP_DIR/remove.log"
  touch "$TMP_DIR/keep.txt"

  run bash "$REPO_ROOT/delete_by_ext.sh" -d "$TMP_DIR" -e log

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/remove.log" ]
  [ -f "$TMP_DIR/keep.txt" ]
}
