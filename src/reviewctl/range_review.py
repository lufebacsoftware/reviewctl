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


def _canonical_diff(repository: Path, base: str, head: str, context_lines: int) -> bytes:
    result = _run_git(
        repository,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "--full-index",
        "--no-renames",
        f"--unified={context_lines}",
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


def _decode_git_path(value: bytes) -> str:
    path = value.decode("utf-8", errors="surrogateescape")
    if path == "/dev/null":
        return ""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _section_paths(section: bytes) -> list[str]:
    paths: list[str] = []
    for line in section.splitlines():
        if line.startswith((b"--- ", b"+++ ")):
            value = _decode_git_path(line[4:])
            if value and value not in paths:
                paths.append(value)
        elif line.startswith((b"rename from ", b"rename to ")):
            value = line.split(b" ", 2)[-1].decode("utf-8", errors="surrogateescape")
            if value and value not in paths:
                paths.append(value)
    if paths:
        return paths
    header = section.splitlines()[0] if section else b""
    if header.startswith(b"diff --git "):
        values = header[len(b"diff --git ") :].split()
        for value in values[:2]:
            decoded = _decode_git_path(value)
            if decoded and decoded not in paths:
                paths.append(decoded)
    return paths


def _diff_sections(diff: bytes) -> list[bytes]:
    starts = [match.start() for match in _DIFF_HEADER.finditer(diff)]
    if not starts:
        if diff:
            raise RangeReviewError("canonical diff has no file sections")
        return []
    return [diff[start:end] for start, end in zip(starts, (*starts[1:], len(diff)), strict=True)]


def _chunks(sections: list[bytes], max_chunk_bytes: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[bytes] = []
    current_size = 0

    def flush() -> None:
        nonlocal current, current_size
        if not current:
            return
        payload = b"".join(current)
        paths: list[str] = []
        for section in current:
            for path in _section_paths(section):
                if path not in paths:
                    paths.append(path)
        chunks.append(
            {
                "index": len(chunks),
                "patchSha256": hashlib.sha256(payload).hexdigest(),
                "byteLength": len(payload),
                "fileCount": len(paths),
                "paths": paths,
                "patch": base64.b64encode(payload).decode("ascii"),
            }
        )
        current = []
        current_size = 0

    for section in sections:
        if len(section) > max_chunk_bytes:
            paths = _section_paths(section)
            label = paths[0] if paths else "one file"
            raise RangeReviewError(
                f"single file diff exceeds {max_chunk_bytes} bytes: {label}"
            )
        if current and current_size + len(section) > max_chunk_bytes:
            flush()
        current.append(section)
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
    chunks = _chunks(sections, max_chunk_bytes)
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
