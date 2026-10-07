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

  run bash "$TMP_DIR/split_by_dash.sh" --dest "$TMP_DIR/dest" "$TMP_DIR/music"

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
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest/иван петров"
  touch "$TMP_DIR/music/Иван Петров - Песня.mp3"
  before="$(cd "$TMP_DIR" && find . -not -path './conf*' | sort)"

  run bash "$TMP_DIR/split_by_dash.sh" -n --dest "$TMP_DIR/dest" "$TMP_DIR/music"

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
  [ -f "$TMP_DIR/music/Hurts/a.mp3" ]
  [ -f "$TMP_DIR/music/Hurts/b.mp3" ]
  [ -f "$TMP_DIR/music/Hurts/c.mp3" ]
  [ ! -e "$TMP_DIR/music/HURTS" ]
  # равное число файлов — эталон первый по кодовым точкам
  [ -f "$TMP_DIR/music/Иван Петров/a.mp3" ]
  [ -f "$TMP_DIR/music/Иван Петров/b.mp3" ]
  [ ! -e "$TMP_DIR/music/иван петров" ]
}

@test "case-merge does not look into nested or other directories" {
  mkdir -p "$TMP_DIR/music/_cloud/Hurts" "$TMP_DIR/music/HURTS" "$TMP_DIR/music/Artist/Album" "$TMP_DIR/music/Artist/album"
  touch "$TMP_DIR/music/_cloud/Hurts/a.mp3" "$TMP_DIR/music/HURTS/b.mp3" "$TMP_DIR/music/Artist/Album/x.mp3" "$TMP_DIR/music/Artist/album/y.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music/_cloud"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/_cloud/Hurts/a.mp3" ]
  [ -f "$TMP_DIR/music/HURTS/b.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Album/x.mp3" ]
  [ -f "$TMP_DIR/music/Artist/album/y.mp3" ]
}

@test "DEST_DIR is case-merged on its own, not against the processed directory" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest/Hurts" "$TMP_DIR/dest/HURTS"
  touch "$TMP_DIR/dest/Hurts/a.mp3" "$TMP_DIR/dest/Hurts/b.mp3" "$TMP_DIR/dest/HURTS/c.mp3"
  touch "$TMP_DIR/music/Other - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" --dest "$TMP_DIR/dest" "$TMP_DIR/music"

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
  [ -f "$TMP_DIR/music/Hurts/a.mp3" ]
  [ -f "$TMP_DIR/music/Hurts/d.mp3" ]
  [ ! -e "$TMP_DIR/music/HURTS" ]
  [ -f "$TMP_DIR/music/Janaga/a.mp3" ]
  [ ! -e "$TMP_DIR/music/JANAGA" ]
  [ -f "$TMP_DIR/music/MD Dj & Olivia/a.mp3" ]
  [ ! -e "$TMP_DIR/music/MD DJ & Olivia" ]
  [ -f "$TMP_DIR/music/In hoode/a.mp3" ]
  [ ! -e "$TMP_DIR/music/in hoode" ]
}

@test "strips trailing dots from folder, artist part and file name" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/Автор. - песня.mp3."

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Автор/Автор - песня.mp3" ]
  [ ! -e "$TMP_DIR/music/Автор." ]
}

@test "strips multiple trailing dots and merges dotted folder into the dotless one" {
  mkdir -p "$TMP_DIR/music/Fred again.." "$TMP_DIR/music/Fred again"
  touch "$TMP_DIR/music/Fred again../a.mp3" "$TMP_DIR/music/Fred again/b.mp3"
  touch "$TMP_DIR/music/Fred again.. - c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Fred again/a.mp3" ]
  [ -f "$TMP_DIR/music/Fred again/b.mp3" ]
  [ -f "$TMP_DIR/music/Fred again/Fred again - c.mp3" ]
  [ ! -e "$TMP_DIR/music/Fred again.." ]
}

@test "renames an existing lone folder with trailing dot" {
  mkdir -p "$TMP_DIR/music/Robin S." "$TMP_DIR/conf"
  touch "$TMP_DIR/music/Robin S./a.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Robin S/a.mp3" ]
  [ ! -e "$TMP_DIR/music/Robin S." ]
}

