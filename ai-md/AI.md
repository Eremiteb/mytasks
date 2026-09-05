# AI.md

This file is the single source of project guidance for all AI coding assistants.
Tool-specific entry points (CLAUDE.md, AGENTS.md, GEMINI.md, .github/copilot-instructions.md, .cursorrules) all redirect here.

## Язык ответов и комментариев

- Все ответы пользователю, промежуточные сообщения и пояснения писать на русском языке.
- Все новые и изменяемые комментарии в коде, строки документации (docstring) и поясняющий текст в документации писать на русском языке.
- При работе с файлами переводить встречающиеся иноязычные комментарии и пояснения на русский, сохраняя их смысл и техническую точность.
- Не переводить идентификаторы кода, команды, пути, ключи конфигурации, имена API и служебные директивы инструментов. Оригинальные сообщения ошибок и цитаты при необходимости сохранять, сопровождая пояснением на русском.

## Validation commands

Run before and after any edit to confirm nothing is broken.

Run the full bats test suite (requires bats-core; install once if missing):
```sh
git clone --depth 1 https://github.com/bats-core/bats-core.git /tmp/bats-core
sudo /tmp/bats-core/install.sh /usr/local
bats --print-output-on-failure tests
```

Run a single script's test file:
```sh
bats --print-output-on-failure tests/getip.bats
```

Syntax-check every root shell script at once (also enforced by `tests/all_scripts_syntax.bats`):
```sh
bash -n ./some_script.sh
```

Lint shell scripts — error level (blocks CI) and style level (brace style, `[[ ]]`, `case *)`…):
```sh
shellcheck -S error ./*.sh
shellcheck -S style ./*.sh
```

Syntax-check all root shell scripts by shebang (also enforced by CI):
```sh
for f in ./*.sh; do
  if head -1 "$f" | grep -qi bash; then bash -n "$f"; else sh -n "$f"; fi
done
```

Scan for unbraced variable references in local shell code (CI-enforced, skips comments and remote-shell strings):
```sh
grep -nP '(?<!")\$(?!\{)[A-Za-z_][A-Za-z0-9_]*' ./*.sh \
  | grep -v '^\s*#' | grep -v "\\\\'" \
  | grep -v 'sh -lc\|bash -c\|MARIADB_ROOT\|REDIS_WAIT\|PURGE_BINLOGS\|TRUNCATE\|in_progress'
```

Python syntax check for root `.py` files (also enforced by CI):
```sh
for f in ./*.py; do python3 -m py_compile "$f"; done
```

Python unit tests (run from the repository root after installing `music_downloader/requirements.txt` into its venv):
```sh
music_downloader/venv/bin/python -m pytest tests/test_music_downloader.py music_downloader/tests -q
```

These tests use temporary destinations and mocked HTTP responses; they cover shared global download paths, rejection of invalid/missing/full destinations before scraper creation, atomic file writes, and database deduplication. Do not run a real download just to validate configuration changes.

Lint `music_downloader/` (ruff config lives at `music_downloader/.ruff.toml`, py311, line-length 120):
```sh
music_downloader/venv/bin/ruff check music_downloader/
```

## Graphify

The generated project graph lives in `graphify-out/` and is excluded from Git.
Use it for navigation and analysis before manually searching the source code.
After changing code or configuration, refresh it from the repository root:

```sh
graphify update .
```

## Architecture

> When asked to work on "the project", always ask which script — there is no shared entry point.

This repo is **not a single application** — it's a flat collection of independent, single-purpose bash/Python scripts living in the repo root. Scripts do not import or call each other's code (the one exception is `music_downloader.sh` invoking `split_by_dash.sh` on success). When asked to work on "the project", clarify which script — there is no shared entry point.

**Shell style conventions.** All variables in root `.sh` scripts must use brace syntax (`${VAR}`, not `$VAR`), double-bracket tests (`[[ ]]` instead of `[ ]` in bash scripts), and quoted `return`/`exit` with numeric variables (`return "${rc}"`). `case` blocks must include an explicit `*)` branch. These are enforced by CI (`shellcheck -S style` + the unbraced variable scan).

**Missed-error analysis is mandatory.** If a user points out a missed warning, bug, or regression, every assistant must first explain why the issue was missed, identify the controlling code path and the specific warning class, and state one falsifiable local hypothesis plus one cheap check before changing code. Do not skip this analysis or narrow the scope to only the first visible symptom.

**Log cleanup pattern.** All `cleanup_logs()` functions use `find … -printf '%T@|%p\n' | sort -nr | awk 'NR > N { print $2 }'` with `mapfile -t` (bash) or a `while read` pipeline (POSIX sh) — never `ls | tail`. The `-name` argument to `find` is always fully quoted: `"${SCRIPT_BASE}-*.jsonl"`.

