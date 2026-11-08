#!/usr/bin/env bats

# Тесты для system_monitor.sh
# smartctl, lsblk, sensors, nvidia-smi и notify-send заменяются стабами;
# sqlite3 и jq — настоящие (проверка реальной записи/чтения SQLite и
# разбора JSON важна сама по себе). CPU опрашивается по-настоящему через
# /proc/stat и /proc/loadavg (эти псевдофайлы есть на любом Linux, включая
# CI-раннеры) — не стабится, т.к. это не внешняя команда, а чтение файла.
# Скрипт копируется во временную папку, поэтому logs/, state/ и
# system_reports/ создаются в TMP_DIR, а не в репозитории.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  TMP_DIR="$(mktemp -d)"
  STUB_DIR="$TMP_DIR/stubs"
  SMART_DIR="$TMP_DIR/smart"
  NOTIFY_LOG="$TMP_DIR/notify.log"
  export SMART_DIR NOTIFY_LOG

  mkdir -p "$STUB_DIR" "$SMART_DIR" "$TMP_DIR/conf" "$TMP_DIR/logs" "$TMP_DIR/state"

  cp "$REPO_ROOT/system_monitor.sh" "$TMP_DIR/system_monitor.sh"
  chmod +x "$TMP_DIR/system_monitor.sh"

  # Здоровые диски по умолчанию: sda и sdb
  write_smart_json sda '{"smart_status":{"passed":true},"model_name":"TestDisk-A","serial_number":"SNAAA","temperature":{"current":35},"power_on_time":{"hours":1000},"ata_smart_attributes":{"table":[{"name":"Reallocated_Sector_Ct","raw":{"value":0}},{"name":"Current_Pending_Sector","raw":{"value":0}},{"name":"Offline_Uncorrectable","raw":{"value":0}}]}}'
  write_smart_json sdb '{"smart_status":{"passed":true},"model_name":"TestDisk-B","serial_number":"SNBBB","temperature":{"current":36},"power_on_time":{"hours":2000},"ata_smart_attributes":{"table":[{"name":"Reallocated_Sector_Ct","raw":{"value":0}},{"name":"Current_Pending_Sector","raw":{"value":0}},{"name":"Offline_Uncorrectable","raw":{"value":0}}]}}'

  # Стаб lsblk: обслуживает три разных вызова скрипта —
  # discover_disks (-dn -o NAME,TYPE), disk_mountpoints (-J -o NAME,MOUNTPOINTS -- <dev>,
  # разбирается через jq, поэтому отдаём настоящий JSON в формате lsblk -J)
  # и disk_size_bytes (-bdn -o SIZE -- <dev>)
  cat > "$STUB_DIR/lsblk" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *NAME,TYPE*)
    printf 'sda disk\nsdb disk\nzram0 disk\n'
    ;;
  *NAME,MOUNTPOINTS*)
    dev="${!#}"
    case "$(basename -- "$dev")" in
      sda) printf '{"blockdevices":[{"name":"sda","mountpoints":[],"children":[{"name":"sda1","mountpoints":["/"]}]}]}\n' ;;
      sdb) printf '{"blockdevices":[{"name":"sdb","mountpoints":[],"children":[{"name":"sdb1","mountpoints":["/home"]}]}]}\n' ;;
      *) printf '{"blockdevices":[{"name":"%s","mountpoints":[]}]}\n' "$(basename -- "$dev")" ;;
    esac
    ;;
  *SIZE*)
    dev="${!#}"
    case "$(basename -- "$dev")" in
      sda) printf '4000787030016\n' ;;
      sdb) printf '2000398934016\n' ;;
      *) printf '1000204886016\n' ;;
    esac
    ;;
  *) ;;
esac
EOF
  chmod +x "$STUB_DIR/lsblk"

  # Стаб smartctl: печатает JSON-фикстуру для устройства из SMART_DIR
  cat > "$STUB_DIR/smartctl" <<'EOF'
#!/usr/bin/env bash
dev="${!#}"
name="$(basename -- "$dev")"
file="$SMART_DIR/${name}.json"
if [[ -f "$file" ]]; then
  cat "$file"
  exit 0
