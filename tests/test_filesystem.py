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


def test_confined_directory_rejects_missing_component_without_creation(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        with filesystem.confined_directory_descriptor(tmp_path / "missing"):
            pass


def test_relative_confinement_rejects_unsupported_platform_and_invalid_paths(
    tmp_path: Path, monkeypatch
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="relative path"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path("../escape"), os.O_RDONLY
            ):
                pass
        monkeypatch.setattr(filesystem, "_OPEN_SUPPORTS_DIR_FD", False)
        with pytest.raises(OSError, match="cannot confine"):
            with filesystem.confined_relative_directory_descriptor(parent, ()):
                pass
        with pytest.raises(OSError, match="cannot confine"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path("missing"), os.O_RDONLY
            ):
                pass
        with pytest.raises(OSError, match="cannot confine"):
            with filesystem.confined_regular_descriptor(tmp_path / "missing", os.O_RDONLY):
                pass
    finally:
        os.close(parent)


def test_relative_directory_validates_parent_missing_and_child_descriptors(
    tmp_path: Path, monkeypatch
) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    file_path = tmp_path / "file"
    file_path.write_text("not a directory")
    file_descriptor = os.open(file_path, os.O_RDONLY)
    try:
        with pytest.raises(OSError, match="parent descriptor"):
            with filesystem.confined_relative_directory_descriptor(file_descriptor, ()):
                pass
        with pytest.raises(FileNotFoundError):
            with filesystem.confined_relative_directory_descriptor(parent, ("missing",)):
                pass
        child = tmp_path / "child"
        child.mkdir()
        real_is_directory = filesystem.stat.S_ISDIR
        calls = 0

        def reject_child(mode: int) -> bool:
            nonlocal calls
            calls += 1
            return calls == 1 and real_is_directory(mode)

        monkeypatch.setattr(filesystem.stat, "S_ISDIR", reject_child)
        with pytest.raises(OSError, match="path component"):
            with filesystem.confined_relative_directory_descriptor(parent, (child.name,)):
                pass
    finally:
        os.close(file_descriptor)
        os.close(parent)


def test_relative_directory_creates_missing_components(tmp_path: Path) -> None:
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with filesystem.confined_relative_directory_descriptor(
            parent, ("created", "nested"), create=True
        ) as descriptor:
            assert filesystem.stat.S_ISDIR(os.fstat(descriptor).st_mode)
    finally:
        os.close(parent)


def test_relative_regular_close_error_preserves_primary_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "receipt.json"
    path.write_text("{}")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_open = filesystem.os.open
    real_close = filesystem.os.close
    regular_descriptor: int | None = None

    def tracked_open(open_path, flags, *args, **kwargs):
        nonlocal regular_descriptor
        descriptor = real_open(open_path, flags, *args, **kwargs)
        if Path(open_path) == Path(path.name) and kwargs.get("dir_fd") is not None:
            regular_descriptor = descriptor
        return descriptor

    def fail_regular_close(descriptor: int) -> None:
        if descriptor == regular_descriptor:
            real_close(descriptor)
            raise OSError("relative regular close failed")
        real_close(descriptor)

    monkeypatch.setattr(filesystem.os, "open", tracked_open)
    monkeypatch.setattr(filesystem.os, "close", fail_regular_close)
    try:
        with pytest.raises(RuntimeError, match="body failed"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path(path.name), os.O_RDONLY
            ):
                raise RuntimeError("body failed")
        with pytest.raises(OSError, match="relative regular close failed"):
            with filesystem.confined_relative_regular_descriptor(
                parent, Path(path.name), os.O_RDONLY
            ):
                pass
    finally:
        real_close(parent)


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
