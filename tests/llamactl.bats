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
  mkdir -p "$WORK_DIR/conf" "$WORK_DIR/llamactl" "$STUB_DIR" "$MODELS/m9" "$TMP_DIR/prefix/bin" "$TMP_DIR/src"
  cp "$REPO_ROOT/llamactl/llamactl.sh" "$REPO_ROOT/llamactl/llamactl_tray.py" "$REPO_ROOT/llamactl/llamactl_status.py" "$REPO_ROOT/llamactl/logcheck_prompt.txt" "$WORK_DIR/llamactl/"
  chmod +x "$WORK_DIR/llamactl/llamactl.sh" "$WORK_DIR/llamactl/llamactl_tray.py"
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
PRESET_LOCK_FILE="$TMP_DIR/models.ini.lock"
LOGCHECK_STATE_DIR="$TMP_DIR/lc"
LOGCHECK_POLL_SECS="1"
LOGCHECK_WAIT_SECS="2"
LOGCHECK_SSH_HOST="test@host"
LOGCHECK_MODEL="m9"
EOF

  # Заглушка curl: роутер, FIM и Hugging Face. Загрузка (-o) пишет GGUF_DATA;
  # если есть файл edit_during_download — во время «загрузки» models.ini
  # меняет посторонний писатель (как параллельный llamactl preset set).
  cat > "$STUB_DIR/curl" <<EOF
#!/usr/bin/env bash
echo "curl \$*" >> "$CALLS"
url="" data="" out="" databin=""
while (( \$# > 0 )); do
  case "\$1" in
    -d) shift; data="\$1" ;;
    --data-binary) shift; databin="\$1" ;;
    -o) shift; out="\$1" ;;
    http*) url="\$1" ;;
    *) ;;
  esac
  shift
