"""Security audit tests: path traversal corpus, symlinks, headers per response type."""

import http.client
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import CSRF, PASSWORD, USERNAME, login
from tests.test_web import APP_CSP, assert_security_headers

NOTE = "20260101-000000-note"
# Sent through httpx, which removes literal "." and ".." segments; those are tested raw below.
TRAVERSAL = [
    "...",
    "%2e%2e",
    "%2E%2E",
    "%2e%2e%2f%2e%2e%2fsecret.txt",
    "..%2fsecret.txt",
    "%2f",
    "%2fetc%2fpasswd",
    "%252e%252e%252fsecret.txt",
    "..%5c..%5csecret.txt",
    "%5c",
    "%5csecret.txt",
    "C:%5cWindows%5cwin.ini",
    "%00",
    "a%00b",
    f"{NOTE}%00",
    "x" * 300,
    "é" * 200,
    "....%2f%2f",
    "%c0%ae%c0%ae",
    "~",
    "%20..%20",
    "secret.txt%2f..",
    "20260101-000000-note%2f..%2f..",
    "20260101-000000_20260101-000000-note%2f..",
]


@pytest.fixture
def setup(client: TestClient, data_root: Path, tmp_path: Path, make_note: Callable) -> Path:
    """A note with an attachment and a trash entry, plus secret files outside the data folder."""
    make_note("Note", "content", note_id=NOTE)
    client.put(
        f"/api/notes/{NOTE}/attachments/real.txt",
        content=b"attached",
        headers={"Content-Type": "application/octet-stream"},
    )
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "secret.md").write_text("secret note")
    trashed = data_root / ".trash" / "20260101-000000_20260101-000000-gone"
    trashed.mkdir(parents=True)
    (trashed / "Gone.md").write_text("gone")
    return tmp_path


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


@pytest.mark.parametrize("value", TRAVERSAL)
def test_traversal_corpus(client: TestClient, setup: Path, value: str) -> None:
    before = snapshot(setup)
    body = {"title": "t", "content": "c", "base_revision": "r"}
    requests = [
        ("GET", f"/api/notes/{value}", None),
        ("PUT", f"/api/notes/{value}", body),
        ("DELETE", f"/api/notes/{value}", None),
        ("GET", f"/api/notes/{value}/attachments", None),
        ("GET", f"/api/notes/{value}/attachments/real.txt", None),
        ("PUT", f"/api/notes/{value}/attachments/x.txt", None),
        ("GET", f"/api/notes/{NOTE}/attachments/{value}", None),
        ("DELETE", f"/api/notes/{NOTE}/attachments/{value}", None),
        ("POST", f"/api/trash/{value}/restore", None),
        ("DELETE", f"/api/trash/{value}", None),
    ]
    for method, path, payload in requests:
        response = client.request(method, path, json=payload)
        assert response.status_code in {404, 405}, (method, path, response.status_code)
        assert "secret" not in response.text
    assert snapshot(setup) == before


