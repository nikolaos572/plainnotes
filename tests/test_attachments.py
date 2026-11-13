"""Attachments: name rules, streaming upload, limits, serving headers and deletion."""

import errno
import os
import threading
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import attachments, fsutil, notes, storage
from tests.conftest import CSRF, PASSWORD, USERNAME, login

SANDBOX_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
MB = 1024 * 1024


# --- name rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("photo.png", "photo.png"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        ('a:b*c?d"e<f>g|h.txt', "a_b_c_d_e_f_g_h.txt"),
        ("tab\there\x7f.txt", "tab_here_.txt"),
        ("  .hidden.  ", "hidden"),
        ("...", "file"),
        ("", "file"),
        ("dir/", "file"),
        ("ελληνικά ☕.md", "ελληνικά ☕.md"),
        ("lone\udc80.txt", "lone_.txt"),
    ],
)
def test_sanitize_name(requested: str, expected: str) -> None:
    assert attachments.sanitize_name(requested) == expected
    assert attachments.is_final_name(expected)


@pytest.mark.parametrize(
    "requested",
    ["a" * 300 + ".png", "é" * 200 + ".txt", "a" * 300, "x." + "e" * 300, "😀" * 100 + ".jpeg"],
)
def test_long_names_are_shortened(requested: str) -> None:
    name = attachments.sanitize_name(requested)
    assert len(name.encode("utf-8")) <= 255
    assert attachments.is_final_name(name)
    if requested.endswith((".png", ".txt", ".jpeg")):
        assert name.endswith(requested[requested.rfind(".") :])


@pytest.mark.parametrize("name", [".hidden", "a/b", "a\\b", " x", "x ", "x.", "a:b", "", "a\x00b"])
def test_not_final_names(name: str) -> None:
    assert not attachments.is_final_name(name)


def test_unique_name() -> None:
    assert attachments.unique_name("file.txt", set()) == "file.txt"
    assert attachments.unique_name("file.txt", {"file.txt"}) == "file (1).txt"
    taken = {"file.txt", "file (1).txt"}
    assert attachments.unique_name("FILE.TXT", taken) == "FILE (2).TXT"
    assert attachments.unique_name("archive.tar.gz", {"archive.tar.gz"}) == "archive.tar (1).gz"
    assert attachments.unique_name("noext", {"noext"}) == "noext (1)"
    long = attachments.sanitize_name("a" * 300 + ".png")
    renamed = attachments.unique_name(long, {attachments.name_key(long)})
    assert renamed.endswith(" (1).png") and len(renamed.encode()) <= 255


@pytest.mark.parametrize(
    ("name", "content_type", "inline"),
    [
        ("a.png", "image/png", True),
        ("a.JPG", "image/jpeg", True),
        ("a.jpeg", "image/jpeg", True),
        ("a.ico", "image/x-icon", True),
        ("a.opus", "audio/ogg", True),
        ("a.m4v", "video/mp4", True),
        ("a.mov", "video/quicktime", True),
        ("a.md", "text/plain; charset=utf-8", True),
        ("a.csv", "text/plain; charset=utf-8", True),
        ("a.html", "application/octet-stream", False),
        ("a.htm", "application/octet-stream", False),
        ("a.svg", "application/octet-stream", False),
        ("a.xml", "application/octet-stream", False),
        ("a.pdf", "application/octet-stream", False),
        ("a.js", "application/octet-stream", False),
        ("a.json", "application/octet-stream", False),
        ("png", "application/octet-stream", False),
        ("a.png.html", "application/octet-stream", False),
    ],
)
def test_content_type(name: str, content_type: str, inline: bool) -> None:
    assert attachments.content_type(name) == (content_type, inline)


# --- API ------------------------------------------------------------------------------


@pytest.fixture
def note_id(client: TestClient) -> str:
    return client.post("/api/notes", json={"title": "Has files", "content": ""}).json()["id"]


def upload(client: TestClient, note_id: str, name: str, body: bytes = b"data") -> httpx.Response:
    return client.put(
        f"/api/notes/{note_id}/attachments/{name}",
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )


def folder(data_root: Path, note_id: str) -> Path:
    return data_root / note_id / "attachments"


