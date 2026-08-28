"""Private, collision-safe review artifact storage."""

from __future__ import annotations

import os
from pathlib import Path

from reviewctl.identity import confine_project_state_path


class ArtifactStore:
    """Write raw review artifacts below one private directory."""

    def __init__(self, root: Path) -> None:
        self.root = confine_project_state_path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _target(self, name: str) -> Path:
        candidate = Path(name)
        if candidate.is_absolute():
            raise ValueError("artifact path must remain below the artifact root")
        target = (self.root / candidate).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError("artifact path escapes the artifact root") from error
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        return target

    def write_bytes(self, name: str, contents: bytes) -> Path:
        target = self._target(name)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
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
