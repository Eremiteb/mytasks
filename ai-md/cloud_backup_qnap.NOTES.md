# `cloud_backup_qnap.sh` — рабочие заметки

Специфика скрипта и QNAP NAS. Общие правила — в [AI.md](AI.md). Опции, параметры, режим dry-run и описание передачи — [README.md](../README.md), раздел `cloud_backup_qnap.sh`; здесь только то, чего там нет.

## Окружение и деплой

- QNAP (адрес — `~/.config/ai-context/machine-notes.md`) — тот же хост, что отдаёт NFS `/mnt/store` (`<QNAP>:/Public`, nfs4). `/mnt/store/tasks/` — рабочий каталог скрипта на QNAP; деплой — `cp -f`, без SSH.
- SSH ассистента к QNAP:
  - ключ `~/.ssh/id_ed25519_qnap` (с 2026-11-14, в `~/.ssh/config` для адреса QNAP), без парольной фразы, права 600;
  - пароль учётной записи QNAP через `ssh-askpass-gui` (вход по паролю оставлен включённым); окно на каждый вызов — независимые проверки объединять в одну SSH-команду.
- Публичный ключ — `/etc/config/ssh/authorized_keys` (700/600, постоянный том; `/root/.ssh` → `/etc/config/ssh`). `/share/homes/admin/.ssh` sshd не читает.
- `admin` — uid 0, сеанс получает `HOME=/root` (не `/share/homes/admin`).
- QNAP не добавлять как SSH-хост удалённой сессии Claude: её `/root/.claude/remote` переполняет корень (см. «Корень и `/root`»).
- BusyBox 1.24: нет `sort -h`, `df -B1`, `smartctl`, `nproc`. `base64 -d` не разбирает ввод — данные передавать через stdin ssh.
- `qcli_*` требуют `qcli -l` с паролем — изменения хранилища делать через веб-интерфейс QTS.
- `docker compose exec`/`logs` на сервере принимают имя **сервиса** из `docker-compose.yml` (колонка `SERVICE` в `docker compose ps -a`), не `container_name`.
- Деплой — только после зелёного CI (локально тесты не запускаются):
  ```sh
  cp -f cloud_backup_qnap.sh /mnt/store/tasks/cloud_backup_qnap.sh \
    && diff -q cloud_backup_qnap.sh /mnt/store/tasks/cloud_backup_qnap.sh \
    && echo "DEPLOY OK"
  ```
  То же для `conf/cloud_backup_qnap.conf.example`. Боевой `conf/cloud_backup_qnap.conf` (gitignored) на `/mnt/store/tasks/` не трогать без явной просьбы.
- Перед боевым прогоном — `--dry-run` на QNAP с боевым конфигом; dry-run — из отдельного каталога: копия скрипта + `conf -> ../conf`, `state -> ../state`, `MYTASKS_LOG_DIR=<каталог>/logs`.
- Логи: `/mnt/store/tasks/logs/cloud_backup_qnap-YYYY-MM-DD-HH-MM-SS.jsonl` (поля `ts/level/event/msg/detail/rc`; разбор — `jq -r '[.ts,.level,.event,.msg,.detail,.rc] | @tsv' <file>`). Число логов ограничивает `BACKUP_KEEP_COUNT`, как и число архивов.

## QNAP: железо и ОС

- **4 ядра** (`/sys/devices/system/cpu/online` = `0-3`). `nproc` нет — `get_local_nproc()` считает `grep -c '^processor' /proc/cpuinfo`. Load ~2–3 на 4 ядрах — не насыщение CPU: узкое место искать в однопоточных участках (ssh/wireguard-go) и сети.
- Entware — минимальная сборка, arch `aarch64-k3.10`, bind-mount `/opt` ← `/share/CACHEDEV1_DATA/Public/entware` (= `/share/Public/entware`). Установлено: `ash bash egrep fgrep find grep nano ncat netstat opkg scp sh sqlite3 ssh ssh-agent ssh-keygen ssh-keyscan ssh-keysign stat unzstd wg wg-quick wireguard-go xargs xxd zstd zstdcat zstdgrep zstdmt`.
- **Нет:** `iostat`, `vmstat`, `top`, `sysstat`, **`timeout`** (ни пакетом, ни applet). Ограничения по времени — `ncat --idle-timeout`/`-w`, фоновый `sleep N && kill` или цикл с лимитом попыток. Внешний `timeout` даёт `rc=127` (так ломался `network_speed_test`). На удалённом сервере (Ubuntu, GNU coreutils) `timeout` есть.
- `/bin/sh` — не bash. Шебанг `#!/opt/bin/bash`; в crontab — через `/opt/bin/bash` или напрямую, не через `sh`.
- Entware не зарегистрирован как QPKG; после обновления прошивки bind-mount `/opt` может отвалиться → нет `/opt/bin/bash`, cron падает молча. `ensure_entware_mount()` без `/opt` не работает. Поэтому в crontab — ежеминутная строка на чистом `/bin/sh`:
  ```sh
  * * * * * [ -x /opt/bin/opkg ] || mount --bind /share/Public/entware /opt
  ```
  Данные Entware не страдают; ручное восстановление — `mkdir -p /opt && mount --bind /share/Public/entware /opt`.

