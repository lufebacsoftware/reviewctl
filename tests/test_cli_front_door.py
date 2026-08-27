from __future__ import annotations

import hashlib
import io
import json
import os
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

import reviewctl.project_cli as project_cli
from reviewctl.api import Finding, ReviewResult
from reviewctl.cli import run_cli
from reviewctl.config import load_config
from reviewctl.errors import Diagnostic, JournalOperationError
from reviewctl.journal import ProjectJournal


class _FormatEnum(Enum):
    VALUE = "enum-value"


def test_project_cli_json_and_text_rendering_edges() -> None:
    assert project_cli._json_default(None) is None
    assert project_cli._json_default(1) == 1
    assert project_cli._json_default(Path("file.py")) == "file.py"
    assert project_cli._json_default(_FormatEnum.VALUE) == "enum-value"
    with pytest.raises(TypeError, match="cannot serialize"):
        project_cli._json_default(object())

    diagnostic = Diagnostic("timeout", "timed out", next="retry later")
    result = ReviewResult(
        status="timeout",
        review_id="",
        receipt_path=Path(),
        findings=(
            Finding(
                severity="high",
                path="",
                line=None,
                title="Project issue",
                evidence="evidence",
                reproduction="reproduce",
            ),
        ),
        diagnostic=diagnostic,
    )
    stream = io.StringIO()
    project_cli._print_result(result, "text", stream=stream)
    output = stream.getvalue()
    assert "(project)" in output
    assert "diagnostic: timeout" in output
    assert "next: retry later" in output
    assert project_cli._result_payload(result)["receipt"] is None

    text_result = ReviewResult(
        status="accepted",
        review_id="review-1",
        receipt_path=Path("receipt.json"),
        findings=(
            Finding(
                severity="low",
                path="src/app.py",
                line=3,
                title="Line issue",
                evidence="evidence",
                reproduction="reproduce",
            ),
        ),
    )
    stream = io.StringIO()
    project_cli._print_result(text_result, "text", stream=stream)
    assert "review: review-1" in stream.getvalue()
    assert "receipt: receipt.json" in stream.getvalue()
    assert "src/app.py:3" in stream.getvalue()