**SSH option arrays.** Scripts that call `ssh`/`sshpass` define options as a bash array (`SSH_OPTS=(...)`) and expand them as `"${SSH_OPTS[@]}"` — never as a plain string with word splitting.

**Standard script layout.** Every root `.sh` script follows the same internal structure: `SCRIPT ID / PATHS` → `HELPERS` → `ARGS` → `MAIN`, with explicit dependency/parameter checks up front and `set -eu` or `set -uo pipefail` (chosen per script's error-handling needs). Follow this shape when editing or adding scripts rather than introducing a different convention.

**Config convention.** Each script that needs configuration reads `conf/<script_name>.conf`, sourced as a shell file at runtime. Only `conf/*.conf.example` templates are committed; real `conf/*.conf` files are gitignored and created by the user (scripts also `mkdir -p conf` on first run if it's missing). Never commit a real `conf/*.conf` file, and when adding a new configurable script, add a matching `.example` template.

**Unified JSONL logging.** Scripts that log write one JSON object per line to `logs/<script>-<timestamp>.jsonl` (or a fixed daily filename, depending on the script), rotating to keep only the last 5 or 10 files per script. The field schema is unified across scripts via `conf/log_template.conf` (sourced if present, falls back to hardcoded defaults) so entries carry both ECS-style fields (`@timestamp`, `log.level`, `event.action`, `service.name`, `schema.version`) for ELK/OpenSearch/Loki/Graylog/Splunk, and legacy fields (`script`, `event`, `msg`, `detail`, `rc`) for backward compatibility. `logs/`, `state/`, and real `conf/*.conf` are all gitignored — they're runtime/machine-specific artifacts, not source.

**Two independent backup scripts, same remote.** `cloud_backup.sh` (desktop, runs via `sudo`, uses `wg-quick` + `sshpass`) and `cloud_backup_qnap.sh` (runs directly on the QNAP NAS itself via Entware cron/Task Scheduler, no sudo, SSH-key auth only) both back up the same `REMOTE_HOST`. The QNAP variant cannot use `wg`/`wg-quick` at all on some QTS kernels (`Protocol not supported`), so it brings the tunnel up manually over the `wireguard-go` userspace UAPI socket (see `wg_up_userspace`/`wg_conf_get`). Both scripts independently run `docker compose down`/`up -d` on the remote host around the backup — **never run both against the same `REMOTE_HOST` concurrently**, or the remote services can flap or get stuck down.

**Testing architecture.** `tests/` mirrors the root scripts roughly 1:1 (`<script>.bats` per script), using bats-core. Tests stub external binaries (curl, notify-send, docker, ssh, etc.) by prepending a temp directory containing fake executables to `PATH` rather than mocking in-process — see `tests/getip.bats` for the pattern (stub dir + `env PATH="$STUB_DIR:$PATH" bash -c "..."`). `tests/all_scripts_syntax.bats` is a catch-all that runs `bash -n`/`sh -n` (based on the shebang) over every root `*.sh` automatically, so new scripts get syntax-checked for free without adding a test entry.

**CI uses independent workflows**, not one pipeline: `.github/workflows/shellcheck.yml` runs `shellcheck -S error`, `shellcheck -S style`, `bash -n`/`sh -n` syntax check, unbraced-variable grep scan, and `python3 -m py_compile` for root `.py` files — triggered only when a push/PR touches `*.sh` or `*.py`; `.github/workflows/bats-tests.yml` runs the full bats suite on every push/PR regardless of what changed. `.github/workflows/music-downloader.yml` installs the downloader requirements, lints `music_downloader/`, publishes Ruff reports, and runs both `music_downloader/tests/` and `tests/test_music_downloader.py` on Python 3.12. Its path filters cover the downloader, its shared tests and the workflow itself.

**Machine-specific disk observation (2026-08-22).** `/mnt/copy` is mounted from `/dev/sda1`; the physical disk is `/dev/sda`, Seagate `ST4000DM004-2U9104` (4 TB, serial `WW608PYW`). In `state/system_monitor.db`, the complete history available on 2026-08-22 contains 26 records from `2026-08-08T18:13:43+0500` through `2026-08-22T18:00:00+0500`. `Reallocated_Sector_Ct` was 7077 through 2026-08-14 18:00 and increased to 7085 at 2026-08-15 00:00, then remained at 7085 through the latest record; pending and uncorrectable sectors remained 0, SMART overall-health was `PASSED`, temperature was 36°C in the latest record. `system_monitor.sh` correctly classifies this as `warn` because any positive reallocated-sector count is degradation. Treat these numbers as a dated operational snapshot, not stable README documentation; verify SQLite again before answering later questions.

**`music_downloader/` is a tracked Python subproject in this repository**, not a nested Git checkout or submodule. Commit its source, drivers, tests, README, `.ruff.toml`, requirements and `.json.example` together with the rest of `mytasks`. Its `.gitignore` keeps the real `music_downloader.json`, SQLite database/backups, logs and venv local; never force-add these or `graphify-out/`. The former nested Git metadata is backed up locally under ignored `state/music_downloader-git-backup-6394179/`; it is not active and must not be added to Git. `newline_to_space/` remains a separate, gitignored local Python subproject.

**Конфигурация загрузчика музыки.** Обязательный непустой список `download_paths` находится в корне JSON рядом с `sites` и действует для всех мостов: каждый трек сохраняется во все указанные папки. Настройки папок внутри сайтов больше не используются; при миграции перенесите один общий список в корень и удалите копии из сайтов. `max_size_mb` — лимит всей папки с подкаталогами (по умолчанию 1024 МиБ), проверяемый в байтах при старте и перед каждым сохранением. `save_response` проверяет также каждый блок потока, включая ответы без `Content-Length`, и учитывает копии в дочерних целевых папках. При превышении доступного объёма `DownloadLimitExceeded` приводит к удалению `.part` и выходу `2`, без записи неготового трека в БД и запуска сортировщика. Уже скачанное не удаляется. Контроль рассчитан на один процесс: сторонние записи во время скачивания требуют квот файловой системы. Отсутствующий/пустой/неверного типа глобальный список, отсутствующие папки и уже заполненные каталоги также завершают запуск с кодом `2`. Поддерживайте одинаковую структуру рабочего JSON, `.json.example` и README, не заменяя реальные пути/лимиты значениями из примера.

**Jamendo.** Категория `/` получает одну страницу до 200 последних треков из официального `tracks` API (`releasedate_desc`), а `/playlist/<ID>` использует API плейлистов. Принимаются только записи с `audiodownload_allowed: true` и непустым `audiodownload`, без подмены на потоковое `audio`. Нужен `client_id` сайта или `JAMENDO_CLIENT_ID`. Ошибки API и пустые результаты логируются по-русски с `site=jamendo_com`. Проверки метаданных не должны скачивать аудиофайлы.

**Контракт драйверов и мосты Shazam/MyChords.** Драйвер реализует `get_tracks(session, base_url, category)`; необязательный `resolve_track(session, track)` вызывается движком только перед скачиванием нового трека (после БД и общей очереди) и возвращает словарь `download_url/referer/src` или `None`. `None`/исключение → трек пропускается без записи в БД, его пара «исполнитель—название» удаляется из `queued_tracks`. Драйверы загружаются через `spec_from_file_location`, каталог загрузчика не в `sys.path` — соседние драйверы подключать по пути (см. `_load_driver` в Shazam), не через `from drivers import`. `shazam_com`: чарт `/ru-ru/charts/top-200/world` запрашивается с проверенными Firefox-заголовками (`CHART_HEADERS`; Chrome-профиль и `curl` без заголовков получали 405/204/чужую страницу), `parse_chart` восстанавливает SSR-вставки `$RS(...)` и требует canonical чарта, ровно 200 карточек `[data-test-id="songItem"]`, ранги 1…200 и 200 уникальных `adam_<id>` из `/song/<id>/`. Аудио ищется в поиске Sefon (`/search/?q=`) и LMusic (`/search?q=`, `lmusic.parse_tracks`) только при точном совпадении `_match_key` исполнителя и названия; превью Shazam/Apple не используются. `mychords_net`: `/ru/novinki` → `li.b-listing__full__item a.b-listing__full__item__name` («Исполнитель - Название», ID из `/<id>-slug.html`) → страница трека `div.b-words__player[data-src]` с iframe `audio.xpleer.com/embed/` → `#file[data-mp3]` на `storage.xpleer.com` (`audio/mpeg`). Сохранённые образцы HTML лежат в игнорируемом `state/` (`shazam-chart.html`, `mychords-*.html`, `xpleer-embed.html`).

**Music downloader wrapper and tests.** `music_downloader.sh` activates `music_downloader/venv` if present, runs `music_downloader.py`, deactivates the venv, and invokes `split_by_dash.sh` only on downloader exit code `0`. The sorter has separate path configuration. Individual driver/track failures are logged and can still yield exit code `0`; a missing JSON file also currently returns `0` without downloading. Engine logs are `logs/music_downloader_engine-<timestamp>.jsonl`, separate from wrapper logs `logs/music_downloader-<timestamp>.jsonl`. `tests/test_music_downloader.py` imports the tracked `music_downloader/` source directly; install its requirements before running the Python tests. Related current docs are the root README, this file, and `music_downloader/README.md`; historical QNAP notes are not a downloader changelog.
