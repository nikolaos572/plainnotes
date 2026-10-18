"""Container smoke tests: build the image, run it, and use it over HTTP."""

import json
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
    folder.chmod(0o777)  # the CI runner's user is not UID 1000
    return folder


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


def test_container(image: str, data_folder: Path, containers: list[str]) -> None:
    container = start(image, data_folder)
    containers.append(container)
    wait_healthy(container)
    assert docker("exec", container, "id", "-u") == "1000"
    assert docker("exec", container, "id", "-g") == "1000"
    note = create_note_over_http(container, "Made in Docker")
    path = data_folder / note["id"] / "Made in Docker.md"
    assert path.read_text() == "from the test\n"
    assert path.stat().st_uid == 1000


def test_container_hardened(image: str, data_folder: Path, containers: list[str]) -> None:
    container = start(image, data_folder, *HARDENED)
    containers.append(container)
    wait_healthy(container)
    note = create_note_over_http(container, "Read-only root")
    assert (data_folder / note["id"] / "Read-only root.md").stat().st_uid == 1000


def test_image_is_small_and_unprivileged(image: str) -> None:
    config = json.loads(docker("image", "inspect", image))[0]
    assert config["Config"]["User"] == "1000:1000"
    assert config["Config"]["Healthcheck"]["Test"] == ["CMD", "python", "-m", "app.healthcheck"]
    assert "Volumes" not in config["Config"] or not config["Config"]["Volumes"]
    assert config["Size"] < 200 * 1024 * 1024


def test_unwritable_data_folder_exits_with_a_message(image: str, tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir(mode=0o755)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "4321:4321",
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
        "Data directory /data is not writable by uid 4321. "
        "Fix it on the host with: sudo chown -R 4321:4321 <your data folder>"
    ) in result.stdout + result.stderr