fi
exit 1
EOF
  chmod +x "$STUB_DIR/smartctl"

  # Стаб sensors: здоровая температура CPU по умолчанию (45°C)
  cat > "$STUB_DIR/sensors" <<'EOF'
#!/usr/bin/env bash
printf '{"k10temp-pci-00c3":{"Adapter":"PCI adapter","Tctl":{"temp1_input": 45.0}}}\n'
EOF
  chmod +x "$STUB_DIR/sensors"

  # Стаб nvidia-smi: одна здоровая видеокарта по умолчанию (40°C)
  cat > "$STUB_DIR/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
printf '0, Test GPU, 40, 20, 512, 4096, 25.50\n'
EOF
  chmod +x "$STUB_DIR/nvidia-smi"

  # Стаб notify-send: фиксирует факт вызова
  cat > "$STUB_DIR/notify-send" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$NOTIFY_LOG"
exit 0
EOF
  chmod +x "$STUB_DIR/notify-send"
}

teardown() {
  rm -rf "$TMP_DIR"
}

write_smart_json() {
  local name="$1" json="$2"
  printf '%s' "$json" > "$SMART_DIR/${name}.json"
}

run_monitor() {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/system_monitor.sh" "$@"
}

last_log() {
  ls -1t "$TMP_DIR/logs"/system_monitor-*.jsonl 2>/dev/null | head -1
}

db_query() {
  sqlite3 -noheader -separator '|' "$TMP_DIR/state/system_monitor.db" "$1"
}

# ---------------------------------------------------------------------------
# Проверка зависимостей
# ---------------------------------------------------------------------------

@test "завершается с ошибкой если smartctl не установлен" {
  local iso="$TMP_DIR/iso_no_smartctl"
  mkdir -p "$iso"
  cp "$STUB_DIR/lsblk" "$iso/lsblk"
  cp "$STUB_DIR/notify-send" "$iso/notify-send"
  for _cmd in bash env date mkdir ls rm sed grep tr awk mktemp basename dirname cat printf cp find sort sqlite3 jq; do
    _bin="$(command -v "$_cmd" 2>/dev/null)"
    [ -n "$_bin" ] && ln -sf "$_bin" "$iso/$_cmd" || true
  done

  run env PATH="$iso" bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 2 ]
  run grep -q '"event":"dependency_missing"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "завершается с ошибкой если не найдено дисков" {
  cat > "$STUB_DIR/lsblk" <<'EOF'
#!/usr/bin/env bash
printf 'zram0 disk\nloop0 disk\n'
EOF
  chmod +x "$STUB_DIR/lsblk"

  run_monitor

  [ "$status" -eq 2 ]
  run grep -q '"event":"no_disks_found"' "$(last_log)"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Опрос и запись в SQLite — диски
# ---------------------------------------------------------------------------

@test "здоровые диски: опрос, запись в БД и отчёт, выход 0" {
  run_monitor

  [ "$status" -eq 0 ]
  [ -f "$TMP_DIR/state/system_monitor.db" ]
  [[ "$output" == *"Отчёт:"* ]]

  run db_query "SELECT COUNT(*) FROM disk_stats;"
  [ "$status" -eq 0 ]
  [ "$output" = "2" ]
}

@test "zram и прочие виртуальные устройства не опрашиваются" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "SELECT device FROM disk_stats WHERE device LIKE '%zram%';"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "в БД есть колонка device_alias и она читается без ошибок" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "SELECT device_alias FROM disk_stats WHERE device='/dev/sda';"
  [ "$status" -eq 0 ]
}

@test "в отчёте есть колонка 'Точка монтирования' с реальными точками" {
  run_monitor
  [ "$status" -eq 0 ]

  run grep -q "Точка монтирования" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
  run grep -q "/home" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
}

@test "аналитическая справка упоминает точки монтирования диска" {
  run_monitor
  [ "$status" -eq 0 ]

  run grep -q "Точки монтирования: sdb1: /home" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
}