def test_project_cli_status_findings_and_status_change_edges(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    class Journal:
        def read_with_diagnostic(self):
            return ([{"reviewId": "r1", "dimensions": ["security"]}], None)

        def findings_with_diagnostic(self, **kwargs):
            return (
                [{"findingId": "f1", "status": "open", "path": "x.py", "message": "msg"}],
                Diagnostic("journal_corrupt", "projection failed"),
            )

        def append_status_change(self, *args, **kwargs):
            pass

        def finding(self, finding_id):
            return None

    class Client:
        def journal(self):
            return Journal()

    (tmp_path / "reviewctl.toml").write_text('[project]\nprivacy_mode = "private"\n')
    config = load_config(tmp_path / "reviewctl.toml", user_path=None)
    Client.config = config
    client = Client()
    args = SimpleNamespace(project=str(tmp_path), dimension="unknown", status=None, format="json")
    monkeypatch.setattr(project_cli.ReviewClient, "from_project", lambda path: client)
    assert project_cli.status_project(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic"]["code"] == "invalid_request"

    args.dimension = "security"
    args.format = "text"
    assert project_cli.status_project(args) == 5
    assert "findings: 1" in capsys.readouterr().out

    args.dimension = None
    args.format = "json"
    assert project_cli.status_project(args) == 5
    capsys.readouterr()

    args.format = "json"
    args.dimension = None
    assert project_cli.findings_project(args) == 5
    assert json.loads(capsys.readouterr().out)["findings"]

    args.format = "text"
    assert project_cli.findings_project(args) == 5
    assert "diagnostic: journal_corrupt" in capsys.readouterr().out

    args.finding_id = "missing"
    args.finding_status = "fixed"
    args.reason = ""
    assert project_cli.set_finding_status(args) == 2
    assert "finding not found" in capsys.readouterr().err


def test_init_creates_private_project_config_and_refuses_accidental_overwrite(
    tmp_path: Path, capsys
) -> None:
    assert run_cli(["init", "--project", str(tmp_path)]) == 0
    config = tmp_path / "reviewctl.toml"
    assert config.is_file()
    assert "[project]" in config.read_text()
    assert os.stat(config).st_mode & 0o777 == 0o600

    assert run_cli(["init", "--project", str(tmp_path)]) == 2
    assert "already exists" in capsys.readouterr().err


def test_init_sensitive_mode_creates_a_loadable_local_only_profile(tmp_path: Path) -> None:
    assert run_cli(["init", "--project", str(tmp_path), "--mode", "sensitive"]) == 0

    config = load_config(tmp_path / "reviewctl.toml", user_path=None)

    assert config.project.privacy_mode == "sensitive"
    assert config.profile("default").execution == "local"
    assert config.profile("default").routes == ()


def test_init_rejects_file_path_and_force_overwrites_existing_config(
    tmp_path: Path, capsys
) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("not a directory")
    assert run_cli(["init", "--project", str(file_path)]) == 2
    assert "not a directory" in capsys.readouterr().err

    assert run_cli(["init", "--project", str(tmp_path)]) == 0
    capsys.readouterr()
    assert run_cli(["init", "--project", str(tmp_path), "--force"]) == 5
    assert "does not match" in capsys.readouterr().err


def test_init_reports_config_write_and_identity_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    args = SimpleNamespace(project=str(tmp_path / "write"), force=False, mode="private")
    monkeypatch.setattr(
        project_cli.os, "open", lambda *args: (_ for _ in ()).throw(OSError("open failed"))
    )
    with pytest.raises(OSError, match="open failed"):
        project_cli.init_project(args)

    project = tmp_path / "identity"
    monkeypatch.undo()
    monkeypatch.setattr(
        project_cli.ProjectIdentityStore,
        "ensure",
        lambda self, project_id: (_ for _ in ()).throw(
            JournalOperationError(Diagnostic("journal_corrupt", "identity failed"))
        ),
    )
    assert (
        project_cli.init_project(SimpleNamespace(project=str(project), force=False, mode="private"))
        == 5
    )
    assert "identity failed" in capsys.readouterr().err

    monkeypatch.setattr(
        project_cli,
        "load_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("config failed")),
    )
    assert (
        project_cli.init_project(
            SimpleNamespace(project=str(tmp_path / "config"), force=False, mode="private")
        )
        == 2
    )
    assert "config failed" in capsys.readouterr().err


def test_review_prompt_file_client_errors_and_exit_rules(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("from file")
    seen = []

    class Client:
        @classmethod
        def from_project(cls, project_dir):
            return cls()

        def review(self, request):
            seen.append(request)
            return ReviewResult("accepted", "r", Path("receipt"), ())

    monkeypatch.setattr(project_cli, "ReviewClient", Client)
    args = SimpleNamespace(
        project=str(tmp_path),
        prompt=None,
        prompt_file=str(prompt_file),
        files=[],
        profile="default",
        review_id=None,
        dimensions=[],
        format="json",
        fail_on=None,
    )
    assert project_cli.review_project(args) == 0
    capsys.readouterr()
    assert seen[0].prompt == "from file"

    class FailingClient:
        @classmethod
        def from_project(cls, project_dir):
            raise JournalOperationError(Diagnostic("journal_corrupt", "client journal failed"))

    monkeypatch.setattr(project_cli, "ReviewClient", FailingClient)
    assert project_cli.review_project(args) == 5
    assert "client journal failed" in capsys.readouterr().out

    class ValueClient:
        @classmethod
        def from_project(cls, project_dir):
            raise ValueError("client config failed")

    monkeypatch.setattr(project_cli, "ReviewClient", ValueClient)
    assert project_cli.review_project(args) == 2
    assert "client config failed" in capsys.readouterr().out

    class ResultClient(Client):
        def review(self, request):
            return ReviewResult(
                "accepted",
                "r",
                Path("receipt"),
                (
                    Finding(
                        severity="critical",
                        path="x.py",
                        line=1,
                        title="bad",
                        evidence="e",
                        reproduction="r",
                    ),
                ),
            )

    monkeypatch.setattr(project_cli, "ReviewClient", ResultClient)
    args.fail_on = "high"
    assert project_cli.review_project(args) == 1
    capsys.readouterr()

    class NonAcceptedClient(Client):
        def review(self, request):
            return ReviewResult("timeout", "r", Path(), ())

    monkeypatch.setattr(project_cli, "ReviewClient", NonAcceptedClient)
    args.fail_on = None
    assert project_cli.review_project(args) == 3
    capsys.readouterr()


def test_findings_status_text_and_client_error_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "reviewctl.toml"
    config_path.write_text('[project]\nprivacy_mode = "private"\n')

    class Journal:
        def append_status_change(self, *args, **kwargs):
            return None

        def finding(self, finding_id):
            return {
                "findingId": finding_id,
                "status": "fixed",
                "message": "done",
                "statusReason": self.reason,
            }

        reason = ""

        def findings_with_diagnostic(self, **kwargs):
            return ([], None)

    journal = Journal()

    class Client:
        config = load_config(config_path, user_path=None)

        def journal(self):
            return journal

    monkeypatch.setattr(project_cli.ReviewClient, "from_project", lambda path: Client())
    args = SimpleNamespace(
        project=str(tmp_path), finding_id="f1", finding_status="fixed", reason="", format="text"
    )
    assert project_cli.set_finding_status(args) == 0
    assert "[fixed] f1" in capsys.readouterr().out
    journal.reason = "patched"
    assert project_cli.set_finding_status(args) == 0
    assert "reason: patched" in capsys.readouterr().out

    class JournalFailureClient:
        @classmethod
        def from_project(cls, path):
            raise JournalOperationError(Diagnostic("journal_corrupt", "journal unavailable"))

    monkeypatch.setattr(project_cli, "ReviewClient", JournalFailureClient)
    assert (
        project_cli.findings_project(
            SimpleNamespace(project=str(tmp_path), status=None, dimension=None, format="json")
        )
        == 5
    )
    assert "journal unavailable" in capsys.readouterr().out

    class ConfigFailureClient:
        @classmethod
        def from_project(cls, path):
            raise ValueError("bad config")

    monkeypatch.setattr(project_cli, "ReviewClient", ConfigFailureClient)
    assert (
        project_cli.status_project(
            SimpleNamespace(project=str(tmp_path), dimension=None, format="json")
        )
        == 2
    )
    assert "bad config" in capsys.readouterr().out

    class ValueFindingsClient:
        @classmethod
        def from_project(cls, path):
            raise ValueError("bad findings config")

    monkeypatch.setattr(project_cli, "ReviewClient", ValueFindingsClient)
    assert (
        project_cli.findings_project(
            SimpleNamespace(project=str(tmp_path), status=None, dimension=None, format="json")
        )
        == 2
    )
    assert "bad findings config" in capsys.readouterr().out

    class JournalErrorStatusClient:
        @classmethod
        def from_project(cls, path):
            raise JournalOperationError(Diagnostic("journal_corrupt", "status journal failed"))

    monkeypatch.setattr(project_cli, "ReviewClient", JournalErrorStatusClient)
    assert (
        project_cli.status_project(
            SimpleNamespace(project=str(tmp_path), dimension=None, format="json")
        )
        == 5
    )
    assert "status journal failed" in capsys.readouterr().out

    class CleanJournal(Journal):
        def findings_with_diagnostic(self, **kwargs):
            return ([{"findingId": "f2", "path": "x.py", "message": "clean"}], None)

    class CleanClient:
        config = Client.config

        def journal(self):
            return CleanJournal()

    monkeypatch.setattr(project_cli.ReviewClient, "from_project", lambda path: CleanClient())
    assert (
        project_cli.findings_project(
            SimpleNamespace(project=str(tmp_path), status=None, dimension=None, format="text")
        )
        == 0
    )
    assert "clean" in capsys.readouterr().out

    class ValueStatusClient:
        @classmethod
        def from_project(cls, path):
            raise ValueError("status change config failed")

    monkeypatch.setattr(project_cli, "ReviewClient", ValueStatusClient)
    assert (
        project_cli.set_finding_status(
            SimpleNamespace(
                project=str(tmp_path),
                finding_id="f1",
                finding_status="fixed",
                reason="",
                format="json",
            )
        )
        == 2
    )
    assert "status change config failed" in capsys.readouterr().out


def test_journal_verify_and_doctor_error_and_text_paths(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "reviewctl.toml"
    config_path.write_text('[project]\nprivacy_mode = "private"\n')
    args = SimpleNamespace(project=str(tmp_path), format="text")
    assert project_cli.doctor_project(args) == 0
    assert "project:" in capsys.readouterr().out

    config_path.write_text("not = [valid")
    assert project_cli.doctor_project(args) == 2
    assert "config_invalid" in capsys.readouterr().err

    class BrokenJournal:
        project_id = "project"
        origin_id = "origin"

        def verify(self):
            return ["broken"]

        def read_with_diagnostic(self):
            return (
                [{"schemaVersion": 1, "projectId": "project", "originId": "origin", "sequence": 1}],
                Diagnostic("journal_corrupt", "bad line"),
            )

        def compatibility(self):
            return "versioned"

    monkeypatch.setattr(
        project_cli,
        "load_config",
        lambda *a, **k: SimpleNamespace(project=SimpleNamespace(project_id="project")),
    )
    monkeypatch.setattr(project_cli.ProjectIdentityStore, "read_existing", lambda self: None)
    monkeypatch.setattr(project_cli, "ProjectJournal", lambda *a, **k: BrokenJournal())
    assert project_cli.verify_journal_project(args) == 5
    output = capsys.readouterr().out
    assert "valid: no" in output and "violation: broken" in output

    class ErrorJournal:
        def __init__(self, *args, **kwargs):
            pass

        def verify(self):
            raise JournalOperationError(Diagnostic("journal_corrupt", "bad line"))

    monkeypatch.setattr(project_cli, "ProjectJournal", ErrorJournal)
    args.format = "text"
    assert project_cli.verify_journal_project(args) == 5
    assert "invalid journal: bad line" in capsys.readouterr().out

    class BrokenIdentity:
        def read_existing(self):
            raise JournalOperationError(Diagnostic("journal_corrupt", "identity malformed"))

    monkeypatch.setattr(project_cli, "ProjectIdentityStore", lambda project: BrokenIdentity())
    args.format = "json"
    assert project_cli.verify_journal_project(args) == 5
    assert json.loads(capsys.readouterr().out)["valid"] is False

    monkeypatch.setattr(
        project_cli, "load_config", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad config"))
    )
    assert project_cli.verify_journal_project(args) == 2
    assert "bad config" in capsys.readouterr().out


def test_review_front_door_renders_typed_result_as_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    class FakeClient:
        @classmethod
        def from_project(cls, project_dir: Path):
            return cls()

        def review(self, request):
            return ReviewResult(
                status="accepted",
                review_id="review-1",
                receipt_path=tmp_path / ".reviewctl/reviews/review-1/receipt.json",
                findings=(),
            )

    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", FakeClient)

    result = run_cli(
        [
            "review",
            "--project",
            str(tmp_path),
            "--prompt",
            "review this",
            "--format",
            "json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "accepted"
    assert payload["reviewId"] == "review-1"


def test_findings_reads_append_only_project_journal(tmp_path: Path, capsys) -> None:
    journal = ProjectJournal(tmp_path / ".reviewctl/journal.jsonl")
    journal.append(
        {
            "type": "finding",
            "reviewId": "review-1",
            "findingId": "finding-1",
            "status": "open",
            "path": "src/app.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    assert (
        run_cli(["findings", "--project", str(tmp_path), "--format", "json", "--status", "open"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["findingId"] == "finding-1"


def test_findings_and_status_filter_by_dimension(tmp_path: Path, capsys) -> None:
    journal = ProjectJournal(tmp_path / ".reviewctl/journal.jsonl")
    journal.append(
        {
            "type": "review_started",
            "reviewId": "review-security",
            "dimensions": ["security"],
        }
    )
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "review-security",
            "findingId": "finding-security",
            "status": "open",
            "path": "src/app.py",
            "message": "Handle the error",
            "dimensions": ["security"],
        }
    )

    assert (
        run_cli(
            [
                "findings",
                "--project",
                str(tmp_path),
                "--dimension",
                "security",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["findingId"] == "finding-security"

    assert (
        run_cli(
            ["status", "--project", str(tmp_path), "--dimension", "security", "--format", "json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["journal"]["reviews"] == 1


def test_doctor_reports_route_and_capability_without_credentials(tmp_path: Path, capsys) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
        'execution = "remote"\n'
    )

    assert run_cli(["doctor", "--project", str(tmp_path), "--format", "json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["privacyMode"] == "private"
    assert payload["portableProjectId"] is False
    default_profile = next(
        profile for profile in payload["profiles"] if profile["name"] == "default"
    )
    assert default_profile["routes"] == ["pi:openrouter/stealth/ox-alpha"]
    assert "OPENROUTER_API_KEY" not in output


def test_review_front_door_maps_transport_diagnostic_to_exit_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    class FakeClient:
        @classmethod
        def from_project(cls, project_dir: Path):
            return cls()

        def review(self, request):
            from reviewctl.errors import Diagnostic

            return ReviewResult(
                status="timeout",
                review_id="review-1",
                receipt_path=tmp_path / ".reviewctl/reviews/review-1/receipt.json",
                findings=(),
                diagnostic=Diagnostic("timeout", "review transport timed out", retryable=True),
            )

    monkeypatch.setattr("reviewctl.project_cli.ReviewClient", FakeClient)

    assert (
        run_cli(
            ["review", "--project", str(tmp_path), "--prompt", "review this", "--format", "json"]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic"]["code"] == "timeout"


def test_verify_accepts_project_receipt_and_rejects_tampering(tmp_path: Path, capsys) -> None:
    unsigned = {"reviewId": "review-1", "configDigest": "config", "status": "accepted"}
    canonical = json.dumps(unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps({**unsigned, "sha256": hashlib.sha256(canonical.encode()).hexdigest()})
    )

    assert run_cli(["verify", str(receipt)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    receipt.write_text(receipt.read_text().replace("accepted", "tampered"))
    assert run_cli(["verify", str(receipt)]) == 5
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_findings_set_status_appends_a_status_event(tmp_path: Path, capsys) -> None:
    journal = ProjectJournal(tmp_path / ".reviewctl/journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "review-1",
            "findingId": "finding-1",
            "status": "open",
            "path": "src/app.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    assert (
        run_cli(
            [
                "findings",
                "set-status",
                "--project",
                str(tmp_path),
                "--id",
                "finding-1",
                "--status",
                "fixed",
                "--reason",
                "patched",
                "--format",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["findingId"] == "finding-1"
    assert payload["status"] == "fixed"
    assert payload["statusReason"] == "patched"
    assert (
        json.loads((tmp_path / ".reviewctl/journal.jsonl").read_text().splitlines()[-1])["type"]
        == "finding_status_changed"
    )


def test_findings_set_status_reports_invalid_transition(tmp_path: Path, capsys) -> None:
    journal = ProjectJournal(tmp_path / ".reviewctl/journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "review-1",
            "findingId": "finding-1",
            "status": "open",
            "path": "src/app.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    assert (
        run_cli(
            [
                "findings",
                "set-status",
                "--project",
                str(tmp_path),
                "--id",
                "finding-1",
                "--status",
                "verified",
                "--format",
                "json",
            ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic"]["code"] == "invalid_request"
    assert "open -> verified" in payload["diagnostic"]["message"]


def test_journal_verify_reports_validity_without_rewriting_bytes(tmp_path: Path, capsys) -> None:
    assert run_cli(["init", "--project", str(tmp_path)]) == 0
    capsys.readouterr()
    journal = ProjectJournal(
        tmp_path / ".reviewctl/journal.jsonl",
        project_id=load_config(tmp_path / "reviewctl.toml", user_path=None).project.project_id,
        origin_id=json.loads((tmp_path / ".reviewctl/identity.json").read_text())["originId"],
    )
    journal.append({"type": "review_started", "reviewId": "r1"})
    before = journal.path.read_bytes()

    assert run_cli(["journal", "verify", "--project", str(tmp_path), "--format", "json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["sequence"] == 1
    assert payload["compatibility"] == "versioned"
    assert journal.path.read_bytes() == before
