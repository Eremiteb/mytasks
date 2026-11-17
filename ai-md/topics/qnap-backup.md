# Резервное копирование QNAP

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md).

## `cloud_backup_qnap.sh`

- Запускается на QNAP через Entware cron/Task Scheduler без `sudo`. Вход на сервер — SSH-ключ `REMOTE_SSH_KEY` (пароль `REMOTE_PASSWORD` — только при установленном `sshpass`). На `REMOTE_HOST` вокруг копирования — `docker compose down`/`up -d`. Десктопный `cloud_backup.sh` удалён (ноябрь 2026).
- На части ядер QTS `wg`/`wg-quick` не работают (`Protocol not supported`): туннель поднимается через userspace UAPI-сокет `wireguard-go` (`wg_up_userspace`, `wg_conf_get`).
- Значения конфига, уходящие в удалённые команды, проверяются строгими шаблонами до работы: имена `A-Za-z0-9._-`, `REMOTE_PATH` — абсолютный путь из `A-Za-z0-9._/-`. `umask` — `022` (архивы читает `check_backups.sh` по NFS). Временные файлы на сервере — через `mktemp`.
- Raw TCP (`RAW_TRANSFER_ENABLED=1`) — без своего шифрования, только при доверенных пирах WireGuard.
- Параметры, dry-run, шифры, сжатие — [README.md](../../README.md), раздел `cloud_backup_qnap.sh`.
- **Перед любой работой с `cloud_backup_qnap.sh` обязательно прочитать `ai-md/cloud_backup_qnap.NOTES.md`.**

## `check_backups.sh`

- Проверяет каталоги `BACKUP_DIRS` (`conf/check_backups.conf`): наличие архивов, возраст, размер, падение относительно предыдущего, опционально целостность. JSONL-лог; код `1` — проблемы с бэкапами, `2` — конфигурация/зависимости; при ошибке — `notify-send`.
- Расписание — user-таймер `systemd/user/check-backups.{service,timer}` (`OnBootSec=4min`, `OnUnitActiveSec=1d`), ставит `check_mytasks.sh` в `~/.config/systemd/user`; `SuccessExitStatus=1`. Установка и проверка — `systemctl --user` (из песочницы user-шина доступна).
- Целостность свежего архива — по умолчанию (`VERIFY_INTEGRITY="yes"`): тайм-аут = размер / `VERIFY_MIN_SPEED_MBPS` + `VERIFY_TIMEOUT_BASE_SECS`; `nice`/`ionice`; кэш в `state/check_backups.verified`; архив моложе `VERIFY_MIN_AGE_MINUTES` откладывается. Замер 2026-10-04: NFS `/mnt/store` ≈112 МБ/с, архив ~33,5 ГБ ≈ 5 мин. Тесты, не о целостности, задают `VERIFY_INTEGRITY="no"`.
- Недописанный архив — не ошибка (`archive_writing`, INFO): последний архив моложе `WRITING_RECENT_MINUTES`, у которого за `WRITING_CHECK_SECS` растут размер/mtime или есть локальный писатель (`lsof`, режим `w`/`u`), исключается; проверяются предыдущие готовые. `cloud_backup_qnap.sh` пишет `tar.zst` сразу под финальным именем, `sources.sh` — `*.7z.part` (маска + `PARTIAL_SUFFIXES`). Растущий `.part` снимает `archive_stale` (WARN `archive_stale_writing`); архив, изменившийся за `verify_archive`, не помечается `archive_corrupt`. Тесты подменяют `sleep` стабом, дописывающим файл, и задают `WRITING_CHECK_SECS=0`.
