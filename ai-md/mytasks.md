# mytasks: правила проекта

Читать перед задачей в `/home/esimych/esimych-docs/mytasks` вместе с [AI.md](AI.md). Темы — в [topics/](topics/), по индексу ниже и только по теме задачи.

## Индекс тем

Перед действием по теме (установка пакета, удаление, юниты, режимы скриптов) тему обязательно открыть и следовать ей, а не действовать по памяти:

- [`topics/qnap-backup.md`](topics/qnap-backup.md) — QNAP: `cloud_backup_qnap.sh`, `check_backups.sh`;
- [`topics/sources.md`](topics/sources.md) — `sources.sh`;
- [`topics/install-agent-skills.md`](topics/install-agent-skills.md) — `install_agent_skills.sh`: доставка индекса ai-md, навыков, хуков;
- [`topics/check-mytasks.md`](topics/check-mytasks.md) — `check_mytasks.sh`: настройка и проверка;
- [`topics/system-monitor.md`](topics/system-monitor.md) — `system_monitor.sh`, юниты systemd;
- [`topics/llamactl.md`](topics/llamactl.md) — `llamactl.sh`, `llamactl_tray.py`, локальные LLM;
- [`topics/music-downloader.md`](topics/music-downloader.md) — `music_downloader`: конфигурация, поведение;
- [`topics/utility-bills.md`](topics/utility-bills.md) — `utility_bills`: квитанции ЖКУ;
- [`topics/books.md`](topics/books.md) — библиотека `/mnt/copy/Books`.

Новые требования проекта — сюда или в файл темы; новая тема — отдельный файл в `topics/` со ссылкой в этом индексе.

## Область работы

- Сессии и чаты — только с `/home/esimych/esimych-docs/mytasks`.
- Группа `tasks` (QNAP): каталог `/mnt/store/tasks` (NFS `/share/Public/tasks`) — боевая копия `cloud_backup_qnap.sh`, `conf/`, `logs/` (JSONL), `state/`. Исходник и git — `mytasks`: правки здесь, затем копия в `/mnt/store/tasks/cloud_backup_qnap.sh`. `conf/cloud_backup_qnap.conf` — только на QNAP, не в git. `/mnt/store/tasks` — не git-репозиторий.
- esimych-cloud (`root@<сервер в туннеле WG>:/opt/esimych-cloud`, адрес — `~/.config/ai-context/machine-notes.md`) — только по прямому указанию пользователя в текущем запросе: не подключаться для сверки, диагностики, открытых вопросов. Скрипты, работающие с сервером (`cloud_backup_qnap.sh`), правятся здесь как обычно.

## Язык

- Ответы, промежуточные сообщения, пояснения — по-русски.
- Новые и изменяемые комментарии, docstring, документация — по-русски; иноязычные пояснения в правимых файлах переводить без потери смысла и точности.
- Идентификаторы, команды, пути, ключи конфига, имена API и директивы не переводить. Ошибки и цитаты — оригинал с русским пояснением.

## Валидация

- Тесты и проверки кода **локально не запускать** — ни на рабочей машине, ни на QNAP или сервере (глобально: «Тесты и проверки кода — только в CI»). Проверяет GitHub Actions после пуша: правка → коммит и пуш по правилу подтверждения.
- CI не ждать (`gh run watch`, опрос, фоновые задачи). Ошибки присылает пользователь: `gh run view <id> --log-failed` → исправить → пуш. Нет нужной проверки — добавить в workflow, не запускать локально.
- CI (`.github/workflows/`):
  - `bats-tests.yml` — все Bats-тесты `tests/` на каждый push/PR; `tests/all_scripts_syntax.bats` — `bash -n`/`sh -n` по shebang каждого корневого `*.sh` и `llamactl/*.sh`.
  - `shellcheck.yml` (при изменении `*.sh`/`*.py`): `shellcheck -S error`, `-S style` (`{}`, `[[ ]]`, `case *)`), `-o all` (включая SC2312), `bash -n`/`sh -n` по shebang, поиск переменных без `{}`, `python3 -m py_compile` корневых `.py` и `llamactl/*.py`, Ruff для `llamactl/llamactl_tray.py` и `llamactl/llamactl_status.py` (`--line-length 130`).
  - `music-downloader.yml` — ставит `music_downloader/requirements-ci.txt` (рабочие зависимости + `pytest`, `ruff`; в локальный venv из `requirements.txt` не попадают); Ruff для `music_downloader/` (`.ruff.toml`: Python 3.14, длина 120), публикует отчёты Ruff; pytest `music_downloader/tests` + `tests/test_music_downloader.py` на Python 3.14. Тесты — временные каталоги и подменённые HTTP-ответы; покрывают глобальные пути загрузки, отказ на неверных, отсутствующих и заполненных каталогах до создания скрапера, атомарную запись, дедупликацию БД. Реальное скачивание не запускать. Фильтры путей: загрузчик, общие тесты, сам workflow.
  - `security-scan.yml` — Semgrep, Trivy, OSV-Scanner, Gitleaks, CodeQL.