@test "dry-run reports dotted folders without renaming" {
  mkdir -p "$TMP_DIR/music/Robin S."
  touch "$TMP_DIR/music/Robin S./a.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" --dry-run "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Robin S./a.mp3" ]
  [ ! -e "$TMP_DIR/music/Robin S" ]
  [[ "$output" == *"[dry-run]"*"Robin S."*"Robin S"* ]]
}

@test "windows-unsafe characters and reserved names are fixed on split" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/AC:DC - Who? Made*Of<Me>.mp3"
  touch "$TMP_DIR/music/CON - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/AC_DC/AC_DC - Who_ Made_Of_Me_.mp3" ]
  [ -f "$TMP_DIR/music/_CON/_CON - Song.mp3" ]
}

@test "existing unsafe names at any depth are renamed, deepest first" {
  mkdir -p "$TMP_DIR/music/Artist/Album. "
  touch "$TMP_DIR/music/Artist/Album. /Track?.mp3" "$TMP_DIR/music/Artist/NUL.txt"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/Album/Track_.mp3" ]
  [ -f "$TMP_DIR/music/Artist/_NUL.txt" ]
  [ ! -e "$TMP_DIR/music/Artist/Album. " ]
}

@test "renamed file keeps the larger one when names differ only by case" {
  mkdir -p "$TMP_DIR/music/Artist"
  printf 'aa' > "$TMP_DIR/music/Artist/song_.mp3"
  printf 'aaaa' > "$TMP_DIR/music/Artist/SONG?.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/SONG_.mp3" ]
  [ ! -e "$TMP_DIR/music/Artist/song_.mp3" ]
  [ "$(wc -c < "$TMP_DIR/music/Artist/SONG_.mp3")" -eq 4 ]
}

@test "case-colliding files: the larger one is kept, the smaller removed" {
  mkdir -p "$TMP_DIR/music/Retro"
  printf 'big-content' > "$TMP_DIR/music/Retro/Haddaway - What Is Love.mp3"
  printf 'small' > "$TMP_DIR/music/Retro/Haddaway - What is love.mp3"
  printf 'x' > "$TMP_DIR/music/Retro/Other.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Retro/Haddaway - What Is Love.mp3" ]
  [ ! -e "$TMP_DIR/music/Retro/Haddaway - What is love.mp3" ]
  [ -f "$TMP_DIR/music/Retro/Other.mp3" ]
}

@test "case-colliding files: larger lowercase variant wins over smaller uppercase" {
  mkdir -p "$TMP_DIR/music/Retro"
  printf 'a' > "$TMP_DIR/music/Retro/Song.mp3"
  printf 'aaaaaa' > "$TMP_DIR/music/Retro/song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Retro/song.mp3" ]
  [ ! -e "$TMP_DIR/music/Retro/Song.mp3" ]
}

@test "dry-run keeps case-colliding files and reports the removal" {
  mkdir -p "$TMP_DIR/music/Retro"
  printf 'big-content' > "$TMP_DIR/music/Retro/A.mp3"
  printf 'small' > "$TMP_DIR/music/Retro/a.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" --dry-run "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Retro/A.mp3" ]
  [ -f "$TMP_DIR/music/Retro/a.mp3" ]
  [[ "$output" == *"[dry-run]"*"a.mp3"* ]]
}

@test "dry-run reports unsafe names without renaming" {
  mkdir -p "$TMP_DIR/music/Artist"
  touch "$TMP_DIR/music/Artist/Track?.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" --dry-run "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/Track?.mp3" ]
  [ ! -e "$TMP_DIR/music/Artist/Track_.mp3" ]
  [[ "$output" == *"[dry-run]"*"Track_.mp3"* ]]
}

@test "merges folders differing in separators, prefers '&'" {
  mkdir -p "$TMP_DIR/music/10AGE Анет Сай" "$TMP_DIR/music/10AGE&Анет Сай"
  touch "$TMP_DIR/music/10AGE Анет Сай/a.mp3" "$TMP_DIR/music/10AGE Анет Сай/b.mp3" "$TMP_DIR/music/10AGE&Анет Сай/c.mp3"
  mkdir -p "$TMP_DIR/music/Ағайындылар Тобы Айгерім Битанова" "$TMP_DIR/music/Ағайындылар тобы & Айгерім Битанова"
  touch "$TMP_DIR/music/Ағайындылар Тобы Айгерім Битанова/a.mp3" "$TMP_DIR/music/Ағайындылар тобы & Айгерім Битанова/b.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/10AGE & Анет Сай/a.mp3" ]
  [ -f "$TMP_DIR/music/10AGE & Анет Сай/c.mp3" ]
  [ ! -e "$TMP_DIR/music/10AGE Анет Сай" ]
  [ -f "$TMP_DIR/music/Ағайындылар тобы & Айгерім Битанова/a.mp3" ]
  [ ! -e "$TMP_DIR/music/Ағайындылар Тобы Айгерім Битанова" ]
}

