"""Security headers, the request-size limit, and the one place that maps errors to JSON."""

import logging
import re
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import attachments, fsutil, models, notes, search, trash

logger = logging.getLogger(__name__)

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
    "media-src 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
MAX_BODY_BYTES = 32 * 1024 * 1024
SESSION_COOKIE_STATE = "session_cookie"


class ApiError(Exception):
    """An error with a fixed HTTP status, machine code and message, raised by routes and auth."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        headers: dict[str, str] | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail
        self.headers, self.extra = headers, extra


def error_response(
    status: int,
    code: str,
    detail: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    """The standard error body: {"code": ..., "detail": ...} plus optional extra fields."""
    return JSONResponse({"code": code, "detail": detail, **(extra or {})}, status, headers)


# Service exceptions → (status, code, default message). Messages never contain paths or content.
SERVICE_ERRORS: dict[type[Exception], tuple[int, str, str]] = {
    notes.NotFound: (404, "not_found", "Not found."),
    fsutil.InvalidName: (404, "not_found", "Not found."),
    notes.InvalidTitle: (400, "invalid_title", "This title is not allowed."),
    notes.TitleExists: (409, "title_exists", "Another note already has this title."),
    notes.NotUtf8: (
        422,
        "not_utf8",
        "This file is not valid UTF-8 and cannot be opened here.",
    ),
    notes.ContentTooLarge: (413, "too_large", "The content is too large."),
    notes.InvalidContent: (422, "validation_error", "The content is not valid Unicode text."),
    search.BadQuery: (400, "bad_query", "The search query is not valid."),
    trash.IdExists: (409, "id_exists", "A note with the same ID already exists."),
    attachments.UploadTooLarge: (413, "too_large", "The file is larger than the upload limit."),
    fsutil.StorageFull: (507, "storage_full", "The disk is full."),
}
_HTTP_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "too_large"}


def _service_error(request: Request, exc: Exception) -> JSONResponse:
    for exc_type in type(exc).__mro__:
        if exc_type in SERVICE_ERRORS:
            status, code, default = SERVICE_ERRORS[exc_type]
            break
    else:
        raise exc
    message = exc.args[0] if exc.args and isinstance(exc.args[0], str) else default
    return error_response(status, code, message)


def _revision_conflict(request: Request, exc: notes.RevisionConflict) -> JSONResponse:
    current = exc.current
    body = {
        "revision": current.revision,
        "title": current.ref.title,
        "content": current.content,
        "modified": models.iso_utc(current.ref.modified),
    }
    return error_response(
        409,
        "revision_conflict",
        "This note was changed somewhere else since you opened it.",
        extra={"current": body},
    )


def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.detail, exc.headers, exc.extra)


def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "error")
    detail = {404: "Not found.", 405: "Method not allowed.", 413: "The request is too large."}
    return error_response(
        exc.status_code,
        code,
        detail.get(exc.status_code, "The request could not be processed."),
        dict(exc.headers) if exc.headers else None,
    )


def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Only field locations are reported; the submitted values may be note content.
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    return error_response(422, "validation_error", "Invalid request: " + ", ".join(fields))


def install_error_handlers(app: FastAPI) -> None:
    """Register every error mapping on the app."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(notes.RevisionConflict, _revision_conflict)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    for exc_type in SERVICE_ERRORS:
        app.add_exception_handler(exc_type, _service_error)


class SecurityMiddleware:
    """Adds the security and cache headers to every response and turns crashes into 500s."""

    def __init__(self, app: ASGIApp, api_prefix: str) -> None:
        self.app = app
        self.api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                self._add_headers(scope, MutableHeaders(scope=message))
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            logger.exception("Unhandled error while serving a request")
            if started:
                raise
            response = error_response(500, "internal", "Something went wrong on the server.")
            await response(scope, receive, send_with_headers)

    def _add_headers(self, scope: Scope, headers: MutableHeaders) -> None:
        headers.setdefault("Content-Security-Policy", CSP)
        for name, value in SECURITY_HEADERS.items():
            headers[name] = value
        if "server" in headers:
            del headers["server"]
        if scope["path"].startswith(self.api_prefix):
            headers.setdefault("Cache-Control", "no-store")
        renewed = scope.get("state", {}).get(SESSION_COOKIE_STATE)
        if renewed and "set-cookie" not in headers:
            headers.append("Set-Cookie", renewed)


class BodyLimitMiddleware:
    """Rejects request bodies over MAX_BODY_BYTES, counting the bytes actually received."""

    def __init__(self, app: ASGIApp, is_exempt: Callable[[Scope], bool]) -> None:
        self.app = app
        self.is_exempt = is_exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.is_exempt(scope):
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await error_response(413, "too_large", "The request is too large.")(
                scope, receive, send
            )
            return
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise HTTPException(413)
            return message

        await self.app(scope, counting_receive, send)


def upload_path_matcher(api_prefix: str) -> Callable[[Scope], bool]:
    """A predicate matching attachment uploads, which have their own size limit."""
    pattern = re.compile(re.escape(api_prefix) + r"notes/[^/]+/attachments/[^/]+")

    def is_upload(scope: Scope) -> bool:
        return scope["method"] == "PUT" and pattern.fullmatch(scope["path"]) is not None

    return is_upload
