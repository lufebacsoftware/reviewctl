"""Private local origin identity for a project journal."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reviewctl.errors import Diagnostic, JournalOperationError


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    origin_id: str
    created_at: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "projectId": self.project_id,
            "originId": self.origin_id,
            "createdAt": self.created_at,
        }


class ProjectIdentityStore:
    """Create and validate the machine-local identity without changing it silently."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.expanduser().resolve()
        self.root = self.project_dir / ".reviewctl"
        self.path = self.root / "identity.json"

    def ensure(self, project_id: str) -> ProjectIdentity:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if self.path.exists():
            identity = self._read()
            if identity.project_id != project_id:
                raise JournalOperationError(
                    Diagnostic(
                        "journal_corrupt",
                        "project identity does not match the configured project id",
                        next=(
                            "restore the original project.id or migrate the journal "
                            "explicitly before changing its portable identity"
                        ),
                    )
                )
            return identity

        identity = ProjectIdentity(
            project_id=project_id,
            origin_id="origin-" + secrets.token_hex(12),
            created_at=_now(),
        )
        self._write(identity)
        return identity

    def read_existing(self) -> ProjectIdentity | None:
        """Read an existing identity without creating or modifying local state."""
        if not self.path.exists():
            return None
        return self._read()

    def _read(self) -> ProjectIdentity:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise JournalOperationError(
                Diagnostic("journal_corrupt", f"could not read project identity: {error}")
            ) from error
        if not isinstance(value, dict):
            raise JournalOperationError(
                Diagnostic("journal_corrupt", "project identity must be a JSON object")
            )
        schema_version = value.get("schemaVersion")
        project_id = value.get("projectId")
        origin_id = value.get("originId")
        created_at = value.get("createdAt")
        if (
            schema_version != 1
            or not isinstance(project_id, str)
            or not project_id
            or not isinstance(origin_id, str)
            or not origin_id
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise JournalOperationError(
                Diagnostic("journal_corrupt", "project identity has an invalid schema")
            )
        return ProjectIdentity(project_id, origin_id, created_at, schema_version)

    def _write(self, identity: ProjectIdentity) -> None:
        payload = json.dumps(
            identity.to_dict(), ensure_ascii=True, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        descriptor, temporary = tempfile.mkstemp(prefix="identity.", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
