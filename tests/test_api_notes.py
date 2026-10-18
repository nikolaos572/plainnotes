"""Notes API: byte-exact storage, CRUD, conflicts, limits and external changes."""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import notes

ROUND_TRIP = (
    "﻿BOM first\n"
    "\ttabs\tinside\n"
    "trailing spaces   \n"
    "windows\r\nline\r\n"
    "backslash \\ dollar $HOME `backticks` ``double``\n"
    "<script>alert(1)</script> *stars* _under_ **bold**\n"
    + "x"
    * 100_000
    + "\nemoji 😀👍🏽 and Greek: Καλημέρα κόσμε\n"
    "no final newline"
)


def create(client: TestClient, title: str = "Note", content: str = "") -> dict:
    response = client.post("/api/notes", json={"title": title, "content": content})
    assert response.status_code == 201, response.text
    return response.json()


def note_file(data_root: Path, note: dict) -> Path:
    return data_root / note["id"] / f"{note['title']}.md"


def test_byte_exact_round_trip(client: TestClient, data_root: Path) -> None:
    note = create(client, "Round trip", ROUND_TRIP)
    assert note_file(data_root, note).read_bytes() == ROUND_TRIP.encode("utf-8")
    assert client.get(f"/api/notes/{note['id']}").json()["content"] == ROUND_TRIP
    updated = client.put(
        f"/api/notes/{note['id']}",
        json={
            "title": "Round trip",
            "content": ROUND_TRIP + "\r\n",
            "base_revision": note["revision"],
        },
    ).json()
    assert note_file(data_root, updated).read_bytes() == (ROUND_TRIP + "\r\n").encode("utf-8")
    assert client.get(f"/api/notes/{note['id']}").json()["content"] == ROUND_TRIP + "\r\n"


def test_create_response(client: TestClient) -> None:
    note = create(client, "  Docker commands  ", "docker ps #docker\n")
    assert note["title"] == "Docker commands"
    assert notes.NOTE_ID_RE.fullmatch(note["id"])
    assert note["tags"] == ["docker"]
    assert note["attachments"] == []
    assert note["created"].endswith("Z") and len(note["created"]) == 20
    assert note["modified"].endswith("Z")
    assert set(note) == {
        "id",
        "title",
        "created",
        "modified",
        "tags",
        "content",
        "revision",
        "attachments",
    }


