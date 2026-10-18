"""Pydantic request and response models for the JSON API, and converters from service objects."""

from datetime import UTC, datetime

from pydantic import BaseModel

from app import notes, search, trash
from app.attachments import AttachmentInfo
from app.text import Segment as TextSegment


class LoginRequest(BaseModel):
    """POST /api/login body."""

    username: str
    password: str


class NoteCreate(BaseModel):
    """POST /api/notes body."""

    title: str
    content: str


class NoteUpdate(BaseModel):
    """PUT /api/notes/{id} body."""

    title: str
    content: str
    base_revision: str


class SessionInfo(BaseModel):
    """GET /api/session response."""

    username: str
    read_only: bool


class Attachment(BaseModel):
    """One file in a note's attachments folder."""

    name: str
    size: int
    modified: str


class NoteSummary(BaseModel):
    """A note without its content."""

    id: str
    title: str
    created: str
    modified: str
    tags: list[str]


class Note(NoteSummary):
    """A note with its content, revision and attachments."""

    content: str
    revision: str
    attachments: list[Attachment]


class Segment(BaseModel):
    """Plain text; mark=True means highlight it."""

    text: str
    mark: bool


class SearchResult(BaseModel):
    """One search hit with highlighted title and snippets."""

    id: str
    title: str
    modified: str
    score: int
    tags: list[str]
    title_segments: list[Segment]
    snippets: list[list[Segment]]


class TagCount(BaseModel):
    """A tag and the number of notes using it."""

    tag: str
    count: int


class TrashEntry(BaseModel):
    """A deleted note; title is null if the folder no longer holds exactly one .md file."""

    trash_id: str
    id: str
    title: str | None
    deleted: str


def iso_utc(moment: datetime) -> str:
    """ISO 8601 UTC with whole seconds, e.g. 2026-10-18T14:32:05Z."""
    m = moment.astimezone(UTC)
    return f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}Z"


def attachment(info: AttachmentInfo) -> Attachment:
    """Convert a service AttachmentInfo."""
    return Attachment(name=info.name, size=info.size, modified=iso_utc(info.modified))


def note_summary(ref: notes.NoteRef, tags: list[str]) -> NoteSummary:
    """Convert a NoteRef plus its tags."""
    return NoteSummary(
        id=ref.id,
        title=ref.title,
        created=iso_utc(ref.created),
        modified=iso_utc(ref.modified),
        tags=tags,
    )


def note(item: notes.Note, tags: list[str], files: list[AttachmentInfo]) -> Note:
    """Convert a service Note plus its tags and attachments."""
    summary = note_summary(item.ref, tags)
    return Note(
        **summary.model_dump(),
        content=item.content,
        revision=item.revision,
        attachments=[attachment(info) for info in files],
    )


def _segments(segments: list[TextSegment]) -> list[Segment]:
    return [Segment(text=s.text, mark=s.mark) for s in segments]


def search_result(result: search.Result) -> SearchResult:
    """Convert a search Result."""
    return SearchResult(
        id=result.ref.id,
        title=result.ref.title,
        modified=iso_utc(result.ref.modified),
        score=result.score,
        tags=result.tags,
        title_segments=_segments(result.title_segments),
        snippets=[_segments(snippet) for snippet in result.snippets],
    )


def trash_entry(entry: trash.TrashEntry) -> TrashEntry:
    """Convert a service TrashEntry."""
    return TrashEntry(
        trash_id=entry.trash_id, id=entry.note_id, title=entry.title, deleted=iso_utc(entry.deleted)
    )
