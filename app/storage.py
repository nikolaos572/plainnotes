"""The data folder layout and the checks that run once before the server starts.

PLAINNOTES_DATA_DIR holds two folders that plainnotes creates itself:

- notes/    every note folder and the trash
- testing/  used only by the storage check below; its contents are deleted at every start

The storage check runs only inside testing/, so it can never touch a note.
"""

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app import fsutil, notes
from app.config import ConfigError

NOTES_DIR = "notes"
TESTING_DIR = "testing"
_DENIED = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})

UNSUPPORTED_STORAGE = (
    "Saving is turned off: the storage that holds your notes does not support the safe file "
    "operations plainnotes needs ({missing}). Network shares (NFS, SMB) and some filesystems "
    "lack them. Your notes are shown read-only. Move the data folder to a local Linux "
    "filesystem such as ext4, XFS or btrfs, then restart plainnotes."
)


@dataclass(frozen=True)
class Prepared:
    """The outcome of prepare(): why saving is off (empty if it is on) and warnings to log."""

    read_only_reason: str = ""
    warnings: list[str] = field(default_factory=list)


def notes_dir(data_dir: Path) -> Path:
    """The folder that holds the notes."""
    return data_dir / NOTES_DIR


def testing_dir(data_dir: Path) -> Path:
    """The folder used only by the storage check."""
    return data_dir / TESTING_DIR


def _not_writable(path: Path) -> ConfigError:
    uid, gid = os.getuid(), os.getgid()
    return ConfigError(
        f"Data directory {path} is not writable by uid {uid}. "
        f"Fix it on the host with: sudo chown -R {uid}:{gid} <your data folder>"
    )


def _check_layout(data_dir: Path) -> list[str]:
    """Refuse the layout of earlier versions (notes directly in the data folder)."""
    try:
        with os.scandir(data_dir) as entries:
            names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    except OSError:
        raise ConfigError(f"PLAINNOTES_DATA_DIR: {data_dir} is not readable.") from None
    if any(
        is_dir and (notes.is_note_id(name) or name == notes.TRASH_DIR) for name, is_dir in names
    ):
        raise ConfigError(
            f"PLAINNOTES_DATA_DIR: {data_dir} holds note folders directly, as earlier versions "
            f"did. Notes now live in {notes_dir(data_dir)}. Stop plainnotes, move every note "
            f"folder and {notes.TRASH_DIR} into it, then start plainnotes again."
        )
    if any(name.endswith(".md") and not is_dir for name, is_dir in names):
        return [
            f"{data_dir} contains .md files, which are not imported from there. "
            f"Put files to import into {notes_dir(data_dir)}."
        ]
    return []


def _real_folder(path: Path, *, create: bool) -> None:
    if create and not os.path.lexists(path):
        path.mkdir()
    if not fsutil.is_real_dir(path):
        raise ConfigError(f"{path} must be a folder, not a file or a link.")


def _empty(folder: Path) -> None:
    """Delete everything inside folder. Links are removed, never followed."""
    with os.scandir(folder) as entries:
        for entry in list(entries):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)


def prepare(data_dir: Path, *, read_only: bool = False) -> Prepared:
    """Check the data folder and create its layout; raise ConfigError if it cannot be used."""
    if not data_dir.is_dir():
        raise ConfigError(f"PLAINNOTES_DATA_DIR: {data_dir} does not exist or is not a directory.")
    warnings = _check_layout(data_dir)
    notes_path, testing_path = notes_dir(data_dir), testing_dir(data_dir)
    if read_only:
        # Nothing is created, emptied or probed: the data folder may be a read-only mount.
        if not fsutil.is_real_dir(notes_path):
            raise ConfigError(
                f"PLAINNOTES_DATA_DIR: {notes_path} does not exist; read-only mode needs it."
            )
        try:
            with os.scandir(notes_path) as entries:
                next(entries, None)
            if not os.access(notes_path, os.R_OK | os.X_OK):
                raise PermissionError
        except OSError:
            raise ConfigError(f"PLAINNOTES_DATA_DIR: {notes_path} is not readable.") from None
        return Prepared(warnings=warnings)
    try:
        _real_folder(notes_path, create=True)
        _real_folder(testing_path, create=True)
        _empty(testing_path)
        missing = fsutil.missing_atomic_renames(testing_path)
        if not os.access(notes_path, os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, "not writable")
    except fsutil.StorageFull:
        raise ConfigError(f"PLAINNOTES_DATA_DIR: the disk that holds {data_dir} is full.") from None
    except OSError as exc:
        if exc.errno in _DENIED:
            raise _not_writable(data_dir) from None
        raise ConfigError(
            f"PLAINNOTES_DATA_DIR: {data_dir} cannot be prepared: {exc.strerror}."
        ) from None
    if os.stat(notes_path).st_dev != os.stat(testing_path).st_dev:
        warnings.append(
            f"{notes_path} and {testing_path} are on different filesystems, so the storage "
            "check may not apply to your notes. Mount the whole data folder at once."
        )
    if missing:
        return Prepared(UNSUPPORTED_STORAGE.format(missing=", ".join(missing)), warnings)
    return Prepared(warnings=warnings)
