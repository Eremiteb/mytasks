import os
import sys
import tempfile
import unittest

# Добавляем путь к music_downloader в sys.path для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "music_downloader")))

from music_downloader import capitalize_first, safe_filename, save_response, track_identity

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


if __name__ == "__main__":
    unittest.main()
