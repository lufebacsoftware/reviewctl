from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import reviewctl.journal as journal_module
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


def test_two_origins_can_verify_independently_for_one_project(tmp_path: Path) -> None:
    first = ProjectJournal(
        tmp_path / "amelia.jsonl", project_id="project-shared", origin_id="origin-amelia"
    )
    second = ProjectJournal(
        tmp_path / "eloisa.jsonl", project_id="project-shared", origin_id="origin-eloisa"
    )

    first.append({"type": "review_started", "reviewId": "r-amelia"})
    second.append({"type": "review_started", "reviewId": "r-eloisa"})

    assert first.verify() == []
    assert second.verify() == []
    assert first.events()[0]["projectId"] == second.events()[0]["projectId"]
    assert first.events()[0]["originId"] != second.events()[0]["originId"]


def test_journal_rejects_partial_identity_and_invalid_events(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provided together"):
        ProjectJournal(tmp_path / "journal.jsonl", project_id="project")
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    with pytest.raises(ValueError, match="string type"):
        journal.append({})
    with pytest.raises(ValueError, match="string type"):
        journal.append({"type": 7})
    with pytest.raises(ValueError, match="status"):
        journal.append({"type": "finding", "status": "unknown"})
    with pytest.raises(ValueError, match="findingId"):
        journal.append({"type": "finding_status_changed", "from": "open", "to": "fixed"})
    with pytest.raises(ValueError, match="from and to"):
        journal.append(
            {"type": "finding_status_changed", "findingId": "f", "from": 1, "to": "fixed"}
        )
    with pytest.raises(ValueError, match="unsupported"):
        journal.append(
            {"type": "finding_status_changed", "findingId": "f", "from": "bad", "to": "fixed"}
        )
    with pytest.raises(ValueError, match="identifiers"):
        journal.append({"type": "review_started", "eventId": 7})
    with pytest.raises(ValueError, match="identifiers"):
        journal.append({"type": "review_started", "reviewId": 7})


def test_journal_status_helpers_cover_unknown_missing_and_reason_paths(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    with pytest.raises(JournalOperationError, match="not found"):
        journal.append_status_change("missing", "fixed")
    journal.append({"type": "finding", "findingId": "f", "status": "open"})
    with pytest.raises(JournalOperationError, match="unsupported"):
        journal.append_status_change("f", "unknown")
    journal.append_status_change("f", "fixed", reason="  patched  ")
    journal.append_status_change("f", "open")
    assert "statusReason" not in journal.finding("f")
    with pytest.raises(ValueError, match="finding not found"):
        journal._validate_event(
            {
                "type": "finding_status_changed",
                "findingId": "missing",
                "from": "open",
                "to": "fixed",
            }
        )
    with pytest.raises(JournalOperationError, match="invalid finding status transition"):
        journal.append_status_change("f", "verified")


def test_journal_append_rejects_descriptor_diagnostic(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    monkeypatch.setattr(
        journal,
        "_read_descriptor",
        lambda descriptor: ([], journal_module.Diagnostic("journal_corrupt", "bad descriptor")),
    )
    with pytest.raises(JournalOperationError, match="bad descriptor"):
        journal.append({"type": "review_started"})


def test_journal_envelope_identity_and_read_diagnostics(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl", project_id="p", origin_id="o")
    with pytest.raises(JournalOperationError, match="identity"):
        journal._with_envelope(
            {"type": "review_started"}, [{"projectId": "other", "schemaVersion": 1}]
        )

    journal.path.write_text(
        '{"schemaVersion":1,"projectId":"p","originId":"o","sequence":1,"type":"review_started"}\n'
    )
    events, diagnostic = journal.read_with_diagnostic()
    assert events and diagnostic is not None
    assert journal.findings_with_diagnostic()[1] is not None
    with pytest.raises(JournalOperationError):
        journal.head_sequence()
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("denied")))
    assert journal.verify()


def test_journal_lock_failures_are_typed(tmp_path: Path, monkeypatch) -> None:
    class LockFailure:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(descriptor, mode):
            if mode == LockFailure.LOCK_EX:
                raise OSError("lock failure")

    monkeypatch.setattr(journal_module, "fcntl", LockFailure)
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    with pytest.raises(JournalOperationError, match="could not lock"):
        journal.append({"type": "review_started"})

    class UnlockFailure:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(descriptor, mode):
            if mode == UnlockFailure.LOCK_UN:
                raise OSError("unlock failure")

    monkeypatch.setattr(journal_module, "fcntl", UnlockFailure)
    with pytest.raises(OSError, match="unlock failure"):
        with journal._exclusive_lock(-1):
            pass


def test_journal_read_and_verify_diagnostics(tmp_path: Path, monkeypatch) -> None:
    missing = ProjectJournal(tmp_path / "missing.jsonl")
    assert missing.read_with_diagnostic() == ([], None)
    assert missing.verify() == []
    path = tmp_path / "bad.jsonl"
    journal = ProjectJournal(path)
    path.write_bytes(b"\xff")
    events, diagnostic = journal.read_with_diagnostic()
    assert events == [] and diagnostic is not None
    assert journal.verify()
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("denied")))
    events, diagnostic = journal.read_with_diagnostic()
    assert events == [] and diagnostic is not None


