"""`python -m app`: load the settings, configure logging and start uvicorn."""

import logging
import os
import sys
import time
from dataclasses import replace

import uvicorn

from app import __version__, storage
from app.config import ConfigError, config_warnings, load_settings
from app.main import create_app

logger = logging.getLogger("app")


class LogFormatter(logging.Formatter):
    """Show Uvicorn's server log as "uvicorn" instead of "uvicorn.error".

    Uvicorn sends all its own messages, routine ones such as "Started server process" included,
    to a logger named "uvicorn.error", after Gunicorn's error log. In this log format that made a
    healthy start look like a list of errors; the level still marks real errors. The record is
    copied, not changed, so other handlers still see the real logger name.
    """

    def format(self, record: logging.LogRecord) -> str:
        if record.name == "uvicorn.error":
            record = logging.makeLogRecord({**record.__dict__, "name": "uvicorn"})
        return super().format(record)


def configure_logging(level: str) -> None:
    """Log to stdout with UTC timestamps."""
    formatter = LogFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def main() -> None:
    """Start the server, or exit with code 1 if the configuration is invalid."""
    configure_logging("INFO")
    try:
        settings = load_settings(os.environ)
        prepared = storage.prepare(settings.data_dir, read_only=settings.read_only)
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    configure_logging(settings.log_level)
    for warning in [*config_warnings(settings), *prepared.warnings]:
        logger.warning("%s", warning)
    if prepared.read_only_reason:
        logger.error("%s", prepared.read_only_reason)
        settings = replace(settings, read_only=True, read_only_reason=prepared.read_only_reason)
    logger.info(
        "plainnotes %s: data folder %s, listening on %s:%d, base path %r, read-only %s, "
        "check attachments hash %s",
        __version__,
        settings.data_dir,
        settings.host,
        settings.port,
        settings.base_path or "/",
        settings.read_only,
        settings.check_attachments_hash,
    )
    for message in prepared.info:
        logger.info("%s", message)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        server_header=False,
        access_log=False,
        log_config=None,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
