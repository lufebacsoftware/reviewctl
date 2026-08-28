from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import reviewctl.journal as journal_module
from reviewctl.errors import JournalOperationError
from reviewctl.journal import ProjectJournal


def test_journal_rejects_symlinked_project_state_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (project / ".reviewctl").symlink_to(external, target_is_directory=True)

    with pytest.raises(JournalOperationError, match="state root"):
        ProjectJournal(project / ".reviewctl" / "journal.jsonl")

    assert list(external.iterdir()) == []


def test_journal_rejects_symlinked_state_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    state = project / ".reviewctl"
    state.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text("outside\n")
    (state / "journal.jsonl").symlink_to(external)

    with pytest.raises(JournalOperationError, match="state path"):
        ProjectJournal(state / "journal.jsonl")

    assert external.read_text() == "outside\n"


def test_journal_append_rejects_state_file_replaced_by_symlink_before_open(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "project" / ".reviewctl" / "journal.jsonl"
    path.parent.parent.mkdir()
    journal = ProjectJournal(path)
    external = tmp_path / "external.jsonl"
    external.write_bytes(b"")
    external.chmod(0o644)
    real_open = journal_module.os.open
    swapped = False

    def raced_open(open_path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(open_path) in {path, Path(path.name)} and not swapped:
            swapped = True
            path.symlink_to(external)
        return real_open(open_path, flags, *args, **kwargs)

    monkeypatch.setattr(journal_module.os, "open", raced_open)

    with pytest.raises(JournalOperationError, match="journal"):
        journal.append({"type": "review_started", "reviewId": "r1"})

    assert swapped
    assert external.read_bytes() == b""
    assert external.stat().st_mode & 0o777 == 0o644


def test_journal_reports_existing_non_regular_path_as_corrupt(tmp_path: Path) -> None:
    path = tmp_path / ".reviewctl" / "journal.jsonl"
    path.mkdir(parents=True)
    journal = ProjectJournal(path)

    events, diagnostic = journal.read_with_diagnostic()

    assert events == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"
    assert "regular file" in diagnostic.message
    assert journal.verify() == [diagnostic.message]


def test_journal_descriptor_open_fails_closed_for_platform_and_path_errors(
    tmp_path: Path, monkeypatch
) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal_module, "_OPEN_SUPPORTS_DIR_FD", False)
    with pytest.raises(JournalOperationError) as unsupported:
        journal.append({"type": "review_started"})
    assert unsupported.value.diagnostic.code == "journal_unavailable"

    monkeypatch.setattr(journal_module, "_OPEN_SUPPORTS_DIR_FD", True)
    real_open = journal_module.os.open

    def fail_parent(path, flags, *args, **kwargs):
        if Path(path) == journal.path.parent:
            raise OSError("parent failed")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(journal_module.os, "open", fail_parent)
    with pytest.raises(JournalOperationError, match="directory"):
        journal.append({"type": "review_started"})


def test_journal_descriptor_open_rejects_non_directory_and_missing_create(
    tmp_path: Path, monkeypatch
) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    real_open = journal_module.os.open
    real_fstat = journal_module.os.fstat
    parent_descriptor: int | None = None

    def tracked_open(path, flags, *args, **kwargs):
        nonlocal parent_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == journal.path.parent:
            parent_descriptor = descriptor
        return descriptor

    def non_directory_parent(descriptor: int):
        if descriptor == parent_descriptor:
            return SimpleNamespace(st_mode=0)
        return real_fstat(descriptor)

    monkeypatch.setattr(journal_module.os, "open", tracked_open)
    monkeypatch.setattr(journal_module.os, "fstat", non_directory_parent)
    with pytest.raises(JournalOperationError, match="not a directory"):
        journal.append({"type": "review_started"})

    monkeypatch.setattr(journal_module.os, "fstat", real_fstat)

    def missing_journal(path, flags, *args, **kwargs):
        if Path(path) == Path(journal.path.name):
            raise FileNotFoundError("raced away")
        return tracked_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(journal_module.os, "open", missing_journal)
    with pytest.raises(JournalOperationError, match="raced away"):
        journal.append({"type": "review_started"})


def test_journal_descriptor_open_preserves_error_when_parent_close_fails(
    tmp_path: Path, monkeypatch
) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    real_open = journal_module.os.open
    real_close = journal_module.os.close

    def fail_file(path, flags, *args, **kwargs):
        if Path(path) == Path(journal.path.name):
            raise RuntimeError("file failed")
        return real_open(path, flags, *args, **kwargs)

    def fail_close(descriptor: int) -> None:
        try:
            raise OSError("close failed")
        finally:
            real_close(descriptor)

    monkeypatch.setattr(journal_module.os, "open", fail_file)
    monkeypatch.setattr(journal_module.os, "close", fail_close)
    with pytest.raises(JournalOperationError, match="file failed"):
        journal.append({"type": "review_started"})


def test_journal_close_failure_is_reported_for_append_and_read(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append({"type": "review_started"})
    real_close = journal_module.os.close

    def fail_close(descriptor: int) -> None:
        try:
            raise OSError("close failed")
        finally:
            real_close(descriptor)

    monkeypatch.setattr(journal_module.os, "close", fail_close)
    with pytest.raises(OSError, match="close failed"):
        journal.append({"type": "review_finished"})

    events, diagnostic = journal.read_with_diagnostic()
    assert events == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"


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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schemaVersion", True, "schema version"),
        ("schemaVersion", 1.0, "schema version"),
        ("sequence", True, "sequence"),
        ("projectId", None, "project identity"),
        ("projectId", "", "project identity"),
        ("originId", None, "origin identity"),
        ("originId", "", "origin identity"),
    ],
)
def test_journal_rejects_malformed_versioned_envelope_scalars(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    event = {
        "schemaVersion": 1,
        "projectId": "project-1",
        "originId": "origin-1",
        "sequence": 1,
        "previousEventSha256": None,
        "type": "review_started",
    }
    event[field] = value
    event["eventSha256"] = journal_module._event_digest(event)
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    journal = ProjectJournal(path)

    violations = journal.verify()
    _events, diagnostic = journal.read_with_diagnostic()

    assert any(message in violation for violation in violations)
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"
    assert journal.compatibility() == "invalid"


@pytest.mark.parametrize("status", ["unknown", ["open"]])
def test_journal_verify_rejects_persisted_unsupported_finding_status(
    tmp_path: Path, status: object
) -> None:
    event = {
        "schemaVersion": 1,
        "projectId": "project-1",
        "originId": "origin-1",
        "sequence": 1,
        "previousEventSha256": None,
        "type": "finding_observed",
        "findingId": "finding-1",
        "reviewId": "review-1",
        "status": status,
    }
    event["eventSha256"] = journal_module._event_digest(event)
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    journal = ProjectJournal(path, project_id="project-1", origin_id="origin-1")

    violations = journal.verify()
    findings, diagnostic = journal.findings_with_diagnostic()

    assert any("finding status" in violation for violation in violations)
    assert findings == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"


def test_journal_verify_rejects_persisted_invalid_dimensions(tmp_path: Path) -> None:
    event = {
        "schemaVersion": 1,
        "projectId": "project-1",
        "originId": "origin-1",
        "sequence": 1,
        "previousEventSha256": None,
        "type": "review_started",
        "reviewId": "review-1",
        "dimensions": None,
    }
    event["eventSha256"] = journal_module._event_digest(event)
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    journal = ProjectJournal(path, project_id="project-1", origin_id="origin-1")

    violations = journal.verify()

    assert any("dimensions" in violation for violation in violations)


@pytest.mark.parametrize("target", ["verified", ["fixed"]])
def test_journal_verify_rejects_persisted_invalid_status_transition(
    tmp_path: Path, target: object
) -> None:
    journal = ProjectJournal(
        tmp_path / "journal.jsonl", project_id="project-1", origin_id="origin-1"
    )
    finding = journal.append(
        {
            "type": "finding_observed",
            "findingId": "finding-1",
            "reviewId": "review-1",
            "status": "open",
        }
    )
    transition = {
        "schemaVersion": 1,
        "projectId": "project-1",
        "originId": "origin-1",
        "sequence": 2,
        "previousEventSha256": finding["eventSha256"],
        "type": "finding_status_changed",
        "findingId": "finding-1",
        "reviewId": "review-1",
        "from": "open",
        "to": target,
    }
    transition["eventSha256"] = journal_module._event_digest(transition)
    with journal.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(transition, sort_keys=True, separators=(",", ":")) + "\n")

    violations = journal.verify()
    findings, diagnostic = journal.findings_with_diagnostic()

    assert violations
    assert findings == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"


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
    events, diagnostic = journal.read_with_diagnostic()
    assert events == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_unavailable"
    assert journal.verify()


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


