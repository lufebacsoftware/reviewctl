from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import reviewctl.api as api_module
from reviewctl.api import (
    MAX_SOURCE_BYTES,
    Finding,
    ReviewClient,
    ReviewRequest,
    verify_project_receipt,
)
from reviewctl.backends import BackendEvidence, BackendExecution, PersistedResponse
from reviewctl.contracts import ContractFragment, EvaluationStatus, FragmentKind
from reviewctl.errors import JournalOperationError
from reviewctl.pi_transport import PiTransport


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
        "[profiles.default]\n"
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
        "[profiles.default]\n"
        'routes = ["pi:first/model"]\n'
        "max_attempts = 2\n"
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
        "[profiles.default]\n"
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


def test_client_rejects_oversized_source_before_reading_it(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    oversized = tmp_path / "oversized.txt"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_SOURCE_BYTES + 1)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(oversized,)))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "exceeds" in result.diagnostic.message


def test_review_records_sorted_dimensions_and_unresolved_coverage(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", dimensions=("security", "architecture")))

    assert result.status == "accepted"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["dimensionSchemaVersion"] == 1
    assert receipt["dimensions"] == ["architecture", "correctness", "security"]
    assert receipt["dimensionCoverage"] == {
        "requested": ["architecture", "correctness", "security"],
        "observed": [],
        "unresolved": ["architecture", "correctness", "security"],
    }
    started = client.journal().events()[0]
    assert started["dimensions"] == ["architecture", "correctness", "security"]


def test_review_rejects_duplicate_request_dimensions(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review", dimensions=("security", "security")))

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "duplicate" in result.diagnostic.message


def test_review_receipt_is_json_and_records_route(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})

    result = client.review(ReviewRequest(prompt="review"))
    receipt = json.loads(result.receipt_path.read_text())

    assert receipt["route"] == "pi:fake/model"
    assert receipt["status"] == "accepted"
    assert receipt["projectId"].startswith("project-")
    assert receipt["originId"].startswith("origin-")
    assert receipt["journalSequence"] >= 1


def test_review_persists_sanitized_source_context_in_packet_receipt_and_journal(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    context = {
        "kind": "github_pull_request",
        "repository": "example/project",
        "pullNumber": 7,
        "baseSha": "a" * 40,
        "headSha": "b" * 40,
        "snapshotSha256": "c" * 64,
        "diffSha256": "d" * 64,
        "changedFiles": [{"path": "src/app.py", "sha256": "e" * 64}],
        "evidence": ["github.pull_request"],
    }

    result = client.review(ReviewRequest(prompt="review", source_context=context))

    assert result.status == "accepted"
    packet = json.loads(result.receipt_path.with_name("packet.json").read_text())
    receipt = json.loads(result.receipt_path.read_text())
    started = client.journal().events()[0]
    assert packet["sourceContext"] == context
    assert receipt["sourceContext"] == context
    assert started["sourceContext"] == context


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


def test_finding_requires_fields_and_integer_line() -> None:
    with pytest.raises(ValueError, match="required"):
        Finding.from_value({"severity": "high"})
    value = {
        "severity": "high",
        "path": "source.py",
        "line": "3",
        "title": "Title",
        "evidence": "Evidence",
        "reproduction": "Reproduce",
    }
    with pytest.raises(ValueError, match="integer"):
        Finding.from_value(value)


def test_private_api_helpers_cover_ids_and_execution_diagnostics() -> None:
    value = Finding("high", "source.py", None, "Title", "Evidence", "Reproduce")
    assert api_module.finding_id(value).startswith("finding-")
    assert api_module._review_id("  explicit ") == "explicit"
    assert (
        api_module._execution_diagnostic(BackendExecution(124, "", None, BackendEvidence())).code
        == "timeout"
    )
    assert (
        api_module._execution_diagnostic(
            BackendExecution(1, "provider failed", None, BackendEvidence())
        ).code
        == "transport_unavailable"
    )
    assert (
        api_module._execution_diagnostic(BackendExecution(0, "", None, BackendEvidence())).code
        == "empty_response"
    )


@pytest.mark.parametrize(
    "value",
    [[], {"bad": object()}, "not-object"],
)
def test_source_context_rejects_nonmapping_unsafe_and_nonobject_values(value: object) -> None:
    with pytest.raises(ValueError, match="source context"):
        api_module._normalize_source_context(value)  # type: ignore[arg-type]


def test_source_context_rejects_oversized_encoded_value() -> None:
    with pytest.raises(ValueError, match="limit"):
        api_module._normalize_source_context({"payload": "x" * 33_000})


def test_source_context_rejects_nonobject_decoded_value(monkeypatch) -> None:
    monkeypatch.setattr(api_module.json, "loads", lambda encoded: [])
    with pytest.raises(ValueError, match="object"):
        api_module._normalize_source_context({"value": 1})


def test_client_rejects_empty_prompt_unknown_profile_and_bad_dimensions(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    assert client.review(ReviewRequest(prompt=" ")).status == "invalid_request"
    assert (
        client.review(ReviewRequest(prompt="review", profile="missing")).status == "config_invalid"
    )
    assert (
        client.review(ReviewRequest(prompt="review", dimensions=("security", "security"))).status
        == "invalid_request"
    )


def test_client_rejects_sensitive_remote_and_empty_route_profiles(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        '[project]\nprivacy_mode = "private"\n'
        '[profiles.remote]\nroutes = ["pi:model"]\nexecution = "remote"\n'
        '[profiles.empty]\nroutes = []\nexecution = "local"\n'
    )
    config = api_module.load_config(tmp_path, user_path=None)
    config = replace(config, project=replace(config.project, privacy_mode="sensitive"))
    client = ReviewClient(tmp_path, config, {"pi": FakeTransport()})
    assert (
        client.review(ReviewRequest(prompt="review", profile="remote")).status == "privacy_denied"
    )
    assert client.review(ReviewRequest(prompt="review", profile="empty")).status == "route_invalid"


def test_from_project_constructs_default_pi_transport(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path)
    assert isinstance(client.transports["pi"], PiTransport)


def test_client_rejects_explicit_and_duplicate_review_ids(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    first = client.review(ReviewRequest(prompt="review", review_id="explicit"))
    second = client.review(ReviewRequest(prompt="review", review_id="explicit"))
    assert first.status == "accepted"
    assert second.status == "invalid_request"


def test_client_rejects_relative_outside_missing_and_invalid_utf8_files(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    (tmp_path / "valid.py").write_text("value = 1\n")
    assert (
        client.review(ReviewRequest(prompt="review", files=(Path("valid.py"),))).status
        == "accepted"
    )
    assert (
        client.review(ReviewRequest(prompt="review", files=(Path("missing.py"),))).status
        == "invalid_request"
    )
    assert (
        client.review(
            ReviewRequest(prompt="review", files=(tmp_path.parent / "outside.py",))
        ).status
        == "privacy_denied"
    )
    invalid = tmp_path / "invalid.py"
    invalid.write_bytes(b"\xff\xfe")
    result = client.review(ReviewRequest(prompt="review", files=(invalid,)))
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "UTF-8" in result.diagnostic.message


def test_client_rejects_duplicate_source_basenames_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    write_default_config(tmp_path)
    first = tmp_path / "first" / "entry.py"
    second = tmp_path / "second" / "entry.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first = 1\n")
    second.write_text("second = 2\n")
    transport = QueueTransport(['{"verdict":"approved","findings":[]}'])
    client = ReviewClient.from_project(tmp_path, transports={"pi": transport})

    result = client.review(
        ReviewRequest(
            prompt="review",
            review_id="duplicate-basename",
            files=(first, second),
        )
    )

    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "invalid_request"
    assert "unique basenames" in result.diagnostic.message
    assert transport.requests == []
    assert not client.review_root.exists()


def test_client_rejects_non_json_source_context(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review", source_context=object()))
    assert result.status == "invalid_request"


def test_client_rejects_read_errors_and_post_stat_growth(tmp_path: Path, monkeypatch) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n")
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    original_read = Path.read_bytes

    def fail_read(path: Path) -> bytes:
        if path == source:
            raise OSError("denied")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    result = client.review(ReviewRequest(prompt="review", files=(source,)))
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "could not be read" in result.diagnostic.message

    monkeypatch.setattr(Path, "read_bytes", lambda path: b"x" * (MAX_SOURCE_BYTES + 1))
    result = client.review(ReviewRequest(prompt="review", files=(source,)))
    assert result.status == "invalid_request"
    assert result.diagnostic is not None
    assert "exceeds" in result.diagnostic.message


def test_client_rejects_unknown_contract_and_unregistered_transport(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["llm:model"]\nresponse_contract = "unknown-contract"\n'
    )
    client = ReviewClient.from_project(tmp_path, transports={})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "contract_failed"

    (tmp_path / "reviewctl.toml").write_text('[profiles.default]\nroutes = ["llm:model"]\n')
    client = ReviewClient.from_project(tmp_path, transports={})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "transport_unavailable"

    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["llm:first", "llm:second"]\n'
    )
    client = ReviewClient.from_project(tmp_path, transports={})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "transport_unavailable"


class RaisingTransport:
    def execute(self, request):
        raise ValueError("transport exploded")


class EmptyTransport:
    def execute(self, request):
        return BackendExecution(
            0, "", PersistedResponse("id", 0, 1, 1, "model", 1, "provider", ""), BackendEvidence()
        )


def test_client_maps_transport_and_empty_response_failures(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    raising = ReviewClient.from_project(tmp_path, transports={"pi": RaisingTransport()})
    assert raising.review(ReviewRequest(prompt="review")).status == "transport_unavailable"
    empty = ReviewClient.from_project(tmp_path, transports={"pi": EmptyTransport()})
    result = empty.review(ReviewRequest(prompt="review"))
    assert result.status == "empty_response"

    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["pi:first", "pi:second"]\n'
    )
    raising = ReviewClient.from_project(tmp_path, transports={"pi": RaisingTransport()})
    result = raising.review(ReviewRequest(prompt="review"))
    assert result.status == "transport_unavailable"


def test_client_rejects_failed_execution_with_parseable_response(tmp_path: Path) -> None:
    write_default_config(tmp_path)

    class FailedExecutionTransport:
        def execute(self, request):
            return BackendExecution(
                exit_code=1,
                diagnostic="provider failed",
                response=PersistedResponse(
                    conversation_id="failed-but-parseable",
                    cost_usd=0.0,
                    duration_ms=1,
                    input_tokens=1,
                    model=request.model,
                    output_tokens=1,
                    provider="fake",
                    response='{"verdict":"approved","findings":[]}',
                ),
                evidence=BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": FailedExecutionTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "transport_unavailable"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "transport_unavailable"


def test_client_freezes_source_bytes_across_fallback_attempts(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
    )
    source = tmp_path / "source.py"
    initial = b"value = 1\n"
    source.write_bytes(initial)
    seen: list[bytes] = []

    class MutatingFallbackTransport:
        def execute(self, request):
            seen.append(request.files[0].read_bytes())
            if len(seen) == 1:
                source.write_bytes(b"value = 2\n")
                request.files[0].write_bytes(b"changed by first attempt\n")
                return BackendExecution(1, "provider failed", None, BackendEvidence())
            return BackendExecution(
                0,
                "",
                PersistedResponse(
                    "conversation",
                    0.0,
                    1,
                    1,
                    request.model,
                    1,
                    "fake",
                    '{"verdict":"approved","findings":[]}',
                ),
                BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": MutatingFallbackTransport()})

    result = client.review(ReviewRequest(prompt="review", files=(source,)))

    assert result.status == "accepted"
    assert seen == [initial, initial]
    packet = json.loads(result.receipt_path.with_name("packet.json").read_text())
    assert packet["files"][0]["sha256"] == api_module._digest(initial)
    packet_digest = api_module._digest(
        json.dumps(packet, ensure_ascii=True, sort_keys=True).encode()
    )
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["packetDigest"] == packet_digest
    assert verify_project_receipt(result.receipt_path) is None


@pytest.mark.parametrize(
    ("conversation_id", "resolved_model"),
    [(None, "fake/model"), ("", "fake/model"), ("conversation", "other/model")],
)
def test_client_rejects_invalid_response_identity_before_contract(
    tmp_path: Path,
    monkeypatch,
    conversation_id: str | None,
    resolved_model: str,
) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[project]\nprivacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:first/model", "pi:second/model"]\n'
    )
    real_contract = api_module.get_contract("findings-json")

    class UnreachableEvaluation:
        def prepare(self, context):
            return real_contract.prepare(context)

        def evaluate(self, *args, **kwargs):
            raise AssertionError("response identity must be checked before contract evaluation")

    monkeypatch.setattr(api_module, "get_contract", lambda name: UnreachableEvaluation())

    class InvalidIdentityTransport:
        def execute(self, request):
            return BackendExecution(
                0,
                "",
                PersistedResponse(
                    conversation_id,
                    0.0,
                    1,
                    1,
                    resolved_model,
                    1,
                    "fake",
                    '{"verdict":"approved","findings":[]}',
                ),
                BackendEvidence(),
            )

    client = ReviewClient.from_project(tmp_path, transports={"pi": InvalidIdentityTransport()})

    result = client.review(ReviewRequest(prompt="review"))

    assert result.status == "transport_unavailable"
    assert result.diagnostic is not None
    assert result.diagnostic.code == "transport_unavailable"


def test_merge_findings_deduplicates_identical_values() -> None:
    value = Finding("high", "source.py", 1, "title", "evidence", "reproduction")
    assert ReviewClient._merge_findings((value,), (value,)) == (value,)


def test_client_maps_contract_evaluation_exception(tmp_path: Path, monkeypatch) -> None:
    write_default_config(tmp_path)
    real_contract = api_module.get_contract("findings-json")

    class RaisingContract:
        def prepare(self, context):
            return real_contract.prepare(context)

        def evaluate(self, *args, **kwargs):
            raise ValueError("evaluation exploded")

    monkeypatch.setattr(api_module, "get_contract", lambda name: RaisingContract())
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "contract_failed"


def test_client_handles_malformed_complete_and_incomplete_findings(
    tmp_path: Path, monkeypatch
) -> None:
    write_default_config(tmp_path)
    real_contract = api_module.get_contract("findings-json")
    original_evaluate = real_contract.evaluate
    state = {"status": EvaluationStatus.COMPLETE}

    class MalformedContract:
        def prepare(self, context):
            return real_contract.prepare(context)

        def evaluate(self, payload, prepared, context, *, evidence=None):
            base = original_evaluate('{"verdict":"approved","findings":[]}', prepared, context)
            if state["status"] is EvaluationStatus.COMPLETE:
                return base.__class__(**{**base.__dict__, "value": {"findings": [{"bad": True}]}})
            fragment = ContractFragment(
                "f", "f", FragmentKind.FINDING, {"bad": True}, base.payload_digest, ()
            )
            return base.__class__(
                **{
                    **base.__dict__,
                    "status": EvaluationStatus.INCOMPLETE,
                    "value": None,
                    "normalized_digest": None,
                    "valid_fragments": (fragment,),
                }
            )

    monkeypatch.setattr(api_module, "get_contract", lambda name: MalformedContract())
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    assert client.review(ReviewRequest(prompt="review")).status == "contract_failed"
    state["status"] = EvaluationStatus.INCOMPLETE
    assert client.review(ReviewRequest(prompt="review")).status == "contract_failed"


def test_client_returns_partial_when_all_attempts_are_incomplete(tmp_path: Path) -> None:
    (tmp_path / "reviewctl.toml").write_text(
        '[profiles.default]\nroutes = ["pi:model"]\nexecution = "local"\n'
    )
    partial = (
        '{"findings":[{"severity":"high","path":"source.py","line":1,'
        '"title":"t","evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(partial)})
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "partial"
    assert len(result.findings) == 1


def test_verify_project_receipt_rejects_unreadable_and_missing_digest(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    diagnostic = verify_project_receipt(missing)
    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"
    no_digest = tmp_path / "no-digest.json"
    no_digest.write_text("{}")
    diagnostic = verify_project_receipt(no_digest)
    assert diagnostic is not None
    assert diagnostic.code == "receipt_invalid"