@test "merges folders differing in apostrophes and underscores, avoids underscore artifacts" {
  mkdir -p "$TMP_DIR/music/Банд'Эрос" "$TMP_DIR/music/Банд\`Эрос" "$TMP_DIR/music/БандЭрос"
  touch "$TMP_DIR/music/Банд'Эрос/a.mp3" "$TMP_DIR/music/Банд\`Эрос/b.mp3" "$TMP_DIR/music/БандЭрос/c.mp3"
  mkdir -p "$TMP_DIR/music/Dua_Lipa_" "$TMP_DIR/music/Dua Lipa"
  touch "$TMP_DIR/music/Dua_Lipa_/a.mp3" "$TMP_DIR/music/Dua_Lipa_/b.mp3" "$TMP_DIR/music/Dua Lipa/c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ "$(find "$TMP_DIR/music" -mindepth 1 -maxdepth 1 -type d -name 'Банд*' -o -mindepth 1 -maxdepth 1 -type d -name 'БандЭрос' | wc -l)" -eq 1 ]
  [ -f "$TMP_DIR/music/Dua Lipa/a.mp3" ]
  [ -f "$TMP_DIR/music/Dua Lipa/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Dua_Lipa_" ]
}

@test "official name from MusicBrainz is used and cached" {
  mkdir -p "$TMP_DIR/music/Банд'Эрос" "$TMP_DIR/music/Банд\`Эрос" "$TMP_DIR/music/БандЭрос" "$TMP_DIR/stubs"
  touch "$TMP_DIR/music/Банд'Эрос/a.mp3" "$TMP_DIR/music/Банд\`Эрос/b.mp3" "$TMP_DIR/music/БандЭрос/c.mp3"
  cat > "$TMP_DIR/stubs/curl" <<STUB
#!/usr/bin/env bash
echo x >> "$TMP_DIR/curl.calls"
printf '{"artists":[{"score":100,"name":"Банд’Эрос"},{"score":95,"name":"Другой Артист"}]}'
STUB
  chmod +x "$TMP_DIR/stubs/curl"

  run env PATH="$TMP_DIR/stubs:$PATH" OFFICIAL_NAMES=yes bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Банд’Эрос/a.mp3" ]
  [ -f "$TMP_DIR/music/Банд’Эрос/b.mp3" ]
  [ -f "$TMP_DIR/music/Банд’Эрос/c.mp3" ]
  [ ! -e "$TMP_DIR/music/БандЭрос" ]
  [ "$(wc -l < "$TMP_DIR/curl.calls")" -eq 1 ]
  grep -q 'бандэрос' "$TMP_DIR/state/split_by_dash_official.tsv"
}

@test "no official match falls back to the best existing variant" {
  mkdir -p "$TMP_DIR/music/A&B" "$TMP_DIR/music/A B" "$TMP_DIR/stubs"
  touch "$TMP_DIR/music/A&B/a.mp3" "$TMP_DIR/music/A B/b.mp3"
  printf '#!/usr/bin/env bash\nprintf '"'"'{"artists":[]}'"'"'\n' > "$TMP_DIR/stubs/curl"
  chmod +x "$TMP_DIR/stubs/curl"

  run env PATH="$TMP_DIR/stubs:$PATH" OFFICIAL_NAMES=yes bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/A & B/a.mp3" ]
  [ -f "$TMP_DIR/music/A & B/b.mp3" ]
  [ ! -e "$TMP_DIR/music/A B" ]
}

