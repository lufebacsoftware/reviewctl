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
