"""Docker HEALTHCHECK: exit 0 if GET <host>:<port><base>/health answers 200, otherwise 1."""

import os
import sys
import urllib.request
from collections.abc import Mapping


def health_url(env: Mapping[str, str]) -> str:
    """The health URL for the configured host, port and base path."""
    host = env.get("PLAINNOTES_HOST", "")
    if host in {"", "0.0.0.0"}:
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    port = env.get("PLAINNOTES_PORT") or "8080"
    return f"http://{host}:{port}{env.get('PLAINNOTES_BASE_PATH', '')}/health"


def main() -> int:
    """Check the server once; proxies from the environment are ignored."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(health_url(os.environ), timeout=3) as response:
            return 0 if response.status == 200 else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