@test "в БД сохраняются корректные метрики диска" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "SELECT health, temperature_c, reallocated_sectors FROM disk_stats WHERE device='/dev/sda';"
  [ "$status" -eq 0 ]
  [ "$output" = "PASSED|35|0" ]
}

@test "объём диска сохраняется в БД и отображается в отчёте" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "SELECT size_bytes FROM disk_stats WHERE device='/dev/sda';"
  [ "$status" -eq 0 ]
  [ "$output" = "4000787030016" ]

  run grep -q "Объём" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
  run grep -q "3.6 ТиБ" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
}

@test "HTML-отчёт создаётся в system_reports рядом со скриптом" {
  run_monitor
  [ "$status" -eq 0 ]

  [ -f "$TMP_DIR/system_reports/latest.html" ]
  count=$(ls -1 "$TMP_DIR/system_reports"/system_monitor-*.html 2>/dev/null | wc -l)
  [ "$count" -ge 1 ]
  run grep -q "Диагностика системы" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Опрос и запись в SQLite — CPU и GPU
# ---------------------------------------------------------------------------

@test "CPU: температура, загрузка и load average сохраняются в БД и в отчёте" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "SELECT temperature_c FROM cpu_stats ORDER BY id DESC LIMIT 1;"
  [ "$status" -eq 0 ]
  [ "$output" = "45" ]

  run db_query "SELECT usage_percent IS NOT NULL, load1 IS NOT NULL FROM cpu_stats ORDER BY id DESC LIMIT 1;"
  [ "$output" = "1|1" ]

  run grep -q "Процессор (CPU)" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
  run grep -qE '<td>45</td>' "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
}

@test "GPU: данные сохраняются в БД и отображаются в отчёте" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "SELECT device, model, temperature_c, mem_total_mb FROM gpu_stats ORDER BY id DESC LIMIT 1;"
  [ "$status" -eq 0 ]
  [ "$output" = "0|Test GPU|40|4096" ]

  run grep -q "Test GPU" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
  run grep -q "Видеокарта (GPU)" "$TMP_DIR/system_reports/latest.html"
  [ "$status" -eq 0 ]
}

# Для "недоступен" нужна ПОЛНОСТЬЮ изолированная PATH (замена, а не
# "$STUB_DIR:$PATH" из run_monitor) — иначе реальные sensors/nvidia-smi,
# если они есть на машине, где запускаются тесты, "просвечивают" через
# оставшуюся часть реального PATH и тест ничего не проверяет.
iso_path_without() {
  local iso="$1"
  mkdir -p "$iso"
  cp "$STUB_DIR/lsblk" "$iso/lsblk"
  cp "$STUB_DIR/smartctl" "$iso/smartctl"
  cp "$STUB_DIR/notify-send" "$iso/notify-send"
  for _cmd in bash env date mkdir ls rm mv chmod sed grep tr awk sleep mktemp basename dirname cat printf cp find sort sqlite3 jq; do
    _bin="$(command -v "$_cmd" 2>/dev/null)"
    [ -n "$_bin" ] && ln -sf "$_bin" "$iso/$_cmd" || true
  done
}

@test "GPU: nvidia-smi недоступен → опрос GPU пропускается без ошибки" {
  local iso="$TMP_DIR/iso_no_gpu"
  iso_path_without "$iso"

  run env PATH="$iso" bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 0 ]
  run grep -q '"event":"gpu_unavailable"' "$(last_log)"
  [ "$status" -eq 0 ]
  run db_query "SELECT COUNT(*) FROM gpu_stats;"
  [ "$output" = "0" ]
}

@test "CPU: sensors недоступен → CPU опрашивается, но без температуры" {
  local iso="$TMP_DIR/iso_no_sensors"
  iso_path_without "$iso"

  run env PATH="$iso" bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 0 ]
  run grep -q '"event":"sensors_unavailable"' "$(last_log)"
  [ "$status" -eq 0 ]
  run db_query "SELECT COUNT(*), temperature_c FROM cpu_stats;"
  [ "$output" = "1|" ]
}

