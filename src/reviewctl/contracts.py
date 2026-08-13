"""Native typed review contracts independent from transport orchestration."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

FINDING_FIELDS = {"severity", "path", "line", "title", "evidence", "reproduction"}
FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
REVIEW_VERDICTS = {"approved", "changes-requested"}

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "findings"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": sorted(REVIEW_VERDICTS)},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": sorted(FINDING_FIELDS),
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": sorted(FINDING_SEVERITIES)},
                    "path": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "reproduction": {"type": "string"},
                },
            },
        },
    },
}

REVIEWED_FILES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {"type": "string", "minLength": 1},
}


def canonical_json(value: object) -> bytes:
    """Serialize contract identity and normalized values deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class ContractContext:
    """Bounded facts that affect preparation and semantic validation."""

    file_names: tuple[str, ...] = ()
    review_declaration_required: bool = False


@dataclass(frozen=True)
class PreparedContract:
    """Provider-neutral contract compiled for one review context."""

    name: str
    version: str
    file_names: tuple[str, ...]
    review_declaration_required: bool
    schema: dict[str, Any]
    output_instructions: str
    digest: str

    @property
    def identity_material(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "context": {
                "fileNames": list(self.file_names),
                "reviewDeclarationRequired": self.review_declaration_required,
            },
            "schema": self.schema,
            "outputInstructions": self.output_instructions,
        }


class EvaluationStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class FragmentKind(StrEnum):
    FINDING = "finding"


@dataclass(frozen=True)
class EvaluationContext:
    packet_digest: str | None = None


@dataclass(frozen=True)
class ContractFragment:
    fragment_id: str
    fingerprint: str
    kind: FragmentKind
    value: dict[str, Any]
    payload_digest: str
    scope: tuple[str, ...]


@dataclass(frozen=True)
class ContractCoverage:
    required_fields: tuple[str, ...]
    covered_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class ContractCompletionRequest:
    prepared_digest: str
    packet_digest: str | None
    missing_fields: tuple[str, ...]
    invalid_fragment_indexes: tuple[int, ...]
    violations: tuple[str, ...]


@dataclass(frozen=True)
class ContractEvaluation:
    """Exact decode and semantic-validation result for one raw payload."""

    name: str
    version: str
    prepared_digest: str
    payload_digest: str
    normalized_digest: str | None
    value: dict[str, Any] | None
    violations: tuple[str, ...]
    status: EvaluationStatus = EvaluationStatus.INVALID
    valid_fragments: tuple[ContractFragment, ...] = ()
    coverage: ContractCoverage | None = None
    completion_request: ContractCompletionRequest | None = None


class ReviewContract(Protocol):
    """Deep contract boundary consumed by transports and orchestration."""

    name: str
    version: str

    def prepare(self, context: ContractContext) -> PreparedContract: ...

    def evaluate(
        self,
        payload: str,
        prepared: PreparedContract,
        context: ContractContext,
        *,
        evidence: EvaluationContext | None = None,
    ) -> ContractEvaluation: ...


class DuplicateJsonField(ValueError):
    """Raised when exact JSON decoding encounters the same object key twice."""


def exact_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonField(key)
        value[key] = item
    return value


