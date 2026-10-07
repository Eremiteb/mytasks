#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"

  cp "$REPO_ROOT/music_downloader.sh" "$TMP_DIR/music_downloader.sh"
  chmod +x "$TMP_DIR/music_downloader.sh"

  mkdir -p "$TMP_DIR/music_downloader"

  cat > "$TMP_DIR/split_by_dash.sh" <<'EOF'
#!/usr/bin/env sh
touch "$(dirname "$0")/split_called"
EOF
  chmod +x "$TMP_DIR/split_by_dash.sh"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "runs split_by_dash.sh after successful python run" {
  cat > "$TMP_DIR/music_downloader/music_downloader.py" <<'EOF'
import sys
sys.exit(0)
EOF

  run bash "$TMP_DIR/music_downloader.sh"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/split_called" ]
}

@test "returns python exit code and skips split script on failure" {
  cat > "$TMP_DIR/music_downloader/music_downloader.py" <<'EOF'
import sys
sys.exit(7)
EOF

  rm -f "$TMP_DIR/split_called"
  run bash "$TMP_DIR/music_downloader.sh"

  [ "$status" -eq 7 ]
  [ ! -e "$TMP_DIR/split_called" ]
}

@test "dry-run does not start downloader and runs sorter with --dry-run" {
  cat > "$TMP_DIR/music_downloader/music_downloader.py" <<'EOF2'
import pathlib
pathlib.Path(__file__).with_name("downloader_called").touch()
EOF2
  cat > "$TMP_DIR/split_by_dash.sh" <<'EOF2'
#!/usr/bin/env sh
echo "$*" > "$(dirname "$0")/split_args"
EOF2

  run bash "$TMP_DIR/music_downloader.sh" --dry-run

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/music_downloader/downloader_called" ]
  [ "$(cat "$TMP_DIR/split_args")" = "--dry-run" ]
  [[ "$output" == *"[dry-run] будет: запуск загрузчика"* ]]
}
