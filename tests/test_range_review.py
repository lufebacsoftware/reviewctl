from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from reviewctl import range_review
from reviewctl.range_review import (
    RangeReviewError,
    build_range_aggregate,
    build_range_manifest,
    manifest_sha256,
    range_identity,
    verify_range_aggregate,
)


def make_fake_receipt(
    path: Path,
    review_id: str,
    chunk: dict[str, object],
    manifest: dict[str, object],
) -> dict[str, object]:
    receipt = {
        "reviewId": review_id,
        "result": "accepted",
        "verdict": "approved",
        "findings": [],
        "source": {
            "files": [
                {
                    "name": f"chunk-{chunk['index']:04d}.patch",
                    "path": str(path),
                    "sha256": chunk["patchSha256"],
                }
            ]
        },
        "extension.rangeReview": {
            "rangeReviewSchemaVersion": 1,
            "manifestSha256": manifest_sha256(manifest),
            "range": range_identity(manifest),
            "chunkIndex": chunk["index"],
            "chunkCount": manifest["chunkCount"],
            "chunkId": chunk["patchSha256"],
        },
    }
    receipt["sha256"] = hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    raw = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
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

    aggregate = build_range_aggregate(manifest, "range", records)

    assert aggregate["result"] == "accepted"
    assert aggregate["aggregate"] == {
        "approved": True,
        "verdict": "approved",
        "findings": [],
    }
    assert verify_range_aggregate(
        manifest,
        aggregate,
        lambda path: (Path(path).read_bytes(), receipts[path]),
    ) == ()


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
        receipt = make_fake_receipt(
            receipt_path, f"range.chunk-{chunk['index']}", chunk, manifest
        )
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
    aggregate = build_range_aggregate(manifest, "range", records)
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
    aggregate = build_range_aggregate(manifest, "range", [record])
    receipt_path.write_bytes(receipt_path.read_bytes() + b"tampered")

    assert "receipt-file-digest" in verify_range_aggregate(
        manifest,
        aggregate,
        lambda path: (Path(path).read_bytes(), receipt),
    )


def test_range_aggregate_does_not_turn_an_empty_range_into_approval(tmp_path: Path) -> None:
    repository, base, _ = make_repository(tmp_path)
    manifest = build_range_manifest(repository, base, base, allow_empty=True)
    aggregate = build_range_aggregate(manifest, "empty-range", [])

    assert "range-empty" in verify_range_aggregate(manifest, aggregate, lambda _: None)
