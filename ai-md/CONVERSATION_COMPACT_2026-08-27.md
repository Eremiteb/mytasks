# Compact беседы: `cloud_backup_qnap.sh` и аудит `mytasks`

Обновлено: 2026-08-27 20:11 +0500.

## Как продолжать работу

- Рабочий каталог: `/home/esimych/esimych-docs/mytasks`.
- Перед изменениями читать `AGENTS.md` и `ai-md/AI.md`.
- Основной объект этой беседы — `cloud_backup_qnap.sh`; не смешивать его с
  `system_monitor.sh` и другими задачами без явного запроса пользователя.
- У ассистента нет прямого SSH к QNAP. Production-файлы доступны через
  `/mnt/store/tasks/`; удалённый сервер `10.66.66.1` при необходимости
  обслуживается отдельно.
- Graphify-граф создан в `graphify-out/` и исключён из Git правилом
  `graphify-out/` в `.gitignore`. Перед ручным поиском по коду использовать
  граф, после изменений кода или конфигурации обновлять его командой
  `graphify update .`. Последнее обновление выполнено Graphify 0.9.48
  2026-08-27: 312 узлов, 468 рёбер, 23 сообщества.
- Любое изменение shell-скрипта проверять командами:

  ```sh
  bash -n path/to/script.sh
  shellcheck -S style path/to/script.sh
  shellcheck -o all path/to/script.sh
  bats --print-output-on-failure tests
  ```

- Предупреждения ShellCheck исправлять структурно, не отключать комментариями.
- Рабочие `conf/*.conf` содержат локальные значения и не коммитятся; в Git
  хранить только `*.conf.example`.

## Текущее production-состояние QNAP-бэкапа

Production:

- скрипт: `/mnt/store/tasks/cloud_backup_qnap.sh`;
- конфиг: `/mnt/store/tasks/conf/cloud_backup_qnap.conf`;
- шаблон: `/mnt/store/tasks/conf/cloud_backup_qnap.conf.example`;
- логи: `/mnt/store/tasks/logs/cloud_backup_qnap-*.jsonl`;
- архивы: `/share/Public/backups` на QNAP.

Ключевые параметры production-конфига на момент compact:

```sh
WG_INTERFACE="wg0-qnap"
WG_ENDPOINT="109.235.117.160:59347"
REMOTE_HOST="10.66.66.1"
OPTIMIZE_REDIS_BEFORE_BACKUP="0"
REDIS_SERVICE_NAME="valkey"
BACKUP_KEEP_COUNT="10"
BACKUP_DEGRADATION_MIBS_THRESHOLD="4"
```

`REMOTE_HOST` — внутренний адрес сервера в WireGuard. При смене публичного IP
его менять нельзя; внешний адрес задаётся через `WG_ENDPOINT`.

Локальный WireGuard-клиент был проверен 2026-08-27:

```text
interface: wg0-client
local address: 10.66.66.2
endpoint: 109.235.117.160:59347
allowed ips: 10.66.66.0/24
route: 10.66.66.1 dev wg0-client src 10.66.66.2
ping 10.66.66.1: успешно, около 3 мс
```

Приватный и preshared-ключи при проверке не выводились.

## Реализованные изменения `cloud_backup_qnap.sh`

- Передача архива может идти raw TCP внутри WireGuard
  (`RAW_TRANSFER_ENABLED=1`) без дополнительного SSH-шифрования.
- Удалённый пайплайн выполняет `tar | zstd -3 --threads=0 -c`; fallback —
  `pigz -1`, затем `gzip -1`.
- `tar` и компрессор работают на `10.66.66.1`; QNAP принимает готовый поток.
- SQLite-оптимизация полностью удалена.
- MariaDB и Redis/Valkey оптимизации опциональны и ограничены интервалом;
  Redis/Valkey AOF-оптимизация в production выключена после двух таймаутов по
  1800 секунд.
- Compose-сервис Redis заменён на Valkey, актуальный ключ — `valkey`.
- Логи Valkey сохраняются перед `docker compose down` событием
  `redis_logs_capture`.
- Nextcloud `occ` выполняется через Compose-сервис `nextcloud`, а не через
  изменяемый `container_name`.