@test "высокая температура CPU выше критического порога → критично, выход 1" {
  cat > "$TMP_DIR/conf/system_monitor.conf" <<'EOF'
CPU_TEMP_CRIT_C=10
EOF

  run_monitor

  [ "$status" -eq 1 ]
  [[ "$output" == *"КРИТИЧНО"* ]]
  [[ "$output" == *"CPU"* ]]
}

@test "высокая температура GPU выше критического порога → критично, выход 1" {
  cat > "$TMP_DIR/conf/system_monitor.conf" <<'EOF'
GPU_TEMP_CRIT_C=10
EOF

  run_monitor

  [ "$status" -eq 1 ]
  [[ "$output" == *"КРИТИЧНО"* ]]
  [[ "$output" == *"GPU0"* ]]
}

# ---------------------------------------------------------------------------
# Критическое состояние и уведомления — диски
# ---------------------------------------------------------------------------

@test "отказавший диск (FAILED) → критично, notify-send и выход 1" {
  write_smart_json sdb '{"smart_status":{"passed":false},"model_name":"TestDisk-B","serial_number":"SNBBB","temperature":{"current":40},"power_on_time":{"hours":2000},"ata_smart_attributes":{"table":[{"name":"Reallocated_Sector_Ct","raw":{"value":0}},{"name":"Current_Pending_Sector","raw":{"value":0}},{"name":"Offline_Uncorrectable","raw":{"value":0}}]}}'

  run_monitor

  [ "$status" -eq 1 ]
  [[ "$output" == *"КРИТИЧНО"* ]]
  [ -f "$NOTIFY_LOG" ]
  run grep -q "/dev/sdb" "$NOTIFY_LOG"
  [ "$status" -eq 0 ]
  run grep -q '"event":"critical_status"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "высокая температура диска выше критического порога → выход 1" {
  write_smart_json sdb '{"smart_status":{"passed":true},"model_name":"TestDisk-B","serial_number":"SNBBB","temperature":{"current":70},"power_on_time":{"hours":2000},"ata_smart_attributes":{"table":[{"name":"Reallocated_Sector_Ct","raw":{"value":0}},{"name":"Current_Pending_Sector","raw":{"value":0}},{"name":"Offline_Uncorrectable","raw":{"value":0}}]}}'

  run_monitor

  [ "$status" -eq 1 ]
}

@test "NVMe: свои пороги NVME_TEMP_* вместо общих TEMP_*, SATA остаётся на общих" {
  write_smart_json nvme0n1 '{"smart_status":{"passed":true},"model_name":"TestNVMe","serial_number":"SNNVME","temperature":{"current":65},"power_on_time":{"hours":500}}'

  # без NVMe-порогов 65 °C выше общего критического (60) → выход 1
  DISKS="nvme0n1" run_monitor
  [ "$status" -eq 1 ]

  # с порогами 70/80 те же 65 °C — норма, без предупреждения
  rm -f "$TMP_DIR/logs"/system_monitor-*.jsonl
  DISKS="nvme0n1" NVME_TEMP_WARN_C=70 NVME_TEMP_CRIT_C=80 run_monitor
  [ "$status" -eq 0 ]
  run grep -q '"event":"system_warning"' "$(last_log)"
  [ "$status" -ne 0 ]

  # 72 °C — «внимание» по NVMe-порогу, но не критично
  write_smart_json nvme0n1 '{"smart_status":{"passed":true},"model_name":"TestNVMe","serial_number":"SNNVME","temperature":{"current":72},"power_on_time":{"hours":500}}'
  rm -f "$TMP_DIR/logs"/system_monitor-*.jsonl
  DISKS="nvme0n1" NVME_TEMP_WARN_C=70 NVME_TEMP_CRIT_C=80 run_monitor
  [ "$status" -eq 0 ]
  run grep -q '"event":"system_warning"' "$(last_log)"
  [ "$status" -eq 0 ]

  # SATA-диск при тех же NVMe-порогах судится по общим TEMP_*: 65 °C → критично
  write_smart_json sdb '{"smart_status":{"passed":true},"model_name":"TestDisk-B","serial_number":"SNBBB","temperature":{"current":65},"power_on_time":{"hours":2000},"ata_smart_attributes":{"table":[{"name":"Reallocated_Sector_Ct","raw":{"value":0}},{"name":"Current_Pending_Sector","raw":{"value":0}},{"name":"Offline_Uncorrectable","raw":{"value":0}}]}}'
  DISKS="sdb" NVME_TEMP_WARN_C=70 NVME_TEMP_CRIT_C=80 run_monitor
  [ "$status" -eq 1 ]
}

