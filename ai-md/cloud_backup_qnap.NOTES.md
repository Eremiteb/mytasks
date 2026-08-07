# `cloud_backup_qnap.sh` — working notes / chat handoff

Накопленный опыт по этому скрипту за несколько сессий диагностики и доработки.
Не дублирует общие правила проекта (см. [AI.md](AI.md)) — только специфика
именно этого скрипта и конкретной QNAP-железки.

## Окружение

- QNAP NAS по адресу **192.168.88.99** — это тот же хост, что отдаёт NFS-шару
  `/mnt/store` (`192.168.88.99:/Public`, nfs4). Поэтому `/mnt/store/tasks/`
  на десктопе == реальная директория скрипта на QNAP; деплой — обычный
  `cp -f` на этот путь, без SSH.
- **У ассистента нет прямого SSH-доступа к самой QNAP.** Все живые проверки
  (df, stat, cat /proc/diskstats, crontab -l, type/command -v и т.п.)
  выполняет пользователь сам и вставляет вывод в чат. Это рабочий паттерн —
  не пытаться SSH'иться самостоятельно, а формулировать точные команды для
  пользователя.
- Деплой-процедура после каждой правки:
  ```sh
  bash -n cloud_backup_qnap.sh && shellcheck -S error cloud_backup_qnap.sh \
    && bats --print-output-on-failure tests/cloud_backup_qnap.bats tests/all_scripts_syntax.bats \
    && cp -f cloud_backup_qnap.sh /mnt/store/tasks/cloud_backup_qnap.sh \
    && diff -q cloud_backup_qnap.sh /mnt/store/tasks/cloud_backup_qnap.sh \
    && bash -n /mnt/store/tasks/cloud_backup_qnap.sh && echo "DEPLOY OK"
  ```
  То же самое для `conf/cloud_backup_qnap.conf.example` при изменении конфига
  (реальный `conf/cloud_backup_qnap.conf` на `/mnt/store/tasks/` не трогать
  без явной просьбы — он гитигнорится и содержит боевые значения).
- Логи прода: `/mnt/store/tasks/logs/cloud_backup_qnap-YYYY-MM-DD-HH-MM-SS.jsonl`
  (одна строка = один JSON-объект, поля `ts/level/event/msg/detail/rc`,
  разобрать через `jq -r '[.ts,.level,.event,.msg,.detail,.rc] | @tsv' <file>`).

## QNAP: аппаратные/ОС факты

- **Одно ядро CPU** (`local_nproc=1` в каждом прогоне) — это ключевое и
  практически неустранимое узкое место всей передачи (zstd-компрессия на
  единственном ядре). Почти все находки по производительности так или иначе
  об этом.
- Entware на этом QNAP — **минимальная сборка**, аrch `aarch64-k3.10`,
  bind-mount `/opt` ← `/share/CACHEDEV1_DATA/Public/entware`. Установленные
  пакеты (проверено вживую): `ash bash egrep fgrep find grep nano ncat
  netstat opkg scp sh sqlite3 ssh ssh-agent ssh-keygen ssh-keyscan
  ssh-keysign stat unzstd wg wg-quick wireguard-go xargs xxd zstd zstdcat
  zstdgrep zstdmt`.
  **Важно, чего тут НЕТ:** `iostat`, `vmstat`, `top`, `sysstat`, и —
  неочевидно — **`timeout`** (ни как Entware-пакет, ни как applet системного
  busybox; подтверждено `type timeout` → `not found`). Любой новый код,
  который хочет что-то замерить или ограничить по времени локально на QNAP,
  должен исходить из этого списка, а не считать coreutils доступным.
- `/bin/sh` на QNAP — настоящий отдельный 965048-байтный бинарник (не
  symlink на bash). Скрипт имеет шебанг `#!/opt/bin/bash`; вызов через
  явный `sh script.sh` в crontab (как было раньше настроено у пользователя)
  игнорирует шебанг. **Это тем не менее НЕ подтверждённая причина каких-либо
  реальных сбоев** — см. раздел про `ncat_rc=127` ниже, где эта гипотеза была
  выдвинута, а затем опровергнута логами. Рекомендация вызывать скрипт
  напрямую (без `sh`) в crontab остаётся хорошей практикой сама по себе, но
  не как объяснение конкретного бага.

## IO-метрики: `/proc/diskstats` без iostat/vmstat

Поля `/proc/diskstats` (1-индексация): 1=major, 2=minor, 3=имя устройства,
4=reads completed, 5=reads merged, 6=sectors read, 7=time reading(ms),
8=writes completed, 9=writes merged, 10=sectors written, 11=time writing(ms),
12=I/Os in progress, **13=time spent doing I/Os(ms)** — это та же величина,
на которой `iostat` считает `%util` (доля времени с ≥1 незавершённой
операцией).

`get_local_io_device()`/`get_local_io_raw()` в скрипте читают это напрямую,
без установки пакетов. Два прошлых бага и их фиксы:

1. **BusyBox `df` без `-P`** переносит длинное имя устройства
   (`/dev/mapper/cachedev1`, 21 символ) на отдельную строку — наивный
   `awk 'NR==2{print $1}'` подхватывал не то поле. Фикс: `df -P` (POSIX,
   всегда одна строка).
