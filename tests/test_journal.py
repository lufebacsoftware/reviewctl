from __future__ import annotations

from pathlib import Path

import pytest

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
