from __future__ import annotations

import json
from pathlib import Path

import pytest

import reviewctl.identity as identity_module
from reviewctl.errors import JournalOperationError
from reviewctl.identity import ProjectIdentity, ProjectIdentityStore


def test_identity_store_creates_reuses_and_serializes_identity(tmp_path: Path) -> None:
    store = ProjectIdentityStore(tmp_path)
    created = store.ensure("project-one")
    assert created.project_id == "project-one"
    assert created.to_dict()["schemaVersion"] == 1
    assert store.read_existing() == created
    assert store.ensure("project-one") == created


def test_identity_store_rejects_project_mismatch(tmp_path: Path) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.ensure("project-one")
    with pytest.raises(JournalOperationError, match="does not match"):
        store.ensure("project-two")


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"schemaVersion": 2},
        {"schemaVersion": 1, "projectId": "", "originId": "o", "createdAt": "t"},
        {"schemaVersion": 1, "projectId": "p", "originId": "", "createdAt": "t"},
    ],
)
def test_identity_store_rejects_malformed_schema(tmp_path: Path, value: object) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path.write_text(json.dumps(value))
    with pytest.raises(JournalOperationError, match="identity"):
        store.read_existing()


def test_identity_store_rejects_malformed_json_and_absent_is_none(tmp_path: Path) -> None:
    store = ProjectIdentityStore(tmp_path)
    assert store.read_existing() is None
    store.root.mkdir(parents=True)
    store.path.write_text("not-json")
    with pytest.raises(JournalOperationError, match="read project identity"):
        store.read_existing()


def test_identity_store_handles_missing_lock_primitive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(identity_module, "fcntl", None)
    with pytest.raises(JournalOperationError, match="lock primitive"):
        ProjectIdentityStore(tmp_path).ensure("project-one")
    with ProjectIdentityStore(tmp_path)._identity_lock():
        pass


def test_identity_store_lock_failure_and_unlock_failure(tmp_path: Path, monkeypatch) -> None:
    store = ProjectIdentityStore(tmp_path)
    calls = []

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(descriptor, mode):
            calls.append(mode)
            if mode == FakeFcntl.LOCK_EX:
                raise OSError("lock failed")

    monkeypatch.setattr(identity_module, "fcntl", FakeFcntl)
    with pytest.raises(JournalOperationError, match="could not lock"):
        store.ensure("project-one")
    assert calls == [FakeFcntl.LOCK_EX]

    closed: list[int] = []
    original_close = identity_module.os.close

    class UnlockFailure(FakeFcntl):
        @staticmethod
        def flock(descriptor, mode):
            if mode == UnlockFailure.LOCK_UN:
                raise OSError("unlock failed")

    monkeypatch.setattr(identity_module, "fcntl", UnlockFailure)

    def track_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(identity_module.os, "close", track_close)
    with pytest.raises(OSError, match="unlock failed"):
        with store._identity_lock():
            pass
    assert closed


