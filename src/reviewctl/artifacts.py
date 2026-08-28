"""Private, collision-safe review artifact storage."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from reviewctl.filesystem import (
    confined_directory_descriptor,
    confined_relative_directory_descriptor,
)
from reviewctl.identity import confine_project_state_path

_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


class ArtifactStore:
    """Write raw review artifacts below one private directory."""

    def __init__(self, root: Path) -> None:
        self.root = confine_project_state_path(root)
        self._root_identity: tuple[int, int] | None = None
        with self._directory_descriptor(()) as descriptor:
            metadata = os.fstat(descriptor)
            self._root_identity = (metadata.st_dev, metadata.st_ino)

    @staticmethod
    def _artifact_parts(name: str) -> tuple[str, ...]:
        candidate = Path(name)
        if candidate.is_absolute():
            raise ValueError("artifact path must remain below the artifact root")
        parts = candidate.parts
        if not parts or any(part in {".", ".."} for part in parts):
            raise ValueError("artifact path escapes the artifact root")
        return parts

    @contextmanager
    def _directory_descriptor(self, parts: tuple[str, ...]):
        if not _OPEN_SUPPORTS_DIR_FD:
            raise OSError("this platform cannot confine review artifacts")
        with confined_directory_descriptor(self.root, create=True) as root:
            metadata = os.fstat(root)
            identity = (metadata.st_dev, metadata.st_ino)
            if self._root_identity is not None and identity != self._root_identity:
                raise OSError("artifact root identity changed")
            with confined_relative_directory_descriptor(root, parts, create=True) as current:
                os.fchmod(current, 0o700)
                yield current

    def write_bytes(self, name: str, contents: bytes) -> Path:
        parts = self._artifact_parts(name)
        target = self.root.joinpath(*parts)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        with self._directory_descriptor(parts[:-1]) as parent:
            descriptor = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=parent,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("artifact is not a regular file")
                os.fchmod(descriptor, 0o600)
                view = memoryview(contents)
                while view:
                    written = os.write(descriptor, view)
                    if written == 0:
                        raise OSError(f"could not finish writing artifact {target}")
                    view = view[written:]
            finally:
                os.close(descriptor)
        return target

    def write_text(self, name: str, contents: str) -> Path:
        return self.write_bytes(name, contents.encode("utf-8"))