@test "dry-run with official names changes no directories" {
  mkdir -p "$TMP_DIR/music/Банд'Эрос" "$TMP_DIR/music/БандЭрос" "$TMP_DIR/stubs"
  touch "$TMP_DIR/music/Банд'Эрос/a.mp3" "$TMP_DIR/music/БандЭрос/c.mp3"
  printf '#!/usr/bin/env bash\nprintf '"'"'{"artists":[{"score":100,"name":"Банд’Эрос"}]}'"'"'\n' > "$TMP_DIR/stubs/curl"
  chmod +x "$TMP_DIR/stubs/curl"

  run env PATH="$TMP_DIR/stubs:$PATH" OFFICIAL_NAMES=yes bash "$TMP_DIR/split_by_dash.sh" --dry-run "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Банд'Эрос/a.mp3" ]
  [ -f "$TMP_DIR/music/БандЭрос/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Банд’Эрос" ]
  [[ "$output" == *"[dry-run]"*"Банд’Эрос"* ]]
}

@test "ampersand without spaces gets spaces in folders and files" {
  mkdir -p "$TMP_DIR/music/Kaskade&BUNT"
  touch "$TMP_DIR/music/Kaskade&BUNT/Kaskade&BUNT - Song.mp3"
  touch "$TMP_DIR/music/A&B&C - Track.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Kaskade & BUNT/Kaskade & BUNT - Song.mp3" ]
  [ ! -e "$TMP_DIR/music/Kaskade&BUNT" ]
  [ -f "$TMP_DIR/music/A & B & C/A & B & C - Track.mp3" ]
}

@test "ampersand with a space on one side only gets spaces on both sides" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/Aly &AJ - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Aly & AJ/Aly & AJ - Song.mp3" ]
}

@test "ampersand with spaces is left unchanged" {
  mkdir -p "$TMP_DIR/music/Hammali & Navai"
  touch "$TMP_DIR/music/Hammali & Navai/Hammali & Navai - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Hammali & Navai/Hammali & Navai - Song.mp3" ]
}

# Заглушка ffprobe: печатает содержимое проверяемого файла как тег исполнителя
make_ffprobe_stub() {
  mkdir -p "$TMP_DIR/stubs"
  printf '#!/usr/bin/env bash\ncat "${!#}"\n' > "$TMP_DIR/stubs/ffprobe"
  chmod +x "$TMP_DIR/stubs/ffprobe"
}

@test "disputed merge confirmed by files (common artist tag)" {
  make_ffprobe_stub
  mkdir -p "$TMP_DIR/music/G.A.M.E" "$TMP_DIR/music/Game"
  printf 'The Game' > "$TMP_DIR/music/G.A.M.E/a.mp3"
  printf 'The Game' > "$TMP_DIR/music/Game/b.mp3"

  run env PATH="$TMP_DIR/stubs:$PATH" bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Game/a.mp3" ]
  [ -f "$TMP_DIR/music/Game/b.mp3" ]
  [ ! -e "$TMP_DIR/music/G.A.M.E" ]
}

@test "disputed merge rejected when tags differ and nothing confirms it" {
  make_ffprobe_stub
  mkdir -p "$TMP_DIR/music/Jay-Z" "$TMP_DIR/music/Jay z"
  printf 'Jay-Z' > "$TMP_DIR/music/Jay-Z/a.mp3"
  printf 'Другой Jay z' > "$TMP_DIR/music/Jay z/b.mp3"

  run env PATH="$TMP_DIR/stubs:$PATH" bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Jay-Z/a.mp3" ]
  [ -f "$TMP_DIR/music/Jay z/b.mp3" ]
  [[ "$output" == *"спорное слияние отклонено"* ]]
  grep -q 'keep' "$TMP_DIR/state/split_by_dash_verdicts.tsv"
}

@test "disputed merge without tags and without official data is rejected" {
  mkdir -p "$TMP_DIR/music/K-Maro" "$TMP_DIR/music/Kmaro"
  touch "$TMP_DIR/music/K-Maro/a.mp3" "$TMP_DIR/music/Kmaro/b.mp3"

  run env PATH="$TMP_DIR/stubs:$PATH" bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/K-Maro/a.mp3" ]
  [ -f "$TMP_DIR/music/Kmaro/b.mp3" ]
}

