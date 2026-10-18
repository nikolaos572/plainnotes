"""Configuration parsing and validation."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, check_data_dir, config_warnings, load_settings

BASE = {
    "PLAINNOTES_USERNAME": "admin",
    "PLAINNOTES_PASSWORD": "a-long-enough-password",
    "PLAINNOTES_SECRET_KEY": "s" * 32,
}


def load(**overrides: str) -> Settings:
    return load_settings({**BASE, **{f"PLAINNOTES_{k}": v for k, v in overrides.items()}})


def test_defaults() -> None:
    settings = load()
    assert settings.username == "admin"
    assert settings.data_dir == Path("/data")
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.session_days == 30
    assert settings.cookie_secure is True
    assert settings.forwarded_allow_ips == "127.0.0.1"
    assert settings.max_upload_mb == 0
    assert settings.log_level == "INFO"
    assert settings.read_only is False
    assert settings.base_path == ""


def test_all_values_parsed() -> None:
    settings = load(
        DATA_DIR="/srv/notes",
        HOST="127.0.0.1",
        PORT="9000",
        SESSION_DAYS="365",
        COOKIE_SECURE="no",
        FORWARDED_ALLOW_IPS="*",
        MAX_UPLOAD_MB="100",
        LOG_LEVEL="debug",
        READ_ONLY="YES",
        BASE_PATH="/notes",
    )
    assert settings.data_dir == Path("/srv/notes")
    assert (settings.host, settings.port, settings.session_days) == ("127.0.0.1", 9000, 365)
    assert settings.cookie_secure is False
    assert settings.forwarded_allow_ips == "*"
    assert settings.max_upload_mb == 100
    assert settings.log_level == "DEBUG"
    assert settings.read_only is True
    assert settings.base_path == "/notes"


@pytest.mark.parametrize("name", ["USERNAME", "PASSWORD", "SECRET_KEY"])
def test_required_variables(name: str) -> None:
    env = {k: v for k, v in BASE.items() if k != f"PLAINNOTES_{name}"}
    with pytest.raises(ConfigError, match=f"PLAINNOTES_{name}"):
        load_settings(env)
    with pytest.raises(ConfigError, match=f"PLAINNOTES_{name}"):
        load_settings({**BASE, f"PLAINNOTES_{name}": ""})


def test_secret_key_length() -> None:
    with pytest.raises(ConfigError, match="PLAINNOTES_SECRET_KEY"):
        load(SECRET_KEY="x" * 31)
    assert load(SECRET_KEY="x" * 32).secret_key == "x" * 32


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "80a"),
        ("PORT", "-1"),
        ("PORT", " 80"),
        ("PORT", "٨٠"),
        ("SESSION_DAYS", "0"),
        ("SESSION_DAYS", "366"),
        ("SESSION_DAYS", "1.5"),
        ("MAX_UPLOAD_MB", "-5"),
        ("MAX_UPLOAD_MB", "lots"),
        ("COOKIE_SECURE", "maybe"),
        ("READ_ONLY", "on"),
        ("LOG_LEVEL", "TRACE"),
        ("BASE_PATH", "notes"),
        ("BASE_PATH", "/notes/"),
        ("BASE_PATH", "/"),
        ("BASE_PATH", "/no tes"),
        ("BASE_PATH", "/notes?x"),
        ("BASE_PATH", "/νότες"),
    ],
)
def test_invalid_values(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=f"PLAINNOTES_{name}"):
        load(**{name: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("Yes", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("NO", False),
    ],
)
def test_boolean_parsing(value: str, expected: bool) -> None:
    assert load(COOKIE_SECURE=value).cookie_secure is expected
    assert load(READ_ONLY=value).read_only is expected


def test_valid_base_paths() -> None:
    assert load(BASE_PATH="/a-b_c.d~e/f").base_path == "/a-b_c.d~e/f"


def test_short_password_warning_does_not_contain_password() -> None:
    warnings = config_warnings(load(PASSWORD="short-pass"))
    assert len(warnings) == 1
    assert "short-pass" not in warnings[0]
    assert config_warnings(load(PASSWORD="twelve-chars")) == []


def test_repr_hides_secrets() -> None:
    text = repr(load(PASSWORD="hunter2-hunter2", SECRET_KEY="k" * 40))
    assert "hunter2" not in text
    assert "k" * 40 not in text


def test_data_dir_checks(tmp_path: Path) -> None:
    check_data_dir(tmp_path)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ConfigError, match="does not exist"):
        check_data_dir(tmp_path / "missing")
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        check_data_dir(file)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_data_dir_not_writable_message(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(ConfigError) as info:
            check_data_dir(locked)
    finally:
        locked.chmod(0o700)
    uid, gid = os.getuid(), os.getgid()
    assert str(info.value) == (
        f"Data directory {locked} is not writable by uid {uid}. "
        f"Fix it on the host with: sudo chown -R {uid}:{gid} <your data folder>"
    )


def test_main_exits_with_code_1_on_invalid_config(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        **BASE,
        "PLAINNOTES_PORT": "99999",
        "PLAINNOTES_DATA_DIR": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, "-m", "app"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "PLAINNOTES_PORT" in result.stdout + result.stderr
    assert BASE["PLAINNOTES_PASSWORD"] not in result.stdout + result.stderr


def test_main_exits_with_code_1_on_missing_data_dir(tmp_path: Path) -> None:
    env = {"PATH": os.environ.get("PATH", ""), **BASE, "PLAINNOTES_DATA_DIR": str(tmp_path / "x")}
    result = subprocess.run(
        [sys.executable, "-m", "app"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "PLAINNOTES_DATA_DIR" in result.stdout + result.stderr


def test_healthcheck_url() -> None:
    from app.healthcheck import health_url

    assert health_url({}) == "http://127.0.0.1:8080/health"
    assert health_url({"PLAINNOTES_HOST": "0.0.0.0"}) == "http://127.0.0.1:8080/health"
    env = {"PLAINNOTES_HOST": "10.0.0.5", "PLAINNOTES_PORT": "9000", "PLAINNOTES_BASE_PATH": "/n"}
    assert health_url(env) == "http://10.0.0.5:9000/n/health"
    assert health_url({"PLAINNOTES_HOST": "::1"}) == "http://[::1]:8080/health"


def test_healthcheck_against_a_live_server(live_server, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import healthcheck

    url = live_server(base_path="/notes")
    host, port = url.removeprefix("http://").split(":")
    monkeypatch.setenv("PLAINNOTES_HOST", host)
    monkeypatch.setenv("PLAINNOTES_PORT", port)
    monkeypatch.setenv("PLAINNOTES_BASE_PATH", "/notes")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert healthcheck.main() == 0
    monkeypatch.setenv("PLAINNOTES_BASE_PATH", "/other")
    assert healthcheck.main() == 1
    monkeypatch.setenv("PLAINNOTES_PORT", "1")
    assert healthcheck.main() == 1
