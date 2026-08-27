from __future__ import annotations

import stat
from pathlib import Path

import pytest

import reviewctl.artifacts as artifacts_module
from reviewctl.artifacts import ArtifactStore


def test_sensitive_artifact_is_private(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / "review")

    path = artifact.write_text("request.json", "private prompt")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "private prompt"


def test_artifact_rejects_path_traversal(tmp_path: Path) -> None:
    artifact = ArtifactStore(tmp_path / "review")

    with pytest.raises(ValueError, match="path"):
        artifact.write_text("../escape", "not allowed")


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
