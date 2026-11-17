# `check_mytasks.sh`: стартовая настройка и проверка

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md). Опции — [README.md](../../README.md), раздел `check_mytasks.sh`.

## Порядок и ОС

- Один скрипт для новой машины и регулярной проверки: ОС → самоустановка в автозагрузку (user-юнит `check-mytasks.service`, `--no-create --notify`) → `conf/check_mytasks.conf` с `MYTASKS_DIR` = каталог запуска (расхождение — предупреждение и правка до юнитов) → мастер отсутствующих конфигов (первый запуск) → зависимости → задания → таблица (`--status`, всегда).
- `detect_family`: по `ID`, затем `ID_LIKE` из `/etc/os-release`: `arch`, `debian`, `redhat` (Fedora Atomic — `/run/ostree-booted`), `suse`, `alpine`, `void`, `gentoo`, `solus`, `nixos`; `PKG_MGR`. Нераспознанная ОС — `unknown` и `OS_NOTE`. Без systemd (`has_systemd`: `systemctl` и `/run/systemd/system`) юниты не создаются.
- Новое семейство — в `detect_family`, `root_pkg`, `root_update`; при другом имени пакета — в `DEPS`.
- Переносимость: шебанги `#!/usr/bin/env bash|python3` (исключения — `cloud_backup_qnap.sh`: `#!/opt/bin/bash`, `#!/bin/sh`); в юнитах `/usr/bin/env КОМАНДА`; GNU-утилиты (`find -printf`, `stat -c`) — зависимости `gnu:find`/`gnu:stat`.

## Обновление системы

- Первый шаг, обязательный: `system_update` → `__root-update` → `root_update`; `UPDATE_MGR`/`UPDATE_MODE`/`UPDATE_DESC`. Garuda — `garuda-update --noconfirm`, отключается `GARUDA_UPDATE="no"`.
- Сбой → `UPDATE_OK=0`: пакеты не ставятся (частичное обновление), остальное проверяется.
- Перезагрузка — `REBOOT_FLAG` или нет `KERNEL_MODULES_DIR/$(uname -r)`.
- Тесты подменяют `garuda-update`, `emaint`, `nixos-rebuild`, `transactional-update` и менеджеры пакетов; реальный `garuda-update` не вызывать (заглушка первой в `PATH`).

## Шаблоны

- Жёстких путей нет: `systemd/*.{service,timer}`, `systemd/user/`, `systemd/autostart/`, примеры — с `@MYTASKS_DIR@`, `@HOME@` (`subst`).
- Новый юнит — шаблон + запись в `JOBS`; не копировать вручную в `/etc` или `~/.config`.
- Сравнение — `same_file` (без `cmp`); расхождение = «отличается», предлагается обновление.

## `JOBS` и `DEPS`

- `JOBS` — `вид|имя|параметр|условие|описание`. Виды: `user-unit`, `system-unit`, `user-ext`, `system-ext`, `no-cron` (строк crontab с перечисленными скриптами быть не должно; исправление — удалить), `link`, `autostart`, `conf`, `venv`, `db`, `root-file`, `linger`, `health`; системные — `system-off` (`__root-disable`), `sys-key` (`КЛЮЧ = значение`, `__root-set-key ФАЙЛ КЛЮЧ`, копия `.bak-<дата>`), `sys-check` (только проверка).
- `root-file|ИМЯ|ИСТОЧНИКИ:КАТАЛОГ:РЕЖИМ|условие|описание`: ИСТОЧНИКИ — через запятую, первый существующий обычный файл в каталоге скрипта, `*.example` — с подстановкой; КАТАЛОГ `lib` = `ROOT_LIB_DIR` (`/usr/local/lib/mytasks`), `conf` = `ROOT_CONF_DIR` (`/etc/mytasks`), в конфиг дописывается `MYTASKS_DIR`; РЕЖИМ `0755`/`0644`; `root:root`. Статусы «есть» / «нет» / «отличается» (содержимое, права или не обычный файл). Установка — `__root-install-file` с подтверждением. Каталог `abs` — шаблон из `etc/`, копия `.bak-<дата>`; в `*/systemd/*` — `daemon-reload`.
- `linger` — `loginctl enable-linger` для `beufm-recorder.service` с загрузки: сначала без прав (polkit), при отказе `__root-linger`.
- `health` — user-служба без `.service`, параметр `МАСКА_ЛОГА:МИНУТЫ`: свежий лог в `LOG_DIR` не старше МИНУТ и без ERROR в последних 200 строках; исправление — `systemctl --user restart`. Задание `beufm-recorder`, 20 мин.
- Условия: `path:`, `cmd:`, `fs:ТОЧКА=ТИП`, `family:`, `nopath:`, `link:`, `swap:zram`.
- `opt` — «рекомендуется»: на код возврата не влияет, с `--yes` не исправляется; только у системных заданий (fstrim, paccache.timer), не у зависимостей.
- `db|ИМЯ|команда|условие|описание` — SQLite: проверка `<команда> --dry-run` из `MYTASKS_DIR` (пути относительны ему; служба входа работает из `$HOME` — до 2026-11-15 `music_downloader.db` давала код 2). `[dry-run] будет:` = «отсутствует или устарела» (ВНИМАНИЕ), иначе OK «схема vN». Исправление — от пользователя, без root; нет исполняемого файла — ВНИМАНИЕ, при исправлении «не создана: нет …». На актуальной базе владельцы (`system_monitor.sh`, `music_downloader.py`, `utility_bills.sh` с `--migrate-db`) печатают `[dry-run] схема БД уже vN …` без «будет:».
- `DEPS` — `команда|пакеты|условие|описание`; пакеты `имя;семейство=имя;…`, `-` — нет в репозиториях, `py:МОДУЛЬ` — системный Python. Без `opt` (решение пользователя 2026-11-16): всё нужное скриптам ставится без вопросов, в том числе с `--yes`.
- Первые две строки (`check-mytasks.service`, `conf/check_mytasks.conf`) добавляет `apply_defaults` — в `JOBS` не дублировать.

