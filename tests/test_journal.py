from __future__ import annotations

from pathlib import Path

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
