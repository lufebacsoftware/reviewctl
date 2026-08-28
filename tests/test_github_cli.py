from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import reviewctl.project_cli as project_cli
from reviewctl.api import Finding, ReviewResult, finding_id
from reviewctl.cli import run_cli
from reviewctl.errors import Diagnostic, JournalOperationError
from reviewctl.github import (
    ChangedFileSnapshot,
    GitHubSourceError,
    PullRequestRef,
    PullRequestSnapshot,
)
from reviewctl.github_publisher import PublicationResult, PublishedComment


def write_config(project: Path) -> None:
    (project / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:fake/model"]\n'
        'execution = "remote"\n'
    )


def snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        ref=PullRequestRef("example/project", 7),
        base_sha="a" * 40,
        head_sha="b" * 40,
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


class FakeSource:
    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def resolve(self, ref: PullRequestRef) -> PullRequestSnapshot:
        assert ref == PullRequestRef("example/project", 7)
        return snapshot()


class FakeClient:
    request = None
    instance = None
    config = SimpleNamespace(project=SimpleNamespace(project_id="project-test"))

    class Journal:
        def __init__(self) -> None:
            self.events = []

        def append(self, event):
            self.events.append(event)

    def __init__(self) -> None:
        self._journal = self.Journal()

    @classmethod
    def from_project(cls, project_dir: Path):
        assert project_dir.is_dir()
        cls.instance = cls()
        return cls.instance

    def review(self, request):
        type(self).request = request
        unsigned = {"reviewId": "github-review-1", "status": "accepted"}
        digest = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt = Path(request.files[0]).parents[2] / "receipt.json"
        receipt.write_text(json.dumps({**unsigned, "sha256": digest}))
        return ReviewResult(
            status="accepted",
            review_id="github-review-1",
            receipt_path=receipt,
            findings=(
                Finding(
                    severity="high",
                    path="src/app.py",
                    line=1,
                    title="Handle failure",
                    evidence="private evidence",
                    reproduction="private reproduction",
                ),
            ),
            receipt_sha256=digest,
        )

    def journal(self):
        return self._journal


class FakePublisher:
    plans = []
    result = None

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def publish(self, plan):
        type(self).plans.append(plan)
        return type(self).result or PublicationResult(
            publication_key="github:example/project:7:review-1",
            head_sha="b" * 40,
            status="published",
            published_comment_ids=("9002",),
            published_comments=(PublishedComment("finding-inline", "9002"),),
            summary_comment_id="9001",
        )


