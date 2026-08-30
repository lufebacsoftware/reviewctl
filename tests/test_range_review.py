from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from reviewctl import range_review
from reviewctl.contracts import ContractContext, canonical_json, get_contract
from reviewctl.range_review import (
    RangeReviewError,
    build_range_aggregate,
    build_range_manifest,
    manifest_sha256,
    range_identity,
    verify_range_aggregate,
)
from reviewctl.review_flow import consolidate


def make_fake_receipt(
    path: Path,
    review_id: str,
    chunk: dict[str, object],
    manifest: dict[str, object],
) -> dict[str, object]:
    filename = f"chunk-{chunk['index']:04d}.patch"
    context = ContractContext(file_names=(filename,))
    contract = get_contract("findings-json")
    payload = json.dumps({"verdict": "approved", "findings": []})
    evaluation = contract.evaluate(payload, contract.prepare(context), context)
    evaluation_dict = {
        "name": evaluation.name,
        "version": evaluation.version,
        "preparedSha256": evaluation.prepared_digest,
        "payloadSha256": evaluation.payload_digest,
        "normalizedSha256": evaluation.normalized_digest,
        "normalizedValue": evaluation.value,
        "contractContext": {
            "fileNames": [filename],
            "reviewDeclarationRequired": False,
        },
        "violations": list(evaluation.violations),
        "status": evaluation.status.value,
        "fragments": [],
        "coverage": {
            "requiredFields": list(evaluation.coverage.required_fields),
            "coveredFields": list(evaluation.coverage.covered_fields),
            "missingFields": list(evaluation.coverage.missing_fields),
        },
        "completionRequest": None,
    }
    receipt = {
        "receiptSchemaVersion": 2,
        "reviewId": review_id,
        "result": "accepted",
        "acceptedAttempt": 1,
        "sourceClass": "synthetic",
        "verdict": "approved",
        "findings": [],
        "source": {
            "files": [
                {
                    "name": filename,
                    "path": str(path),
                    "sha256": chunk["patchSha256"],
                }
            ]
        },
        "transport": "llm",
        "reviewContract": "findings-json",
        "contract": {"name": "findings-json", "version": "1"},
        "prompt": {"packetSha256": "b" * 64},
        "routes": [{"model": "model", "transport": "llm"}],
        "attempts": [
            {
                "number": 1,
                "routeIndex": 0,
                "route": {"model": "model", "transport": "llm"},
                "transport": "llm",
                "model": {"requested": "model", "resolved": "model"},
                "result": "accepted",
                "rawResponse": {
                    "path": "attempts/01/raw-response.txt",
                    "sha256": evaluation.payload_digest,
                    "characters": len(payload),
                },
                "contractEvaluation": evaluation_dict,
                "promotedFragments": [],
                "findings": [],
            }
        ],
        "fallbackRelationships": [],
        "consolidatedReview": consolidate(
            {"verdict": "approved", "findings": []},
            (),
            1,
            contract_context=context,
        ).to_dict(),
        "extension.rangeReview": {
            "rangeReviewSchemaVersion": 1,
            "manifestSha256": manifest_sha256(manifest),
            "range": range_identity(manifest),
            "chunkIndex": chunk["index"],
            "chunkCount": manifest["chunkCount"],
            "chunkId": chunk["patchSha256"],
        },
    }
    receipt["sha256"] = hashlib.sha256(canonical_json(receipt)).hexdigest()
    raw = canonical_json(receipt)
    path.write_bytes(raw + b"\n")
    return receipt


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
        ("diff.suppressBlankEmpty", "true"),
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


