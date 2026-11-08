# `cloud_backup_qnap.sh` — рабочие заметки

Специфика этого скрипта и конкретного QNAP NAS. Общие правила проекта — в
[AI.md](AI.md), здесь не дублируются.

## Окружение и деплой

- QNAP NAS **192.168.88.99** — тот же хост, что отдаёт NFS `/mnt/store`
  (`192.168.88.99:/Public`, nfs4). `/mnt/store/tasks/` на десктопе — рабочий
  каталог скрипта на QNAP; деплой — `cp -f` на этот путь, без SSH.
- **SSH ассистента к QNAP — только по паролю `admin@192.168.88.99` через
  `ssh-askpass-gui`** (окно на каждый вызов — независимые проверки объединять
  в одну SSH-команду). Ключа для `admin` нет. `admin` — uid 0, сеанс получает
  `HOME=/root` (а не `/share/homes/admin` из `/etc/passwd`). QNAP не добавлять
  как SSH-хост удалённой сессии Claude: её `/root/.claude/remote` переполняет
  корень (см. «Корень и `/root`»).
- BusyBox 1.24: нет `sort -h`, `df -B1`, `smartctl`, `nproc`; `base64 -d`
  отказывается разбирать ввод — данные передавать через stdin ssh. `qcli_*`
  требуют `qcli -l` с паролем — изменения хранилища делать через веб-интерфейс
  QTS.
- `docker compose exec`/`logs` на сервере принимают имя **сервиса** из
  `docker-compose.yml` (колонка `SERVICE` в `docker compose ps -a`), а не
  `container_name`. Команды для пользователя сверять именно с `SERVICE`.
- Деплой после правки — только после зелёного CI (тесты и проверки локально не
  запускаются):
  ```sh
  cp -f cloud_backup_qnap.sh /mnt/store/tasks/cloud_backup_qnap.sh \
    && diff -q cloud_backup_qnap.sh /mnt/store/tasks/cloud_backup_qnap.sh \
    && echo "DEPLOY OK"
  ```
  То же для `conf/cloud_backup_qnap.conf.example` при изменении конфига.
  Боевой `conf/cloud_backup_qnap.conf` на `/mnt/store/tasks/` не трогать без
  явной просьбы (gitignored, боевые значения). Перед боевым прогоном —
  `--dry-run` на QNAP с боевым конфигом; dry-run из отдельного каталога: копия
  скрипта + ссылки `conf -> ../conf`, `state -> ../state`,
  `MYTASKS_LOG_DIR=<каталог>/logs`.
- Логи: `/mnt/store/tasks/logs/cloud_backup_qnap-YYYY-MM-DD-HH-MM-SS.jsonl`
  (поля `ts/level/event/msg/detail/rc`; разбор —
  `jq -r '[.ts,.level,.event,.msg,.detail,.rc] | @tsv' <file>`). Число логов
  ограничивает тот же `BACKUP_KEEP_COUNT`, что и число архивов.

## QNAP: аппаратные и ОС-факты

- **4 ядра CPU** (`/sys/devices/system/cpu/online` = `0-3`). `nproc` нет ни в
  BusyBox, ни в Entware — `get_local_nproc()` считает
  `grep -c '^processor' /proc/cpuinfo`. Load ~2–3 на 4 ядрах — не насыщение
  CPU: узкое место искать в однопоточных участках (ssh/wireguard-go) и сети.
- Entware — **минимальная сборка**, arch `aarch64-k3.10`, bind-mount
  `/opt` ← `/share/CACHEDEV1_DATA/Public/entware` (он же
  `/share/Public/entware`). Установлено: `ash bash egrep fgrep find grep nano
  ncat netstat opkg scp sh sqlite3 ssh ssh-agent ssh-keygen ssh-keyscan
  ssh-keysign stat unzstd wg wg-quick wireguard-go xargs xxd zstd zstdcat
  zstdgrep zstdmt`.
  **Нет:** `iostat`, `vmstat`, `top`, `sysstat` и **`timeout`** (ни пакетом,
  ни applet BusyBox). Локальные ограничения по времени — `ncat
  --idle-timeout`/`-w`, фоновый `sleep N && kill` или цикл с лимитом попыток
  (как в raw-transfer); внешний `timeout` вокруг локальной команды даёт
  `rc=127` (так ломался `network_speed_test`). На удалённом сервере (Ubuntu,
  GNU coreutils) `timeout` есть.
- `/bin/sh` — отдельный бинарник, не bash. Шебанг скрипта `#!/opt/bin/bash`;
  в crontab вызывать скрипт через `/opt/bin/bash` или напрямую, не через `sh`.
- Entware не зарегистрирован как QPKG (`/etc/config/qpkg.conf`), штатный
  автозапуск его не поднимает, а bind-mount `/opt` может отвалиться (например,
  после обновления прошивки QTS) — тогда `/opt/bin/bash` нет и cron падает
  молча. `ensure_entware_mount()` в скрипте этого не исправит: без `/opt`
  скрипт не стартует. Поэтому в crontab есть ежеминутная строка на чистом
  `/bin/sh`:
  ```sh
  * * * * * [ -x /opt/bin/opkg ] || mount --bind /share/Public/entware /opt
  ```
  Данные Entware при этом не страдают; ручное восстановление —
  `mkdir -p /opt && mount --bind /share/Public/entware /opt`.

