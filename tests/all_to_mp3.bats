#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  WORK_DIR="$TMP_DIR/work"
  SRC_DIR="$TMP_DIR/src"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$WORK_DIR" "$SRC_DIR" "$STUB_DIR" "$WORK_DIR/conf"
  cp "$REPO_ROOT/all_to_mp3.sh" "$WORK_DIR/all_to_mp3.sh"
  chmod +x "$WORK_DIR/all_to_mp3.sh"

  cat > "$WORK_DIR/conf/audio_to_mp3.conf" <<EOF
FORMATS="wav"
QUALITY=0
COVER_NAMES="cover.jpg folder.jpg front.jpg"
JOBS=1
OUTPUT_DIR="$TMP_DIR/out"
ERROR_LOG="audio_to_mp3_errors.log"
REPORT_FILE="audio_to_mp3_report.txt"
EOF

  touch "$SRC_DIR/Track.wav"

  cat > "$STUB_DIR/ffprobe" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "$STUB_DIR/ffmpeg" <<'EOF'
#!/usr/bin/env bash
out="${@: -1}"
: > "$out"
exit 0
EOF

  chmod +x "$STUB_DIR/ffprobe" "$STUB_DIR/ffmpeg"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "shows usage when source directory is missing" {
  run bash "$REPO_ROOT/all_to_mp3.sh"

  [ "$status" -eq 1 ]
  [[ "$output" == *"Использование:"* ]]
}

@test "creates mp3 output with mocked ffmpeg and ffprobe" {
  run env PATH="$STUB_DIR:$PATH" bash -c "cd \"$WORK_DIR\" && ./all_to_mp3.sh \"$SRC_DIR\""

  [ "$status" -eq 0 ]
  run find "$TMP_DIR/out" -type f -name '*.mp3'
  [ "$status" -eq 0 ]
  [ -n "$output" ]
}