@test "переназначенные секторы (warning) не вызывают notify-send" {
  write_smart_json sdb '{"smart_status":{"passed":true},"model_name":"TestDisk-B","serial_number":"SNBBB","temperature":{"current":36},"power_on_time":{"hours":2000},"ata_smart_attributes":{"table":[{"name":"Reallocated_Sector_Ct","raw":{"value":5}},{"name":"Current_Pending_Sector","raw":{"value":0}},{"name":"Offline_Uncorrectable","raw":{"value":0}}]}}'

  run_monitor

  [ "$status" -eq 0 ]
  [ ! -f "$NOTIFY_LOG" ]
  run grep -q '"event":"system_warning"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "недоступный диску (ошибка smartctl) помечается UNKNOWN, но не критичен" {
  rm -f "$SMART_DIR/sdb.json"

  run_monitor

  [ "$status" -eq 0 ]
  run grep -q '"event":"smartctl_read_failed"' "$(last_log)"
  [ "$status" -eq 0 ]
  run db_query "SELECT health FROM disk_stats WHERE device='/dev/sdb';"
  [ "$output" = "UNKNOWN" ]
}

# ---------------------------------------------------------------------------
# Режим --report (только пересборка отчёта)
# ---------------------------------------------------------------------------

@test "--report завершается с ошибкой если база ещё пуста" {
  run_monitor --report

  [ "$status" -eq 2 ]
  run grep -q '"event":"no_data"' "$(last_log)"
  [ "$status" -eq 0 ]
}

@test "--report пересобирает отчёт из БД без повторного опроса" {
  run_monitor
  [ "$status" -eq 0 ]
  before=$(db_query "SELECT COUNT(*) FROM disk_stats;")
  before_cpu=$(db_query "SELECT COUNT(*) FROM cpu_stats;")

  # Второй запуск изолирован от smartctl/lsblk/sensors/nvidia-smi — режим
  # отчёта читает всё из уже сохранённых в БД данных
  local iso="$TMP_DIR/iso_report_only"
  mkdir -p "$iso"
  cp "$STUB_DIR/notify-send" "$iso/notify-send"
  for _cmd in bash env date mkdir ls rm mv chmod sed grep tr awk mktemp basename dirname cat printf cp find sort sqlite3 jq; do
    _bin="$(command -v "$_cmd" 2>/dev/null)"
    [ -n "$_bin" ] && ln -sf "$_bin" "$iso/$_cmd" || true
  done

  run env PATH="$iso" bash "$TMP_DIR/system_monitor.sh" -r

  [ "$status" -eq 0 ]
  after=$(db_query "SELECT COUNT(*) FROM disk_stats;")
  after_cpu=$(db_query "SELECT COUNT(*) FROM cpu_stats;")
  [ "$before" = "$after" ]
  [ "$before_cpu" = "$after_cpu" ]
}

# ---------------------------------------------------------------------------
# Аргументы
# ---------------------------------------------------------------------------

@test "-h/--help выводит справку и завершается с 0" {
  run_monitor --help

  [ "$status" -eq 0 ]
  [[ "$output" == *"Использование"* ]]
}