def test_upload_list_and_download(client: TestClient, note_id: str, data_root: Path) -> None:
    response = upload(client, note_id, "compose.yml", b"services: {}\n")
    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["size"]) == ("compose.yml", 13)
    assert body["modified"].endswith("Z")
    assert (folder(data_root, note_id) / "compose.yml").read_bytes() == b"services: {}\n"
    upload(client, note_id, "Zebra.txt")
    upload(client, note_id, "apple.txt")
    listed = client.get(f"/api/notes/{note_id}/attachments").json()
    assert [a["name"] for a in listed] == ["apple.txt", "compose.yml", "Zebra.txt"]
    assert client.get(f"/api/notes/{note_id}").json()["attachments"] == listed
    downloaded = client.get(f"/api/notes/{note_id}/attachments/compose.yml")
    assert downloaded.content == b"services: {}\n"


def test_names_are_sanitized_and_deduplicated(client: TestClient, note_id: str) -> None:
    names = [
        upload(client, note_id, name).json()["name"]
        for name in ["photo.png", "photo.png", "PHOTO.png", "a%5Cb.txt", "%2E%2E", "a%3Ab.txt"]
    ]
    assert names == ["photo.png", "photo (1).png", "PHOTO (2).png", "b.txt", "file", "a_b.txt"]


def test_upload_needs_an_existing_note(client: TestClient, note_id: str) -> None:
    assert upload(client, "20260101-000000-none", "x.txt").status_code == 404
    client.delete(f"/api/notes/{note_id}")
    assert upload(client, note_id, "x.txt").status_code == 404


def test_size_limit_from_content_length(app_factory: Callable, data_root: Path) -> None:
    with TestClient(app_factory(max_upload_mb=1), headers=CSRF) as client:
        login(client)
        note = client.post("/api/notes", json={"title": "n", "content": ""}).json()["id"]
        assert upload(client, note, "ok.bin", b"x" * MB).status_code == 201
        response = upload(client, note, "big.bin", b"x" * (MB + 1))
        assert response.status_code == 413
        assert response.json()["code"] == "too_large"
        assert sorted(p.name for p in folder(data_root, note).iterdir()) == ["ok.bin"]


def chunks(total: int, size: int = MB) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        yield b"x" * piece
        sent += piece


@contextmanager
def live_client(base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, headers=CSRF, timeout=60) as client:
        response = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        assert response.status_code == 204
        yield client


def test_streamed_size_limit(live_server: Callable, data_root: Path) -> None:
    with live_client(live_server(max_upload_mb=1)) as client:
        note = client.post("/api/notes", json={"title": "n", "content": ""}).json()["id"]
        response = client.put(f"/api/notes/{note}/attachments/big.bin", content=chunks(3 * MB))
        assert response.status_code == 413
        assert list(folder(data_root, note).iterdir()) == []


