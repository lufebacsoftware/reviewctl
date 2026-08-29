from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path

import pytest

from reviewctl.range_review import RangeReviewError, build_range_manifest


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def make_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--quiet")
    git(repository, "config", "user.email", "reviewctl@example.invalid")
    git(repository, "config", "user.name", "reviewctl tests")
    (repository / "README.md").write_text("base\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "--quiet", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("base\nchanged\n")
    (repository / "ledger.txt").write_text("one\ntwo\n")
    (repository / "notes.txt").write_text("note\n")
    git(repository, "add", "README.md", "ledger.txt", "notes.txt")
    git(repository, "commit", "--quiet", "-m", "changes")
    head = git(repository, "rev-parse", "HEAD")
    return repository, base, head


def test_manifest_freezes_range_identity_and_deterministic_chunks(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)

    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=4096)

    assert manifest["repository"] == str(repository.resolve())
    assert manifest["baseSha"] == base
    assert manifest["headSha"] == head
    assert manifest["mergeBaseSha"] == base
    assert manifest["comparison"] == f"{base}..{head}"
    assert manifest["contextLines"] == 3
    assert manifest["chunkingVersion"] == "file-sections-v1"
    assert manifest["canonicalDiffSha256"]
    assert manifest["chunkCount"] == len(manifest["chunks"]) == 1
    chunk = manifest["chunks"][0]
    assert chunk["index"] == 0
    assert chunk["patchSha256"]
    assert chunk["byteLength"] > 0
    assert chunk["fileCount"] == 3
    assert chunk["paths"] == ["README.md", "ledger.txt", "notes.txt"]
    assert chunk["patch"].encode()  # The frozen packet is retained for later transport.

    repeated = build_range_manifest(repository, base, head, max_chunk_bytes=4096)
    assert repeated == manifest


def test_manifest_rejects_invalid_revision_and_empty_range(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)

    with pytest.raises(RangeReviewError, match="could not resolve base"):
        build_range_manifest(repository, "missing", head)

    with pytest.raises(RangeReviewError, match="range has no changes"):
        build_range_manifest(repository, base, base)

    empty = build_range_manifest(repository, base, base, allow_empty=True)
    assert empty["chunkCount"] == 0
    assert empty["chunks"] == []
    assert empty["canonicalDiffSha256"]


def test_manifest_rejects_non_positive_context_and_oversized_file_chunk(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)

    with pytest.raises(RangeReviewError, match="context_lines must be positive"):
        build_range_manifest(repository, base, head, context_lines=0)

    with pytest.raises(RangeReviewError, match="single file diff exceeds"):
        build_range_manifest(repository, base, head, max_chunk_bytes=32)


def test_manifest_rejects_non_repository_path(tmp_path: Path) -> None:
    with pytest.raises(RangeReviewError, match="not a Git repository"):
        build_range_manifest(tmp_path, "HEAD", "HEAD")


def test_manifest_chunks_are_ordered_non_overlapping_and_reassemble_diff(
    tmp_path: Path,
) -> None:
    repository, base, head = make_repository(tmp_path)

    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=300)

    assert manifest["chunkCount"] > 1
    chunks = manifest["chunks"]
    assert [chunk["index"] for chunk in chunks] == list(range(len(chunks)))
    payload = b"".join(base64.b64decode(chunk["patch"]) for chunk in chunks)
    assert hashlib.sha256(payload).hexdigest() == manifest["canonicalDiffSha256"]
    assert all(chunk["byteLength"] <= 300 for chunk in chunks)


def test_manifest_is_insensitive_to_ambient_git_diff_configuration(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)
    baseline = build_range_manifest(repository, base, head, max_chunk_bytes=4096)
    order_file = tmp_path / "order.txt"
    order_file.write_text("notes.txt\nREADME.md\nledger.txt\n")
    info_attributes = repository / ".git" / "info" / "attributes"
    info_attributes.write_text("README.md binary\n")

    for key, value in (
        ("color.ui", "always"),
        ("diff.algorithm", "patience"),
        ("diff.indentHeuristic", "true"),
        ("diff.noprefix", "true"),
        ("diff.orderFile", str(order_file)),
    ):
        git(repository, "config", key, value)

    configured = build_range_manifest(repository, base, head, max_chunk_bytes=4096)
    assert configured == baseline


def test_manifest_preserves_git_paths_with_c_quoted_characters(tmp_path: Path) -> None:
    repository, base, _ = make_repository(tmp_path)
    special_name = "tab\tname.txt"
    (repository / special_name).write_text("special\n")
    git(repository, "add", special_name)
    git(repository, "commit", "--quiet", "-m", "special path")
    head = git(repository, "rev-parse", "HEAD")

    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=4096)

    assert any(special_name in chunk["paths"] for chunk in manifest["chunks"])