@test "неизвестный аргумент завершается с ошибкой" {
  run_monitor --unknown-flag

  [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

@test "лог-файл создаётся и содержит обязательные поля" {
  run_monitor
  [ "$status" -eq 0 ]

  log_file="$(last_log)"
  [ -n "$log_file" ]
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    [[ "$line" == *'"@timestamp"'* ]]
    [[ "$line" == *'"level"'* ]]
    [[ "$line" == *'"event"'* ]]
  done < "$log_file"
}

@test "старые JSONL-логи удаляются согласно KEEP_LOGS" {
  cat > "$TMP_DIR/conf/system_monitor.conf" <<'EOF'
KEEP_LOGS=2
EOF

  for i in 1 2 3 4 5; do
    touch "$TMP_DIR/logs/system_monitor-2020-01-0${i}-00-00-00.jsonl"
  done

  run_monitor
  [ "$status" -eq 0 ]

  count=$(ls -1 "$TMP_DIR/logs"/system_monitor-*.jsonl 2>/dev/null | wc -l)
  [ "$count" -le 2 ]
}

# ---------------------------------------------------------------------------
# Ротация HTML-отчётов
# ---------------------------------------------------------------------------

@test "старые HTML-отчёты удаляются согласно REPORT_KEEP" {
  cat > "$TMP_DIR/conf/system_monitor.conf" <<'EOF'
REPORT_KEEP=2
EOF

  mkdir -p "$TMP_DIR/system_reports"
  for i in 1 2 3 4 5; do
    touch "$TMP_DIR/system_reports/system_monitor-2020-01-0${i}-00-00-00.html"
  done

  run_monitor
  [ "$status" -eq 0 ]

  count=$(ls -1 "$TMP_DIR/system_reports"/system_monitor-*.html 2>/dev/null | wc -l)
  [ "$count" -le 2 ]
  # latest.html — фиксированное имя, ротацией не затрагивается
  [ -f "$TMP_DIR/system_reports/latest.html" ]
}

@test "dry-run polls but changes neither DB nor reports" {
  run env PATH="$STUB_DIR:$PATH" bash "$TMP_DIR/system_monitor.sh" --dry-run

  [ "$status" -eq 0 ]
  [ ! -e "$TMP_DIR/state/system_monitor.db" ]
  [ ! -e "$TMP_DIR/system_reports" ]
  [[ "$output" == *"[dry-run] будет: запись в"*"disk_stats"* ]]
  [[ "$output" == *"[dry-run] Изменения не выполнены."* ]]
}

@test "SMARTCTL_OPTS из конфигурации передаются в smartctl" {
  local args_log="$TMP_DIR/smartctl_args.log"
  cat > "$STUB_DIR/smartctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$args_log"
dev="\${!#}"
name="\$(basename -- "\$dev")"
file="$SMART_DIR/\${name}.json"
[[ -f "\$file" ]] && cat "\$file"
exit 0
EOF
  chmod +x "$STUB_DIR/smartctl"
  printf 'SMARTCTL_OPTS="-a -j -d sat"\n' > "$TMP_DIR/conf/system_monitor.conf"

  DISKS="sda" run_monitor

  [ "$status" -eq 0 ]
  grep -qF -- '-a -j -d sat /dev/sda' "$args_log"
}

# ---------------------------------------------------------------------------
# Версионированные миграции схемы БД (PRAGMA user_version)
# ---------------------------------------------------------------------------

make_old_db() {
  # Старая схема: device_alias/mountpoints/size_bytes в конце; счётчик AUTOINCREMENT = 4
  # при MAX(id) = 3 (четвёртая строка была удалена)
  sqlite3 "$TMP_DIR/state/system_monitor.db" <<'SQL'
CREATE TABLE disk_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, device TEXT NOT NULL, model TEXT, serial TEXT,
    health TEXT NOT NULL, temperature_c INTEGER, power_on_hours INTEGER, reallocated_sectors INTEGER,
    pending_sectors INTEGER, uncorrectable_errors INTEGER, device_alias TEXT, mountpoints TEXT, size_bytes INTEGER
);
INSERT INTO disk_stats (ts, device, model, serial, health, temperature_c, power_on_hours, reallocated_sectors, pending_sectors, uncorrectable_errors, device_alias, mountpoints, size_bytes)
VALUES ('2026-01-01T00:00:00+0000', '/dev/sda', 'M1', 'S1', 'PASSED', 30, 100, 0, 0, 0, 'a1', '/', 111),
       ('2026-01-01T00:00:00+0000', '/dev/sdb', 'M2', 'S2', 'PASSED', 31, 200, 0, 0, 0, 'a2', '/home', 222),
       ('2026-01-02T00:00:00+0000', '/dev/sda', 'M1', 'S1', 'PASSED', 32, 101, 0, 0, 0, 'a1', '/', 111),
       ('2026-01-03T00:00:00+0000', '/dev/sda', 'M1', 'S1', 'PASSED', 33, 102, 0, 0, 0, 'a1', '/', 111);
DELETE FROM disk_stats WHERE id = 4;
SQL
}

@test "миграция: старая БД пересобирается в канонический порядок, данные и id на месте, есть копия" {
  make_old_db

  run_monitor --report
  [ "$status" -eq 0 ]

  run db_query "SELECT group_concat(name, ',') FROM pragma_table_info('disk_stats');"
  [ "$output" = "id,ts,device,device_alias,mountpoints,model,serial,size_bytes,health,temperature_c,power_on_hours,reallocated_sectors,pending_sectors,uncorrectable_errors" ]
  run db_query "PRAGMA user_version;"
  [ "$output" = "1" ]
  run db_query "SELECT id, device, device_alias, mountpoints, size_bytes, temperature_c FROM disk_stats ORDER BY id;"
  [ "$output" = $'1|/dev/sda|a1|/|111|30\n2|/dev/sdb|a2|/home|222|31\n3|/dev/sda|a1|/|111|32' ]
  # счётчик AUTOINCREMENT сохранён (не меньше прежнего 4), индекс пересоздан
  run db_query "SELECT seq FROM sqlite_sequence WHERE name='disk_stats';"
  [ "$output" = "4" ]
  run db_query "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_disk_stats_device_ts';"
  [ "$output" = "1" ]
  # недостающие таблицы созданы
  run db_query "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('cpu_stats','gpu_stats');"
  [ "$output" = "2" ]
  compgen -G "$TMP_DIR/state/system_monitor.db.bak-v0-*" > /dev/null
  run sqlite3 -noheader "$(compgen -G "$TMP_DIR/state/system_monitor.db.bak-v0-*" | head -1)" "SELECT COUNT(*) FROM disk_stats; PRAGMA user_version;"
  [ "$output" = $'3\n0' ]
}

@test "миграция: повторный запуск ничего не меняет и не делает копий" {
  make_old_db
  run_monitor --report
  [ "$status" -eq 0 ]
  before="$(ls "$TMP_DIR/state" | wc -l)"

  run_monitor --report
  [ "$status" -eq 0 ]
  [ "$(ls "$TMP_DIR/state" | wc -l)" = "$before" ]
  run db_query "PRAGMA user_version;"
  [ "$output" = "1" ]
}

@test "новая БД получает user_version = 1 без резервной копии" {
  run_monitor
  [ "$status" -eq 0 ]

  run db_query "PRAGMA user_version;"
  [ "$output" = "1" ]
  run compgen -G "$TMP_DIR/state/system_monitor.db.bak-*"
  [ "$status" -ne 0 ]
}

@test "миграция: --dry-run не меняет старую БД и печатает план" {
  make_old_db
  before="$(sha256sum "$TMP_DIR/state/system_monitor.db")"

  run_monitor --dry-run
  [ "$status" -eq 0 ]
  plan="$output"

  [ "$(sha256sum "$TMP_DIR/state/system_monitor.db")" = "$before" ]
  run compgen -G "$TMP_DIR/state/system_monitor.db.bak-*"
  [ "$status" -ne 0 ]
  [[ "$plan" == *"[dry-run] будет: миграция БД v0 → v1"* ]]
  [[ "$plan" == *"будет: резервная копия"*".bak-v0-"* ]]
  [[ "$plan" == *"таблица disk_stats: 3 строк"* ]]
  [[ "$plan" == *"таблица cpu_stats: нет, будет создана"* ]]
}

@test "миграция: --dry-run без базы сообщает о создании новой базы" {
  run_monitor --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: создана новая база v1"* ]]
  [ ! -e "$TMP_DIR/state/system_monitor.db" ]
}

# ---------------------------------------------------------------------------
# FILE_OWNER / fix_owner (chown стабится, root имитируется SYSTEM_MONITOR_EUID)
# ---------------------------------------------------------------------------

make_chown_stub() {
  CHOWN_LOG="$TMP_DIR/chown.log"
  : > "$CHOWN_LOG"
  cat > "$STUB_DIR/chown" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$CHOWN_LOG"
exit 0
EOF
  chmod +x "$STUB_DIR/chown"
}

seed_own_files() {
  mkdir -p "$TMP_DIR/system_reports"
  touch "$TMP_DIR/state/system_monitor.db.bak-v0-20200101-000000" \
    "$TMP_DIR/logs/system_monitor-2020-01-01-00-00-00.jsonl" \
    "$TMP_DIR/logs/other-2020-01-01-00-00-00.jsonl" \
    "$TMP_DIR/system_reports/system_monitor-2020-01-01-00-00-00.html" \
    "$TMP_DIR/system_reports/foreign.html"
}

@test "FILE_OWNER: не от root chown не вызывается" {
  make_chown_stub
  seed_own_files
  printf 'FILE_OWNER="nobodyx:nogroupx"\n' > "$TMP_DIR/conf/system_monitor.conf"

  run env PATH="$STUB_DIR:$PATH" SYSTEM_MONITOR_EUID=1000 bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 0 ]
  [ ! -s "$CHOWN_LOG" ]
}

