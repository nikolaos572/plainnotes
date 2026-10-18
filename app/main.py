"""create_app(settings): the FastAPI app with its middleware, routers and static files."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app import notes, web
from app.auth import Auth
from app.config import Settings
from app.fsutil import DataDir
from app.routes import attachments as attachment_routes
from app.routes import auth as auth_routes
from app.routes import notes as note_routes
from app.routes import search as search_routes
from app.routes import trash as trash_routes

STATIC_DIR = Path(__file__).parent / "static"
# Explicit types, so serving never depends on the container's MIME database.
_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# FastAPI's built-in OpenTelemetry support stays off: no telemetry of any kind.
_NO_TELEMETRY = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}


class _StaticFiles(StaticFiles):
    """Static files with explicit content types and Cache-Control: no-cache."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """Serve a static file with an explicit type and revalidation on every use."""
        response = super().file_response(full_path, stat_result, scope, status_code)
        media_type = _MEDIA_TYPES.get(Path(full_path).suffix)
        if media_type and response.status_code != 304:
            response.headers["Content-Type"] = media_type
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings: Settings) -> FastAPI:
    """Build an independent app instance from settings."""
    data_dir = DataDir(settings.data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        notes.cleanup_temp_files(data_dir)
        notes.import_loose_files(data_dir)
        yield

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
        telemetry=_NO_TELEMETRY,
    )
    app.state.settings = settings
    app.state.data_dir = data_dir
    app.state.auth = Auth(settings)

    base = settings.base_path
    api_prefix = base + "/api"
    web.install_error_handlers(app)
    app.add_middleware(web.BodyLimitMiddleware, is_exempt=web.upload_path_matcher(api_prefix + "/"))
    app.add_middleware(web.SecurityMiddleware, api_prefix=api_prefix + "/")
    for module in (auth_routes, note_routes, attachment_routes, search_routes, trash_routes):
        app.include_router(module.router, prefix=api_prefix)

    static = _StaticFiles(directory=STATIC_DIR)

    async def index(request: Request) -> Response:
        return await static.get_response("index.html", request.scope)

    def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.add_api_route(base + "/", index, methods=["GET"], include_in_schema=False)
    app.add_api_route(base + "/health", health, methods=["GET"], include_in_schema=False)
    app.mount(base + "/static", static, name="static")
    if base:

        def to_base() -> RedirectResponse:
            return RedirectResponse(base + "/", status_code=308)

        app.add_api_route(base, to_base, methods=["GET"], include_in_schema=False)
    return app
