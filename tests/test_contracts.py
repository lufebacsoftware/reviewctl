import hashlib

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