## Требования стека

- `conf/check_mytasks.requirements` — единый источник `JOBS`/`DEPS` (`source` и в root-шагах): меняется стек — правится он, не скрипт.
- Машинные дополнения — `JOBS+=(…)` в `conf/check_mytasks.conf` (под root не читаются).
- Обязательные: запрет сна и zram (llama.cpp, OCR, круглосуточные службы). Рекомендуемые: TRIM, cron выкл., paccache, `ParallelDownloads`.
- GRUB, gamemode, электропитание и личные настройки ОС в источник не входят.

## Мастер и вопросы

- `parse_example`/`write_conf`: описание — комментарии над переменной, умолчание — из примера с подстановкой; неизменённые строки копировать как есть (иначе `quote_value` экранирует `${HOME}`); массивы — по элементу на строку; файлы без переменных (`sources.conf`, JSON) — правкой текста. `cloud_backup_qnap.conf` мастером не предлагается (`CONF_WIZARD_SKIP`).
- Вопросы `ui_yesno`/`ui_input`/`ui_text`/`ui_choice`: `zenity` → `kdialog` (у kdialog `--cancel-label`/третья кнопка работают только в `--yesnocancel`, проверено 2026-10-23) → терминал (`--ask`) → без терминала только предупреждения. `--yes` — всё обязательное без вопросов.
- «Отмена» — только текущее окно (конфиг не создаётся, поле — по умолчанию). «Стоп» (zenity `--extra-button`, kdialog `--yesnocancel`, пункт списка, в терминале «стоп») → `STOP_RC` и `request_stop`: больше ничего не спрашивается и не меняется, печатается таблица.
- `smart_default` — определимое на машине (`OS_FAMILY`, `CUDA_ARCH`, `BUILD_JOBS`). «Нет» запоминается на запуск (`HANDLED`, `SKIPPED_CONFS`).
- Root-шаги — повторный запуск через `sudo -A`/`pkexec`/`sudo`: `__root-install-units`, `__root-install-file`, `__root-enable`, `__root-linger`, `__root-pkg`, `__root-update` (логи не пишутся).
- Пользовательские службы входа — только `enable` (иначе `enable --now` ждёт oneshot с `sleep 60`); таймеры — `enable --now`.

## Привилегии

- Root-шаги разбираются в блоке ARGS до `load_user_conf`: под root пользовательский конфиг не выполняется (в тестах с `SUDO_CMD=""` — из конфига).
- Проверка аргументов: пакеты — только из `DEPS` (`dep_known_package`); `__root-install-units` — `system-unit`; `__root-enable` — `system-unit`/`system-ext` из `JOBS`; `__root-install-file` — только `root-file` из `JOBS`; linger — только для `SUDO_USER`/`PKEXEC_UID`; `MYTASKS_DIR` = каталог скрипта.
- `valid_dest_dir`: абсолютный, без `.`, `..`, `//` и симлинков; под root каждый существующий компонент принадлежит root и не доступен на запись группе и остальным. `valid_source`: обычные файлы в каталоге скрипта без симлинков.
- Плейсхолдеры `@ROOT_LIB_DIR@`, `@ROOT_CONF_DIR@` — `subst`; `ExecStart` root-копии в `systemd/system-monitor.service`.
- Осознанный компромисс: сам скрипт запускается от root из домашнего каталога (как `sudo make install`); граница — проверка аргументов.

## Песочница и тесты

- В агентской песочнице — только `--status` или `--dry-run --no-gui` (без `--no-gui` при `DISPLAY` откроются окна на экране пользователя).
- `tests/check_mytasks.bats`: копия скрипта и шаблонов во временном каталоге, временный `HOME`, `SUDO_CMD=""`, `OS_RELEASE_FILE`, каталоги юнитов в `TMP_DIR`; подмены `systemctl`, `crontab`, `apt-get`, `pacman`, `dnf`, `notify-send`.
</content>
