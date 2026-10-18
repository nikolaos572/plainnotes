"""Attachment routes: list, upload (raw streamed body), open/download and delete."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app import attachments, models
from app.routes import PROTECTED, WRITABLE, DataDirDep
from app.web import ApiError

router = APIRouter(dependencies=PROTECTED)

# Replaces the app CSP: nothing in an attachment may run, even if a browser renders it.
ATTACHMENT_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-cache",
}


@router.get("/notes/{note_id}/attachments")
def list_attachments(note_id: str, data_dir: DataDirDep) -> list[models.Attachment]:
    """The note's attachments, sorted by name."""
    return [models.attachment(info) for info in attachments.list_attachments(data_dir, note_id)]


@router.put("/notes/{note_id}/attachments/{filename}", status_code=201, dependencies=WRITABLE)
async def upload_attachment(
    note_id: str, filename: str, request: Request, data_dir: DataDirDep
) -> models.Attachment:
    """Stream the raw body into a temp file, then give it a free final name."""
    limit = request.app.state.settings.max_upload_mb * 1024 * 1024
    declared = request.headers.get("content-length", "")
    if limit and declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise attachments.UploadTooLarge
    upload = await run_in_threadpool(attachments.Upload, data_dir, note_id)
    try:
        async for chunk in request.stream():
            if limit and upload.size + len(chunk) > limit:
                raise attachments.UploadTooLarge
            await run_in_threadpool(upload.write, chunk)
        info = await run_in_threadpool(upload.finish, data_dir, filename)
    except ClientDisconnect:
        await run_in_threadpool(upload.discard)
        raise ApiError(400, "bad_request", "The upload was interrupted.") from None
    except BaseException:
        await run_in_threadpool(upload.discard)
        raise
    return models.attachment(info)


@router.get("/notes/{note_id}/attachments/{filename}")
def get_attachment(note_id: str, filename: str, data_dir: DataDirDep) -> FileResponse:
    """Serve an attachment: allowlisted types inline, everything else as a download."""
    path, info = attachments.find_attachment(data_dir, note_id, filename)
    media_type, inline = attachments.content_type(filename)
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        stat_result=info,
        content_disposition_type="inline" if inline else "attachment",
        headers=ATTACHMENT_HEADERS,
    )


@router.delete("/notes/{note_id}/attachments/{filename}", status_code=204, dependencies=WRITABLE)
def delete_attachment(note_id: str, filename: str, data_dir: DataDirDep) -> Response:
    """Delete one attachment permanently."""
    attachments.delete_attachment(data_dir, note_id, filename)
    return Response(status_code=204)
