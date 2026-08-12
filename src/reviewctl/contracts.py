"""Native typed review contracts independent from transport orchestration."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
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
    schema: dict[str, Any]
    output_instructions: str
    digest: str

    @property
    def identity_material(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "schema": self.schema,
            "outputInstructions": self.output_instructions,
        }


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


class ReviewContract(Protocol):
    """Deep contract boundary consumed by transports and orchestration."""

    name: str
    version: str

    def prepare(self, context: ContractContext) -> PreparedContract: ...

    def evaluate(
        self, payload: str, prepared: PreparedContract, context: ContractContext
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
            "schema": schema,
            "outputInstructions": instructions,
        }
        return PreparedContract(
            name=self.name,
            version=self.version,
            schema=schema,
            output_instructions=instructions,
            digest=hashlib.sha256(canonical_json(identity)).hexdigest(),
        )

    def evaluate(
        self, payload: str, prepared: PreparedContract, context: ContractContext
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

        try:
            value = json.loads(payload, object_pairs_hook=exact_json_object)
        except (json.JSONDecodeError, DuplicateJsonField):
            return rejected("invalid-json")
        if not isinstance(value, dict):
            return rejected("top-level-not-object")

        expected_fields = {"verdict", "findings"}
        if context.review_declaration_required:
            expected_fields.add("reviewedFiles")
        if set(value) != expected_fields:
            return rejected("response-fields")

        verdict = value["verdict"]
        if not isinstance(verdict, str) or verdict not in REVIEW_VERDICTS:
            return rejected("verdict")
        findings = value["findings"]
        if not isinstance(findings, list):
            return rejected("findings-shape")

        normalized_findings: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
                return rejected("finding-fields")
            string_fields = FINDING_FIELDS - {"line"}
            if not all(
                isinstance(finding[field], str) and finding[field].strip()
                for field in string_fields
            ):
                return rejected("finding-value")
            if finding["severity"] not in FINDING_SEVERITIES:
                return rejected("finding-value")
            line = finding["line"]
            if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                return rejected("finding-value")
            if context.file_names and finding["path"] not in context.file_names:
                return rejected("finding-path")
            normalized_findings.append(dict(finding))

        if (verdict == "approved") != (not findings):
            return rejected("verdict-invariant")

        normalized: dict[str, Any] = {"verdict": verdict, "findings": normalized_findings}
        if context.review_declaration_required:
            reviewed_files = value["reviewedFiles"]
            if not isinstance(reviewed_files, list):
                return rejected("review-declaration")
            normalized_files: list[str] = []
            for reviewed in reviewed_files:
                if not isinstance(reviewed, str) or not reviewed.strip():
                    return rejected("review-declaration")
                normalized_files.append(Path(reviewed.strip()).name)
            if len(normalized_files) != len(set(normalized_files)) or set(
                normalized_files
            ) != set(context.file_names):
                return rejected("review-declaration")
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
        )


_CONTRACTS: dict[str, ReviewContract] = {"findings-json": FindingsJsonContract()}


def get_contract(name: str) -> ReviewContract:
    """Return a native contract by its stable name."""
    return _CONTRACTS[name]
