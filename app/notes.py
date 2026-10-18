"""Note IDs, title rules, and listing, reading, creating, updating, deleting and importing notes."""

import hashlib
import logging
import os
import re
import secrets
import shutil
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app import fsutil
from app.fsutil import DataDir

logger = logging.getLogger(__name__)

NOTE_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}")
TRASH_DIR = ".trash"
ATTACHMENTS_DIR = "attachments"
MAX_CONTENT_BYTES = 10 * 1024 * 1024
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_FORBIDDEN_TITLE_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


class NotFound(Exception):
    """The note (or another object addressed by the request) does not exist."""


class InvalidTitle(Exception):
    """The submitted title breaks the title rules."""


class TitleExists(Exception):
    """Another note already uses this title (ignoring case)."""


class NotUtf8(Exception):
    """The note file is not valid UTF-8 and cannot be opened."""


class ContentTooLarge(Exception):
    """The content exceeds MAX_CONTENT_BYTES."""


class InvalidContent(Exception):
    """The content cannot be encoded as UTF-8 (for example, lone surrogates)."""


@dataclass(frozen=True)
class NoteRef:
    """A note found on disk: its ID, title, file path and modification time."""

    id: str
    title: str
    path: Path
    mtime: float

    @property
    def folder(self) -> Path:
        """The note folder."""
        return self.path.parent

    @property
    def modified(self) -> datetime:
        """Modification time of the .md file."""
        return mtime_moment(self.mtime)

    @property
    def created(self) -> datetime:
        """Creation time from the ID (the mtime if a hand-made ID holds an impossible date)."""
        try:
            return datetime.strptime(self.id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            return mtime_moment(self.mtime)


@dataclass(frozen=True)
class Note:
    """A note with its decoded content and revision."""

    ref: NoteRef
    content: str
    revision: str


class RevisionConflict(Exception):
    """The file changed since the client loaded it; carries the current version."""

    def __init__(self, current: Note) -> None:
        super().__init__("revision conflict")
        self.current = current


def is_note_id(name: str) -> bool:
    """True if name has the note ID format."""
    return NOTE_ID_RE.fullmatch(name) is not None


def format_id_time(moment: datetime) -> str:
    """Format a UTC time as the YYYYMMDD-HHMMSS part of an ID."""
    m = moment.astimezone(UTC)
    return f"{m.year:04d}{m.month:02d}{m.day:02d}-{m.hour:02d}{m.minute:02d}{m.second:02d}"


def new_id(moment: datetime, taken: Callable[[str], bool]) -> str:
    """Generate a note ID for moment, retrying the random suffix while taken(id) is true."""
    prefix = format_id_time(moment)
    while True:
        suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
        candidate = f"{prefix}-{suffix}"
        if not taken(candidate):
            return candidate


def is_valid_title(title: str) -> bool:
    """True if title follows the title rules (it is not trimmed here)."""
    return (
        bool(title)
        and not title.startswith(".")
        and not _FORBIDDEN_TITLE_RE.search(title)
        and fsutil.is_utf8_name(title)
        and len((title + ".md").encode("utf-8")) <= fsutil.MAX_NAME_BYTES
    )


def clean_title(raw: str) -> str:
    """Trim a submitted title and validate it; raise InvalidTitle if it breaks the rules."""
    title = raw.strip()
    if not is_valid_title(title):
        raise InvalidTitle(
            "Titles must not be empty, start with '.', contain / \\ : * ? \" < > | or "
            "control characters, or be longer than 252 bytes."
        )
    return title


def title_key(title: str) -> str:
    """Comparison key for title uniqueness: NFC-normalized and case-folded."""
    return unicodedata.normalize("NFC", title).casefold()


def _is_note_filename(name: str) -> bool:
    return name.endswith(".md") and is_valid_title(name[:-3])


def revision_of(data: bytes) -> str:
    """The revision of a file's bytes: their SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def encode_content(content: str) -> bytes:
    """Encode content as UTF-8 exactly; raise if it is too large or not encodable."""
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidContent("The content is not valid Unicode text.") from None
    if len(data) > MAX_CONTENT_BYTES:
        raise ContentTooLarge("Notes can be at most 10 MiB.")
    return data


def md_files(folder: Path) -> list[os.DirEntry[str]]:
    """The regular, non-hidden .md files directly inside folder."""
    with os.scandir(folder) as entries:
        return [
            entry
            for entry in entries
            if entry.name.endswith(".md")
            and not entry.name.startswith(".")
            and entry.is_file(follow_symlinks=False)
        ]


def _note_in_folder(data_dir: DataDir, folder: Path, note_id: str) -> NoteRef | None:
    try:
        found = md_files(folder)
    except OSError:
        return None
    if len(found) != 1:
        data_dir.warn_once(
            logger, "Skipping note folder %r: it contains %d .md files", note_id, len(found)
        )
        return None
    entry = found[0]
    if not fsutil.is_utf8_name(entry.name):
        data_dir.warn_once(logger, "Skipping note folder %r: file name is not UTF-8", note_id)
        return None
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return None
    return NoteRef(id=note_id, title=entry.name[:-3], path=Path(entry.path), mtime=mtime)


def list_notes(data_dir: DataDir) -> list[NoteRef]:
    """All notes currently on disk, in no particular order."""
    refs = []
    with os.scandir(data_dir.root) as entries:
        candidates = [
            entry
            for entry in entries
            if is_note_id(entry.name) and entry.is_dir(follow_symlinks=False)
        ]
    for entry in candidates:
        ref = _note_in_folder(data_dir, Path(entry.path), entry.name)
        if ref is not None:
            refs.append(ref)
    return refs


def find_note(data_dir: DataDir, note_id: str) -> NoteRef:
    """Look up one note by ID; raise NotFound for invalid or unknown IDs."""
    try:
        folder = fsutil.safe_child(data_dir.root, note_id, is_note_id)
    except fsutil.InvalidName:
        raise NotFound from None
    ref = _note_in_folder(data_dir, folder, note_id) if fsutil.is_real_dir(folder) else None
    if ref is None:
        raise NotFound
    return ref


def _load(ref: NoteRef) -> tuple[bytes, str]:
    try:
        data = fsutil.read_file(ref.path)
    except OSError:
        raise NotFound from None
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        raise NotUtf8 from None


def read_content(ref: NoteRef) -> str | None:
    """The note's text, or None if the file is unreadable or not UTF-8."""
    try:
        return _load(ref)[1]
    except (NotFound, NotUtf8):
        return None


def read_note(data_dir: DataDir, note_id: str) -> Note:
    """Read one note; raise NotFound or NotUtf8."""
    ref = find_note(data_dir, note_id)
    data, content = _load(ref)
    return Note(ref=ref, content=content, revision=revision_of(data))


def _ensure_title_free(data_dir: DataDir, title: str, own_id: str | None = None) -> None:
    key = title_key(title)
    for ref in list_notes(data_dir):
        if ref.id != own_id and title_key(ref.title) == key:
            raise TitleExists("Another note already has this title.")


def _id_taken(data_dir: DataDir) -> Callable[[str], bool]:
    return lambda candidate: os.path.lexists(data_dir.root / candidate)


def create_note(data_dir: DataDir, title: str, content: str) -> Note:
    """Create a note folder with one .md file; the note appears complete or not at all."""
    title = clean_title(title)
    data = encode_content(content)
    with data_dir.lock:
        _ensure_title_free(data_dir, title)
        temp = fsutil.create_temp_dir(data_dir.root)
        try:
            fsutil.atomic_write(fsutil.safe_child(temp, title + ".md", _is_note_filename), data)
            note_id = new_id(datetime.now(UTC), _id_taken(data_dir))
            temp.rename(data_dir.root / note_id)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        fsutil.fsync_dir(data_dir.root)
        ref = find_note(data_dir, note_id)
    return Note(ref=ref, content=content, revision=revision_of(data))


def update_note(
    data_dir: DataDir, note_id: str, title: str, content: str, base_revision: str
) -> Note:
    """Save new content and/or a new title, refusing if the file changed since base_revision."""
    data = encode_content(content)
    with data_dir.lock:
        ref = find_note(data_dir, note_id)
        current_data, current_content = _load(ref)
        current = Note(ref=ref, content=current_content, revision=revision_of(current_data))
        if current.revision != base_revision:
            raise RevisionConflict(current)
        new_title = ref.title if title == ref.title else clean_title(title)
        if new_title == ref.title and data == current_data:
            return current
        if new_title != ref.title:
            _ensure_title_free(data_dir, new_title, own_id=ref.id)
        if data != current_data:
            fsutil.atomic_write(ref.path, data)
        if new_title != ref.title:
            ref.path.rename(fsutil.safe_child(ref.folder, new_title + ".md", _is_note_filename))
            fsutil.fsync_dir(ref.folder)
        ref = find_note(data_dir, note_id)
    return Note(ref=ref, content=content, revision=revision_of(data))


def trash_dir(data_dir: DataDir) -> Path:
    """The trash folder, created if missing; raise OSError if it is not a real folder."""
    path = data_dir.root / TRASH_DIR
    if not os.path.lexists(path):
        path.mkdir(exist_ok=True)
    if not fsutil.is_real_dir(path):
        raise OSError(f"{TRASH_DIR} is not a directory")
    return path


def delete_note(data_dir: DataDir, note_id: str, now: datetime | None = None) -> str:
    """Move a note folder, attachments included, into the trash; return the trash ID."""
    with data_dir.lock:
        ref = find_note(data_dir, note_id)
        trash = trash_dir(data_dir)
        moment = (now or datetime.now(UTC)).timestamp()
        while True:
            trash_id = f"{format_id_time(datetime.fromtimestamp(moment, UTC))}_{ref.id}"
            if not os.path.lexists(trash / trash_id):
                break
            moment += 1
        ref.folder.rename(trash / trash_id)
        fsutil.fsync_dir(data_dir.root)
    return trash_id


def cleanup_temp_files(data_dir: DataDir) -> None:
    """Startup cleanup: remove leftover temp entries at the top level and inside note folders."""
    removed = fsutil.remove_temp_entries(data_dir.root)
    with os.scandir(data_dir.root) as entries:
        folders = [
            Path(entry.path)
            for entry in entries
            if is_note_id(entry.name) and entry.is_dir(follow_symlinks=False)
        ]
    for folder in folders:
        removed += fsutil.remove_temp_entries(folder)
        attachments = folder / ATTACHMENTS_DIR
        if fsutil.is_real_dir(attachments):
            removed += fsutil.remove_temp_entries(attachments)
    if removed:
        logger.info("Removed %d leftover temporary files", removed)


def mtime_moment(mtime: float) -> datetime:
    """A file timestamp as a UTC datetime; now if the timestamp is out of range."""
    try:
        return datetime.fromtimestamp(mtime, UTC)
    except (OverflowError, OSError, ValueError):
        return datetime.now(UTC)


def import_loose_files(data_dir: DataDir) -> None:
    """Move every loose top-level .md file into a new note folder (the drop-in import)."""
    with os.scandir(data_dir.root) as entries:
        names = [
            entry.name
            for entry in entries
            if entry.name.endswith(".md")
            and not entry.name.startswith(".")
            and entry.is_file(follow_symlinks=False)
        ]
    if not names:
        return
    with data_dir.lock:
        taken = {title_key(ref.title) for ref in list_notes(data_dir)}
        for name in sorted(names):
            _import_one(data_dir, name, taken)


def _import_one(data_dir: DataDir, name: str, taken: set[str]) -> None:
    title = name[:-3]
    if not is_valid_title(title):
        data_dir.warn_once(logger, "Not importing %r: the file name is not a valid title", name)
        return
    if title_key(title) in taken:
        data_dir.warn_once(logger, "Not importing %r: a note with this title exists", name)
        return
    source = fsutil.safe_child(data_dir.root, name, _is_note_filename)
    try:
        mtime = source.lstat().st_mtime
    except OSError:
        return
    if not fsutil.is_real_file(source):
        return
    temp = fsutil.create_temp_dir(data_dir.root)
    try:
        source.rename(temp / name)
        note_id = new_id(mtime_moment(mtime), _id_taken(data_dir))
        temp.rename(data_dir.root / note_id)
    except OSError:
        if os.path.lexists(temp / name):
            (temp / name).rename(source)
        shutil.rmtree(temp, ignore_errors=True)
        data_dir.warn_once(logger, "Could not import %r", name)
        return
    fsutil.fsync_dir(data_dir.root)
    taken.add(title_key(title))
    logger.info("Imported %r as note %s", name, note_id)
