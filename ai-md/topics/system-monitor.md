# `system_monitor.sh` и systemd

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md). Устройство и отчёт — [README.md](../../README.md), раздел `system_monitor.sh`.

## Скрипт

- Опрос: диски, CPU (`sensors -j`, `/proc/stat`, `/proc/loadavg`), каждая GPU через `nvidia-smi`. `sensors` и `nvidia-smi` необязательны: без них нет температуры CPU или раздела GPU, запуск не падает. Проверено на Ryzen 7 5800X и RTX 3060 Ti.
- `shellcheck -o all` — без замечаний. Вложенные `$(sql_escape ...)`/`$(html_escape ...)` в больших строках — сначала в `local`.
- Сценарии без зависимостей тестировать с изолированным `PATH` через `iso_path_without()` в Bats, не `PATH="$STUB_DIR:$PATH"` (иначе видны реальные `sensors`/`nvidia-smi`).
- Скрипт работает от root: созданные файлы (БД, `logs/system_monitor-*.jsonl`, `system_reports/system_monitor-*.html`, `latest.html`) отдаются `FILE_OWNER` (`пользователь` или `пользователь:группа`; пусто — владелец каталога скрипта; умолчание задаётся до конфига). `fix_owner`: не от root — ничего; при нужном владельце `chown` не вызывать; `chown -h`; ошибка — WARN; каталоги не трогать. Вызывается после `init_db`, при отчёте и в `cleanup_logs`. Под root `fix_existing_owner` чинит прежние файлы по маскам скрипта (чужие не трогает). `--dry-run` печатает `[dry-run] будет: сменить владельца на …: N файлов`. EUID для тестов — `SYSTEM_MONITOR_EUID`. Юнит остаётся под root.
- Под root скрипт отказывает при симлинках в путях каталогов данных и файлах БД. Лог — эксклюзивно (noclobber); `latest.html` — через `mktemp` рядом и `mv`.
- Таблицы создаются только при создании новой базы (`db_create_sql`); существующая v1-база с отсутствующей таблицей не досоздаётся.

## Схема БД

- Схема — только миграцией: `PRAGMA user_version`, `DB_LATEST=1`. Новая база сразу в текущей схеме (`db_create_sql`). База старше v1 не поддерживается: `init_db` падает с `db_version_unsupported` без изменений; `--dry-run` предупреждает.
- Следующая смена схемы: DDL в `ddl_<таблица>` и миграция v2 с пересборкой таблиц (образец миграции 1 — в истории git, удалена в ноябре 2026). `ALTER TABLE` вручную не использовать.
- `--migrate-db` только создаёт базу или приводит схему к текущей, без опроса (с `-n` — план; несовместим с `--report`). `check_mytasks.sh` вызывает его видом `db`.

## systemd

- Расписание — `systemd/system-monitor.{service,timer}` в `/etc/systemd/system`; cron не использовать.
- Служба — root-копия: `ExecStart=@ROOT_LIB_DIR@/system_monitor.sh --config @ROOT_CONF_DIR@/system_monitor.conf` (`/usr/local/lib/mytasks/system_monitor.sh`, задание `root-file`; конфиг — копия `/etc/mytasks/system_monitor.conf` из `conf/system_monitor.conf`, без него — из примера). Root-копия читает только доверенный конфиг: обычный файл, владелец root, без записи для группы и остальных, иначе код 2; `MYTASKS_DIR` обязателен.
- Изменения скрипта и `conf/system_monitor.conf` действуют на таймере только после «обновить» в `check_mytasks.sh`.
- Песочница: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=-@MYTASKS_DIR@/system_monitor -@MYTASKS_DIR@/logs -@MYTASKS_DIR@/system_reports` (`-` — нет каталога, не сбой), `RestrictAddressFamilies=AF_UNIX AF_NETLINK`, `MemoryDenyWriteExecute`. `PrivateDevices`, `DeviceAllow`, `CapabilityBoundingSet` не заданы — нужны диски, NVIDIA и smartctl. Другой `DB_FILE`/`REPORT_DIR` — правка `ReadWritePaths`.
- `Persistent=true` — один пропущенный интервал при загрузке без двойного запуска (вместо `@reboot`). `SuccessExitStatus=1`: код 1 — найденное критическое состояние, не сбой юнита.
- `-c/--config ФАЙЛ` задаёт конфиг явно (разбирается до загрузки конфига).
- Из агентской песочницы `systemctl` не видит system D-Bus: установка и проверка — `pkexec` или вне песочницы.
