# Резервное копирование QNAP

Справка проекта mytasks по теме. Общие правила — [AI.md](../AI.md), правила проекта — [mytasks.md](../mytasks.md).

## Резервное копирование QNAP

- `cloud_backup_qnap.sh` запускается непосредственно на QNAP NAS через Entware cron/Task Scheduler без `sudo`; вход на сервер — SSH-ключ `REMOTE_SSH_KEY` (пароль `REMOTE_PASSWORD` — только если на QNAP установлен `sshpass`); вокруг копирования выполняет на `REMOTE_HOST` `docker compose down`/`up -d`. Десктопный `cloud_backup.sh` удалён (ноябрь 2026).
- На части ядер QTS вариант QNAP не может использовать `wg`/`wg-quick` (`Protocol not supported`) и поднимает туннель вручную через userspace UAPI-сокет `wireguard-go`; см. `wg_up_userspace` и `wg_conf_get`.
- Значения конфига, подставляемые в удалённые команды, проверяются по строгим шаблонам до начала работы (имена `A-Za-z0-9._-`, `REMOTE_PATH` — абсолютный путь из `A-Za-z0-9._/-`); `umask` — `022` (архивы читает `check_backups.sh` по NFS). Raw TCP (`RAW_TRANSFER_ENABLED=1`) идёт без собственного шифрования — только при доверенных пирах WireGuard; временные файлы на сервере — через `mktemp`. Подробности — `README.md`.
- **Перед любой работой с `cloud_backup_qnap.sh` обязательно прочитать `ai-md/cloud_backup_qnap.NOTES.md`.**

## `check_backups.sh`

- Проверяет каталоги из `BACKUP_DIRS` (`conf/check_backups.conf`): наличие архивов, возраст, размер, падение размера относительно предыдущего и опционально целостность; пишет JSONL-лог, при ошибке (код `1` — проблемы с бэкапами, `2` — конфигурация/зависимости) показывает уведомление через `notify-send`.
- Расписание — systemd **user** timer `systemd/user/check-backups.{service,timer}` (`OnBootSec=4min`, `OnUnitActiveSec=1d`), ставит `check_mytasks.sh` в `~/.config/systemd/user`; в service `SuccessExitStatus=1`. Установка и проверка — `systemctl --user` (из песочницы агента user-шина доступна).
- Целостность свежего архива проверяется по умолчанию (`VERIFY_INTEGRITY="yes"`) с учётом размера: тайм-аут = размер / `VERIFY_MIN_SPEED_MBPS` + `VERIFY_TIMEOUT_BASE_SECS`, `nice`/`ionice`, кэш уже проверенных архивов в `state/check_backups.verified`, откладывание для архива моложе `VERIFY_MIN_AGE_MINUTES`. Замер 2026-10-04: NFS `/mnt/store` ≈112 МБ/с (узкое место — гигабит, `zstd -t` успевает), архив ~33,5 ГБ ≈ 5 мин. Тесты, не касающиеся целостности, задают `VERIFY_INTEGRITY="no"`.
- Недописанный архив — не ошибка (`archive_writing`, INFO): последний архив моложе `WRITING_RECENT_MINUTES`, у которого за `WRITING_CHECK_SECS` растут размер/mtime или есть локальный писатель (`lsof`, режим `w`/`u`), исключается, проверяются предыдущие готовые. `cloud_backup_qnap.sh` пишет `tar.zst` сразу под финальным именем, `sources.sh` — в `*.7z.part` (маска + `PARTIAL_SUFFIXES`); растущий `.part` снимает `archive_stale` (WARN `archive_stale_writing`), архив, изменившийся за время `verify_archive`, не помечается `archive_corrupt`. Тесты подменяют `sleep` стабом, дописывающим файл, и задают `WRITING_CHECK_SECS=0`.