def test_manifest_is_insensitive_to_diff_driver_hunk_headers(tmp_path: Path) -> None:
    repository = tmp_path / "driver-repository"
    repository.mkdir()
    git(repository, "init", "--quiet")
    git(repository, "config", "user.email", "reviewctl@example.invalid")
    git(repository, "config", "user.name", "reviewctl tests")
    (repository / ".gitattributes").write_text("source.txt diff=custom\n")
    (repository / "source.txt").write_text("FUNC alpha\n" + "line\n" * 12)
    git(repository, "add", ".gitattributes", "source.txt")
    git(repository, "commit", "--quiet", "-m", "attributes")
    base = git(repository, "rev-parse", "HEAD")
    lines = (repository / "source.txt").read_text().splitlines()
    lines[8] = "changed"
    (repository / "source.txt").write_text("\n".join(lines) + "\n")
    git(repository, "add", "source.txt")
    git(repository, "commit", "--quiet", "-m", "hunk")
    head = git(repository, "rev-parse", "HEAD")
    baseline = build_range_manifest(repository, base, head, max_chunk_bytes=4096)
    git(repository, "config", "diff.custom.xfuncname", "^FUNC")

    configured = build_range_manifest(repository, base, head, max_chunk_bytes=4096)

    assert configured == baseline


def test_bounded_git_capture_keeps_only_limit_plus_one_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeStream:
        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self, _size: int = -1) -> bytes:
            payload, self.payload = self.payload, b""
            return payload

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStream(b"x" * 1024)
            self.stderr = FakeStream(b"")
            self.returncode = None
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self) -> int:
            self.returncode = self.returncode or 0
            return self.returncode

    process = FakeProcess()

    def fake_popen(command: list[str], *, stdout, stderr):
        assert stdout is subprocess.PIPE
        assert stderr is subprocess.PIPE
        return process

    monkeypatch.setattr(range_review.subprocess, "Popen", fake_popen)

    result = range_review._run_git_bounded(tmp_path, "diff", max_stdout_bytes=32)

    assert len(result.stdout) == 33
    assert process.killed


def test_bounded_git_capture_handles_second_read_and_process_exit_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class QueueStream:
        def __init__(self, payloads: list[bytes]):
            self.payloads = payloads

        def read(self, _size: int = -1) -> bytes:
            return self.payloads.pop(0) if self.payloads else b""

        def close(self) -> None:
            return None

    class RaceProcess:
        def __init__(self):
            self.stdout = QueueStream([b"x" * 33, b"y"])
            self.stderr = QueueStream([b"diagnostic"])
            self.returncode = None

        def kill(self) -> None:
            raise ProcessLookupError

        def wait(self) -> int:
            self.returncode = -9
            return self.returncode

    process = RaceProcess()
    monkeypatch.setattr(
        range_review.subprocess,
        "Popen",
        lambda command, *, stdout, stderr: process,
    )

    result = range_review._run_git_bounded(tmp_path, "diff", max_stdout_bytes=32)

    assert len(result.stdout) == 33
    assert result.stderr == b"diagnostic"


def test_bounded_git_capture_caps_stderr_after_multiple_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stream:
        def __init__(self, values: list[bytes]):
            self.values = values

        def read(self, _size: int = -1) -> bytes:
            return self.values.pop(0) if self.values else b""

        def close(self) -> None:
            return None

    class Process:
        stdout = Stream([b""])
        stderr = Stream([b"x" * (range_review.MAX_GIT_STDERR_BYTES + 1), b"y"])
        returncode = 0

        def wait(self) -> int:
            return self.returncode

    monkeypatch.setattr(
        range_review.subprocess,
        "Popen",
        lambda command, *, stdout, stderr: Process(),
    )

    result = range_review._run_git_bounded(tmp_path, "diff", max_stdout_bytes=32)

    assert len(result.stderr) == range_review.MAX_GIT_STDERR_BYTES + 1


def test_bounded_git_capture_rejects_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_stdout_bytes must be positive"):
        range_review._run_git_bounded(tmp_path, "diff", max_stdout_bytes=0)


