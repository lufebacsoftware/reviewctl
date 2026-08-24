from __future__ import annotations

import json
from pathlib import Path

from reviewctl.github import (
    ChangedFileSnapshot,
    CommandResult,
    PullRequestRef,
    PullRequestSnapshot,
    build_publication_plan,
)
from reviewctl.github_publisher import GitHubPublisher

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
BASE = "a" * 40


def make_snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        ref=PullRequestRef("example/project", 7),
        base_sha=BASE,
        head_sha=HEAD,
        visibility="private",
        changed_files=(
            ChangedFileSnapshot(path="src/app.py", status="modified", content="value = 2\n"),
        ),
        diff=(
            "diff --git a/src/app.py b/src/app.py\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        ),
        evidence=("test",),
    )


def make_plan():
    snapshot = make_snapshot()
    return build_publication_plan(
        snapshot,
        project_id="project-1",
        review_id="review-1",
        findings=(
            {
                "findingId": "finding-inline",
                "severity": "high",
                "path": "src/app.py",
                "line": 1,
                "title": "Handle failure",
            },
            {
                "findingId": "finding-summary",
                "severity": "medium",
                "path": "README.md",
                "line": 1,
                "title": "Document behavior",
            },
        ),
        review_status="accepted",
    )


class FakeRunner:
    def __init__(
        self,
        *,
        head_values: list[str] | None = None,
        comments: dict[int, list[dict]] | None = None,
        reviews: dict[int, list[dict]] | None = None,
    ) -> None:
        self.head_values = list(head_values or [HEAD])
        self.comments = comments or {1: []}
        self.reviews = reviews or {1: []}
        self.calls: list[tuple[str, ...]] = []
        self.post_payload = None

    def __call__(
        self, command, *, cwd: Path, timeout_seconds: int, input_bytes: bytes | None = None
    ) -> CommandResult:
        del cwd, timeout_seconds
        call = tuple(command)
        self.calls.append(call)
        endpoint = command[2]
        if "--method" in command and command[command.index("--method") + 1] == "POST":
            self.post_payload = json.loads(input_bytes.decode("utf-8"))
            return CommandResult(
                0,
                json.dumps({"id": 9001, "comments": [{"id": 9002}, {"id": 9003}]}).encode(),
                b"",
            )
        if endpoint == "repos/example/project/pulls/7":
            value = self.head_values.pop(0) if len(self.head_values) > 1 else self.head_values[0]
            return CommandResult(0, json.dumps({"head": {"sha": value}}).encode(), b"")
        if endpoint.endswith("/comments"):
            page = int(
                next(field.split("=", 1)[1] for field in command if field.startswith("page="))
            )
            return CommandResult(0, json.dumps(self.comments.get(page, [])).encode(), b"")
        if endpoint.endswith("/reviews"):
            page = int(
                next(field.split("=", 1)[1] for field in command if field.startswith("page="))
            )
            return CommandResult(0, json.dumps(self.reviews.get(page, [])).encode(), b"")
        raise AssertionError(f"unexpected command: {command}")


def post_calls(runner: FakeRunner) -> list[tuple[str, ...]]:
    return [
        call
        for call in runner.calls
        if "--method" in call and call[call.index("--method") + 1] == "POST"
    ]


def test_publisher_reconciles_both_comment_and_review_bodies_and_posts_one_group() -> None:
    plan = make_plan()
    runner = FakeRunner(comments={1: [{"id": 1, "body": "unrelated"}], 2: []}, reviews={1: []})

    result = GitHubPublisher(Path("."), runner=runner, page_size=2).publish(plan)

    assert result.status == "published"
    assert result.summary_comment_id == "9001"
    assert result.published_comment_ids == ("9002", "9003")
    assert result.skipped_finding_ids == ()
    posts = post_calls(runner)
    assert len(posts) == 1
    assert runner.post_payload["commit_id"] == HEAD
    assert runner.post_payload["comments"][0]["path"] == "src/app.py"
    assert "finding-summary" in runner.post_payload["body"]


def test_publisher_skips_existing_markers_even_on_a_later_head() -> None:
    plan = make_plan()
    runner = FakeRunner(
        head_values=[HEAD],
        comments={1: [{"id": 1, "body": plan.items[0].body}], 2: []},
        reviews={1: [{"id": 2, "body": plan.items[1].body}], 2: []},
    )

    result = GitHubPublisher(Path("."), runner=runner, page_size=2).publish(plan)

    assert result.status == "skipped_duplicate"
    assert result.published_comment_ids == ()
    assert result.skipped_finding_ids == ("finding-inline", "finding-summary")
    assert post_calls(runner) == []


def test_publisher_accepts_github_reviews_with_empty_summary_body() -> None:
    runner = FakeRunner(comments={1: [], 2: []}, reviews={1: [{"id": 2, "body": None}]})

    result = GitHubPublisher(Path("."), runner=runner, page_size=2).publish(make_plan())

    assert result.status == "published"


def test_publisher_refuses_stale_head_before_post() -> None:
    runner = FakeRunner(head_values=[OTHER_HEAD])

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "stale_head"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_stale_head"
    assert post_calls(runner) == []


def test_publisher_fails_closed_when_reconciliation_budget_cannot_prove_exhaustion() -> None:
    plan = make_plan()
    runner = FakeRunner(comments={1: [{"id": 1, "body": "x"}], 2: [{"id": 2, "body": "x"}]})

    result = GitHubPublisher(Path("."), runner=runner, page_size=1, max_pages=2).publish(plan)

    assert result.status == "reconciliation_incomplete"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "publication_reconciliation_incomplete"
    assert post_calls(runner) == []


def test_publisher_fails_closed_on_malformed_reconciliation_page() -> None:
    class MalformedRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if command[2].endswith("/comments"):
                self.calls.append(tuple(command))
                return CommandResult(0, b"{}", b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = MalformedRunner()
    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "reconciliation_incomplete"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "publication_reconciliation_incomplete"
    assert post_calls(runner) == []


def test_publisher_does_not_claim_success_for_malformed_post_response() -> None:
    class BadPostRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            if "--method" in command:
                self.calls.append(tuple(command))
                return CommandResult(0, b"{}", b"")
            return super().__call__(
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
            )

    runner = BadPostRunner()
    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_response_invalid"


def test_publisher_classifies_post_head_race_without_retry() -> None:
    runner = FakeRunner(head_values=[HEAD, HEAD, OTHER_HEAD])

    result = GitHubPublisher(Path("."), runner=runner).publish(make_plan())

    assert result.status == "stale_head_race"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_stale_head_race"
    assert len(post_calls(runner)) == 1
    assert result.summary_comment_id == "9001"


def test_publisher_redacts_failed_command_details() -> None:
    class FailedRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds, input_bytes=None):
            del cwd, timeout_seconds, input_bytes
            self.calls.append(tuple(command))
            return CommandResult(1, b"", b"Authorization: super-secret-token")

    result = GitHubPublisher(Path("."), runner=FailedRunner()).publish(make_plan())

    assert result.status == "failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "github_publication_failed"
    assert "super-secret-token" not in result.diagnostic.message
