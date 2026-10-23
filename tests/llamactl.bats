#!/usr/bin/env bats

# Тесты llamactl.sh и llamactl_tray.py: роутер, systemctl, nvidia-smi, git и
# cmake подменяются заглушками; пресеты и модели — во временном каталоге,
# SUDO_CMD пустой (root-шаги выполняются напрямую).

setup() {
  export MYTASKS_LOG_DIR="$BATS_TEST_TMPDIR/logs"
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  WORK_DIR="$TMP_DIR/work"
  STUB_DIR="$TMP_DIR/stubs"
  MODELS="$TMP_DIR/models"
  PRESET="$TMP_DIR/models.ini"
  CALLS="$TMP_DIR/calls.log"
  STATE="$TMP_DIR/model_state"
  mkdir -p "$WORK_DIR/conf" "$STUB_DIR" "$MODELS/m9" "$TMP_DIR/prefix/bin" "$TMP_DIR/src"
  cp "$REPO_ROOT/llamactl.sh" "$REPO_ROOT/llamactl_tray.py" "$REPO_ROOT/llamactl_status.py" "$WORK_DIR/"
  chmod +x "$WORK_DIR/llamactl.sh" "$WORK_DIR/llamactl_tray.py"
  : > "$CALLS"
  echo unloaded > "$STATE"
  : > "$MODELS/m9/m9.gguf"
  : > "$MODELS/m9/mmproj.gguf"

  cat > "$PRESET" <<EOF
version = 1

[*]
ctx-size = 32768
cache-type-k = q8_0
cache-type-v = q8_0
fit-target = 512

[m9]
model = $MODELS/m9/m9.gguf
mmproj = $MODELS/m9/mmproj.gguf
temp = 0.6
EOF

  cat > "$WORK_DIR/conf/llamactl.conf" <<EOF
ROUTER_URL="http://router.test"
FIM_URL="http://fim.test"
PRESET_FILE="$PRESET"
MODELS_DIR="$MODELS"
LLAMA_PREFIX="$TMP_DIR/prefix"
LLAMA_SRC="$TMP_DIR/src"
SUDO_CMD=""
HEALTH_TIMEOUT_SECS="4"
LOAD_TIMEOUT_SECS="6"
EOF

  # Заглушка curl: роутер, FIM и Hugging Face
  cat > "$STUB_DIR/curl" <<EOF
#!/usr/bin/env bash
echo "curl \$*" >> "$CALLS"
url="" data=""
while (( \$# > 0 )); do
  case "\$1" in
    -d) shift; data="\$1" ;;
    http*) url="\$1" ;;
    *) ;;
  esac
  shift
