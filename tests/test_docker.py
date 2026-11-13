"""Container smoke tests: build the image, run it, and use it over HTTP."""

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.conftest import CSRF, PASSWORD, SECRET_KEY, USERNAME

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parent.parent
HARDENED = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
APP_UID = 61001  # the user the image runs as; it matches no real account on a host
APP_USER = f"{APP_UID}:{APP_UID}"


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=600, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("the docker CLI is not installed")
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("the Docker daemon is not reachable")
    tag = f"plainnotes-test:{uuid.uuid4().hex[:12]}"
    docker("build", "-t", tag, str(ROOT))
    yield tag
    docker("rmi", "-f", tag, check=False)


@pytest.fixture
def data_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    folder.chmod(0o777)  # writable by the container's user, which the test's user is not
    return folder


@pytest.fixture
def owned_data_folder(image: str, tmp_path: Path) -> Iterator[Path]:
    """A data folder prepared as the README says: given to the container's user."""
    folder = tmp_path / "owned"
    folder.mkdir()
    as_root = ["run", "--rm", "--user", "0", "-v", f"{folder}:/data", image]
    docker(*as_root, "chown", APP_USER, "/data")  # sudo chown on the host, without sudo
    yield folder
    # Hand everything back to the test's user, so pytest can delete it.
    docker(*as_root, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/data", check=False)


def start(image: str, data: Path, *extra: str) -> str:
    return docker(
        "run",
        "-d",
        "-p",
        "127.0.0.1::8080",
        "-v",
        f"{data}:/data",
        "-e",
        f"PLAINNOTES_USERNAME={USERNAME}",
        "-e",
        f"PLAINNOTES_PASSWORD={PASSWORD}",
        "-e",
        f"PLAINNOTES_SECRET_KEY={SECRET_KEY}",
        "-e",
        "PLAINNOTES_COOKIE_SECURE=false",
        # Check health every second instead of every 30 s, so the test does not wait.
        "--health-interval",
        "1s",
        *extra,
        image,
    )


@pytest.fixture
def containers() -> Iterator[list[str]]:
    started: list[str] = []
    yield started
    for container in started:
        docker("rm", "-f", container, check=False)


def wait_healthy(container: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = json.loads(docker("inspect", "-f", "{{json .State}}", container))
        if not state["Running"]:
            raise AssertionError(f"container exited:\n{docker('logs', container, check=False)}")
        if state.get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(0.5)
    raise AssertionError(f"container not healthy:\n{docker('logs', container, check=False)}")


def base_url(container: str) -> str:
    address = docker("port", container, "8080/tcp").splitlines()[0]
    return f"http://{address}"


def create_note_over_http(container: str, title: str) -> dict:
    with httpx.Client(base_url=base_url(container), headers=CSRF, timeout=10) as client:
        assert client.get("/health").text == "ok"
        assert client.get("/api/notes").status_code == 401
        login = client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 204
        note = client.post("/api/notes", json={"title": title, "content": "from the test\n"})
        assert note.status_code == 201, note.text
        assert client.get("/").headers["content-security-policy"].startswith("default-src")
        return note.json()


def test_container(image: str, owned_data_folder: Path, containers: list[str]) -> None:
    container = start(image, owned_data_folder)
    containers.append(container)
    wait_healthy(container)
    assert docker("exec", container, "id", "-u") == str(APP_UID)
    assert docker("exec", container, "id", "-g") == str(APP_UID)
    note = create_note_over_http(container, "Made in Docker")
    path = owned_data_folder / "notes" / note["id"] / "Made in Docker.md"
    assert path.read_text() == "from the test\n"
    assert (path.stat().st_uid, path.stat().st_gid) == (APP_UID, APP_UID)
    testing = owned_data_folder / "testing"
    assert testing.stat().st_uid == APP_UID
    assert list(testing.iterdir()) == []  # the storage check cleaned up after itself


def test_container_hardened(image: str, data_folder: Path, containers: list[str]) -> None:
    container = start(image, data_folder, *HARDENED)
    containers.append(container)
    wait_healthy(container)
    note = create_note_over_http(container, "Read-only root")
    assert (data_folder / "notes" / note["id"] / "Read-only root.md").stat().st_uid == APP_UID


def test_image_is_small_and_unprivileged(image: str) -> None:
    config = json.loads(docker("image", "inspect", image))[0]
    assert config["Config"]["User"] == APP_USER
    assert config["Config"]["Healthcheck"]["Test"] == ["CMD", "python", "-m", "app.healthcheck"]
    assert "Volumes" not in config["Config"] or not config["Config"]["Volumes"]
    assert config["Size"] < 200 * 1024 * 1024


def test_unwritable_data_folder_exits_with_a_message(image: str, tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir(mode=0o755)  # owned by the test's user (or root), not by the container's
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{folder}:/data",
            "-e",
            f"PLAINNOTES_USERNAME={USERNAME}",
            "-e",
            f"PLAINNOTES_PASSWORD={PASSWORD}",
            "-e",
            f"PLAINNOTES_SECRET_KEY={SECRET_KEY}",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    assert (
        f"Data directory /data is not writable by uid {APP_UID}. "
        f"Fix it on the host with: sudo chown -R {APP_USER} <your data folder>"
    ) in result.stdout + result.stderr


def test_hardened_container_updates_and_reads_a_readonly_mount(image, data_folder, containers):
    for name in ["notes", "testing"]:
        (data_folder / name).mkdir()
        (data_folder / name).chmod(0o777)  # the CI runner's user writes into them below
    container = start(image, data_folder, *HARDENED)
    containers.append(container)
    wait_healthy(container)
    note = create_note_over_http(container, "Durable Docker note")
    with httpx.Client(base_url=base_url(container), headers=CSRF, timeout=10) as client:
        client.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
        saved = client.put(
            f"/api/notes/{note['id']}",
            json={
                "title": note["title"],
                "content": "updated under hardening",
                "base_revision": note["revision"],
            },
        )
        assert saved.status_code == 200, saved.text
        attachment_url = f"/api/notes/{note['id']}/attachments/same.txt"
        first = client.put(attachment_url, content=b"first complete upload")
        second = client.put(attachment_url, content=b"second complete upload")
        assert first.status_code == second.status_code == 201
        assert first.json()["name"] == "same.txt"
        assert second.json()["name"] == "same (1).txt"
        assert client.get(attachment_url).content == b"first complete upload"
        notes = data_folder / "notes"
        assert (notes / note["id"] / "attachments" / "same (1).txt").read_bytes() == (
            b"second complete upload"
        )
    # History is private to the container's user, so read it from inside (CI runs as another uid).
    history = docker(
        "exec",
        container,
        "python",
        "-c",
        "import pathlib, sys; "
        "print([p.read_text() for p in pathlib.Path(sys.argv[1]).rglob('*.md')])",
        f"/data/notes/{note['id']}/.history",
    )
    assert history == repr(["from the test\n"])
    docker("stop", container)
    loose = data_folder / "notes" / "Do not import.md"
    loose.write_text("untouched")
    staged = data_folder / "notes" / ".plainnotes-tmp-do-not-clean"
    staged.write_text("untouched")
    check_leftover = data_folder / "testing" / "left by a killed check"
    check_leftover.write_text("untouched")
    reader = docker(
        "run",
        "-d",
        "-p",
        "127.0.0.1::8080",
        "-v",
        f"{data_folder}:/data:ro",
        "-e",
        f"PLAINNOTES_USERNAME={USERNAME}",
        "-e",
        f"PLAINNOTES_PASSWORD={PASSWORD}",
        "-e",
        f"PLAINNOTES_SECRET_KEY={SECRET_KEY}",
        "-e",
        "PLAINNOTES_COOKIE_SECURE=false",
        "-e",
        "PLAINNOTES_READ_ONLY=true",
        "--health-interval",
        "1s",
        *HARDENED,
        image,
    )
    containers.append(reader)
    wait_healthy(reader)
    with httpx.Client(base_url=base_url(reader), headers=CSRF, timeout=10) as client:
        assert (
            client.post(
                "/api/login",
                json={"username": USERNAME, "password": PASSWORD},
            ).status_code
            == 204
        )
        for route in ["/api/notes", "/api/search", "/api/tags"]:
            assert client.get(route).status_code == 200
        assert client.get(f"/api/notes/{note['id']}").json()["content"] == "updated under hardening"
        assert client.delete(f"/api/notes/{note['id']}").status_code == 403
    assert loose.read_text() == staged.read_text() == check_leftover.read_text() == "untouched"
