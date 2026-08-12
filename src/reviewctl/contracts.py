"""Native typed review contracts independent from transport orchestration."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
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


class ReviewContract(Protocol):
    """Deep contract boundary consumed by transports and orchestration."""

    name: str
    version: str

    def prepare(self, context: ContractContext) -> PreparedContract: ...


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


_CONTRACTS: dict[str, ReviewContract] = {"findings-json": FindingsJsonContract()}


def get_contract(name: str) -> ReviewContract:
    """Return a native contract by its stable name."""
    return _CONTRACTS[name]
