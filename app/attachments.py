"""Attachments: file-name rules, listing, upload, serving and deletion."""

import base64
import binascii
import hashlib
import logging
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import fsutil, notes
from app.fsutil import DataDir

logger = logging.getLogger(__name__)

_UNSAFE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|\ud800-\udfff]')
_SEPARATORS_RE = re.compile(r"[/\\]")
# A Structured Field byte sequence (RFC 8941): base64 between colons.
_BYTE_SEQUENCE_RE = re.compile(r":([A-Za-z0-9+/=]*):")
SHA256_BYTES = 32


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


class DigestRequired(Exception):
    """PLAINNOTES_CHECK_ATTACHMENTS_HASH is on, but the upload carries no SHA-256."""


class InvalidDigest(Exception):
    """The upload's Content-Digest header has a sha-256 entry that is not a valid SHA-256."""


class DigestMismatch(Exception):
    """The bytes received do not have the SHA-256 the client sent; nothing was saved."""


def expected_sha256(header: str) -> bytes:
    """The SHA-256 in a Content-Digest header (RFC 9530), such as "sha-256=:<base64>:".

    Other algorithms in the header are ignored. Raise DigestRequired if it has no sha-256
    entry and InvalidDigest if that entry is not 32 bytes in base64.
    """
    for member in header.split(","):
        key, _, value = member.strip().partition("=")
        if key != "sha-256":
            continue
        match = _BYTE_SEQUENCE_RE.fullmatch(value.split(";", 1)[0])
        if match is None:
            raise InvalidDigest
        try:
            digest = base64.b64decode(match[1], validate=True)
        except binascii.Error:
            raise InvalidDigest from None
        if len(digest) != SHA256_BYTES:
            raise InvalidDigest
        return digest
    raise DigestRequired


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
    if create:
        # Also sync on retry: a previous mkdir may have succeeded before its sync failed.
        fsutil.fsync_dir(ref.folder)
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
    """A temp file inside a note's attachments folder that receives an upload in chunks.

    With expected_sha256, every chunk is also hashed, and finish() publishes the file only if
    the received bytes have that SHA-256.
    """

    def __init__(
        self, data_dir: DataDir, note_id: str, expected_sha256: bytes | None = None
    ) -> None:
        self.note_id = note_id
        # Locked, so the note cannot move to the trash between the lookup and the create.
        with data_dir.lock:
            self.folder = _folder(notes.find_note(data_dir, note_id), create=True)
            fd, self.temp = fsutil.create_temp_file(self.folder)
        self.file = os.fdopen(fd, "wb")
        self.size = 0
        self.expected_sha256 = expected_sha256
        self._hash = hashlib.sha256() if expected_sha256 is not None else None

    def write(self, chunk: bytes) -> None:
        """Append a chunk; disk-full errors become StorageFull."""
        try:
            self.file.write(chunk)
        except OSError as exc:
            fsutil.raise_if_full(exc)
            raise
        self.size += len(chunk)
        if self._hash is not None:
            self._hash.update(chunk)

    def discard(self) -> None:
        """Close and delete the temp file."""
        try:
            self.file.close()
        except OSError as exc:
            fsutil.raise_if_full(exc)
            raise
        finally:
            self.temp.unlink(missing_ok=True)

    def finish(self, data_dir: DataDir, requested_name: str) -> AttachmentInfo:
        """Move the temp file to a free final name; never overwrites an existing file.

        Raise DigestMismatch, before anything is published, if the SHA-256 check fails; the
        caller then discards the temp file.
        """
        if self._hash is not None and self._hash.digest() != self.expected_sha256:
            logger.warning(
                "Upload to note %s failed the SHA-256 check after %d bytes; it was not saved",
                self.note_id,
                self.size,
            )
            raise DigestMismatch
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
                    fsutil.rename_no_replace(self.temp, path)
                except FileExistsError:
                    taken.add(name_key(final))
                    continue
                except OSError as exc:
                    fsutil.raise_if_full(exc)
                    raise
                break
            fsutil.fsync_dir(self.folder)
        info = path.lstat()
        return AttachmentInfo(final, info.st_size, notes.mtime_moment(info.st_mtime))