def test_streaming_50_mb_with_constant_memory(live_server: Callable, data_root: Path) -> None:
    with live_client(live_server()) as client:
        note = client.post("/api/notes", json={"title": "n", "content": ""}).json()["id"]
        tracemalloc.start()
        try:
            response = client.put(
                f"/api/notes/{note}/attachments/big.bin", content=chunks(50 * MB, 256 * 1024)
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert response.status_code == 201, response.text
    assert response.json()["size"] == 50 * MB
    assert (folder(data_root, note) / "big.bin").stat().st_size == 50 * MB
    assert peak < 16 * MB, f"peak traced memory {peak / MB:.1f} MB"


def test_failure_removes_temp_file(
    client: TestClient, note_id: str, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: attachments.Upload, chunk: bytes) -> None:
        raise OSError(errno.EIO, "simulated I/O error")

    monkeypatch.setattr(attachments.Upload, "write", broken)
    response = upload(client, note_id, "x.bin")
    assert response.status_code == 500
    assert response.json()["code"] == "internal"
    assert list(folder(data_root, note_id).iterdir()) == []


def test_disk_full(
    client: TestClient, note_id: str, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(self: attachments.Upload, chunk: bytes) -> None:
        raise fsutil.StorageFull

    monkeypatch.setattr(attachments.Upload, "write", full)
    response = upload(client, note_id, "x.bin")
    assert response.status_code == 507
    assert response.json()["code"] == "storage_full"
    assert list(folder(data_root, note_id).iterdir()) == []


@pytest.mark.parametrize(
    ("name", "content_type", "disposition"),
    [
        ("image.png", "image/png", 'inline; filename="image.png"'),
        ("notes.txt", "text/plain; charset=utf-8", 'inline; filename="notes.txt"'),
        ("clip.mp4", "video/mp4", 'inline; filename="clip.mp4"'),
        ("page.html", "application/octet-stream", 'attachment; filename="page.html"'),
        ("drawing.svg", "application/octet-stream", 'attachment; filename="drawing.svg"'),
        ("paper.pdf", "application/octet-stream", 'attachment; filename="paper.pdf"'),
        ("data.json", "application/octet-stream", 'attachment; filename="data.json"'),
        ("καφές.png", "image/png", "inline; filename*=utf-8''%CE%BA%CE%B1%CF%86%CE%AD%CF%82.png"),
    ],
)
def test_serving_headers(
    client: TestClient, note_id: str, name: str, content_type: str, disposition: str
) -> None:
    upload(client, note_id, name, b"<html><script>alert(1)</script></html>")
    response = client.get(f"/api/notes/{note_id}/attachments/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["content-disposition"] == disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == SANDBOX_CSP
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["x-frame-options"] == "DENY"


def test_range_requests(client: TestClient, note_id: str) -> None:
    upload(client, note_id, "clip.mp4", b"0123456789")
    response = client.get(
        f"/api/notes/{note_id}/attachments/clip.mp4", headers={"Range": "bytes=2-5"}
    )
    assert response.status_code == 206
    assert response.content == b"2345"


def test_only_final_names_are_served(client: TestClient, note_id: str, data_root: Path) -> None:
    upload(client, note_id, "real.txt")
    (folder(data_root, note_id) / ".DS_Store").write_bytes(b"x")
    (folder(data_root, note_id) / "trailing ").write_bytes(b"x")
    assert [a["name"] for a in client.get(f"/api/notes/{note_id}/attachments").json()] == [
        "real.txt"
    ]
    for name in [".DS_Store", "trailing%20", "missing.txt", ".plainnotes-tmp-x"]:
        assert client.get(f"/api/notes/{note_id}/attachments/{name}").status_code == 404
        assert client.delete(f"/api/notes/{note_id}/attachments/{name}").status_code == 404


def test_symlinks_are_refused(
    client: TestClient, note_id: str, data_root: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    upload(client, note_id, "real.txt")
    (folder(data_root, note_id) / "link.txt").symlink_to(secret)
    assert [a["name"] for a in client.get(f"/api/notes/{note_id}/attachments").json()] == [
        "real.txt"
    ]
    assert client.get(f"/api/notes/{note_id}/attachments/link.txt").status_code == 404
    assert client.delete(f"/api/notes/{note_id}/attachments/link.txt").status_code == 404
    assert secret.read_text() == "secret"


def test_symlinked_attachments_folder(
    client: TestClient, data_root: Path, tmp_path: Path, make_note: Callable
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    path = make_note("Linked", "x", note_id="20260101-000000-link")
    (path.parent / "attachments").symlink_to(outside)
    note = "20260101-000000-link"
    assert client.get(f"/api/notes/{note}/attachments").json() == []
    assert client.get(f"/api/notes/{note}").json()["attachments"] == []
    assert client.get(f"/api/notes/{note}/attachments/secret.txt").status_code == 404
    assert upload(client, note, "new.txt").status_code == 404
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_delete(client: TestClient, note_id: str, data_root: Path) -> None:
    upload(client, note_id, "gone.txt")
    assert client.delete(f"/api/notes/{note_id}/attachments/gone.txt").status_code == 204
    assert list(folder(data_root, note_id).iterdir()) == []
    assert client.delete(f"/api/notes/{note_id}/attachments/gone.txt").status_code == 404


def test_attachments_move_to_trash_with_the_note(
    client: TestClient, note_id: str, data_root: Path
) -> None:
    upload(client, note_id, "keep.png")
    client.delete(f"/api/notes/{note_id}")
    [entry] = list((data_root / ".trash").iterdir())
    assert (entry / "attachments" / "keep.png").exists()


def test_first_attachment_syncs_new_directory_parent(data_dir, monkeypatch):
    note = notes.create_note(data_dir, "Attachment durability", "content")
    calls = []
    sync = fsutil.fsync_dir

    def trace(path):
        calls.append(path)
        return sync(path)

    monkeypatch.setattr(fsutil, "fsync_dir", trace)
    upload = attachments.Upload(data_dir, note.ref.id)
    upload.write(b"first attachment")
    upload.finish(data_dir, "first.txt")
    assert calls[0] == note.ref.folder
    assert note.ref.folder / "attachments" in calls


def test_attachment_parent_sync_failure_is_retried(data_dir, monkeypatch):
    note = notes.create_note(data_dir, "Retry upload", "content")
    sync = fsutil.fsync_dir
    attempts = []

    def fail_parent(path):
        if path == note.ref.folder:
            attempts.append(path)
            raise OSError(errno.EIO, "injected parent sync failure")
        sync(path)

    monkeypatch.setattr(fsutil, "fsync_dir", fail_parent)
    for _ in range(2):
        with pytest.raises(OSError):
            attachments.Upload(data_dir, note.ref.id)
        assert list((note.ref.folder / "attachments").iterdir()) == []
    assert len(attempts) == 2
    monkeypatch.setattr(fsutil, "fsync_dir", sync)
    upload = attachments.Upload(data_dir, note.ref.id)
    upload.write(b"complete")
    upload.finish(data_dir, "retry.txt")
    assert (note.ref.folder / "attachments" / "retry.txt").read_bytes() == b"complete"


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EIO])
def test_discard_unlinks_temp_even_when_close_fails(data_dir, error):
    note = notes.create_note(data_dir, "Cleanup", "original")
    upload = attachments.Upload(data_dir, note.ref.id)
    original = upload.file

    class BrokenClose:
        def close(self):
            original.close()
            raise OSError(error, "injected close failure")

    upload.file = BrokenClose()
    expected = fsutil.StorageFull if error == errno.ENOSPC else OSError
    with pytest.raises(expected):
        upload.discard()
    assert not upload.temp.exists()
    assert note.ref.path.read_text() == "original"


def test_publication_collision_preserves_external_file(client, note_id, data_root, monkeypatch):
    publish = fsutil.rename_no_replace
    calls = []

    def collision(source, target):
        calls.append(target.name)
        if len(calls) == 1:
            target.write_bytes(b"external file")
        publish(source, target)

    monkeypatch.setattr(fsutil, "rename_no_replace", collision)
    response = upload(client, note_id, "race.txt", b"uploaded")
    assert response.status_code == 201
    assert response.json()["name"] == "race (1).txt"
    assert calls == ["race.txt", "race (1).txt"]
    assert (folder(data_root, note_id) / "race.txt").read_bytes() == b"external file"
    assert (folder(data_root, note_id) / "race (1).txt").read_bytes() == b"uploaded"
    assert not list(folder(data_root, note_id).glob(fsutil.TEMP_PREFIX + "*"))


@pytest.mark.parametrize("error", [errno.EIO, errno.ENOSPC, errno.ENOSYS, errno.EOPNOTSUPP])
def test_publication_failure_never_leaves_placeholder(
    client, note_id, data_root, monkeypatch, error
):
    assert upload(client, note_id, "keep.txt", b"keep").status_code == 201

    def fail(_source, _target):
        raise OSError(error, "injected publication failure")

    monkeypatch.setattr(fsutil, "rename_no_replace", fail)
    response = upload(client, note_id, "failed.txt", b"complete body")
    assert response.status_code == (507 if error == errno.ENOSPC else 500)
    assert sorted(p.name for p in folder(data_root, note_id).iterdir()) == ["keep.txt"]
    assert (folder(data_root, note_id) / "keep.txt").read_bytes() == b"keep"


def test_publication_flushes_file_before_rename_and_directory_after(data_dir, monkeypatch):
    import stat

    note = notes.create_note(data_dir, "Sync ordering", "note")
    transfer = attachments.Upload(data_dir, note.ref.id)
    transfer.write(b"complete body")
    calls = []
    real_sync = os.fsync
    real_publish = fsutil.rename_no_replace

    def sync(fd):
        calls.append("file" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory")
        real_sync(fd)

    def publish(source, target):
        assert source.read_bytes() == b"complete body"
        assert not target.exists()
        calls.append("publish")
        real_publish(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(fsutil, "rename_no_replace", publish)
    result = transfer.finish(data_dir, "complete.txt")
    assert result.size == len(b"complete body")
    assert calls == ["file", "publish", "directory"]


def test_failed_post_publication_sync_does_not_delete_completed_file(
    client,
    note_id,
    data_root,
    monkeypatch,
):
    real_sync = fsutil.fsync_dir

    def fail(folder_path):
        if folder_path.name == "attachments":
            raise OSError(errno.EIO, "injected directory sync failure")
        real_sync(folder_path)

    monkeypatch.setattr(fsutil, "fsync_dir", fail)
    response = upload(client, note_id, "complete.txt", b"complete body")
    assert response.status_code == 500
    assert (folder(data_root, note_id) / "complete.txt").read_bytes() == b"complete body"
    assert not list(folder(data_root, note_id).glob(fsutil.TEMP_PREFIX + "*"))


def test_intentional_empty_upload_is_preserved(client, note_id, data_root):
    response = upload(client, note_id, "empty.txt", b"")
    assert response.status_code == 201
    assert response.json()["size"] == 0
    storage.prepare(data_root.parent)  # a restart
    assert (folder(data_root, note_id) / "empty.txt").read_bytes() == b""


@pytest.mark.parametrize("stage", ["before", "after"])
def test_process_crash_at_publication_never_leaves_empty_attachment(data_dir, stage):
    import subprocess
    import sys

    note = notes.create_note(data_dir, "Crash publication", "unchanged")
    directory = note.ref.folder / "attachments"
    directory.mkdir()
    (directory / "keep.txt").write_bytes(b"keep")
    program = """
import os, sys
from pathlib import Path
from app import attachments, fsutil
data = fsutil.DataDir(Path(sys.argv[1]))
transfer = attachments.Upload(data, sys.argv[2])
transfer.write(b"complete upload")
publish = fsutil.rename_no_replace
def crash(source, target):
    if sys.argv[3] == "after":
        publish(source, target)
    os._exit(77)
fsutil.rename_no_replace = crash
transfer.finish(data, "upload.bin")
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(data_dir.root), note.ref.id, stage],
        cwd=Path(__file__).resolve().parents[1],
        timeout=20,
        check=False,
    )
    assert result.returncode == 77
    storage.prepare(data_dir.root.parent)  # a restart deletes nothing among the notes
    assert note.ref.path.read_bytes() == b"unchanged"
    assert (directory / "keep.txt").read_bytes() == b"keep"
    shown = [item.name for item in attachments.list_attachments(data_dir, note.ref.id)]
    hidden = [p for p in directory.iterdir() if p.name.startswith(fsutil.TEMP_PREFIX)]
    if stage == "before":
        assert shown == ["keep.txt"]
        assert [p.read_bytes() for p in hidden] == [b"complete upload"]  # kept, never shown
    else:
        assert shown == ["keep.txt", "upload.bin"]
        assert hidden == []
        assert (directory / "upload.bin").read_bytes() == b"complete upload"


@pytest.mark.parametrize("error", [errno.EIO, errno.ENOSPC])
def test_failed_file_sync_never_publishes_attachment(
    client, note_id, data_root, monkeypatch, error
):
    import stat

    real_sync = os.fsync

    def fail_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(error, "injected file sync failure")
        real_sync(fd)

    monkeypatch.setattr(os, "fsync", fail_file)
    response = upload(client, note_id, "failed.txt", b"complete body")
    assert response.status_code == (507 if error == errno.ENOSPC else 500)
    assert list(folder(data_root, note_id).iterdir()) == []


def test_note_deleted_while_an_upload_starts_is_not_found(data_dir, monkeypatch):
    note = notes.create_note(data_dir, "Deleted while uploading", "content")
    look_up = attachments._folder
    deleters = []

    def then_delete(ref, create=False):
        folder = look_up(ref, create)
        # Another request moves the note to the trash right after its folder was looked up.
        deleter = threading.Thread(target=notes.delete_note, args=(data_dir, ref.id))
        deleter.start()
        deleter.join(timeout=0.5)
        deleters.append(deleter)
        return folder

    monkeypatch.setattr(attachments, "_folder", then_delete)
    transfer = attachments.Upload(data_dir, note.ref.id)  # used to fail: FileNotFoundError
    deleters[0].join(timeout=10)
    assert not deleters[0].is_alive()
    transfer.write(b"late")
    with pytest.raises(notes.NotFound):
        transfer.finish(data_dir, "late.txt")
    transfer.discard()


def test_simultaneous_uploads_keep_both_contents(data_dir):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    note = notes.create_note(data_dir, "Concurrent uploads", "note")
    ready = Barrier(2)

    def send(body):
        transfer = attachments.Upload(data_dir, note.ref.id)
        transfer.write(body)
        ready.wait(timeout=10)
        return transfer.finish(data_dir, "same.txt")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send, b"first")
        second = executor.submit(send, b"second")
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert {item.name for item in results} == {"same.txt", "same (1).txt"}
    directory = note.ref.folder / "attachments"
    assert {path.read_bytes() for path in directory.iterdir()} == {b"first", b"second"}
