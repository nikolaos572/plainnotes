"""`python -m app`: load the settings, configure logging and start uvicorn."""

import logging
import os
import sys
import time

import uvicorn

from app import __version__
from app.config import ConfigError, check_data_dir, config_warnings, load_settings
from app.main import create_app

logger = logging.getLogger("app")


def configure_logging(level: str) -> None:
    """Log to stdout with UTC timestamps."""
    formatter = logging.Formatter(
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
        check_data_dir(settings.data_dir)
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    configure_logging(settings.log_level)
    for warning in config_warnings(settings):
        logger.warning("%s", warning)
    logger.info(
        "plainnotes %s: data folder %s, listening on %s:%d, base path %r, read-only %s",
        __version__,
        settings.data_dir,
        settings.host,
        settings.port,
        settings.base_path or "/",
        settings.read_only,
    )
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
