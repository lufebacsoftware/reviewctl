from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewctl.api import ReviewClient, ReviewRequest, verify_project_receipt
from reviewctl.backends import BackendEvidence, BackendExecution, PersistedResponse
from reviewctl.errors import JournalOperationError


class FakeTransport:
    def __init__(self, response: str | None = None) -> None:
        self.response = response or '{"verdict":"approved","findings":[]}'

    def execute(self, request):
        return BackendExecution(
            exit_code=0,
            diagnostic="",
            response=PersistedResponse(
                conversation_id="fake-1",
                cost_usd=0.0,
                duration_ms=1,
                input_tokens=1,
                model=request.model,
                output_tokens=1,
                provider="fake",
                response=self.response,
            ),
            evidence=BackendEvidence(),
        )


class QueueTransport:
    def __init__(self, responses: list[str | None]) -> None:
        self.responses = list(responses)
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if response is None:
            return BackendExecution(
                exit_code=1,
                diagnostic="provider unavailable",
                response=None,
                evidence=BackendEvidence(),
            )
        return BackendExecution(
            exit_code=0,
            diagnostic="",
            response=PersistedResponse(
                conversation_id=f"fake-{len(self.requests)}",
                cost_usd=0.0,
                duration_ms=1,
                input_tokens=1,
                model=request.model,
                output_tokens=1,
                provider="fake",
                response=response,
            ),
            evidence=BackendEvidence(),
        )


def write_default_config(project: Path) -> None:
    (project / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        '[profiles.default]\n'
        'routes = ["pi:fake/model"]\n'
        'execution = "local"\n'
    )


def test_client_review_returns_typed_result_and_journal(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    (tmp_path / "a.py").write_text("value = 1\n")
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(tmp_path / "a.py",)))

    assert result.status == "accepted"
    assert result.findings == ()
    assert result.receipt_path.is_file()
    assert [event["type"] for event in client.journal().events()] == [
        "review_started",
        "review_finished",
    ]


def test_client_injects_contract_instructions_into_transport_prompt(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    client.review(ReviewRequest(prompt="review"))

    assert "Return only JSON matching the supplied schema" in transport.requests[0].prompt


def test_client_rejects_review_id_path_traversal(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", review_id="../../escape"))

    assert result.status == "invalid_request"
    assert not (tmp_path.parent / "escape").exists()


def test_client_uses_max_attempts_as_bounded_per_route_retries(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        '[profiles.default]\n'
        'routes = ["pi:first/model"]\n'
        'max_attempts = 2\n'
        'execution = "remote"\n'
    )
    transport = QueueTransport([None, '{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "accepted"
    assert len(transport.requests) == 2
    receipt = json.loads(result.receipt_path.read_text())
    assert [attempt["status"] for attempt in receipt["attempts"]] == [
        "transport_unavailable",
        "accepted",
    ]


def test_client_falls_back_and_keeps_valid_findings_from_partial_attempt(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        '[profiles.default]\n'
        'routes = ["pi:first/model", "pi:second/model"]\n'
        'execution = "remote"\n'
    )
    partial = (
        '{"findings":[{"severity":"high","path":"app.py","line":1,'
        '"title":"Handle failure","evidence":"e","reproduction":"r"}]}'
    )
    complete = '{"verdict":"approved","findings":[]}'
    transport = QueueTransport([partial, complete])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "accepted"
    assert [finding.title for finding in result.findings] == ["Handle failure"]
    assert len(transport.requests) == 2
    receipt = json.loads(result.receipt_path.read_text())
    assert [attempt["status"] for attempt in receipt["attempts"]] == ["partial", "accepted"]
    assert receipt["fallbackRelationships"][0]["from"] == "pi:first/model"


def test_project_receipt_verification_detects_tampering(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))

    assert verify_project_receipt(result.receipt_path) is None
    receipt = json.loads(result.receipt_path.read_text())
    receipt["status"] = "tampered"
    result.receipt_path.write_text(json.dumps(receipt))
    diagnostic = verify_project_receipt(result.receipt_path)
    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"


def test_client_maps_malformed_findings_payload_to_contract_failure(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(
        tmp_path,
        transports={"pi": FakeTransport(response="not-json")},
    )

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "contract_failed"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "contract_failed"


def test_client_does_not_require_cli_parser(tmp_path: Path) -> None:
    write_default_config(tmp_path)

    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    assert client.config.project.privacy_mode == "private"


def test_review_receipt_is_json_and_records_route(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())

    assert receipt["route"] == "pi:fake/model"
    assert receipt["status"] == "accepted"


def test_client_reuses_finding_identity_across_reviews(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    response = (
        '{"verdict":"changes-requested","findings":[{"severity":"high",'
        '"path":"app.py","line":1,"title":"Handle failure",'
        '"evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(response)})

    first = client.review(ReviewRequest(prompt="review one"))
    second = client.review(ReviewRequest(prompt="review two"))

    assert first.status == "accepted"
    assert second.status == "accepted"
    findings = client.journal().findings()
    assert len(findings) == 1
    assert findings[0]["observations"] == 2
    assert findings[0]["firstReviewId"] == first.review_id
    assert findings[0]["lastReviewId"] == second.review_id

    receipt = json.loads(second.receipt_path.read_text())
    assert receipt["findings"][0]["findingId"] == findings[0]["findingId"]


def test_reobservation_does_not_reopen_a_fixed_finding(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    response = (
        '{"verdict":"changes-requested","findings":[{"severity":"high",'
        '"path":"app.py","line":1,"title":"Handle failure",'
        '"evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(response)})

    first = client.review(ReviewRequest(prompt="review one"))
    finding_id = client.journal().findings()[0]["findingId"]
    client.journal().append_status_change(finding_id, "fixed", reason="patched")
    second = client.review(ReviewRequest(prompt="review two"))

    assert first.status == "accepted"
    assert second.status == "accepted"
    finding = client.journal().finding(finding_id)
    assert finding is not None
    assert finding["status"] == "fixed"
    assert finding["observations"] == 2


def test_client_findings_does_not_hide_journal_corruption(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    journal_path = tmp_path / ".reviewctl/journal.jsonl"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text('{"type":"finding_status_changed","findingId":"f1"}\n')
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    with pytest.raises(JournalOperationError) as error:
        client.findings()

    assert error.value.diagnostic.code == "journal_corrupt"
