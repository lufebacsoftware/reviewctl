"""Private local origin identity for a project journal."""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - journal support is POSIX-only
    fcntl = None

from reviewctl.config import PROJECT_ID
from reviewctl.contracts import exact_json_object
from reviewctl.errors import Diagnostic, JournalOperationError
from reviewctl.filesystem import confined_directory_descriptor

_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


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


def _unsupported_identity_confinement() -> JournalOperationError:
    return JournalOperationError(
        Diagnostic(
            "journal_unavailable",
            "this platform cannot confine project identity files",
            retryable=False,
        )
    )


def ensure_project_state_root(project_dir: Path, *, create: bool = True) -> Path | None:
    """Return a literal private .reviewctl directory without following a root symlink."""
    project = project_dir.expanduser().resolve()
    root = project / ".reviewctl"
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
        raise _unsupported_identity_confinement()
    flags = os.O_RDONLY | directory_flag | no_follow
    descriptor: int | None = None
    try:
        with confined_directory_descriptor(project, create=create) as project_descriptor:
            try:
                descriptor = os.open(root.name, flags, dir_fd=project_descriptor)
            except FileNotFoundError:
                if not create:
                    return None
                os.mkdir(root.name, mode=0o700, dir_fd=project_descriptor)
                descriptor = os.open(root.name, flags, dir_fd=project_descriptor)
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
                descriptor = None
    except FileNotFoundError as error:
        if not create:
            return None
        raise _unsafe_state_root(root) from error
    except JournalOperationError:
        raise
    except OSError as error:
        raise _unsafe_state_root(root) from error
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
        with self._root_descriptor() as root_descriptor:
            with self._identity_lock(root_descriptor):
                try:
                    identity = self._read(root_descriptor)
                except FileNotFoundError:
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
                        ) from None
                    self._write(identity, root_descriptor)
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
        with self._root_descriptor(create=False) as root_descriptor:
            try:
                return self._read(root_descriptor)
            except FileNotFoundError:
                return None

    @contextmanager
    def _root_descriptor(self, *, create: bool = True):
        root = self.root
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory_flag is None or not _OPEN_SUPPORTS_DIR_FD:
            raise _unsupported_identity_confinement()
        with ExitStack() as descriptors:
            try:
                project_descriptor = descriptors.enter_context(
                    confined_directory_descriptor(self.project_dir, create=create)
                )
            except OSError as error:
                raise _unsafe_state_root(root) from error
            try:
                try:
                    descriptor = os.open(
                        root.name,
                        os.O_RDONLY | directory_flag | no_follow,
                        dir_fd=project_descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise _unsafe_state_root(root) from None
                    os.mkdir(root.name, mode=0o700, dir_fd=project_descriptor)
                    descriptor = os.open(
                        root.name,
                        os.O_RDONLY | directory_flag | no_follow,
                        dir_fd=project_descriptor,
                    )
            except JournalOperationError:
                raise
            except OSError as error:
                raise _unsafe_state_root(root) from error
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise _unsafe_state_root(root)
                yield descriptor
                current = os.stat(
                    root.name,
                    dir_fd=project_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(current.st_mode) or (
                    current.st_dev,
                    current.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
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

    def _read(self, root_descriptor: int | None = None) -> ProjectIdentity:
        if root_descriptor is None:
            with self._root_descriptor() as owned_descriptor:
                return self._read(owned_descriptor)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("project identity is not a regular file")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                value = json.load(stream, object_pairs_hook=exact_json_object)
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            if isinstance(error, OSError) and error.errno == errno.ELOOP:
                raise _unsafe_state_path(self.path) from error
            raise JournalOperationError(
                Diagnostic("journal_corrupt", f"could not read project identity: {error}")
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
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
            or not PROJECT_ID.fullmatch(project_id)
            or not isinstance(origin_id, str)
            or not origin_id
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise JournalOperationError(
                Diagnostic("journal_corrupt", "project identity has an invalid schema")
            )
        return ProjectIdentity(project_id, origin_id, created_at, schema_version)

    def _write(self, identity: ProjectIdentity, root_descriptor: int | None = None) -> None:
        if root_descriptor is None:
            with self._root_descriptor() as owned_descriptor:
                self._write(identity, owned_descriptor)
            return
        payload = (
            json.dumps(identity.to_dict(), ensure_ascii=True, sort_keys=True, indent=2).encode(
                "utf-8"
            )
            + b"\n"
        )
        temporary = f"identity.{secrets.token_hex(16)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        staged_identity = None
        try:
            staged_identity = os.fstat(descriptor)
            if not stat.S_ISREG(staged_identity.st_mode):
                raise OSError("temporary identity is not a regular file")
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise OSError(f"could not finish writing project identity {self.path}")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.replace(
                temporary,
                self.path.name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
        except BaseException:
            try:
                candidate = os.stat(
                    temporary,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                if staged_identity is not None and (candidate.st_dev, candidate.st_ino) == (
                    staged_identity.st_dev,
                    staged_identity.st_ino,
                ):
                    os.unlink(temporary, dir_fd=root_descriptor)
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
    def _identity_lock(self, root_descriptor: int | None = None):
        if root_descriptor is None:
            with self._root_descriptor() as owned_descriptor:
                with self._identity_lock(owned_descriptor):
                    yield
            return
        if fcntl is None:
            yield
            return
        lock_path = self.root / "identity.lock"
        try:
            descriptor = os.open(
                lock_path.name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_descriptor,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("project identity lock is not a regular file")
        except OSError as error:
            raise _unsafe_state_path(lock_path) from error
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
