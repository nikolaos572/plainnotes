"""Security headers, error format, crash handling and disabled docs endpoints."""

import logging

import pytest
from fastapi.testclient import TestClient

from app import search, web

APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
    "media-src 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
COMMON = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def assert_security_headers(response, csp: str = APP_CSP) -> None:
    for name, value in COMMON.items():
        assert response.headers.get(name) == value, name
    assert response.headers.get("content-security-policy") == csp
    assert "server" not in response.headers
    assert "strict-transport-security" not in response.headers


def test_api_json_headers(client: TestClient) -> None:
    response = client.get("/api/notes")
    assert response.status_code == 200
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"


def test_api_error_headers(anon: TestClient) -> None:
    response = anon.get("/api/notes")
    assert response.status_code == 401
    assert_security_headers(response)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": "not_authenticated", "detail": "Please log in."}


def test_health(anon: TestClient) -> None:
    response = anon.get("/health")
    assert (response.status_code, response.text) == (200, "ok")
    assert response.headers["content-type"].startswith("text/plain")
    assert_security_headers(response)


def test_unknown_paths(client: TestClient) -> None:
    for path in ["/nothing", "/api/nothing", "/api/notes/", "/static/missing.js"]:
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"code": "not_found", "detail": "Not found."}
        assert_security_headers(response)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_are_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_method_not_allowed(client: TestClient) -> None:
    response = client.patch("/api/notes/20260101-000000-abcd", json={})
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_unexpected_errors_are_generic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret detail /data/path")

    monkeypatch.setattr(search, "search", explode)
    with caplog.at_level(logging.ERROR):
        response = client.get("/api/notes")
    assert response.status_code == 500
    assert response.json() == {
        "code": "internal",
        "detail": "Something went wrong on the server.",
    }
    assert "secret detail" not in response.text
    assert_security_headers(response)
    assert "secret detail /data/path" in caplog.text
    assert "Traceback" in caplog.text


def test_validation_errors_do_not_echo_input(client: TestClient) -> None:
    response = client.post("/api/notes", json={"title": "x", "content": 12345678})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "12345678" not in response.text
    assert set(body) == {"code", "detail"}


def test_csp_constant_matches_spec() -> None:
    assert web.CSP == APP_CSP