def test_journal_parse_and_descriptor_fail_closed(tmp_path: Path) -> None:
    events, diagnostic = ProjectJournal._parse_bytes(b"\n[]\n")
    assert events == [] and diagnostic is not None
    events, diagnostic = ProjectJournal._parse_bytes(b"{}\n")
    assert events == [] and diagnostic is not None
    events, diagnostic = ProjectJournal._parse_bytes(b"{\n")
    assert events == [] and diagnostic is not None
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    _events, diagnostic = journal._read_descriptor(-1)
    assert diagnostic is not None


def test_journal_verify_events_covers_legacy_and_versioned_violations(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl", project_id="p", origin_id="o")
    legacy = {"type": "review_started"}
    event = {
        "schemaVersion": 2,
        "projectId": "wrong",
        "originId": "wrong",
        "sequence": 4,
        "previousEventSha256": "wrong",
        "eventSha256": "wrong",
        "type": "review_finished",
    }
    assert journal._verify_events([legacy, event, legacy])
    unconfigured = ProjectJournal(tmp_path / "other.jsonl")
    assert unconfigured._verify_events(
        [{"schemaVersion": 1, "projectId": "p", "originId": "o", "sequence": 1, "eventSha256": "x"}]
    )


def test_journal_project_and_status_projection_rejects_malformed_events() -> None:
    assert ProjectJournal._project([{"type": "review_started"}]) == []
    assert ProjectJournal._project([{"type": "finding"}]) == []
    assert (
        ProjectJournal._project([{"type": "finding", "findingId": "f", "status": "unknown"}]) == []
    )
    with pytest.raises(ValueError, match="missing findingId"):
        ProjectJournal._project([{"type": "finding_status_changed"}])
    with pytest.raises(ValueError, match="unknown finding"):
        ProjectJournal._project([{"type": "finding_status_changed", "findingId": "f"}])
    with pytest.raises(ValueError, match="source"):
        ProjectJournal._project(
            [
                {"type": "finding", "findingId": "f", "status": "open"},
                {"type": "finding_status_changed", "findingId": "f", "from": "fixed", "to": "open"},
            ]
        )
    with pytest.raises(ValueError, match="transition"):
        ProjectJournal._project(
            [
                {"type": "finding", "findingId": "f", "status": "open"},
                {
                    "type": "finding_status_changed",
                    "findingId": "f",
                    "from": "open",
                    "to": "verified",
                },
            ]
        )
    with pytest.raises(ValueError, match="dimensions"):
        ProjectJournal._event_dimensions({"dimensions": ["bad"]})


def test_journal_compatibility_head_and_dimension_diagnostics(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    assert journal.compatibility() == "empty"
    journal.append({"type": "review_started"})
    assert journal.compatibility() == "legacy"
    assert journal.head_sequence() == 0
    findings, diagnostic = journal.findings_with_diagnostic(dimension="bad")
    assert findings == [] and diagnostic is not None
    path = journal.path
    path.write_text('{"type":"finding_status_changed","findingId":"unknown"}\n')
    findings, diagnostic = journal.findings_with_diagnostic()
    assert findings == [] and diagnostic is not None
    with pytest.raises(JournalOperationError):
        journal.findings()
    journal.path.write_bytes(b"\xff")
    assert journal.compatibility() == "invalid"
