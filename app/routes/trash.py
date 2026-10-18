"""Trash routes: list, restore, delete permanently and empty."""

from fastapi import APIRouter, Response

from app import models, notes, text, trash
from app.routes import PROTECTED, WRITABLE, DataDirDep

router = APIRouter(dependencies=PROTECTED)


@router.get("/trash")
def list_trash(data_dir: DataDirDep) -> list[models.TrashEntry]:
    """Deleted notes, most recently deleted first."""
    return [models.trash_entry(entry) for entry in trash.list_trash(data_dir)]


@router.post("/trash/{trash_id}/restore", dependencies=WRITABLE)
def restore(trash_id: str, data_dir: DataDirDep) -> models.NoteSummary:
    """Move a deleted note back into the notes list."""
    ref = trash.restore(data_dir, trash_id)
    content = notes.read_content(ref)
    return models.note_summary(ref, text.extract_tags(content) if content else [])


@router.delete("/trash/{trash_id}", status_code=204, dependencies=WRITABLE)
def delete_permanently(trash_id: str, data_dir: DataDirDep) -> Response:
    """Delete one trash entry and its attachments for good."""
    trash.delete_permanently(data_dir, trash_id)
    return Response(status_code=204)


@router.delete("/trash", status_code=204, dependencies=WRITABLE)
def empty_trash(data_dir: DataDirDep) -> Response:
    """Delete everything in the trash."""
    trash.empty(data_dir)
    return Response(status_code=204)
