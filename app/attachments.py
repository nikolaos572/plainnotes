"""Attachments: file-name rules, listing, upload, serving and deletion."""

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import fsutil, notes
from app.fsutil import DataDir

_UNSAFE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|\ud800-\udfff]')
_SEPARATORS_RE = re.compile(r"[/\\]")


# Shown in the browser with exactly this type; everything else is forced to download.
INLINE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogv": "video/ogg",
    "mov": "video/quicktime",
    "txt": "text/plain; charset=utf-8",
    "log": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
    "csv": "text/plain; charset=utf-8",
}
DOWNLOAD_TYPE = "application/octet-stream"


class UploadTooLarge(Exception):
    """The upload exceeds PLAINNOTES_MAX_UPLOAD_MB."""


@dataclass(frozen=True)
class AttachmentInfo:
    """One attachment: its file name, size in bytes and modification time."""

    name: str
    size: int
    modified: datetime


def _split_extension(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    return (name, "") if dot <= 0 else (name[:dot], name[dot:])


def _fit(name: str, suffix: str = "") -> str:
    """Insert suffix before the extension and shorten the stem until it fits in 255 bytes."""
    stem, extension = _split_extension(name)
    if len((suffix + extension).encode("utf-8")) > fsutil.MAX_NAME_BYTES - 4:
        stem, extension = name, ""
    candidate = stem + suffix + extension
    if len(candidate.encode("utf-8")) <= fsutil.MAX_NAME_BYTES:
        return candidate
    while len((stem + suffix + extension).encode("utf-8")) > fsutil.MAX_NAME_BYTES:
        stem = stem[:-1]
    if not extension and not suffix:
        stem = stem.rstrip(". ")
    return stem + suffix + extension


def sanitize_name(requested: str) -> str:
    """Steps 1-5 of the file-name rules: last path part, safe characters, trimmed, fits."""
    name = _SEPARATORS_RE.split(requested)[-1]
    name = _UNSAFE_CHARS_RE.sub("_", name)
    name = name.strip(". ") or "file"
    return _fit(name)


def is_final_name(name: str) -> bool:
    """True if name is already in final form (unchanged by sanitize_name)."""
    return sanitize_name(name) == name


def name_key(name: str) -> str:
    """Comparison key for names that must be unique ignoring case."""
    return unicodedata.normalize("NFC", name).casefold()


def unique_name(name: str, taken: set[str]) -> str:
    """Step 6: add " (1)", " (2)", … before the extension while the name is taken."""
    candidate = name
    number = 0
    while name_key(candidate) in taken:
        number += 1
        candidate = _fit(name, f" ({number})")
    return candidate


def content_type(name: str) -> tuple[str, bool]:
    """The Content-Type for a file name and whether it may be shown inline; by extension only."""
    extension = name.rpartition(".")[2].lower() if "." in name else ""
    if extension in INLINE_TYPES:
        return INLINE_TYPES[extension], True
    return DOWNLOAD_TYPE, False


def list_attachments(data_dir: DataDir, note_id: str) -> list[AttachmentInfo]:
    """The note's attachments sorted by name; raise NotFound if the note does not exist."""
    return attachments_of(notes.find_note(data_dir, note_id))


def attachments_of(ref: notes.NoteRef) -> list[AttachmentInfo]:
    """The attachments of a note that was already looked up."""
    try:
        folder = _folder(ref)
    except notes.NotFound:
        return []
    items = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not fsutil.is_utf8_name(entry.name) or not is_final_name(entry.name):
                    continue
                info = entry.stat(follow_symlinks=False)
                items.append(
                    AttachmentInfo(entry.name, info.st_size, notes.mtime_moment(info.st_mtime))
                )
    except OSError:
        return []
    return sorted(items, key=lambda item: (name_key(item.name), item.name))


def _folder(ref: notes.NoteRef, create: bool = False) -> Path:
    folder = ref.folder / notes.ATTACHMENTS_DIR
    if create and not os.path.lexists(folder):
        folder.mkdir(exist_ok=True)
    if not fsutil.is_real_dir(folder):
        raise notes.NotFound
    return folder


def find_attachment(data_dir: DataDir, note_id: str, name: str) -> tuple[Path, os.stat_result]:
    """The path and lstat of an existing regular attachment file named in final form."""
    folder = _folder(notes.find_note(data_dir, note_id))
    try:
        path = fsutil.safe_child(folder, name, is_final_name)
        info = path.lstat()
    except (fsutil.InvalidName, OSError):
        raise notes.NotFound from None
    if not stat.S_ISREG(info.st_mode):
        raise notes.NotFound
    return path, info


def delete_attachment(data_dir: DataDir, note_id: str, name: str) -> None:
    """Delete one attachment permanently."""
    with data_dir.lock:
        path, _ = find_attachment(data_dir, note_id, name)
        path.unlink()
        fsutil.fsync_dir(path.parent)


class Upload:
    """A temp file inside a note's attachments folder that receives an upload in chunks."""

    def __init__(self, data_dir: DataDir, note_id: str) -> None:
        self.note_id = note_id
        self.folder = _folder(notes.find_note(data_dir, note_id), create=True)
        fd, self.temp = fsutil.create_temp_file(self.folder)
        self.file = os.fdopen(fd, "wb")
        self.size = 0

    def write(self, chunk: bytes) -> None:
        """Append a chunk; disk-full errors become StorageFull."""
        try:
            self.file.write(chunk)
        except OSError as exc:
            fsutil.raise_if_full(exc)
            raise
        self.size += len(chunk)

    def discard(self) -> None:
        """Close and delete the temp file."""
        self.file.close()
        self.temp.unlink(missing_ok=True)

    def finish(self, data_dir: DataDir, requested_name: str) -> AttachmentInfo:
        """Move the temp file to a free final name; never overwrites an existing file."""
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
        except OSError as exc:
            fsutil.raise_if_full(exc)
            raise
        name = sanitize_name(requested_name)
        with data_dir.lock:
            if _folder(notes.find_note(data_dir, self.note_id)) != self.folder:
                raise notes.NotFound
            taken = {name_key(entry) for entry in os.listdir(self.folder)}
            while True:
                final = unique_name(name, taken)
                path = fsutil.safe_child(self.folder, final, is_final_name)
                try:
                    fsutil.reserve(path)
                except FileExistsError:
                    taken.add(name_key(final))
                    continue
                break
            try:
                os.replace(self.temp, path)
            except OSError:
                path.unlink(missing_ok=True)
                raise
            fsutil.fsync_dir(self.folder)
        info = path.lstat()
        return AttachmentInfo(final, info.st_size, notes.mtime_moment(info.st_mtime))
