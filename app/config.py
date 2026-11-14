"""Parse and validate the PLAINNOTES_* environment variables into an immutable Settings object."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_BOOL_VALUES = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_INTEGER_RE = re.compile(r"[0-9]{1,9}")
_BASE_PATH_RE = re.compile(r"/[A-Za-z0-9\-_.~/]*")
MIN_SECRET_KEY_LENGTH = 32
SHORT_PASSWORD_LENGTH = 12


class ConfigError(Exception):
    """An environment variable is missing or invalid; the message names the variable."""


@dataclass(frozen=True)
class Settings:
    """All runtime configuration. Secrets are excluded from repr so they never reach a log."""

    username: str
    password: str = field(repr=False)
    secret_key: str = field(repr=False)
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8080
    session_days: int = 30
    cookie_secure: bool = True
    forwarded_allow_ips: str = "127.0.0.1"
    max_upload_mb: int = 0
    # Uploads must carry their SHA-256, and are saved only if the received bytes match it.
    check_attachments_hash: bool = False
    log_level: str = "INFO"
    read_only: bool = False
    base_path: str = ""
    # Set at startup, not from the environment, when the storage check turns saving off.
    read_only_reason: str = ""


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build Settings from an environment mapping; raise ConfigError on the first invalid value."""

    def optional(name: str) -> str | None:
        value = env.get("PLAINNOTES_" + name)
        return value if value else None

    def required(name: str) -> str:
        value = optional(name)
        if value is None:
            raise ConfigError(f"PLAINNOTES_{name} is required and must not be empty.")
        return value

    secret_key = required("SECRET_KEY")
    if len(secret_key) < MIN_SECRET_KEY_LENGTH:
        raise ConfigError(
            f"PLAINNOTES_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters long."
        )
    defaults = Settings(username="", password="", secret_key="")
    return Settings(
        username=required("USERNAME"),
        password=required("PASSWORD"),
        secret_key=secret_key,
        data_dir=Path(optional("DATA_DIR") or defaults.data_dir),
        host=optional("HOST") or defaults.host,
        port=_integer(optional("PORT"), "PORT", defaults.port, 1, 65535),
        session_days=_integer(
            optional("SESSION_DAYS"), "SESSION_DAYS", defaults.session_days, 1, 365
        ),
        cookie_secure=_boolean(optional("COOKIE_SECURE"), "COOKIE_SECURE", defaults.cookie_secure),
        forwarded_allow_ips=optional("FORWARDED_ALLOW_IPS") or defaults.forwarded_allow_ips,
        max_upload_mb=_integer(
            optional("MAX_UPLOAD_MB"), "MAX_UPLOAD_MB", defaults.max_upload_mb, 0, None
        ),
        check_attachments_hash=_boolean(
            optional("CHECK_ATTACHMENTS_HASH"),
            "CHECK_ATTACHMENTS_HASH",
            defaults.check_attachments_hash,
        ),
        log_level=_log_level(optional("LOG_LEVEL")),
        read_only=_boolean(optional("READ_ONLY"), "READ_ONLY", defaults.read_only),
        base_path=_base_path(optional("BASE_PATH")),
    )


def config_warnings(settings: Settings) -> list[str]:
    """Return warnings about weak but accepted settings, without any secret values."""
    warnings = []
    if len(settings.password) < SHORT_PASSWORD_LENGTH:
        warnings.append(
            f"PLAINNOTES_PASSWORD is shorter than {SHORT_PASSWORD_LENGTH} characters; "
            "choose a longer password."
        )
    return warnings


def _integer(value: str | None, name: str, default: int, low: int, high: int | None) -> int:
    if value is None:
        return default
    number = int(value) if _INTEGER_RE.fullmatch(value) else None
    if number is None or number < low or (high is not None and number > high):
        limit = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ConfigError(f"PLAINNOTES_{name} must be a whole number {limit}.")
    return number


def _boolean(value: str | None, name: str, default: bool) -> bool:
    if value is None:
        return default
    parsed = _BOOL_VALUES.get(value.lower())
    if parsed is None:
        raise ConfigError(f"PLAINNOTES_{name} must be one of true/false/1/0/yes/no.")
    return parsed


def _log_level(value: str | None) -> str:
    level = (value or "INFO").upper()
    if level not in _LOG_LEVELS:
        raise ConfigError("PLAINNOTES_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR.")
    return level


def _base_path(value: str | None) -> str:
    if value is None:
        return ""
    if not _BASE_PATH_RE.fullmatch(value) or any(
        part in {"", ".", ".."} for part in value.split("/")[1:]
    ):
        raise ConfigError(
            "PLAINNOTES_BASE_PATH must start with '/', must not end with '/', and may only "
            "contain A-Z a-z 0-9 - _ . ~ / with no empty, dot or dot-dot segments"
        )
    return value