def test_manifest_validator_rejects_malformed_identity_and_chunks(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)
    valid = build_range_manifest(repository, base, head, max_chunk_bytes=300)

    malformed: list[tuple[object, str]] = [(None, "manifest-object")]
    for key, value, code in (
        ("unexpected", True, "manifest-fields"),
        ("schemaVersion", 2, "manifest-schema-version"),
        ("status", "formal", "manifest-status"),
        ("evidenceStatus", "formal", "manifest-evidence-status"),
        ("repository", "", "manifest-repository"),
        ("baseSha", "bad", "manifest-base-sha"),
        ("headSha", "bad", "manifest-head-sha"),
        ("mergeBaseSha", "bad", "manifest-merge-base-sha"),
        ("comparison", "other", "manifest-comparison"),
        ("contextLines", 0, "manifest-context"),
        ("chunkingVersion", "other", "manifest-chunking-version"),
        ("canonicalDiffSha256", "bad", "manifest-diff-sha"),
        ("chunks", {}, "manifest-chunks"),
        ("chunkCount", -1, "manifest-chunk-count"),
        ("chunkCount", 1, "manifest-chunk-count"),
    ):
        candidate = deepcopy(valid)
        candidate[key] = value
        malformed.append((candidate, code))
    for candidate, expected in malformed:
        assert expected in range_review.validate_range_manifest(candidate)

    chunk = valid["chunks"][0]
    candidates = [
        (["not-a-chunk"], "chunk-object"),
        ([{**chunk, "extra": True}], "chunk-fields"),
        ([{**chunk, "index": -1}], "chunk-index"),
        ([{**chunk, "patch": "not-base64"}], "chunk-payload"),
        ([{**chunk, "patchSha256": "bad"}], "chunk-sha"),
        ([{**chunk, "patchSha256": "0" * 64}], "chunk-sha"),
        ([{**chunk, "byteLength": 0}], "chunk-length"),
        ([{**chunk, "paths": []}], "chunk-paths"),
        ([{**chunk, "paths": ["same", "same"]}], "chunk-paths"),
        ([{**chunk, "fileCount": 0}], "chunk-file-count"),
        ([{**chunk, "index": 1}], "chunk-order"),
        ([{**chunk, "paths": ["README.md"]}], "manifest-diff-sha"),
    ]
    for chunks, expected in candidates:
        candidate = deepcopy(valid)
        candidate["chunks"] = chunks
        candidate["chunkCount"] = len(chunks)
        assert expected in range_review.validate_range_manifest(candidate)

    overlap = deepcopy(valid)
    overlap["chunks"][1]["paths"] = overlap["chunks"][0]["paths"]
    overlap["chunks"][1]["fileCount"] = len(overlap["chunks"][1]["paths"])
    assert "chunk-overlap" in range_review.validate_range_manifest(overlap)


def test_range_validator_and_digest_helpers_cover_unusual_inputs(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, head)
    assert range_review.validate_range_manifest(manifest) == ()
    assert range_review._manifest_chunk_payload(None) is None
    assert range_review._manifest_chunk_payload({"patch": 1}) is None
    assert range_review._manifest_chunk_payload({"patch": "@@@"}) is None
    assert range_review._manifest_chunk_payload({"patch": "YQ"}) is None
    original_b64encode = range_review.base64.b64encode
    range_review.base64.b64encode = lambda _value: b"different"  # type: ignore[assignment]
    try:
        assert range_review._manifest_chunk_payload({"patch": "YQ=="}) is None
    finally:
        range_review.base64.b64encode = original_b64encode  # type: ignore[assignment]
    assert range_review._canonical_digest({1: "unsupported"}) is None
    with pytest.raises(RangeReviewError, match="not canonical JSON"):
        range_review.manifest_sha256({1: "unsupported"})  # type: ignore[dict-item]
    assert (
        range_review._git_error(subprocess.CompletedProcess([], 7, b"", b""))
        == "git exited with status 7"
    )


