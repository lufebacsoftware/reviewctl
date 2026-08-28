"""Descriptor-confined filesystem access for review evidence and project state."""

from __future__ import annotations

import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


def _close_descriptors(descriptors: list[int]) -> None:
    primary = sys.exc_info()[1]
    close_error: BaseException | None = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except BaseException as error:
            if primary is None and close_error is None:
                close_error = error
    if primary is None and close_error is not None:
        raise close_error


@contextmanager
def confined_directory_descriptor(path: Path, *, create: bool = False):
    """Open an absolute directory without following any pathname component."""
    absolute = Path(os.path.abspath(path.expanduser()))
    anchor = Path(absolute.anchor)
    parts = absolute.relative_to(anchor).parts
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    flags = os.O_RDONLY | directory_flag | no_follow
    descriptors: list[int] = []
    try:
        current = os.open(anchor, flags)
        descriptors.append(current)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("filesystem anchor is not a directory")
        for component in parts:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise OSError("filesystem path component is not a directory")
            current = child
        yield current
    finally:
        _close_descriptors(descriptors)


@contextmanager
def confined_relative_directory_descriptor(
    parent_descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
):
    """Walk relative directory components from an already validated descriptor."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    flags = os.O_RDONLY | directory_flag | no_follow
    descriptors = [os.dup(parent_descriptor)]
    current = descriptors[0]
    try:
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("filesystem parent descriptor is not a directory")
        for component in parts:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise OSError("filesystem path component is not a directory")
            current = child
        yield current
    finally:
        _close_descriptors(descriptors)


@contextmanager
def confined_relative_regular_descriptor(
    parent_descriptor: int,
    path: Path,
    flags: int,
    mode: int = 0o600,
):
    """Open a relative regular file below an already validated directory descriptor."""
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ValueError("filesystem path must be a confined relative path")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    with confined_relative_directory_descriptor(parent_descriptor, path.parts[:-1]) as parent:
        descriptor = os.open(
            path.parts[-1],
            flags | no_follow | getattr(os, "O_NONBLOCK", 0),
            mode,
            dir_fd=parent,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("filesystem path is not a regular file")
            yield descriptor
        finally:
            primary = sys.exc_info()[1]
            try:
                os.close(descriptor)
            except BaseException as close_error:
                if primary is not None:
                    raise primary from close_error
                raise


@contextmanager
def confined_regular_descriptor(path: Path, flags: int, mode: int = 0o600):
    """Open a regular file relative to a fully confined parent directory."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None or not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot confine filesystem paths")
    with confined_directory_descriptor(path.parent) as parent:
        descriptor = os.open(
            path.name,
            flags | no_follow | getattr(os, "O_NONBLOCK", 0),
            mode,
            dir_fd=parent,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("filesystem path is not a regular file")
            yield descriptor
        finally:
            primary = sys.exc_info()[1]
            try:
                os.close(descriptor)
            except BaseException as close_error:
                if primary is not None:
                    raise primary from close_error
                raise


def read_confined_bytes(path: Path) -> bytes:
    """Read bytes from a confined regular-file descriptor."""
    with confined_regular_descriptor(path, os.O_RDONLY) as descriptor:
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read()


def read_confined_text(path: Path) -> str:
    """Read UTF-8 text from a confined regular-file descriptor."""
    return read_confined_bytes(path).decode("utf-8")
