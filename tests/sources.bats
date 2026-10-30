#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"

  mkdir -p "$STUB_DIR" "$TMP_DIR/src" "$TMP_DIR/dst" "$TMP_DIR/conf"
  cp "$REPO_ROOT/sources.sh" "$TMP_DIR/sources.sh"
  chmod +x "$TMP_DIR/sources.sh"

  printf 'example\n' > "$TMP_DIR/src/file1.txt"

  cat > "$TMP_DIR/conf/custom.conf" <<EOF
$TMP_DIR/src/ | $TMP_DIR/dst/ | -a
EOF

  cat > "$STUB_DIR/rsync" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$TMP_DIR/rsync_args.txt"
printf '>f+++++++++\tfile1.txt\n'
exit 0
EOF
  chmod +x "$STUB_DIR/rsync"
}

teardown() {
  rm -rf "$TMP_DIR"
}

@test "prints usage with -h" {
  run sh "$TMP_DIR/sources.sh" -h

  [ "$status" -eq 0 ]
  [[ "$output" == *"Использование"* ]]
}

@test "dry-run logs would_write and forwards --dry-run to rsync" {
  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/custom.conf" -n

  [ "$status" -eq 0 ]
  run grep -q -- "--dry-run" "$TMP_DIR/rsync_args.txt"
  [ "$status" -eq 0 ]

  run find "$TMP_DIR/logs" -maxdepth 1 -type f -name 'sources_*-report.jsonl'
  [ "$status" -eq 0 ]
  [ -n "$output" ]

  log_file="$output"
  run grep -q '"event":"would_write"' "$log_file"
  [ "$status" -eq 0 ]
}

# --- архивация в 7z ---------------------------------------------------------

# Заглушка 7z: пишет аргументы и создаёт файл-архив (предпоследний аргумент).
make_7z_stub() {
  cat > "$STUB_DIR/7z" <<EOF2
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$TMP_DIR/7z_args.txt"
args=("\$@")
printf 'archive' > "\${args[\$((\${#args[@]} - 2))]}"
exit \${STUB_7Z_RC:-0}
EOF2
  chmod +x "$STUB_DIR/7z"
}

@test "7z: dry-run ничего не создаёт и не удаляет, план ротации учитывает новый архив" {
  make_7z_stub
  mkdir -p "$TMP_DIR/arch"
  touch -d '2026-01-01' "$TMP_DIR/arch/src-2026-01-01.7z"
  touch -d '2026-01-02' "$TMP_DIR/arch/src-2026-01-02.7z"
  printf '7z | %s/src/ | %s/arch/ | 2 | -t7z\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf" -n

  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: 7z a -t7z"* ]]
  [[ "$output" == *"удалить старый архив $TMP_DIR/arch/src-2026-01-01.7z"* ]]
  [[ "$output" != *"src-2026-01-02.7z"* ]]
  [ ! -e "$TMP_DIR/7z_args.txt" ]
  [ -e "$TMP_DIR/arch/src-2026-01-01.7z" ]
  [ "$(find "$TMP_DIR/arch" -type f | wc -l)" -eq 2 ]
  grep -q '"event":"would_archive"' "$TMP_DIR"/logs/sources_*-report.jsonl
  grep -q '"event":"would_delete"' "$TMP_DIR"/logs/sources_*-report.jsonl
}

@test "7z: создаёт архив с датой, удаляет лишние старые, .part не остаётся" {
  make_7z_stub
  mkdir -p "$TMP_DIR/arch"
  touch -d '2026-01-01' "$TMP_DIR/arch/src-2026-01-01.7z"
  touch -d '2026-01-02' "$TMP_DIR/arch/src-2026-01-02.7z"
  touch -d '2026-01-03' "$TMP_DIR/arch/other-2026-01-01.7z"
  printf '7z | %s/src/ | %s/arch/ | 2 | -t7z -xr!.cache\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf"

  [ "$status" -eq 0 ]
  today="$(date '+%Y-%m-%d')"
  [ -s "$TMP_DIR/arch/src-$today.7z" ]
  [ -e "$TMP_DIR/arch/src-2026-01-02.7z" ]
  [ ! -e "$TMP_DIR/arch/src-2026-01-01.7z" ]
  [ -e "$TMP_DIR/arch/other-2026-01-01.7z" ]
  [ -z "$(find "$TMP_DIR/arch" -name '*.part')" ]
  grep -q -- '-xr!.cache' "$TMP_DIR/7z_args.txt"
  grep -q '"event":"archived"' "$TMP_DIR"/logs/sources_*-report.jsonl
}

