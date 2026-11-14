import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "music_downloader.py"
SPEC = importlib.util.spec_from_file_location("music_downloader", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load music_downloader module for smoke test")
music_downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(music_downloader)


def test_capitalize_first_smoke() -> None:
    assert music_downloader.capitalize_first("artist") == "Artist"
    assert music_downloader.capitalize_first(" Artist") == "Artist"
    assert music_downloader.capitalize_first("") == ""
    assert music_downloader.capitalize_first(None) == ""


def test_number_setting_reads_config_and_falls_back() -> None:
    assert music_downloader.number_setting({"download_timeout_seconds": 5}, "download_timeout_seconds", 60, minimum=1) == 5
    assert music_downloader.number_setting({}, "download_pause_seconds", 1.2) == 1.2
    assert music_downloader.number_setting({"download_pause_seconds": 0}, "download_pause_seconds", 1.2) == 0
    assert music_downloader.number_setting({"download_timeout_seconds": 0}, "download_timeout_seconds", 60, minimum=1) == 60
    assert music_downloader.number_setting({"download_pause_seconds": "x"}, "download_pause_seconds", 1.2) == 1.2


def test_migrate_database_dry_run_on_current_schema_reports_no_changes(tmp_path) -> None:
    path = tmp_path / "music.db"
    music_downloader.migrate_database(path)

    lines: list[str] = []
    music_downloader.migrate_database(path, dry_run=True, out=lines.append)

    assert len(lines) == 1
    assert lines[0].startswith(f"[dry-run] схема БД уже v{music_downloader.DB_LATEST}")
    assert "будет:" not in lines[0]