done
oid="abc"
[ -f "$TMP_DIR/oid" ] && oid="\$(cat "$TMP_DIR/oid")"
case "\$url" in
  */health) [ -e "$TMP_DIR/down" ] && exit 7; exit 0 ;;
  */models/load) echo loaded > "$STATE"; echo '{"success":true}' ;;
  */models/unload) echo unloaded > "$STATE"; echo '{"success":true}' ;;
  */models|*/models\?reload=1)
    # logcheck: состояния status.json, видимые роутеру при опросе GPU
    [ -f "$TMP_DIR/lc/status.json" ] && jq -r .state "$TMP_DIR/lc/status.json" >> "$TMP_DIR/seen_states"
    # models_busy_until_wait: «чужая» модель уходит, как только logcheck записал waiting_gpu
    if [ -f "$TMP_DIR/models_busy_until_wait" ] && grep -q waiting_gpu "$TMP_DIR/seen_states" 2>/dev/null; then
      rm -f "$TMP_DIR/models_busy_until_wait"
    fi
    if [ -f "$TMP_DIR/models_busy_until_wait" ]; then cat "$TMP_DIR/models_busy_until_wait"; exit 0; fi
    if [ -f "$TMP_DIR/models_override" ]; then cat "$TMP_DIR/models_override"; exit 0; fi
    st="\$(cat "$STATE")"
    printf '{"data":[{"id":"m9","status":{"value":"%s","args":["llama-server","--ctx-size","32768"]}}]}\n' "\$st" ;;
  */v1/chat/completions)
    # Тело запроса (--data-binary @файл) сохраняется для проверки; ответ — из chat_resp.json, если он есть
    [ -n "\$databin" ] && cp -- "\${databin#@}" "$TMP_DIR/chat_body.json"
    if [ -f "$TMP_DIR/chat_resp.json" ]; then cat "$TMP_DIR/chat_resp.json"; exit 0; fi
    echo '{"timings":{"predicted_per_second":42.42,"prompt_per_second":900.4}}' ;;
  */api/models/*) printf '[{"path":"x.gguf","size":1000,"lfs":{"oid":"%s"}}]\n' "\$oid" ;;
  */resolve/main/*)
    printf 'gguf-data' > "\$out"
    [ -e "$TMP_DIR/edit_during_download" ] && sed -i 's/^temp = 0.6\$/temp = 0.9/' "$PRESET"
    exit 0 ;;
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

  # Заглушка ssh: аргументы в $CALLS, в stdout — отчёт check-logs.py (report_fixture.md);
  # при наличии файла ssh_fail — ошибка соединения
  cat > "$STUB_DIR/ssh" <<EOF
#!/usr/bin/env bash
echo "ssh \$*" >> "$CALLS"
if [ -e "$TMP_DIR/ssh_fail" ]; then echo "ssh: connect to host host port 22: Connection refused" >&2; exit 255; fi
if [ -f "$TMP_DIR/report_fixture.md" ]; then cat "$TMP_DIR/report_fixture.md"; else printf '# Отчёт по логам\nвсё штатно\n'; fi
EOF

  cat > "$STUB_DIR/notify-send" <<EOF
#!/usr/bin/env bash
echo "notify-send \$*" >> "$CALLS"
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
  env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 "$WORK_DIR/llamactl/llamactl.sh" "$@"
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
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 GPU_APPS="4242, 512" python3 "$WORK_DIR/llamactl/llamactl_status.py" \
    --proc "$P" --router-url http://127.0.0.1:9 --fim-url http://127.0.0.1:9 --models-dir "$MODELS"
  [ "$status" -eq 0 ]
  run jq -r '[.ram.total, .ram.used, .ram.swap_used, .foreign[0].pid, .foreign[0].port, (.foreign|length),
             .gpu.procs[0].kind, .gpu.procs[0].mib, .gpu.total_mib] | map(tostring) | join(" ")' <<<"$output"
  [ "$output" = "1024000 614400 61440 4242 8099 1 foreign 512 8192" ]
}

@test "llamactl_status.py --dry-run: принимается и печатает статус" {
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 python3 "$WORK_DIR/llamactl/llamactl_status.py" --dry-run \
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

@test "add: правка models.ini во время загрузки не затирается" {
  printf 'gguf-data' | sha256sum | cut -d' ' -f1 > "$TMP_DIR/oid"
  touch "$TMP_DIR/edit_during_download"
  run ctl add user/repo x.gguf --name newm
  [ "$status" -eq 0 ]
  [ "$(cat "$MODELS/newm/x.gguf")" = "gguf-data" ]
  grep -q '^\[newm\]$' "$PRESET"
  grep -q "^model = $MODELS/newm/x.gguf$" "$PRESET"
  # Правка постороннего писателя, сделанная во время загрузки, сохранилась
  grep -q '^temp = 0.9$' "$PRESET"
  ! grep -q '^temp = 0.6$' "$PRESET"
}

@test "preset set: параллельные правки разных ключей обе сохраняются" {
  ctl preset set m9 a-key 1 >/dev/null 2>&1 &
  p1=$!
  ctl preset set m9 b-key 2 >/dev/null 2>&1 &
  p2=$!
  wait "$p1"
  wait "$p2"
  grep -q '^a-key = 1$' "$PRESET"
  grep -q '^b-key = 2$' "$PRESET"
}

@test "preset edit: models.ini изменился во время правки — правка не применяется" {
  cat > "$TMP_DIR/editor" <<EOF
#!/usr/bin/env bash
sed -i 's/^temp = 0.6\$/temp = 0.5/' "\$1"
sed -i 's/^fit-target = 512\$/fit-target = 256/' "$PRESET"
EOF
  chmod +x "$TMP_DIR/editor"
  EDITOR="$TMP_DIR/editor" run ctl preset edit
  [ "$status" -eq 1 ]
  [[ "$output" == *"изменился во время правки"* ]]
  grep -q '^fit-target = 256$' "$PRESET"
  grep -q '^temp = 0.6$' "$PRESET"
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
  run /usr/bin/python3 "$WORK_DIR/llamactl/llamactl_tray.py" --print-menu --status-file "$TMP_DIR/status.json"
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
  run /usr/bin/python3 "$WORK_DIR/llamactl/llamactl_tray.py" --print-menu --dry-run --status-file "$TMP_DIR/status.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: warn"* ]]
  [[ "$output" == *"Завершить сторонние llama-server (1)"* ]]
  [[ "$output" == *"DRY-RUN"* ]]
}

@test "трей --print-menu без файла: статус модулем llamactl_status (без llamactl.sh)" {
  start_router loaded
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 /usr/bin/python3 "$WORK_DIR/llamactl/llamactl_tray.py" --print-menu
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: "* ]]
  [[ "$output" == *"m9"* ]]
  ! grep -q 'status' "$CALLS"
}

@test "трей --print-menu: нет статуса — состояние down" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  echo 'не json' > "$TMP_DIR/status.json"
  run /usr/bin/python3 "$WORK_DIR/llamactl/llamactl_tray.py" --print-menu --status-file "$TMP_DIR/status.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"state: down"* ]]
}

###############################################################################
# llamactl logcheck и поле logcheck в статусе/трее
###############################################################################

# Отрицание grep, роняющее тест (голый `! grep` в bats тест не роняет)
no_grep() { ! grep "$@"; }

# Ответ модели для chat/completions: текст разбора и скорость генерации
chat_resp() {
  jq -n --arg c "$1" '{choices: [{message: {content: $c}}], timings: {predicted_per_second: 30.5}}' > "$TMP_DIR/chat_resp.json"
}

# status.json команды logcheck для проверки статуса и трея
write_lc_status() {
  mkdir -p "$TMP_DIR/lc"
  printf '%s\n' "$1" > "$TMP_DIR/lc/status.json"
}

@test "logcheck --dry-run: ssh читает отчёт, chat/completions не вызывается, каталог состояния не создаётся" {
  run ctl --dry-run logcheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: отправить отчёт модели m9"* ]]
  grep -q '^ssh .*test@host' "$CALLS"
  grep -q -- '--no-save' "$CALLS"
  no_grep -q 'v1/chat/completions' "$CALLS"
  no_grep -q '^notify-send' "$CALLS"
  [ ! -e "$TMP_DIR/lc" ]
}

@test "logcheck -n: сокращённый ключ тоже dry-run" {
  run ctl -n logcheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: отправить отчёт модели"* ]]
  [ ! -e "$TMP_DIR/lc" ]
}

@test "logcheck: ИТОГ в порядке — код 0, отчёт, last, status.json и уведомление" {
  printf '# Отчёт по логам\nисходная-строка-отчёта\n' > "$TMP_DIR/report_fixture.md"
  chat_resp $'Всё штатно, ошибок нет.\nнорма-строка-разбора\n\nИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  # ssh: удалённая команда с --no-save и каталогом проекта
  grep -q "ssh .*-o BatchMode=yes .* -- test@host cd /opt/esimych-cloud && python3 -I scripts/check-logs.py --since .* --no-save" "$CALLS"
  # Файл отчёта: разбор модели и исходный отчёт
  ls "$TMP_DIR"/lc/20*.md
  report="$(ls "$TMP_DIR"/lc/20*.md | head -n 1)"
  grep -q '^## Разбор модели$' "$report"
  grep -q 'норма-строка-разбора' "$report"
  grep -q '^## Отчёт check-logs.py$' "$report"
  grep -q 'исходная-строка-отчёта' "$report"
  grep -q 'Модель: m9, 30 ток/с' "$report"
  # Метка последней проверки
  [ -s "$TMP_DIR/lc/last" ]
  # status.json
  run jq -r '[.state, .verdict, .report] | join("|")' "$TMP_DIR/lc/status.json"
  [ "$output" = "done|в порядке|$report" ]
  # Запрос к модели: без reasoning, нужная модель, отчёт в user-сообщении
  jq -e '.chat_template_kwargs.enable_thinking == false and .model == "m9"' "$TMP_DIR/chat_body.json"
  jq -e '.messages[1].content | contains("исходная-строка-отчёта")' "$TMP_DIR/chat_body.json"
  # Уведомление с низкой срочностью
  grep -q '^notify-send .*-u low .*Логи сервера: в порядке' "$CALLS"
}

@test "logcheck: модель была выгружена — после проверки выгружается снова" {
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  grep -q 'models/unload' "$CALLS"
}

@test "logcheck: модель уже была загружена — не выгружается" {
  echo loaded > "$STATE"
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  no_grep -q 'models/unload' "$CALLS"
}

@test "logcheck: ИТОГ критично — код 1 и уведомление с -u critical" {
  chat_resp $'Диск заполнен.\n\nИТОГ: критично'
  run ctl logcheck
  [ "$status" -eq 1 ]
  run jq -r .verdict "$TMP_DIR/lc/status.json"
  [ "$output" = "критично" ]
  grep -q '^notify-send .*-u critical .*Логи сервера: критично' "$CALLS"
}

@test "logcheck: ИТОГ внимание — код 1, срочность normal" {
  chat_resp $'Есть предупреждения.\n\nИТОГ: внимание'
  run ctl logcheck
  [ "$status" -eq 1 ]
  grep -q '^notify-send .*-u normal ' "$CALLS"
}

@test "logcheck: модель нарушила формат ответа — итог «внимание», код 1" {
  chat_resp 'просто текст без итоговой строки'
  run ctl logcheck
  [ "$status" -eq 1 ]
  run jq -r .verdict "$TMP_DIR/lc/status.json"
  [ "$output" = "внимание" ]
}

@test "logcheck: пустой ответ модели — код 4 и state=error" {
  chat_resp ''
  run ctl logcheck
  [ "$status" -eq 4 ]
  run jq -r .state "$TMP_DIR/lc/status.json"
  [ "$output" = "error" ]
  [ ! -e "$TMP_DIR/lc/last" ]
}

@test "logcheck: GPU занята другой моделью — пропуск, код 3, state=skipped с именем модели" {
  cat > "$TMP_DIR/models_override" <<'JSON'
{"data":[{"id":"other","status":{"value":"loaded","args":[]}},{"id":"m9","status":{"value":"unloaded","args":[]}}]}
JSON
  run ctl logcheck
  [ "$status" -eq 3 ]
  run jq -r '[.state, .reason] | join("|")' "$TMP_DIR/lc/status.json"
  [[ "$output" == "skipped|"*"other"* ]]
  # Во время ожидания трей видел waiting_gpu
  grep -q waiting_gpu "$TMP_DIR/seen_states"
  no_grep -q 'v1/chat/completions' "$CALLS"
  no_grep -q '^notify-send' "$CALLS"
  [ ! -e "$TMP_DIR/lc/last" ]
}

@test "logcheck: загружающаяся чужая модель тоже занимает GPU" {
  cat > "$TMP_DIR/models_override" <<'JSON'
{"data":[{"id":"other","status":{"value":"loading","args":[]}},{"id":"m9","status":{"value":"unloaded","args":[]}}]}
JSON
  run ctl logcheck
  [ "$status" -eq 3 ]
}

@test "logcheck: мало свободной VRAM — пропуск, код 3, причина с числами" {
  echo 'LOGCHECK_MIN_FREE_VRAM_MIB="9000"' >> "$WORK_DIR/conf/llamactl.conf"
  run ctl logcheck
  [ "$status" -eq 3 ]
  run jq -r '[.state, .reason] | join("|")' "$TMP_DIR/lc/status.json"
  [[ "$output" == "skipped|свободно 6892 МиБ VRAM из 8192 (нужно 9000)" ]]
  no_grep -q 'v1/chat/completions' "$CALLS"
}

@test "logcheck: уже загруженная своя модель не считается занятой VRAM" {
  echo loaded > "$STATE"
  echo 'LOGCHECK_MIN_FREE_VRAM_MIB="9000"' >> "$WORK_DIR/conf/llamactl.conf"
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
}

@test "logcheck: GPU освободилась во время ожидания — проверка выполняется" {
  cat > "$TMP_DIR/models_busy_until_wait" <<'JSON'
{"data":[{"id":"other","status":{"value":"loaded","args":[]}},{"id":"m9","status":{"value":"unloaded","args":[]}}]}
JSON
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  grep -q waiting_gpu "$TMP_DIR/seen_states"
  grep -q 'v1/chat/completions' "$CALLS"
  run jq -r .state "$TMP_DIR/lc/status.json"
  [ "$output" = "done" ]
}

@test "logcheck --dry-run: занятая GPU не ждётся и не пишет состояние" {
  cat > "$TMP_DIR/models_override" <<'JSON'
{"data":[{"id":"other","status":{"value":"loaded","args":[]}},{"id":"m9","status":{"value":"unloaded","args":[]}}]}
JSON
  run ctl --dry-run logcheck
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: ждать освобождения GPU"* ]]
  [ ! -e "$TMP_DIR/lc" ]
  no_grep -q 'v1/chat/completions' "$CALLS"
}

@test "logcheck: ssh падает — код 4, state=error, критичное уведомление" {
  touch "$TMP_DIR/ssh_fail"
  run ctl logcheck
  [ "$status" -eq 4 ]
  run jq -r '[.state, .reason] | join("|")' "$TMP_DIR/lc/status.json"
  [[ "$output" == "error|"*"test@host"* ]]
  [[ "$output" == *"Connection refused"* ]]
  grep -q '^notify-send .*-u critical ' "$CALLS"
  no_grep -q 'v1/chat/completions' "$CALLS"
}

@test "logcheck: роутер недоступен — код 4, state=error" {
  touch "$TMP_DIR/down"
  run ctl logcheck
  [ "$status" -eq 4 ]
  run jq -r .state "$TMP_DIR/lc/status.json"
  [ "$output" = "error" ]
}

@test "logcheck: нет такой модели в роутере — код 2" {
  echo 'LOGCHECK_MODEL="nope"' >> "$WORK_DIR/conf/llamactl.conf"
  run ctl logcheck
  [ "$status" -eq 2 ]
  no_grep -q 'v1/chat/completions' "$CALLS"
}

@test "logcheck: нечисловое или нулевое LOGCHECK_* — код 2 без обращения к серверу" {
  echo 'LOGCHECK_WAIT_SECS="много"' >> "$WORK_DIR/conf/llamactl.conf"
  run ctl logcheck
  [ "$status" -eq 2 ]
  no_grep -q '^ssh' "$CALLS"
  sed -i '/LOGCHECK_WAIT_SECS="много"/d' "$WORK_DIR/conf/llamactl.conf"
  echo 'LOGCHECK_POLL_SECS="0"' >> "$WORK_DIR/conf/llamactl.conf"
  run ctl logcheck
  [ "$status" -eq 2 ]
  no_grep -q '^ssh' "$CALLS"
}

@test "logcheck: второй запуск берёт --since из last" {
  mkdir -p "$TMP_DIR/lc"
  echo '2026-01-02 03:04:05' > "$TMP_DIR/lc/last"
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  # printf %q экранирует пробел в удалённой команде
  grep -qF -- '--since 2026-01-02\ 03:04:05 --no-save' "$CALLS"
  # last обновлён на время нового запуска
  [ "$(cat "$TMP_DIR/lc/last")" != "2026-01-02 03:04:05" ]
  # Третий запуск берёт --since уже из обновлённого last
  prev="$(cat "$TMP_DIR/lc/last")"
  : > "$CALLS"
  run ctl logcheck
  [ "$status" -eq 0 ]
  no_grep -qF -- '2026-01-02\ 03:04:05' "$CALLS"
  grep -qF -- "--since ${prev// /\\ } --no-save" "$CALLS"
}

@test "logcheck: без last отчёт запрашивается за сутки" {
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  expected="$(date -d '-1 day' '+%F')"
  grep -qF -- "--since $expected" "$CALLS"
}

@test "logcheck: ротация оставляет LOGCHECK_KEEP последних отчётов" {
  echo 'LOGCHECK_KEEP="2"' >> "$WORK_DIR/conf/llamactl.conf"
  mkdir -p "$TMP_DIR/lc"
  for d in 2001-01-01_0100 2001-01-02_0100 2001-01-03_0100; do
    : > "$TMP_DIR/lc/$d.md"
    touch -d "${d:0:10}" "$TMP_DIR/lc/$d.md"
  done
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  [ "$(ls "$TMP_DIR"/lc/20*.md | wc -l)" -eq 2 ]
  [ ! -e "$TMP_DIR/lc/2001-01-01_0100.md" ]
  [ ! -e "$TMP_DIR/lc/2001-01-02_0100.md" ]
}

@test "logcheck: слишком большой отчёт обрезается до LOGCHECK_MAX_REPORT_BYTES" {
  echo 'LOGCHECK_MAX_REPORT_BYTES="100"' >> "$WORK_DIR/conf/llamactl.conf"
  head -c 2000 /dev/zero | tr '\0' 'x' > "$TMP_DIR/report_fixture.md"
  chat_resp $'ИТОГ: в порядке'
  run ctl logcheck
  [ "$status" -eq 0 ]
  jq -e '.messages[1].content | contains("отчёт обрезан до 100 байт из 2000")' "$TMP_DIR/chat_body.json"
}

@test "logcheck: параллельный запуск не стартует второй раз" {
  mkdir -p "$TMP_DIR/lc"
  exec 8>"$TMP_DIR/lc/.lock"
  flock -n 8
  run ctl logcheck
  exec 8>&-
  [ "$status" -eq 0 ]
  [[ "$output" == *"уже выполняется"* ]]
  no_grep -q '^ssh' "$CALLS"
}

@test "llamactl_status.py: поле logcheck берётся из status.json в LOGCHECK_STATE_DIR" {
  write_lc_status '{"state":"waiting_gpu","reason":"загружена модель other","verdict":"","report":"","updated":"2026-10-29 10:00:00","wait_secs":1800}'
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 python3 "$WORK_DIR/llamactl/llamactl_status.py" \
    --conf "$WORK_DIR/conf/llamactl.conf" --router-url http://127.0.0.1:9 --fim-url http://127.0.0.1:9
  [ "$status" -eq 0 ]
  run jq -r '[.logcheck.state, .logcheck.reason, (.logcheck.wait_secs | tostring)] | join("|")' <<<"$output"
  [ "$output" = "waiting_gpu|загружена модель other|1800" ]
}

@test "llamactl_status.py: logcheck = null без status.json и при битом файле" {
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 python3 "$WORK_DIR/llamactl/llamactl_status.py" \
    --conf "$WORK_DIR/conf/llamactl.conf" --router-url http://127.0.0.1:9 --fim-url http://127.0.0.1:9
  [ "$status" -eq 0 ]
  jq -e '.logcheck == null' <<<"$output"
  mkdir -p "$TMP_DIR/lc"
  echo 'не json' > "$TMP_DIR/lc/status.json"
  run env PATH="$STUB_DIR:$PATH" LLAMACTL_NO_NVML=1 python3 "$WORK_DIR/llamactl/llamactl_status.py" \
    --conf "$WORK_DIR/conf/llamactl.conf" --router-url http://127.0.0.1:9 --fim-url http://127.0.0.1:9
  [ "$status" -eq 0 ]
  jq -e '.logcheck == null' <<<"$output"
}

@test "llamactl status --json: logcheck из status.json попадает в вывод команды" {
  start_router loaded
  write_lc_status '{"state":"done","reason":"","verdict":"в порядке","report":"/x/r.md","updated":"2026-10-29 10:00:00","wait_secs":2}'
  run ctl status --json
  [ "$status" -eq 0 ]
  run jq -r '.logcheck.verdict' <<<"$output"
  [ "$output" = "в порядке" ]
}

# Статус для трея с заданным блоком logcheck
tray_menu() {
  local lc="$1"
  cat > "$TMP_DIR/status.json" <<JSON
{"services":{"server":"active","fim":"active"},"router":{"up":true},"fim":{"up":true},"models":[],
 "gpu":{"used_mib":1,"total_mib":8192,"procs":[]},"ram":{},"disk":{},"foreign":[],"logcheck":${lc}}
JSON
  /usr/bin/python3 "$WORK_DIR/llamactl/llamactl_tray.py" --print-menu --status-file "$TMP_DIR/status.json"
}

@test "трей --print-menu: waiting_gpu — сообщение, подсказка, подменю и отчёт" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  run tray_menu '{"state":"waiting_gpu","reason":"загружена модель other","verdict":"","report":"/x/20261029_0300.md","updated":"2026-10-29 03:00:00","wait_secs":1800}'
  [ "$status" -eq 0 ]
  [[ "$output" == *"сообщение: Проверка логов сервера ждёт GPU"* ]]
  [[ "$output" == *"Жду до 30 мин"* ]]
  # Строка подсказки трея — отдельная строка без отступа (пункт меню печатается с отступом)
  grep -qx 'Проверка логов сервера: ждёт освобождения GPU, 10-29 03:00' <<<"$output"
  [[ "$output" == *"Запустить сейчас  <bg: llamactl logcheck>"* ]]
  [[ "$output" == *"Открыть последний отчёт"* ]]
  [[ "$output" == *"загружена модель other"* ]]
}

@test "трей --print-menu: skipped — сообщение о пропуске" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  run tray_menu '{"state":"skipped","reason":"загружена модель other","verdict":"","report":"","updated":"2026-10-29 03:30:00","wait_secs":1800}'
  [ "$status" -eq 0 ]
  [[ "$output" == *"сообщение: Проверка логов сервера пропущена"* ]]
  [[ "$output" == *"Проверка логов сервера: пропущена — GPU занята"* ]]
  [[ "$output" == *"Запустить сейчас"* ]]
  [[ "$output" != *"Открыть последний отчёт"* ]]
}

@test "трей --print-menu: done — строки «сообщение:» нет, отчёт доступен" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  run tray_menu '{"state":"done","reason":"","verdict":"в порядке","report":"/x/20261029_0300.md","updated":"2026-10-29 03:05:00","wait_secs":1800}'
  [ "$status" -eq 0 ]
  [[ "$output" != *"сообщение:"* ]]
  grep -qx 'Проверка логов сервера: выполнена (в порядке), 10-29 03:05' <<<"$output"
  [[ "$output" == *"Открыть последний отчёт"* ]]
}

@test "трей --print-menu: logcheck = null — «ещё не запускалась», сообщения нет" {
  [ -x /usr/bin/python3 ] || skip "нет /usr/bin/python3"
  run tray_menu 'null'
  [ "$status" -eq 0 ]
  [[ "$output" != *"сообщение:"* ]]
  [[ "$output" == *"Проверка логов сервера: ещё не запускалась"* ]]
  [[ "$output" == *"Запустить сейчас  <bg: llamactl logcheck>"* ]]
  [[ "$output" != *"Открыть последний отчёт"* ]]
}
