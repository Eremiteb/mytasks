import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "mht_to_fb2.py"
SPEC = importlib.util.spec_from_file_location("mht_to_fb2", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
mht = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mht)

MHT = (
    "From: Автор <a@example.invalid>\n"
    "Subject: Тема\n"
    "MIME-Version: 1.0\n"
    "Content-Type: text/html; charset=utf-8\n\n"
    "<html><head><title>Заголовок</title></head><body><p>текст</p></body></html>\n"
)


def fake_pandoc(returncode=0, content="<FictionBook/>"):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if returncode == 0:
            Path(cmd[cmd.index("-o") + 1]).write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", "boom")

    return run, calls


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "book.mht"
    src.write_text(MHT, encoding="utf-8")
    log = tmp_path / "log.jsonl"
    with patch.object(mht, "default_log_path", return_value=log), \
            patch.object(mht.shutil, "which", return_value="/usr/bin/pandoc"):
        yield tmp_path, src


def run_main(tmp_path, *extra):
    with patch("sys.argv", ["mht_to_fb2.py", str(tmp_path), *extra]):
        return mht.main()


def test_pandoc_uses_sandbox_and_timeout(env):
    tmp_path, src = env
    run, calls = fake_pandoc()
    with patch.object(mht.subprocess, "run", run):
        assert run_main(tmp_path, "--timeout", "42") == 0
    cmd, kwargs = calls[0]
    assert "--sandbox" in cmd
    assert kwargs["timeout"] == 42
    assert (tmp_path / "book.fb2").read_text(encoding="utf-8") == "<FictionBook/>"
    assert not src.exists()
    assert not list(tmp_path.glob("*.part"))


def test_dry_run_writes_nothing(env):
    tmp_path, src = env
    run, calls = fake_pandoc()
    with patch.object(mht.subprocess, "run", run):
        assert run_main(tmp_path, "--dry-run") == 0
    assert calls == []
    assert src.exists()
    assert not (tmp_path / "book.fb2").exists()


def test_source_kept_when_pandoc_fails(env):
    tmp_path, src = env
    run, _ = fake_pandoc(returncode=1)
    with patch.object(mht.subprocess, "run", run):
        run_main(tmp_path)
    assert src.exists()
    assert not (tmp_path / "book.fb2").exists()


def test_source_kept_when_result_empty(env):
    tmp_path, src = env
    run, _ = fake_pandoc(content="")
    with patch.object(mht.subprocess, "run", run):
        run_main(tmp_path)
    assert src.exists()
    assert not (tmp_path / "book.fb2").exists()


def test_timeout_is_reported_and_source_kept(env):
    tmp_path, src = env

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    with patch.object(mht.subprocess, "run", run):
        run_main(tmp_path)
    assert src.exists()


def test_decode_best_unknown_declared_charset():
    assert mht.decode_best("привет".encode("cp1251"), "no-such-charset") == "привет"


def test_extract_title():
    assert mht.extract_title("<title> A  b </title>") == "A b"
    assert mht.extract_title("<p>x</p>") is None