2. **Несовпадение имён**: `/dev/mapper/cachedev1` — настоящий device-mapper
   узел (`readlink -f` возвращает его же самого, это не symlink), но в
   `/proc/diskstats` та же физическая сущность зарегистрирована под другим
   именем, `dm-N`. Единственный надёжный способ сопоставить — **по
   major:minor**, не по имени. `stat -c '%t %T' <path>` даёт major:minor в
   **hex**, нужно `$((16#...))` перед сравнением с диск-статс (там decimal).
   Проверено вживую: `cachedev1` → `stat` → `fb 9` (hex) → major=251,
   minor=9 → совпадает с `/proc/diskstats` строкой `251 9 dm-9 ...`.

Текущая (рабочая, задеплоенная) реализация — `get_local_io_device()` в
`cloud_backup_qnap.sh` использует именно `df -P` + major:minor. В логе от
2026-08-05 `io_device=dm9` резолвится верно на всех прогонах, `io_util_pct`
стабильно в районе 11–15% — IO не является узким местом.

## Совмещённый замер CPU/RAM/IO — `backup_resource_diag`

По явному запросу пользователя все три метрики снимаются **одновременно**,
раз в ~10 минут (не раздельно с разной частотой) — чтобы значения относились
к одному и тому же моменту. Реализовано в `_progress_monitor()`:
тик каждые 60 сек, каждый 5-й тик — дополнительно remote diag (loadavg/mem
удалённого сервера) + RTT (отдельное SSH-соединение + ping, поэтому не на
каждом тике), каждый 10-й тик (подмножество 5-минутных) — локальный
CPU+RAM+IO одним замером, событие `backup_resource_diag`. Средние по всему
прогону (`avg_local_load1`, `avg_remote_load1`, `avg_rtt_ms`,
`avg_io_util_pct`) попадают в `backup_metrics` в конце и используются для
`probable_cause` в `backup_degradation`.

## Raw TCP transfer (обход двойного шифрования) — уже реализовано и живёт в проде

Полный план см. в `/home/esimych/.claude/plans/zippy-sleeping-wolf.md` (если
ещё существует) — здесь только суть и текущий статус.

**Мотивация:** узкое место — CPU QNAP насыщается двойным шифрованием
(WireGuard AEAD + SSH поверх него для потока `tar|zstd`). Замена SSH-канала
на сырой TCP-сокет (`nc`/`ncat`) **внутри уже зашифрованного** WG-туннеля не
теряет конфиденциальность/целостность.

**Статус: реализовано, задеплоено, включено (`RAW_TRANSFER_ENABLED=1` в
боевом конфиге), подтверждено рабочим в проде** (лог 2026-08-05: чистый
`raw_transfer_listener_launch` → `raw_transfer_status_fetch rc=0` без единого
`raw_transfer_connect_retry`).

Ключевые элементы дизайна (код — `cloud_backup_qnap.sh`, блок
~строка 1182+):
- Удалённый пайплайн запускается через `ssh_remote` командой вида
  `nohup bash -c 'set -o pipefail; tar ... | zstd ... | timeout <N> nc -l -s
  <host> <port>; echo $? > <status_file>' </dev/null >/dev/null 2>err_file &`
  — **явно `bash -c`, не `sh -c`**, т.к. `/bin/sh` на Ubuntu (удалённый
  сервер) — dash и не умеет `set -o pipefail`. На удалённой (Ubuntu) стороне
  `timeout` есть (GNU coreutils) — там его использовать безопасно, в отличие
  от локальной QNAP-стороны.
- Локальный клиент — цикл подключений с ретраями
  (`RAW_TRANSFER_CONNECT_RETRIES`, по умолчанию 10, sleep 2 между попытками).
  Обрыв **в середине** потока (байты уже росли) не ретраится — листенера
  больше нет, шанса нет.
- После успешного локального приёма — обязательный опрос статус-файла на
  удалённой стороне (`RAW_TRANSFER_STATUS_POLL_RETRIES`), т.к. чистый EOF у
  `nc` не доказывает, что `tar`/`zstd` выше по пайпу не упали.
- Фолбэк на сегодняшний SSH-путь: если на удалённой стороне нет `nc`
  (`raw_transfer_nc_missing`, WARN) или не удалось подключиться после всех
  попыток (`raw_transfer_connect_exhausted`, WARN) — безопасно, т.к. в
  `BACKUP_PATH` в этом случае ничего не записано.
- Firewall: правило добавлено через контейнер `esimych-cloud-fail2ban`
  (`docker exec esimych-cloud-fail2ban ufw allow ...`) — `ufw` на самом
  удалённом хосте управляется только оттуда (`network_mode: host`,
  `cap_add: NET_ADMIN`, том `ufw_data:/etc/ufw`).