@test "FILE_OWNER: от root владелец меняется у БД, логов, отчётов и копий, чужие файлы не трогаются" {
  make_chown_stub
  seed_own_files
  printf 'FILE_OWNER="nobodyx:nogroupx"\n' > "$TMP_DIR/conf/system_monitor.conf"

  run env PATH="$STUB_DIR:$PATH" SYSTEM_MONITOR_EUID=0 bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 0 ]
  grep -q -- "nobodyx:nogroupx .*state/system_monitor.db$" "$CHOWN_LOG"
  grep -q -- "state/system_monitor.db.bak-v0-" "$CHOWN_LOG"
  grep -q -- "logs/system_monitor-2020-01-01" "$CHOWN_LOG"
  grep -q -- "system_reports/system_monitor-2020-01-01" "$CHOWN_LOG"
  grep -q -- "system_reports/latest.html" "$CHOWN_LOG"
  ! grep -q -- "other-" "$CHOWN_LOG"
  ! grep -q -- "foreign.html" "$CHOWN_LOG"
  # chown вызывается с -h (симлинки не разыменовываются)
  ! grep -qv -- "^-h -- " "$CHOWN_LOG"
}

@test "FILE_OWNER: файлы с нужным владельцем не трогаются" {
  make_chown_stub
  seed_own_files
  local me
  me="$(id -un):$(id -gn)"
  printf 'FILE_OWNER="%s"\n' "$me" > "$TMP_DIR/conf/system_monitor.conf"

  run env PATH="$STUB_DIR:$PATH" SYSTEM_MONITOR_EUID=0 bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 0 ]
  [ ! -s "$CHOWN_LOG" ]
}

