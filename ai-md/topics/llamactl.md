# `llamactl.sh` и `llamactl_tray.py`

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md). Устройство и команды — [README.md](../../README.md), раздел `llamactl.sh`.

## Управление llama.cpp

- Локальный llama.cpp: роутер `llama-server.service` на `:8080` (пресеты `/etc/llama.cpp/models.ini`), FIM `llama-fim.service` на `:8012`, модели `/var/lib/llama.cpp/models`; независимо от Zed/VS Code.
- Ссылки `~/.local/bin/llamactl` и `~/.local/bin/llamactl-tray` (цели — `llamactl/llamactl.sh`, `llamactl/llamactl_tray.py`); трей — `~/.config/autostart/llamactl-tray.desktop`.
- Любые действия с llama.cpp — через `llamactl` (сначала `-n`), не ручные `curl`/`systemctl`.
- Root-шаги — только `sudo -A` root-хелпера `llamactl/llamactl-root.sh` (`ROOT_HELPER` в `conf/llamactl.conf`; `check_mytasks.sh` ставит его `root-file` в `/usr/local/lib/mytasks/llamactl-root`, root:root 0755): `install-preset`, `restore-preset`, `swap-install`; один запрос пароля на действие; эти вызовы логов не пишут (чтобы в `logs/` не было root-файлов).
- Пути — зашитые или из `/etc/mytasks/llamactl-root.conf` (владелец root, не исполняется, разбор `КЛЮЧ="значение"`). Файлы пользователя читаются с его правами (`setpriv`). Из сборки копируются только `bin/` и `lib/`, ничего не исполняется.
- Хелпер нет или устарел — `llamactl` отказывает в root-шаге с подсказкой запустить `check_mytasks.sh`.
- Правило sudoers `NOPASSWD` для сервисов классификатор Claude Code блокирует как выдачу прав — не предлагать.

## Загрузка моделей

- `add` без sha256 от Hugging Face отказывает; обход — `--no-verify` (без проверки). Размер сверяется, если Hugging Face его вернул.

## `models.ini`

- Каждая запись строит новую версию из **текущего** файла и ставит её между `preset_lock`/`preset_unlock` (`flock` на `PRESET_LOCK_FILE`, по умолчанию `${XDG_RUNTIME_DIR}/llamactl-models-ini.lock`): `preset_set_key`, `preset_apply_edit`, `add_apply_preset`, `remove_apply_preset`.
- Новый писатель `models.ini` — только так; файл заранее не строить; блокировку не держать во время загрузки или редактора. `add` берёт её после скачивания; `edit` не применяет правку, если `sha256` изменился с открытия редактора.
- Обёртку `with_lock "$@"` не использовать: shellcheck `-o all` даёт SC2329.
- Тесты: «add: правка models.ini во время загрузки не затирается», параллельные `preset set`, конфликт `preset edit`.
- Пресеты применяются без перезапуска: `GET /models?reload=1`; при неудаче — `systemctl restart`, затем откат из `models.ini.bak-*`.

## Обновление llama.cpp

- `update` без `--ref` ставит последний релизный тег `bNNNN` (`LLAMA_REF="latest-tag"` в конфиге); `--ref ТЕГ|SHA` — явный тег или полный SHA. Сборку, `--version` и `ldd` делает от пользователя.
- `swap-install` (root): `bin/` и `lib/` → `/opt/llama.cpp.new`, обмен с `/opt/llama.cpp` (прежняя — `/opt/llama.cpp.prev`); откат, если сервисы не ответили за `HEALTH_TIMEOUT_SECS` (90 с).

## Код

- jq-программы получают значения через окружение (`env.X`), не `--arg`/`$x`: CI-проверка переменных без фигурных скобок не отличает jq-переменные от shell. Литерал `$ORIGIN` для RPATH — в `RPATH_LIT` по той же причине.
- Короткий `-n` — dry-run только до имени команды (у `logs` свой `-n N`); `--dry-run`/`--json` допустимы в любом месте.
- `status`/`models` JSONL-логов не пишут: трей вызывает их каждые 5 с.
- Статус — единственный источник `llamactl_status.py` (stdlib: HTTP к роутеру/FIM, `/proc`, `statvfs`, NVML через ctypes с откатом на `nvidia-smi`, один `systemctl is-active`). `cmd_status` только форматирует; трей импортирует модуль (`spec_from_file_location`), опрос — в фоновом потоке (сигнал Qt). Формат JSON менять синхронно в модуле, `cmd_status` и трее. Сбор статуса в bash не возвращать (было 61–73 процесса и ~160 мс CPU на вызов, стало ~20 мс, замер 2026-10-23).
- Трей: шебанг `/usr/bin/env python3` (NixOS); нет PyQt6 у найденного `python3` (venv) — `os.execv` системного `/usr/bin/python3`. PyQt6 импортируется только в графическом режиме: `--print-menu [--status-file]` проверяется в `tests/llamactl.bats` без дисплея.
- Ruff: `music_downloader/venv/bin/ruff check --line-length 130 llamactl/llamactl_tray.py llamactl/llamactl_status.py`.
- Тесты `tests/llamactl.bats` подменяют `curl` (роутер/FIM/Hugging Face), `systemctl`, `nvidia-smi`, `journalctl`, `git`, `cmake`; пресет и модели — во временном каталоге, `SUDO_CMD=""`. Тесты статуса поднимают `python3 -m http.server` вместо роутера (`start_router`) и задают `LLAMACTL_NO_NVML=1`. Сторонний `llama-server` в `kill-foreign` — копия `sleep` с этим именем.

## `llama-vscode` и процессы

- Встроенные env расширения поднимают свои `llama serve -hf …` (в т.ч. gpt-oss с `-c 0 -np 2`) и падают по VRAM. В настройках VS Code — `llama-vscode.env_start_last_used = false`; использовать только env «Локальный llama.cpp».
- `llamactl doctor` показывает такие процессы и кеши `~/.cache/huggingface`; `llamactl kill-foreign` их завершает.

## Прокси `llama_lang_router.py`

- Прокси `:8081` перед роутером (user-юнит `llama-lang-router.service`, задание в `JOBS`), виртуальная модель `auto`.
- Выбор по доле казахских букв (ә ғ қ ң ө ұ ү һ і ≥ 3 % кириллицы) последнего сообщения пользователя: `LANG_ROUTER_KK_MODEL`, иначе `LANG_ROUTER_DEFAULT_MODEL`. Явную модель не трогает; `auto` добавляет в `/models` и `/v1/models` копией модели по умолчанию; потоковые ответы — чанками.
- Только stdlib. Тесты `tests/test_llama_lang_router.py` (unittest, фиктивный роутер на случайном порту) — job `python-check` в `shellcheck.yml` вместе с Ruff.