@test "disputed merge confirmed by MusicBrainz uses the artist's official name (without 'The')" {
  make_ffprobe_stub
  mkdir -p "$TMP_DIR/music/G.A.M.E" "$TMP_DIR/music/Game"
  printf 'G.A.M.E.' > "$TMP_DIR/music/G.A.M.E/a.mp3"
  printf 'Taxi Dogg' > "$TMP_DIR/music/Game/b.mp3"
  cat > "$TMP_DIR/stubs/curl" <<'STUB'
#!/usr/bin/env bash
printf '{"artists":[{"score":100,"id":"mbid-1","name":"The Game","aliases":[{"name":"G.A.M.E"},{"name":"Game"},{"name":"Taxi Dogg"}]}]}'
STUB
  chmod +x "$TMP_DIR/stubs/curl"

  run env PATH="$TMP_DIR/stubs:$PATH" OFFICIAL_NAMES=yes bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Game/a.mp3" ]
  [ -f "$TMP_DIR/music/Game/b.mp3" ]
  [ ! -e "$TMP_DIR/music/The Game" ]
  [ ! -e "$TMP_DIR/music/G.A.M.E" ]
}

@test "different artists per MusicBrainz are not merged" {
  make_ffprobe_stub
  mkdir -p "$TMP_DIR/music/G.A.M.E" "$TMP_DIR/music/Game"
  printf 'G.A.M.E.' > "$TMP_DIR/music/G.A.M.E/a.mp3"
  printf 'Gamer X' > "$TMP_DIR/music/Game/b.mp3"
  cat > "$TMP_DIR/stubs/curl" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  *G.A.M.E*) printf '{"artists":[{"score":100,"id":"id-game-dots","name":"G.A.M.E.","aliases":[]}]}' ;;
  *) printf '{"artists":[{"score":100,"id":"id-game","name":"Game","aliases":[]}]}' ;;
esac
STUB
  chmod +x "$TMP_DIR/stubs/curl"

  run env PATH="$TMP_DIR/stubs:$PATH" OFFICIAL_NAMES=yes bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/G.A.M.E/a.mp3" ]
  [ -f "$TMP_DIR/music/Game/b.mp3" ]
}

@test "manual verdict override 'merge' forces a disputed merge" {
  mkdir -p "$TMP_DIR/music/K-Maro" "$TMP_DIR/music/Kmaro" "$TMP_DIR/state"
  touch "$TMP_DIR/music/K-Maro/a.mp3" "$TMP_DIR/music/Kmaro/b.mp3"
  printf 'kmaro\tmerge\tвручную\n' > "$TMP_DIR/state/split_by_dash_verdicts.tsv"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ "$(find "$TMP_DIR/music" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 1 ]
}

@test "DEST_DIR from the config is ignored when a directory is given explicitly" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest" "$TMP_DIR/conf"
  touch "$TMP_DIR/music/Artist - Song.mp3"
  printf 'DEST_DIR=%s\n' "$TMP_DIR/dest" > "$TMP_DIR/conf/split_by_dash.conf"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/Artist - Song.mp3" ]
  [ -z "$(ls -A "$TMP_DIR/dest")" ]
}

@test "config mode processes listed directories and applies DEST_DIR" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/dest" "$TMP_DIR/conf"
  touch "$TMP_DIR/music/Artist - Song.mp3"
  printf '%s\n\nDEST_DIR=%s\n' "$TMP_DIR/music" "$TMP_DIR/dest" > "$TMP_DIR/conf/split_by_dash.conf"

  run bash "$TMP_DIR/split_by_dash.sh"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/dest/Artist/Artist - Song.mp3" ]
}

@test "nonexistent explicit directory is an error, not a fallback to the config" {
  mkdir -p "$TMP_DIR/music" "$TMP_DIR/conf"
  touch "$TMP_DIR/music/Artist - Song.mp3"
  printf '%s\n' "$TMP_DIR/music" > "$TMP_DIR/conf/split_by_dash.conf"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/no-such-dir"

  [ "$status" -eq 2 ]
  [ -f "$TMP_DIR/music/Artist - Song.mp3" ]
}

@test "names from the keep list are not renamed" {
  mkdir -p "$TMP_DIR/music/Artist" "$TMP_DIR/conf"
  touch "$TMP_DIR/music/Artist/VashKevich&Olisha - Song.mp3" "$TMP_DIR/music/Artist/Other&Name.mp3"
  printf '# исключения\nVashKevich&Olisha - Song.mp3\n' > "$TMP_DIR/conf/split_by_dash.keep.conf"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/VashKevich&Olisha - Song.mp3" ]
  [ -f "$TMP_DIR/music/Artist/Other & Name.mp3" ]
  [ ! -e "$TMP_DIR/music/Artist/Other&Name.mp3" ]
}

@test "without the keep list the same name is renamed" {
  mkdir -p "$TMP_DIR/music/Artist"
  touch "$TMP_DIR/music/Artist/VashKevich&Olisha - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Artist/VashKevich & Olisha - Song.mp3" ]
}