@test "FILE_OWNER: ошибка chown — только WARN, скрипт не падает" {
  make_chown_stub
  printf '#!/usr/bin/env bash\nexit 1\n' > "$STUB_DIR/chown"
  printf 'FILE_OWNER="nobodyx:nogroupx"\n' > "$TMP_DIR/conf/system_monitor.conf"

  run env PATH="$STUB_DIR:$PATH" SYSTEM_MONITOR_EUID=0 bash "$TMP_DIR/system_monitor.sh"

  [ "$status" -eq 0 ]
  grep -q '"event":"chown_failed"' "$TMP_DIR"/logs/system_monitor-*.jsonl
}

@test "FILE_OWNER: --dry-run печатает план и не вызывает chown" {
  make_chown_stub
  seed_own_files
  printf 'FILE_OWNER="nobodyx:nogroupx"\n' > "$TMP_DIR/conf/system_monitor.conf"

  run env PATH="$STUB_DIR:$PATH" SYSTEM_MONITOR_EUID=0 bash "$TMP_DIR/system_monitor.sh" --dry-run

  [ "$status" -eq 0 ]
  [[ "$output" == *"[dry-run] будет: сменить владельца на nobodyx:nogroupx: "* ]]
  [ ! -s "$CHOWN_LOG" ]
}