- Запуск сервисов после бэкапа повторяется и проверяет состояния контейнеров;
  окончательная ошибка делает весь запуск неуспешным.
- Число архивов и JSONL-логов регулируется одним `BACKUP_KEEP_COUNT`.
- Сетевой тест сохраняется в `backup_metrics` как `network_test_mib_s`;
  возможная причина деградации — `network_throughput_limit`.
- Добавлен `WG_ENDPOINT`: непустое значение из backup-конфига переопределяет
  `Endpoint` из `/opt/etc/wireguard/<WG_INTERFACE>.conf`. Выбор логируется как
  `wg_endpoint_selected` с `source=backup_config` или `wireguard_config`.

Резервные копии production-конфига:

```text
/mnt/store/tasks/conf/cloud_backup_qnap.conf.bak-20260822-valkey
/mnt/store/tasks/conf/cloud_backup_qnap.conf.bak-20260827-endpoint
```

## Последний разобранный лог

Подробно анализировался:

```text
/mnt/store/tasks/logs/cloud_backup_qnap-2026-08-22-03-59-59.jsonl
```

Результат:

- бэкап успешен;
- архив 20 374 268 642 байта (около 19 GiB);
- архивирование 4215 секунд;
- средняя скорость 4.61 MiB/s;
- сетевой тест 4.55 MiB/s;
- raw-transfer успешен;
- сервисы поднялись с первой попытки;
- была найдена и затем исправлена ошибка `no such service: redis` в
  `redis_logs_capture` — production теперь использует `valkey`.

После этого появились более свежие логи, ещё не разобранные в этой беседе:

```text
/mnt/store/tasks/logs/cloud_backup_qnap-2026-08-25-03-59-59.jsonl
/mnt/store/tasks/logs/cloud_backup_qnap-2026-08-26-04-00-00.jsonl
/mnt/store/tasks/logs/cloud_backup_qnap-2026-08-27-03-59-59.jsonl
```

Следующая полезная проверка — проанализировать лог 2026-08-27 и убедиться,
что `redis_logs_capture rc=0`, а после следующего запуска с новым endpoint —
что появилось `wg_endpoint_selected` с новым адресом.

## QNAP/Entware: важные эксплуатационные факты

- Скрипт запускается с шебангом `#!/opt/bin/bash`; в crontab нельзя вызывать
  его через `sh`.
- `/opt` — bind-mount постоянного Entware-каталога
  `/share/Public/entware`. После потери mount `/opt/bin/bash` отсутствует и
  cron завершается молча до входа в сам скрипт.
- На QNAP добавлена cron-проверка, восстанавливающая bind-mount:

  ```cron
  * * * * * [ -x /opt/bin/opkg ] || mount --bind /share/Public/entware /opt
  ```

- QNAP `crond` читает live-файл `/tmp/cron/crontabs/admin`; постоянный мастер
  — `/etc/config/crontab`. Изменения нужно синхронизировать в оба места.
- В минимальном Entware нет внешнего `timeout`, `iostat`, `vmstat`, `top`.
  Для `ncat` использовать встроенный `--idle-timeout`, а не `timeout ncat`.

## README и состояние репозитория

2026-08-27 выполнен аудит всех 19 отслеживаемых корневых shell/Python-скриптов.
`README.md` сокращён и синхронизирован с фактическим кодом:

- удалены отсутствующие `newline_to_space_v1-fix-width.py`,
  `newline_to_space_v2-spaces.py`, `wireguard-install.sh`;
- удалён раздел несуществующего Graphify-графа;
- удалена несвязанная диагностика `nvidia_drm` и датированная история QNAP;
- исправлены CLI `mht_to_fb2.py`, зависимости, конфиги и имена/ротация логов;
- 19 текущих скриптов соответствуют 19 разделам README;
- локальные ссылки и Markdown fences проверены, `git diff --check` проходит.

На момент создания compact рабочее дерево содержит только изменение
`README.md`; compact-файл добавляется этим действием. Коммит и push не
выполнялись.

## Известный отдельный дефект

`mht_to_fb2.py --dry-run` сейчас не является полностью безопасным dry-run:
для MHT/MHTML он всё равно запускает `pandoc` и может создать/перезаписать FB2,
хотя исходный MHT не удаляет. README теперь описывает фактическое поведение;
код пока не исправлялся.
