"""Deterministic Git range manifests for bounded formal reviews.

This module deliberately stops at freezing a range.  It does not invoke a
model or make an approval decision; later transports can consume the frozen
chunk payloads without recomputing the diff.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from reviewctl.contracts import canonical_json as contract_canonical_json

CHUNKING_VERSION = "file-sections-v1"
RANGE_REVIEW_SCHEMA_VERSION = 1
DEFAULT_CONTEXT_LINES = 3
DEFAULT_MAX_CHUNK_BYTES = 128 * 1024
MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_GIT_STDERR_BYTES = 100 * 1024
_DIFF_HEADER = re.compile(rb"(?m)^diff --git ")
_HUNK_HEADER = re.compile(
    rb"(?m)^(@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@)[^\r\n]*(\r?\n|$)"
)


class RangeReviewError(ValueError):
    """A requested range cannot be frozen safely."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*$")
_MANIFEST_FIELDS = {
    "schemaVersion",
    "status",
    "repository",
    "baseSha",
    "headSha",
    "mergeBaseSha",
    "comparison",
    "contextLines",
    "chunkingVersion",
    "canonicalDiffSha256",
    "chunkCount",
    "chunks",
    "evidenceStatus",
}
_CHUNK_FIELDS = {
    "index",
    "patchSha256",
    "byteLength",
    "fileCount",
    "paths",
    "patch",
}
_RANGE_FIELDS = {
    "repository",
    "baseSha",
    "headSha",
    "mergeBaseSha",
    "comparison",
    "contextLines",
    "chunkingVersion",
    "canonicalDiffSha256",
    "chunkCount",
}
_AGGREGATE_FIELDS = {
    "rangeReviewSchemaVersion",
    "result",
    "reviewId",
    "evidenceStatus",
    "manifestSha256",
    "range",
    "chunks",
    "aggregate",
    "sha256",
}
_AGGREGATE_CHUNK_FIELDS = {
    "index",
    "chunkId",
    "patchSha256",
    "reviewId",
    "receipt",
    "receiptFileSha256",
    "receiptSha256",
    "result",
    "verdict",
    "findings",
}


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
    )


def _run_git_bounded(
    repository: Path, *arguments: str, max_stdout_bytes: int
) -> subprocess.CompletedProcess[bytes]:
    """Run Git while keeping captured stdout/stderr bounded in memory."""
    if max_stdout_bytes <= 0:
        raise ValueError("max_stdout_bytes must be positive")
    command = ["git", "-C", str(repository), *arguments]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        raise RuntimeError("Git pipes were not created")
    stdout = bytearray()
    stderr = bytearray()

    def drain_stderr() -> None:
        while True:
            chunk = process.stderr.read(64 * 1024)
            if not chunk:
                return
            remaining = MAX_GIT_STDERR_BYTES + 1 - len(stderr)
            if remaining > 0:
                stderr.extend(chunk[:remaining])

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    oversized = False
    try:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            remaining = max_stdout_bytes + 1 - len(stdout)
            if remaining <= 0:
                oversized = True
                break
            stdout.extend(chunk[:remaining])
            if len(chunk) > remaining:
                oversized = True
                break
    finally:
        if oversized:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        stderr_thread.join()
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(command, process.returncode, bytes(stdout), bytes(stderr))


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
    return diagnostic[:400] or f"git exited with status {result.returncode}"


def _is_sha256(value: object) -> bool:
    return type(value) is str and bool(_SHA256.fullmatch(value))


def _is_object_id(value: object) -> bool:
    return type(value) is str and bool(_OBJECT_ID.fullmatch(value))


def _canonical_digest(value: object) -> str | None:
    try:
        return hashlib.sha256(contract_canonical_json(value)).hexdigest()
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        return None


def manifest_sha256(manifest: dict[str, Any]) -> str:
    """Return the digest of the canonical manifest object, excluding no fields."""
    digest = _canonical_digest(manifest)
    if digest is None:
        raise RangeReviewError("range manifest is not canonical JSON")
    return digest