def test_update_rename_and_case_only_rename(client: TestClient, data_root: Path) -> None:
    note = create(client, "docker", "one")
    renamed = client.put(
        f"/api/notes/{note['id']}",
        json={"title": "Docker", "content": "one", "base_revision": note["revision"]},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Docker"
    assert [p.name for p in (data_root / note["id"]).iterdir()] == ["Docker.md"]
    moved = client.put(
        f"/api/notes/{note['id']}",
        json={"title": "Containers", "content": "two", "base_revision": renamed.json()["revision"]},
    ).json()
    assert moved["title"] == "Containers"
    assert moved["content"] == "two"
    assert [p.name for p in (data_root / note["id"]).iterdir()] == ["Containers.md"]
    assert client.get(f"/api/notes/{note['id']}").json()["title"] == "Containers"


def test_title_errors(client: TestClient) -> None:
    create(client, "Taken")
    for title, code, status in [
        ("taken", "title_exists", 409),
        ("TAKEN ", "title_exists", 409),
        ("", "invalid_title", 400),
        ("a/b", "invalid_title", 400),
        (".dot", "invalid_title", 400),
        ("x" * 253, "invalid_title", 400),
    ]:
        response = client.post("/api/notes", json={"title": title, "content": ""})
        assert response.status_code == status, title
        assert response.json()["code"] == code
    other = create(client, "Other")
    response = client.put(
        f"/api/notes/{other['id']}",
        json={"title": "TAKEN", "content": "", "base_revision": other["revision"]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "title_exists"


def test_revision_conflict(client: TestClient, data_root: Path) -> None:
    note = create(client, "Shared", "original")
    path = note_file(data_root, note)
    path.write_text("changed elsewhere")
    response = client.put(
        f"/api/notes/{note['id']}",
        json={"title": "Shared", "content": "mine", "base_revision": note["revision"]},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "revision_conflict"
    assert body["current"]["content"] == "changed elsewhere"
    assert body["current"]["title"] == "Shared"
    assert body["current"]["revision"] == notes.revision_of(b"changed elsewhere")
    assert body["current"]["modified"].endswith("Z")
    assert path.read_text() == "changed elsewhere"
    overwrite = client.put(
        f"/api/notes/{note['id']}",
        json={"title": "Shared", "content": "mine", "base_revision": body["current"]["revision"]},
    )
    assert overwrite.status_code == 200
    assert path.read_text() == "mine"


def test_unchanged_put_writes_nothing(client: TestClient, data_root: Path) -> None:
    note = create(client, "Same", "content")
    path = note_file(data_root, note)
    os.utime(path, (1_000_000_000, 1_000_000_000))
    response = client.put(
        f"/api/notes/{note['id']}",
        json={"title": "Same", "content": "content", "base_revision": note["revision"]},
    )
    assert response.status_code == 200
    assert response.json()["modified"] == "2001-09-09T01:46:40Z"
    assert path.stat().st_mtime == 1_000_000_000


def test_not_found(client: TestClient) -> None:
    missing = "20260101-000000-abcd"
    assert client.get(f"/api/notes/{missing}").json()["code"] == "not_found"
    assert client.get(f"/api/notes/{missing}").status_code == 404
    response = client.put(
        f"/api/notes/{missing}", json={"title": "t", "content": "c", "base_revision": "r"}
    )
    assert response.status_code == 404
    assert client.delete(f"/api/notes/{missing}").status_code == 404


def test_folder_deleted_while_editing(client: TestClient, data_root: Path) -> None:
    note = create(client, "Vanishing")
    note_file(data_root, note).unlink()
    (data_root / note["id"]).rmdir()
    response = client.put(
        f"/api/notes/{note['id']}",
        json={"title": "Vanishing", "content": "x", "base_revision": note["revision"]},
    )
    assert response.status_code == 404


def test_content_too_large(client: TestClient) -> None:
    response = client.post(
        "/api/notes", json={"title": "Big", "content": "x" * (10 * 1024 * 1024 + 1)}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"
    assert client.get("/api/notes").json() == []


def test_lone_surrogate_content(client: TestClient) -> None:
    response = client.post(
        "/api/notes",
        content=b'{"title": "s", "content": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_body_limit(client: TestClient) -> None:
    big = b'{"title": "x", "content": "' + b"y" * (32 * 1024 * 1024) + b'"}'
    response = client.post("/api/notes", content=big, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"

    def chunks():
        yield b'{"title": "x", "content": "'
        for _ in range(33):
            yield b"y" * (1024 * 1024)
        yield b'"}'

    response = client.post(
        "/api/notes", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"


def test_not_utf8(client: TestClient, make_note: Callable) -> None:
    make_note("Latin-1", "café".encode("latin-1"), note_id="20260101-000000-lat1")
    listed = client.get("/api/notes").json()
    assert [n["title"] for n in listed] == ["Latin-1"]
    response = client.get("/api/notes/20260101-000000-lat1")
    assert response.status_code == 422
    assert response.json() == {
        "code": "not_utf8",
        "detail": "This file is not valid UTF-8 and cannot be opened here.",
    }


def test_delete_moves_to_trash(client: TestClient, data_root: Path) -> None:
    note = create(client, "Gone", "bye")
    assert client.delete(f"/api/notes/{note['id']}").status_code == 204
    assert client.get(f"/api/notes/{note['id']}").status_code == 404
    [entry] = list((data_root / ".trash").iterdir())
    assert entry.name.endswith("_" + note["id"])
    assert (entry / "Gone.md").read_text() == "bye"
    assert client.get("/api/notes").json() == []


def test_list_newest_first_with_tags(client: TestClient, data_root: Path) -> None:
    first = create(client, "First", "#one")
    second = create(client, "Second", "#two #Two")
    os.utime(note_file(data_root, first), (2_000_000_000, 2_000_000_000))
    os.utime(note_file(data_root, second), (1_000_000_000, 1_000_000_000))
    listed = client.get("/api/notes").json()
    assert [n["title"] for n in listed] == ["First", "Second"]
    assert listed[1]["tags"] == ["two"]
    assert set(listed[0]) == {"id", "title", "created", "modified", "tags"}
    search = client.get("/api/search", params={"q": "", "sort": "modified"}).json()
    assert [n["id"] for n in search] == [n["id"] for n in listed]


def test_external_changes(client: TestClient, make_note: Callable) -> None:
    path = make_note("Outside", "v1", note_id="20260101-000000-ext1")
    assert client.get("/api/notes/20260101-000000-ext1").json()["content"] == "v1"
    path.write_text("v2 #new")
    note = client.get("/api/notes/20260101-000000-ext1").json()
    assert (note["content"], note["tags"]) == ("v2 #new", ["new"])
    path.rename(path.with_name("Renamed outside.md"))
    assert client.get("/api/notes/20260101-000000-ext1").json()["title"] == "Renamed outside"
    assert [n["title"] for n in client.get("/api/notes").json()] == ["Renamed outside"]


def test_skipped_and_ignored_folders(
    client: TestClient, data_root: Path, make_note: Callable
) -> None:
    make_note("Real", "x", note_id="20260101-000000-real")
    (data_root / "20260101-000000-none").mkdir()
    two = data_root / "20260101-000000-twoo"
    two.mkdir()
    (two / "a.md").write_text("a")
    (two / "b.md").write_text("b")
    (data_root / "random-folder").mkdir()
    (data_root / "notes.txt").write_text("not markdown")
    assert [n["title"] for n in client.get("/api/notes").json()] == ["Real"]
    assert client.get("/api/notes/20260101-000000-twoo").status_code == 404


@pytest.mark.parametrize("endpoint", ["/api/notes", "/api/search", "/api/tags"])
def test_drop_in_import(client: TestClient, data_root: Path, endpoint: str) -> None:
    loose = data_root / "Dropped in.md"
    loose.write_bytes(b"imported #tag\r\nbytes stay")
    mtime = datetime(2019, 3, 4, 5, 6, 7, tzinfo=UTC).timestamp()
    os.utime(loose, (mtime, mtime))
    (data_root / "bad:name.md").write_text("stays")
    create(client, "Existing")
    (data_root / "existing.md").write_text("duplicate stays")
    assert client.get(endpoint).status_code == 200
    assert not loose.exists()
    [note] = [n for n in client.get("/api/notes").json() if n["title"] == "Dropped in"]
    assert note["id"].startswith("20190304-050607-")
    assert note["created"] == "2019-03-04T05:06:07Z"
    imported = data_root / note["id"] / "Dropped in.md"
    assert imported.read_bytes() == b"imported #tag\r\nbytes stay"
    assert imported.stat().st_mtime == mtime
    assert (data_root / "bad:name.md").read_text() == "stays"
    assert (data_root / "existing.md").read_text() == "duplicate stays"


def test_import_at_startup(app_factory: Callable, data_root: Path) -> None:
    (data_root / "At startup.md").write_text("hello")
    (data_root / ".plainnotes-tmp-leftover").mkdir()
    with TestClient(app_factory()):
        pass
    names = sorted(p.name for p in data_root.iterdir())
    assert len(names) == 1 and notes.is_note_id(names[0])
