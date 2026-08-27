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

    class UnlockFailure(FakeFcntl):
        @staticmethod
        def flock(descriptor, mode):
            if mode == UnlockFailure.LOCK_UN:
                raise OSError("unlock failed")

    monkeypatch.setattr(identity_module, "fcntl", UnlockFailure)
    monkeypatch.setattr(identity_module.os, "close", lambda descriptor: closed.append(descriptor))
    with pytest.raises(OSError, match="unlock failed"):
        with store._identity_lock():
            pass
    assert closed


def test_identity_store_replace_cleanup(tmp_path: Path, monkeypatch) -> None:
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

    def fail(*args, **kwargs):
        raise OSError(f"{operation} failed")

    monkeypatch.setattr(identity_module.os, operation, fail)
    with pytest.raises(OSError, match=f"{operation} failed"):
        store._write(identity)
    assert [path for path in store.root.glob("identity.*") if path.name != "identity.json"] == []
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
        raise OSError("close failed")

    monkeypatch.setattr(identity_module.os, "write", fail_write)
    monkeypatch.setattr(identity_module.os, "close", fail_close)
    with pytest.raises(OSError, match="write failed"):
        store._write(identity)
    assert close_attempts


def test_identity_write_preserves_replace_error_when_unlink_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")
    unlink_attempts: list[str] = []

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    def fail_unlink(path: str) -> None:
        unlink_attempts.append(path)
        raise OSError("unlink failed")

    monkeypatch.setattr(identity_module.os, "replace", fail_replace)
    monkeypatch.setattr(identity_module.os, "unlink", fail_unlink)
    with pytest.raises(OSError, match="replace failed"):
        store._write(identity)
    assert unlink_attempts


def test_identity_write_preserves_standalone_unlink_error(tmp_path: Path, monkeypatch) -> None:
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    identity = ProjectIdentity("project-one", "origin-one", "2026-08-27T00:00:00Z")

    monkeypatch.setattr(identity_module.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        identity_module.os,
        "unlink",
        lambda path: (_ for _ in ()).throw(OSError("unlink failed")),
    )
    with pytest.raises(OSError, match="unlink failed"):
        store._write(identity)


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
    monkeypatch.setattr(
        identity_module.os,
        "close",
        lambda descriptor: (_ for _ in ()).throw(OSError("close failed")),
    )
    store = ProjectIdentityStore(tmp_path)
    store.root.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="body failed"):
        with store._identity_lock():
            raise RuntimeError("body failed")
    with pytest.raises(OSError, match="close failed"):
        with store._identity_lock():
            pass
