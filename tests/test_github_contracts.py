from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import reviewctl.github as github_module
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


def test_github_contracts_reject_invalid_identity_and_paths() -> None:
    with pytest.raises(ValueError, match="repository"):
        PullRequestRef("invalid", 1)
    with pytest.raises(ValueError, match="positive"):
        PullRequestRef("owner/name", 0)
    with pytest.raises(ValueError, match="status"):
        github_module.ChangedFileSnapshot("file.py", "unknown", "x")
    with pytest.raises(ValueError, match="UTF-8"):
        github_module.ChangedFileSnapshot("file.py", "added", 7)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="line"):
        github_module.PublicationTarget("file.py", 0)
    with pytest.raises(ValueError, match="side"):
        github_module.PublicationTarget("file.py", 1, "MIDDLE")


def test_github_contracts_reject_invalid_snapshots_and_path_regex(monkeypatch) -> None:
    ref = PullRequestRef("owner/name")
    file_value = github_module.ChangedFileSnapshot("file.py", "added", "x")
    with pytest.raises(ValueError, match="SHA"):
        github_module.PullRequestSnapshot(ref, "a", "b" * 40, "private", (file_value,), "")
    with pytest.raises(ValueError, match="visibility"):
        github_module.PullRequestSnapshot(ref, "a" * 40, "b" * 40, "internal", (file_value,), "")
    with pytest.raises(ValueError, match="UTF-8"):
        github_module.PullRequestSnapshot(ref, "a" * 40, "b" * 40, "private", (file_value,), 7)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        github_module.PullRequestSnapshot(
            ref, "a" * 40, "b" * 40, "private", (file_value, file_value), ""
        )

    class NotAChangedFile:
        path = "file.py"

    with pytest.raises(ValueError, match="ChangedFile"):
        github_module.PullRequestSnapshot(
            ref, "a" * 40, "b" * 40, "private", (NotAChangedFile(),), ""
        )  # type: ignore[arg-type]

    class NoPathPart:
        @staticmethod
        def fullmatch(value):
            return None

    monkeypatch.setattr(github_module, "_PATH_PART", NoPathPart)
    with pytest.raises(ValueError, match="component"):
        github_module._validate_relative_path("valid.py")


def test_github_publication_plan_handles_invalid_status_and_finding_locations() -> None:
    snapshot = github_module.PullRequestSnapshot(
        PullRequestRef("owner/name", 7),
        "a" * 40,
        "b" * 40,
        "private",
        (
            github_module.ChangedFileSnapshot("added.py", "added", "new"),
            github_module.ChangedFileSnapshot("deleted.py", "deleted", "old"),
        ),
        "diff",
    )
    not_accepted = github_module.build_publication_plan(
        snapshot, project_id="project", review_id="review", findings=(), review_status="partial"
    )
    assert not_accepted.reason == "review_not_accepted"
    findings = [
        {"findingId": "f1", "path": "added.py", "line": 1, "title": "title"},
        {"findingId": "f2", "path": "deleted.py", "line": 1, "title": "title"},
        {"findingId": "f3", "path": "other.py", "line": "1", "title": "title"},
    ]
    plan = github_module.build_publication_plan(
        snapshot,
        project_id="project",
        review_id="review",
        findings=findings,
        review_status="accepted",
    )
    assert all(item.target is None for item in plan.items)
    with pytest.raises(ValueError, match="findingId"):
        github_module.build_publication_plan(
            snapshot,
            project_id="project",
            review_id="review",
            findings=[{}],
            review_status="accepted",
        )


def test_github_diff_parsing_covers_added_deleted_renamed_and_dev_null() -> None:
    diff = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+x\n+y\n"
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n"
        "--- a/old.py\n+++ /dev/null\n"
        "diff --git a/oldname.py b/newname.py\nrename from oldname.py\nrename to newname.py\n"
    )
    values = github_module._diff_files(diff)
    assert {value.status for value in values} == {"added", "deleted", "renamed"}
    assert github_module._diff_added_lines(
        github_module.PullRequestSnapshot(
            PullRequestRef("owner/name"),
            "a" * 40,
            "b" * 40,
            "private",
            (github_module.ChangedFileSnapshot("new.py", "added", "x"),),
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,1 @@\n+x\n",
        )
    ) == {("new.py", 1)}


