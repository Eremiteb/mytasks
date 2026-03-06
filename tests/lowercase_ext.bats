#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "dry-run does not rename files" {
  touch "$TMP_DIR/Track.MP3"

  run bash "$REPO_ROOT/lowercase_ext.sh" --dry-run "$TMP_DIR"

  [ "$status" -eq 0 ]
  [[ "$output" == *"WOULD RENAME: $TMP_DIR/Track.MP3 -> $TMP_DIR/Track.mp3"* ]]
  [ -f "$TMP_DIR/Track.MP3" ]
  [ ! -e "$TMP_DIR/Track.mp3" ]
}

@test "apply mode renames extension to lowercase" {
  touch "$TMP_DIR/Cover.JPG"
  touch "$TMP_DIR/keep.mp3"

  run bash "$REPO_ROOT/lowercase_ext.sh" --apply "$TMP_DIR"

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/Cover.JPG" ]
  [ -f "$TMP_DIR/Cover.jpg" ]
  [ -f "$TMP_DIR/keep.mp3" ]
}