def test_identity_store_cleans_source_after_replace_failure(tmp_path: Path, monkeypatch) -> None:
    store = ProjectIdentityStore(tmp_path)
    original_replace = identity_module.os.replace
    temporary_paths: list[str] = []

    def fail_replace(temporary, target):
        temporary_paths.append(temporary)
        raise OSError("replace failed")

    monkeypatch.setattr(identity_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.ensure("project-one")
    assert temporary_paths
    assert not Path(temporary_paths[0]).exists()
    monkeypatch.setattr(identity_module.os, "replace", original_replace)


@pytest.mark.parametrize("operation", ["fchmod", "write", "fsync", "close", "replace", "chmod"])
def test_identity_write_cleans_temporary_files_on_every_failure(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    original = getattr(identity_module.os, operation)
    original_close = identity_module.os.close
    closed_descriptors: list[int] = []

    def fail(*args, **kwargs):
        if operation == "close":
            closed_descriptors.append(args[0])
            try:
                raise OSError(f"{operation} failed")
            finally:
                original_close(*args, **kwargs)
        raise OSError(f"{operation} failed")

    monkeypatch.setattr(identity_module.os, operation, fail)
    with pytest.raises(OSError, match=f"{operation} failed"):
        store._write(identity)
    assert [path for path in store.root.glob("identity.*") if path.name != "identity.json"] == []
    if operation == "close":
        assert closed_descriptors
    monkeypatch.setattr(identity_module.os, operation, original)


def test_identity_write_preserves_write_error_when_close_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    close_attempts: list[int] = []

    def fail_write(*args, **kwargs):
        raise OSError("write failed")

    def fail_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        try:
            raise OSError("close failed")
        finally:
            original_close(descriptor)

    original_close = identity_module.os.close
    monkeypatch.setattr(identity_module.os, "write", fail_write)
    monkeypatch.setattr(identity_module.os, "close", fail_close)
    with pytest.raises(OSError, match="write failed"):
        store._write(identity)
    assert close_attempts


def test_identity_write_preserves_write_error_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    monkeypatch.setattr(
        identity_module.os,
        "write",
        lambda descriptor, contents: (_ for _ in ()).throw(RuntimeError("write primary")),
    )
    monkeypatch.setattr(
        identity_module.os,
        "unlink",
        lambda path: (_ for _ in ()).throw(OSError("cleanup secondary")),
    )

    with pytest.raises(RuntimeError, match="write primary"):
        store._write(identity)


def test_identity_write_retries_short_writes(tmp_path: Path, monkeypatch) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    real_write = identity_module.os.write
    write_sizes: list[int] = []

    def short_first_write(descriptor, contents):
        limit = max(1, len(contents) // 2) if not write_sizes else len(contents)
        written = real_write(descriptor, contents[:limit])
        write_sizes.append(written)
        return written

    monkeypatch.setattr(identity_module.os, "write", short_first_write)

    store._write(identity)

    assert len(write_sizes) == 2
    assert store._read() == identity


def test_identity_write_rejects_zero_length_write(tmp_path: Path, monkeypatch) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    monkeypatch.setattr(identity_module.os, "write", lambda descriptor, contents: 0)

    with pytest.raises(OSError, match="could not finish writing project identity"):
        store._write(identity)

    assert not store.path.exists()


def test_identity_write_does_not_unlink_reused_temporary_name(tmp_path: Path, monkeypatch) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    real_replace = identity_module.os.replace
    temporary_paths: list[Path] = []

    def replace_and_reuse(source, destination):
        real_replace(source, destination)
        temporary = Path(source)
        temporary.write_bytes(b"unrelated")
        temporary_paths.append(temporary)

    monkeypatch.setattr(identity_module.os, "replace", replace_and_reuse)

    store._write(identity)

    assert len(temporary_paths) == 1
    assert store._read() == identity
    assert temporary_paths[0].read_bytes() == b"unrelated"


def test_identity_write_does_not_unlink_reused_name_after_replace_failure(
    tmp_path: Path, monkeypatch
) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    temporary_paths: list[Path] = []

    def fail_replace(source, destination):
        del destination
        temporary = Path(source)
        temporary.unlink()
        temporary.write_bytes(b"unrelated")
        temporary_paths.append(temporary)
        raise OSError("replace failed")

    monkeypatch.setattr(identity_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store._write(identity)
    assert len(temporary_paths) == 1
    assert temporary_paths[0].read_bytes() == b"unrelated"


def test_identity_lock_preserves_body_error_when_close_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    class SuccessfulFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(descriptor, mode):
            pass

    monkeypatch.setattr(identity_module, "fcntl", SuccessfulFcntl)
    original_close = identity_module.os.close
    close_attempts: list[int] = []

    def fail_close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        try:
            raise OSError("close failed")
        finally:
            original_close(descriptor)

    monkeypatch.setattr(
        identity_module.os,
        "close",
        fail_close,
    )
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="body failed"):
        with store._identity_lock():
            raise RuntimeError("body failed")
    with pytest.raises(OSError, match="close failed"):
        with store._identity_lock():
            pass
    assert len(close_attempts) == 2


def test_identity_lock_preserves_body_error_when_unlock_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    class UnlockFailure:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(descriptor: int, mode: int) -> None:
            if mode == UnlockFailure.LOCK_UN:
                raise OSError("unlock failed")

    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True)
    monkeypatch.setattr(identity_module, "fcntl", UnlockFailure)
    original_close = identity_module.os.close
    closed: list[int] = []

    def track_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(identity_module.os, "close", track_close)
    with pytest.raises(RuntimeError, match="body failed"):
        with store._identity_lock():
            raise RuntimeError("body failed")
    assert closed
    with pytest.raises(OSError):
        identity_module.os.fstat(closed[0])
