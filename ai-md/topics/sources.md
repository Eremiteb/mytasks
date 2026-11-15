# `sources.sh`

Справка проекта mytasks по теме. Общие правила — [AI.md](../AI.md), правила проекта — [mytasks.md](../mytasks.md).

## `sources.sh`

- Расписание — user-таймер `systemd/user/sources.{service,timer}` (21:30, `Persistent=true`), не cron: компьютер часто выключают до 23:00, а cron пропущенный запуск не догоняет. `sources.service` ждёт окончания `music_downloader.(sh|py)` (шаблон не должен совпадать с `music_downloader/beufm_recorder.py`) и запускает скрипт под `systemd-inhibit --mode=block` (polkit разрешает блокировку и для user-служб вне сеанса, проверено 2026-10-31). В unit-файле `$` в shell-коде экранируется как `$$`.
- Строки `7z|…` архивируют каталог (через `.part`, ротация `SEVENZIP_KEEP`, по умолчанию 3); недописанные `.part` прошлых дат удаляются в начале следующего запуска. Архив 7z `esimych-docs` не делать: ~300 ГБ (из них `media` 275 ГБ почти не сжимается) пишется ~2 ч и проверяется ~50 мин по NFS, а компьютер выключают раньше; каталог и так копируется rsync в `/mnt/store` и `/mnt/copy`. `/mnt/store/backups/esimych` в `BACKUP_DIRS` не добавлять.
- Все расписания mytasks — user-таймеры systemd, cron скриптам не нужен (демон cron и пакет `cronie` в `JOBS`/`DEPS` не добавлять): `music-downloader.timer` (21:00), `sources.timer` (21:30), `btrfs-monitor.timer` (00/06/12/18), `getip.timer` (+2 мин после загрузки и каждые 30 мин), все с догоном (`Persistent=true`, кроме `getip`). Порядок — `After=`: `sources` после `music-downloader`, `check-backups` после `sources`. Вид задания `no-cron` в `check_mytasks.sh` удаляет оставшиеся строки crontab этих скриптов (иначе двойной запуск); новый перенос из cron — добавить имя скрипта в его параметр.
- **Не править `sources.sh` на месте, пока он работает:** `/bin/sh` дочитывает скрипт по смещению. Сначала `cp -p sources.sh sources.sh.tmp && mv -f sources.sh.tmp sources.sh` (новый inode, старый остаётся у процесса).