## Cron на QNAP

- Бэкап: `0 4 * * * /opt/bin/bash /share/Public/tasks/cloud_backup_qnap.sh >/dev/null 2>&1`.
- `crond` (BusyBox, без `@reboot`; «При запуске» в QTS нет) запущен как `/usr/sbin/crond -d 9 -c /tmp/cron/crontabs` и читает live-файл `/tmp/cron/crontabs/admin` (tmpfs, обнуляется при перезагрузке), не `/etc/config/crontab`. Постоянные строки — **в оба места** (`/etc/config/crontab` пересоздаёт live-файл при загрузке). Применить: `crontab /etc/config/crontab`, затем `/etc/init.d/crond.sh restart`. «cron есть, а не работает» — сначала проверять это.

## Корень и `/root`

- Корень QNAP — `tmpfs` 205 МиБ, пересобирается при загрузке; штатно занято ~184 МиБ: 130 МиБ — `/opt/rfs_usr.img` (loop0 → `/usr`, под bind-mount Entware; `du` не видит — смотреть `mount --bind / <tmp>`), остальное — QTS. При переполнении LVM не пишет `/etc/lvm/cache`.
- Поверх `/root` — `tmpfs` 8 МиБ (`roottmp`). Переподключение — ежеминутная строка в crontab, идемпотентная по `/proc/mounts`:
  ```sh
  * * * * * grep -q '^roottmp /root ' /proc/mounts || { d=/tmp/.rootcopy; rm -rf "${d}"; mkdir "${d}" && cp -a /root/. "${d}/" && mount -t tmpfs -o size=8m,mode=0755 roottmp /root && cp -a "${d}/." /root/; rm -rf "${d}"; }
  ```
  Бэкап crontab до правки — `/etc/config/crontab.bak-rootguard`. Откат: удалить строку, `crontab /etc/config/crontab`, `umount /root`. Защищён только `/root`; `/etc` и `/var` закрыть нельзя — туда пишет QTS.
- DataVol1: пул `tp1` 5,33 ТиБ, тонкий том `lv1` 4,78 ТиБ, ФС 4,745 ТиБ. Разницу ~558 ГиБ занимает резерв снапшотов `snapshot_reserved = 10` (`qlvm.conf`, `llreservedP` в `storage_lib.log`); пока он включён, QTS не даёт расширить том. Снапшот — еженедельный (понедельник 00:00), хранится один.

## IO-метрики без iostat

- Поле 13 `/proc/diskstats` (1-индексация) — мс с незавершёнными I/O; на нём `iostat` считает `%util`. `get_local_io_device()`/`get_local_io_raw()` читают напрямую.
- Устройство ФС — через `df -P` (BusyBox `df` без `-P` переносит длинное имя на отдельную строку).
- `/dev/mapper/cachedev1` в `/proc/diskstats` — `dm-N`; сопоставлять **по major:minor**, не по имени. `stat -c '%t %T'` даёт hex — переводить `$((16#...))` (пример: `fb 9` → `251 9 dm-9`).

## Метрики прогона и деградация

- `_progress_monitor()`: тик 60 с; каждый 5-й — удалённые loadavg/mem и RTT (отдельное SSH + ping); каждый 10-й — локальные CPU+RAM+IO одним замером (`backup_resource_diag`), чтобы значения относились к одному моменту.
- Средние (`avg_local_load1`, `avg_remote_load1`, `avg_rtt_ms`, `avg_io_util_pct`) и `network_test_mib_s` — в `backup_metrics`; по ним `probable_cause` в `backup_degradation` (в т.ч. `network_throughput_limit`). Порог — `BACKUP_DEGRADATION_MIBS_THRESHOLD` (4 МиБ/с).
- Ориентир (сентябрь 2026): ~9 МиБ/с, 23–35 ГБ за 2600–3900 с; `io_util_pct` QNAP ~11–15 % — диск не узкое место.
- Сжатие — `zstd -5 --threads=0` на удалённом сервере (сервер esimych-cloud в туннеле WG); QNAP принимает уже сжатый поток.

## Raw TCP

