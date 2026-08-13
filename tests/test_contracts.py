import hashlib
import json
from copy import copy, deepcopy

import pytest

from reviewctl.contracts import (
    ContractContext,
    ContractEvaluation,
    EvaluationContext,
    EvaluationStatus,
    FragmentKind,
    canonical_json,
    get_contract,
)


def test_findings_contract_prepares_a_stable_portable_contract() -> None:
    prepared = get_contract("findings-json").prepare(ContractContext())

    assert prepared.name == "findings-json"
    assert prepared.version == "1"
    assert prepared.schema["required"] == ["verdict", "findings"]
    assert "reviewedFiles" not in prepared.schema["properties"]
    assert prepared.digest == hashlib.sha256(canonical_json(prepared.identity_material)).hexdigest()
    assert "changes-requested" in prepared.output_instructions
    assert "approved" in prepared.output_instructions


def test_findings_contract_can_require_a_review_declaration_without_mutating_portable_schema() -> (
    None
):
    contract = get_contract("findings-json")

    declared = contract.prepare(
        ContractContext(file_names=("source.py",), review_declaration_required=True)
    )
    portable = contract.prepare(ContractContext(file_names=("source.py",)))

    assert declared.schema["required"] == ["verdict", "findings", "reviewedFiles"]
    assert declared.schema["properties"]["reviewedFiles"] == {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "minLength": 1},
    }
    assert "reviewedFiles" not in portable.schema["properties"]
    assert declared.digest != portable.digest


def test_contract_registry_rejects_unknown_contracts() -> None:
    try:
        get_contract("unknown")
    except KeyError as error:
        assert error.args == ("unknown",)
    else:
        raise AssertionError("unknown contract was accepted")


def finding_payload(**finding_overrides: object) -> dict[str, object]:
    finding = {
        "severity": "high",
        "path": "source.py",
        "line": 3,
        "title": "Duplicate effect",
        "evidence": "The same key reaches the write twice.",
        "reproduction": "Submit the same key twice.",
    }
    finding.update(finding_overrides)
    return {"verdict": "changes-requested", "findings": [finding]}


def test_findings_contract_evaluates_and_hashes_a_normalized_value() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload(), indent=2)

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.value == finding_payload()
    assert evaluation.violations == ()
    assert evaluation.payload_digest == hashlib.sha256(payload.encode()).hexdigest()
    assert (
        evaluation.normalized_digest
        == hashlib.sha256(canonical_json(finding_payload())).hexdigest()
    )
    assert evaluation.status is EvaluationStatus.COMPLETE
    assert len(evaluation.valid_fragments) == 1
    assert evaluation.valid_fragments[0].kind is FragmentKind.FINDING
    assert evaluation.valid_fragments[0].value == finding_payload()["findings"][0]
    assert evaluation.valid_fragments[0].scope == ("source.py",)
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == ("verdict", "findings")
    assert evaluation.coverage.covered_fields == ("verdict", "findings")
    assert evaluation.coverage.missing_fields == ()
    assert evaluation.completion_request is None


def test_findings_contract_normalizes_a_required_review_declaration() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(
        file_names=("source.py", "test_source.py"), review_declaration_required=True
    )
    prepared = contract.prepare(context)
    value = finding_payload()
    value["reviewedFiles"] = [
        "/private/tmp/reviewctl-input-abc/source.py",
        "test_source.py",
    ]

    evaluation = contract.evaluate(json.dumps(value), prepared, context)

    assert evaluation.violations == ()
    assert evaluation.value == {
        **finding_payload(),
        "reviewedFiles": ["source.py", "test_source.py"],
    }
    assert evaluation.status is EvaluationStatus.COMPLETE
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )
    assert evaluation.coverage.covered_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )


def test_findings_contract_rejects_duplicate_json_fields() -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)

    evaluation = contract.evaluate(
        '{"verdict":"approved","verdict":"changes-requested","findings":[]}',
        prepared,
        context,
    )

    assert evaluation.value is None
    assert evaluation.normalized_digest is None
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_mutated_prepared_material() -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)
    prepared.schema["additionalProperties"] = True

    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": []}), prepared, context
    )

    assert evaluation.value is None
    assert evaluation.violations == ("prepared-contract",)
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()


def test_findings_contract_rejects_a_prepared_contract_from_another_context() -> None:
    contract = get_contract("findings-json")
    prepared = contract.prepare(ContractContext())
    declaration_context = ContractContext(
        file_names=("source.py",), review_declaration_required=True
    )

    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": []}),
        prepared,
        declaration_context,
    )

    assert evaluation.value is None
    assert evaluation.violations == ("prepared-contract",)


