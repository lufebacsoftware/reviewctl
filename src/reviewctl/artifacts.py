"""Private, collision-safe review artifact storage."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from reviewctl.identity import confine_project_state_path

_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


class ArtifactStore:
    """Write raw review artifacts below one private directory."""

    def __init__(self, root: Path) -> None:
        self.root = confine_project_state_path(root)
        self._anchor = self.root.parent
        self._root_parts = (self.root.name,)
        for candidate in (self.root, *self.root.parents):
            if candidate.name == ".reviewctl":
                self._anchor = candidate
                self._root_parts = self.root.relative_to(candidate).parts
                break
        with self._directory_descriptor(()):
            pass

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
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
            raise OSError("this platform cannot confine review artifacts")
        flags = os.O_RDONLY | directory_flag | no_follow
        with ExitStack() as descriptors:
            current = os.open(self._anchor, flags)
            descriptors.callback(os.close, current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise OSError("artifact anchor is not a directory")
            for component in (*self._root_parts, *parts):
                try:
                    child = os.open(component, flags, dir_fd=current)
                except FileNotFoundError:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                    child = os.open(component, flags, dir_fd=current)
                descriptors.callback(os.close, child)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise OSError("artifact path component is not a directory")
                current = child
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
