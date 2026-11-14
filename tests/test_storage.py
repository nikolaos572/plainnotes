"""The data folder layout (notes/ and testing/) and the storage check at startup."""

import errno
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import __main__ as entry
from app import fsutil, storage
from app.config import ConfigError
from tests.conftest import CSRF, PASSWORD, SECRET_KEY, USERNAME, login

NOTE_ID = "20261018-143205-k3f9"


def snapshot(folder: Path) -> dict[str, tuple[bytes | None, int]]:
    """Every path below folder with its bytes (None for folders) and mtime."""
    result = {}
    for path in sorted(folder.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        result[str(path.relative_to(folder))] = (data, info.st_mtime_ns)
    return result


def unsupported(flag: int, code: int = errno.EINVAL):
    """A renameat2 that rejects one flag the way NFS, SMB, exFAT or old ZFS do."""
    real = fsutil._renameat2

    def rename(first: Path, second: Path, flags: int) -> None:
        if flags == flag:
            raise OSError(code, os.strerror(code))
        real(first, second, flags)

    return rename


def test_fresh_data_folder_gets_notes_and_testing(tmp_path):
    prepared = storage.prepare(tmp_path)
    passed = storage.CHECK_PASSED.format(folder=tmp_path / "testing")
    assert prepared == storage.Prepared(info=[passed])
    assert "RENAME_EXCHANGE" in passed and "RENAME_NOREPLACE" in passed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes", "testing"]
    assert list((tmp_path / "notes").iterdir()) == list((tmp_path / "testing").iterdir()) == []


def test_missing_data_folder(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        storage.prepare(tmp_path / "missing")
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        storage.prepare(file)


def test_notes_are_never_touched(tmp_path):
    folder = tmp_path / "notes" / NOTE_ID
    (folder / "attachments").mkdir(parents=True)
    (folder / "Note.md").write_bytes(b"content")
    (folder / "attachments" / "file.bin").write_bytes(b"\0" * 10)
    (tmp_path / "notes" / "Loose.md").write_bytes(b"to import")
    (tmp_path / "notes" / ".plainnotes-tmp-leftover").write_bytes(b"left alone")
    before = snapshot(tmp_path / "notes")
    storage.prepare(tmp_path)
    storage.prepare(tmp_path)
    assert snapshot(tmp_path / "notes") == before


def test_testing_is_emptied_without_following_links(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    data = tmp_path / "data"
    testing = data / "testing"
    (testing / "nested" / "deeper").mkdir(parents=True)
    (testing / "leftover").write_bytes(b"from a killed check")
    (testing / "nested" / "deeper" / "file").write_bytes(b"x")
    (testing / "nested" / "link-to-outside").symlink_to(outside, target_is_directory=True)
    (testing / "file-link").symlink_to(outside / "keep.txt")
    (testing / "dir-link").symlink_to(outside, target_is_directory=True)
    (testing / "dangling").symlink_to(tmp_path / "missing")
    storage.prepare(data)
    assert list(testing.iterdir()) == []
    assert (outside / "keep.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("name", ["notes", "testing"])
@pytest.mark.parametrize("kind", ["link", "file"])
def test_links_or_files_instead_of_folders_are_refused(tmp_path, name, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    data = tmp_path / "data"
    data.mkdir()
    if kind == "link":
        (data / name).symlink_to(outside, target_is_directory=True)
    else:
        (data / name).write_bytes(b"not a folder")
    with pytest.raises(ConfigError, match="must be a folder"):
        storage.prepare(data)
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("old", [NOTE_ID, ".trash"])
@pytest.mark.parametrize("read_only", [False, True])
def test_layout_of_earlier_versions_is_refused(tmp_path, old, read_only):
    (tmp_path / old).mkdir()
    (tmp_path / old / "Note.md").write_bytes(b"only copy")
    with pytest.raises(ConfigError, match="Notes now live in"):
        storage.prepare(tmp_path, read_only=read_only)
    assert sorted(p.name for p in tmp_path.iterdir()) == [old]
    assert (tmp_path / old / "Note.md").read_bytes() == b"only copy"


def test_loose_files_in_the_data_folder_get_a_warning(tmp_path):
    (tmp_path / "Dropped.md").write_bytes(b"stays")
    prepared = storage.prepare(tmp_path)
    assert len(prepared.warnings) == 1
    assert str(tmp_path / "notes") in prepared.warnings[0]
    assert (tmp_path / "Dropped.md").read_bytes() == b"stays"


def test_read_only_creates_empties_and_probes_nothing(tmp_path, monkeypatch):
    def no_writes(*_args, **_kwargs):
        raise AssertionError("write in read-only mode")

    monkeypatch.setattr(fsutil, "create_temp_file", no_writes)
    monkeypatch.setattr(fsutil, "missing_atomic_renames", no_writes)
    (tmp_path / "notes").mkdir()
    (tmp_path / "testing").mkdir()
    (tmp_path / "testing" / "leftover").write_bytes(b"kept")
    before = snapshot(tmp_path)
    skipped = storage.Prepared(info=[storage.CHECK_SKIPPED])
    assert storage.prepare(tmp_path, read_only=True) == skipped
    assert snapshot(tmp_path) == before


def test_read_only_needs_the_notes_folder(tmp_path):
    with pytest.raises(ConfigError, match="read-only mode needs it"):
        storage.prepare(tmp_path, read_only=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("flag", "name"),
    [(fsutil._RENAME_EXCHANGE, "RENAME_EXCHANGE"), (fsutil._RENAME_NOREPLACE, "RENAME_NOREPLACE")],
)
def test_unsupported_storage_turns_saving_off(tmp_path, monkeypatch, flag, name):
    monkeypatch.setattr(fsutil, "_renameat2", unsupported(flag))
    prepared = storage.prepare(tmp_path)
    assert prepared.read_only_reason.startswith("Saving is turned off")
    assert f"({name})" in prepared.read_only_reason
    assert list((tmp_path / "testing").iterdir()) == []


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (errno.ENOSPC, storage.DISK_FULL),
        (errno.EDQUOT, storage.DISK_FULL),
        (errno.EROFS, storage.READ_ONLY_STORAGE),
    ],
    ids=["ENOSPC", "EDQUOT", "EROFS"],
)
def test_full_or_read_only_storage_turns_saving_off(tmp_path, monkeypatch, code, reason):
    # The check cannot write, but the notes can still be shown: start read-only, do not stop.
    (tmp_path / "notes" / NOTE_ID).mkdir(parents=True)

    def cannot_write(_folder: Path) -> list[str]:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(fsutil, "missing_atomic_renames", cannot_write)
    assert storage.prepare(tmp_path).read_only_reason == reason


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EROFS])
def test_full_or_read_only_storage_without_notes_stops_startup(tmp_path, monkeypatch, code):
    def cannot_create(*_args: object, **_kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(Path, "mkdir", cannot_create)
    with pytest.raises(ConfigError, match=f"cannot be prepared: {os.strerror(code)}") as raised:
        storage.prepare(tmp_path)
    assert "chown" not in str(raised.value)  # changing the owner cannot help here


def test_other_check_failures_stop_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(fsutil, "_renameat2", unsupported(fsutil._RENAME_EXCHANGE, errno.EIO))
    with pytest.raises(ConfigError, match="cannot be prepared"):
        storage.prepare(tmp_path)
    assert list((tmp_path / "testing").iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
@pytest.mark.parametrize("locked", ["data", "testing", "notes"])
def test_not_writable_message(tmp_path, locked):
    data = tmp_path / "data"
    (data / "notes").mkdir(parents=True)
    (data / "testing").mkdir()
    folder = data if locked == "data" else data / locked
    if locked == "data":
        (data / "notes").rmdir()
        (data / "testing").rmdir()
    folder.chmod(0o500)
    try:
        with pytest.raises(ConfigError) as info:
            storage.prepare(data)
    finally:
        folder.chmod(0o700)
    uid, gid = os.getuid(), os.getgid()
    assert str(info.value) == (
        f"Data directory {data} is not writable by uid {uid}. "
        f"Fix it on the host with: sudo chown -R {uid}:{gid} <your data folder>"
    )


def test_different_filesystems_get_a_warning(tmp_path, monkeypatch):
    real_stat = os.stat
    testing = tmp_path / "testing"

    class Other:
        st_dev = -1

    def stat(path, *args, **kwargs):
        follows = kwargs.get("follow_symlinks", True)
        if follows and Path(path) == testing:
            return Other()
        return real_stat(path, *args, **kwargs)

    storage.prepare(tmp_path)
    monkeypatch.setattr(os, "stat", stat)
    prepared = storage.prepare(tmp_path)
    assert prepared.read_only_reason == ""
    assert any("different filesystems" in warning for warning in prepared.warnings)


@pytest.fixture
def server_start(tmp_path, monkeypatch, capsys):
    """Run app.__main__.main() up to uvicorn.run and return the app it would serve."""
    for name, value in {
        "USERNAME": USERNAME,
        "PASSWORD": PASSWORD,
        "SECRET_KEY": SECRET_KEY,
        "DATA_DIR": str(tmp_path),
        "COOKIE_SECURE": "false",
    }.items():
        monkeypatch.setenv("PLAINNOTES_" + name, value)
    served = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **_kwargs: served.append(app))
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level

    def start():
        entry.main()
        return served[0], capsys.readouterr().out

    yield start
    root.handlers[:] = handlers
    root.setLevel(level)


def test_startup_on_unsupported_storage_serves_read_only(server_start, monkeypatch, tmp_path):
    monkeypatch.setattr(fsutil, "_renameat2", unsupported(fsutil._RENAME_EXCHANGE))
    app, log = server_start()
    reason = app.state.settings.read_only_reason
    assert reason.startswith("Saving is turned off") and "(RENAME_EXCHANGE)" in reason
    assert f"ERROR app: {reason}" in log
    assert "Storage check passed" not in log
    (tmp_path / "notes" / "Loose.md").write_bytes(b"not imported in read-only mode")
    with TestClient(app, headers=CSRF) as client:
        login(client)
        assert client.get("/api/session").json() == {
            "username": USERNAME,
            "read_only": True,
            "read_only_reason": reason,
            "check_attachments_hash": False,
        }
        assert client.get("/api/notes").json() == []
        created = client.post("/api/notes", json={"title": "Blocked", "content": ""})
        assert created.status_code == 403
        assert created.json()["code"] == "read_only"
    assert sorted(p.name for p in (tmp_path / "notes").iterdir()) == ["Loose.md"]


def test_startup_on_supported_storage_allows_saving(server_start, tmp_path):
    app, log = server_start()
    assert app.state.settings.read_only is False
    assert "Saving is turned off" not in log
    assert "read-only False, check attachments hash False\n" in log
    passed = storage.CHECK_PASSED.format(folder=tmp_path / "testing")
    assert f"INFO app: {passed}\n" in log
    with TestClient(app, headers=CSRF) as client:
        login(client)
        assert client.get("/api/session").json()["read_only_reason"] == ""
        created = client.post("/api/notes", json={"title": "Saved", "content": "x"})
        assert created.status_code == 201
    assert (tmp_path / "notes" / created.json()["id"] / "Saved.md").read_bytes() == b"x"
