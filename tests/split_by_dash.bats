#!/usr/bin/env bats

setup() {
  # Логи скриптов — во временный каталог теста, а не в logs/ репозитория
  export MYTASKS_LOG_DIR="$BATS_TEST_TMPDIR/logs"
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

@test "merges case-variant folders into the one matching the artist case" {
  mkdir -p "$TMP_DIR/music/ARTIST" "$TMP_DIR/music/artist"
  touch "$TMP_DIR/music/ARTIST/Old - One.mp3" "$TMP_DIR/music/artist/Old - Two.mp3"
  touch "$TMP_DIR/music/Artist - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/Artist - Song.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Old - One.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Old - Two.mp3" ]
  [ ! -e "$TMP_DIR/music/ARTIST" ]
  [ ! -e "$TMP_DIR/music/artist" ]
}

@test "merges Cyrillic 'Имя Фамилия' folders case-insensitively" {
  mkdir -p "$TMP_DIR/music/ИВАН ПЕТРОВ" "$TMP_DIR/music/иван петров"
  touch "$TMP_DIR/music/ИВАН ПЕТРОВ/Старый - Один.mp3" "$TMP_DIR/music/иван петров/Старый - Два.mp3"
  touch "$TMP_DIR/music/Иван Петров - Песня.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Иван Петров/Иван Петров - Песня.mp3" ]
  [ -f "$TMP_DIR/music/Иван Петров/Старый - Один.mp3" ]
  [ -f "$TMP_DIR/music/Иван Петров/Старый - Два.mp3" ]
  [ ! -e "$TMP_DIR/music/ИВАН ПЕТРОВ" ]
  [ ! -e "$TMP_DIR/music/иван петров" ]
}

@test "merges case-variant folders in DEST_DIR" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest/иван петров"
  touch "$TMP_DIR/dest/иван петров/Старый - Один.mp3"
  touch "$TMP_DIR/music/Иван Петров - Песня.mp3"
  mkdir -p "$TMP_DIR/conf"
  printf 'DEST_DIR=%s\n' "$TMP_DIR/dest" > "$TMP_DIR/conf/split_by_dash.conf"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/dest/Иван Петров/Иван Петров - Песня.mp3" ]
  [ -f "$TMP_DIR/dest/Иван Петров/Старый - Один.mp3" ]
  [ ! -e "$TMP_DIR/dest/иван петров" ]
}

@test "dry-run changes nothing but reports the plan" {
  mkdir -p "$TMP_DIR/music/иван петров" "$TMP_DIR/music/ИВАН ПЕТРОВ"
  touch "$TMP_DIR/music/иван петров/Старый - Один.mp3"
  touch "$TMP_DIR/music/Иван Петров - Песня.mp3" "$TMP_DIR/music/Artist - Song.mp3"
  before="$(cd "$TMP_DIR/music" && find . | sort)"

  run bash "$TMP_DIR/split_by_dash.sh" --dry-run "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ "$(cd "$TMP_DIR/music" && find . | sort)" = "$before" ]
  [[ "$output" == *"[dry-run]"*"Иван Петров - Песня.mp3"* ]]
  [[ "$output" == *"[dry-run]"*"Artist/Artist - Song.mp3"* ]]
  [[ "$output" == *"Изменения не выполнены"* ]]
}

@test "dry-run with DEST_DIR changes nothing" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest/иван петров" "$TMP_DIR/conf"
  touch "$TMP_DIR/music/Иван Петров - Песня.mp3"
  printf 'DEST_DIR=%s\n' "$TMP_DIR/dest" > "$TMP_DIR/conf/split_by_dash.conf"
  before="$(cd "$TMP_DIR" && find . -not -path './conf*' | sort)"

  run bash "$TMP_DIR/split_by_dash.sh" -n "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ "$(cd "$TMP_DIR" && find . -not -path './conf*' | sort)" = "$before" ]
  [[ "$output" == *"[dry-run]"*"dest"* ]]
}