done
case "\$url" in
  */health) [ -e "$TMP_DIR/down" ] && exit 7; exit 0 ;;
  */models/load) echo loaded > "$STATE"; echo '{"success":true}' ;;
  */models/unload) echo unloaded > "$STATE"; echo '{"success":true}' ;;
  */models|*/models\?reload=1)
    st="\$(cat "$STATE")"
    printf '{"data":[{"id":"m9","status":{"value":"%s","args":["llama-server","--ctx-size","32768"]}}]}\n' "\$st" ;;
  */v1/chat/completions) echo '{"timings":{"predicted_per_second":42.42,"prompt_per_second":900.4}}' ;;
  */api/models/*) echo '[{"path":"x.gguf","size":1000,"lfs":{"oid":"abc"}}]' ;;
  *) exit 22 ;;
esac
EOF

  cat > "$STUB_DIR/systemctl" <<EOF
#!/usr/bin/env bash
echo "systemctl \$*" >> "$CALLS"
[ "\$1" = "is-active" ] && { echo active; exit 0; }
exit 0
EOF

  cat > "$STUB_DIR/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *query-gpu*) echo "1300, 8192, 5" ;;
  *query-compute-apps*) [ -n "${GPU_APPS:-}" ] && printf '%s\n' "$GPU_APPS" ;;
  *) ;;
esac
EOF

  cat > "$STUB_DIR/journalctl" <<EOF
#!/usr/bin/env bash
echo "journalctl \$*" >> "$CALLS"
echo "лог-строка"
EOF

  local real_git
  real_git="$(command -v git)"
  cat > "$STUB_DIR/git" <<EOF
#!/usr/bin/env bash
echo "git \$*" >> "$CALLS"
case "\$*" in
  *ls-remote*) printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tHEAD\n' ;;
  *fetch*|*checkout*) exit 0 ;;
  *) exec "$real_git" "\$@" ;;
esac
EOF

  cat > "$STUB_DIR/cmake" <<EOF
#!/usr/bin/env bash
echo "cmake \$*" >> "$CALLS"
EOF

  cat > "$TMP_DIR/prefix/bin/llama-server" <<'EOF'
#!/usr/bin/env bash
echo "version: 1 (build 1, commit aaaaaaa)"
EOF

  cat > "$TMP_DIR/prefix/bin/llama-fit-params" <<'EOF'
#!/usr/bin/env bash
echo '-c 32768 -ngl -1'
EOF

  chmod +x "$STUB_DIR"/* "$TMP_DIR/prefix/bin/"*
  git -C "$TMP_DIR/src" init -q
}

teardown() {
  [ -n "${FOREIGN_PID:-}" ] && kill "$FOREIGN_PID" 2>/dev/null || true
  [ -n "${HTTP_PID:-}" ] && kill "$HTTP_PID" 2>/dev/null || true
  rm -rf "$TMP_DIR"
}

# NVML отключён: статус берёт VRAM из заглушки nvidia-smi, а не с реальной GPU
ctl() {
  env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 "$WORK_DIR/llamactl.sh" "$@"
}

# Локальный HTTP-«роутер» для статуса (llamactl_status.py ходит по HTTP, не через curl):
# /health и /models — файлы каталога www; адрес дописывается в conf/llamactl.conf
start_router() {
  mkdir -p "$TMP_DIR/www"
  echo ok > "$TMP_DIR/www/health"
  printf '{"data":[{"id":"m9","status":{"value":"%s","args":["llama-server","--ctx-size","32768"]}}]}\n' "${1:-loaded}" > "$TMP_DIR/www/models"
  PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$TMP_DIR/www" >/dev/null 2>&1 &
  HTTP_PID=$!
  python3 - "$PORT" <<'PYEOF'
import sys, time, urllib.request
for _ in range(100):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=1)
        break
    except OSError:
        time.sleep(0.05)
PYEOF
  printf 'ROUTER_URL="http://127.0.0.1:%s"\nFIM_URL="http://127.0.0.1:%s"\n' "$PORT" "$PORT" >> "$WORK_DIR/conf/llamactl.conf"
}

@test "help: печатает справку и завершается с кодом 0" {
  run ctl --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Использование:"* ]]
}

@test "неизвестная команда: код 2" {
  run ctl nonsense
  [ "$status" -eq 2 ]
}

@test "status --json: корректный JSON с моделями, сервисами и VRAM" {
  start_router loaded
  run ctl status --json
  [ "$status" -eq 0 ]
  run jq -r '[.services.server, (.router.up|tostring), .models[0].id, .models[0].ctx, (.gpu.used_mib|tostring)] | join(" ")' <<<"$output"
  [ "$output" = "active true m9 32768 1300" ]
}

@test "status --json: роутер недоступен — up=false и пустой список моделей" {
  printf 'ROUTER_URL="http://127.0.0.1:9"\nFIM_URL="http://127.0.0.1:9"\n' >> "$WORK_DIR/conf/llamactl.conf"
  run ctl status --json
  [ "$status" -eq 0 ]
  run jq -r '[(.router.up|tostring), (.models|length|tostring)] | join(" ")' <<<"$output"
  [ "$output" = "false 0" ]
}

@test "llamactl_status.py: /proc, meminfo, GPU-процессы и сторонний llama-server" {
  P="$TMP_DIR/proc"
  mkdir -p "$P/4242" "$P/77"
  printf 'MemTotal:       1000 kB\nMemAvailable:    400 kB\nSwapTotal:       100 kB\nSwapFree:         40 kB\n' > "$P/meminfo"
  printf '/opt/x/llama-server\0--port\08099\0' > "$P/4242/cmdline"
  printf 'bash\0' > "$P/77/cmdline"
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 GPU_APPS="4242, 512" python3 "$WORK_DIR/llamactl_status.py" \
    --proc "$P" --router-url http://127.0.0.1:9 --fim-url http://127.0.0.1:9 --models-dir "$MODELS"
  [ "$status" -eq 0 ]
  run jq -r '[.ram.total, .ram.used, .ram.swap_used, .foreign[0].pid, .foreign[0].port, (.foreign|length),
             .gpu.procs[0].kind, .gpu.procs[0].mib, .gpu.total_mib] | map(tostring) | join(" ")' <<<"$output"
  [ "$output" = "1024000 614400 61440 4242 8099 1 foreign 512 8192" ]
}

@test "llamactl_status.py --dry-run: принимается и печатает статус" {
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 python3 "$WORK_DIR/llamactl_status.py" --dry-run \
    --router-url http://127.0.0.1:9 --fim-url http://127.0.0.1:9
  [ "$status" -eq 0 ]
  jq -e '.services and .gpu and .ram' <<<"$output"
}

@test "status: текстовый вывод перечисляет модели" {
  start_router loaded
  run ctl status
  [ "$status" -eq 0 ]
  [[ "$output" == *"m9"* ]]
  [[ "$output" == *"1300/8192"* ]]
}

@test "load --dry-run: план без запроса к роутеру" {
  run ctl --dry-run load m9
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: загрузить модель m9"* ]]
  ! grep -q 'models/load' "$CALLS"
  [ "$(cat "$STATE")" = "unloaded" ]
}

@test "load и unload: модель загружается и выгружается" {
  run ctl load m9
  [ "$status" -eq 0 ]
  [[ "$output" == *"m9: загружена"* ]]
  [ "$(cat "$STATE")" = "loaded" ]
  run ctl unload all
  [ "$status" -eq 0 ]
  [[ "$output" == *"m9: выгружена"* ]]
  [ "$(cat "$STATE")" = "unloaded" ]
}

@test "load: неизвестная модель — код 2" {
  run ctl load nope
  [ "$status" -eq 2 ]
}

@test "preset set --dry-run: показывает diff, файл не меняется" {
  before="$(sha256sum "$PRESET")"
  run ctl --dry-run preset set m9 ctx-size 65536
  [ "$status" -eq 0 ]
  [[ "$output" == *"+ctx-size = 65536"* ]]
  [[ "$output" == *"[dry-run] будет: установить"* ]]
  [ "$(sha256sum "$PRESET")" = "$before" ]
  ! ls "$PRESET".bak-* >/dev/null 2>&1
}

@test "preset set/unset: ключ добавляется и удаляется, есть резервная копия и reload" {
  run ctl preset set m9 ctx-size 65536
  [ "$status" -eq 0 ]
  run ctl preset show m9
  [[ "$output" == *"ctx-size = 65536"* ]]
  ls "$PRESET".bak-* >/dev/null
  grep -q 'models?reload=1' "$CALLS"
  run ctl preset set m9 temp 0.7
  [ "$status" -eq 0 ]
  run grep -c '^temp = 0.7$' "$PRESET"
  [ "$output" = "1" ]
  run ctl preset unset m9 temp
  [ "$status" -eq 0 ]
  ! grep -q '^temp' "$PRESET"
  grep -q '^\[\*\]' "$PRESET"
}

@test "preset set: недопустимый ключ — код 2" {
  run ctl preset set m9 'bad key' 1
  [ "$status" -eq 2 ]
}

@test "remove --dry-run --delete-files: ничего не удаляет" {
  before="$(sha256sum "$PRESET")"
  run ctl --dry-run remove m9 --delete-files
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: удалить $MODELS/m9/m9.gguf"* ]]
  [ "$(sha256sum "$PRESET")" = "$before" ]
  [ -f "$MODELS/m9/m9.gguf" ]
}

@test "remove --delete-files: секция и файлы модели удалены" {
  run ctl remove m9 --delete-files
  [ "$status" -eq 0 ]
  ! grep -q '^\[m9\]' "$PRESET"
  [ ! -e "$MODELS/m9" ]
}

@test "free-vram --dry-run: FIM не останавливается" {
  run ctl --dry-run free-vram
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: остановить llama-fim"* ]]
  ! grep -q 'systemctl stop' "$CALLS"
}

@test "restart --dry-run: systemctl restart не вызывается" {
  run ctl --dry-run restart all
  [ "$status" -eq 0 ]
  ! grep -q 'systemctl restart' "$CALLS"
}

@test "restart: перезапуск и ожидание ответа сервисов" {
  run ctl restart server
  [ "$status" -eq 0 ]
  grep -q 'systemctl restart llama-server' "$CALLS"
  [[ "$output" == *"роутер: отвечает"* ]]
}

@test "logs: читает journalctl нужного юнита" {
  run ctl logs fim -n 5
  [ "$status" -eq 0 ]
  grep -q 'journalctl -u llama-fim -n 5' "$CALLS"
}

@test "doctor: отсутствующий файл модели — код 1" {
  rm -f "$MODELS/m9/mmproj.gguf"
  run ctl doctor
  [ "$status" -eq 1 ]
  [[ "$output" == *"нет файла модели: $MODELS/m9/mmproj.gguf"* ]]
}

@test "doctor: роутер не отвечает — код 1" {
  touch "$TMP_DIR/down"
  run ctl doctor
  [ "$status" -eq 1 ]
  [[ "$output" == *"роутер не отвечает"* ]]
}

@test "fit: все слои на GPU" {
  run ctl fit m9
  [ "$status" -eq 0 ]
  [[ "$output" == *"все слои на GPU"* ]]
}

@test "bench: печатает скорость генерации" {
  run ctl bench m9
  [ "$status" -eq 0 ]
  [[ "$output" == *"генерация 42.4 ток/с"* ]]
}

@test "add --dry-run: план загрузки и diff пресета без изменений" {
  before="$(sha256sum "$PRESET")"
  run ctl --dry-run add user/repo x.gguf --name newm --ctx 16384
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: скачать user/repo/x.gguf"* ]]
  [[ "$output" == *"+[newm]"* ]]
  [[ "$output" == *"+ctx-size = 16384"* ]]
  [ "$(sha256sum "$PRESET")" = "$before" ]
  [ ! -e "$MODELS/newm" ]
}

@test "add: существующее имя модели — код 2" {
  run ctl add user/repo x.gguf --name m9
  [ "$status" -eq 2 ]
}

@test "kill-foreign: dry-run не трогает, боевой режим завершает сторонний llama-server" {
  cp "$(command -v sleep)" "$TMP_DIR/llama-server"
  "$TMP_DIR/llama-server" 300 &
  FOREIGN_PID=$!
  run ctl --dry-run kill-foreign
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: завершить pid $FOREIGN_PID"* ]]
  kill -0 "$FOREIGN_PID"
  run ctl status --json
  run jq -r '.foreign[0].pid' <<<"$output"
  [ "$output" = "$FOREIGN_PID" ]
  run ctl kill-foreign
  [ "$status" -eq 0 ]
  sleep 1
  ! kill -0 "$FOREIGN_PID" 2>/dev/null
}

@test "update --dry-run: план без fetch и сборки" {
  run ctl --dry-run update
  [ "$status" -eq 0 ]
  [[ "$output" == *"Установлено: aaaaaaa; в origin HEAD: bbbbbbb"* ]]
  [[ "$output" == *"[dry-run] будет: установить в $TMP_DIR/prefix.new"* ]]
  ! grep -q 'git .*fetch' "$CALLS"
  ! grep -q '^cmake' "$CALLS"
}

@test "update: актуальная версия — ничего не делает" {
  cat > "$TMP_DIR/prefix/bin/llama-server" <<'EOF'
#!/usr/bin/env bash
echo "version: 1 (build 1, commit bbbbbbb)"
EOF
  run ctl update
  [ "$status" -eq 0 ]
  [[ "$output" == *"уже актуален"* ]]
  ! grep -q '^cmake' "$CALLS"
}

@test "трей --print-menu: состояние и пункты по статусу" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  cat > "$TMP_DIR/status.json" <<'EOF'
{"services":{"server":"active","fim":"active"},"router":{"up":true,"url":"http://router.test"},"fim":{"up":true},
 "models":[{"id":"m9","status":"loaded","ctx":"32768"},{"id":"big","status":"unloaded","ctx":"131072"}],
 "gpu":{"used_mib":6000,"total_mib":8192,"util":40,"procs":[]},"ram":{"total":1,"used":1,"available":1,"swap_used":0},
 "disk":{"models_avail":1},"foreign":[]}
EOF
  run /usr/bin/python3 "$WORK_DIR/llamactl_tray.py" --print-menu --status-file "$TMP_DIR/status.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: busy"* ]]
  [[ "$output" == *"[x] m9 — 32k [loaded]  <bg: llamactl unload m9>"* ]]
  [[ "$output" == *"[ ] big — 128k [unloaded]  <bg: llamactl load big>"* ]]
}

@test "трей --print-menu: сторонний llama-server даёт состояние warn и пункт завершения" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  cat > "$TMP_DIR/status.json" <<'EOF'
{"services":{"server":"active","fim":"active"},"router":{"up":true},"fim":{"up":true},"models":[],
 "gpu":{"used_mib":1,"total_mib":8192,"procs":[]},"ram":{},"disk":{},"foreign":[{"pid":1,"user":"u","port":"8009","cmd":"llama serve"}]}
EOF
  run /usr/bin/python3 "$WORK_DIR/llamactl_tray.py" --print-menu --dry-run --status-file "$TMP_DIR/status.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: warn"* ]]
  [[ "$output" == *"Завершить сторонние llama-server (1)"* ]]
  [[ "$output" == *"DRY-RUN"* ]]
}

@test "трей --print-menu без файла: статус модулем llamactl_status (без llamactl.sh)" {
  start_router loaded
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 /usr/bin/python3 "$WORK_DIR/llamactl_tray.py" --print-menu
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: "* ]]
  [[ "$output" == *"m9"* ]]
  ! grep -q 'status' "$CALLS"
}

@test "трей --print-menu: нет статуса — состояние down" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  echo 'не json' > "$TMP_DIR/status.json"
  run /usr/bin/python3 "$WORK_DIR/llamactl_tray.py" --print-menu --status-file "$TMP_DIR/status.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: down"* ]]
}
