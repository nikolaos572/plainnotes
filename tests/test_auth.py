"""Sessions, the login rate limiter, login/logout, route protection and CSRF."""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app import auth
from app.auth import RateLimiter
from tests.conftest import CSRF, PASSWORD, USERNAME, login, make_settings

# Routes that must work without a session.
PUBLIC = {("POST", "/api/login"), ("GET", "/health"), ("GET", "/")}
PARAM_VALUES = {
    "note_id": "20260101-000000-abcd",
    "filename": "file.txt",
    "trash_id": "20260101-000000_20260101-000000-abcd",
}


def api_routes(app: FastAPI) -> list[tuple[str, str]]:
    """(method, path) for every route in app.routes (static mounts excluded), params filled in."""
    found = []
    for route in iter_route_contexts(app.routes):
        if not route.methods:
            continue
        path = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: PARAM_VALUES[m.group(1)], route.path)
        found.extend((method, path) for method in sorted(route.methods))
    return found


# --- tokens ---------------------------------------------------------------------------


@pytest.fixture
def key(tmp_path) -> bytes:
    return auth.signing_key(make_settings(tmp_path))


def test_token_round_trip(key: bytes) -> None:
    token = auth.make_token(key, 2_000_000_000)
    assert re.fullmatch(r"v1\.2000000000\.[\w-]{22}\.[\w-]{43}", token)
    assert auth.token_expiry(key, token, now=1_999_999_999) == 2_000_000_000


def test_expired_token(key: bytes) -> None:
    token = auth.make_token(key, 1000)
    assert auth.token_expiry(key, token, now=1000) is None
    assert auth.token_expiry(key, token, now=999) == 1000


def flip(char: str) -> str:
    return "A" if char != "A" else "B"


def test_tampered_tokens(key: bytes) -> None:
    token = auth.make_token(key, 2_000_000_000)
    version, expires, nonce, signature = token.split(".")
    tampered = [
        f"{version}.2000000001.{nonce}.{signature}",
        f"{version}.{expires}.{flip(nonce[0])}{nonce[1:]}.{signature}",
        f"{version}.{expires}.{nonce}.{flip(signature[0])}{signature[1:]}",
        f"v2.{expires}.{nonce}.{signature}",
        token + ".",
        token[:-1],
        "",
        "garbage",
        f"{version}.{expires}.{nonce}",
    ]
    for value in tampered:
        assert auth.token_expiry(key, value, now=0) is None, value


def test_wrong_version_even_if_signed(key: bytes) -> None:
    payload = "v2.2000000000.AAAAAAAAAAAAAAAAAAAAAA"
    token = f"{payload}.{auth._sign(key, payload)}"
    assert auth.token_expiry(key, token, now=0) is None


def test_different_secret_or_credentials(tmp_path) -> None:
    settings = make_settings(tmp_path)
    token = auth.make_token(auth.signing_key(settings), 2_000_000_000)
    for changed in [
        replace(settings, secret_key="another-secret-key-0123456789abcdef"),
        replace(settings, password="new password"),
        replace(settings, username="someone"),
    ]:
        assert auth.token_expiry(auth.signing_key(changed), token, now=0) is None
    assert auth.token_expiry(auth.signing_key(settings), token, now=0) == 2_000_000_000