@pytest.mark.parametrize("value", TRAVERSAL)
def test_upload_names_stay_inside_the_attachments_folder(
    client: TestClient, setup: Path, data_root: Path, value: str
) -> None:
    outside = {p for p in setup.rglob("*") if data_root not in p.parents and p != data_root}
    response = client.put(
        f"/api/notes/{NOTE}/attachments/{value}",
        content=b"payload",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code in {201, 404, 405}
    if response.status_code == 201:
        name = response.json()["name"]
        assert "/" not in name and "\\" not in name and "\x00" not in name
        assert not name.startswith(".")
        assert (data_root / NOTE / "attachments" / name).read_bytes() == b"payload"
    assert {p for p in setup.rglob("*") if data_root not in p.parents and p != data_root} == outside


RAW_REQUESTS = [
    ("GET", "/api/notes/.."),
    ("GET", "/api/notes/."),
    ("GET", "/api/notes/../../secret.txt"),
    ("PUT", "/api/notes/.."),
    ("DELETE", "/api/notes/.."),
    ("GET", f"/api/notes/{NOTE}/attachments/.."),
    ("GET", f"/api/notes/{NOTE}/attachments/../../../secret.txt"),
    ("DELETE", f"/api/notes/{NOTE}/attachments/.."),
    ("PUT", f"/api/notes/{NOTE}/attachments/.."),
    ("PUT", f"/api/notes/{NOTE}/attachments/../../x.txt"),
    ("POST", "/api/trash/../restore"),
    ("DELETE", "/api/trash/.."),
    ("GET", "/static/../main.py"),
    ("GET", "/static/../../secret.txt"),
    ("GET", "/static/js/../../../../secret.txt"),
]


def test_literal_dot_segments(live_server: Callable, data_root: Path, tmp_path: Path) -> None:
    """Send request lines exactly as written, without any client-side normalization."""
    url = live_server()
    host, port = url.removeprefix("http://").split(":")
    folder = data_root / NOTE
    folder.mkdir()
    (folder / "Note.md").write_text("content")
    (folder / "attachments").mkdir()
    (folder / "attachments" / "real.txt").write_text("attached")
    (tmp_path / "secret.txt").write_text("secret")
    before = snapshot(tmp_path)
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    connection.request(
        "POST",
        "/api/login",
        body=json.dumps({"username": USERNAME, "password": PASSWORD}),
        headers={**CSRF, "Content-Type": "application/json"},
    )
    login_response = connection.getresponse()
    login_response.read()
    cookie = login_response.getheader("Set-Cookie").split(";")[0]
    body = json.dumps({"title": "t", "content": "c", "base_revision": "r"})
    headers = {**CSRF, "Cookie": cookie, "Content-Type": "application/json"}
    for method, path in RAW_REQUESTS:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        text = response.read()
        # An upload named ".." is not refused but sanitized to "file", inside the folder.
        allowed = (
            {201, 404, 405} if path.endswith("/attachments/..") and method == "PUT" else {404, 405}
        )
        assert response.status in allowed, (method, path, response.status)
        assert b"secret" not in text and b"create_app" not in text
    connection.close()
    assert sorted(os.listdir(folder / "attachments")) == ["file", "real.txt"]
    before["data/" + NOTE + "/attachments/file"] = body.encode()
    assert snapshot(tmp_path) == before


def test_symlinks_pointing_outside(client: TestClient, setup: Path, data_root: Path) -> None:
    outside_folder = setup / "outside-note"
    outside_folder.mkdir()
    (outside_folder / "Outside.md").write_text("outside")
    (data_root / "20260101-000000-lnkd").symlink_to(outside_folder)
    (data_root / "20260101-000000-lnkf").mkdir()
    (data_root / "20260101-000000-lnkf" / "Linked.md").symlink_to(setup / "secret.md")
    (data_root / "Loose link.md").symlink_to(setup / "secret.md")
    before = snapshot(setup)
    titles = [note["title"] for note in client.get("/api/notes").json()]
    assert titles == ["Note"]
    for note_id in ["20260101-000000-lnkd", "20260101-000000-lnkf"]:
        assert client.get(f"/api/notes/{note_id}").status_code == 404
        body = {"title": "x", "content": "overwritten", "base_revision": "r"}
        assert client.put(f"/api/notes/{note_id}", json=body).status_code == 404
        assert client.delete(f"/api/notes/{note_id}").status_code == 404
        upload = client.put(f"/api/notes/{note_id}/attachments/x.txt", content=b"x")
        assert upload.status_code == 404
    assert client.get("/api/search", params={"q": "secret"}).json() == []
    assert client.get("/api/tags").json() == []
    assert snapshot(setup) == before
    assert (data_root / "Loose link.md").is_symlink()


def test_invalid_ids_are_404_without_details(client: TestClient, setup: Path) -> None:
    for path in ["/api/notes/..%2f..%2fetc", "/api/notes/%00", "/api/trash/%2e%2e/restore"]:
        response = client.request("POST" if "restore" in path else "GET", path)
        assert response.status_code == 404
        assert response.json() == {"code": "not_found", "detail": "Not found."}


# --- headers on every response type ---------------------------------------------------

SANDBOX_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def test_headers_on_every_response_type(
    app_factory: Callable[..., FastAPI], data_root: Path
) -> None:
    with TestClient(app_factory(base_path="/n"), headers=CSRF, follow_redirects=False) as client:
        login(client, "/n")
        note = client.post("/n/api/notes", json={"title": "H", "content": ""}).json()["id"]
        for name in ["inline.png", "download.html"]:
            client.put(f"/n/api/notes/{note}/attachments/{name}", content=b"x")
        index = client.get("/n/")
        responses = {
            "html": (index, APP_CSP, "no-cache"),
            "static": (client.get("/n/static/js/main.js"), APP_CSP, "no-cache"),
            "not modified": (
                client.get("/n/", headers={"If-None-Match": index.headers["etag"]}),
                APP_CSP,
                "no-cache",
            ),
            "api json": (client.get("/n/api/notes"), APP_CSP, "no-store"),
            "api error": (client.get("/n/api/notes/20260101-000000-none"), APP_CSP, "no-store"),
            "inline attachment": (
                client.get(f"/n/api/notes/{note}/attachments/inline.png"),
                SANDBOX_CSP,
                "private, no-cache",
            ),
            "downloaded attachment": (
                client.get(f"/n/api/notes/{note}/attachments/download.html"),
                SANDBOX_CSP,
                "private, no-cache",
            ),
            "health": (client.get("/n/health"), APP_CSP, None),
            "404": (client.get("/n/nothing-here"), APP_CSP, None),
            "redirect": (client.get("/n"), APP_CSP, None),
        }
    statuses = {kind: response.status_code for kind, (response, _, _) in responses.items()}
    assert statuses == {
        "html": 200,
        "static": 200,
        "not modified": 304,
        "api json": 200,
        "api error": 404,
        "inline attachment": 200,
        "downloaded attachment": 200,
        "health": 200,
        "404": 404,
        "redirect": 308,
    }
    for kind, (response, csp, cache_control) in responses.items():
        assert_security_headers(response, csp)
        assert response.headers.get("cache-control") == cache_control, kind
    assert responses["inline attachment"][0].headers["content-disposition"].startswith("inline")
    downloaded = responses["downloaded attachment"][0]
    assert downloaded.headers["content-disposition"].startswith("attachment")
    assert downloaded.headers["content-type"] == "application/octet-stream"


def test_no_secrets_in_logs(
    app_factory: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
) -> None:
    from tests.conftest import SECRET_KEY

    with TestClient(app_factory(), headers=CSRF) as client:
        client.post("/api/login", json={"username": "wrong", "password": PASSWORD + "x"})
        login(client)
        client.post("/api/notes", json={"title": "Logged?", "content": "private content"})
        client.get("/api/search", params={"q": "private search"})
    text = caplog.text
    for secret in [PASSWORD, SECRET_KEY, "private content", "private search", "plainnotes_session"]:
        assert secret not in text
    assert os.environ.get("PLAINNOTES_PASSWORD") is None
