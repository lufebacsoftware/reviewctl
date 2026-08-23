from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from reviewctl.api import ReviewResult
from reviewctl.cli import run_cli
from reviewctl.config import load_config
from reviewctl.journal import ProjectJournal


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

    assert run_cli(
        ["findings", "--project", str(tmp_path), "--format", "json", "--status", "open"]
    ) == 0
    assert json.loads(capsys.readouterr().out)[0]["findingId"] == "finding-1"


def test_doctor_reports_route_and_capability_without_credentials(tmp_path: Path, capsys) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        "[project]\nprivacy_mode = \"private\"\n"
        "[profiles.default]\n"
        "routes = [\"pi:openrouter/stealth/ox-alpha\"]\n"
        "execution = \"remote\"\n"
    )

    assert run_cli(["doctor", "--project", str(tmp_path), "--format", "json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["privacyMode"] == "private"
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

    assert run_cli(
        ["review", "--project", str(tmp_path), "--prompt", "review this", "--format", "json"]
    ) == 3
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

    assert run_cli(
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
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["findingId"] == "finding-1"
    assert payload["status"] == "fixed"
    assert payload["statusReason"] == "patched"
    assert json.loads((tmp_path / ".reviewctl/journal.jsonl").read_text().splitlines()[-1])[
        "type"
    ] == "finding_status_changed"


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

    assert run_cli(
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
    ) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic"]["code"] == "invalid_request"
    assert "open -> verified" in payload["diagnostic"]["message"]