- `shellcheck`, `bats`, `pytest`, `ruff` на рабочую машину не ставить: `check_mytasks.sh` их не предлагает, `requirements.txt` не содержит.
- `--dry-run` — не проверка кода, а обязательный шаг перед боевым запуском (см. «Устройство»).

## Graphify

Обязательно для агентов через `.cursorrules`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`.

- Граф — `graphify-out/` (вне Git, не добавлять принудительно). Перед ручным поиском проверять `graphify-out/graph.json`, навигация и связи — через Graphify.
- Граф есть: вопросы о кодовой базе сначала `graphify query "<вопрос>"`; без нужды не перестраивать. Графа нет, а анализ нужен — из корня `graphify .`. После изменения кода или конфига — `graphify update .` из корня.
- Ключи не нужны: не запрашивать `GEMINI_API_KEY`/`GOOGLE_API_KEY`, не останавливаться из-за их отсутствия; использовать локальное или агентское извлечение.

## Устройство репозитория и общие соглашения

- Плоский набор независимых Bash/Python-скриптов в корне; не импортируют и не вызывают друг друга, кроме `split_by_dash.sh` из `music_downloader.sh` при успехе. «Работать с проектом» — уточнить скрипт: общей точки входа нет.
- llamactl — исключение: `llamactl/llamactl.sh`, `llamactl/llamactl_tray.py`, `llamactl/llamactl_status.py`; общие `conf/llamactl.conf`, `logs/`, `state/` — в корне; `REPO_DIR` — каталог выше `llamactl/`.
- `split_by_dash.sh`: файл без ` - ` переименовывается по тегам ffprobe (`artist`/`album_artist`, `title`) в `Исполнитель - Название.ext` и раскладывается. События `renamed_by_tag` / `dry_renamed_by_tag`; нет тега исполнителя или ffprobe — `skip_no_artist` (WARN), файл не трогать.
  - Сверка тегов со старым именем (translit_key: кириллица→латиница, `[a-z0-9]`, `y`→`i`; первые 3 символа первого слова): совпал только title — перестановка (`tags_swapped`); ничего — `skip_tag_mismatch`; artist с « - » — `skip_artist_dash`.
  - Конфликт имён (в каталоге, папке исполнителя или `DEST_DIR`) решает размер: больший остаётся; при равном удаляется новый (`tag_dup_removed`; общий с конфликтом регистра `resolve_case_files`).
  - `name_from_tags` печатает имя или `SKIP:причина:детали`; `process_directory` разбирает префикс. Переименование — `[ "${DRY_RUN}" -eq 1 ] || mv`.
- Bash: `${VAR}`, `[[ ]]`, числа в `return`/`exit` в кавычках (`return "${rc}"`); в каждом `case` — ветка `*)`. Контроль — `shellcheck -S style` и поиск переменных без `{}`.
- Пропущенное предупреждение, ошибка или регрессия: до правок объяснить пропуск — управляющий путь кода и класс предупреждения; одна опровергаемая локальная гипотеза и одна дешёвая проверка. Не ограничиваться первым симптомом.
- `cleanup_logs()`: `find … -printf '%T@|%p\n' | sort -nr | awk 'NR > N { print $2 }'` + `mapfile -t` (Bash) или `while read` (POSIX sh); не `ls | tail`. `find -name` — всегда в кавычках: `"${SCRIPT_BASE}-*.jsonl"`.
- `ssh`/`sshpass`: параметры — массив `SSH_OPTS=(...)`, раскрывать `"${SSH_OPTS[@]}"`; не хранить в строке с word splitting.
- Структура корневого `.sh`: `SCRIPT ID / PATHS` → `HELPERS` → `ARGS` → `MAIN`; проверки зависимостей и параметров — в начале; `set -eu` или `set -uo pipefail` по модели ошибок скрипта. Сохранять при правке и добавлении.
- **`--dry-run`** (короткая `-n`, если не занята) — обязателен в каждом корневом `.sh`/`.py`: ничего не меняет (файлы, каталоги, БД, сервисы, систему, сеть); печатает `[dry-run] будет: …`, пишет действия в лог событиями `dry_<event>`; чтение и проверки — как обычно. Мониторы в dry-run не пишут в БД/отчёты и не шлют уведомления. Служебные логи запуска (`logs/*.jsonl`) допустимы.
  - Эталон — `split_by_dash.sh`: `log_action`, `DRY_RUN`, обёртки `mkdir`/`mv`/`rmdir`; перемещение — `[ "${DRY_RUN}" -eq 1 ] || mv …`, не функция (иначе SC2310 в `shellcheck -o all`).
  - Скрипт без `--dry-run` не завершён: добавить режим, описание в README и bats-тест «состояние не изменилось».
- **Сначала dry-run (для ИИ-агентов).** Перед боевым запуском скрипта, меняющего файлы, каталоги, БД, сервисы или систему, нужен успешный `--dry-run` на тех же аргументах и конфиге: без ошибок, план просмотрен (количество и состав изменений, удаления, спорные и отклонённые операции соответствуют ожиданиям). Тогда предлагать решение с итогом dry-run. Без успешного dry-run не предлагать ни боевой запуск, ни применение исправления — сначала устранить причину сбоя или расхождения. Боевой запуск — только по прямому подтверждению именно этого действия.
  - Не запускать скрипт без `--dry-run` «для проверки» или демо на боевых данных (`/mnt/copy/Music`, `/mnt/copy/Books`, бэкапы, БД). Для демо — копия скрипта во временной папке без `conf/`, с `MYTASKS_LOG_DIR` вне репозитория.
  - Dry-run не воспроизводит ожидаемое — боевой запуск не выполнять, сообщить пользователю.
- Длинные имена в `/mnt/copy/Music`: вид `Исполнители/Исполнители - Название.mp3` не менять. Только если имя в этом виде длиннее 254 байт (UTF-8), файл называется по полному названию без повторения исполнителей (`Название.mp3`); исполнители — один раз в имени папки через ` & `, без запятых. Решение 2026-10-30 для единственного файла (`Төреғали Төреәлі & … & Еркін Нұржанов/Алақанымнан ұшқан аққуым.mp3`, 266 байт); на эту дату других имён длиннее 254 байт в `/mnt/copy/Music` и `mobile-music` нет.
- Имена для Windows: музыкальная и книжная библиотеки синхронизируются Syncthing Linux↔Windows. Скрипты, создающие или переименовывающие файлы и папки, соблюдают: запрещённые символы `< > : " \ | ? *`; управляющие символы, конечные точки и пробелы; зарезервированные `CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`; конфликты имён, различающихся регистром. Эталон — `win_safe()` в `split_by_dash.sh` (awk, нужен gawk). «&» без пробелов с обеих сторон получает их (`A&B` → `A & B`) в именах папок и файлов.
  - Ручная проверка `split_by_dash.sh` на боевых данных — сначала `-n`/`--dry-run`. `DEST_DIR` из `conf/split_by_dash.conf` при явном `--dest` не применяется (целевой каталог — только `--dest`); dry-run перед `/mnt/copy/Music` обязателен.
- **Зашитые значения — в конфиг.** Пути машины, имена контейнеров и пользователей, адреса, порты, тайм-ауты, паузы, пороги, сроки хранения — не литералами в коде, а `ПАРАМЕТР="${ПАРАМЕТР:-по умолчанию}"` в начале скрипта (переопределяется окружением или `conf/<script_name>.conf`); описывать в `conf/<script_name>.conf.example` и README.
  - Исключения: пути стандартов ОС (`/proc`, `/etc/os-release`), внутренние пути образов (`/var/lib/mysql/general.log`), константы протоколов.
  - Срок хранения логов — `KEEP_LOGS` из `conf/log_template.conf` (`-v keep="${KEEP_LOGS:-10}"` в `cleanup_logs`), не число в `awk`. Пути данных по умолчанию — в репозитории рядом со скриптом, не в каталоге пользователя (см. `state/`).
- **`state/` — только файлы состояния**: маркеры последнего запуска и проверки, кэши, временные промежуточные результаты — то, потеря чего безвредна и восстанавливается следующим запуском. Важное и невосстановимое — базы с историей и отметками пользователя, JSON-отчёты перед изменением или удалением файлов (по ним откатываются изменения), записи, резервные копии — в `state/` не писать. Такие данные — в собственном каталоге скрипта или подпроекта (`system_monitor/system_monitor.db`, `music_downloader/music_downloader.db`, `utility_bills/utility_bills.db`, `books_library/books_library.db`, `books_library/reports/`) с отдельной строкой в `.gitignore`. Новый скрипт сразу выбирает такой каталог; найденные в `state/` важные данные переносятся с миграцией путей в коде, конфигах, юнитах, `check_mytasks.requirements` и README.
  - Правило — по требованию пользователя 2026-11-16. Прежние данные в `state/`, которые надо проверить и при необходимости перенести (отдельная задача): `beufm_recordings/`, `books-graph/`, `split_by_dash_*.tsv`, резервная копия git-метаданных music_downloader.
- Конфиг — `conf/<script_name>.conf`, подключается как shell-файл. В Git — только `conf/*.conf.example`; реальные `conf/*.conf` игнорируются и создаёт пользователь, скрипт при первом запуске делает `mkdir -p conf`. Для нового настраиваемого скрипта — `.example`.
- Логи — по одному JSON-объекту на строку в `logs/<script>-<timestamp>.jsonl` или фиксированном суточном файле; ротация сохраняет последние 5 или 10 файлов скрипта. Схема — `conf/log_template.conf`, иначе встроенная. Поля: ECS (`@timestamp`, `log.level`, `event.action`, `service.name`, `schema.version`) для ELK/OpenSearch/Loki/Graylog/Splunk и совместимые старые (`script`, `event`, `msg`, `detail`, `rc`). `logs/`, `state/`, реальные `conf/*.conf` — в `.gitignore` как машинно-зависимые.
  - Каталог логов переопределяется `MYTASKS_LOG_DIR` (все корневые скрипты, `music_downloader/setup_venv.sh`, `music_downloader.py`). bats-тесты, запускающие скрипт из репозитория, экспортируют её в `$BATS_TEST_TMPDIR/logs`; тесты с копией скрипта во временном каталоге — не требуют.

## Тесты, CI и Git

- `tests/` ≈ один к одному корневым скриптам (`<script>.bats`, bats-core). Внешние программы (`curl`, `notify-send`, `docker`, `ssh`…) подменяются заглушками в начале `PATH`; образец — `tests/getip.bats`: каталог заглушек и `env PATH="$STUB_DIR:$PATH" bash -c "..."`. Новые скрипты получают проверку синтаксиса через `all_scripts_syntax.bats` без отдельной записи.
- Состав workflows — см. «Валидация». Версии CI закреплены: `shellcheck-py==0.11.0.1` (PyPI, `pipx`); `semgrep==1.183.0` (1.95.0 падала при импорте: protobuf и pkg_resources; `security-scan.yml`); bats-core `v1.11.1`; Ruff: `shellcheck.yml` для `llamactl` — `ruff==0.17.0`, `requirements-ci.txt` загрузчика — `ruff>=0.17.0`, `utility_bills` — `>=0.17.0,<0.18`. У всех workflows верхний уровень `permissions: contents: read`.
- `music_downloader/` — отслеживаемый Python-подпроект, не вложенный checkout и не submodule. Исходники, драйверы, тесты, README, `.ruff.toml`, requirements и `.json.example` — в Git. Реальные `music_downloader.json`, SQLite-БД и бэкапы, логи, venv — локальные по `.gitignore`; не добавлять их и `graphify-out/` принудительно. Бэкап прежних вложенных Git-метаданных — `state/music_downloader-git-backup-6394179/` (игнорируемый, неактивный, не в Git). `newline_to_space/` — отдельный локальный Python-подпроект, исключён из Git.