def github_args(project: Path, **overrides: object) -> SimpleNamespace:
    values = {
        "repo": "example/project",
        "pr": 7,
        "project": str(project),
        "profile": "default",
        "dimensions": [],
        "review_id": None,
        "format": "json",
        "publish": True,
        "publish_event": "comment",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_github_review_is_dry_run_and_passes_typed_context_to_existing_flow(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)
    monkeypatch.setattr("reviewctl.project_cli.LocalGitHubSource", FakeSource)
    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", FakeClient)

    result = run_cli(
        [
            "github",
            "review",
            "--repo",
            "example/project",
            "--pr",
            "7",
            "--project",
            str(tmp_path),
            "--format",
            "json",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["snapshot"]["headSha"] == "b" * 40
    assert payload["snapshot"]["snapshotSha256"] == snapshot().snapshot_sha256
    assert payload["publicationPlan"]["executable"] is True
    assert payload["publicationPlan"]["items"][0]["target"] == {
        "path": "src/app.py",
        "line": 1,
        "side": "RIGHT",
    }
    assert Path(payload["publicationPlanArtifact"]).is_file()
    assert "Handle failure" in Path(payload["publicationPlanArtifact"]).read_text()
    assert "value = 2" not in output
    assert FakeClient.request.source_context == snapshot().to_context()
    assert all(str(path).startswith(str(tmp_path)) for path in FakeClient.request.files)
    assert FakeClient.request is not None


def test_github_review_maps_unique_basename_to_snapshot_path_for_inline_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)

    class BasenameClient(FakeClient):
        def review(self, request):
            result = super().review(request)
            return replace(
                result,
                findings=(replace(result.findings[0], path="app.py"),),
            )

    monkeypatch.setattr("reviewctl.project_cli.LocalGitHubSource", FakeSource)
    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", BasenameClient)

    assert project_cli.github_review_project(github_args(tmp_path, publish=False)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["publicationPlan"]["items"][0]["target"] == {
        "path": "src/app.py",
        "line": 1,
        "side": "RIGHT",
    }
    assert payload["publicationPlan"]["items"][0]["findingId"] == finding_id(
        Finding(
            severity="high",
            path="app.py",
            line=1,
            title="Handle failure",
            evidence="private evidence",
            reproduction="private reproduction",
        )
    )


def test_github_finding_path_mapping_leaves_ambiguous_and_unknown_paths_unchanged() -> None:
    ambiguous_snapshot = replace(
        snapshot(),
        changed_files=(
            ChangedFileSnapshot("src/entry.py", "modified", "first"),
            ChangedFileSnapshot("tests/entry.py", "modified", "second"),
        ),
    )
    ambiguous = Finding("high", "entry.py", 1, "title", "evidence", "reproduction")
    unknown = replace(ambiguous, path="missing.py")

    assert project_cli._map_github_finding_paths(ambiguous_snapshot, (ambiguous, unknown)) == (
        ambiguous,
        unknown,
    )


def test_github_dry_run_does_not_instantiate_publisher_and_records_plan_event(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)
    monkeypatch.setattr("reviewctl.project_cli.LocalGitHubSource", FakeSource)
    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", FakeClient)

    class ForbiddenPublisher:
        def __init__(self, project_dir: Path) -> None:
            raise AssertionError(f"publisher should not be created: {project_dir}")

    monkeypatch.setattr("reviewctl.project_cli.GitHubPublisher", ForbiddenPublisher)

    assert (
        run_cli(
            [
                "github",
                "review",
                "--repo",
                "example/project",
                "--pr",
                "7",
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert [event["type"] for event in FakeClient.instance.journal().events] == [
        "github_publication_planned"
    ]


def test_github_publish_is_explicit_and_receives_only_the_plan(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)
    monkeypatch.setattr("reviewctl.project_cli.LocalGitHubSource", FakeSource)
    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", FakeClient)
    FakePublisher.plans = []
    monkeypatch.setattr("reviewctl.project_cli.GitHubPublisher", FakePublisher)

    result = run_cli(
        [
            "github",
            "review",
            "--repo",
            "example/project",
            "--pr",
            "7",
            "--project",
            str(tmp_path),
            "--publish",
            "--publish-event",
            "comment",
            "--format",
            "json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["publication"]["status"] == "published"
    assert len(FakePublisher.plans) == 1
    assert FakePublisher.plans[0].head_sha == "b" * 40
    assert [event["type"] for event in FakeClient.instance.journal().events] == [
        "github_publication_planned",
        "github_publication_started",
        "github_comment_published",
        "github_summary_published",
    ]
    assert FakeClient.instance.journal().events[2]["findingId"] == "finding-inline"
    assert FakeClient.instance.journal().events[2]["commentId"] == "9002"


def test_github_invalid_review_without_receipt_does_not_write_plan_to_cwd(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)

    class InvalidClient(FakeClient):
        def review(self, request):
            return ReviewResult(
                status="invalid_request",
                review_id="invalid",
                receipt_path=Path(),
                findings=(),
            )

    monkeypatch.setattr("reviewctl.project_cli.LocalGitHubSource", FakeSource)
    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", InvalidClient)

    assert (
        run_cli(
            [
                "github",
                "review",
                "--repo",
                "example/project",
                "--pr",
                "7",
                "--project",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["publicationPlanArtifact"] is None
    assert not (tmp_path / "publication-plan.json").exists()


@pytest.mark.parametrize(
    "status,event_type,extra",
    [
        ("skipped_duplicate", "github_comment_skipped_duplicate", {"findingId": "f1"}),
        ("stale_head", "github_publication_stale_head", {"observedHeadSha": "c" * 40}),
        ("stale_head_race", "github_publication_stale_head_race", {"observedHeadSha": "d" * 40}),
        ("failed", "github_publication_failed", {"status": "failed", "diagnostic": None}),
        (
            "failed",
            "github_publication_failed",
            {
                "status": "failed",
                "diagnostic": Diagnostic("github_publication_failed", "failed").to_dict(),
            },
        ),
        (
            "reconciliation_incomplete",
            "github_publication_failed",
            {"status": "reconciliation_incomplete", "diagnostic": None},
        ),
        (
            "plan_invalid",
            "github_publication_failed",
            {"status": "plan_invalid", "diagnostic": None},
        ),
    ],
)
def test_github_publication_event_projection_has_exact_payload(
    status: str, event_type: str, extra: dict[str, object]
) -> None:
    plan = project_cli.build_publication_plan(
        snapshot(),
        project_id="project-test",
        review_id="review-1",
        findings=(),
        review_status="accepted",
    )

    class Journal:
        def __init__(self) -> None:
            self.events = []

        def append(self, event):
            self.events.append(event)

    class Client:
        def __init__(self) -> None:
            self._journal = Journal()

        def journal(self):
            return self._journal

    diagnostic = (
        Diagnostic("github_publication_failed", "failed") if extra.get("diagnostic") else None
    )
    result = PublicationResult(
        "key",
        plan.head_sha,
        status,
        skipped_finding_ids=("f1",) if status == "skipped_duplicate" else (),
        observed_head_sha=extra.get("observedHeadSha"),
        diagnostic=diagnostic,
    )
    client = Client()
    project_cli._record_github_publication_events(client, plan, result)
    common = {
        "publicationKey": "key",
        "reviewId": "review-1",
        "repository": "example/project",
        "pullNumber": 7,
        "headSha": plan.head_sha,
        "type": event_type,
    }
    assert client.journal().events == [{**common, **extra}]


@pytest.mark.parametrize(
    "failure,expected,code,fragment",
    [
        ("client", 5, "journal_corrupt", "client failed"),
        ("source", 2, "github_path_invalid", "source failed"),
        ("value", 2, "invalid_request", "source value failed"),
        ("os_error", 2, "invalid_request", "source os failed"),
    ],
)
def test_github_front_door_setup_errors(
    tmp_path: Path,
    monkeypatch,
    capsys,
    failure: str,
    expected: int,
    code: str,
    fragment: str,
) -> None:
    write_config(tmp_path)
    args = github_args(tmp_path, publish=False)
    if failure == "client":

        class FailingClient:
            @classmethod
            def from_project(cls, project_dir):
                raise JournalOperationError(Diagnostic("journal_corrupt", "client failed"))

        monkeypatch.setattr(project_cli, "ReviewClient", FailingClient)
    elif failure == "source":

        class FailingSource:
            def __init__(self, project_dir):
                pass

            def resolve(self, ref):
                raise GitHubSourceError(Diagnostic("github_path_invalid", "source failed"))

        monkeypatch.setattr(project_cli, "LocalGitHubSource", FailingSource)
    elif failure == "value":

        class ValueSource:
            def __init__(self, project_dir):
                pass

            def resolve(self, ref):
                raise ValueError("source value failed")

        monkeypatch.setattr(project_cli, "LocalGitHubSource", ValueSource)
    else:

        class OSErrorSource:
            def __init__(self, project_dir):
                pass

            def resolve(self, ref):
                raise OSError("source os failed")

        monkeypatch.setattr(project_cli, "LocalGitHubSource", OSErrorSource)
    assert project_cli.github_review_project(args) == expected
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic"]["code"] == code
    assert fragment in payload["diagnostic"]["message"]


def test_github_front_door_materialization_review_and_plan_errors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)
    args = github_args(tmp_path, publish=False)
    monkeypatch.setattr(project_cli, "LocalGitHubSource", FakeSource)

    class Client:
        config = SimpleNamespace(project=SimpleNamespace(project_id="project-test"))

        @classmethod
        def from_project(cls, project_dir):
            return cls()

        def review(self, request):
            raise ValueError("review failed")

    monkeypatch.setattr(project_cli, "ReviewClient", Client)

    class BrokenContext:
        def __enter__(self):
            raise OSError("materialization failed")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(project_cli, "_materialized_github_files", lambda *a, **k: BrokenContext())
    assert project_cli.github_review_project(args) == 2
    assert "materialization failed" in capsys.readouterr().out

    class BrokenContext2:
        def __enter__(self):
            return (tmp_path / "file.py",)

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(project_cli, "_materialized_github_files", lambda *a, **k: BrokenContext2())
    assert project_cli.github_review_project(args) == 2
    assert "review failed" in capsys.readouterr().out

    monkeypatch.setattr(project_cli, "ReviewClient", FakeClient)
    monkeypatch.setattr(project_cli, "LocalGitHubSource", FakeSource)
    monkeypatch.setattr(
        project_cli,
        "_persist_github_plan",
        lambda *a, **k: (_ for _ in ()).throw(OSError("plan persist failed")),
    )
    assert project_cli.github_review_project(args) == 5
    assert "plan persist failed" in capsys.readouterr().out


def test_github_front_door_invalid_receipt_nonexecutable_and_text_publication(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    write_config(tmp_path)

    class InvalidReceiptClient(FakeClient):
        def review(self, request):
            receipt = Path(request.files[0]).parents[2] / "receipt.json"
            receipt.write_text("not json")
            return ReviewResult("accepted", "review-1", receipt, ())

    monkeypatch.setattr(project_cli, "LocalGitHubSource", FakeSource)
    monkeypatch.setattr(project_cli, "ReviewClient", InvalidReceiptClient)
    assert project_cli.github_review_project(github_args(tmp_path, format="json")) == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["publicationPlan"]["executable"] is False
    (tmp_path / ".reviewctl" / "publication-plan.json").unlink()

    class NoReceiptClient(FakeClient):
        def review(self, request):
            return ReviewResult("accepted", "review-1", Path(), ())

    monkeypatch.setattr(project_cli, "ReviewClient", NoReceiptClient)
    assert (
        project_cli.github_review_project(github_args(tmp_path, format="text", publish=False)) == 5
    )
    assert "diagnostic: receipt_invalid" in capsys.readouterr().out

    FakePublisher.result = None
    monkeypatch.setattr(project_cli, "ReviewClient", FakeClient)
    monkeypatch.setattr(project_cli, "GitHubPublisher", FakePublisher)
    assert project_cli.github_review_project(github_args(tmp_path, format="text")) == 0
    assert "publication: published" in capsys.readouterr().out

    FakePublisher.result = PublicationResult(
        "key", "b" * 40, "failed", diagnostic=Diagnostic("github_publication_failed", "failed")
    )
    monkeypatch.setattr(project_cli, "ReviewClient", FakeClient)
    monkeypatch.setattr(project_cli, "GitHubPublisher", FakePublisher)
    (tmp_path / ".reviewctl" / "publication-plan.json").unlink(missing_ok=True)
    assert project_cli.github_review_project(github_args(tmp_path, format="text")) == 3
    output = capsys.readouterr().out
    assert "publication: failed" in output
    assert "publication diagnostic" in output
    FakePublisher.result = None
