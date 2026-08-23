from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from reviewctl.errors import JournalOperationError
from reviewctl.journal import ProjectJournal


def test_journal_appends_and_reads_events(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append({"type": "review_started", "reviewId": "r1"})
    journal.append({"type": "finding", "reviewId": "r1", "status": "open"})

    events = journal.events()

    assert [event["type"] for event in events] == ["review_started", "finding"]
    assert all(event["eventId"] for event in events)
    assert all(event["at"] for event in events)


def test_journal_never_rewrites_previous_events(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append({"type": "review_started", "reviewId": "r1"})
    before = journal.path.read_bytes()

    journal.append({"type": "review_finished", "reviewId": "r1"})

    assert journal.path.read_bytes().startswith(before)


def test_truncated_journal_line_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"type":"review_started"}\n{"type":')
    journal = ProjectJournal(path)

    events, diagnostic = journal.read_with_diagnostic()

    assert len(events) == 1
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"


def test_findings_projection_filters_status(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "one",
        }
    )
    journal.append(
        {
            "type": "finding",
            "reviewId": "r2",
            "findingId": "f2",
            "status": "fixed",
            "path": "src/b.py",
            "message": "two",
        }
    )

    assert [finding["findingId"] for finding in journal.findings(status="open")] == ["f1"]


def test_findings_projection_filters_and_unions_dimensions(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "one",
            "dimensions": ["security"],
        }
    )
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r2",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "one",
            "dimensions": ["architecture"],
        }
    )

    findings = journal.findings(dimension="security")

    assert len(findings) == 1
    assert findings[0]["dimensions"] == ["architecture", "security"]


def test_findings_projection_collapses_repeated_observations(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r2",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    findings = journal.findings()

    assert len(findings) == 1
    assert findings[0]["findingId"] == "f1"
    assert findings[0]["firstReviewId"] == "r1"
    assert findings[0]["lastReviewId"] == "r2"
    assert findings[0]["observations"] == 2


def test_finding_status_change_is_projected_without_rewriting_journal(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )
    before = journal.path.read_bytes()
    journal.append(
        {
            "type": "finding_status_changed",
            "findingId": "f1",
            "from": "open",
            "to": "fixed",
            "reason": "patched in commit abc123",
        }
    )

    finding = journal.findings()[0]
    assert finding["status"] == "fixed"
    assert finding["statusReason"] == "patched in commit abc123"
    assert journal.path.read_bytes().startswith(before)


def test_invalid_finding_status_transition_is_rejected(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    with pytest.raises(ValueError, match="invalid finding status transition"):
        journal.append(
            {
                "type": "finding_status_changed",
                "findingId": "f1",
                "from": "open",
                "to": "verified",
            }
        )


def test_projection_preserves_distinct_observation_variants(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    for review_id, severity, evidence in (
        ("r1", "medium", "first evidence"),
        ("r2", "high", "stronger evidence"),
    ):
        journal.append(
            {
                "type": "finding_observed",
                "reviewId": review_id,
                "findingId": "f1",
                "status": "open",
                "path": "src/a.py",
                "line": 10,
                "message": "Handle the error",
                "severity": severity,
                "evidence": evidence,
                "reproduction": "run the failing case",
            }
        )

    findings = journal.findings()

    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert len(findings[0]["observationVariants"]) == 2
    assert {variant["severity"] for variant in findings[0]["observationVariants"]} == {
        "medium",
        "high",
    }


def test_projection_reports_invalid_status_event_as_journal_corruption(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = ProjectJournal(path)
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            '{"type":"finding_status_changed","eventId":"e2",'
            '"at":"2026-08-23T12:00:00Z","findingId":"f1",'
            '"from":"open","to":"verified","reviewId":""}\n'
        )

    findings, diagnostic = journal.findings_with_diagnostic()

    assert findings == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"
    assert "open -> verified" in diagnostic.message


def test_new_events_have_identity_sequence_and_continuity(tmp_path: Path) -> None:
    journal = ProjectJournal(
        tmp_path / "journal.jsonl", project_id="project-1", origin_id="origin-1"
    )

    first = journal.append({"type": "review_started", "reviewId": "r1"})
    second = journal.append({"type": "review_finished", "reviewId": "r1"})

    assert first["schemaVersion"] == 1
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["projectId"] == second["projectId"] == "project-1"
    assert first["originId"] == second["originId"] == "origin-1"
    assert second["previousEventSha256"] == first["eventSha256"]
    assert journal.verify() == []


def test_journal_verify_reports_tamper_gap_and_identity_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = ProjectJournal(path, project_id="project-1", origin_id="origin-1")
    journal.append({"type": "review_started", "reviewId": "r1"})
    journal.append({"type": "review_finished", "reviewId": "r1"})

    lines = path.read_text().splitlines()
    second = json.loads(lines[1])
    second["sequence"] = 4
    second["projectId"] = "project-2"
    lines[1] = json.dumps(second, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    violations = journal.verify()

    assert any("sequence" in violation for violation in violations)
    assert any("project identity" in violation for violation in violations)
    assert any("event digest" in violation for violation in violations)


def test_versioned_event_extends_a_legacy_prefix_with_canonical_digest(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    legacy = {"type": "review_started", "reviewId": "legacy"}
    path.write_text(json.dumps(legacy) + "\n")
    journal = ProjectJournal(path, project_id="project-1", origin_id="origin-1")

    event = journal.append({"type": "review_finished", "reviewId": "new"})

    canonical = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    assert event["sequence"] == 1
    assert event["previousEventSha256"] == hashlib.sha256(canonical).hexdigest()
    assert journal.verify() == []
    assert journal.compatibility() == "legacy-prefix"


def test_append_requires_a_supported_lock(tmp_path: Path, monkeypatch) -> None:
    import reviewctl.journal as journal_module

    monkeypatch.setattr(journal_module, "fcntl", None)
    journal = ProjectJournal(
        tmp_path / "journal.jsonl", project_id="project-1", origin_id="origin-1"
    )

    with pytest.raises(JournalOperationError) as error:
        journal.append({"type": "review_started", "reviewId": "r1"})

    assert error.value.diagnostic.code == "journal_unavailable"
    assert journal.path.read_bytes() == b""


def test_append_rejects_a_different_configured_identity(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    original = ProjectJournal(path, project_id="project-1", origin_id="origin-1")
    original.append({"type": "review_started", "reviewId": "r1"})
    before = path.read_bytes()
    other = ProjectJournal(path, project_id="project-2", origin_id="origin-2")

    with pytest.raises(JournalOperationError) as error:
        other.append({"type": "review_finished", "reviewId": "r1"})

    assert error.value.diagnostic.code == "journal_corrupt"
    assert path.read_bytes() == before
