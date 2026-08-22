# AI.md

This file is the single source of project guidance for all AI coding assistants.
Tool-specific entry points (CLAUDE.md, AGENTS.md, GEMINI.md, .github/copilot-instructions.md, .cursorrules) all redirect here.

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

Python unit tests (only relevant if the local, gitignored `music_downloader/` project is present):
```sh
python3 -m pytest tests/test_music_downloader.py
```

Lint `music_downloader/` (ruff config lives at `music_downloader/.ruff.toml`, py311, line-length 120):
```sh
ruff check music_downloader/
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

**CI is two independent, differently-triggered workflows**, not one pipeline: `.github/workflows/shellcheck.yml` runs `shellcheck -S error`, `shellcheck -S style`, `bash -n`/`sh -n` syntax check, unbraced-variable grep scan, and `python3 -m py_compile` for root `.py` files — triggered only when a push/PR touches `*.sh` or `*.py`; `.github/workflows/bats-tests.yml` runs the full bats suite on every push/PR regardless of what changed.

**Machine-specific disk observation (2026-08-22).** `/mnt/copy` is mounted from `/dev/sda1`; the physical disk is `/dev/sda`, Seagate `ST4000DM004-2U9104` (4 TB, serial `WW608PYW`). In `state/system_monitor.db`, the complete history available on 2026-08-22 contains 26 records from `2026-08-08T18:13:43+0500` through `2026-08-22T18:00:00+0500`. `Reallocated_Sector_Ct` was 7077 through 2026-08-14 18:00 and increased to 7085 at 2026-08-15 00:00, then remained at 7085 through the latest record; pending and uncorrectable sectors remained 0, SMART overall-health was `PASSED`, temperature was 36°C in the latest record. `system_monitor.sh` correctly classifies this as `warn` because any positive reallocated-sector count is degradation. Treat these numbers as a dated operational snapshot, not stable README documentation; verify SQLite again before answering later questions.

**`music_downloader/` and `newline_to_space/` are separate, local-only Python subprojects**, entirely excluded from this git repo (gitignored as top-level dirs) — they have their own venvs, dependencies, and `music_downloader/` has its own `.ruff.toml` and README. `music_downloader.sh` in the repo root is only a thin wrapper: activate `music_downloader/venv`, run `music_downloader.py`, then chain into `split_by_dash.sh` on success. `tests/test_music_downloader.py` imports directly from the gitignored `music_downloader/` directory, so it only runs where that project exists locally.
