from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from reviewctl.filesystem import confined_directory_descriptor

pytestmark = pytest.mark.platform


def test_confined_directory_accepts_the_platform_temporary_directory() -> None:
    """Exercise the actual tempfile root, including macOS's /var alias."""
    with tempfile.TemporaryDirectory(prefix="reviewctl-platform-") as directory:
        artifact_root = Path(directory) / "artifacts"
        with confined_directory_descriptor(artifact_root, create=True) as descriptor:
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