def _validate_finding(
    finding: object, context: ContractContext
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
        return None, "finding-fields"
    string_fields = FINDING_FIELDS - {"line"}
    if not all(
        isinstance(finding[field], str) and finding[field].strip()
        for field in string_fields
    ):
        return None, "finding-value"
    if finding["severity"] not in FINDING_SEVERITIES:
        return None, "finding-value"
    line = finding["line"]
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None, "finding-value"
    if context.file_names and finding["path"] not in context.file_names:
        return None, "finding-path"
    return dict(finding), None


class FindingsJsonContract:
    name = "findings-json"
    version = "1"

    def prepare(self, context: ContractContext) -> PreparedContract:
        schema = deepcopy(FINDINGS_SCHEMA)
        if context.review_declaration_required:
            schema["required"].append("reviewedFiles")
            schema["properties"]["reviewedFiles"] = deepcopy(REVIEWED_FILES_SCHEMA)
        instructions = (
            "Return only JSON matching the supplied schema. The top-level object has exactly "
            "`verdict` and `findings`. Each finding has exactly six fields: `severity`, `path`, "
            "`line`, `title`, `evidence`, and `reproduction`. Use `changes-requested` if and only "
            "if `findings` is non-empty; use `approved` if and only if `findings` is empty."
        )
        if context.review_declaration_required:
            instructions += (
                " Also list every frozen snapshot actually reviewed in `reviewedFiles`; do not "
                "emit a verdict if a supplied file could not be read. The runner records the "
                "authoritative source hashes."
            )
        identity = {
            "name": self.name,
            "version": self.version,
            "context": {
                "fileNames": list(context.file_names),
                "reviewDeclarationRequired": context.review_declaration_required,
            },
            "schema": schema,
            "outputInstructions": instructions,
        }
        return PreparedContract(
            name=self.name,
            version=self.version,
            file_names=context.file_names,
            review_declaration_required=context.review_declaration_required,
            schema=schema,
            output_instructions=instructions,
            digest=hashlib.sha256(canonical_json(identity)).hexdigest(),
        )

    def evaluate(
        self,
        payload: str,
        prepared: PreparedContract,
        context: ContractContext,
        *,
        evidence: EvaluationContext | None = None,
    ) -> ContractEvaluation:
        payload_digest = hashlib.sha256(payload.encode()).hexdigest()

        def rejected(code: str) -> ContractEvaluation:
            return ContractEvaluation(
                name=self.name,
                version=self.version,
                prepared_digest=prepared.digest,
                payload_digest=payload_digest,
                normalized_digest=None,
                value=None,
                violations=(code,),
            )

        if prepared != self.prepare(context) or prepared.digest != hashlib.sha256(
            canonical_json(prepared.identity_material)
        ).hexdigest():
            return rejected("prepared-contract")

        try:
            value = json.loads(payload, object_pairs_hook=exact_json_object)
        except (json.JSONDecodeError, DuplicateJsonField):
            return rejected("invalid-json")
        if not isinstance(value, dict):
            return rejected("top-level-not-object")

        required_fields = ("verdict", "findings")
        if context.review_declaration_required:
            required_fields += ("reviewedFiles",)
        expected_fields = set(required_fields)
        violation = "response-fields" if set(value) != expected_fields else None

        verdict = value.get("verdict")
        verdict_valid = isinstance(verdict, str) and verdict in REVIEW_VERDICTS
        if violation is None and not verdict_valid:
            violation = "verdict"

        findings = value.get("findings")
        findings_are_list = isinstance(findings, list)
        if violation is None and not findings_are_list:
            violation = "findings-shape"

        normalized_findings: list[dict[str, Any]] = []
        invalid_fragment_indexes: list[int] = []
        first_finding_violation: str | None = None
        if findings_are_list:
            for index, finding in enumerate(findings):
                normalized_finding, finding_violation = _validate_finding(finding, context)
                if finding_violation is not None:
                    invalid_fragment_indexes.append(index)
                    if first_finding_violation is None:
                        first_finding_violation = finding_violation
                    continue
                assert normalized_finding is not None
                normalized_findings.append(normalized_finding)
        if violation is None and first_finding_violation is not None:
            violation = first_finding_violation

        verdict_invariant = (
            verdict_valid
            and findings_are_list
            and (verdict == "approved") == (not findings)
        )
        if violation is None and not verdict_invariant:
            violation = "verdict-invariant"

        normalized_files: list[str] | None = None
        if context.review_declaration_required:
            reviewed_files = value.get("reviewedFiles")
            candidate_files: list[str] = []
            review_declaration_valid = isinstance(reviewed_files, list)
            if review_declaration_valid:
                for reviewed in reviewed_files:
                    if not isinstance(reviewed, str) or not reviewed.strip():
                        review_declaration_valid = False
                        break
                    declared = reviewed.strip()
                    if declared in context.file_names:
                        candidate_files.append(declared)
                        continue
                    declared_path = Path(declared)
                    if (
                        not declared_path.is_absolute()
                        or not declared_path.parent.name.startswith("reviewctl-input-")
                        or declared_path.name not in context.file_names
                    ):
                        review_declaration_valid = False
                        break
                    candidate_files.append(declared_path.name)
            if review_declaration_valid and (
                len(candidate_files) != len(set(candidate_files))
                or set(candidate_files) != set(context.file_names)
            ):
                review_declaration_valid = False
            if review_declaration_valid:
                normalized_files = candidate_files
            elif violation is None:
                violation = "review-declaration"

        findings_valid = findings_are_list and not invalid_fragment_indexes
        covered_fields: list[str] = []
        missing_fields: list[str] = []
        if verdict_invariant:
            covered_fields.append("verdict")
        else:
            missing_fields.append("verdict")
        if findings_valid or normalized_findings:
            covered_fields.append("findings")
        if not findings_valid:
            missing_fields.append("findings")
        if context.review_declaration_required:
            if normalized_files is not None:
                covered_fields.append("reviewedFiles")
            else:
                missing_fields.append("reviewedFiles")
        coverage = ContractCoverage(
            required_fields=required_fields,
            covered_fields=tuple(covered_fields),
            missing_fields=tuple(missing_fields),
        )

        fragments: list[ContractFragment] = []
        for finding in normalized_findings:
            scope = (finding["path"],)
            fingerprint = hashlib.sha256(
                canonical_json(
                    {
                        "contract": self.name,
                        "version": self.version,
                        "kind": FragmentKind.FINDING.value,
                        "value": finding,
                        "scope": scope,
                    }
                )
            ).hexdigest()
            fragment_id = hashlib.sha256(
                canonical_json(
                    {"fingerprint": fingerprint, "payloadDigest": payload_digest}
                )
            ).hexdigest()
            fragments.append(
                ContractFragment(
                    fragment_id=fragment_id,
                    fingerprint=fingerprint,
                    kind=FragmentKind.FINDING,
                    value=finding,
                    payload_digest=payload_digest,
                    scope=scope,
                )
            )

        if violation is not None:
            violations = (violation,)
            if not fragments:
                return ContractEvaluation(
                    name=self.name,
                    version=self.version,
                    prepared_digest=prepared.digest,
                    payload_digest=payload_digest,
                    normalized_digest=None,
                    value=None,
                    violations=violations,
                    coverage=coverage,
                )
            completion_request = ContractCompletionRequest(
                prepared_digest=prepared.digest,
                packet_digest=evidence.packet_digest if evidence is not None else None,
                missing_fields=coverage.missing_fields,
                invalid_fragment_indexes=tuple(invalid_fragment_indexes),
                violations=violations,
            )
            return ContractEvaluation(
                name=self.name,
                version=self.version,
                prepared_digest=prepared.digest,
                payload_digest=payload_digest,
                normalized_digest=None,
                value=None,
                violations=violations,
                status=EvaluationStatus.INCOMPLETE,
                valid_fragments=tuple(fragments),
                coverage=coverage,
                completion_request=completion_request,
            )

        assert verdict_valid
        normalized: dict[str, Any] = {"verdict": verdict, "findings": normalized_findings}
        if normalized_files is not None:
            normalized["reviewedFiles"] = normalized_files

        normalized_digest = hashlib.sha256(canonical_json(normalized)).hexdigest()
        return ContractEvaluation(
            name=self.name,
            version=self.version,
            prepared_digest=prepared.digest,
            payload_digest=payload_digest,
            normalized_digest=normalized_digest,
            value=normalized,
            violations=(),
            status=EvaluationStatus.COMPLETE,
            valid_fragments=tuple(fragments),
            coverage=coverage,
        )


_CONTRACTS: dict[str, ReviewContract] = {"findings-json": FindingsJsonContract()}


def get_contract(name: str) -> ReviewContract:
    """Return a native contract by its stable name."""
    return _CONTRACTS[name]
