# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

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

Lint shell scripts the same way CI does (only `*.sh` in repo root, not `music_downloader/`):
```sh
shellcheck -S error ./*.sh
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

This repo is **not a single application** — it's a flat collection of independent, single-purpose bash/Python scripts living in the repo root. Scripts do not import or call each other's code (the one exception is `music_downloader.sh` invoking `split_by_dash.sh` on success). When asked to work on "the project", clarify which script — there is no shared entry point.

**Standard script layout.** Every root `.sh` script follows the same internal structure: `SCRIPT ID / PATHS` → `HELPERS` → `ARGS` → `MAIN`, with explicit dependency/parameter checks up front and `set -eu` or `set -uo pipefail` (chosen per script's error-handling needs). Follow this shape when editing or adding scripts rather than introducing a different convention.

**Config convention.** Each script that needs configuration reads `conf/<script_name>.conf`, sourced as a shell file at runtime. Only `conf/*.conf.example` templates are committed; real `conf/*.conf` files are gitignored and created by the user (scripts also `mkdir -p conf` on first run if it's missing). Never commit a real `conf/*.conf` file, and when adding a new configurable script, add a matching `.example` template.

**Unified JSONL logging.** Scripts that log write one JSON object per line to `logs/<script>-<timestamp>.jsonl` (or a fixed daily filename, depending on the script), rotating to keep only the last 5 or 10 files per script. The field schema is unified across scripts via `conf/log_template.conf` (sourced if present, falls back to hardcoded defaults) so entries carry both ECS-style fields (`@timestamp`, `log.level`, `event.action`, `service.name`, `schema.version`) for ELK/OpenSearch/Loki/Graylog/Splunk, and legacy fields (`script`, `event`, `msg`, `detail`, `rc`) for backward compatibility. `logs/`, `state/`, and real `conf/*.conf` are all gitignored — they're runtime/machine-specific artifacts, not source.

**Two independent backup scripts, same remote.** `cloud_backup.sh` (desktop, runs via `sudo`, uses `wg-quick` + `sshpass`) and `cloud_backup_qnap.sh` (runs directly on the QNAP NAS itself via Entware cron/Task Scheduler, no sudo, SSH-key auth only) both back up the same `REMOTE_HOST`. The QNAP variant cannot use `wg`/`wg-quick` at all on some QTS kernels (`Protocol not supported`), so it brings the tunnel up manually over the `wireguard-go` userspace UAPI socket (see `wg_up_userspace`/`wg_conf_get`). Both scripts independently run `docker compose down`/`up -d` on the remote host around the backup — **never run both against the same `REMOTE_HOST` concurrently**, or the remote services can flap or get stuck down.

**Testing architecture.** `tests/` mirrors the root scripts roughly 1:1 (`<script>.bats` per script), using bats-core. Tests stub external binaries (curl, notify-send, docker, ssh, etc.) by prepending a temp directory containing fake executables to `PATH` rather than mocking in-process — see `tests/getip.bats` for the pattern (stub dir + `env PATH="$STUB_DIR:$PATH" bash -c "..."`). `tests/all_scripts_syntax.bats` is a catch-all that runs `bash -n`/`sh -n` (based on the shebang) over every root `*.sh` automatically, so new scripts get syntax-checked for free without adding a test entry.

**CI is two independent, differently-triggered workflows**, not one pipeline: `.github/workflows/shellcheck.yml` runs `shellcheck -S error ./*.sh` only when a push/PR touches `*.sh`; `.github/workflows/bats-tests.yml` runs the full bats suite on every push/PR regardless of what changed.

**`music_downloader/` and `newline_to_space/` are separate, local-only Python subprojects**, entirely excluded from this git repo (gitignored as top-level dirs) — they have their own venvs, dependencies, and `music_downloader/` has its own `.ruff.toml` and README. `music_downloader.sh` in the repo root is only a thin wrapper: activate `music_downloader/venv`, run `music_downloader.py`, then chain into `split_by_dash.sh` on success. `tests/test_music_downloader.py` imports directly from the gitignored `music_downloader/` directory, so it only runs where that project exists locally.