- Эффект в проде: скромный, но положительный прирост скорости (5.05 → 5.12
  МиБ/с между 08-04 и 08-05), `avg_local_load1` тоже чуть ниже (2.51 → 2.23).
  **CPU всё равно остаётся насыщен** (`nproc=1`) — `backup_degradation` с
  `probable_cause=local_cpu_or_io_contention` продолжает срабатывать почти
  каждый прогон, и это ожидаемо: raw-transfer убрал только слой
  SSH-шифрования, а не саму нехватку ядра под zstd. Дальнейшее ускорение
  потребует либо более быстрого/слабого сжатия, либо другого железа.

## `network_speed_test_failed ncat_rc=127` — история диагностики (важно не наступить повторно)

Дважды подряд (логи 2026-08-04 и 2026-08-05) один и тот же симптом:
`network_speed_test_failed`, `bytes=0, duration_s=1, ncat_rc=127`.

**Первая гипотеза (2026-08-04, ОШИБОЧНАЯ):** crontab вызывал скрипт через
`sh /share/Public/tasks/cloud_backup_qnap.sh`, что игнорирует шебанг
`#!/opt/bin/bash` и должно было исполнять его под системным `/bin/sh`
(busybox-подобный бинарник, не bash). Была задокументирована в README как
вероятная причина.

**Опровержение (2026-08-05):** тот же лог, где `ncat_rc=127` снова
воспроизвёлся, **также содержит** полностью успешный прогон raw-transfer,
который использует bash-специфичный синтаксис (`[[ ]]`) в той же ветке
исполнения. Под настоящим `sh`/dash такой синтаксис вызвал бы немедленную
синтаксическую ошибку и обвалил бы весь скрипт целиком — а он доехал до
`backup_done`/`wg_down_ok`. Значит, скрипт совершенно точно исполняется под
bash, и гипотеза про `sh` в crontab не объясняет этот баг (хотя как общая
практика — не вызывать через `sh` — осталась разумной рекомендацией).

**Настоящая причина (найдена и подтверждена вживую 2026-08-05):**
`measure_network_speed()` — **единственное место во всём скрипте**, где
локальный `ncat` на QNAP оборачивался во внешний `timeout 60 ...`. На этой
QNAP бинаря `timeout` просто нет (см. раздел про Entware выше — подтверждено
`command -v timeout` / `which timeout` / `type timeout`, все пустые/`not
found`). `rc=127` от `timeout X` в GNU-семантике означает «не найден X,
переданный timeout» — то есть падал сам `timeout`, а не `ncat`. Раз-transfer
клиентский `ncat`-вызов **не** оборачивался в `timeout` и поэтому всегда
работал нормально — это и было ключевой уликой, которая привела к разгадке.

**Фикс (задеплоен 2026-08-05):** заменена внешняя обёртка на встроенный
idle-таймаут самого `ncat`:
```sh
# было:
timeout 60 ncat --recv-only "${REMOTE_HOST}" "${RAW_TRANSFER_PORT}" > "${test_out}" ...
# стало:
ncat --recv-only --idle-timeout 60s "${REMOTE_HOST}" "${RAW_TRANSFER_PORT}" > "${test_out}" ...
```
Не требует отдельного бинаря, даёт ту же защиту от зависания (закрывает
соединение, если нет данных дольше 60с). Удалённая сторона (Ubuntu, есть
`timeout`) не менялась — там всё было в порядке.

**Проверить в следующем логе:** событие `network_speed_test` (успех) или
`network_speed_test_failed` с другим `ncat_rc` — если снова `127`, гипотеза
про `timeout` тоже неверна и нужна новая итерация диагностики тем же живым
способом (просить пользователя выполнить команды на QNAP и прислать вывод).

## Открытые вопросы / что смотреть дальше

- Следующий ночной прогон после 2026-08-05 — подтвердить, что
  `network_speed_test` больше не падает с `ncat_rc=127`.
  - **Обновление 2026-08-05 (позже в тот же день):** пользователь запускал
    диагностику; `command -v timeout` / `which timeout` / `type timeout` на
    QNAP подтвердили отсутствие бинаря. Фикс на `--idle-timeout` уже
    задеплоен — ждём лог, где это видно в бою.
- Тренд `avg_mib_s`/`avg_local_load1` по прогонам с raw-transfer (нужно
  накопить больше ночей, чем один прогон, для уверенного вывода):
  - 2026-07-29 (SSH, до смены шифра): avg_local_load1=1.77, avg_mib_s=4.65
  - 2026-07-30 (SSH, после смены приоритета SSH_CIPHERS): 1.82 / 5.01
  - 2026-08-04 (raw-transfer включён, но `io_device=n/a` баг): load ~2.51,
    avg_mib_s=5.05
  - 2026-08-05 (raw-transfer + io_device фикс): load=2.23, avg_mib_s=5.12
  - Общий вывод пока предварительный: raw-transfer дал небольшой, но
    стабильный прирост; CPU (1 ядро) остаётся доминирующим ограничением.
- Если в будущем понадобится ещё один локальный таймаут/лимит на QNAP —
  **не использовать внешний `timeout`**, он отсутствует. Варианты: `ncat
  --idle-timeout`/`-w`, фоновый `sleep N && kill` вручную, или явный
  retry-loop с ограничением попыток (как в raw-transfer).
