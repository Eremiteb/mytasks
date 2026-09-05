import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Добавляем путь к music_downloader в sys.path для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "music_downloader")))

import music_downloader
from music_downloader import DatabaseManager, capitalize_first, safe_filename, save_response, track_identity

class TestMusicDownloader(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(safe_filename("Test File?"), "Test File")
        self.assertEqual(safe_filename("Artist - Title*"), "Artist - Title")
        self.assertEqual(safe_filename("Hello/World"), "HelloWorld")
        self.assertEqual(safe_filename(""), "")
        self.assertEqual(safe_filename(None), "")
        self.assertEqual(safe_filename("Әсем әуен"), "Әсем әуен")

    def test_capitalize_first(self):
        self.assertEqual(capitalize_first("hello"), "Hello")
        self.assertEqual(capitalize_first(" world"), "World")
        self.assertEqual(capitalize_first(""), "")
        self.assertEqual(capitalize_first(None), "")

    def test_track_identity_ignores_case_quotes_and_whitespace(self):
        self.assertEqual(
            track_identity("  ӘСЕМ  ", "Менің ‘Әнім’"),
            track_identity("әсем", "менің әнім"),
        )

    def test_save_response_writes_all_destinations_atomically(self):
        class Response:
            def iter_content(self, chunk_size):
                yield b"abc"
                yield b"def"

        with tempfile.TemporaryDirectory() as root:
            paths = [{"path": os.path.join(root, name)} for name in ("one", "two")]
            for item in paths:
                os.mkdir(item["path"])

            size, saved = save_response(Response(), paths, "track.mp3", expected_size=6)

            self.assertEqual(size, 6)
            self.assertEqual([open(path, "rb").read() for path in saved], [b"abcdef", b"abcdef"])
            self.assertFalse(any(os.path.exists(f"{path}.part") for path in saved))

    def test_save_response_removes_partial_files_on_size_mismatch(self):
        class Response:
            def iter_content(self, chunk_size):
                yield b"partial"

        with tempfile.TemporaryDirectory() as root:
            paths = [{"path": root}]
            with self.assertRaises(OSError):
                save_response(Response(), paths, "track.mp3", expected_size=100)
            self.assertFalse(os.path.exists(os.path.join(root, "track.mp3")))
            self.assertFalse(os.path.exists(os.path.join(root, "track.mp3.part")))

    def test_save_response_enforces_smallest_destination_limit(self):
        for expected_size in (0, 4, 6):
            with self.subTest(expected_size=expected_size), tempfile.TemporaryDirectory() as root:
                paths = [
                    {"path": str(Path(root) / "one"), "max_size_mb": 100 / 1048576},
                    {"path": str(Path(root) / "two"), "max_size_mb": 9 / 1048576},
                ]
                for folder in paths:
                    Path(folder["path"]).mkdir()
                    (Path(folder["path"]) / "existing.mp3").write_bytes(b"keep")
                response = MagicMock()
                response.iter_content.return_value = [b"abc", b"def"]
                with self.assertRaises(music_downloader.DownloadLimitExceeded):
                    save_response(response, paths, "track.mp3", expected_size=expected_size)
                for folder in paths:
                    self.assertEqual(list(Path(folder["path"]).iterdir()), [Path(folder["path"]) / "existing.mp3"])
                    self.assertEqual((Path(folder["path"]) / "existing.mp3").read_bytes(), b"keep")
                if expected_size == 6:
                    response.iter_content.assert_not_called()

    def test_save_response_recounts_size_and_allows_exact_limit(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [{"path": root, "max_size_mb": 6 / 1048576}]
            response = MagicMock()
            response.iter_content.return_value = [b"abc"]
            save_response(response, paths, "one.mp3", expected_size=3)
            save_response(response, paths, "two.mp3", expected_size=3)
            with self.assertRaises(music_downloader.DownloadLimitExceeded):
                save_response(response, paths, "three.mp3", expected_size=3)
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ["one.mp3", "two.mp3"])

    def test_save_response_counts_copies_in_nested_destinations(self):
        with tempfile.TemporaryDirectory() as root:
            child = Path(root) / "child"
            child.mkdir()
            paths = [
                {"path": root, "max_size_mb": 5 / 1048576},
                {"path": str(child), "max_size_mb": 100 / 1048576},
            ]
            response = MagicMock()
            response.iter_content.return_value = [b"a", b"bc"]
            with self.assertRaises(music_downloader.DownloadLimitExceeded):
                save_response(response, paths, "track.mp3")
            self.assertEqual(list(Path(root).rglob("*")), [child])
            paths[0]["max_size_mb"] = 6 / 1048576
            save_response(response, paths, "track.mp3", expected_size=3)
            self.assertEqual(music_downloader.get_dir_size_bytes(root), 6)

    def test_run_uses_global_destinations_for_all_sites(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            paths = [{"path": str(root / name), "max_size_mb": 1024} for name in ("one", "two")]
            for folder in paths:
                Path(folder["path"]).mkdir()
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [{'id': '1', 'artist': 'Artist', 'title': base_url, "
                "'download_url': 'https://example.com/track.mp3'}]\n",
                encoding="utf-8",
            )
            config = {
                "download_paths": paths,
                "sites": [
                    {"site_name": name, "site_script": str(driver), "base_url": name, "categories": ["/"]}
                    for name in ("First", "Second")
                ],
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            response = MagicMock(status_code=200, headers={"content-length": "3"})
            response.iter_content.return_value = [b"abc"]
            scraper = MagicMock()
            scraper.get.return_value.__enter__.return_value = response
            with (
                patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                patch.object(music_downloader, "setup_logging"),
                patch.object(music_downloader, "cleanup_old_logs"),
                patch.object(music_downloader.time, "sleep"),
                patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
                patch.object(music_downloader, "check_paths_and_limits", wraps=music_downloader.check_paths_and_limits) as check,
            ):
                music_downloader.run()

            check.assert_called_once_with(paths, "system")
            self.assertEqual(scraper.get.call_count, 2)
            for folder in paths:
                for title in ("First", "Second"):
                    self.assertEqual((Path(folder["path"]) / f"Artist - {title}.mp3").read_bytes(), b"abc")
            with sqlite3.connect(root / "db.sqlite") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 2)

    def test_run_stops_on_limit_without_recording_partial_track(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [{'id': title, 'artist': 'Artist', 'title': title, "
                "'download_url': 'https://example.com/track.mp3'} for title in ('One', 'Two', 'Three')]\n",
                encoding="utf-8",
            )
            config = {
                "download_paths": [{"path": str(destination), "max_size_mb": 5 / 1048576}],
                "sites": [{"site_name": "test", "site_script": str(driver), "base_url": "test", "categories": ["/"]}],
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            response = MagicMock(status_code=200, headers={})
            response.iter_content.return_value = [b"a", b"bc"]
            scraper = MagicMock()
            scraper.get.return_value.__enter__.return_value = response
            with (
                patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                patch.object(music_downloader, "setup_logging"),
                patch.object(music_downloader, "cleanup_old_logs"),
                patch.object(music_downloader.time, "sleep"),
                patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
                self.assertRaises(SystemExit) as error,
            ):
                music_downloader.run()
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(scraper.get.call_count, 2)
            self.assertEqual(list(destination.iterdir()), [destination / "Artist - One.mp3"])
            self.assertEqual((destination / "Artist - One.mp3").read_bytes(), b"abc")
            with sqlite3.connect(root / "db.sqlite") as conn:
                self.assertEqual(conn.execute("SELECT title FROM downloads").fetchall(), [("one",)])

    def test_run_rejects_invalid_global_destinations_before_scraper(self):
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "config.json"
            configs = [
                {"sites": [{"download_paths": [{"path": root}]}]},
                {"download_paths": []},
                {"download_paths": "not a list"},
                {"download_paths": [{"path": str(Path(root) / "missing")}]},
                {"download_paths": [{"path": root, "max_size_mb": 0}]},
            ]
            for config in configs:
                with self.subTest(config=config):
                    config_path.write_text(json.dumps(config), encoding="utf-8")
                    with (
                        patch.object(music_downloader, "CONFIG_PATH", str(config_path)),
                        patch.object(music_downloader, "setup_logging"),
                        patch.object(music_downloader.cloudscraper, "create_scraper") as create_scraper,
                        self.assertRaises(SystemExit) as error,
                    ):
                        music_downloader.run()
                    self.assertEqual(error.exception.code, 2)
                    create_scraper.assert_not_called()

    def test_database_removes_legacy_table_and_case_insensitive_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            with sqlite3.connect(db_path) as conn:
                schema = "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER)"
                conn.execute(f"CREATE TABLE downloads {schema}")
                conn.execute(f"CREATE TABLE allsongs {schema}")
                conn.executemany(
                    "INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("old", "ӘСЕМ", "  Ән  ", "one", "2026-01-01", None, "old.mp3", 1),
                        ("new", "әсем", "ән", "two", "2026-01-02", None, "new.mp3", 2),
                    ],
                )

            DatabaseManager(db_path)

            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT id FROM downloads").fetchall(), [("new",)])
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='allsongs'").fetchone()
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("again", "әсем", "ән", "three", "2026-01-03", None, "again.mp3", 3),
                    )


if __name__ == "__main__":
    unittest.main()