## Cron на QNAP

- Бэкап: `0 4 * * * /opt/bin/bash /share/Public/tasks/cloud_backup_qnap.sh >/dev/null 2>&1`.
- `crond` (BusyBox, без `@reboot`; планировщика «При запуске» в QTS нет)
  запущен как `/usr/sbin/crond -d 9 -c /tmp/cron/crontabs` и читает live-файл
  `/tmp/cron/crontabs/admin` (tmpfs, обнуляется при перезагрузке), а не
  `/etc/config/crontab`. Постоянные строки добавлять **в оба места**:
  `/etc/config/crontab` пересоздаёт live-файл при загрузке. Применить правку —
  `crontab /etc/config/crontab`, затем `/etc/init.d/crond.sh restart`. При
  «cron есть, а не работает» первым делом проверять это.

## Корень и `/root`

- Корень QNAP — `tmpfs` 205 МиБ, пересобирается при загрузке. Штатно занято
  ~184 МиБ: 130 МиБ — образ `/opt/rfs_usr.img` (backing `/dev/loop0` → `/usr`,
  скрыт под bind-mount Entware, `du` его не видит — смотреть через
  `mount --bind / <tmp>`), остальное — QTS. При переполнении LVM не может
  писать `/etc/lvm/cache`.
- Поверх `/root` — отдельный `tmpfs` 8 МиБ (`roottmp`). Переподключение после
  загрузки — ежеминутная строка в crontab, идемпотентная по `/proc/mounts`:
  ```sh
  * * * * * grep -q '^roottmp /root ' /proc/mounts || { d=/tmp/.rootcopy; rm -rf "${d}"; mkdir "${d}" && cp -a /root/. "${d}/" && mount -t tmpfs -o size=8m,mode=0755 roottmp /root && cp -a "${d}/." /root/; rm -rf "${d}"; }
  ```
  Резервная копия crontab до этой правки — `/etc/config/crontab.bak-rootguard`.
  Откат: удалить строку, `crontab /etc/config/crontab`, `umount /root`.
  Защищён только `/root`; `/etc`, `/var` закрыть нельзя — туда пишет QTS.
- DataVol1: пул `tp1` 5,33 ТиБ, тонкий том `lv1` 4,78 ТиБ, ФС 4,745 ТиБ.
  Разницу (~558 ГиБ) занимает резерв снапшотов `snapshot_reserved = 10`
  (`qlvm.conf`, `llreservedP` в `storage_lib.log`) — пока он включён, QTS не
  даёт расширить том. Снапшот — еженедельный (понедельник 00:00), хранится один.

## IO-метрики: `/proc/diskstats` без iostat

- Поле 13 `/proc/diskstats` (1-индексация) — время с незавершёнными I/O, мс;
  на нём `iostat` считает `%util`. `get_local_io_device()`/`get_local_io_raw()`
  читают его напрямую.
- Устройство ФС — через `df -P`: BusyBox `df` без `-P` переносит длинное имя
  (`/dev/mapper/cachedev1`) на отдельную строку.
- `/dev/mapper/cachedev1` в `/proc/diskstats` называется `dm-N`;
  сопоставлять **по major:minor**, не по имени. `stat -c '%t %T'` даёт их в
  hex — переводить `$((16#...))` (пример: `fb 9` → `251 9 dm-9`).

## Метрики прогона и деградация

- `_progress_monitor()`: тик 60 с; каждый 5-й тик — удалённые loadavg/mem и
  RTT (отдельное SSH + ping); каждый 10-й — локальные CPU+RAM+IO одним замером
  (`backup_resource_diag`), чтобы значения относились к одному моменту.
- Средние по прогону (`avg_local_load1`, `avg_remote_load1`, `avg_rtt_ms`,
  `avg_io_util_pct`) и `network_test_mib_s` (результат `network_speed_test`)
  пишутся в `backup_metrics`; по ним выбирается `probable_cause` в
  `backup_degradation` (в т.ч. `network_throughput_limit`). Порог —
  `BACKUP_DEGRADATION_MIBS_THRESHOLD` (по умолчанию 4 МиБ/с).
- Ориентир (сентябрь 2026): ~9 МиБ/с, 23–35 ГБ за 2600–3900 с; `io_util_pct`
  QNAP ~11–15 % — диск не узкое место.
- Сжатие — `zstd -5 --threads=0` на удалённом сервере (`10.66.66.1`); QNAP
  принимает уже сжатый поток, CPU сжатия тратит сервер.

## Raw TCP transfer

Поток `tar|zstd` идёт сырым TCP (`nc`/`ncat`) внутри WireGuard-туннеля вместо
SSH поверх WG — без двойного шифрования, конфиденциальность и целостность
обеспечивает WG. Включён в боевом конфиге (`RAW_TRANSFER_ENABLED=1`), порт
8873.