def test_credentials_match(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert auth.credentials_match(settings, USERNAME, PASSWORD)
    assert not auth.credentials_match(settings, USERNAME.upper(), PASSWORD)
    assert not auth.credentials_match(settings, USERNAME, PASSWORD + " ")
    assert not auth.credentials_match(settings, PASSWORD, USERNAME)
    assert not auth.credentials_match(settings, "\ud800", "\udfff")


def session_with_expiry(client: TestClient, key: bytes, expires: int) -> None:
    client.cookies.set(auth.COOKIE_NAME, auth.make_token(key, expires))


def test_renewal_when_less_than_half_remains(anon: TestClient) -> None:
    state = anon.app.state.auth
    session_with_expiry(anon, state.key, int(time.time()) + state.lifetime // 2 - 60)
    response = anon.get("/api/session")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    new_token = re.match(r"plainnotes_session=([^;]+);", cookie).group(1)
    expires = auth.token_expiry(state.key, new_token, time.time())
    assert expires is not None and expires > time.time() + state.lifetime - 60


def test_no_renewal_for_fresh_session(anon: TestClient) -> None:
    state = anon.app.state.auth
    session_with_expiry(anon, state.key, int(time.time()) + state.lifetime - 60)
    response = anon.get("/api/session")
    assert response.status_code == 200
    assert "set-cookie" not in response.headers


def test_expired_session_is_rejected(anon: TestClient) -> None:
    session_with_expiry(anon, anon.app.state.auth.key, int(time.time()) - 1)
    response = anon.get("/api/session")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# --- rate limiter ---------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_per_ip_limit_and_window() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(4):
        limiter.record_failure("1.1.1.1")
        clock.now += 10
    assert limiter.retry_after("1.1.1.1") is None
    limiter.record_failure("1.1.1.1")
    retry = limiter.retry_after("1.1.1.1")
    assert retry == 15 * 60 - 40
    assert limiter.retry_after("2.2.2.2") is None
    clock.now = 1000 + 15 * 60 + 1
    assert limiter.retry_after("1.1.1.1") is None


def test_global_limit() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for i in range(50):
        limiter.record_failure(f"10.0.0.{i}")
    assert limiter.retry_after("192.168.1.1") == 15 * 60
    clock.now += 15 * 60
    assert limiter.retry_after("192.168.1.1") is None


def test_clear_on_success() -> None:
    limiter = RateLimiter(clock=Clock())
    for _ in range(5):
        limiter.record_failure("1.1.1.1")
    limiter.clear("1.1.1.1")
    assert limiter.retry_after("1.1.1.1") is None


def test_memory_bound() -> None:
    limiter = RateLimiter(max_ips=100, total=10**9, clock=Clock())
    for i in range(1000):
        limiter.record_failure(f"ip-{i}")
    assert limiter.tracked_ips() == 100
    for _ in range(5):
        limiter.record_failure("ip-0")
    assert limiter.retry_after("ip-0") is not None
    assert limiter.tracked_ips() == 100
    default = RateLimiter()
    assert default.max_ips == 10_000


# --- login and logout -----------------------------------------------------------------


def test_login_success_and_session(anon: TestClient) -> None:
    assert anon.get("/api/session").status_code == 401
    response = anon.post(
        "/api/login", json={"username": USERNAME, "password": PASSWORD}, headers=CSRF
    )
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/;" in cookie
    assert f"Max-Age={30 * 86400}" in cookie
    assert anon.get("/api/session").json() == {"username": USERNAME, "read_only": False}


def test_secure_cookie_by_default(app_factory: Callable[..., FastAPI]) -> None:
    with TestClient(app_factory(cookie_secure=True), base_url="https://testserver") as client:
        response = client.post(
            "/api/login", json={"username": USERNAME, "password": PASSWORD}, headers=CSRF
        )
    assert "; Secure" in response.headers["set-cookie"]


def test_login_failure_does_not_reveal_which_field(anon: TestClient) -> None:
    bodies = [
        {"username": USERNAME, "password": "wrong"},
        {"username": "wrong", "password": PASSWORD},
    ]
    responses = [anon.post("/api/login", json=body, headers=CSRF) for body in bodies]
    assert [r.status_code for r in responses] == [401, 401]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["code"] == "login_failed"
    assert "set-cookie" not in responses[0].headers


def test_login_rate_limit(anon: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            response = anon.post(
                "/api/login", json={"username": USERNAME, "password": "guess"}, headers=CSRF
            )
            assert response.status_code == 401
        limited = anon.post(
            "/api/login", json={"username": USERNAME, "password": PASSWORD}, headers=CSRF
        )
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 900
    assert "set-cookie" not in limited.headers
    log = caplog.text
    assert log.count("Failed login from testclient") == 5
    assert "rate limited" in log
    assert "guess" not in log
    assert PASSWORD not in log
    assert USERNAME not in log


def test_success_clears_failures(anon: TestClient) -> None:
    for _ in range(4):
        anon.post("/api/login", json={"username": "x", "password": "y"}, headers=CSRF)
    login(anon)
    for _ in range(4):
        response = anon.post("/api/login", json={"username": "x", "password": "y"}, headers=CSRF)
        assert response.status_code == 401


def test_login_validation(anon: TestClient) -> None:
    response = anon.post("/api/login", json={"username": USERNAME}, headers=CSRF)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_logout(client: TestClient) -> None:
    response = client.post("/api/logout")
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/api/session").status_code == 401


def test_logout_is_not_undone_by_renewal(anon: TestClient) -> None:
    state = anon.app.state.auth
    session_with_expiry(anon, state.key, int(time.time()) + 60)
    response = anon.post("/api/logout", headers=CSRF)
    assert response.headers.get_list("set-cookie") == [state.expired_cookie()]


# --- route protection -----------------------------------------------------------------


def test_every_route_requires_a_session(anon: TestClient) -> None:
    routes = api_routes(anon.app)
    assert ("GET", "/api/notes") in routes
    assert ("POST", "/api/logout") in routes
    checked = 0
    for method, path in routes:
        if (method, path) in PUBLIC:
            continue
        response = anon.request(method, path, headers=CSRF, json={})
        assert response.status_code == 401, (method, path)
        assert response.json()["code"] == "not_authenticated"
        checked += 1
    assert checked >= 8


def test_only_known_routes_are_public(anon: TestClient) -> None:
    public = {(m, p) for m, p in api_routes(anon.app) if anon.request(m, p).status_code != 401}
    assert public <= PUBLIC


def test_every_mutation_requires_the_csrf_header(client: TestClient) -> None:
    mutations = [
        (m, p) for m, p in api_routes(client.app) if m in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert ("POST", "/api/login") in mutations
    for method, path in mutations:
        for headers in [{"X-Requested-With": ""}, {"X-Requested-With": "XMLHttpRequest"}]:
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code == 403, (method, path)
            assert response.json()["code"] == "csrf"


def test_login_without_csrf_header(anon: TestClient) -> None:
    response = anon.post("/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_no_cors_headers(client: TestClient) -> None:
    response = client.options(
        "/api/notes",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert not any(name.startswith("access-control-") for name in response.headers)
    response = client.get("/api/notes", headers={"Origin": "https://evil.example"})
    assert not any(name.startswith("access-control-") for name in response.headers)
