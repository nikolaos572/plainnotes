"""Shared fixtures: isolated data folders, apps and clients."""

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.config import Settings
from app.fsutil import DataDir
from app.main import create_app

USERNAME = "admin"
PASSWORD = "correct horse battery staple"
SECRET_KEY = "test-secret-key-0123456789abcdef-0123456789"
CSRF = {"X-Requested-With": "plainnotes"}


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def data_dir(data_root: Path) -> DataDir:
    return DataDir(data_root)


@pytest.fixture
def make_note(data_root: Path) -> Callable[..., Path]:
    """Write a note folder directly on disk, as an external tool would; return the .md path."""
    counter = iter(range(10_000))

    def make(
        title: str,
        content: str | bytes = "",
        note_id: str | None = None,
        mtime: float | None = None,
    ) -> Path:
        note_id = note_id or f"20260101-{next(counter):06d}-test"
        folder = data_root / note_id
        folder.mkdir()
        path = folder / f"{title}.md"
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    return make


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed logins normally wait one second; tests do not."""
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY", 0)


def make_settings(data_root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "username": USERNAME,
        "password": PASSWORD,
        "secret_key": SECRET_KEY,
        "data_dir": data_root,
        "cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def app_factory(data_root: Path) -> Callable[..., FastAPI]:
    def factory(**overrides: object) -> FastAPI:
        return create_app(make_settings(data_root, **overrides))

    return factory


def login(client: TestClient, base: str = "") -> None:
    response = client.post(
        f"{base}/api/login", json={"username": USERNAME, "password": PASSWORD}, headers=CSRF
    )
    assert response.status_code == 204, response.text


@pytest.fixture
def anon(app_factory: Callable[..., FastAPI]) -> Iterator[TestClient]:
    """A client without a session or default headers."""
    with TestClient(app_factory()) as client:
        yield client


@pytest.fixture
def client(app_factory: Callable[..., FastAPI]) -> Iterator[TestClient]:
    """A logged-in client that sends the CSRF header on every request."""
    with TestClient(app_factory(), headers=CSRF) as client:
        login(client)
        yield client


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(app_factory: Callable[..., FastAPI]) -> Iterator[Callable[..., str]]:
    """Start real uvicorn servers in threads (for true streaming); yields a factory → base URL."""
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start(**overrides: object) -> str:
        port = free_port()
        config = uvicorn.Config(
            app_factory(**overrides), host="127.0.0.1", port=port, log_level="warning"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        servers.append((server, thread))
        return f"http://127.0.0.1:{port}"

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)
