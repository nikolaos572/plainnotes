"""Safe path helpers, symlink checks, atomic writes and temp files.

Every path derived from user input is built with safe_child(); nothing here follows symlinks.
"""

import ctypes
import errno
import logging
import os
import secrets
import shutil
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

TEMP_PREFIX = ".plainnotes-tmp-"
MAX_NAME_BYTES = 255
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


class InvalidName(ValueError):
    """A name from user input failed validation and must not be used as a path."""


class StorageFull(Exception):
    """The disk (or quota) is full."""


@dataclass
class DataDir:
    """The data folder plus the per-app lock and the set of warnings already logged."""

    root: Path
    read_only: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    _warned: set[str] = field(default_factory=set)
    _warned_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        """Bind the administrator-configured root once; children still reject symlinks."""
        self.root = self.root.resolve(strict=True)

    def warn_once(self, logger: logging.Logger, message: str, *args: object) -> None:
        """Log a warning the first time this exact message occurs in this process."""
        key = message % args
        with self._warned_lock:
            if key in self._warned:
                return
            self._warned.add(key)
        logger.warning(message, *args)


def is_utf8_name(name: str) -> bool:
    """True if a name read from disk decodes as UTF-8 (no surrogate escapes)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def safe_child(parent: Path, name: str, is_valid: Callable[[str], bool]) -> Path:
    """Validate one path component from user input and join it to a known parent folder."""
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or not is_utf8_name(name)
        or len(name.encode("utf-8")) > MAX_NAME_BYTES
        or not is_valid(name)
    ):
        raise InvalidName
    return parent / name


def _lstat_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except OSError:
        return None


def is_real_dir(path: Path) -> bool:
    """True if path is a directory and not a symlink."""
    mode = _lstat_mode(path)
    return mode is not None and stat.S_ISDIR(mode)


def is_real_file(path: Path) -> bool:
    """True if path is a regular file and not a symlink."""
    mode = _lstat_mode(path)
    return mode is not None and stat.S_ISREG(mode)


def read_file(path: Path) -> bytes:
    """Read a regular file without following symlinks; raise OSError otherwise."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        return file.read()


def raise_if_full(exc: OSError) -> None:
    """Turn a disk-full OSError into StorageFull; return for any other error."""
    if exc.errno in _FULL_ERRNOS:
        raise StorageFull from exc


def _temp_name() -> str:
    return TEMP_PREFIX + secrets.token_hex(8)


def create_temp_file(directory: Path, *, mode: int = 0o666) -> tuple[int, Path]:
    """Create a new, empty temp file with an exclusive create; return its fd and path."""
    while True:
        path = directory / _temp_name()
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
            return os.open(path, flags, mode), path
        except FileExistsError:
            continue


def reserve(path: Path) -> None:
    """Create an empty file with an exclusive create; raise FileExistsError if path exists."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o666))


def create_temp_dir(parent: Path) -> Path:
    """Create a new, empty temp folder inside parent and return its path."""
    while True:
        path = parent / _temp_name()
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path


def fsync_dir(path: Path) -> None:
    """Flush a folder's entries to disk; ignored where the filesystem does not support it."""
    fd = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}:
                raise_if_full(exc)
                raise
    finally:
        os.close(fd)


def fsync_file(path: Path) -> None:
    """Flush an imported regular file without following symlinks."""
    fd = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW | _NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(target: Path, data: bytes, *, mode: int = 0o666) -> None:
    """Replace target with data so that a crash leaves either the old or the new file."""
    fd, temp = create_temp_file(target.parent, mode=mode)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            target_mode = _lstat_mode(target)
            if target_mode is not None and stat.S_ISREG(target_mode):
                os.fchmod(file.fileno(), stat.S_IMODE(target_mode))
        os.replace(temp, target)
    except BaseException as exc:
        temp.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise_if_full(exc)
        raise
    fsync_dir(target.parent)


def remove_temp_entries(folder: Path, *, preserve_dirs: bool = False) -> int:
    """Delete leftover temp files and folders directly inside folder; return how many."""
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(TEMP_PREFIX):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                if preserve_dirs:
                    os.rmdir(entry.path)  # Only empty legacy staging directories are disposable.
                else:
                    shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


def exchange_files(first: Path, second: Path) -> None:
    """Atomically exchange two existing paths on Linux; never fall back to replacement."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        exchange = libc.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, "Atomic exchange is unavailable on this platform") from None
    exchange.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def replace_with_history(target: Path, data: bytes, check_current: Callable[[], None]) -> None:
    """Publish data while retaining the exact displaced inode in .history.

    Both sides exist before exchange, so an interruption leaves both versions recoverable.
    Failed attempts may leave a proposed version in history; cleanup must never remove it.
    """
    history = target.parent / ".history"
    history.mkdir(mode=0o700, exist_ok=True)
    if not is_real_dir(history):
        raise OSError(errno.EINVAL, "History is not a real directory")
    # Also protect recovery artifacts retained by earlier releases.
    history.chmod(0o700)
    fsync_dir(target.parent)
    version = history / secrets.token_hex(16)
    version.mkdir(mode=0o700)
    fsync_dir(history)
    previous = version / target.name
    atomic_write(previous, data, mode=0o600)
    mode = target.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "Note is not a regular file")
    previous.chmod(stat.S_IMODE(mode))
    fsync_file(previous)
    check_current()
    exchange_files(previous, target)
    fsync_file(previous)
    fsync_dir(version)
    fsync_dir(target.parent)
