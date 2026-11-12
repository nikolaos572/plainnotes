"""Read-only mode and sub-path hosting on the server."""

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import CSRF, login
from tests.test_auth import PUBLIC, api_routes
from tests.test_web import assert_security_headers

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture
def read_only(app_factory: Callable[..., FastAPI], make_note: Callable) -> TestClient:
    make_note("Existing", "#tag", note_id="20260101-000000-read")
    with TestClient(app_factory(read_only=True), headers=CSRF) as client:
        login(client)
        yield client


def test_read_only_rejects_every_mutation(read_only: TestClient) -> None:
    mutations = [
        (method, path)
        for method, path in api_routes(read_only.app)
        if method in MUTATING and path not in {"/api/login", "/api/logout"}
    ]
    assert len(mutations) == 8
    for method, path in mutations:
        response = read_only.request(method, path, json={"title": "x", "content": "y"})
        assert response.status_code == 403, (method, path)
        assert response.json()["code"] == "read_only"


def test_read_only_still_reads_and_logs_in(read_only: TestClient, data_root: Path) -> None:
    assert read_only.get("/api/session").json()["read_only"] is True
    assert [n["title"] for n in read_only.get("/api/notes").json()] == ["Existing"]
    assert read_only.get("/api/notes/20260101-000000-read").status_code == 200
    assert read_only.get("/api/search", params={"q": "#tag"}).status_code == 200
    assert read_only.get("/api/tags").status_code == 200
    assert read_only.get("/api/trash").status_code == 200
    assert read_only.post("/api/logout").status_code == 204
    login(read_only)
    assert sorted(p.name for p in data_root.iterdir()) == ["20260101-000000-read"]


def test_read_only_upload_is_rejected_before_reading_the_body(read_only: TestClient) -> None:
    response = read_only.put(
        "/api/notes/20260101-000000-read/attachments/x.bin",
        content=b"x" * 1024,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "read_only"


@pytest.fixture
def prefixed(app_factory: Callable[..., FastAPI]) -> TestClient:
    with TestClient(app_factory(base_path="/notes"), follow_redirects=False) as client:
        yield client


def test_base_path_serves_everything_under_the_prefix(prefixed: TestClient) -> None:
    redirect = prefixed.get("/notes")
    assert redirect.status_code == 308
    assert redirect.headers["location"] == "/notes/"
    assert_security_headers(redirect)
    index = prefixed.get("/notes/")
    assert index.status_code == 200
    assert 'src="static/js/main.js"' in index.text
    assert prefixed.get("/notes/static/js/main.js").status_code == 200
    assert prefixed.get("/notes/static/favicon.svg").status_code == 200
    assert prefixed.get("/notes/health").text == "ok"
    assert prefixed.get("/notes/api/notes").status_code == 401
    for outside in ["/", "/health", "/api/notes", "/static/js/main.js", "/notesx/"]:
        assert prefixed.get(outside).status_code == 404, outside


def test_base_path_login_cookie_and_api(prefixed: TestClient) -> None:
    prefixed.headers.update(CSRF)
    response = prefixed.post(
        "/notes/api/login", json={"username": "admin", "password": "correct horse battery staple"}
    )
    assert response.status_code == 204
    assert "Path=/notes/;" in response.headers["set-cookie"]
    created = prefixed.post("/notes/api/notes", json={"title": "Under prefix", "content": ""})
    assert created.status_code == 201
    note_id = created.json()["id"]
    upload = prefixed.put(
        f"/notes/api/notes/{note_id}/attachments/a.txt",
        content=b"hello",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert upload.status_code == 201
    attachment = prefixed.get(f"/notes/api/notes/{note_id}/attachments/a.txt")
    assert attachment.headers["cache-control"] == "private, no-cache"
    assert prefixed.get("/notes/api/notes").headers["cache-control"] == "no-store"


def test_base_path_routes_all_require_a_session(prefixed: TestClient) -> None:
    for method, path in api_routes(prefixed.app):
        bare = path.removeprefix("/notes")
        if (method, bare or "/") in PUBLIC or bare in {"", "/"}:
            continue
        response = prefixed.request(method, path, headers=CSRF, json={})
        assert response.status_code == 401, (method, path)


def test_read_only_startup_and_gets_never_mutate_files(app_factory, data_root):
    loose = data_root / "Loose.md"
    loose.write_bytes(b"original")
    staging = data_root / ".plainnotes-tmp-original"
    staging.write_bytes(b"keep staging")
    with TestClient(app_factory(read_only=True), headers=CSRF) as client:
        login(client)
        for path in ["/api/notes", "/api/search", "/api/tags"]:
            assert client.get(path).status_code == 200
            assert loose.read_bytes() == b"original"
            assert staging.read_bytes() == b"keep staging"
    assert sorted(p.name for p in data_root.iterdir()) == [staging.name, loose.name]


def test_configured_symlink_root_supports_import_and_mutations(app_factory, tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "Import.md").write_bytes(b"imported")
    link = tmp_path / "configured"
    link.symlink_to(actual, target_is_directory=True)
    app = app_factory(data_dir=link)
    assert app.state.data_dir.root == actual.resolve()
    with TestClient(app, headers=CSRF) as client:
        login(client)
        assert [n["title"] for n in client.get("/api/notes").json()] == ["Import"]
        response = client.post("/api/notes", json={"title": "Created", "content": "one"})
        assert response.status_code == 201
        note = response.json()
        path = "/api/notes/" + note["id"]
        saved = client.put(
            path,
            json={
                "title": "Renamed",
                "content": "two",
                "base_revision": note["revision"],
            },
        )
        assert saved.status_code == 200
        assert client.get(path).json()["content"] == "two"
        assert client.delete(path).status_code == 204
        entry = client.get("/api/trash").json()[0]["trash_id"]
        assert client.post("/api/trash/" + entry + "/restore").status_code == 200
        # Configuration aliases are resolved once, not followed on every request.
        other = tmp_path / "other"
        other.mkdir()
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        assert client.get(path).json()["content"] == "two"
        assert list(other.iterdir()) == []