def range_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract the immutable identity repeated in every formal chunk result."""
    return {key: manifest[key] for key in _RANGE_FIELDS}


def _manifest_chunk_payload(chunk: object) -> bytes | None:
    if type(chunk) is not dict:
        return None
    payload = chunk.get("patch")
    if type(payload) is not str:
        return None
    try:
        decoded = base64.b64decode(payload.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None
    if base64.b64encode(decoded).decode("ascii") != payload:
        return None
    return decoded


def validate_range_manifest(manifest: object) -> tuple[str, ...]:
    """Validate a frozen manifest before any formal transport consumes it."""
    violations: list[str] = []

    def reject(code: str) -> None:
        if code not in violations:
            violations.append(code)

    if type(manifest) is not dict:
        return ("manifest-object",)
    if set(manifest) - _MANIFEST_FIELDS:
        reject("manifest-fields")
    if type(manifest.get("schemaVersion")) is not int or manifest.get("schemaVersion") != 1:
        reject("manifest-schema-version")
    if manifest.get("status") != "manifest-created":
        reject("manifest-status")
    if "evidenceStatus" in manifest and manifest.get("evidenceStatus") != "planning-only":
        reject("manifest-evidence-status")

    repository = manifest.get("repository")
    if type(repository) is not str or not repository.strip():
        reject("manifest-repository")
    base_sha = manifest.get("baseSha")
    head_sha = manifest.get("headSha")
    merge_base_sha = manifest.get("mergeBaseSha")
    if not _is_object_id(base_sha):
        reject("manifest-base-sha")
    if not _is_object_id(head_sha):
        reject("manifest-head-sha")
    if merge_base_sha is not None and not _is_object_id(merge_base_sha):
        reject("manifest-merge-base-sha")
    if (
        type(base_sha) is str
        and type(head_sha) is str
        and manifest.get("comparison") != f"{base_sha}..{head_sha}"
    ):
        reject("manifest-comparison")
    context_lines = manifest.get("contextLines")
    if type(context_lines) is not int or isinstance(context_lines, bool) or context_lines <= 0:
        reject("manifest-context")
    if manifest.get("chunkingVersion") != CHUNKING_VERSION:
        reject("manifest-chunking-version")
    if not _is_sha256(manifest.get("canonicalDiffSha256")):
        reject("manifest-diff-sha")

    chunks = manifest.get("chunks")
    chunk_count = manifest.get("chunkCount")
    if type(chunks) is not list:
        reject("manifest-chunks")
        return tuple(violations)
    if type(chunk_count) is not int or isinstance(chunk_count, bool) or chunk_count < 0:
        reject("manifest-chunk-count")
    elif chunk_count != len(chunks):
        reject("manifest-chunk-count")

    payloads: list[bytes] = []
    all_paths: set[str] = set()
    indexes: list[object] = []
    for chunk in chunks:
        if type(chunk) is not dict:
            reject("chunk-object")
            continue
        if set(chunk) != _CHUNK_FIELDS:
            reject("chunk-fields")
        index = chunk.get("index")
        indexes.append(index)
        if type(index) is not int or isinstance(index, bool) or index < 0:
            reject("chunk-index")
        payload = _manifest_chunk_payload(chunk)
        if payload is None:
            reject("chunk-payload")
            continue
        payloads.append(payload)
        if not _is_sha256(chunk.get("patchSha256")):
            reject("chunk-sha")
        elif hashlib.sha256(payload).hexdigest() != chunk["patchSha256"]:
            reject("chunk-sha")
        if type(chunk.get("byteLength")) is not int or isinstance(
            chunk.get("byteLength"), bool
        ) or chunk.get("byteLength") != len(payload):
            reject("chunk-length")
        paths = chunk.get("paths")
        if (
            type(paths) is not list
            or not paths
            or any(type(path) is not str or not path for path in paths)
            or len(paths) != len(set(paths))
        ):
            reject("chunk-paths")
        else:
            duplicate_paths = all_paths.intersection(paths)
            if duplicate_paths:
                reject("chunk-overlap")
            all_paths.update(paths)
        if type(chunk.get("fileCount")) is not int or isinstance(
            chunk.get("fileCount"), bool
        ) or (
            type(paths) is list and chunk.get("fileCount") != len(paths)
        ):
            reject("chunk-file-count")

    if indexes != list(range(len(chunks))):
        reject("chunk-order")
    if len(payloads) == len(chunks):
        if hashlib.sha256(b"".join(payloads)).hexdigest() != manifest.get("canonicalDiffSha256"):
            reject("manifest-diff-sha")
    return tuple(violations)


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
        "-c",
        "diff.suppressBlankEmpty=false",
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
        "--submodule=short",
        "-O/dev/null",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        *options,
        f"--unified={context_lines}",
    )


def _canonical_diff(repository: Path, base: str, head: str, context_lines: int) -> bytes:
    result = _run_git_bounded(
        repository,
        *_canonical_diff_arguments(
            context_lines,
            "--full-index",
        ),
        base,
        head,
        "--",
        max_stdout_bytes=MAX_DIFF_BYTES,
    )
    if result.returncode != 0:
        raise RangeReviewError(f"could not compute canonical diff: {_git_error(result)}")
    if len(result.stdout) > MAX_DIFF_BYTES:
        raise RangeReviewError(
            f"canonical diff exceeds {MAX_DIFF_BYTES} bytes; narrow the range before reviewing"
        )
    # Diff-driver xfuncname only adds a human-oriented suffix to hunk headers.
    # Strip it so repository attributes cannot change the frozen patch digest.
    return _HUNK_HEADER.sub(rb"\1\2", result.stdout)


def _canonical_paths(repository: Path, base: str, head: str) -> list[str]:
    result = _run_git_bounded(
        repository,
        *_canonical_diff_arguments(0, "--name-only", "-z"),
        base,
        head,
        "--",
        max_stdout_bytes=MAX_DIFF_BYTES,
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


def build_range_aggregate(
    manifest: dict[str, Any],
    review_id: str,
    chunk_records: list[dict[str, Any]],
    *,
    receipt_loader: Callable[[str], tuple[bytes, object] | None] | None = None,
    receipt_validator: Callable[[object], Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic aggregate from verified persisted chunk results.

    A complete aggregate is never emitted unless the caller supplies both a
    receipt loader and the schema-v2 validator used to verify every receipt.
    Callers that only need a deterministic incomplete progress artifact may
    omit those callbacks.
    """
    manifest_violations = validate_range_manifest(manifest)
    if manifest_violations:
        raise RangeReviewError(
            "cannot aggregate invalid range manifest: " + ", ".join(manifest_violations)
        )
    if type(review_id) is not str or not _REVIEW_ID.fullmatch(review_id):
        raise RangeReviewError("invalid range review id")
    expected_count = manifest["chunkCount"]
    structurally_complete = len(chunk_records) == expected_count and all(
        type(record) is dict
        and set(record) == _AGGREGATE_CHUNK_FIELDS
        and record.get("index") == index
        and record.get("chunkId") == manifest["chunks"][index].get("patchSha256")
        and record.get("patchSha256") == manifest["chunks"][index].get("patchSha256")
        and record.get("reviewId") == f"{review_id}.chunk-{index}"
        and type(record.get("receipt")) is str
        and bool(record.get("receipt"))
        and _is_sha256(record.get("receiptFileSha256"))
        and _is_sha256(record.get("receiptSha256"))
        and record.get("result") == "accepted"
        and type(record.get("verdict")) is str
        and type(record.get("findings")) is list
        for index, record in enumerate(chunk_records)
    )
    complete = structurally_complete and callable(receipt_loader) and callable(receipt_validator)
    findings: list[dict[str, Any]] = []
    for record in chunk_records:
        if type(record) is not dict or type(record.get("findings")) is not list:
            continue
        for finding in record["findings"]:
            findings.append({"chunkIndex": record.get("index"), "finding": finding})
    if complete:
        verdict = "approved" if not findings else "changes-requested"
        aggregate = {"approved": not findings, "verdict": verdict, "findings": findings}
        result = "accepted"
    else:
        aggregate = {"approved": False, "verdict": None, "findings": findings}
        result = "incomplete"
    unsigned: dict[str, Any] = {
        "rangeReviewSchemaVersion": RANGE_REVIEW_SCHEMA_VERSION,
        "result": result,
        "reviewId": review_id,
        "evidenceStatus": "formal",
        "manifestSha256": manifest_sha256(manifest),
        "range": range_identity(manifest),
        "chunks": chunk_records,
        "aggregate": aggregate,
    }
    unsigned["sha256"] = _canonical_digest(unsigned)
    if unsigned["sha256"] is None:  # pragma: no cover - canonical JSON is required above
        raise RangeReviewError("could not digest range aggregate")
    if complete:
        violations = verify_range_aggregate(
            manifest,
            unsigned,
            receipt_loader,
            receipt_validator,
        )
        if violations:
            complete = False
            aggregate = {"approved": False, "verdict": None, "findings": findings}
            unsigned["result"] = "incomplete"
            unsigned["aggregate"] = aggregate
            unsigned["sha256"] = _canonical_digest(unsigned)
            if unsigned["sha256"] is None:  # pragma: no cover - canonical JSON is required above
                raise RangeReviewError("could not digest range aggregate")
    return unsigned


