# CLAUDE.md

Project guidance is in [AI.md](ai-md/AI.md). Read it before making any changes.
This file holds Claude/project-specific context not promoted to AI.md.

## `system_monitor.sh` (2026-08-08)

Renamed from `disk_monitor.sh`; now also polls CPU (`sensors -j` temp,
`/proc/stat` usage, `/proc/loadavg`) and GPU (`nvidia-smi`, per card).
`sensors`/`nvidia-smi` are optional deps — missing degrades gracefully
(no CPU temp / no GPU section), doesn't fail the run. Verified live on
this machine's real hardware (Ryzen 7 5800X, RTX 3060 Ti).

`shellcheck -o all` is fully clean (0 findings) — nested `$(sql_escape/
html_escape ...)` calls inside larger strings were hoisted to `local`
vars first. Testing "dependency unavailable" needs a from-scratch
isolated `PATH` (`iso_path_without()` in the bats file), not the usual
`PATH="$STUB_DIR:$PATH"` prepend, or a real `sensors`/`nvidia-smi` on
the test machine leaks through.

Production `state/system_monitor.db` is root-owned from before this
change; `cpu_stats`/`gpu_stats` tables appear on the next `sudo
./system_monitor.sh` run (`CREATE TABLE IF NOT EXISTS`), not by editing
the script.