def test_singular_finding_and_status_change_propagate_journal_corruption(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append({"type": "finding", "findingId": "f", "status": "open"})
    with journal.path.open("a") as stream:
        stream.write('{"type":')

    with pytest.raises(JournalOperationError) as finding_error:
        journal.finding("f")
    with pytest.raises(JournalOperationError) as status_error:
        journal.append_status_change("f", "fixed")

    assert finding_error.value.diagnostic.code == "journal_corrupt"
    assert status_error.value.diagnostic.code == "journal_corrupt"


def test_journal_append_rejects_descriptor_diagnostic(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    monkeypatch.setattr(
        journal,
        "_read_descriptor",
        lambda descriptor: ([], journal_module.Diagnostic("journal_corrupt", "bad descriptor")),
    )
    with pytest.raises(JournalOperationError, match="bad descriptor"):
        journal.append({"type": "review_started"})


def test_journal_append_retries_short_writes(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    real_write = journal_module.os.write
    write_sizes: list[int] = []

    def short_first_write(descriptor, contents):
        limit = max(1, len(contents) // 2) if not write_sizes else len(contents)
        written = real_write(descriptor, contents[:limit])
        write_sizes.append(written)
        return written

    monkeypatch.setattr(journal_module.os, "write", short_first_write)

    event = journal.append({"type": "review_started", "reviewId": "r1"})

    assert len(write_sizes) == 2
    assert journal.events() == [event]


@pytest.mark.parametrize("operation", ["read", "verify"])
def test_journal_readers_wait_for_partial_append(
    tmp_path: Path, monkeypatch, operation: str
) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append({"type": "review_started", "reviewId": "r1"})
    real_write = journal_module.os.write
    partial_written = threading.Event()
    finish_write = threading.Event()
    reader_done = threading.Event()
    write_paused = False
    writer_errors: list[BaseException] = []
    reader_results: list[object] = []

    def short_then_pause(descriptor: int, contents) -> int:
        nonlocal write_paused
        if write_paused:
            return real_write(descriptor, contents)
        write_paused = True
        written = real_write(descriptor, contents[: max(1, len(contents) // 2)])
        partial_written.set()
        if not finish_write.wait(2):
            raise RuntimeError("reader did not release writer")
        return written

    def append_event() -> None:
        try:
            journal.append({"type": "review_finished", "reviewId": "r1"})
        except BaseException as error:
            writer_errors.append(error)

    def read_journal() -> None:
        reader_results.append(
            journal.read_with_diagnostic() if operation == "read" else journal.verify()
        )
        reader_done.set()

    monkeypatch.setattr(journal_module.os, "write", short_then_pause)
    writer = threading.Thread(target=append_event)
    writer.start()
    assert partial_written.wait(2)
    reader = threading.Thread(target=read_journal)
    reader.start()
    reader_blocked = not reader_done.wait(0.1)
    finish_write.set()
    writer.join(2)
    reader.join(2)

    assert reader_blocked
    assert not writer.is_alive() and not reader.is_alive()
    assert writer_errors == []
    if operation == "read":
        events, diagnostic = reader_results[0]
        assert len(events) == 2
        assert diagnostic is None
    else:
        assert reader_results == [[]]


def test_journal_append_retries_short_reads_before_deriving_envelope(
    tmp_path: Path, monkeypatch
) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl", project_id="p", origin_id="o")
    journal.append({"type": "review_started", "reviewId": "r1"})
    journal.append({"type": "review_attempt", "reviewId": "r1", "attempt": 1})
    first_line_size = len(journal.path.read_bytes().splitlines(keepends=True)[0])
    real_read = journal_module.os.read
    read_sizes: list[int] = []

    def short_first_read(descriptor: int, size: int) -> bytes:
        limit = first_line_size if not read_sizes else size
        chunk = real_read(descriptor, limit)
        read_sizes.append(len(chunk))
        return chunk

    monkeypatch.setattr(journal_module.os, "read", short_first_read)

    appended = journal.append({"type": "review_finished", "reviewId": "r1"})

    assert len(read_sizes) == 2
    assert appended["sequence"] == 3
    assert journal.verify() == []


def test_journal_descriptor_read_rejects_early_eof(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.path.write_bytes(b'{"type":"review_started"}\n')
    monkeypatch.setattr(journal_module.os, "read", lambda descriptor, size: b"")

    with journal.path.open("rb") as stream:
        events, diagnostic = journal._read_descriptor(stream.fileno())

    assert events == []
    assert diagnostic is not None
    assert "complete journal" in diagnostic.message


def test_journal_append_rejects_growth_during_descriptor_read(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl", project_id="p", origin_id="o")
    journal.append({"type": "review_started", "reviewId": "r1"})
    journal.append({"type": "review_attempt", "reviewId": "r1", "attempt": 1})
    first, second = journal.path.read_bytes().splitlines(keepends=True)
    journal.path.write_bytes(first)
    real_read = journal_module.os.read
    injected = False

    def grow_before_read(descriptor: int, size: int) -> bytes:
        nonlocal injected
        if not injected:
            injected = True
            with journal.path.open("ab") as intruder:
                intruder.write(second)
        return real_read(descriptor, size)

    monkeypatch.setattr(journal_module.os, "read", grow_before_read)

    with pytest.raises(JournalOperationError, match="changed while reading"):
        journal.append({"type": "review_finished", "reviewId": "r1"})

    assert journal.path.read_bytes() == first + second


def test_journal_append_rejects_zero_length_write(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal_module.os, "write", lambda descriptor, contents: 0)

    with pytest.raises(OSError, match="could not finish appending journal"):
        journal.append({"type": "review_started", "reviewId": "r1"})


def test_journal_append_rolls_back_a_partial_failed_write(tmp_path: Path, monkeypatch) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append({"type": "review_started", "reviewId": "r1"})
    before = journal.path.read_bytes()
    real_write = journal_module.os.write
    write_calls = 0

    def partial_then_fail(descriptor, contents):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return real_write(descriptor, contents[: max(1, len(contents) // 2)])
        raise OSError("disk full")

    monkeypatch.setattr(journal_module.os, "write", partial_then_fail)

    with pytest.raises(OSError, match="disk full"):
        journal.append({"type": "review_finished", "reviewId": "r1"})

    assert write_calls == 2
    assert journal.path.read_bytes() == before
    assert journal.verify() == []


def test_journal_append_preserves_primary_when_rollback_and_close_fail(
    tmp_path: Path, monkeypatch
) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    real_open = journal_module.os.open
    real_close = journal_module.os.close
    descriptors: list[int] = []
    rollback_attempts: list[tuple[int, int]] = []

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def fail_rollback(descriptor, length):
        rollback_attempts.append((descriptor, length))
        raise OSError("rollback secondary")

    monkeypatch.setattr(journal_module.os, "open", tracked_open)
    monkeypatch.setattr(
        journal_module.os,
        "write",
        lambda descriptor, contents: (_ for _ in ()).throw(RuntimeError("write primary")),
    )
    monkeypatch.setattr(journal_module.os, "ftruncate", fail_rollback)
    monkeypatch.setattr(
        journal_module.os,
        "close",
        lambda descriptor: (_ for _ in ()).throw(OSError("close secondary")),
    )

    with pytest.raises(RuntimeError, match="write primary"):
        journal.append({"type": "review_started", "reviewId": "r1"})

    monkeypatch.undo()
    assert len(descriptors) == 2
    assert rollback_attempts == [(descriptors[1], 0)]
    for descriptor in descriptors:
        real_close(descriptor)


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
    monkeypatch.setattr(
        Path,
        "open",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    assert journal.verify()


def test_journal_lock_failures_are_typed(tmp_path: Path, monkeypatch) -> None:
    class LockFailure:
        LOCK_SH = 0
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(descriptor, mode):
            if mode in {LockFailure.LOCK_SH, LockFailure.LOCK_EX}:
                raise OSError("lock failure")

    monkeypatch.setattr(journal_module, "fcntl", LockFailure)
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    with pytest.raises(JournalOperationError, match="could not lock"):
        journal.append({"type": "review_started"})
    events, diagnostic = journal.read_with_diagnostic()
    assert events == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_unavailable"

    class UnlockFailure:
        LOCK_SH = 0
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
    with pytest.raises(RuntimeError, match="read primary"):
        with journal._shared_lock(-1):
            raise RuntimeError("read primary")

    monkeypatch.setattr(
        journal_module.os,
        "write",
        lambda descriptor, contents: (_ for _ in ()).throw(RuntimeError("write primary")),
    )
    with pytest.raises(RuntimeError, match="write primary"):
        journal.append({"type": "review_started"})


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
    monkeypatch.setattr(
        Path,
        "open",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    events, diagnostic = journal.read_with_diagnostic()
    assert events == [] and diagnostic is not None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_journal_append_rejects_nonfinite_values(tmp_path: Path, value: float) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")

    with pytest.raises(ValueError, match="JSON"):
        journal.append({"type": "review_started", "value": value})

    assert journal.path.read_bytes() == b""


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_journal_reads_reject_nonfinite_json_numbers(tmp_path: Path, number: str) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.path.write_text(f'{{"type":"review_started","value":{number}}}\n')

    events, diagnostic = journal.read_with_diagnostic()

    assert events == []
    assert diagnostic is not None
    assert diagnostic.code == "journal_corrupt"
    assert journal.verify()


def test_journal_reads_preserve_finite_json_float(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.path.write_text('{"type":"review_started","value":0.5}\n')

    events, diagnostic = journal.read_with_diagnostic()

    assert events == [{"type": "review_started", "value": 0.5}]
    assert diagnostic is None


def test_journal_events_raises_on_corrupt_suffix(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.path.write_text('{"type":"review_started","reviewId":"r1"}\n{"type":')

    with pytest.raises(JournalOperationError) as error:
        journal.events()

    assert error.value.diagnostic.code == "journal_corrupt"


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


@pytest.mark.parametrize("dimensions", [None, 1])
def test_journal_append_rejects_malformed_dimensions(tmp_path: Path, dimensions: object) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")

    with pytest.raises(ValueError, match="dimensions"):
        journal.append({"type": "review_started", "dimensions": dimensions})

    assert not journal.path.exists()


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
