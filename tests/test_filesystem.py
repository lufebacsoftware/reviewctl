from __future__ import annotations

import os
from pathlib import Path

import pytest

import reviewctl.filesystem as filesystem


def test_confined_directory_fails_closed_without_platform_support(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(filesystem, "_OPEN_SUPPORTS_DIR_FD", False)

    with pytest.raises(OSError, match="cannot confine"):
        with filesystem.confined_directory_descriptor(tmp_path):
            pass


def test_confined_directory_close_error_does_not_replace_a_primary_error(
    tmp_path: Path, monkeypatch
) -> None:
    real_close = filesystem.os.close

    def fail_close(descriptor: int) -> None:
        try:
            raise OSError("close failed")
        finally:
            real_close(descriptor)

    monkeypatch.setattr(filesystem.os, "close", fail_close)

    with pytest.raises(RuntimeError, match="body failed"):
        with filesystem.confined_directory_descriptor(tmp_path):
            raise RuntimeError("body failed")
    with pytest.raises(OSError, match="close failed"):
        with filesystem.confined_directory_descriptor(tmp_path):
            pass


def test_confined_regular_close_error_does_not_replace_a_primary_error(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    real_open = filesystem.os.open
    real_close = filesystem.os.close
    regular_descriptor: int | None = None
    close_failures = 0

    def tracked_open(open_path, flags, *args, **kwargs):
        nonlocal regular_descriptor
        descriptor = real_open(open_path, flags, *args, **kwargs)
        if Path(open_path) == Path(path.name) and kwargs.get("dir_fd") is not None:
            regular_descriptor = descriptor
        return descriptor

    def fail_regular_close(descriptor: int) -> None:
        nonlocal close_failures
        if descriptor == regular_descriptor:
            close_failures += 1
            real_close(descriptor)
            raise OSError("regular close failed")
        real_close(descriptor)

    monkeypatch.setattr(filesystem.os, "open", tracked_open)
    monkeypatch.setattr(filesystem.os, "close", fail_regular_close)

    with pytest.raises(RuntimeError, match="body failed"):
        with filesystem.confined_regular_descriptor(path, os.O_RDONLY):
            raise RuntimeError("body failed")
    with pytest.raises(OSError, match="regular close failed"):
        with filesystem.confined_regular_descriptor(path, os.O_RDONLY):
            pass
    assert close_failures == 2