@test "merges co-author folders with and without the conjunction 'и'" {
  mkdir -p "$TMP_DIR/music/Виктор Рыбин и Наталья Сенчукова" "$TMP_DIR/music/Виктор РыбинНаталья Сенчукова"
  touch "$TMP_DIR/music/Виктор Рыбин и Наталья Сенчукова/a.mp3" "$TMP_DIR/music/Виктор РыбинНаталья Сенчукова/b.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Виктор Рыбин и Наталья Сенчукова/a.mp3" ]
  [ -f "$TMP_DIR/music/Виктор Рыбин и Наталья Сенчукова/b.mp3" ]
  [ ! -e "$TMP_DIR/music/Виктор РыбинНаталья Сенчукова" ]
}

@test "'&' variant wins over the 'и' and concatenated variants" {
  mkdir -p "$TMP_DIR/music/Ann и Bob" "$TMP_DIR/music/AnnBob" "$TMP_DIR/music/Ann & Bob"
  touch "$TMP_DIR/music/Ann и Bob/a.mp3" "$TMP_DIR/music/AnnBob/b.mp3" "$TMP_DIR/music/Ann & Bob/c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Ann & Bob/a.mp3" ]
  [ -f "$TMP_DIR/music/Ann & Bob/b.mp3" ]
  [ -f "$TMP_DIR/music/Ann & Bob/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Ann и Bob" ]
}

@test "html-escaped ampersand &amp; becomes a spaced ampersand" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/Tom Boxer &amp; Morena - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Tom Boxer & Morena/Tom Boxer & Morena - Song.mp3" ]
}

@test "leading ampersand in a name is kept without extra space" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/&ME&Black Coffee - Song.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/&ME & Black Coffee/&ME & Black Coffee - Song.mp3" ]
}

@test "folders differing only by &amp; versus & merge without verification" {
  mkdir -p "$TMP_DIR/music/DJ Sandro & Katrin Queen" "$TMP_DIR/music/DJ Sandro &amp; Katrin Queen"
  touch "$TMP_DIR/music/DJ Sandro & Katrin Queen/a.mp3" "$TMP_DIR/music/DJ Sandro &amp; Katrin Queen/b.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/DJ Sandro & Katrin Queen/a.mp3" ]
  [ -f "$TMP_DIR/music/DJ Sandro & Katrin Queen/b.mp3" ]
  [ ! -e "$TMP_DIR/music/DJ Sandro &amp; Katrin Queen" ]
}

@test "pattern 1: Latin diacritics merge, the variant without diacritics is the reference even with fewer files" {
  mkdir -p "$TMP_DIR/music/Beyonce" "$TMP_DIR/music/Beyoncé" "$TMP_DIR/music/Maneskin" "$TMP_DIR/music/Måneskin"
  touch "$TMP_DIR/music/Beyonce/a.mp3" "$TMP_DIR/music/Beyoncé/b.mp3" "$TMP_DIR/music/Beyoncé/c.mp3" "$TMP_DIR/music/Beyoncé/d.mp3"
  touch "$TMP_DIR/music/Maneskin/a.mp3" "$TMP_DIR/music/Måneskin/b.mp3" "$TMP_DIR/music/Måneskin/c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Beyonce/a.mp3" ]
  [ -f "$TMP_DIR/music/Beyonce/b.mp3" ]
  [ -f "$TMP_DIR/music/Beyonce/d.mp3" ]
  [ ! -e "$TMP_DIR/music/Beyoncé" ]
  [ -f "$TMP_DIR/music/Maneskin/a.mp3" ]
  [ -f "$TMP_DIR/music/Maneskin/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Måneskin" ]
}

@test "pattern 1: ё has priority over е even with fewer files" {
  mkdir -p "$TMP_DIR/music/Серега" "$TMP_DIR/music/Серёга" "$TMP_DIR/music/Ёлка" "$TMP_DIR/music/Елка"
  touch "$TMP_DIR/music/Серега/a.mp3" "$TMP_DIR/music/Серега/b.mp3" "$TMP_DIR/music/Серёга/c.mp3"
  touch "$TMP_DIR/music/Елка/a.mp3" "$TMP_DIR/music/Елка/b.mp3" "$TMP_DIR/music/Ёлка/c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Серёга/a.mp3" ]
  [ -f "$TMP_DIR/music/Серёга/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Серега" ]
  [ -f "$TMP_DIR/music/Ёлка/a.mp3" ]
  [ -f "$TMP_DIR/music/Ёлка/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Елка" ]
}

