"""Note routes: list, create, read, update/rename and delete."""

from fastapi import APIRouter, Response

from app import attachments, models, notes, search, text
from app.models import NoteCreate, NoteUpdate
from app.routes import PROTECTED, WRITABLE, DataDirDep

router = APIRouter(dependencies=PROTECTED)


def _full(note: notes.Note) -> models.Note:
    return models.note(note, text.extract_tags(note.content), attachments.attachments_of(note.ref))


@router.get("/notes")
def list_notes(data_dir: DataDirDep) -> list[models.NoteSummary]:
    """All notes, newest first (the same notes as an empty search)."""
    notes.import_loose_files(data_dir)
    return [models.note_summary(r.ref, r.tags) for r in search.search(data_dir, "")]


@router.post("/notes", status_code=201, dependencies=WRITABLE)
def create_note(body: NoteCreate, data_dir: DataDirDep) -> models.Note:
    """Create a note."""
    return _full(notes.create_note(data_dir, body.title, body.content))


@router.get("/notes/{note_id}")
def get_note(note_id: str, data_dir: DataDirDep) -> models.Note:
    """One note with content, revision and attachments."""
    return _full(notes.read_note(data_dir, note_id))


@router.put("/notes/{note_id}", dependencies=WRITABLE)
def update_note(note_id: str, body: NoteUpdate, data_dir: DataDirDep) -> models.Note:
    """Save and/or rename, if the file has not changed since base_revision."""
    note = notes.update_note(data_dir, note_id, body.title, body.content, body.base_revision)
    return _full(note)


@router.delete("/notes/{note_id}", status_code=204, dependencies=WRITABLE)
def delete_note(note_id: str, data_dir: DataDirDep) -> Response:
    """Move the note and its attachments to the trash."""
    notes.delete_note(data_dir, note_id)
    return Response(status_code=204)
