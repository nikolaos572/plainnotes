"""Safe path helpers, symlink checks, atomic writes and temp files.

Every path derived from user input is built with safe_child(); nothing here follows symlinks.
"""

import ctypes
import errno
import logging
import os
import secrets
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
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
# What renameat2 reports when the filesystem (or libc) lacks a flag.
_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP})


class InvalidName(ValueError):
    """A name from user input failed validation and must not be used as a path."""


class StorageFull(Exception):
    """The disk (or quota) is full."""


@dataclass
class DataDir:
    """The notes folder plus the per-app lock and the set of warnings already logged."""

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
    """Replace target with data so that a crash leaves either the old or the new file.

    The new content goes to a hidden temp file next to target first. If the process dies
    before the rename, that file stays behind; nothing ever deletes it automatically.
    """
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


def _renameat2(first: Path, second: Path, flags: int) -> None:
    """Call Linux renameat2; never emulate atomic flags with multiple operations."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, "Atomic rename is unavailable on this platform") from None
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(_AT_FDCWD, os.fsencode(first), _AT_FDCWD, os.fsencode(second), flags) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def exchange_files(first: Path, second: Path) -> None:
    """Atomically exchange two existing paths on Linux."""
    _renameat2(first, second, _RENAME_EXCHANGE)


def rename_no_replace(source: Path, target: Path) -> None:
    """Atomically publish source only if target does not exist, including dangling symlinks."""
    _renameat2(source, target, _RENAME_NOREPLACE)


def missing_atomic_renames(folder: Path) -> list[str]:
    """Try the atomic renames that saves rely on; return the names of unsupported ones.

    Only files this function creates exclusively in folder are used, and only they are removed,
    each by its exact name. Errors other than "not supported" are raised.
    """
    created: list[Path] = []

    def new_file(data: bytes) -> Path:
        fd, path = create_temp_file(folder, mode=0o600)
        created.append(path)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        return path

    missing = []
    try:
        first, second = new_file(b"first"), new_file(b"second")
        try:
            exchange_files(first, second)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_ERRNOS:
                raise
            missing.append("RENAME_EXCHANGE")
        else:
            if read_file(first) != b"second" or read_file(second) != b"first":
                missing.append("RENAME_EXCHANGE")
        try:
            rename_no_replace(first, second)
        except FileExistsError:
            # The existing file was protected, as it must be. Now publish to a free name.
            target = folder / _temp_name()
            rename_no_replace(first, target)
            created.append(target)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_ERRNOS:
                raise
            missing.append("RENAME_NOREPLACE")
        else:
            missing.append("RENAME_NOREPLACE")  # It replaced an existing file.
    finally:
        for path in created:
            path.unlink(missing_ok=True)
    return missing


def replace_with_history(target: Path, data: bytes, check_current: Callable[[], None]) -> None:
    """Publish data while retaining the exact displaced inode in .history.

    Both sides exist before exchange, so an interruption leaves both versions recoverable.
    Failed attempts may leave a proposed version in history.
    """
    history = target.parent / ".history"
    history.mkdir(mode=0o700, exist_ok=True)
    if not is_real_dir(history):
        raise OSError(errno.EINVAL, "History is not a real directory")
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