def test_range_git_edge_errors_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base, head = make_repository(tmp_path)
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("x")
    with pytest.raises(RangeReviewError, match="repository is not a directory"):
        build_range_manifest(file_path, base, head)
    for revision in ("", "-bad"):
        with pytest.raises(RangeReviewError, match="invalid revision"):
            range_review._resolve_revision(repository, revision, "base")
    with pytest.raises(RangeReviewError, match="max_chunk_bytes must be positive"):
        build_range_manifest(repository, base, head, max_chunk_bytes=0)
    with pytest.raises(RangeReviewError, match="canonical diff has no file sections"):
        range_review._diff_sections(b"not-a-diff")
    with pytest.raises(RangeReviewError, match="path count"):
        range_review._chunks([b"x"], [], 10)
    assert len(range_review._chunks([b"x", b"y"], ["same", "same"], 10)[0]["paths"]) == 1

    def fake_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 1, b"", b"failure")

    monkeypatch.setattr(range_review, "_run_git", fake_git)
    with pytest.raises(RangeReviewError, match="not a Git repository"):
        range_review._repository_root(repository)

    def invalid_root_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"\xff", b"")

    monkeypatch.setattr(range_review, "_run_git", invalid_root_git)
    with pytest.raises(RangeReviewError, match="not valid UTF-8"):
        range_review._repository_root(repository)

    def invalid_revision_git(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"invalid", b"")

    monkeypatch.setattr(range_review, "_run_git", invalid_revision_git)
    with pytest.raises(RangeReviewError, match="invalid object id"):
        range_review._resolve_revision(repository, "HEAD", "head")

    def merge_base_status(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 2, b"", b"merge failure")

    monkeypatch.setattr(range_review, "_run_git", merge_base_status)
    with pytest.raises(RangeReviewError, match="could not compute merge base"):
        range_review._merge_base(repository, base, head)

    def disconnected(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 1, b"", b"")

    monkeypatch.setattr(range_review, "_run_git", disconnected)
    assert range_review._merge_base(repository, base, head) is None

    def merge_base_invalid(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"invalid", b"")

    monkeypatch.setattr(range_review, "_run_git", merge_base_invalid)
    with pytest.raises(RangeReviewError, match="invalid object id"):
        range_review._merge_base(repository, base, head)


def test_range_diff_and_aggregate_edge_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base, head = make_repository(tmp_path)
    valid = build_range_manifest(repository, base, head)

    def failed_capture(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 2, b"", b"diff failure")

    monkeypatch.setattr(range_review, "_run_git_bounded", failed_capture)
    with pytest.raises(RangeReviewError, match="could not compute canonical diff"):
        range_review._canonical_diff(repository, base, head, 3)
    with pytest.raises(RangeReviewError, match="could not capture canonical paths"):
        range_review._canonical_paths(repository, base, head)

    def oversized_capture(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"x" * (range_review.MAX_DIFF_BYTES + 1), b"")

    monkeypatch.setattr(range_review, "_run_git_bounded", oversized_capture)
    with pytest.raises(RangeReviewError, match="canonical diff exceeds"):
        range_review._canonical_diff(repository, base, head, 3)

    with pytest.raises(RangeReviewError, match="invalid range manifest"):
        range_review.build_range_aggregate({}, "range", [])
    with pytest.raises(RangeReviewError, match="invalid range review id"):
        range_review.build_range_aggregate(valid, "bad/id", [])
    assert range_review._receipt_digest([]) is None
    assert not range_review._same_json({1: "bad"}, {1: "bad"})

    incomplete_record = {"result": "unavailable", "findings": None}
    assert (
        range_review.build_range_aggregate(valid, "incomplete", [incomplete_record])["result"]
        == "incomplete"
    )
    assert (
        range_review.build_range_aggregate(
            valid, "range", [{"result": "accepted", "findings": []}]
        )["result"]
        == "incomplete"
    )


