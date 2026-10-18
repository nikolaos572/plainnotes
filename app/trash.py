"""The trash: list deleted notes, restore them, or delete them permanently."""

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app import fsutil, notes
from app.fsutil import DataDir

TRASH_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}_[0-9]{8}-[0-9]{6}-[a-z0-9]{4}")


class IdExists(Exception):
    """A note with the same ID is already back in the data folder."""


@dataclass(frozen=True)
class TrashEntry:
    """A deleted note folder; title is None unless it holds exactly one .md file."""

    trash_id: str
    note_id: str
    title: str | None
    deleted: datetime


def is_trash_id(name: str) -> bool:
    """True if name has the trash ID format <deleted at>_<note ID>."""
    return TRASH_ID_RE.fullmatch(name) is not None


def _trash_root(data_dir: DataDir) -> Path | None:
    path = data_dir.root / notes.TRASH_DIR
    return path if fsutil.is_real_dir(path) else None


def _entry_folder(data_dir: DataDir, trash_id: str) -> Path:
    root = _trash_root(data_dir)
    if root is None:
        raise notes.NotFound
    try:
        folder = fsutil.safe_child(root, trash_id, is_trash_id)
    except fsutil.InvalidName:
        raise notes.NotFound from None
    if not fsutil.is_real_dir(folder):
        raise notes.NotFound
    return folder


def _title(folder: Path) -> str | None:
    try:
        found = notes.md_files(folder)
    except OSError:
        return None
    if len(found) != 1 or not fsutil.is_utf8_name(found[0].name):
        return None
    return found[0].name[:-3]


def _deleted_at(trash_id: str) -> datetime:
    try:
        return datetime.strptime(trash_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)


def list_trash(data_dir: DataDir) -> list[TrashEntry]:
    """Everything in the trash, most recently deleted first."""
    root = _trash_root(data_dir)
    if root is None:
        return []
    entries = []
    for folder in root.iterdir():
        if is_trash_id(folder.name) and fsutil.is_real_dir(folder):
            entries.append(
                TrashEntry(
                    trash_id=folder.name,
                    note_id=folder.name.partition("_")[2],
                    title=_title(folder),
                    deleted=_deleted_at(folder.name),
                )
            )
    return sorted(entries, key=lambda entry: entry.trash_id, reverse=True)


def restore(data_dir: DataDir, trash_id: str) -> notes.NoteRef:
    """Move a deleted note back; refuse if its ID or title is in use."""
    with data_dir.lock:
        folder = _entry_folder(data_dir, trash_id)
        note_id = trash_id.partition("_")[2]
        title = _title(folder)
        if title is None:
            raise notes.NotFound("This trash entry has no note file to restore.")
        target = fsutil.safe_child(data_dir.root, note_id, notes.is_note_id)
        if target.exists() or target.is_symlink():
            raise IdExists(
                "A note with the same ID already exists. Rename or delete that note first."
            )
        key = notes.title_key(title)
        if any(notes.title_key(ref.title) == key for ref in notes.list_notes(data_dir)):
            raise notes.TitleExists(
                f"Another note is called “{title}”. Rename that note first, then restore."
            )
        folder.rename(target)
        fsutil.fsync_dir(data_dir.root)
        return notes.find_note(data_dir, note_id)


def delete_permanently(data_dir: DataDir, trash_id: str) -> None:
    """Delete one trash entry and everything in it."""
    with data_dir.lock:
        shutil.rmtree(_entry_folder(data_dir, trash_id))


def empty(data_dir: DataDir) -> None:
    """Delete every entry in the trash."""
    with data_dir.lock:
        for entry in list_trash(data_dir):
            shutil.rmtree(_entry_folder(data_dir, entry.trash_id))
