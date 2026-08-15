"""Private, symlink-safe local file primitives used by the compatibility CLI."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class PrivatePathError(RuntimeError):
    """Raised when a secret-bearing local path is unsafe to access."""


def _owner_is_current(stat_result: os.stat_result) -> bool:
    get_euid = getattr(os, "geteuid", None)
    return get_euid is None or stat_result.st_uid == get_euid()


def _validate_stat(path: Path, stat_result: os.stat_result) -> None:
    if not stat.S_ISREG(stat_result.st_mode):
        raise PrivatePathError(f"Unsafe private path (not a regular file): {path}")
    if not _owner_is_current(stat_result):
        raise PrivatePathError(f"Unsafe private path (wrong owner): {path}")
    if stat_result.st_nlink != 1:
        raise PrivatePathError(f"Unsafe private path (multiple hard links): {path}")


def ensure_private_directory(
    path: str | os.PathLike[str], *, harden_existing: bool
) -> Path:
    """Create a private directory and optionally harden an existing one.

    ``harden_existing`` is reserved for CLI-managed directories. Custom
    ``--config`` parents must never have their permissions changed.
    """

    directory = Path(path).expanduser()
    existed = directory.exists() or directory.is_symlink()
    if existed:
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PrivatePathError(
                f"Unsafe private directory (symbolic link): {directory}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise PrivatePathError(
                f"Unsafe private directory (not a directory): {directory}"
            )
        if harden_existing and not _owner_is_current(info):
            raise PrivatePathError(
                f"Unsafe private directory (wrong owner): {directory}"
            )
    else:
        directory.mkdir(parents=True, mode=PRIVATE_DIR_MODE)

    if harden_existing or not existed:
        directory.chmod(PRIVATE_DIR_MODE)
    return directory


def _open_flags(flags: int) -> int:
    return flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def ensure_private_file(path: str | os.PathLike[str], *, create: bool = True) -> Path:
    """Validate a private file and enforce owner-only permissions."""

    file_path = Path(path).expanduser()
    if file_path.is_symlink():
        raise PrivatePathError(f"Unsafe private path (symbolic link): {file_path}")

    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(file_path, _open_flags(flags), PRIVATE_FILE_MODE)
    except OSError as error:
        raise PrivatePathError(
            f"Unable to open private path safely: {file_path}"
        ) from error
    try:
        info = os.fstat(descriptor)
        _validate_stat(file_path, info)
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    return file_path


def read_private_text(path: str | os.PathLike[str]) -> str:
    file_path = Path(path).expanduser()
    if file_path.is_symlink():
        raise PrivatePathError(f"Unsafe private path (symbolic link): {file_path}")
    try:
        descriptor = os.open(file_path, _open_flags(os.O_RDONLY))
    except OSError as error:
        raise PrivatePathError(
            f"Unable to read private path safely: {file_path}"
        ) from error
    try:
        _validate_stat(file_path, os.fstat(descriptor))
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_private_text(path: str | os.PathLike[str], data: str) -> None:
    """Atomically replace ``path`` with UTF-8 text using private permissions."""

    file_path = Path(path).expanduser()
    ensure_private_file(file_path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=file_path.parent,
        prefix=f".{file_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, file_path)
        replaced = True
        ensure_private_file(file_path, create=False)
        _fsync_directory(file_path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def append_private_text(path: str | os.PathLike[str], data: str) -> None:
    """Append one caller-locked record to a private file."""

    file_path = Path(path).expanduser()
    if file_path.is_symlink():
        raise PrivatePathError(f"Unsafe private path (symbolic link): {file_path}")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    try:
        descriptor = os.open(file_path, _open_flags(flags), PRIVATE_FILE_MODE)
    except OSError as error:
        raise PrivatePathError(
            f"Unable to append private path safely: {file_path}"
        ) from error
    try:
        _validate_stat(file_path, os.fstat(descriptor))
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        payload = data.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