@test "pattern 1: й is not folded to и, unrelated names stay separate" {
  mkdir -p "$TMP_DIR/music/Дүйсен" "$TMP_DIR/music/Дүисен" "$TMP_DIR/music/Maneskin Duo" "$TMP_DIR/music/Maneskin"
  touch "$TMP_DIR/music/Дүйсен/a.mp3" "$TMP_DIR/music/Дүисен/b.mp3" "$TMP_DIR/music/Maneskin Duo/c.mp3" "$TMP_DIR/music/Maneskin/d.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Дүйсен/a.mp3" ]
  [ -f "$TMP_DIR/music/Дүисен/b.mp3" ]
  [ -f "$TMP_DIR/music/Maneskin Duo/c.mp3" ]
  [ -f "$TMP_DIR/music/Maneskin/d.mp3" ]
}

@test "pattern 2: Kazakh letters merge with Russian spelling, the Kazakh variant wins regardless of file count" {
  mkdir -p "$TMP_DIR/music/Кайрат Нуртас" "$TMP_DIR/music/Қайрат Нұртас"
  touch "$TMP_DIR/music/Кайрат Нуртас/a.mp3" "$TMP_DIR/music/Кайрат Нуртас/b.mp3" "$TMP_DIR/music/Қайрат Нұртас/c.mp3"
  mkdir -p "$TMP_DIR/music/Кенжебек Жанабилов" "$TMP_DIR/music/Кенжебек Жанабілов" "$TMP_DIR/music/Кенжебек Жанәбілов"
  touch "$TMP_DIR/music/Кенжебек Жанабилов/a.mp3" "$TMP_DIR/music/Кенжебек Жанабілов/b.mp3" "$TMP_DIR/music/Кенжебек Жанәбілов/c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Қайрат Нұртас/a.mp3" ]
  [ -f "$TMP_DIR/music/Қайрат Нұртас/b.mp3" ]
  [ -f "$TMP_DIR/music/Қайрат Нұртас/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Кайрат Нуртас" ]
  [ "$(find "$TMP_DIR/music" -mindepth 1 -maxdepth 1 -type d -name 'Кенжебек*' | wc -l)" -eq 1 ]
}

@test "pattern 2: Kazakh letters do not make 'г' and 'ғ' folders with different letters collide" {
  mkdir -p "$TMP_DIR/music/Қанат" "$TMP_DIR/music/Қарат"
  touch "$TMP_DIR/music/Қанат/a.mp3" "$TMP_DIR/music/Қарат/b.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Қанат/a.mp3" ]
  [ -f "$TMP_DIR/music/Қарат/b.mp3" ]
}

@test "leading 'The' is removed from artist folder names and merged with the plain variant" {
  mkdir -p "$TMP_DIR/music/The Rasmus" "$TMP_DIR/music/Rasmus" "$TMP_DIR/music/The Police" "$TMP_DIR/music/The wanted" "$TMP_DIR/music/Wanted"
  touch "$TMP_DIR/music/The Rasmus/a.mp3" "$TMP_DIR/music/The Rasmus/b.mp3" "$TMP_DIR/music/Rasmus/c.mp3"
  touch "$TMP_DIR/music/The Police/p.mp3" "$TMP_DIR/music/The wanted/w1.mp3" "$TMP_DIR/music/Wanted/w2.mp3" "$TMP_DIR/music/Wanted/w3.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Rasmus/a.mp3" ]
  [ -f "$TMP_DIR/music/Rasmus/b.mp3" ]
  [ -f "$TMP_DIR/music/Rasmus/c.mp3" ]
  [ ! -e "$TMP_DIR/music/The Rasmus" ]
  [ -f "$TMP_DIR/music/Police/p.mp3" ]
  [ ! -e "$TMP_DIR/music/The Police" ]
  [ -f "$TMP_DIR/music/Wanted/w1.mp3" ]
  [ -f "$TMP_DIR/music/Wanted/w2.mp3" ]
  [ ! -e "$TMP_DIR/music/The wanted" ]
}

