from __future__ import annotations

from dataclasses import replace

import pytest

from reviewctl.github import (
    ChangedFileSnapshot,
    PullRequestRef,
    PullRequestSnapshot,
    build_publication_plan,
)


def make_snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        ref=PullRequestRef("example/project"),
        base_sha="a" * 40,
        head_sha="b" * 40,
        visibility="private",
        changed_files=(
            ChangedFileSnapshot(
                path="src/app.py",
                status="modified",
                content="value = 2\n",
            ),
            ChangedFileSnapshot(
                path="docs/old.md",
                status="deleted",
                content="# old\n",
            ),
        ),
        diff=(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        ),
        evidence=("pull_request", "pull_request_diff"),
    )


def test_pull_request_identity_and_file_paths_are_bounded() -> None:
    assert PullRequestRef("example/project").repository == "example/project"
    with pytest.raises(ValueError, match="repository"):
        PullRequestRef("example/project/other")
    with pytest.raises(ValueError, match="path"):
        ChangedFileSnapshot(path="../secret", status="modified", content="x")


def test_snapshot_digest_is_stable_and_changes_with_immutable_input() -> None:
    first = make_snapshot()
    second = make_snapshot()

    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.diff_sha256 != ""
    assert replace(first, head_sha="c" * 40).snapshot_sha256 != first.snapshot_sha256
    assert (
        replace(
            first,
            changed_files=(
                replace(first.changed_files[0], content="value = 3\n"),
                first.changed_files[1],
            ),
        ).snapshot_sha256
        != first.snapshot_sha256
    )


def test_complete_review_builds_stable_inline_and_summary_items() -> None:
    snapshot = make_snapshot()
    findings = (
        {
            "findingId": "finding-inline",
            "severity": "high",
            "path": "src/app.py",
            "line": 1,
            "title": "Handle the failure",
        },
        {
            "findingId": "finding-summary",
            "severity": "medium",
            "path": "README.md",
            "line": 1,
            "title": "Document the behavior",
        },
    )

    first = build_publication_plan(
        snapshot,
        project_id="project-1",
        review_id="review-1",
        findings=findings,
        review_status="accepted",
    )
    second = build_publication_plan(
        snapshot,
        project_id="project-1",
        review_id="review-1",
        findings=findings,
        review_status="accepted",
    )

    assert first.executable is True
    assert first.plan_sha256 == second.plan_sha256
    assert first.items[0].target is not None
    assert first.items[0].target.path == "src/app.py"
    assert first.items[0].target.line == 1
    assert first.items[1].target is None
    assert "finding-inline" in first.items[0].marker
    assert "reviewctl-head: bbbbb" in first.items[0].body


def test_incomplete_review_cannot_build_executable_plan() -> None:
    plan = build_publication_plan(
        make_snapshot(),
        project_id="project-1",
        review_id="review-1",
        findings=(),
        review_status="partial",
    )

    assert plan.executable is False
    assert plan.items == ()
    assert plan.reason == "review_not_accepted"