@test "7z: пустые опции — значения по умолчанию, СКОЛЬКО_ХРАНИТЬ=0 не удаляет" {
  make_7z_stub
  mkdir -p "$TMP_DIR/arch"
  touch -d '2026-01-01' "$TMP_DIR/arch/src-2026-01-01.7z"
  printf '7z | %s/src/ | %s/arch/ | 0\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf"

  [ "$status" -eq 0 ]
  grep -q -- '-t7z -mx=5 -snl' "$TMP_DIR/7z_args.txt"
  [ -e "$TMP_DIR/arch/src-2026-01-01.7z" ]
}

@test "7z: код 2 — ошибка, архив не появляется, старые целы" {
  make_7z_stub
  mkdir -p "$TMP_DIR/arch"
  touch -d '2026-01-01' "$TMP_DIR/arch/src-2026-01-01.7z"
  printf '7z | %s/src/ | %s/arch/ | 1 | -t7z\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" STUB_7Z_RC=2 sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf"

  [ "$status" -eq 2 ]
  [ -e "$TMP_DIR/arch/src-2026-01-01.7z" ]
  [ "$(find "$TMP_DIR/arch" -type f | wc -l)" -eq 1 ]
}

@test "7z: код 1 (файл изменился при чтении) — предупреждение, архив сохраняется" {
  make_7z_stub
  printf '7z | %s/src/ | %s/arch/ | 1 | -t7z\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" STUB_7Z_RC=1 sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf"

  [ "$status" -eq 0 ]
  [ -s "$TMP_DIR/arch/src-$(date '+%Y-%m-%d').7z" ]
}

@test "7z: неверное число хранимых архивов — config_error, код 2" {
  make_7z_stub
  printf '7z | %s/src/ | %s/arch/ | много | -t7z\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf"

  [ "$status" -eq 2 ]
  [ ! -e "$TMP_DIR/7z_args.txt" ]
}

@test "7z: пустое СКОЛЬКО_ХРАНИТЬ — хранится SEVENZIP_KEEP (по умолчанию 3)" {
  make_7z_stub
  mkdir -p "$TMP_DIR/arch"
  for d in 01 02 03 04 05; do
    touch -d "2026-01-$d" "$TMP_DIR/arch/src-2026-01-$d.7z"
  done
  printf '7z | %s/src/ | %s/arch/ | | -t7z\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/7z.conf"

  run env PATH="$STUB_DIR:$PATH" sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/7z.conf"

  [ "$status" -eq 0 ]
  # 5 старых + новый = 6, остаётся 3 последних: новый, 05 и 04
  [ "$(find "$TMP_DIR/arch" -type f -name '*.7z' | wc -l)" -eq 3 ]
  [ ! -e "$TMP_DIR/arch/src-2026-01-03.7z" ]
  [ -e "$TMP_DIR/arch/src-2026-01-04.7z" ]
  [ -e "$TMP_DIR/arch/src-$(date '+%Y-%m-%d').7z" ]
}

# --- лог: удаления и экранирование -----------------------------------------

@test "лог: удаления настоящего rsync — верный путь и валидный JSON в каждой строке" {
  command -v rsync >/dev/null || skip "нет rsync"
  mkdir -p "$TMP_DIR/dst/sub"
  printf 'x\n' > "$TMP_DIR/dst/old file.txt"
  printf 'y\n' > "$TMP_DIR/dst/sub/z"
  printf 'q\n' > "$TMP_DIR/src/таб	\"кав\" \\.txt"
  printf '%s/src/ | %s/dst/ | -a --delete\n' "$TMP_DIR" "$TMP_DIR" > "$TMP_DIR/conf/del.conf"

  run sh "$TMP_DIR/sources.sh" -c "$TMP_DIR/conf/del.conf"

  [ "$status" -eq 0 ]
  log_file="$(find "$TMP_DIR/logs" -name 'sources_*-report.jsonl')"
  run python3 - "$log_file" "$TMP_DIR" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
tmp = sys.argv[2]
deleted = {r["dst_file"] for r in rows if r["event"] == "deleted"}
written = {r["dst_file"] for r in rows if r["event"] == "written"}
assert deleted == {f"{tmp}/dst/old file.txt", f"{tmp}/dst/sub/z"}, deleted
assert f'{tmp}/dst/таб\t"кав" \\.txt' in written, written
PY
  [ "$status" -eq 0 ]
}