@test "file 'The Band - Song.mp3' goes into folder 'Band' and keeps its file name" {
  mkdir -p "$TMP_DIR/music"
  touch "$TMP_DIR/music/The Police - Roxanne.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Police/The Police - Roxanne.mp3" ]
  [ ! -e "$TMP_DIR/music/The Police" ]
}

@test "a folder named just 'The' and names with 'the' inside are not changed" {
  mkdir -p "$TMP_DIR/music/The" "$TMP_DIR/music/Theory of a Deadman" "$TMP_DIR/music/Eminem & The Weeknd"
  touch "$TMP_DIR/music/The/a.mp3" "$TMP_DIR/music/Theory of a Deadman/b.mp3" "$TMP_DIR/music/Eminem & The Weeknd/c.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/The/a.mp3" ]
  [ -f "$TMP_DIR/music/Theory of a Deadman/b.mp3" ]
  [ -f "$TMP_DIR/music/Eminem & The Weeknd/c.mp3" ]
}

@test "official name 'The Game' is used without the article" {
  mkdir -p "$TMP_DIR/music/G.A.M.E" "$TMP_DIR/music/Game" "$TMP_DIR/stubs"
  printf 'G.A.M.E.' > "$TMP_DIR/music/G.A.M.E/a.mp3"
  printf 'The Game' > "$TMP_DIR/music/Game/b.mp3"
  printf '#!/usr/bin/env bash\ncat "${!#}"\n' > "$TMP_DIR/stubs/ffprobe"
  cat > "$TMP_DIR/stubs/curl" <<'STUB'
#!/usr/bin/env bash
printf '{"artists":[{"score":100,"id":"mbid-1","name":"The Game","aliases":[{"name":"G.A.M.E"},{"name":"Game"}]}]}'
STUB
  chmod +x "$TMP_DIR/stubs/ffprobe" "$TMP_DIR/stubs/curl"

  run env PATH="$TMP_DIR/stubs:$PATH" OFFICIAL_NAMES=yes bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Game/a.mp3" ]
  [ -f "$TMP_DIR/music/Game/b.mp3" ]
  [ ! -e "$TMP_DIR/music/The Game" ]
  [ ! -e "$TMP_DIR/music/G.A.M.E" ]
}

@test "pattern 5: service words are ignored in the key and the variant WITH the service word wins regardless of file count" {
  mkdir -p "$TMP_DIR/music/Жігіттер" "$TMP_DIR/music/Жігіттер тобы" "$TMP_DIR/music/Группа губы" "$TMP_DIR/music/Губы"
  touch "$TMP_DIR/music/Жігіттер/a.mp3" "$TMP_DIR/music/Жігіттер/b.mp3" "$TMP_DIR/music/Жігіттер тобы/c.mp3"
  touch "$TMP_DIR/music/Губы/d.mp3" "$TMP_DIR/music/Губы/e.mp3" "$TMP_DIR/music/Губы/f.mp3" "$TMP_DIR/music/Группа губы/g.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Жігіттер тобы/a.mp3" ]
  [ -f "$TMP_DIR/music/Жігіттер тобы/b.mp3" ]
  [ -f "$TMP_DIR/music/Жігіттер тобы/c.mp3" ]
  [ ! -e "$TMP_DIR/music/Жігіттер" ]
  [ -f "$TMP_DIR/music/Группа губы/d.mp3" ]
  [ -f "$TMP_DIR/music/Группа губы/f.mp3" ]
  [ -f "$TMP_DIR/music/Группа губы/g.mp3" ]
  [ ! -e "$TMP_DIR/music/Губы" ]
}

@test "pattern 5: a name made only of service words is not emptied, words inside other words are untouched" {
  mkdir -p "$TMP_DIR/music/Band" "$TMP_DIR/music/Bandana" "$TMP_DIR/music/Группировка" "$TMP_DIR/music/Official"
  touch "$TMP_DIR/music/Band/a.mp3" "$TMP_DIR/music/Bandana/b.mp3" "$TMP_DIR/music/Группировка/c.mp3" "$TMP_DIR/music/Official/d.mp3"

  run bash "$TMP_DIR/split_by_dash.sh" "$TMP_DIR/music"

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/music/Band/a.mp3" ]
  [ -f "$TMP_DIR/music/Bandana/b.mp3" ]
  [ -f "$TMP_DIR/music/Группировка/c.mp3" ]
  [ -f "$TMP_DIR/music/Official/d.mp3" ]
}
