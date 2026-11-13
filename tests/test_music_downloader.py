import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# Лог движка при импорте модуля — во временный каталог, а не в logs/ репозитория
os.environ.setdefault("MYTASKS_LOG_DIR", tempfile.mkdtemp(prefix="music_downloader_test_logs_"))

MODULE_PATH = Path(__file__).resolve().parents[1] / "music_downloader" / "music_downloader.py"
SPEC = importlib.util.spec_from_file_location("music_downloader_engine", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
music_downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(music_downloader)

DatabaseManager = music_downloader.DatabaseManager
capitalize_first = music_downloader.capitalize_first
safe_filename = music_downloader.safe_filename
save_response = music_downloader.save_response
track_identity = music_downloader.track_identity

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

    def test_track_identity_ignores_separators_and_punctuation(self):
        # «&», «+», пробелы, скобки — всё это отбрасывается
        self.assertEqual(
            track_identity("Вася & Петя", "Song (Remix)"),
            track_identity("Вася  +Петя", "song remix"),
        )
        self.assertEqual(
            track_identity("AC/DC", "T.N.T."),
            track_identity("ac dc", "tnt"),
        )
        # слова при этом не склеиваются в один ключ
        self.assertNotEqual(
            track_identity("Artist", "Song"),
            track_identity("Artist", "Song Remix"),
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

            size, saved, filehash = save_response(Response(), paths, "track.mp3", expected_size=6)

            self.assertEqual(size, 6)
            self.assertEqual(filehash, hashlib.sha256(b"abcdef").hexdigest())
            self.assertEqual([Path(path).read_bytes() for path in saved], [b"abcdef", b"abcdef"])
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
                "    return [{'id': '1', 'artist': 'artist', 'title': base_url, "
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
            # У каждого сайта своё содержимое — иначе второй трек отсеется дедупом по хешу
            bodies = {"First": b"abc", "Second": b"abcd"}
            responses = []
            for body in bodies.values():
                item = MagicMock(status_code=200, headers={"content-length": str(len(body))})
                item.iter_content.return_value = [body]
                responses.append(item)
            scraper = MagicMock()
            scraper.get.return_value.__enter__.side_effect = responses
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
            # TLS: штатный HTTPAdapter requests без своего ssl_context (без ослабления)
            scraper.mount.assert_called_once()
            prefix, adapter = scraper.mount.call_args.args
            self.assertEqual(prefix, "https://")
            self.assertIs(type(adapter), music_downloader.HTTPAdapter)
            self.assertEqual(scraper.get.call_count, 2)
            for folder in paths:
                for title, body in bodies.items():
                    self.assertEqual((Path(folder["path"]) / f"Artist - {title}.mp3").read_bytes(), body)
            with sqlite3.connect(root / "db.sqlite") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT original_artist, original_title FROM downloads ORDER BY id"
                    ).fetchall(),
                    [("artist", "First"), ("artist", "Second")],
                )

    def test_run_round_robin_refills_each_site_without_waiting_for_big_queue(self):
        """Большая очередь одного сайта не задерживает следующие категории других сайтов.

        Категория читается только когда очередь её сайта опустела (ссылки со страниц
        бывают с меткой времени), а сайты по кругу отдают по одной песне.
        """
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            events = []
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    scraper.events.append(f'list {base_url}{category}')\n"
                "    count = 5 if base_url == 'Big' else 1\n"
                "    return [{'id': f'{category}{n}', 'artist': base_url, 'title': f'{category}{n}',\n"
                "             'download_url': f'https://x/{base_url}{category}{n}.mp3'} for n in range(count)]\n",
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "download_paths": [{"path": str(destination)}],
                "sites": [
                    {"site_name": "Big", "site_script": str(driver), "base_url": "Big", "categories": ["a"]},
                    {"site_name": "Two", "site_script": str(driver), "base_url": "Two", "categories": ["a", "b"]},
                ],
            }), encoding="utf-8")
            scraper = MagicMock()
            scraper.events = events

            def get(url, **kwargs):
                events.append(f"get {url.rsplit('/', 1)[1]}")
                body = url.encode()  # разное содержимое — без дедупа по хешу
                response = MagicMock(status_code=200, headers={"content-length": str(len(body))})
                response.iter_content.return_value = [body]
                context = MagicMock()
                context.__enter__.return_value = response
                return context

            scraper.get.side_effect = get
            with (
                patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                patch.object(music_downloader, "setup_logging"),
                patch.object(music_downloader, "cleanup_old_logs"),
                patch.object(music_downloader.time, "sleep"),
                patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
            ):
                music_downloader.run()

            self.assertEqual(events, [
                "list Biga", "get Biga0.mp3",
                "list Twoa", "get Twoa0.mp3",
                "get Biga1.mp3",
                "list Twob", "get Twob0.mp3",  # вторая категория — не дожидаясь конца Big
                "get Biga2.mp3", "get Biga3.mp3", "get Biga4.mp3",
            ])

    def test_run_resolves_only_new_chart_track_after_deduplication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            (destination / "Artist - Old.mp3").write_bytes(b"keep")
            db_path = root / "db.sqlite"
            DatabaseManager(str(db_path)).add_record(
                "shazam_old", "Artist", "Old", "shazam", filename="Artist - Old.mp3", filesize=4
            )
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [{'id': key, 'artist': 'Artist', 'title': title} for key, title in\n"
                "            [('old', 'Renamed'), ('alias', 'Old'), ('chart-new', 'New'), ('duplicate', 'New')]]\n"
                "def resolve_track(session, track):\n"
                "    return session.resolve_track(session, track.copy())\n",
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "download_paths": [{"path": str(destination)}],
                "sites": [{"site_name": "shazam", "site_script": str(driver),
                           "base_url": "https://chart.example", "categories": ["/"]}],
            }), encoding="utf-8")
            resolved = {
                "download_url": "https://audio.example/new.mp3",
                "referer": "https://audio.example/new",
                "src": "https://source.example/new",
            }
            response = MagicMock(status_code=200, headers={"content-length": "3"})
            response.iter_content.return_value = [b"abc"]
            scraper = MagicMock()
            scraper.resolve_track.return_value = resolved
            scraper.get.return_value.__enter__.return_value = response
            with (
                patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(db_path)),
                patch.object(music_downloader, "setup_logging"),
                patch.object(music_downloader, "cleanup_old_logs"),
                patch.object(music_downloader.time, "sleep"),
                patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
            ):
                music_downloader.run()

            scraper.resolve_track.assert_called_once()
            session, track = scraper.resolve_track.call_args.args
            self.assertIs(session, scraper)
            self.assertEqual(track["id"], "chart-new")
            self.assertNotIn("download_url", track)
            scraper.get.assert_called_once_with(
                resolved["download_url"], timeout=60, headers={"Referer": resolved["referer"]}, stream=True
            )
            self.assertEqual(sorted(p.name for p in destination.iterdir()), ["Artist - New.mp3", "Artist - Old.mp3"])
            self.assertEqual((destination / "Artist - New.mp3").read_bytes(), b"abc")
            self.assertEqual((destination / "Artist - Old.mp3").read_bytes(), b"keep")
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute(
                    "SELECT id, src, filename, filesize FROM downloads ORDER BY id"
                ).fetchall(), [
                    ("shazam_chart-new", resolved["src"], "Artist - New.mp3", 3),
                    ("shazam_old", None, "Artist - Old.mp3", 4),
                ])

    def test_run_downloads_through_driver_fetch_without_http(self):
        """Источник без HTTP-ссылки (yt-dlp) отдаёт fetch(): движок не вызывает scraper.get."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [{'id': 'yt', 'artist': 'Artist', 'title': 'Song'}]\n"
                "def resolve_track(session, track):\n"
                "    return session.resolve_track(session, track.copy())\n",
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "download_paths": [{"path": str(destination)}],
                "sites": [{"site_name": "shazam", "site_script": str(driver),
                           "base_url": "https://chart.example", "categories": ["/"]}],
            }), encoding="utf-8")
            response = MagicMock(status_code=200, headers={"content-length": "4"})
            response.iter_content.return_value = [b"opus"]
            fetch = MagicMock()
            fetch.return_value.__enter__.return_value = response
            scraper = MagicMock()
            scraper.resolve_track.return_value = {
                "download_url": "https://music.youtube.com/watch?v=x", "referer": "r",
                "src": "https://music.youtube.com/watch?v=x", "fetch": fetch,
            }
            with (
                patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                patch.object(music_downloader, "setup_logging"),
                patch.object(music_downloader, "cleanup_old_logs"),
                patch.object(music_downloader.time, "sleep"),
                patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
            ):
                music_downloader.run()

            fetch.assert_called_once_with()
            scraper.get.assert_not_called()
            self.assertEqual((destination / "Artist - Song.mp3").read_bytes(), b"opus")

    def test_run_skips_unresolved_chart_track_and_continues(self):
        for outcome in (None, RuntimeError("Сбой resolver")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                destination = root / "music"
                destination.mkdir()
                driver = root / "driver.py"
                driver.write_text(
                    "def get_tracks(scraper, base_url, category):\n"
                    "    return [{'id': key, 'artist': 'Artist', 'title': title} for key, title in\n"
                    "            [('chart-skip', 'Skip'), ('chart-next', 'Next')]]\n"
                    "def resolve_track(session, track):\n"
                    "    return session.resolve_track(session, track.copy())\n",
                    encoding="utf-8",
                )
                config_path = root / "config.json"
                config_path.write_text(json.dumps({
                    "download_paths": [{"path": str(destination)}],
                    "sites": [{"site_name": "shazam", "site_script": str(driver),
                               "base_url": "https://chart.example", "categories": ["/"]}],
                }), encoding="utf-8")
                resolved = {
                    "download_url": "https://audio.example/next.mp3",
                    "referer": "https://audio.example/next",
                    "src": "https://source.example/next",
                }
                response = MagicMock(status_code=200, headers={"content-length": "3"})
                response.iter_content.return_value = [b"abc"]
                scraper = MagicMock()
                scraper.resolve_track.side_effect = [outcome, resolved]
                scraper.get.return_value.__enter__.return_value = response
                with (
                    patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                    patch.object(music_downloader, "setup_logging"),
                    patch.object(music_downloader, "cleanup_old_logs"),
                    patch.object(music_downloader.time, "sleep"),
                    patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
                    patch.object(music_downloader.logger, "error") as log_error,
                ):
                    music_downloader.run()

                self.assertEqual(
                    [call.args[1]["id"] for call in scraper.resolve_track.call_args_list],
                    ["chart-skip", "chart-next"],
                )
                for call in scraper.resolve_track.call_args_list:
                    self.assertIs(call.args[0], scraper)
                    self.assertNotIn("download_url", call.args[1])
                if outcome is None:
                    log_error.assert_not_called()
                else:
                    log_error.assert_called_once_with(
                        "Ошибка скачивания Skip: Сбой resolver", extra={"site": "shazam"}
                    )
                scraper.get.assert_called_once_with(
                    resolved["download_url"], timeout=60, headers={"Referer": resolved["referer"]}, stream=True
                )
                self.assertEqual(list(destination.iterdir()), [destination / "Artist - Next.mp3"])
                self.assertEqual((destination / "Artist - Next.mp3").read_bytes(), b"abc")
                with sqlite3.connect(root / "db.sqlite") as conn:
                    self.assertEqual(conn.execute(
                        "SELECT id, src, filename, filesize FROM downloads"
                    ).fetchall(), [("shazam_chart-next", resolved["src"], "Artist - Next.mp3", 3)])

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

    def test_database_removes_case_insensitive_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            with sqlite3.connect(db_path) as conn:
                schema = "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER)"
                conn.execute(f"CREATE TABLE downloads {schema}")
                conn.executemany(
                    "INSERT INTO downloads (id, artist, title, site, timestamp, src, filename, filesize) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("old", "ӘСЕМ", "  Ән  ", "one", "2026-01-01", None, "old.mp3", 1),
                        ("new", "Әсем", "ӘН!", "two", "2026-01-02", None, "new.mp3", 2),
                    ],
                )

            DatabaseManager(db_path)

            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT id FROM downloads").fetchall(), [("new",)])
                self.assertEqual(
                    conn.execute(
                        "SELECT artist, title, original_artist, original_title FROM downloads"
                    ).fetchone(),
                    ("әсем", "ән", "Әсем", "ӘН!"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO downloads (id, artist, title, site, timestamp, src, filename, filesize) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("again", "әсем", "ән", "three", "2026-01-03", None, "again.mp3", 3),
                    )

    def test_database_collapses_rows_that_share_key_only_after_wider_normalization(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE downloads (id TEXT PRIMARY KEY, artist TEXT, title TEXT, original_artist TEXT, "
                    "original_title TEXT, site TEXT, timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER, "
                    "filehash TEXT)"
                )
                conn.execute("CREATE UNIQUE INDEX idx_downloads_artist_title ON downloads(artist, title)")
                conn.executemany(
                    "INSERT INTO downloads (id, artist, title, site, timestamp, src, filename, filesize) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        # различались только по прежней нормализации (пробел vs без)
                        ("a", "artist", "song remix", "s", "2026-01-01", None, "a.mp3", 1),
                        ("b", "artist", "songremix", "s", "2026-01-02", None, "b.mp3", 2),
                    ],
                )

            DatabaseManager(db_path)  # не должно падать на ещё живом UNIQUE-индексе

            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT id FROM downloads").fetchall(), [("b",)])

    def test_database_collapses_filehash_duplicates_and_enforces_partial_unique(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE downloads (id TEXT PRIMARY KEY, artist TEXT, title TEXT, original_artist TEXT, "
                    "original_title TEXT, site TEXT, timestamp DATETIME, src TEXT, filename TEXT, filesize INTEGER, "
                    "filehash TEXT)"
                )
                conn.executemany(
                    "INSERT INTO downloads (id, artist, title, site, timestamp, filename, filehash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("a", "x", "one", "s", "2026-01-01", "a.mp3", "HHH"),
                        ("b", "y", "two", "s", "2026-01-02", "b.mp3", "HHH"),  # тот же контент
                        ("c", "z", "three", "s", "2026-01-03", "c.mp3", None),
                        ("d", "w", "four", "s", "2026-01-04", "d.mp3", None),  # NULL не ограничен
                    ],
                )

            DatabaseManager(db_path)

            with sqlite3.connect(db_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT id FROM downloads ORDER BY id").fetchall(),
                    [("b",), ("c",), ("d",)],  # из пары одинакового хеша осталась свежая
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO downloads (id, artist, title, site, timestamp, filehash) "
                        "VALUES ('e', 'q', 'five', 's', '2026-01-05', 'HHH')"
                    )
                # несколько NULL-хешей разрешены
                conn.execute(
                    "INSERT INTO downloads (id, artist, title, site, timestamp, filehash) "
                    "VALUES ('f', 'q', 'six', 's', '2026-01-06', NULL)"
                )

    # --- Версионированные миграции схемы (PRAGMA user_version) ---

    CANONICAL_DOWNLOADS = [
        "id", "artist", "title", "original_artist", "original_title", "site",
        "timestamp", "src", "filename", "filesize", "filehash",
    ]

    @staticmethod
    def _columns(db_path, table):
        with sqlite3.connect(db_path) as conn:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

    @staticmethod
    def _backups(db_path):
        return sorted(Path(db_path).parent.glob(Path(db_path).name + ".bak-v*"))

    def _make_old_db(self, db_path, schema, rows_sql, rows):
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"CREATE TABLE downloads {schema}")
            conn.executemany(rows_sql, rows)

    def test_migration_reorders_columns_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            # Старый порядок: original_* и filehash в конце
            self._make_old_db(
                db_path,
                "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME, src TEXT, "
                "filename TEXT, filesize INTEGER, original_artist TEXT, original_title TEXT, filehash TEXT)",
                "INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("a", "x", "one", "s1", "2026-01-01", "u1", "a.mp3", 11, "X", "One", "H1"),
                    ("b", "y", "two", "s2", "2026-01-02", None, "b.mp3", 22, None, None, None),
                ],
            )

            DatabaseManager(db_path)

            self.assertEqual(self._columns(db_path, "downloads"), self.CANONICAL_DOWNLOADS)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                row = conn.execute(
                    "SELECT id, artist, title, original_artist, original_title, site, timestamp, src, "
                    "filename, filesize, filehash FROM downloads WHERE id = 'a'"
                ).fetchone()
                self.assertEqual(row, ("a", "x", "one", "X", "One", "s1", "2026-01-01", "u1", "a.mp3", 11, "H1"))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 2)
            backups = self._backups(db_path)
            self.assertEqual(len(backups), 1)
            self.assertRegex(backups[0].name, r"\.bak-v0-\d{8}-\d{6}$")
            with sqlite3.connect(backups[0]) as conn:  # копия — до миграции, в старом порядке
                self.assertEqual(conn.execute("PRAGMA table_info(downloads)").fetchall()[-1][1], "filehash")
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_migration_adds_missing_columns_as_null(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            self._make_old_db(
                db_path,
                "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME)",
                "INSERT INTO downloads VALUES (?, ?, ?, ?, ?)",
                [("a", "x", "one", "s", "2026-01-01")],
            )

            DatabaseManager(db_path)

            self.assertEqual(self._columns(db_path, "downloads"), self.CANONICAL_DOWNLOADS)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT id, filename, filesize, filehash FROM downloads").fetchone()
                self.assertEqual(row, ("a", None, None, None))

    def test_migration_drops_extra_column(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            self._make_old_db(
                db_path,
                "(id TEXT PRIMARY KEY, junk TEXT, artist TEXT, title TEXT, site TEXT, timestamp DATETIME)",
                "INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?)",
                [("a", "zzz", "x", "one", "s", "2026-01-01")],
            )

            DatabaseManager(db_path)

            self.assertNotIn("junk", self._columns(db_path, "downloads"))
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT id, artist FROM downloads").fetchall(), [("a", "x")])

    def test_migration_new_database_gets_latest_version_without_backup(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")

            DatabaseManager(db_path)

            self.assertEqual(self._columns(db_path, "downloads"), self.CANONICAL_DOWNLOADS)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], music_downloader.DB_LATEST)
            self.assertEqual(self._backups(db_path), [])

    def test_migration_reopen_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            self._make_old_db(
                db_path,
                "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME)",
                "INSERT INTO downloads VALUES (?, ?, ?, ?, ?)",
                [("a", "x", "one", "s", "2026-01-01")],
            )
            DatabaseManager(db_path)
            backups = self._backups(db_path)

            DatabaseManager(db_path)

            self.assertEqual(self._backups(db_path), backups)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 1)

    def test_migrate_db_dry_run_does_not_change_file_and_prints_plan(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            self._make_old_db(
                db_path,
                "(id TEXT PRIMARY KEY, junk TEXT, artist TEXT, title TEXT, site TEXT, timestamp DATETIME)",
                "INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?)",
                [("a", "zzz", "x", "one", "s", "2026-01-01")],
            )
            before = Path(db_path).read_bytes()
            buffer = StringIO()

            with patch.object(music_downloader, "DB_PATH", db_path), redirect_stdout(buffer):
                code = music_downloader.main(["--migrate-db", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertEqual(Path(db_path).read_bytes(), before)
            self.assertEqual(self._backups(db_path), [])
            output = buffer.getvalue()
            self.assertIn("[dry-run] будет: миграция БД v0 → v1", output)
            self.assertIn("[dry-run] будет: резервная копия", output)
            self.assertIn("таблица downloads: 1 строк", output)
            self.assertIn("отбрасываются: junk", output)

    def test_migrate_db_without_dry_run_migrates_without_dedup(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, "music.db")
            self._make_old_db(
                db_path,
                "(id TEXT PRIMARY KEY, artist TEXT, title TEXT, site TEXT, timestamp DATETIME)",
                "INSERT INTO downloads VALUES (?, ?, ?, ?, ?)",
                [("a", "ARTIST", "One", "s", "2026-01-01")],
            )

            with (
                patch.object(music_downloader, "DB_PATH", db_path),
                patch.object(music_downloader, "setup_logging"),
                redirect_stdout(StringIO()),
            ):
                code = music_downloader.main(["--migrate-db"])

            self.assertEqual(code, 0)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                # перенормализации нет: регистр сохранён
                self.assertEqual(conn.execute("SELECT artist FROM downloads").fetchone()[0], "ARTIST")

    def test_run_deduplicates_identical_audio_by_content_hash(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [\n"
                "        {'id': 'a', 'artist': 'Alpha', 'title': 'Original', 'download_url': 'https://e/a.mp3'},\n"
                "        {'id': 'b', 'artist': 'Beta', 'title': 'Reupload', 'download_url': 'https://e/b.mp3'},\n"
                "    ]\n",
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "download_paths": [{"path": str(destination)}],
                "sites": [{"site_name": "s", "site_script": str(driver), "base_url": "b", "categories": ["/"]}],
            }), encoding="utf-8")
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
            ):
                music_downloader.run()

            # оба ID зафиксированы (повторно не качаются), но на диске одна копия,
            # и хеш хранит только оригинал — дубликат записан с filehash=NULL,
            # чтобы не нарушать частичный UNIQUE-индекс.
            self.assertEqual([p.name for p in destination.iterdir()], ["Alpha - Original.mp3"])
            self.assertEqual((destination / "Alpha - Original.mp3").read_bytes(), b"abc")
            with sqlite3.connect(root / "db.sqlite") as conn:
                rows = conn.execute("SELECT id, filehash FROM downloads ORDER BY id").fetchall()
            self.assertEqual(rows, [
                ("s_a", hashlib.sha256(b"abc").hexdigest()),
                ("s_b", None),
            ])


    def test_run_writes_metrics_to_log_and_runs_table(self):
        """Итог запуска: строки по мостам, источникам поиска и месту в логе и запись в таблице runs."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [{'id': str(n), 'artist': 'A', 'title': f'T{n}',\n"
                "             'download_url': f'https://x/{n}.mp3'} for n in range(3)]\n"
                "def resolve_track(session, track):\n"
                "    if track['title'] == 'T2':\n"
                "        return None\n"
                "    session.search_stats_seen = track['_search_stats']\n"
                "    track['_search_stats'].setdefault('src', {'queries': 0, 'chosen': 0})\n"
                "    track['_search_stats']['src']['queries'] += 1\n"
                "    track['_search_stats']['src']['chosen'] += 1\n"
                "    return {'download_url': track['download_url'], 'referer': 'r', 'src': 's'}\n",
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "download_paths": [{"path": str(destination), "max_size_mb": 100}],
                "sites": [{"site_name": "S", "site_script": str(driver), "base_url": "b", "categories": ["/"]}],
            }), encoding="utf-8")

            def get(url, **kwargs):
                body = url.encode()
                response = MagicMock(status_code=200, headers={"content-length": str(len(body))})
                response.iter_content.return_value = [body]
                context = MagicMock()
                context.__enter__.return_value = response
                return context

            scraper = MagicMock()
            scraper.get.side_effect = get
            with (
                patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                patch.object(music_downloader, "setup_logging"),
                patch.object(music_downloader, "cleanup_old_logs"),
                patch.object(music_downloader.time, "sleep"),
                patch.object(music_downloader.cloudscraper, "create_scraper", return_value=scraper),
                self.assertLogs("Engine", level="INFO") as logs,
            ):
                music_downloader.run()

            text = "\n".join(logs.output)
            self.assertIn("Итог S: в списках 3, новых в очереди 3, скачано 2", text)
            self.assertIn("не найдено 1", text)
            self.assertIn("Итог поиска src: запросов 2, кандидатов", text.replace("кандидатов 0", "кандидатов"))
            self.assertIn("Место:", text)
            self.assertIn("Итог запуска: скачано 2", text)
            self.assertIn("bari: S=2, total=2", text)  # прежняя строка итога сохранена
            with sqlite3.connect(root / "db.sqlite") as conn:
                row = conn.execute(
                    "SELECT exit_code, downloaded, bytes, sites, sources, destinations FROM runs"
                ).fetchone()
            self.assertEqual((row[0], row[1]), (0, 2))
            self.assertGreater(row[2], 0)
            self.assertEqual(json.loads(row[3])["S"]["not_found"], 1)
            self.assertEqual(json.loads(row[4])["src"]["chosen"], 2)
            self.assertEqual(json.loads(row[5])[0]["limit"], 100 * 1048576)

    def test_run_records_exit_code_when_limit_stops_run(self):
        """Остановка по лимиту папки (код 2) всё равно сохраняет итог запуска в runs."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            destination = root / "music"
            destination.mkdir()
            driver = root / "driver.py"
            driver.write_text(
                "def get_tracks(scraper, base_url, category):\n"
                "    return [{'id': '1', 'artist': 'A', 'title': 'one', 'download_url': 'https://x/1.mp3'}]\n",
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "download_paths": [{"path": str(destination), "max_size_mb": 2 / 1048576}],
                "sites": [{"site_name": "S", "site_script": str(driver), "base_url": "b", "categories": ["/"]}],
            }), encoding="utf-8")
            response = MagicMock(status_code=200, headers={})
            response.iter_content.return_value = [b"abc"]
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
            with sqlite3.connect(root / "db.sqlite") as conn:
                self.assertEqual(conn.execute("SELECT exit_code, downloaded FROM runs").fetchone(), (2, 0))

    def test_run_rejects_malformed_search_settings(self):
        """Неверные search_sources / artist_aliases дают код 2 до создания скрапера."""
        for bad in ({"search_sources": {"name": "x"}}, {"artist_aliases": ["Madi"]}, {"artist_aliases": [[1]]}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                destination = root / "music"
                destination.mkdir()
                config_path = root / "config.json"
                config_path.write_text(json.dumps({"download_paths": [{"path": str(destination)}], "sites": [], **bad}),
                                       encoding="utf-8")
                with (
                    patch.multiple(music_downloader, CONFIG_PATH=str(config_path), DB_PATH=str(root / "db.sqlite")),
                    patch.object(music_downloader, "setup_logging"),
                    patch.object(music_downloader.cloudscraper, "create_scraper") as create_scraper,
                    self.assertRaises(SystemExit) as error,
                ):
                    music_downloader.run()
                self.assertEqual(error.exception.code, 2)
                create_scraper.assert_not_called()


class TestDownloadHardening(unittest.TestCase):
    def test_deadline_aborts_track_and_removes_part(self):
        class Response:
            def iter_content(self, chunk_size):
                yield b"abc"
                yield b"def"

        with tempfile.TemporaryDirectory() as root:
            # старт дедлайна = 0, после первого чанка «прошло» 100 с при лимите 10 с
            with patch.object(music_downloader.time, "monotonic", side_effect=[0, 1, 100]):
                with self.assertRaises(music_downloader.TrackDownloadError):
                    save_response(Response(), [{"path": root}], "t.mp3", max_seconds=10)
            self.assertEqual(os.listdir(root), [])

    def test_file_size_limit_aborts_track_and_is_not_folder_limit(self):
        class Response:
            def iter_content(self, chunk_size):
                yield b"abcd"
                yield b"efgh"

        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(music_downloader.TrackDownloadError) as error:
                save_response(Response(), [{"path": root}], "t.mp3", max_bytes=6)
            self.assertNotIsInstance(error.exception, music_downloader.DownloadLimitExceeded)
            self.assertEqual(os.listdir(root), [])
            with self.assertRaises(music_downloader.TrackDownloadError):
                save_response(Response(), [{"path": root}], "t.mp3", expected_size=7, max_bytes=6)

    def test_limits_not_exceeded_save_normally(self):
        class Response:
            def iter_content(self, chunk_size):
                yield b"abc"

        with tempfile.TemporaryDirectory() as root:
            size, _, _ = save_response(Response(), [{"path": root}], "t.mp3", 3, max_seconds=60, max_bytes=3)
            self.assertEqual(size, 3)

    def test_symlink_at_part_path_is_track_error(self):
        class Response:
            def iter_content(self, chunk_size):
                yield b"abc"

        with tempfile.TemporaryDirectory() as root:
            victim = os.path.join(root, "victim")
            Path(victim).write_text("keep")
            os.symlink(victim, os.path.join(root, "t.mp3.part"))
            with self.assertRaises(OSError):
                save_response(Response(), [{"path": root}], "t.mp3")
            self.assertEqual(Path(victim).read_text(), "keep")

    def test_require_http_url(self):
        self.assertEqual(music_downloader.require_http_url("https://a.test/x.mp3"), "https://a.test/x.mp3")
        for bad in ("file:///etc/passwd", "ftp://a.test/x", "/local/path", "", None):
            with self.assertRaises(ValueError):
                music_downloader.require_http_url(bad)

    def test_resolve_driver_path_only_py_inside_drivers(self):
        ok = music_downloader.resolve_driver_path("drivers/music_downloader_sefon_pro.py")
        self.assertTrue(ok.endswith("music_downloader_sefon_pro.py"))
        for bad in ("../music_downloader.py", "/etc/passwd.py", "drivers/../music_downloader.py", "drivers/x.txt"):
            with self.assertRaises(ValueError):
                music_downloader.resolve_driver_path(bad)

    def test_safe_filename_length_windows_names_and_trailing_dots(self):
        long_name = safe_filename("Я" * 300 + ".mp3")
        self.assertLessEqual(len(long_name.encode("utf-8")), 200)
        self.assertTrue(long_name.endswith(".mp3"))
        self.assertEqual(safe_filename("Name. . .mp3"), "Name.mp3")
        self.assertEqual(safe_filename("CON.mp3"), "_CON.mp3")
        self.assertEqual(safe_filename("lpt1.mp3"), "_lpt1.mp3")
        self.assertEqual(safe_filename("Console.mp3"), "Console.mp3")
        self.assertEqual(safe_filename("Mr. Smith"), "Mr. Smith")


if __name__ == "__main__":
    unittest.main()
