"""Private local origin identity for a project journal."""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - journal support is POSIX-only
    fcntl = None

from reviewctl.errors import Diagnostic, JournalOperationError


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unsafe_state_root(root: Path) -> JournalOperationError:
    return JournalOperationError(
        Diagnostic(
            "journal_corrupt",
            f"project state root is unsafe: {root}",
            next="replace .reviewctl with a private directory inside the project",
        )
    )


def _unsafe_state_path(path: Path) -> JournalOperationError:
    return JournalOperationError(
        Diagnostic(
            "journal_corrupt",
            f"project state path is unsafe: {path}",
            next="replace symlinks below .reviewctl with private project-local paths",
        )
    )


def ensure_project_state_root(project_dir: Path, *, create: bool = True) -> Path | None:
    """Return a literal private .reviewctl directory without following a root symlink."""
    project = project_dir.expanduser().resolve()
    root = project / ".reviewctl"
    if root.is_symlink():
        raise _unsafe_state_root(root)
    if not root.exists():
        if not create:
            return None
        root.mkdir(mode=0o700)
    if not root.is_dir():
        raise _unsafe_state_root(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(root, flags)
    except OSError as error:
        raise _unsafe_state_root(root) from error
    try:
        if create:
            os.fchmod(descriptor, 0o700)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _unsafe_state_root(root)
    finally:
        primary = sys.exc_info()[1]
        if primary is None:
            os.close(descriptor)
        else:
            try:
                os.close(descriptor)
            except BaseException:
                pass
    return root


def confine_project_state_path(path: Path, *, create_root: bool = True) -> Path:
    """Validate a literal .reviewctl ancestor before resolving a state path."""
    absolute = Path(os.path.abspath(path.expanduser()))
    for candidate in (absolute, *absolute.parents):
        if candidate.name == ".reviewctl":
            root = ensure_project_state_root(candidate.parent, create=create_root)
            if root is None:
                raise _unsafe_state_path(absolute)
            confined = root
            for part in absolute.relative_to(candidate).parts:
                confined /= part
                if confined.is_symlink():
                    raise _unsafe_state_path(confined)
            return confined
    return absolute.resolve()


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
        ensure_project_state_root(self.project_dir)
        confine_project_state_path(self.path)
        with self._identity_lock():
            if self.path.exists():
                identity = self._read()
            else:
                identity = ProjectIdentity(
                    project_id=project_id,
                    origin_id="origin-" + secrets.token_hex(12),
                    created_at=_now(),
                )
                if fcntl is None:
                    raise JournalOperationError(
                        Diagnostic(
                            "journal_unavailable",
                            "this platform has no supported project identity lock primitive",
                            retryable=True,
                            next="run reviewctl on a POSIX filesystem with advisory locking",
                        )
                    )
                self._write(identity)
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

    def read_existing(self) -> ProjectIdentity | None:
        """Read an existing identity without creating or modifying local state."""
        if ensure_project_state_root(self.project_dir, create=False) is None:
            return None
        confine_project_state_path(self.path, create_root=False)
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
            type(schema_version) is not int
            or schema_version != 1
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
        payload = (
            json.dumps(identity.to_dict(), ensure_ascii=True, sort_keys=True, indent=2).encode(
                "utf-8"
            )
            + b"\n"
        )
        descriptor, temporary = tempfile.mkstemp(prefix="identity.", dir=self.root)
        staged_identity = None
        try:
            staged_identity = os.fstat(descriptor)
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError(f"could not finish writing project identity {self.path}")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                candidate = os.stat(temporary, follow_symlinks=False)
                if staged_identity is not None and (candidate.st_dev, candidate.st_ino) == (
                    staged_identity.st_dev,
                    staged_identity.st_ino,
                ):
                    os.unlink(temporary)
            except BaseException:
                pass
            raise
        finally:
            primary = sys.exc_info()[1]
            if primary is None:
                os.close(descriptor)
            else:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass

    @contextmanager
    def _identity_lock(self):
        if fcntl is None:
            yield
            return
        lock_path = self.root / "identity.lock"
        if lock_path.is_symlink():
            raise _unsafe_state_path(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        primary: BaseException | None = None
        unlock_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            try:
                os.fchmod(descriptor, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except OSError as error:
                    raise JournalOperationError(
                        Diagnostic(
                            "journal_unavailable",
                            f"could not lock project identity: {error}",
                            retryable=True,
                            next="check filesystem locking and retry",
                        )
                    ) from error
                acquired = True
                try:
                    yield
                except BaseException as error:
                    primary = error
            except BaseException as error:
                primary = error
        finally:
            try:
                if acquired:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except BaseException as error:
                        unlock_error = error
            finally:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    close_error = error
        if primary is not None:
            raise primary
        if unlock_error is not None:
            raise unlock_error
        if close_error is not None:
            raise close_error
