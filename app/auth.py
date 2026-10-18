"""Password check, signed session tokens, the login rate limiter, and auth/CSRF dependencies."""

import base64
import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

from fastapi import Request

from app.config import Settings
from app.web import SESSION_COOKIE_STATE, ApiError

COOKIE_NAME = "plainnotes_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "plainnotes"
FAILED_LOGIN_DELAY = 1.0
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TOKEN_RE = re.compile(r"v1\.([0-9]{1,12})\.([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    """Compare both values in constant time; both are always checked."""
    username_ok = hmac.compare_digest(_digest(username), _digest(settings.username))
    password_ok = hmac.compare_digest(_digest(password), _digest(settings.password))
    return username_ok & password_ok


def signing_key(settings: Settings) -> bytes:
    """Session signing key; changing the secret, username or password ends all sessions."""
    message = "plainnotes-session|" + settings.username + "|" + settings.password
    return hmac.new(
        settings.secret_key.encode("utf-8", "surrogatepass"),
        message.encode("utf-8", "surrogatepass"),
        hashlib.sha256,
    ).digest()


def _sign(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def make_token(key: bytes, expires: int) -> str:
    """A session token: v1.<expires_unix>.<nonce>.<signature>."""
    payload = f"v1.{expires}.{_b64(secrets.token_bytes(16))}"
    return f"{payload}.{_sign(key, payload)}"


def token_expiry(key: bytes, token: str, now: float) -> int | None:
    """The token's expiry time if it is well-formed, correctly signed and not expired."""
    match = _TOKEN_RE.fullmatch(token)
    if match is None:
        return None
    payload = token.rsplit(".", 1)[0]
    if not hmac.compare_digest(_sign(key, payload), match.group(3)):
        return None
    expires = int(match.group(1))
    return expires if expires > now else None


class RateLimiter:
    """Counts failed logins per client IP and across all IPs within a sliding window."""

    def __init__(
        self,
        window: float = 15 * 60,
        per_ip: int = 5,
        total: int = 50,
        max_ips: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window, self.per_ip, self.total, self.max_ips = window, per_ip, total, max_ips
        self.clock = clock
        self._by_ip: OrderedDict[str, deque[float]] = OrderedDict()
        self._all: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, failures: deque[float], now: float) -> None:
        while failures and failures[0] <= now - self.window:
            failures.popleft()

    def retry_after(self, ip: str) -> int | None:
        """Seconds until this IP may try again, or None if it may try now."""
        with self._lock:
            now = self.clock()
            waits = []
            self._prune(self._all, now)
            if len(self._all) >= self.total:
                waits.append(self._all[0] + self.window - now)
            failures = self._by_ip.get(ip)
            if failures is not None:
                self._prune(failures, now)
                if not failures:
                    del self._by_ip[ip]
                elif len(failures) >= self.per_ip:
                    waits.append(failures[0] + self.window - now)
            return max(1, math.ceil(max(waits))) if waits else None

    def record_failure(self, ip: str) -> None:
        """Count one failed login from ip."""
        with self._lock:
            now = self.clock()
            failures = self._by_ip.pop(ip, None) or deque()
            failures.append(now)
            while len(failures) > self.per_ip:
                failures.popleft()
            self._by_ip[ip] = failures
            while len(self._by_ip) > self.max_ips:
                self._by_ip.popitem(last=False)
            self._all.append(now)
            while len(self._all) > self.total:
                self._all.popleft()

    def clear(self, ip: str) -> None:
        """Forget the failures of ip (after a successful login)."""
        with self._lock:
            self._by_ip.pop(ip, None)

    def tracked_ips(self) -> int:
        """How many IPs are currently tracked."""
        with self._lock:
            return len(self._by_ip)


class Auth:
    """Per-app authentication state: settings, signing key and rate limiter."""

    def __init__(self, settings: Settings, limiter: RateLimiter | None = None) -> None:
        self.settings = settings
        self.key = signing_key(settings)
        self.lifetime = settings.session_days * 86400
        self.limiter = limiter or RateLimiter()

    def new_cookie(self, now: float | None = None) -> str:
        """A Set-Cookie header value holding a fresh session token."""
        expires = int(now if now is not None else time.time()) + self.lifetime
        return self._cookie(make_token(self.key, expires), self.lifetime)

    def expired_cookie(self) -> str:
        """A Set-Cookie header value that removes the session cookie."""
        return self._cookie("", 0) + "; Expires=Thu, 01 Jan 1970 00:00:00 GMT"

    def _cookie(self, value: str, max_age: int) -> str:
        path = self.settings.base_path + "/"
        secure = "; Secure" if self.settings.cookie_secure else ""
        return (
            f"{COOKIE_NAME}={value}; Max-Age={max_age}; Path={path}; HttpOnly; "
            f"SameSite=Strict{secure}"
        )


def get_auth(request: Request) -> Auth:
    """The app's Auth object."""
    return request.app.state.auth


def require_session(request: Request) -> str:
    """Dependency: a valid session cookie; renew it when less than half its lifetime remains."""
    auth = get_auth(request)
    token = request.cookies.get(COOKIE_NAME)
    now = time.time()
    expires = token_expiry(auth.key, token, now) if token else None
    if expires is None:
        raise ApiError(401, "not_authenticated", "Please log in.")
    if expires - now < auth.lifetime / 2:
        setattr(request.state, SESSION_COOKIE_STATE, auth.new_cookie(now))
    return auth.settings.username


def require_csrf(request: Request) -> None:
    """Dependency: mutating requests must carry X-Requested-With: plainnotes."""
    if request.method in _MUTATING_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise ApiError(403, "csrf", "This request is missing the X-Requested-With header.")


def require_writable(request: Request) -> None:
    """Dependency for every mutating route except login/logout: refuse in read-only mode."""
    if request.app.state.settings.read_only:
        raise ApiError(403, "read_only", "This server is read-only.")
