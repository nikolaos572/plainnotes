"""Safe paths, symlink handling, atomic writes and temp-file cleanup."""

import logging
import os
import stat
from pathlib import Path

import pytest

from app import fsutil
from app.fsutil import DataDir


def anything(_name: str) -> bool:
    return True


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "a\\b", "/etc", "a\x00b", "x" * 256, "é" * 128, "\udcff"],
)
def test_safe_child_rejects(tmp_path: Path, name: str) -> None:
    with pytest.raises(fsutil.InvalidName):
        fsutil.safe_child(tmp_path, name, anything)


def test_safe_child_applies_rule(tmp_path: Path) -> None:
    assert fsutil.safe_child(tmp_path, "ok.md", anything) == tmp_path / "ok.md"
    assert fsutil.safe_child(tmp_path, "x" * 255, anything) == tmp_path / ("x" * 255)
    with pytest.raises(fsutil.InvalidName):
        fsutil.safe_child(tmp_path, "ok.md", lambda name: False)


def test_real_file_and_dir_checks(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    (tmp_path / "dir").mkdir()
    (tmp_path / "file-link").symlink_to(tmp_path / "file")
    (tmp_path / "dir-link").symlink_to(tmp_path / "dir")
    assert fsutil.is_real_file(tmp_path / "file")
    assert fsutil.is_real_dir(tmp_path / "dir")
    assert not fsutil.is_real_file(tmp_path / "file-link")
    assert not fsutil.is_real_dir(tmp_path / "dir-link")
    assert not fsutil.is_real_file(tmp_path / "missing")


def test_read_file_refuses_symlinks_and_fifos(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"data")
    (tmp_path / "link").symlink_to(tmp_path / "file")
    os.mkfifo(tmp_path / "fifo")
    assert fsutil.read_file(tmp_path / "file") == b"data"
    with pytest.raises(OSError):
        fsutil.read_file(tmp_path / "link")
    with pytest.raises(OSError):
        fsutil.read_file(tmp_path / "fifo")


def test_atomic_write_creates_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    fsutil.atomic_write(target, b"one")
    assert target.read_bytes() == b"one"
    target.chmod(0o640)
    fsutil.atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]


@pytest.mark.parametrize("failing", ["replace", "fsync", "write"])
def test_atomic_write_failure_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    target = tmp_path / "note.md"
    target.write_bytes(b"original")

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("simulated failure")

    if failing == "write":
        monkeypatch.setattr(fsutil.os, "fdopen", lambda *a, **k: _FailingFile(*a))
    else:
        monkeypatch.setattr(fsutil.os, failing, fail)
    with pytest.raises(OSError, match="simulated"):
        fsutil.atomic_write(target, b"new content")
    monkeypatch.undo()
    assert target.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]


class _FailingFile:
    def __init__(self, fd: int, *_: object) -> None:
        self.fd = fd

    def __enter__(self) -> "_FailingFile":
        return self

    def __exit__(self, *_: object) -> None:
        os.close(self.fd)

    def write(self, _data: bytes) -> None:
        raise OSError("simulated failure")


def test_atomic_write_disk_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil.os, "fsync", full)
    with pytest.raises(fsutil.StorageFull):
        fsutil.atomic_write(tmp_path / "x.md", b"data")
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def test_remove_temp_entries(tmp_path: Path) -> None:
    (tmp_path / ".plainnotes-tmp-file").write_text("x")
    (tmp_path / ".plainnotes-tmp-dir").mkdir()
    (tmp_path / ".plainnotes-tmp-dir" / "inner").write_text("x")
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    (outside / "keep").write_text("x")
    (tmp_path / ".plainnotes-tmp-link").symlink_to(outside)
    (tmp_path / "keep.md").write_text("x")
    assert fsutil.remove_temp_entries(tmp_path) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["keep.md"]
    assert (outside / "keep").exists()


def test_warn_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data_dir = DataDir(tmp_path)
    logger = logging.getLogger("test")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            data_dir.warn_once(logger, "Skipping %r", "folder")
        data_dir.warn_once(logger, "Skipping %r", "other")
    assert [r.getMessage() for r in caplog.records] == ["Skipping 'folder'", "Skipping 'other'"]
