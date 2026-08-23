from __future__ import annotations

import stat
from pathlib import Path

import pytest

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
