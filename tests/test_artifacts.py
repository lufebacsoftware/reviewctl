from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import reviewctl.artifacts as artifacts_module
from reviewctl.artifacts import ArtifactStore
from reviewctl.errors import JournalOperationError


def test_sensitive_artifact_is_private(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / "review")

    path = artifact.write_text("request.json", "private prompt")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "private prompt"


@pytest.mark.parametrize("name", ["../escape", "."])
def test_artifact_rejects_path_traversal(tmp_path: Path, name: str) -> None:
    artifact = ArtifactStore(tmp_path / "review")

    with pytest.raises(ValueError, match="path"):
        artifact.write_text(name, "not allowed")


def test_artifact_replaces_existing_file_only_explicitly(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / "review")
    artifact.write_text("receipt.json", "first")

    with pytest.raises(FileExistsError):
        artifact.write_text("receipt.json", "second")


def test_artifact_rejects_absolute_path(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / "review")

    with pytest.raises(ValueError, match="root"):
        artifact.write_text(str(tmp_path / "outside"), "not allowed")


def test_artifact_rejects_zero_progress_write(tmp_path: Path, monkeypatch) -> None:
    artifact = ArtifactStore(tmp_path / "review")
    monkeypatch.setattr(artifacts_module.os, "write", lambda descriptor, view: 0)

    with pytest.raises(OSError, match="finish"):
        artifact.write_bytes("packet.json", b"payload")


def test_artifact_store_rejects_symlinked_project_state_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (project / ".reviewctl").symlink_to(external, target_is_directory=True)

    with pytest.raises(JournalOperationError, match="state root"):
        ArtifactStore(project / ".reviewctl" / "reviews")

    assert list(external.iterdir()) == []


def test_artifact_store_rejects_symlinked_state_descendant(tmp_path: Path) -> None:
    project = tmp_path / "project"
    state = project / ".reviewctl"
    state.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (state / "reviews").symlink_to(external, target_is_directory=True)

    with pytest.raises(JournalOperationError, match="state path"):
        ArtifactStore(state / "reviews" / "review-one")

    assert list(external.iterdir()) == []


def test_artifact_store_writes_nested_project_state_artifact(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    store = ArtifactStore(tmp_path / "project" / ".reviewctl" / "reviews" / "r1")

    target = store.write_text("source/module.py", "private source")

    assert target.read_text() == "private source"
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_artifact_store_fails_closed_without_descriptor_confinement(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(artifacts_module, "_OPEN_SUPPORTS_DIR_FD", False)

    with pytest.raises(OSError, match="cannot confine"):
        ArtifactStore(tmp_path / "review")


@pytest.mark.parametrize("invalid_descriptor", ["anchor", "component", "artifact"])
def test_artifact_store_rejects_non_directory_and_non_file_descriptors(
    tmp_path: Path, monkeypatch, invalid_descriptor: str
) -> None:
    store = ArtifactStore(tmp_path / "review")
    if invalid_descriptor == "artifact":
        monkeypatch.setattr(artifacts_module.stat, "S_ISREG", lambda mode: False)
        with pytest.raises(OSError, match="not a regular file"):
            store.write_text("packet.json", "private prompt")
        return
    real_fstat = artifacts_module.os.fstat
    calls = 0

    def fake_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        invalid_call = {"anchor": 1, "component": 2}[invalid_descriptor]
        if calls == invalid_call:
            return SimpleNamespace(st_mode=0)
        return real_fstat(descriptor)

    monkeypatch.setattr(artifacts_module.os, "fstat", fake_fstat)

    with pytest.raises(OSError, match="not a directory|not a regular file"):
        store.write_text("packet.json", "private prompt")


def test_artifact_write_rejects_root_replaced_by_external_symlink_before_open(
    tmp_path: Path, monkeypatch
) -> None:
    store = ArtifactStore(tmp_path / "review")
    displaced = tmp_path / "review-displaced"
    external = tmp_path / "external"
    external.mkdir()
    target = store.root / "packet.json"
    real_open = artifacts_module.os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            Path(path) == target
            or (Path(path) == Path(store.root.name) and kwargs.get("dir_fd") is not None)
        ) and not swapped:
            swapped = True
            store.root.rename(displaced)
            store.root.symlink_to(external, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "open", raced_open)

    with pytest.raises(OSError):
        store.write_text("packet.json", "private prompt")

    assert swapped
    assert list(external.iterdir()) == []


def test_artifact_write_never_follows_nested_directory_replaced_after_open(
    tmp_path: Path, monkeypatch
) -> None:
    store = ArtifactStore(tmp_path / "review")
    nested = store.root / "nested"
    nested.mkdir()
    displaced = store.root / "nested-displaced"
    external = tmp_path / "external"
    external.mkdir()
    real_open = artifacts_module.os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path("packet.json") and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            nested.rename(displaced)
            nested.symlink_to(external, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "open", raced_open)

    store.write_text("nested/packet.json", "private prompt")

    assert swapped
    assert list(external.iterdir()) == []
    assert (displaced / "packet.json").read_text() == "private prompt"


def test_artifact_write_rejects_project_root_replaced_before_anchor_open(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = ArtifactStore(project / ".reviewctl" / "reviews" / "r1")
    displaced = tmp_path / "project-displaced"
    external_project = tmp_path / "external-project"
    (external_project / ".reviewctl" / "reviews" / "r1").mkdir(parents=True)
    real_open = artifacts_module.os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path(store.root.anchor) and not swapped:
            swapped = True
            project.rename(displaced)
            project.symlink_to(external_project, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "open", raced_open)

    with pytest.raises(OSError):
        store.write_text("packet.json", "private prompt")

    assert swapped
    assert list((external_project / ".reviewctl" / "reviews" / "r1").iterdir()) == []


def test_artifact_write_rejects_project_parent_replaced_before_anchor_open(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    store = ArtifactStore(project / ".reviewctl" / "reviews" / "r1")
    displaced = tmp_path / "workspace-displaced"
    external_workspace = tmp_path / "external-workspace"
    external_root = external_workspace / "project" / ".reviewctl" / "reviews" / "r1"
    external_root.mkdir(parents=True)
    real_open = artifacts_module.os.open
    swapped = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == Path(store.root.anchor) and not swapped:
            swapped = True
            workspace.rename(displaced)
            workspace.symlink_to(external_workspace, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "open", raced_open)

    with pytest.raises(OSError):
        store.write_text("packet.json", "private prompt")

    assert swapped
    assert list(external_root.iterdir()) == []


def test_artifact_write_rejects_root_replaced_by_a_real_directory(tmp_path: Path) -> None:
    root = tmp_path / "review"
    store = ArtifactStore(root)
    displaced = tmp_path / "review-displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    root.rename(displaced)
    replacement.rename(root)

    with pytest.raises(OSError, match="identity changed"):
        store.write_text("packet.json", "private prompt")

    assert list(root.iterdir()) == []


def test_artifact_write_does_not_return_a_path_after_root_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "review"
    store = ArtifactStore(root)
    displaced = tmp_path / "review-displaced"
    real_write = artifacts_module.os.write
    swapped = False

    def raced_write(descriptor: int, value: memoryview) -> int:
        nonlocal swapped
        written = real_write(descriptor, value)
        if not swapped:
            swapped = True
            root.rename(displaced)
            root.mkdir()
        return written

    monkeypatch.setattr(artifacts_module.os, "write", raced_write)

    with pytest.raises(OSError, match="identity changed"):
        store.write_text("packet.json", "private prompt")

    assert swapped
    assert list(root.iterdir()) == []
    assert (displaced / "packet.json").read_text() == "private prompt"