- Поток `tar|zstd` — сырым TCP (`nc`/`ncat`) внутри WireGuard, порт 8873 (`RAW_TRANSFER_ENABLED=1` в боевом конфиге). WG даёт конфиденциальность и целостность; двойного шифрования нет.
- Удалённый пайплайн — через `ssh_remote`:
  `nohup bash -c 'set -o pipefail; tar ... | zstd ... | timeout <N> nc -l -s <host> <port>; echo $? > <status_file>' </dev/null >/dev/null 2>err_file &`
  — **именно `bash -c`**: `/bin/sh` на Ubuntu — dash, без `pipefail`.
- Удалённая сторона — `nc -N -l ...`: без `-N` OpenBSD nc висит до `RAW_TRANSFER_REMOTE_TIMEOUT_SEC` (часы). QNAP — `ncat --recv-only ... < /dev/null`.
- Клиент на QNAP — цикл подключений (`RAW_TRANSFER_CONNECT_RETRIES`, 10 попыток по 2 с). Обрыв посреди потока не повторяется: листенера уже нет.
- Обязательный опрос статус-файла на сервере (`RAW_TRANSFER_STATUS_POLL_RETRIES`): EOF у `nc` не доказывает, что `tar`/`zstd` не упали.
- Фолбэк на SSH при `raw_transfer_nc_missing` или `raw_transfer_connect_exhausted` (WARN) безопасен: в `BACKUP_PATH` ещё ничего не записано. `detail` у `raw_transfer_connect_exhausted` — `remote_status=<rc|none>, remote_err=<stderr|none>`.
- Правило фаервола: `ufw allow from <сеть WG> to any port 8873 proto tcp` (`<сеть WG>` — значение в `~/.config/ai-context/machine-notes.md`) в docker-томе `ufw_data:/etc/ufw` контейнера `esimych-cloud-fail2ban` (`network_mode: host`, `NET_ADMIN`); управлять только через `docker exec esimych-cloud-fail2ban ufw ...`. При пересборке сервера теряется вместе с томом.
- Диагностика по stderr `ncat` (`ncat_rc=1`):
  - `Ncat: TIMEOUT.` (`remote_status=none, remote_err=none`) — пакеты дропаются фаерволом: `docker exec esimych-cloud-fail2ban ufw show added | grep 8873`;
  - `Ncat: Connection refused.` — порт открыт, листенер не поднялся: смотреть rc и stderr упавшего `nc`/`tar` в `detail`.
- Стаб ssh в `tests/cloud_backup_qnap.bats`: `RAW_LAUNCH_CMD` начинается с `rm -f '...'`, поэтому ветка `*"rm -f '"*` срабатывает и на запуск листенера.

## WireGuard и SSH к серверу

- `REMOTE_HOST="<сервер esimych-cloud в туннеле WG>"` — адрес сервера в туннеле (значение — `~/.config/ai-context/machine-notes.md`). `WG_ENDPOINT` (необязательный) заменяет `Endpoint` из `/opt/etc/wireguard/<WG_INTERFACE>.conf`; выбор — событие `wg_endpoint_selected`.
- На сервере в `/etc/wireguard/wg0.conf` — оба пира: пир рабочего ПК и пир QNAP (адреса — `~/.config/ai-context/machine-notes.md`).
- Туннель не поднимается, в логах сервера тишина, на QNAP `tx_bytes>0`, `rx_bytes=0` — проверить `[Peer].PublicKey` в конфиге QNAP: сервер молча отбрасывает handshake с неверным ключом (штатно для WG). Диагностика — `tcpdump` на сервере при ручном подъёме с QNAP.
- Ключ QNAP (`admin@EsimychStore-1`) — в `authorized_keys` root на сервере.
- После смены host key сервера `ssh_remote()` падает на каждом шаге, в т.ч. ложно как `network_speed_test_skip_no_nc`/`raw_transfer_nc_missing`. Новый отпечаток — сверять с десктопом и `ssh-keyscan`; старые записи — `ssh-keygen -R`.

## Шаги на сервере

- Запуск сервисов после бэкапа повторяется до `SERVICES_START_RETRIES` раз с проверкой `docker compose ps -a`; окончательная ошибка — ненулевой код скрипта.
- `occ` — через `docker compose exec -T` и сервис `NEXTCLOUD_SERVICE_NAME` (по умолчанию `nextcloud`), перед этим событие `nextcloud_preflight`.
- `files_versions` и `files_trashbin` отключены осознанно (решение пользователя): поэтому очистки `occ versions:cleanup`/`trashbin:cleanup` нет — в лог пишутся `occ_versions_skipped`/`occ_trashbin_skipped`. Если приложения включат снова (`occ app:enable files_versions files_trashbin`), шаги очистки вернуть в скрипт отдельно.
- Оптимизация MariaDB — раз в `DB_OPTIMIZE_INTERVAL_DAYS` (флаг `DB_OPTIMIZE_DUE`).
- `json_escape`: управляющие байты — явным списком octal-кодов: диапазоны в QNAP-реализации `tr` удаляли дефисы.