def test_findings_contract_rejects_file_context_substitution() -> None:
    contract = get_contract("findings-json")
    prepared = contract.prepare(
        ContractContext(file_names=("source.py",), review_declaration_required=True)
    )
    substituted_context = ContractContext(
        file_names=("other.py",), review_declaration_required=True
    )

    evaluation = contract.evaluate(
        json.dumps({"verdict": "approved", "findings": [], "reviewedFiles": ["other.py"]}),
        prepared,
        substituted_context,
    )

    assert evaluation.value is None
    assert evaluation.violations == ("prepared-contract",)


def invalid_contract_cases() -> list[tuple[str, ContractContext, str]]:
    missing_field = finding_payload()
    del missing_field["findings"][0]["title"]  # type: ignore[index]
    extra_field = finding_payload(extra="not allowed")
    declaration = finding_payload()
    declaration["reviewedFiles"] = ["source.py", "source.py"]
    return [
        ("not json", ContractContext(), "invalid-json"),
        ("[]", ContractContext(), "top-level-not-object"),
        (
            json.dumps({"verdict": "approved", "findings": [], "extra": True}),
            ContractContext(),
            "response-fields",
        ),
        (
            json.dumps({"verdict": "unavailable", "findings": []}),
            ContractContext(),
            "verdict",
        ),
        (
            json.dumps({"verdict": "changes-requested", "findings": {}}),
            ContractContext(),
            "findings-shape",
        ),
        (json.dumps(missing_field), ContractContext(), "finding-fields"),
        (json.dumps(extra_field), ContractContext(), "finding-fields"),
        (json.dumps(finding_payload(title="  ")), ContractContext(), "finding-value"),
        (json.dumps(finding_payload(line=True)), ContractContext(), "finding-value"),
        (
            json.dumps(finding_payload(path="invented.py")),
            ContractContext(file_names=("source.py",)),
            "finding-path",
        ),
        (
            json.dumps({"verdict": "approved", "findings": finding_payload()["findings"]}),
            ContractContext(),
            "verdict-invariant",
        ),
        (
            json.dumps(declaration),
            ContractContext(file_names=("source.py",), review_declaration_required=True),
            "review-declaration",
        ),
        (
            json.dumps({"verdict": "approved", "findings": [], "reviewedFiles": ["../source.py"]}),
            ContractContext(file_names=("source.py",), review_declaration_required=True),
            "review-declaration",
        ),
        (
            json.dumps(
                {
                    "verdict": "approved",
                    "findings": [],
                    "reviewedFiles": ["unrelated/source.py"],
                }
            ),
            ContractContext(file_names=("source.py",), review_declaration_required=True),
            "review-declaration",
        ),
    ]


def test_findings_contract_reports_stable_semantic_violation_codes() -> None:
    contract = get_contract("findings-json")

    for payload, context, expected_code in invalid_contract_cases():
        prepared = contract.prepare(context)
        evaluation = contract.evaluate(payload, prepared, context)

        assert evaluation.value is None, expected_code
        assert evaluation.normalized_digest is None, expected_code
        assert evaluation.violations == (expected_code,)


