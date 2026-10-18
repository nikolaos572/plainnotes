"""Trash: list, restore (with conflicts), delete permanently and empty."""

import shutil
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient


def create(client: TestClient, title: str, content: str = "") -> dict:
    return client.post("/api/notes", json={"title": title, "content": content}).json()


def trash_ids(client: TestClient) -> list[str]:
    return [entry["trash_id"] for entry in client.get("/api/trash").json()]


def test_delete_and_list(client: TestClient) -> None:
    note = create(client, "Old note", "#tag")
    assert client.get("/api/trash").json() == []
    client.delete(f"/api/notes/{note['id']}")
    [entry] = client.get("/api/trash").json()
    assert entry["id"] == note["id"]
    assert entry["title"] == "Old note"
    assert entry["trash_id"].endswith("_" + note["id"])
    assert entry["deleted"].endswith("Z")
    assert set(entry) == {"trash_id", "id", "title", "deleted"}


def test_newest_first(client: TestClient, data_root: Path) -> None:
    (data_root / ".trash").mkdir()
    for trash_id in [
        "20260101-000000_20250101-000000-aaaa",
        "20260301-000000_20250101-000000-bbbb",
    ]:
        (data_root / ".trash" / trash_id).mkdir()
    assert trash_ids(client) == [
        "20260301-000000_20250101-000000-bbbb",
        "20260101-000000_20250101-000000-aaaa",
    ]


def test_restore(client: TestClient, data_root: Path) -> None:
    note = create(client, "Comeback", "#back")
    client.put(
        f"/api/notes/{note['id']}/attachments/a.txt",
        content=b"attached",
        headers={"Content-Type": "application/octet-stream"},
    )
    client.delete(f"/api/notes/{note['id']}")
    [trash_id] = trash_ids(client)
    response = client.post(f"/api/trash/{trash_id}/restore")
    assert response.status_code == 200
    assert response.json()["title"] == "Comeback"
    assert response.json()["tags"] == ["back"]
    assert set(response.json()) == {"id", "title", "created", "modified", "tags"}
    assert trash_ids(client) == []
    restored = client.get(f"/api/notes/{note['id']}").json()
    assert restored["attachments"][0]["name"] == "a.txt"
    assert (data_root / note["id"] / "attachments" / "a.txt").read_bytes() == b"attached"


def test_restore_conflicts(client: TestClient, data_root: Path) -> None:
    note = create(client, "Twin")
    client.delete(f"/api/notes/{note['id']}")
    [trash_id] = trash_ids(client)
    replacement = create(client, "TWIN")
    response = client.post(f"/api/trash/{trash_id}/restore")
    assert response.status_code == 409
    assert response.json()["code"] == "title_exists"
    assert "Rename" in response.json()["detail"]
    client.put(
        f"/api/notes/{replacement['id']}",
        json={"title": "Other", "content": "", "base_revision": replacement["revision"]},
    )
    shutil.copytree(data_root / ".trash" / trash_id, data_root / note["id"])
    (data_root / note["id"] / "Twin.md").rename(data_root / note["id"] / "Copy.md")
    response = client.post(f"/api/trash/{trash_id}/restore")
    assert response.status_code == 409
    assert response.json()["code"] == "id_exists"
    assert trash_ids(client) == [trash_id]


def test_restore_without_note_file(client: TestClient, data_root: Path) -> None:
    trash_id = "20260101-000000_20250101-000000-empt"
    (data_root / ".trash" / trash_id).mkdir(parents=True)
    [entry] = client.get("/api/trash").json()
    assert entry["title"] is None
    assert client.post(f"/api/trash/{trash_id}/restore").status_code == 404


def test_delete_permanently(client: TestClient, data_root: Path) -> None:
    note = create(client, "Bye")
    client.delete(f"/api/notes/{note['id']}")
    [trash_id] = trash_ids(client)
    assert client.delete(f"/api/trash/{trash_id}").status_code == 204
    assert not (data_root / ".trash" / trash_id).exists()
    assert client.delete(f"/api/trash/{trash_id}").status_code == 404
    assert client.post(f"/api/trash/{trash_id}/restore").status_code == 404


def test_empty_trash(client: TestClient, data_root: Path) -> None:
    for title in ["One", "Two", "Three"]:
        client.delete(f"/api/notes/{create(client, title)['id']}")
    (data_root / ".trash" / "keep-me.txt").write_text("not a trash entry")
    assert len(trash_ids(client)) == 3
    assert client.delete("/api/trash").status_code == 204
    assert trash_ids(client) == []
    assert sorted(p.name for p in (data_root / ".trash").iterdir()) == ["keep-me.txt"]
    assert client.delete("/api/trash").status_code == 204


def test_symlinks_in_trash_are_not_followed(
    client: TestClient, data_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "Precious.md").write_text("keep")
    trash = data_root / ".trash"
    trash.mkdir()
    link_id = "20260101-000000_20250101-000000-link"
    (trash / link_id).symlink_to(outside)
    assert trash_ids(client) == []
    assert client.delete(f"/api/trash/{link_id}").status_code == 404
    assert client.post(f"/api/trash/{link_id}/restore").status_code == 404
    assert client.delete("/api/trash").status_code == 204
    assert (outside / "Precious.md").read_text() == "keep"


def test_symlinked_trash_folder(
    client: TestClient, data_root: Path, tmp_path: Path, make_note: Callable
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    entry = outside / "20260101-000000_20250101-000000-abcd"
    entry.mkdir()
    (entry / "Note.md").write_text("x")
    (data_root / ".trash").symlink_to(outside)
    assert client.get("/api/trash").json() == []
    assert client.delete("/api/trash").status_code == 204
    assert (entry / "Note.md").exists()
    make_note("Kept", "x", note_id="20260101-000000-kept")
    assert client.delete("/api/notes/20260101-000000-kept").status_code == 500
    assert (data_root / "20260101-000000-kept" / "Kept.md").exists()