- Удалённый пайплайн запускается через `ssh_remote`:
  `nohup bash -c 'set -o pipefail; tar ... | zstd ... | timeout <N> nc -l -s <host> <port>; echo $? > <status_file>' </dev/null >/dev/null 2>err_file &`
  — **именно `bash -c`**: `/bin/sh` на Ubuntu — dash, без `pipefail`.
- Клиент на QNAP — цикл подключений (`RAW_TRANSFER_CONNECT_RETRIES`, по
  умолчанию 10, пауза 2 с). Обрыв посреди потока не повторяется — листенера
  уже нет.
- После приёма — обязательный опрос статус-файла на сервере
  (`RAW_TRANSFER_STATUS_POLL_RETRIES`): EOF у `nc` не доказывает, что
  `tar`/`zstd` не упали.
- Фолбэк на SSH-канал при `raw_transfer_nc_missing` или
  `raw_transfer_connect_exhausted` (WARN) безопасен: в `BACKUP_PATH` ещё ничего
  не записано. `detail` у `raw_transfer_connect_exhausted` содержит
  `remote_status=<rc|none>, remote_err=<stderr|none>` из `REMOTE_STATUS_FILE`/
  `REMOTE_ERR_FILE`.
- Правило фаервола — `ufw allow from 10.66.66.0/24 to any port 8873 proto tcp`
  в docker-томе `ufw_data:/etc/ufw` контейнера `esimych-cloud-fail2ban`
  (`network_mode: host`, `NET_ADMIN`); управлять им можно только через
  `docker exec esimych-cloud-fail2ban ufw ...`. При пересборке сервера теряется
  вместе с томом.
- Диагностика по тексту stderr `ncat` (`ncat_rc=1` в обоих случаях):
  - `Ncat: TIMEOUT.` (`remote_status=none, remote_err=none`) — пакеты
    дропаются фаерволом: `docker exec esimych-cloud-fail2ban ufw show added | grep 8873`;
  - `Ncat: Connection refused.` — порт открыт, листенер не поднялся: смотреть
    реальный rc и stderr упавшего `nc`/`tar` в `detail`.
- Стаб ssh в `tests/cloud_backup_qnap.bats`: `RAW_LAUNCH_CMD` сам начинается с
  `rm -f '...'`, поэтому ветка `*"rm -f '"*` срабатывает и на запуск
  листенера, не только на зачистку.

## WireGuard и SSH к серверу

- `REMOTE_HOST="10.66.66.1"` — адрес сервера внутри туннеля. Необязательный
  `WG_ENDPOINT` в `conf/cloud_backup_qnap.conf` заменяет `Endpoint` из
  `/opt/etc/wireguard/<WG_INTERFACE>.conf`; выбор пишется событием
  `wg_endpoint_selected`.
- На сервере в `/etc/wireguard/wg0.conf` должны быть оба пира: десктоп
  `10.66.66.2` и QNAP `10.66.66.3`.
- Если туннель не поднимается без единой строки в логах сервера, а на QNAP
  `tx_bytes>0`, `rx_bytes=0` — проверить `[Peer].PublicKey` в конфиге QNAP:
  сервер молча отбрасывает handshake с неверным ключом (штатно для WG).
  Диагностика — `tcpdump` на сервере при ручном подъёме туннеля с QNAP.
- Ключ QNAP (`admin@EsimychStore-1`) должен быть в `authorized_keys` root на
  сервере. После смены host key сервера `ssh_remote()` падает на каждом шаге —
  в т.ч. ложно выглядит как `network_speed_test_skip_no_nc`/
  `raw_transfer_nc_missing`. Новый отпечаток сверять с десктопом и
  `ssh-keyscan`, старые записи — `ssh-keygen -R`.

## Шаги на сервере

- Запуск сервисов после бэкапа повторяется до `SERVICES_START_RETRIES` раз с
  проверкой `docker compose ps -a`; окончательная ошибка даёт ненулевой код
  скрипта.
- `occ` — через `docker compose exec -T` и сервис `NEXTCLOUD_SERVICE_NAME`
  (по умолчанию `nextcloud`), перед этим событие `nextcloud_preflight`.
- `files_versions` и `files_trashbin` на сервере отключены осознанно
  (решение пользователя): версии и корзина не используются, поэтому очистки
  `occ versions:cleanup`/`trashbin:cleanup` в скрипте нет — в лог пишутся
  `occ_versions_skipped`/`occ_trashbin_skipped`. Если приложения снова
  включат (`occ app:enable files_versions files_trashbin`), шаги очистки
  придётся вернуть в скрипт отдельно.
- Оптимизация MariaDB — раз в `DB_OPTIMIZE_INTERVAL_DAYS` (флаг
  `DB_OPTIMIZE_DUE`).
- `json_escape`: управляющие байты задаются явным списком octal-кодов — с
  диапазонами QNAP-реализация `tr` удаляла дефисы.
