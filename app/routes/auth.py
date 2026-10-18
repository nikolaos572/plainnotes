"""Login, logout and session routes."""

import asyncio
import logging

from fastapi import APIRouter, Depends, Request, Response

from app import auth
from app.models import LoginRequest, SessionInfo
from app.routes import PROTECTED
from app.web import ApiError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/login", status_code=204, dependencies=[Depends(auth.require_csrf)])
async def login(body: LoginRequest, request: Request) -> Response:
    """Check the credentials (rate-limited) and set the session cookie."""
    state = auth.get_auth(request)
    ip = request.client.host if request.client else "unknown"
    retry_after = state.limiter.retry_after(ip)
    if retry_after is not None:
        logger.warning("Login attempt from %s rejected: rate limited", ip)
        raise ApiError(
            429,
            "rate_limited",
            "Too many failed logins. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not auth.credentials_match(state.settings, body.username, body.password):
        state.limiter.record_failure(ip)
        logger.warning("Failed login from %s", ip)
        await asyncio.sleep(auth.FAILED_LOGIN_DELAY)
        raise ApiError(401, "login_failed", "Wrong username or password.")
    state.limiter.clear(ip)
    response = Response(status_code=204)
    response.headers.append("Set-Cookie", state.new_cookie())
    return response


@router.post("/logout", status_code=204, dependencies=PROTECTED)
def logout(request: Request) -> Response:
    """Expire the session cookie."""
    response = Response(status_code=204)
    response.headers.append("Set-Cookie", auth.get_auth(request).expired_cookie())
    return response


@router.get("/session", dependencies=PROTECTED)
def session(request: Request) -> SessionInfo:
    """The logged-in user and whether the server is read-only."""
    settings = request.app.state.settings
    return SessionInfo(username=settings.username, read_only=settings.read_only)
