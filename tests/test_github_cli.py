from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from reviewctl.api import Finding, ReviewResult
from reviewctl.cli import run_cli
from reviewctl.github import ChangedFileSnapshot, PullRequestRef, PullRequestSnapshot
from reviewctl.github_publisher import PublicationResult


def write_config(project: Path) -> None:
    (project / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:fake/model"]\n'
        'execution = "local"\n'
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
        )

    def journal(self):
        return self._journal


class FakePublisher:
    plans = []

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def publish(self, plan):
        type(self).plans.append(plan)
        return PublicationResult(
            publication_key="github:example/project:7:review-1",
            head_sha="b" * 40,
            status="published",
            published_comment_ids=("9002",),
            summary_comment_id="9001",
        )


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