def test_range_aggregate_verifies_every_chunk_and_receipt_identity(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=300)
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    records = []
    receipts: dict[str, dict[str, object]] = {}
    for chunk in manifest["chunks"]:
        receipt_path = receipt_dir / f"receipt-{chunk['index']}.json"
        receipt = make_fake_receipt(
            receipt_path,
            f"range.chunk-{chunk['index']}",
            chunk,
            manifest,
        )
        receipts[str(receipt_path)] = receipt
        records.append(
            {
                "index": chunk["index"],
                "chunkId": chunk["patchSha256"],
                "patchSha256": chunk["patchSha256"],
                "reviewId": f"range.chunk-{chunk['index']}",
                "receipt": str(receipt_path),
                "receiptFileSha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                "receiptSha256": receipt["sha256"],
                "result": receipt["result"],
                "verdict": receipt["verdict"],
                "findings": receipt["findings"],
            }
        )

    aggregate = build_range_aggregate(
        manifest,
        "range",
        records,
        receipt_loader=lambda path: (Path(path).read_bytes(), receipts[path]),
        receipt_validator=lambda _: (),
    )

    assert aggregate["result"] == "accepted"
    assert aggregate["aggregate"] == {
        "approved": True,
        "verdict": "approved",
        "findings": [],
    }
    assert (
        verify_range_aggregate(
            manifest,
            aggregate,
            lambda path: (Path(path).read_bytes(), receipts[path]),
            lambda _: (),
        )
        == ()
    )
    unverified = build_range_aggregate(
        manifest,
        "range",
        records,
        receipt_loader=lambda path: (Path(path).read_bytes(), receipts[path]),
        receipt_validator=lambda _: ("invalid",),
    )
    assert unverified["result"] == "incomplete"
    minimal = {
        "reviewId": "range.chunk-0",
        "result": "accepted",
        "verdict": "approved",
        "findings": [],
    }
    forged = build_range_aggregate(
        manifest,
        "range",
        records,
        receipt_loader=lambda path: (canonical_json(minimal), minimal),
        receipt_validator=lambda _: (),
    )
    assert forged["result"] == "incomplete"


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda records: records.pop(), "chunk-count"),
        (lambda records: records.reverse(), "chunk-order"),
        (lambda records: records.__setitem__(0, {**records[0], "chunkId": "f" * 64}), "chunk-id"),
    ],
)
def test_range_aggregate_rejects_missing_reordered_or_mixed_chunks(
    tmp_path: Path, mutation: object, expected: str
) -> None:
    repository, base, head = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=300)
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    records = []
    for chunk in manifest["chunks"]:
        receipt_path = receipt_dir / f"receipt-{chunk['index']}.json"
        receipt = make_fake_receipt(receipt_path, f"range.chunk-{chunk['index']}", chunk, manifest)
        records.append(
            {
                "index": chunk["index"],
                "chunkId": chunk["patchSha256"],
                "patchSha256": chunk["patchSha256"],
                "reviewId": f"range.chunk-{chunk['index']}",
                "receipt": str(receipt_path),
                "receiptFileSha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                "receiptSha256": receipt["sha256"],
                "result": receipt["result"],
                "verdict": receipt["verdict"],
                "findings": receipt["findings"],
            }
        )
    aggregate = build_range_aggregate(
        manifest,
        "range",
        records,
        receipt_loader=lambda path: (Path(path).read_bytes(), json.loads(Path(path).read_text())),
        receipt_validator=lambda _: (),
    )
    mutation(aggregate["chunks"])
    aggregate["sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in aggregate.items() if key != "sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    violations = verify_range_aggregate(
        manifest,
        aggregate,
        lambda path: (Path(path).read_bytes(), json.loads(Path(path).read_text())),
        lambda _: (),
    )

    assert expected in violations


def test_range_aggregate_fails_closed_when_receipt_file_changes(tmp_path: Path) -> None:
    repository, base, head = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=4096)
    receipt_path = tmp_path / "receipt.json"
    chunk = manifest["chunks"][0]
    receipt = make_fake_receipt(receipt_path, "range.chunk-0", chunk, manifest)
    record = {
        "index": 0,
        "chunkId": chunk["patchSha256"],
        "patchSha256": chunk["patchSha256"],
        "reviewId": "range.chunk-0",
        "receipt": str(receipt_path),
        "receiptFileSha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "receiptSha256": receipt["sha256"],
        "result": receipt["result"],
        "verdict": receipt["verdict"],
        "findings": receipt["findings"],
    }
    aggregate = build_range_aggregate(
        manifest,
        "range",
        [record],
        receipt_loader=lambda path: (Path(path).read_bytes(), receipt),
        receipt_validator=lambda _: (),
    )
    receipt_path.write_bytes(receipt_path.read_bytes() + b"tampered")

    assert "receipt-file-digest" in verify_range_aggregate(
        manifest,
        aggregate,
        lambda path: (Path(path).read_bytes(), receipt),
        lambda _: (),
    )