@pytest.mark.parametrize(
    ("payload", "expected_violation"),
    [
        ('{"hostile":' + ("9" * 5000) + "}", "invalid-json"),
        (("[" * 2000) + "0" + ("]" * 2000), "top-level-not-object"),
    ],
)
def test_findings_contract_rejects_hostile_json_parser_limits(
    payload: str, expected_violation: str
) -> None:
    contract = get_contract("findings-json")
    context = ContractContext()

    evaluation = contract.evaluate(payload, contract.prepare(context), context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == (expected_violation,)


def test_contract_evaluation_additive_defaults_preserve_positional_compatibility() -> None:
    evaluation = ContractEvaluation(
        "findings-json",
        "1",
        "prepared",
        "payload",
        None,
        None,
        ("invalid-json",),
    )

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_finding_fingerprint_ignores_payload_formatting_but_fragment_id_does_not() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    compact = json.dumps(finding_payload(), separators=(",", ":"))
    formatted = json.dumps(finding_payload(), indent=2)

    compact_fragment = contract.evaluate(compact, prepared, context).valid_fragments[0]
    formatted_fragment = contract.evaluate(formatted, prepared, context).valid_fragments[0]

    assert compact_fragment.fingerprint == formatted_fragment.fingerprint
    assert compact_fragment.fragment_id != formatted_fragment.fragment_id
    assert compact_fragment.payload_digest == hashlib.sha256(compact.encode()).hexdigest()
    expected_fingerprint = hashlib.sha256(
        canonical_json(
            {
                "contract": "findings-json",
                "version": "1",
                "kind": "finding",
                "value": finding_payload()["findings"][0],
                "scope": ["source.py"],
            }
        )
    ).hexdigest()
    assert compact_fragment.fingerprint == expected_fingerprint
    assert (
        compact_fragment.fragment_id
        == hashlib.sha256(
            canonical_json(
                {
                    "fingerprint": expected_fingerprint,
                    "payloadDigest": compact_fragment.payload_digest,
                }
            )
        ).hexdigest()
    )


def test_mixed_findings_extract_only_valid_siblings_and_request_completion() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    valid_finding = finding_payload()["findings"][0]
    invalid_finding = {**valid_finding, "severity": "urgent"}
    payload = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [invalid_finding, valid_finding, {**valid_finding, "path": "other.py"}],
        }
    )

    evaluation = contract.evaluate(
        payload,
        prepared,
        context,
        evidence=EvaluationContext(packet_digest="packet-sha256"),
    )

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert evaluation.value is None
    assert evaluation.normalized_digest is None
    assert evaluation.violations == ("finding-value",)
    assert [fragment.value for fragment in evaluation.valid_fragments] == [valid_finding]
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == ("verdict", "findings")
    assert evaluation.coverage.covered_fields == ("verdict",)
    assert evaluation.coverage.missing_fields == ("findings",)
    assert not set(evaluation.coverage.covered_fields).intersection(
        evaluation.coverage.missing_fields
    )
    assert evaluation.completion_request is not None
    assert evaluation.completion_request.prepared_digest == prepared.digest
    assert evaluation.completion_request.packet_digest == "packet-sha256"
    assert evaluation.completion_request.missing_fields == ("findings",)
    assert evaluation.completion_request.invalid_fragment_indexes == (0, 2)
    assert evaluation.completion_request.violations == ("finding-value",)


def test_valid_finding_can_survive_invalid_top_level_requirements() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",), review_declaration_required=True)
    prepared = contract.prepare(context)
    value = finding_payload()
    value["verdict"] = "unavailable"
    value["extra"] = True

    evaluation = contract.evaluate(json.dumps(value), prepared, context)

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert len(evaluation.valid_fragments) == 1
    assert evaluation.violations == ("response-fields",)
    assert evaluation.coverage is not None
    assert evaluation.coverage.required_fields == (
        "verdict",
        "findings",
        "reviewedFiles",
    )
    assert evaluation.coverage.covered_fields == ("findings",)
    assert evaluation.coverage.missing_fields == ("verdict", "reviewedFiles")
    assert evaluation.completion_request is not None
    assert evaluation.completion_request.invalid_fragment_indexes == ()


def test_verdict_invariant_with_a_valid_finding_is_incomplete_without_verdict_fragment() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    value = finding_payload()
    value["verdict"] = "approved"

    evaluation = contract.evaluate(json.dumps(value), prepared, context)

    assert evaluation.status is EvaluationStatus.INCOMPLETE
    assert evaluation.violations == ("verdict-invariant",)
    assert len(evaluation.valid_fragments) == 1
    assert all(fragment.kind is FragmentKind.FINDING for fragment in evaluation.valid_fragments)
    assert evaluation.coverage is not None
    assert evaluation.coverage.covered_fields == ("findings",)
    assert evaluation.coverage.missing_fields == ("verdict",)


def test_invalid_findings_do_not_extract_or_make_response_incomplete() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)

    for overrides in (
        {"severity": "urgent"},
        {"path": "other.py"},
        {"title": ""},
        {"extra": "field"},
    ):
        evaluation = contract.evaluate(json.dumps(finding_payload(**overrides)), prepared, context)

        assert evaluation.status is EvaluationStatus.INVALID
        assert evaluation.valid_fragments == ()
        assert evaluation.completion_request is None


def test_missing_findings_and_approved_response_never_extract_fragments() -> None:
    contract = get_contract("findings-json")
    context = ContractContext()
    prepared = contract.prepare(context)

    for value in (
        {"verdict": "changes-requested"},
        {"verdict": "approved", "findings": []},
    ):
        evaluation = contract.evaluate(json.dumps(value), prepared, context)

        assert evaluation.valid_fragments == ()
        assert evaluation.completion_request is None
    assert (
        contract.evaluate(
            json.dumps({"verdict": "approved", "findings": []}), prepared, context
        ).status
        is EvaluationStatus.COMPLETE
    )


