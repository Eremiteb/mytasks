#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  cp "$REPO_ROOT/split_by_dash.sh" "$TMP_DIR/split_by_dash.sh"
  chmod +x "$TMP_DIR/split_by_dash.sh"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "moves file into artist directory" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/Artist - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/music/Artist - Song.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Artist - Song.mp3" ]
}

@test "adds numeric suffix on name conflict" {
  mkdir -p "$TMP_DIR/music/Artist"
  touch "$TMP_DIR/music/Artist/Artist - Song.mp3"
  touch "$TMP_DIR/music/Artist - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/Artist - Song.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Artist - Song.mp3.1" ]
}