def test_range_aggregate_does_not_turn_an_empty_range_into_approval(tmp_path: Path) -> None:
    repository, base, _ = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, base, allow_empty=True)
    aggregate = build_range_aggregate(manifest, "empty-range", [])

    assert "range-empty" in verify_range_aggregate(
        manifest, aggregate, lambda _: None, lambda _: ()
    )


def test_range_aggregate_rejects_every_identity_and_receipt_corruption_shape(
    tmp_path: Path,
) -> None:
    repository, base, head = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, head, max_chunk_bytes=4096)
    receipt_path = tmp_path / "aggregate-receipt.json"
    chunk = manifest["chunks"][0]
    receipt = make_fake_receipt(receipt_path, "range.chunk-0", chunk, manifest)
    record = {
        "index": 0,
        "chunkId": chunk["patchSha256"],
        "patchSha256": chunk["patchSha256"],
        "reviewId": "range.chunk-0",
        "receipt": str(receipt_path),
        "receiptFileSha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "receiptSha256": receipt["sha256"],
        "result": receipt["result"],
        "verdict": receipt["verdict"],
        "findings": receipt["findings"],
    }
    baseline = build_range_aggregate(
        manifest,
        "range",
        [record],
        receipt_loader=lambda path: (Path(path).read_bytes(), receipt),
        receipt_validator=lambda _: (),
    )
    raw = receipt_path.read_bytes()

    def redigest(aggregate: dict[str, object]) -> None:
        aggregate["sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in aggregate.items() if key != "sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def run(
        aggregate: object,
        *,
        loaded: object = receipt,
        raw_value: object = raw,
        validator: object = None,
    ) -> tuple[str, ...]:
        def loader(_path: str) -> tuple[bytes, object] | None:
            if isinstance(loaded, BaseException):
                raise loaded
            if loaded == "__missing__":
                return None
            return raw_value, loaded  # type: ignore[return-value]

        return verify_range_aggregate(
            manifest,
            aggregate,
            loader,
            receipt_validator=validator or (lambda _: ()),  # type: ignore[arg-type]
        )

    assert "manifest-object" in verify_range_aggregate(None, baseline, lambda _: None, lambda _: ())
    assert "aggregate-object" in run(None)

    aggregate_mutations = [
        (lambda value: value.update({"extra": True}), "aggregate-fields"),
        (lambda value: value.update({"sha256": "bad"}), "aggregate-digest"),
        (lambda value: value.update({"rangeReviewSchemaVersion": 2}), "aggregate-schema-version"),
        (lambda value: value.update({"reviewId": "bad/id"}), "aggregate-review-id"),
        (
            lambda value: value.update({"evidenceStatus": "planning-only"}),
            "aggregate-evidence-status",
        ),
        (lambda value: value.update({"manifestSha256": "f" * 64}), "manifest-digest"),
        (lambda value: value.update({"range": {}}), "range-identity"),
    ]
    for mutation, expected in aggregate_mutations:
        candidate = deepcopy(baseline)
        mutation(candidate)
        if expected != "aggregate-digest":
            redigest(candidate)
        assert expected in run(candidate)

    chunks_candidate = deepcopy(baseline)
    chunks_candidate["chunks"] = {}
    redigest(chunks_candidate)
    assert "chunk-count" in run(chunks_candidate)

    record_mutations = [
        (lambda value: value["chunks"].__setitem__(0, None), "chunk-object"),
        (
            lambda value: value["chunks"][0].update({"extra": True}),
            "chunk-fields",
        ),
        (
            lambda value: value["chunks"][0].update({"index": 99}),
            "chunk-index",
        ),
        (
            lambda value: value["chunks"][0].update({"reviewId": "other"}),
            "chunk-review-id",
        ),
        (
            lambda value: value["chunks"][0].update({"receipt": ""}),
            "receipt-missing",
        ),
        (
            lambda value: value["chunks"][0].update({"receiptFileSha256": "bad"}),
            "receipt-file-digest",
        ),
        (
            lambda value: value["chunks"][0].update({"receiptSha256": "bad"}),
            "receipt-digest",
        ),
        (
            lambda value: value["chunks"][0].update({"result": "unavailable"}),
            "chunk-result",
        ),
        (
            lambda value: value["chunks"][0].update({"verdict": "changes-requested"}),
            "chunk-verdict",
        ),
        (
            lambda value: value["chunks"][0].update({"findings": ["different"]}),
            "chunk-findings",
        ),
    ]
    for mutation, expected in record_mutations:
        candidate = deepcopy(baseline)
        mutation(candidate)
        redigest(candidate)
        assert expected in run(candidate)

    assert "receipt-missing" in run(baseline, loaded="__missing__")
    assert "receipt-missing" in run(baseline, loaded=ValueError("bad loader"))
    assert "receipt-bytes" in run(baseline, raw_value="not bytes")
    assert "receipt-object" in run(baseline, loaded=[])

    receipt_mutations = [
        (lambda value: value.update({"reviewId": "other"}), "receipt-review-id"),
        (lambda value: value.update({"extension.rangeReview": {}}), "receipt-range-identity"),
        (lambda value: value.update({"result": "unavailable"}), "receipt-result"),
        (lambda value: value.update({"source": {}}), "receipt-source"),
    ]
    for mutation, expected in receipt_mutations:
        changed = deepcopy(receipt)
        mutation(changed)
        assert expected in run(baseline, loaded=changed)
    changed_findings = deepcopy(receipt)
    changed_findings["findings"] = "not-a-list"
    assert "chunk-findings" in run(baseline, loaded=changed_findings)
    changed_source = deepcopy(receipt)
    changed_source["source"]["files"][0]["name"] = "other.patch"  # type: ignore[index]
    assert "receipt-source" in run(baseline, loaded=changed_source)
    assert "receipt-invalid" in run(baseline, validator=lambda _: ("invalid",))

    malformed_section = deepcopy(baseline)
    malformed_section["aggregate"] = []
    redigest(malformed_section)
    assert "aggregate-section" in run(malformed_section)
    mismatched_findings = deepcopy(baseline)
    mismatched_findings["aggregate"]["findings"] = ["unexpected"]  # type: ignore[index]
    redigest(mismatched_findings)
    assert "aggregate-findings" in run(mismatched_findings)

    finding = {"severity": "low"}
    finding_receipt = deepcopy(receipt)
    finding_receipt["findings"] = [finding]
    finding_record = deepcopy(record)
    finding_record["findings"] = [finding]
    finding_aggregate = build_range_aggregate(
        manifest,
        "range",
        [finding_record],
        receipt_loader=lambda path: (Path(path).read_bytes(), finding_receipt),
        receipt_validator=lambda _: (),
    )
    assert "changes-requested" not in run(finding_aggregate, loaded=finding_receipt)

    multi_manifest = build_range_manifest(repository, base, head, max_chunk_bytes=300)
    multi_records = []
    multi_receipts = {}
    for multi_chunk in multi_manifest["chunks"]:
        multi_path = tmp_path / f"multi-{multi_chunk['index']}.json"
        multi_receipt = make_fake_receipt(
            multi_path,
            f"multi.chunk-{multi_chunk['index']}",
            multi_chunk,
            multi_manifest,
        )
        multi_receipts[str(multi_path)] = (multi_path.read_bytes(), multi_receipt)
        multi_records.append(
            {
                **record,
                "index": multi_chunk["index"],
                "chunkId": multi_chunk["patchSha256"],
                "patchSha256": multi_chunk["patchSha256"],
                "reviewId": f"multi.chunk-{multi_chunk['index']}",
                "receipt": str(multi_path),
                "receiptFileSha256": hashlib.sha256(multi_path.read_bytes()).hexdigest(),
                "receiptSha256": multi_receipt["sha256"],
            }
        )
    multi_aggregate = build_range_aggregate(
        multi_manifest,
        "multi",
        multi_records,
        receipt_loader=lambda path: multi_receipts[path],
        receipt_validator=lambda _: (),
    )
    multi_aggregate["chunks"][1]["index"] = 0
    multi_aggregate["chunks"][0]["chunkId"] = "f" * 64
    redigest(multi_aggregate)
    assert "chunk-order" in verify_range_aggregate(
        multi_manifest, multi_aggregate, lambda path: multi_receipts[path], lambda _: ()
    )