@test "merges case-variant folders inside a directory independently, keeping the one with more files" {
  mkdir -p "$TMP_DIR/music/Hurts" "$TMP_DIR/music/HURTS" "$TMP_DIR/music/Иван Петров" "$TMP_DIR/music/иван петров"
  touch "$TMP_DIR/music/Hurts/a.mp3" "$TMP_DIR/music/Hurts/b.mp3" "$TMP_DIR/music/HURTS/c.mp3"
  touch "$TMP_DIR/music/Иван Петров/a.mp3" "$TMP_DIR/music/иван петров/b.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Hurts/a.mp3" ] && [ -f "$TMP_DIR/music/Hurts/b.mp3" ] && [ -f "$TMP_DIR/music/Hurts/c.mp3" ]
  [ ! -e "$TMP_DIR/music/HURTS" ]
  # равное число файлов — эталон первый по кодовым точкам
  [ -f "$TMP_DIR/music/Иван Петров/a.mp3" ] && [ -f "$TMP_DIR/music/Иван Петров/b.mp3" ]
  [ ! -e "$TMP_DIR/music/иван петров" ]
}

@test "case-merge does not look into nested or other directories" {
  mkdir -p "$TMP_DIR/music/_cloud/Hurts" "$TMP_DIR/music/HURTS" "$TMP_DIR/music/Artist/Album" "$TMP_DIR/music/Artist/album"
  touch "$TMP_DIR/music/_cloud/Hurts/a.mp3" "$TMP_DIR/music/HURTS/b.mp3" "$TMP_DIR/music/Artist/Album/x.mp3" "$TMP_DIR/music/Artist/album/y.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music/_cloud"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/_cloud/Hurts/a.mp3" ]
  [ -f "$TMP_DIR/music/HURTS/b.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Album/x.mp3" ] && [ -f "$TMP_DIR/music/Artist/album/y.mp3" ]
}

@test "DEST_DIR is case-merged on its own, not against the processed directory" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest/Hurts" "$TMP_DIR/dest/HURTS" "$TMP_DIR/conf"
  touch "$TMP_DIR/dest/Hurts/a.mp3" "$TMP_DIR/dest/Hurts/b.mp3" "$TMP_DIR/dest/HURTS/c.mp3"
  touch "$TMP_DIR/music/Other - Song.mp3"
  printf 'DEST_DIR=%s\n' "$TMP_DIR/dest" > "$TMP_DIR/conf/split_by_dash.conf"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/dest/Hurts/c.mp3" ]
  [ ! -e "$TMP_DIR/dest/HURTS" ]
  [ -f "$TMP_DIR/dest/Other/Other - Song.mp3" ]
}

@test "case-merge: all-caps loses even with more files, ties prefer 'Первая заглавная, остальные строчные'" {
  mkdir -p "$TMP_DIR/music/HURTS" "$TMP_DIR/music/Hurts" "$TMP_DIR/music/JANAGA" "$TMP_DIR/music/Janaga"
  mkdir -p "$TMP_DIR/music/MD DJ&Olivia" "$TMP_DIR/music/MD Dj&Olivia" "$TMP_DIR/music/in hoode" "$TMP_DIR/music/In hoode"
  touch "$TMP_DIR/music/HURTS/a.mp3" "$TMP_DIR/music/HURTS/b.mp3" "$TMP_DIR/music/HURTS/c.mp3" "$TMP_DIR/music/Hurts/d.mp3"
  touch "$TMP_DIR/music/JANAGA/a.mp3" "$TMP_DIR/music/Janaga/b.mp3"
  touch "$TMP_DIR/music/MD DJ&Olivia/a.mp3" "$TMP_DIR/music/MD Dj&Olivia/b.mp3"
  touch "$TMP_DIR/music/in hoode/a.mp3" "$TMP_DIR/music/In hoode/b.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Hurts/a.mp3" ] && [ -f "$TMP_DIR/music/Hurts/d.mp3" ] && [ ! -e "$TMP_DIR/music/HURTS" ]
  [ -f "$TMP_DIR/music/Janaga/a.mp3" ] && [ ! -e "$TMP_DIR/music/JANAGA" ]
  [ -f "$TMP_DIR/music/MD Dj&Olivia/a.mp3" ] && [ ! -e "$TMP_DIR/music/MD DJ&Olivia" ]
  [ -f "$TMP_DIR/music/In hoode/a.mp3" ] && [ ! -e "$TMP_DIR/music/in hoode" ]
}
