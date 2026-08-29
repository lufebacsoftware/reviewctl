"""Deterministic Git range manifests for bounded formal reviews.

This module deliberately stops at freezing a range.  It does not invoke a
model or make an approval decision; later transports can consume the frozen
chunk payloads without recomputing the diff.
"""

from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

CHUNKING_VERSION = "file-sections-v1"
DEFAULT_CONTEXT_LINES = 3
DEFAULT_MAX_CHUNK_BYTES = 128 * 1024
MAX_DIFF_BYTES = 4 * 1024 * 1024
_DIFF_HEADER = re.compile(rb"(?m)^diff --git ")


class RangeReviewError(ValueError):
    """A requested range cannot be frozen safely."""


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
    )


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
    return diagnostic[:400] or f"git exited with status {result.returncode}"


def _repository_root(repository: Path) -> Path:
    candidate = Path(repository).expanduser().resolve()
    if not candidate.is_dir():
        raise RangeReviewError(f"repository is not a directory: {candidate}")
    result = _run_git(candidate, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise RangeReviewError(f"not a Git repository: {candidate}")
    try:
        return Path(result.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as error:
        raise RangeReviewError("Git repository root is not valid UTF-8") from error


def _resolve_revision(repository: Path, revision: str, label: str) -> str:
    if not revision or revision.startswith("-"):
        raise RangeReviewError(f"could not resolve {label}: invalid revision")
    result = _run_git(
        repository,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{revision}^{{commit}}",
    )
    if result.returncode != 0:
        raise RangeReviewError(f"could not resolve {label}: {_git_error(result)}")
    value = result.stdout.decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise RangeReviewError(f"could not resolve {label}: Git returned an invalid object id")
    return value.lower()


def _merge_base(repository: Path, base: str, head: str) -> str | None:
    result = _run_git(repository, "merge-base", base, head)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise RangeReviewError(f"could not compute merge base: {_git_error(result)}")
    value = result.stdout.decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise RangeReviewError("could not compute merge base: Git returned an invalid object id")
    return value.lower()


def _canonical_diff_arguments(context_lines: int, *options: str) -> tuple[str, ...]:
    """Pin diff output knobs that otherwise come from user or repository config."""
    return (
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-color",
        "--text",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
        "--inter-hunk-context=0",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--no-relative",
        "--ignore-submodules=none",
        "-O/dev/null",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        *options,
        f"--unified={context_lines}",
    )


def _canonical_diff(repository: Path, base: str, head: str, context_lines: int) -> bytes:
    result = _run_git(
        repository,
        *_canonical_diff_arguments(
            context_lines,
            "--full-index",
        ),
        base,
        head,
        "--",
    )
    if result.returncode != 0:
        raise RangeReviewError(f"could not compute canonical diff: {_git_error(result)}")
    if len(result.stdout) > MAX_DIFF_BYTES:
        raise RangeReviewError(
            f"canonical diff exceeds {MAX_DIFF_BYTES} bytes; narrow the range before reviewing"
        )
    return result.stdout


def _canonical_paths(repository: Path, base: str, head: str) -> list[str]:
    result = _run_git(
        repository,
        *_canonical_diff_arguments(0, "--name-only", "-z"),
        base,
        head,
        "--",
    )
    if result.returncode != 0:
        raise RangeReviewError(f"could not capture canonical paths: {_git_error(result)}")
    return [
        value.decode("utf-8", errors="surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
    ]


def _diff_sections(diff: bytes) -> list[bytes]:
    starts = [match.start() for match in _DIFF_HEADER.finditer(diff)]
    if not starts:
        if diff:
            raise RangeReviewError("canonical diff has no file sections")
        return []
    return [diff[start:end] for start, end in zip(starts, (*starts[1:], len(diff)), strict=True)]


def _chunks(sections: list[bytes], paths: list[str], max_chunk_bytes: int) -> list[dict[str, Any]]:
    if len(sections) != len(paths):
        raise RangeReviewError(
            "canonical path count does not match canonical diff file-section count"
        )
    chunks: list[dict[str, Any]] = []
    current: list[tuple[bytes, str]] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if not current:
            return
        payload = b"".join(section for section, _ in current)
        chunk_paths: list[str] = []
        for _, path in current:
            if path not in chunk_paths:
                chunk_paths.append(path)
        chunks.append(
            {
                "index": len(chunks),
                "patchSha256": hashlib.sha256(payload).hexdigest(),
                "byteLength": len(payload),
                "fileCount": len(chunk_paths),
                "paths": chunk_paths,
                "patch": base64.b64encode(payload).decode("ascii"),
            }
        )
        current = []
        current_size = 0

    for section, path in zip(sections, paths, strict=True):
        if len(section) > max_chunk_bytes:
            raise RangeReviewError(
                f"single file diff exceeds {max_chunk_bytes} bytes: {path or 'one file'}"
            )
        if current and current_size + len(section) > max_chunk_bytes:
            flush()
        current.append((section, path))
        current_size += len(section)
    flush()
    return chunks


def build_range_manifest(
    repository: Path,
    base: str,
    head: str,
    *,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """Freeze one immutable Git range into a deterministic JSON-compatible mapping."""
    if context_lines <= 0:
        raise RangeReviewError("context_lines must be positive")
    if max_chunk_bytes <= 0:
        raise RangeReviewError("max_chunk_bytes must be positive")
    root = _repository_root(repository)
    resolved_base = _resolve_revision(root, base, "base")
    resolved_head = _resolve_revision(root, head, "head")
    merge_base = _merge_base(root, resolved_base, resolved_head)
    diff = _canonical_diff(root, resolved_base, resolved_head, context_lines)
    if not diff and not allow_empty:
        raise RangeReviewError("range has no changes; pass allow_empty to manifest it")
    sections = _diff_sections(diff)
    paths = _canonical_paths(root, resolved_base, resolved_head)
    chunks = _chunks(sections, paths, max_chunk_bytes)
    return {
        "schemaVersion": 1,
        "status": "manifest-created",
        "repository": str(root),
        "baseSha": resolved_base,
        "headSha": resolved_head,
        "mergeBaseSha": merge_base,
        "comparison": f"{resolved_base}..{resolved_head}",
        "contextLines": context_lines,
        "chunkingVersion": CHUNKING_VERSION,
        "canonicalDiffSha256": hashlib.sha256(diff).hexdigest(),
        "chunkCount": len(chunks),
        "chunks": chunks,
    }
