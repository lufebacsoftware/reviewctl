import hashlib
import json

from reviewctl.contracts import ContractContext, canonical_json, get_contract


def test_findings_contract_prepares_a_stable_portable_contract() -> None:
    prepared = get_contract("findings-json").prepare(ContractContext())

    assert prepared.name == "findings-json"
    assert prepared.version == "1"
    assert prepared.schema["required"] == ["verdict", "findings"]
    assert "reviewedFiles" not in prepared.schema["properties"]
    assert prepared.digest == hashlib.sha256(
        canonical_json(prepared.identity_material)
    ).hexdigest()
    assert "changes-requested" in prepared.output_instructions
    assert "approved" in prepared.output_instructions


def test_findings_contract_can_require_a_review_declaration_without_mutating_portable_schema(
) -> None:
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
    assert evaluation.normalized_digest == hashlib.sha256(
        canonical_json(finding_payload())
    ).hexdigest()


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
            json.dumps(
                {"verdict": "approved", "findings": [], "reviewedFiles": ["../source.py"]}
            ),
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
