# `sources.sh`

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md).

## `sources.sh`

- Расписание — user-таймер `systemd/user/sources.{service,timer}` (21:30, `Persistent=true`), не cron: компьютер выключают до 23:00, cron пропущенный запуск не догоняет. `sources.service` ждёт окончания `music_downloader.(sh|py)` (шаблон не совпадает с `music_downloader/beufm_recorder.py`) и запускается под `systemd-inhibit --mode=block` (polkit разрешает блокировку и для user-служб вне сеанса, проверено 2026-10-31). В unit-файле `$` в shell-коде — `$$`.
- Все расписания mytasks — user-таймеры, cron не нужен; демон cron и `cronie` в `JOBS`/`DEPS` не добавлять: `music-downloader.timer` (21:00), `sources.timer` (21:30), `host-checks.timer` (+5 мин после загрузки и каждые 6 ч, 00/06/12/18); все с `Persistent=true`. Порядок — `After=`: `sources` после `music-downloader`, `check-backups` после `sources`. Вид `no-cron` в `check_mytasks.sh` удаляет строки crontab этих скриптов; новый перенос из cron — добавить имя скрипта в его параметр.
- Строки `7z|…` архивируют каталог через `.part`, ротация `SEVENZIP_KEEP` (по умолчанию 3); недописанные `.part` прошлых дат удаляются в начале следующего запуска.
- Архив `esimych-docs` в 7z не делать: ~300 ГБ (`media` 275 ГБ почти не сжимается) пишется ~2 ч и проверяется ~50 мин по NFS, компьютер выключают раньше; каталог уже копируется rsync в `/mnt/store` и `/mnt/copy`. `/mnt/store/backups/esimych` в `BACKUP_DIRS` не добавлять.
- **Не править `sources.sh` на месте, пока он работает:** `/bin/sh` дочитывает скрипт по смещению. Сначала `cp -p sources.sh sources.sh.tmp && mv -f sources.sh.tmp sources.sh` (новый inode).