def test_exact_json_object_and_prepared_contract_are_required_before_extraction() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    finding = json.dumps(finding_payload()["findings"][0])
    payloads = (
        f'{{"verdict":"changes-requested","findings":[{finding}],"findings":[]}}',
        f"[{finding}]",
        f'{{"verdict":"changes-requested","findings":[{finding}]',
    )

    for payload in payloads:
        evaluation = contract.evaluate(payload, prepared, context)
        assert evaluation.status is EvaluationStatus.INVALID
        assert evaluation.valid_fragments == ()
        assert evaluation.coverage is None

    prepared.schema["additionalProperties"] = True
    evaluation = contract.evaluate(json.dumps(finding_payload()), prepared, context)
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.valid_fragments == ()
    assert evaluation.violations == ("prepared-contract",)


def assert_non_finite_json_is_rejected(constant: str) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    finding = json.dumps(finding_payload()["findings"][0])
    payload = f'{{"verdict":"changes-requested","findings":[{finding}],"nonFinite":{constant}}}'

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_nan_before_fragment_extraction() -> None:
    assert_non_finite_json_is_rejected("NaN")


def test_findings_contract_rejects_infinity_before_fragment_extraction() -> None:
    assert_non_finite_json_is_rejected("Infinity")


def test_findings_contract_rejects_negative_infinity_before_fragment_extraction() -> None:
    assert_non_finite_json_is_rejected("-Infinity")


def assert_isolated_surrogate_is_rejected(surrogate: str, *, extra_field: bool) -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    value = finding_payload(title=surrogate)
    if extra_field:
        value["extra"] = True

    evaluation = contract.evaluate(json.dumps(value, ensure_ascii=True), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_isolated_surrogates_before_fragment_extraction() -> None:
    assert_isolated_surrogate_is_rejected(chr(0xD800), extra_field=True)
    assert_isolated_surrogate_is_rejected(chr(0xDC00), extra_field=True)


def test_findings_contract_rejects_isolated_surrogate_on_complete_path() -> None:
    assert_isolated_surrogate_is_rejected(chr(0xD800), extra_field=False)


def test_findings_contract_rejects_escaped_surrogate_in_top_level_extra() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    value = {**finding_payload(), "extra": chr(0xD800)}

    evaluation = contract.evaluate(json.dumps(value, ensure_ascii=True), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_literal_surrogate_with_stable_payload_digest() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload())[:-1] + f', "extra": "{chr(0xD800)}"}}'

    evaluation = contract.evaluate(payload, prepared, context)

    assert (
        evaluation.payload_digest
        == hashlib.sha256(payload.encode(errors="surrogatepass")).hexdigest()
    )
    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_findings_contract_rejects_surrogate_in_reviewed_files() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",), review_declaration_required=True)
    prepared = contract.prepare(context)
    value = {**finding_payload(), "reviewedFiles": [chr(0xDC00)]}

    evaluation = contract.evaluate(json.dumps(value, ensure_ascii=True), prepared, context)

    assert evaluation.status is EvaluationStatus.INVALID
    assert evaluation.violations == ("invalid-json",)
    assert evaluation.valid_fragments == ()
    assert evaluation.coverage is None
    assert evaluation.completion_request is None


def test_valid_unicode_payload_digest_is_unchanged() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    payload = json.dumps(finding_payload(title="Résumé"), ensure_ascii=False)

    evaluation = contract.evaluate(payload, prepared, context)

    assert evaluation.status is EvaluationStatus.COMPLETE
    assert evaluation.payload_digest == hashlib.sha256(payload.encode()).hexdigest()


def test_finding_values_are_immutable_without_losing_dict_compatibility() -> None:
    contract = get_contract("findings-json")
    context = ContractContext(file_names=("source.py",))
    prepared = contract.prepare(context)
    evaluation = contract.evaluate(json.dumps(finding_payload()), prepared, context)
    assert evaluation.value is not None
    fragment_value = evaluation.valid_fragments[0].value
    evaluation_finding = evaluation.value["findings"][0]
    original = finding_payload()["findings"][0]

    assert fragment_value is evaluation_finding
    assert fragment_value == original
    assert json.loads(json.dumps(fragment_value)) == original

    with pytest.raises(TypeError):
        fragment_value["title"] = "mutated"
    with pytest.raises(TypeError):
        del evaluation_finding["title"]
    with pytest.raises(TypeError):
        fragment_value.update({"title": "mutated"})

    assert fragment_value == original
    assert evaluation_finding == original

    assert copy(fragment_value) is fragment_value
    assert deepcopy(fragment_value) is fragment_value
    copied_evaluation = deepcopy(evaluation)
    assert copied_evaluation == evaluation
    assert copied_evaluation.valid_fragments[0].value is fragment_value
    assert copied_evaluation.value is not None
    assert copied_evaluation.value["findings"][0] is fragment_value