def test_github_added_lines_decode_c_quoted_utf8_paths() -> None:
    snapshot = github_module.PullRequestSnapshot(
        PullRequestRef("owner/name"),
        "a" * 40,
        "b" * 40,
        "private",
        (github_module.ChangedFileSnapshot("café.py", "added", "x"),),
        '--- /dev/null\n+++ "b/caf\\303\\251.py"\n@@ -0,0 +1,1 @@\n+x\n',
    )

    assert github_module._diff_added_lines(snapshot) == {("café.py", 1)}


def test_github_c_quoted_path_decoder_handles_escape_failures() -> None:
    assert github_module._decode_git_path('"a/foo\\\\bar.py"') == "a/foo\\bar.py"
    with pytest.raises(ValueError, match="unterminated"):
        github_module._decode_git_path('"unterminated')
    with pytest.raises(ValueError, match="incomplete"):
        github_module._decode_git_path('"bad\\"')
    with pytest.raises(ValueError, match="invalid quoted"):
        github_module._decode_git_path('"bad\\q"')
    with pytest.raises(ValueError, match="valid UTF-8"):
        github_module._decode_git_path('"bad\\377"')


def test_github_diff_path_removes_exactly_one_side_prefix() -> None:
    assert github_module._diff_path("a/b/foo.py") == "b/foo.py"
    assert github_module._diff_path("b/a/foo.py") == "a/foo.py"
    with pytest.raises(ValueError, match="whitespace"):
        github_module._diff_path("a/b/foo ")


def test_github_diff_rename_paths_do_not_strip_repository_directories() -> None:
    diff = (
        "diff --git a/b/old.py b/b/new.py\n"
        "similarity index 100%\n"
        "rename from b/old.py\n"
        "rename to b/new.py\n"
    )

    assert github_module._diff_files(diff) == (
        github_module._DiffFile(path="b/new.py", status="renamed", old_path="b/old.py"),
    )


def test_github_diff_parsers_keep_plus_plus_plus_hunk_lines_as_source() -> None:
    diff = (
        "diff --git a/src/counter.c b/src/counter.c\n"
        "--- a/src/counter.c\n"
        "+++ b/src/counter.c\n"
        "@@ -0,0 +1,1 @@\n"
        "+++ counter;\n"
    )

    assert github_module._diff_files(diff) == (
        github_module._DiffFile(path="src/counter.c", status="modified", old_path="src/counter.c"),
    )
    snapshot = github_module.PullRequestSnapshot(
        PullRequestRef("owner/name"),
        "a" * 40,
        "b" * 40,
        "private",
        (github_module.ChangedFileSnapshot("src/counter.c", "modified", "+++ counter;\n"),),
        diff,
    )
    assert github_module._diff_added_lines(snapshot) == {("src/counter.c", 1)}


def test_github_command_runner_maps_timeout_oserror_and_success(
    monkeypatch, tmp_path: Path
) -> None:
    def timeout(*args, **kwargs):
        raise github_module.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(github_module.subprocess, "run", timeout)
    assert github_module._run_command(["gh"], cwd=tmp_path, timeout_seconds=1).returncode == 124

    def unavailable(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(github_module.subprocess, "run", unavailable)
    assert github_module._run_command(["gh"], cwd=tmp_path, timeout_seconds=1).returncode == 127

    class Completed:
        returncode = 0
        stdout = b"out"
        stderr = b"err"

    monkeypatch.setattr(github_module.subprocess, "run", lambda *args, **kwargs: Completed())
    result = github_module._run_command(["gh"], cwd=tmp_path, timeout_seconds=1, input_bytes=b"in")
    assert result.stdout == b"out"


def test_snapshot_context_and_added_line_fallback_branches() -> None:
    snapshot = github_module.PullRequestSnapshot(
        PullRequestRef("owner/name"),
        "a" * 40,
        "b" * 40,
        "private",
        (github_module.ChangedFileSnapshot("file.py", "modified", "x"),),
        "plain text",
    )
    assert snapshot.to_context()["kind"] == "github_pull_request"
    assert github_module._diff_added_lines(snapshot) == {("file.py", 1)}

    with_lines = github_module.PullRequestSnapshot(
        PullRequestRef("owner/name"),
        "a" * 40,
        "b" * 40,
        "private",
        (github_module.ChangedFileSnapshot("file.py", "modified", "x"),),
        "@@ -1,0 +1,1 @@\n+x\n context\n+++ /dev/null\n",
    )
    assert github_module._diff_added_lines(with_lines) == {("file.py", 1)}
    assert github_module._diff_files("") == ()
    with pytest.raises(github_module._UnsupportedDiffError, match="diff --git"):
        github_module._diff_files("+++ /dev/null\n")