def _receipt_digest(receipt: object) -> str | None:
    if type(receipt) is not dict:
        return None
    recorded = receipt.get("sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "sha256"}
    digest = _canonical_digest(unsigned)
    return recorded if type(recorded) is str and recorded == digest else None


def _same_json(left: object, right: object) -> bool:
    try:
        return contract_canonical_json(left) == contract_canonical_json(right)
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        return False


def verify_range_aggregate(
    manifest: object,
    aggregate: object,
    receipt_loader: Callable[[str], tuple[bytes, object] | None],
    receipt_validator: Callable[[object], Iterable[str]],
) -> tuple[str, ...]:
    """Verify range identity, chunk coverage, and every referenced receipt.

    Any missing, duplicated, reordered, changed, or mixed chunk causes a
    violation.  A formal aggregate is valid only when every manifest chunk has
    a valid accepted receipt for the exact frozen patch bytes.
    """
    violations: list[str] = []

    def reject(code: str) -> None:
        if code not in violations:
            violations.append(code)

    manifest_violations = validate_range_manifest(manifest)
    for violation in manifest_violations:
        reject(violation)
    if manifest_violations or type(manifest) is not dict:
        return tuple(violations)
    if type(aggregate) is not dict:
        return ("aggregate-object",)
    if set(aggregate) - _AGGREGATE_FIELDS:
        reject("aggregate-fields")
    recorded_digest = aggregate.get("sha256")
    unsigned = {key: value for key, value in aggregate.items() if key != "sha256"}
    if not _is_sha256(recorded_digest) or recorded_digest != _canonical_digest(unsigned):
        reject("aggregate-digest")
    if aggregate.get("rangeReviewSchemaVersion") != RANGE_REVIEW_SCHEMA_VERSION:
        reject("aggregate-schema-version")
    review_id = aggregate.get("reviewId")
    if type(review_id) is not str or not _REVIEW_ID.fullmatch(review_id):
        reject("aggregate-review-id")
    if aggregate.get("evidenceStatus") != "formal":
        reject("aggregate-evidence-status")
    expected_manifest_sha = manifest_sha256(manifest)
    if aggregate.get("manifestSha256") != expected_manifest_sha:
        reject("manifest-digest")
    if not _same_json(aggregate.get("range"), range_identity(manifest)):
        reject("range-identity")

    manifest_chunks = manifest.get("chunks")
    aggregate_chunks = aggregate.get("chunks")
    if type(manifest_chunks) is not list or type(aggregate_chunks) is not list:
        reject("chunk-count")
        return tuple(violations)
    expected_count = len(manifest_chunks)
    if expected_count == 0:
        reject("range-empty")
    if len(aggregate_chunks) != expected_count:
        reject("chunk-count")
    indexes = [chunk.get("index") if type(chunk) is dict else None for chunk in aggregate_chunks]
    if indexes != list(range(len(aggregate_chunks))):
        reject("chunk-order")
    seen_indexes: set[int] = set()
    all_receipts_valid = len(aggregate_chunks) == expected_count
    normalized_findings: list[dict[str, Any]] = []
    for record in aggregate_chunks:
        if type(record) is not dict:
            reject("chunk-object")
            all_receipts_valid = False
            continue
        if set(record) != _AGGREGATE_CHUNK_FIELDS:
            reject("chunk-fields")
            all_receipts_valid = False
        index = record.get("index")
        if type(index) is not int or isinstance(index, bool) or index in seen_indexes:
            reject("chunk-order")
            all_receipts_valid = False
        else:
            seen_indexes.add(index)
        if type(index) is not int or not 0 <= index < expected_count:
            reject("chunk-index")
            all_receipts_valid = False
            continue
        manifest_chunk = manifest_chunks[index]
        patch_sha = manifest_chunk.get("patchSha256")
        if record.get("chunkId") != patch_sha or record.get("patchSha256") != patch_sha:
            reject("chunk-id")
            all_receipts_valid = False
        expected_chunk_review_id = f"{review_id}.chunk-{index}"
        if record.get("reviewId") != expected_chunk_review_id:
            reject("chunk-review-id")
            all_receipts_valid = False
        receipt_path = record.get("receipt")
        if type(receipt_path) is not str or not receipt_path:
            reject("receipt-missing")
            all_receipts_valid = False
            continue
        try:
            loaded = receipt_loader(receipt_path)
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            loaded = None
        if loaded is None:
            reject("receipt-missing")
            all_receipts_valid = False
            continue
        raw_receipt, receipt = loaded
        if type(raw_receipt) is not bytes:
            reject("receipt-bytes")
            all_receipts_valid = False
        if not _is_sha256(record.get("receiptFileSha256")) or (
            type(raw_receipt) is bytes
            and hashlib.sha256(raw_receipt).hexdigest() != record.get("receiptFileSha256")
        ):
            reject("receipt-file-digest")
            all_receipts_valid = False
        receipt_digest = _receipt_digest(receipt)
        if not _is_sha256(record.get("receiptSha256")) or receipt_digest != record.get(
            "receiptSha256"
        ):
            reject("receipt-digest")
            all_receipts_valid = False
        if type(receipt) is not dict:
            reject("receipt-object")
            all_receipts_valid = False
            continue
        if receipt.get("reviewId") != expected_chunk_review_id:
            reject("receipt-review-id")
            all_receipts_valid = False
        expected_context = {
            "rangeReviewSchemaVersion": RANGE_REVIEW_SCHEMA_VERSION,
            "manifestSha256": expected_manifest_sha,
            "range": range_identity(manifest),
            "chunkIndex": index,
            "chunkCount": expected_count,
            "chunkId": patch_sha,
        }
        if not _same_json(receipt.get("extension.rangeReview"), expected_context):
            reject("receipt-range-identity")
            all_receipts_valid = False
        if receipt.get("result") != "accepted":
            reject("receipt-result")
            all_receipts_valid = False
        source = receipt.get("source")
        source_files = source.get("files") if type(source) is dict else None
        if type(source_files) is not list or len(source_files) != 1:
            reject("receipt-source")
            all_receipts_valid = False
        else:
            source_file = source_files[0]
            if (
                type(source_file) is not dict
                or source_file.get("sha256") != patch_sha
                or source_file.get("name") != f"chunk-{index:04d}.patch"
            ):
                reject("receipt-source")
                all_receipts_valid = False
        for violation in receipt_validator(receipt):
            reject(f"receipt-{violation}")
            all_receipts_valid = False
        if record.get("result") != receipt.get("result"):
            reject("chunk-result")
            all_receipts_valid = False
        if record.get("verdict") != receipt.get("verdict"):
            reject("chunk-verdict")
            all_receipts_valid = False
        receipt_findings = receipt.get("findings")
        if not _same_json(record.get("findings"), receipt_findings):
            reject("chunk-findings")
            all_receipts_valid = False
        if type(receipt_findings) is list:
            normalized_findings.extend(
                {"chunkIndex": index, "finding": finding} for finding in receipt_findings
            )

    expected_result = "accepted" if all_receipts_valid else "incomplete"
    if aggregate.get("result") != expected_result:
        reject("aggregate-result")
    aggregate_section = aggregate.get("aggregate")
    if type(aggregate_section) is not dict or set(aggregate_section) != {
        "approved",
        "verdict",
        "findings",
    }:
        reject("aggregate-section")
    else:
        expected_approved = all_receipts_valid and not normalized_findings
        expected_verdict = (
            "approved"
            if expected_approved
            else "changes-requested"
            if all_receipts_valid
            else None
        )
        if aggregate_section.get("approved") is not expected_approved:
            reject("aggregate-approval")
        if aggregate_section.get("verdict") != expected_verdict:
            reject("aggregate-verdict")
        if not _same_json(aggregate_section.get("findings"), normalized_findings):
            reject("aggregate-findings")
    if aggregate.get("result") == "incomplete":
        reject("range-incomplete")
    return tuple(violations)
