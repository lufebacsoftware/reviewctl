"""Command line interface for independent, bounded review receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from reviewctl import __version__

MAX_FILES = 3
MAX_FRAGMENT_BYTES = 24 * 1024
REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*$")
FINDING_FIELDS = {"severity", "path", "line", "title", "evidence", "reproduction"}
FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
FINDING_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
REVIEW_VERDICTS = {"approved", "changes-requested"}
PRODUCT_CONSTRAINT_DISPOSITIONS = {"satisfied", "rejected", "assumed"}
PRODUCT_SCORE_FIELDS = {
    "delivery",
    "domainIntegrity",
    "operationalCorrectness",
    "problemFidelity",
    "scopeDiscipline",
}
RESPONSE_CONTRACTS = {"verdict", "findings-json", "product-review-json", "product-judge-json"}
TOURNAMENT_TRANSPORTS = {"llm", "codex", "openrouter", "agy"}
TOURNAMENT_COST_MODES = {"metered", "account-included", "subscription"}
RETRIABLE_REVIEW_RESULTS = {
    "timeout",
    "transport-failed",
    "missing-response",
    "empty",
    "missing-conversation",
    "incomplete",
}
PROVIDER_PREFERENCE_KEYS = {
    "allow_fallbacks",
    "data_collection",
    "require_parameters",
    "only",
    "order",
    "sort",
}
FINDINGS_SCHEMA = {
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
CODEX_FINDINGS_SCHEMA = {
    **FINDINGS_SCHEMA,
    "required": ["verdict", "findings", "reviewedFiles"],
    "properties": {
        **FINDINGS_SCHEMA["properties"],
        "reviewedFiles": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

_STRING_SCHEMA = {"type": "string", "minLength": 1}
_STRING_LIST_SCHEMA = {"type": "array", "items": _STRING_SCHEMA}
_NON_EMPTY_STRING_LIST_SCHEMA = {**_STRING_LIST_SCHEMA, "minItems": 1}
_PRODUCT_OBJECT_SCHEMAS = {
    "interactionFlow": {
        "type": "object",
        "required": ["actor", "action", "outcome"],
        "additionalProperties": False,
        "properties": {
            "actor": _STRING_SCHEMA,
            "action": _STRING_SCHEMA,
            "outcome": _STRING_SCHEMA,
        },
    },
    "domainEntities": {
        "type": "object",
        "required": ["name", "purpose"],
        "additionalProperties": False,
        "properties": {"name": _STRING_SCHEMA, "purpose": _STRING_SCHEMA},
    },
    "stateTransitions": {
        "type": "object",
        "required": ["from", "to", "guard"],
        "additionalProperties": False,
        "properties": {"from": _STRING_SCHEMA, "to": _STRING_SCHEMA, "guard": _STRING_SCHEMA},
    },
    "architecture": {
        "type": "object",
        "required": ["boundary", "owns", "commands", "events", "readModels"],
        "additionalProperties": False,
        "properties": {
            "boundary": _STRING_SCHEMA,
            "owns": _STRING_SCHEMA,
            "commands": _STRING_LIST_SCHEMA,
            "events": _STRING_LIST_SCHEMA,
            "readModels": _STRING_LIST_SCHEMA,
        },
    },
    "operationalControls": {
        "type": "object",
        "required": ["control", "approach"],
        "additionalProperties": False,
        "properties": {"control": _STRING_SCHEMA, "approach": _STRING_SCHEMA},
    },
    "constraintChecks": {
        "type": "object",
        "required": ["constraintId", "disposition", "rationale"],
        "additionalProperties": False,
        "properties": {
            "constraintId": _STRING_SCHEMA,
            "disposition": {"type": "string", "enum": sorted(PRODUCT_CONSTRAINT_DISPOSITIONS)},
            "rationale": _STRING_SCHEMA,
        },
    },
}
PRODUCT_REVIEW_SCHEMA = {
    "type": "object",
    "required": [
        "summary",
        "userJobs",
        "mvp",
        "nonGoals",
        "interactionFlow",
        "domainEntities",
        "stateTransitions",
        "architecture",
        "operationalControls",
        "constraintChecks",
        "risks",
        "acceptanceTests",
        "openQuestions",
    ],
    "additionalProperties": False,
    "properties": {
        "summary": _STRING_SCHEMA,
        "userJobs": _NON_EMPTY_STRING_LIST_SCHEMA,
        "mvp": _NON_EMPTY_STRING_LIST_SCHEMA,
        "nonGoals": _NON_EMPTY_STRING_LIST_SCHEMA,
        "interactionFlow": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["interactionFlow"],
        },
        "domainEntities": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["domainEntities"],
        },
        "stateTransitions": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["stateTransitions"],
        },
        "architecture": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["architecture"],
        },
        "operationalControls": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["operationalControls"],
        },
        "constraintChecks": {
            "type": "array",
            "minItems": 1,
            "items": _PRODUCT_OBJECT_SCHEMAS["constraintChecks"],
        },
        "risks": _NON_EMPTY_STRING_LIST_SCHEMA,
        "acceptanceTests": _NON_EMPTY_STRING_LIST_SCHEMA,
        "openQuestions": {"type": "array", "items": {"type": "string"}},
    },
}
PRODUCT_JUDGE_SCHEMA = {
    "type": "object",
    "required": ["scores", "hardConstraintViolations", "rationale"],
    "additionalProperties": False,
    "properties": {
        "scores": {
            "type": "object",
            "required": sorted(PRODUCT_SCORE_FIELDS),
            "additionalProperties": False,
            "properties": {
                field: {"type": "integer", "minimum": 0, "maximum": 4}
                for field in sorted(PRODUCT_SCORE_FIELDS)
            },
        },
        "hardConstraintViolations": _STRING_LIST_SCHEMA,
        "rationale": _STRING_SCHEMA,
    },
}


def codex_schema(schema: dict[str, object]) -> dict[str, object]:
    """Add frozen-file read proof to a portable structured response schema."""
    properties = dict(schema["properties"])
    properties["reviewedFiles"] = CODEX_FINDINGS_SCHEMA["properties"]["reviewedFiles"]
    return {
        **schema,
        "required": [*schema["required"], "reviewedFiles"],
        "properties": properties,
    }


def response_schema(contract: str, *, codex: bool = False) -> dict[str, object] | None:
    """Return the strict JSON schema for one supported response contract."""
    schema = {
        "findings-json": FINDINGS_SCHEMA,
        "product-review-json": PRODUCT_REVIEW_SCHEMA,
        "product-judge-json": PRODUCT_JUDGE_SCHEMA,
    }.get(contract)
    if schema is None:
        return None
    return codex_schema(schema) if codex else schema


@dataclass(frozen=True)
class PersistedResponse:
    """The response persisted by one isolated `llm` invocation."""

    conversation_id: str
    cost_usd: float | None
    duration_ms: int | None
    input_tokens: int | None
    model: str
    output_tokens: int | None
    provider: str | None
    response: str


@dataclass(frozen=True)
class TournamentCandidate:
    """One independently auditable tournament participant."""

    council_eligible: bool
    family: str
    identifier: str
    model: str
    cost_mode: str
    pricing: tuple[float, float] | None
    provider_preferences: dict[str, object] | None
    transport: str
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class CodexIsolation:
    """Ephemeral Codex home and macOS profile for a proprietary review."""

    environment: dict[str, str]
    home: Path
    profile: Path


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fail(parser: argparse.ArgumentParser, message: str) -> None:
    parser.error(message)


def validate_request(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[str, list[Path]]:
    if not REVIEW_ID.fullmatch(args.review_id):
        fail(parser, "invalid review id")
    if not args.models:
        fail(parser, "at least one --model is required")
    if len(args.files) == 0:
        fail(parser, "at least one --file is required")
    if len(args.files) > MAX_FILES:
        fail(parser, f"at most {MAX_FILES} review files are allowed")
    if args.prompt and args.prompt_file:
        fail(parser, "use either --prompt or --prompt-file")
    if not args.prompt and not args.prompt_file:
        fail(parser, "one of --prompt or --prompt-file is required")

    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt.strip():
        fail(parser, "review prompt must not be empty")

    files = [Path(value).resolve() for value in args.files]
    for file in files:
        if not file.is_file():
            fail(parser, f"review file does not exist: {file}")
        if file.stat().st_size > MAX_FRAGMENT_BYTES:
            fail(parser, f"review file exceeds {MAX_FRAGMENT_BYTES} bytes: {file}")
    if len({file.name for file in files}) != len(files):
        fail(parser, "review files must have unique basenames")
    return prompt, files


def review_root(artifact_root: Path, review_id: str) -> Path:
    turn_name = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.{secrets.token_hex(3)}"
    directory = artifact_root.expanduser().resolve() / review_id / turn_name
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def git_metadata(cwd: Path) -> dict[str, str | None]:
    def value(*command: str) -> str | None:
        result = subprocess.run(
            ["git", *command], cwd=cwd, text=True, capture_output=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return {
        "head": value("rev-parse", "HEAD"),
        "remote": value("config", "--get", "remote.origin.url"),
        "repositoryRoot": value("rev-parse", "--show-toplevel"),
    }


def load_policy(path: str) -> dict[str, Any]:
    import tomllib

    return tomllib.loads(Path(path).read_text())


def policy_sha256(path: str) -> str:
    """Return the digest of the policy bytes applied to a review."""
    return sha256_bytes(Path(path).read_bytes())


def normalize_provider_preferences(value: object) -> dict[str, object] | None:
    """Validate OpenRouter provider routing preferences for reproducible reviews."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - PROVIDER_PREFERENCE_KEYS:
        raise ValueError("provider preferences contain unsupported fields")
    normalized: dict[str, object] = {}
    for key in ("only", "order"):
        item = value.get(key)
        if item is None:
            continue
        if (
            not isinstance(item, list)
            or not item
            or not all(isinstance(provider, str) and provider for provider in item)
        ):
            raise ValueError(f"provider.{key} must be a non-empty string list")
        normalized[key] = item
    allow_fallbacks = value.get("allow_fallbacks")
    if allow_fallbacks is not None:
        if not isinstance(allow_fallbacks, bool):
            raise ValueError("provider.allow_fallbacks must be boolean")
        normalized["allow_fallbacks"] = allow_fallbacks
    require_parameters = value.get("require_parameters")
    if require_parameters is not None:
        if not isinstance(require_parameters, bool):
            raise ValueError("provider.require_parameters must be boolean")
        normalized["require_parameters"] = require_parameters
    data_collection = value.get("data_collection")
    if data_collection is not None:
        if data_collection not in {"allow", "deny"}:
            raise ValueError("provider.data_collection must be allow or deny")
        normalized["data_collection"] = data_collection
    sort = value.get("sort")
    if sort is not None:
        if sort not in {"price", "throughput", "latency"}:
            raise ValueError("provider.sort must be price, throughput, or latency")
        normalized["sort"] = sort
    return normalized or None


def provider_preferences_from_args(args: argparse.Namespace) -> dict[str, object] | None:
    """Construct explicit provider policy from `run` command options."""
    return normalize_provider_preferences(
        {
            "allow_fallbacks": args.provider_allow_fallbacks,
            "data_collection": args.provider_data_collection,
            "require_parameters": args.provider_require_parameters,
            "only": args.provider_only or None,
            "order": args.provider_order or None,
            "sort": args.provider_sort,
        }
    )


def provider_slug(value: str) -> str:
    """Normalize an OpenRouter provider display name for pinned-route comparison."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def resolved_provider_matches(
    provider_preferences: dict[str, object] | None, provider: str | None
) -> bool:
    """Require a response to come from an explicitly pinned OpenRouter provider."""
    if provider_preferences is None:
        return True
    requested = provider_preferences.get("only")
    if not isinstance(requested, list):
        return True
    return isinstance(provider, str) and provider_slug(provider) in {
        provider_slug(item) for item in requested if isinstance(item, str)
    }


def openrouter_model_id(model: str) -> str:
    """Remove the local transport prefix before addressing an OpenRouter endpoint."""
    return model.removeprefix("openrouter/")


def endpoint_price_per_million(endpoint: dict[str, object], field: str) -> float | None:
    """Read one OpenRouter endpoint price, expressed in USD per million tokens."""
    pricing = endpoint.get("pricing")
    value = pricing.get(field) if isinstance(pricing, dict) else None
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def assess_pinned_provider_endpoint(
    candidate: TournamentCandidate, endpoints: list[dict[str, object]]
) -> dict[str, object]:
    """Assess whether a pinned tournament route can honor its JSON contract today."""
    preferences = candidate.provider_preferences or {}
    requested = preferences.get("only")
    if not isinstance(requested, list) or len(requested) != 1 or not isinstance(requested[0], str):
        raise ValueError("provider preflight requires exactly one pinned provider per candidate")
    provider = requested[0]
    endpoint = next(
        (
            item
            for item in endpoints
            if isinstance(item.get("provider_name"), str)
            and provider_slug(str(item["provider_name"])) == provider_slug(provider)
        ),
        None,
    )
    reasons: list[str] = []
    if endpoint is None:
        reasons.append("provider-not-found")
    else:
        if endpoint.get("status") != 0:
            reasons.append("inactive")
        parameters = endpoint.get("supported_parameters")
        if not isinstance(parameters, list) or "response_format" not in parameters:
            reasons.append("missing-response-format")
        if not isinstance(parameters, list) or "structured_outputs" not in parameters:
            reasons.append("missing-structured-outputs")
        expected = candidate.pricing
        actual = (
            (
                endpoint_price_per_million(endpoint, "prompt"),
                endpoint_price_per_million(endpoint, "completion"),
            )
            if expected is not None
            else None
        )
        if (
            actual is None
            or expected is None
            or any(
                actual_value is None or abs(actual_value - expected_value) > 0.000001
                for actual_value, expected_value in zip(actual, expected, strict=True)
            )
        ):
            reasons.append("price-mismatch")
    return {
        "candidate": candidate.identifier,
        "model": openrouter_model_id(candidate.model),
        "provider": provider,
        "result": "accepted" if not reasons else "rejected",
        "reasons": reasons,
    }


def fetch_openrouter_model_endpoints(
    *, api_key: str | None, model: str, timeout_seconds: int
) -> tuple[int, str, dict[str, object] | None]:
    """Fetch one live OpenRouter endpoint inventory without exposing the API key."""
    if not api_key:
        return 127, "OPENROUTER_API_KEY is not configured", None
    request = urlrequest.Request(
        f"https://openrouter.ai/api/v1/models/{urlparse.quote(model, safe='/')}/endpoints",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except urlerror.HTTPError as error:
        return error.code, error.read().decode(errors="replace"), None
    except urlerror.URLError as error:
        return 502, str(error.reason), None
    except TimeoutError:
        return 124, "provider preflight timed out", None
    except json.JSONDecodeError:
        return 502, "OpenRouter returned invalid endpoint JSON", None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 502, "OpenRouter returned an invalid endpoint response", None
    return 0, "", data


def write_provider_preflight(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Snapshot and validate explicit OpenRouter provider routes before a tournament."""
    plan_path = Path(args.plan).resolve()
    if not plan_path.is_file():
        parser.error(f"tournament plan does not exist: {plan_path}")
    try:
        plan = load_policy(str(plan_path))
        candidates = parse_tournament_candidates(plan)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    selected = [
        candidate
        for candidate in candidates
        if candidate.transport == "openrouter" and candidate.provider_preferences is not None
    ]
    if not selected:
        parser.error("provider preflight requires pinned OpenRouter tournament candidates")
    root = tournament_report_path(plan, plan_path).parent
    endpoint_data: dict[str, dict[str, object]] = {}
    fetch_errors: dict[str, str] = {}
    for candidate in selected:
        model = openrouter_model_id(candidate.model)
        if model in endpoint_data or model in fetch_errors:
            continue
        code, error, payload = fetch_openrouter_model_endpoints(
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            model=model,
            timeout_seconds=args.timeout_seconds,
        )
        if code == 0 and payload is not None:
            endpoint_data[model] = payload
        else:
            fetch_errors[model] = error
    assessments: list[dict[str, object]] = []
    for candidate in selected:
        model = openrouter_model_id(candidate.model)
        if model in fetch_errors:
            preferences = candidate.provider_preferences or {}
            requested = preferences.get("only")
            provider = requested[0] if isinstance(requested, list) and len(requested) == 1 else None
            assessments.append(
                {
                    "candidate": candidate.identifier,
                    "model": model,
                    "provider": provider,
                    "result": "rejected",
                    "reasons": ["endpoint-fetch-failed"],
                }
            )
            continue
        endpoints = endpoint_data[model].get("endpoints")
        if not isinstance(endpoints, list) or not all(isinstance(item, dict) for item in endpoints):
            parser.error(f"OpenRouter returned invalid endpoints for {model}")
        assessments.append(assess_pinned_provider_endpoint(candidate, endpoints))
    result = "accepted" if all(item["result"] == "accepted" for item in assessments) else "rejected"
    snapshot = {
        "assessments": assessments,
        "endpoints": endpoint_data,
        "errors": fetch_errors,
        "generatedAt": utc_now(),
        "planSha256": sha256_bytes(plan_path.read_bytes()),
        "result": result,
    }
    output = root / "provider-preflight.json"
    output.write_bytes(canonical_json(snapshot) + b"\n")
    print(output)
    return 0 if result == "accepted" else 4


def parse_candidate_pricing(value: object) -> tuple[float, float] | None:
    """Validate one metered candidate's declared OpenRouter list pricing."""
    if not isinstance(value, dict) or set(value) != {
        "input_per_million_usd",
        "output_per_million_usd",
    }:
        return None
    input_price = numeric_value(value["input_per_million_usd"])
    output_price = numeric_value(value["output_per_million_usd"])
    if input_price is None or output_price is None or input_price < 0 or output_price < 0:
        return None
    return input_price, output_price


def parse_tournament_candidates(plan: dict[str, Any]) -> list[TournamentCandidate]:
    """Parse the mixed-transport participant list used by product tournaments."""
    raw_blended_price_cap = plan.get("max_blended_price_usd")
    blended_price_cap = (
        numeric_value(raw_blended_price_cap) if raw_blended_price_cap is not None else None
    )
    if raw_blended_price_cap is not None and (blended_price_cap is None or blended_price_cap <= 0):
        raise ValueError("max_blended_price_usd must be a positive number")
    raw_candidates = plan.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidate tournament requires a non-empty candidates list")
    candidates: list[TournamentCandidate] = []
    identifiers: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            raise ValueError("tournament candidate must be an object")
        identifier = raw.get("id")
        family = raw.get("family")
        model = raw.get("model")
        transport = raw.get("transport")
        cost_mode = raw.get("cost_mode")
        council_eligible = raw.get("council_eligible", True)
        if not all(isinstance(item, str) and item.strip() for item in (identifier, family, model)):
            raise ValueError("tournament candidate requires non-empty id, family, and model")
        if not isinstance(council_eligible, bool):
            raise ValueError("tournament candidate council_eligible must be boolean")
        if not REVIEW_ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("tournament candidate ids must be unique valid review ids")
        if transport not in TOURNAMENT_TRANSPORTS or cost_mode not in TOURNAMENT_COST_MODES:
            raise ValueError("tournament candidate has unsupported transport or cost mode")
        try:
            provider_preferences = normalize_provider_preferences(raw.get("provider"))
        except ValueError as error:
            raise ValueError(f"candidate {identifier}: {error}") from error
        if provider_preferences and transport != "openrouter":
            raise ValueError("candidate provider preferences require openrouter transport")
        pricing = parse_candidate_pricing(raw.get("pricing"))
        raw_max_output_tokens = raw.get("max_output_tokens")
        if raw_max_output_tokens is not None and (
            not isinstance(raw_max_output_tokens, int)
            or isinstance(raw_max_output_tokens, bool)
            or raw_max_output_tokens <= 0
        ):
            raise ValueError("tournament candidate max_output_tokens must be a positive integer")
        if cost_mode == "metered":
            if transport != "openrouter" or pricing is None:
                raise ValueError("metered candidates require openrouter transport and pricing")
            if (
                blended_price_cap is not None
                and (2 * pricing[0] + pricing[1]) / 3 > blended_price_cap
            ):
                raise ValueError("metered candidate exceeds max blended price")
        elif pricing is not None:
            raise ValueError(
                "account-included and subscription candidates must not declare pricing"
            )
        identifiers.add(identifier)
        candidates.append(
            TournamentCandidate(
                council_eligible=council_eligible,
                family=family,
                identifier=identifier,
                model=model,
                cost_mode=cost_mode,
                pricing=pricing,
                provider_preferences=provider_preferences,
                transport=transport,
                max_output_tokens=raw_max_output_tokens,
            )
        )
    return candidates


def select_council_judges(
    candidates: list[TournamentCandidate], candidate: TournamentCandidate
) -> tuple[TournamentCandidate, TournamentCandidate]:
    """Choose a distinct Codex judge and a second non-self family judge."""
    eligible = [
        item
        for item in candidates
        if item.identifier != candidate.identifier and item.family != candidate.family
    ]
    codex = next((item for item in eligible if item.transport == "codex"), None)
    external = next((item for item in eligible if item.transport != "codex"), None)
    if codex is None or external is None:
        raise ValueError(
            "council requires one non-self Codex judge and one non-self external judge"
        )
    return codex, external


def build_product_council_plan(
    candidates: list[TournamentCandidate], package: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    """Assign independent judges without adding candidate identity to the public plan."""
    if package.get("format") != "reviewctl.product-blind.v1":
        raise ValueError("council requires a product blind package")
    entries = package.get("entries")
    if not isinstance(entries, list):
        raise ValueError("blind package requires an entries list")
    candidates_by_id = {candidate.identifier: candidate for candidate in candidates}
    assignments: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("blind package entries must be objects")
        blind_id = entry.get("blindId")
        case = entry.get("case")
        if not isinstance(blind_id, str) or not blind_id or not isinstance(case, str) or not case:
            raise ValueError("blind package entries require blindId and case")
        identity = mapping.get(blind_id)
        if not isinstance(identity, dict):
            raise ValueError("blind package mapping is missing an identity")
        candidate_id = identity.get("candidate")
        if identity.get("case") != case or not isinstance(candidate_id, str):
            raise ValueError("blind package mapping does not match its public entry")
        candidate = candidates_by_id.get(candidate_id)
        if candidate is None:
            raise ValueError("blind package candidate is not declared in the tournament plan")
        codex, external = select_council_judges(candidates, candidate)
        assignments.append(
            {
                "blindId": blind_id,
                "case": case,
                "judges": [
                    {"id": codex.identifier, "model": codex.model, "transport": codex.transport},
                    {
                        "id": external.identifier,
                        "model": external.model,
                        "transport": external.transport,
                    },
                ],
            }
        )
    return {
        "entries": assignments,
        "format": "reviewctl.product-council-plan.v1",
        "responseContract": "product-judge-json",
    }


def build_blind_product_package(
    report: dict[str, Any], *, salt: bytes
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Strip candidate identity from accepted product responses before council review."""
    if not salt:
        raise ValueError("blind package requires a non-empty salt")
    raw_runs = report.get("runs")
    if not isinstance(raw_runs, list):
        raise ValueError("tournament report requires a runs list")
    entries: list[dict[str, Any]] = []
    mapping: dict[str, dict[str, str]] = {}
    for run in raw_runs:
        if (
            not isinstance(run, dict)
            or run.get("result") != "accepted"
            or run.get("councilEligible") is False
        ):
            continue
        candidate = run.get("candidate")
        case = run.get("case")
        receipt_path = run.get("receipt")
        if not all(isinstance(item, str) and item for item in (candidate, case, receipt_path)):
            raise ValueError("accepted product run requires candidate, case, and receipt")
        receipt = json.loads(Path(receipt_path).read_text())
        if receipt.get("result") != "accepted" or not valid_receipt(receipt):
            raise ValueError("accepted product run requires a verified accepted receipt")
        response = receipt.get("review")
        if not isinstance(response, dict):
            raise ValueError("accepted product receipt requires a structured review")
        blind_id = sha256_bytes(salt + f"{case}\0{candidate}".encode())[:16]
        if blind_id in mapping:
            raise ValueError("blind package collision")
        response_digest = receipt.get("response", {}).get("sha256")
        entries.append(
            {
                "blindId": blind_id,
                "case": case,
                "response": response,
                "responseSha256": response_digest if isinstance(response_digest, str) else None,
            }
        )
        mapping[blind_id] = {"candidate": candidate, "case": case, "receipt": receipt_path}
    return {"format": "reviewctl.product-blind.v1", "entries": entries}, mapping


def write_blind_product_package(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Create public anonymous responses and a separately stored private identity map."""
    report_path = Path(args.report).resolve()
    output_path = Path(args.output).resolve()
    mapping_path = Path(args.mapping_output).resolve()
    if not report_path.is_file():
        parser.error(f"tournament report does not exist: {report_path}")
    if output_path == mapping_path:
        parser.error("blind package output and private mapping output must differ")
    try:
        report = json.loads(report_path.read_text())
    except json.JSONDecodeError as error:
        parser.error(f"tournament report is not JSON: {error.msg}")
    if not isinstance(report, dict):
        parser.error("tournament report must be a JSON object")
    try:
        package, mapping = build_blind_product_package(report, salt=secrets.token_bytes(32))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json(package) + b"\n")
    mapping_path.touch(exist_ok=True)
    mapping_path.chmod(0o600)
    mapping_path.write_bytes(canonical_json(mapping) + b"\n")
    print(output_path)
    return 0


def write_product_council_plan(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Persist public judge assignments from public blinded and private mapping artifacts."""
    plan_path = Path(args.plan).resolve()
    package_path = Path(args.blind_package).resolve()
    mapping_path = Path(args.mapping).resolve()
    output_path = Path(args.output).resolve()
    if not plan_path.is_file() or not package_path.is_file() or not mapping_path.is_file():
        parser.error("council plan, blind package, and private mapping must exist")
    if output_path in {package_path, mapping_path}:
        parser.error("council plan output must not overwrite its input artifacts")
    try:
        plan = load_policy(str(plan_path))
        package = json.loads(package_path.read_text())
        mapping = json.loads(mapping_path.read_text())
        candidates = parse_tournament_candidates(plan)
        if not isinstance(package, dict) or not isinstance(mapping, dict):
            raise ValueError("council artifacts must be JSON objects")
        council_plan = build_product_council_plan(candidates, package, mapping)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_json(council_plan) + b"\n")
    print(output_path)
    return 0


@contextmanager
def frozen_review_files(files: list[Path]) -> Iterator[tuple[list[dict[str, str]], list[Path]]]:
    """Provide immutable, private snapshots for a single model invocation round."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="reviewctl-input-") as directory:
        root = Path(directory)
        source: list[dict[str, str]] = []
        snapshots: list[Path] = []
        for file in files:
            contents = file.read_bytes()
            snapshot = root / file.name
            snapshot.write_bytes(contents)
            source.append({"path": str(file), "sha256": sha256_bytes(contents)})
            snapshots.append(snapshot)
        yield source, snapshots


def review_source_roots(files: list[Path]) -> list[Path]:
    """Return source roots denied to Codex while it reviews their snapshots."""
    roots: list[Path] = []
    for file in files:
        result = subprocess.run(
            ["git", "-C", str(file.parent), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
        root = Path(result.stdout.strip()).resolve() if result.returncode == 0 else file.parent
        root = root.resolve()
        if root not in roots:
            roots.append(root)
    return roots


def sandbox_profile_path(path: Path) -> str:
    """Encode one canonical path as a sandbox-exec profile string literal."""
    return json.dumps(str(path.resolve()))


@contextmanager
def codex_isolation(
    source_roots: list[Path], *, auth_path: Path | None = None
) -> Iterator[CodexIsolation]:
    """Create a minimal Codex home and deny reads from reviewed source roots.

    macOS sandbox-exec cannot provide a portable whole-filesystem boundary. This
    targeted profile guarantees that the original proprietary checkout cannot be
    read after frozen snapshots are created; the prompt and Codex working
    directory point only at those snapshots.
    """
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise RuntimeError("proprietary Codex reviews require macOS sandbox-exec")
    source_auth = (
        auth_path or Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser()
    )
    if not source_auth.is_file():
        raise RuntimeError(f"Codex isolation requires auth file: {source_auth}")

    with tempfile.TemporaryDirectory(prefix="reviewctl-codex-") as directory:
        home = Path(directory)
        copied_auth = home / "auth.json"
        shutil.copyfile(source_auth, copied_auth)
        copied_auth.chmod(0o600)
        profile = home / "source-root-deny.sb"
        denies = "\n".join(
            f"(deny file-read* (subpath {sandbox_profile_path(root)}))" for root in source_roots
        )
        profile.write_text(f"(version 1)\n(allow default)\n{denies}\n")
        environment = dict(os.environ)
        environment.update({"CODEX_HOME": str(home), "HOME": str(home), "TMPDIR": str(home)})
        environment.pop("CODEX_AUTH_FILE", None)
        yield CodexIsolation(environment=environment, home=home, profile=profile)


def source_allowed(policy: dict[str, Any], model: str) -> bool:
    return bool(policy.get("models", {}).get(model, {}).get("source_allowed", False))


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def packet_prompt(prompt: str, files: list[Path], response_contract: str = "verdict") -> str:
    """Add stable file names so structured findings are comparable across models."""
    supplied = ", ".join(file.name for file in files)
    if response_contract in {"product-review-json", "product-judge-json"}:
        return (
            f"{prompt}\n\n"
            f"Supplied synthetic briefing files: {supplied}. "
            "Use only the stated briefing. Do not invent product requirements, integrations, "
            "or business facts not present in it."
        )
    return (
        f"{prompt}\n\n"
        f"Supplied review files: {supplied}. "
        "For every finding, set path to the exact supplied basename, "
        "never an inferred path or symbol. "
        "Report only defects demonstrable from the supplied files; "
        "do not infer missing schema, fields, or surrounding behavior."
    )


def invoke_llm(
    *,
    llm_bin: str,
    prompt: str,
    model: str,
    database: Path,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
) -> tuple[int, str]:
    command = [
        llm_bin,
        "prompt",
        prompt,
        "-m",
        model,
        "-d",
        str(database),
        "--log",
        "--no-stream",
        "--usage",
        "-o",
        "max_tokens",
        str(max_output_tokens),
    ]
    for file in files:
        command.extend(["-f", str(file)])
    if schema := response_schema(response_contract):
        command.extend(["--schema", json.dumps(schema, separators=(",", ":"))])

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return process.returncode, stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        return 124, "review attempt timed out"


def openrouter_packet(
    prompt: str, files: list[Path], response_contract: str = "findings-json"
) -> str:
    """Embed bounded frozen fragments in the direct OpenRouter request."""
    fragments = "\n\n".join(
        f"--- BEGIN {file.name} ---\n{file.read_text()}\n--- END {file.name} ---" for file in files
    )
    if response_contract == "findings-json":
        contract = (
            "Return only JSON matching the supplied schema. The top-level object has exactly "
            "`verdict` and `findings`. Each finding has exactly six fields: `severity`, `path`, "
            "`line`, `title`, `evidence`, and `reproduction`. Use `changes-requested` if and only "
            "if `findings` is non-empty; use `approved` if and only if `findings` is empty."
        )
    elif response_contract == "product-review-json":
        contract = (
            "Return only JSON matching the supplied product-design schema. Address every stated "
            "non-negotiable in `constraintChecks`; do not turn a simulation into execution."
        )
    elif response_contract == "product-judge-json":
        contract = (
            "Return only JSON matching the supplied council-judgment schema. Score the proposal, "
            "not its prose style, and list every violated non-negotiable by its exact identifier."
        )
    else:
        return f"{prompt}\n\n{fragments}"
    return f"{prompt}\n\n{contract}\n\n{fragments}"


def numeric_value(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def token_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def positive_timeout_seconds(value: str) -> int:
    """Parse a CLI timeout that preserves the same deadline across transports."""
    try:
        timeout_seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout seconds must be an integer") from error
    if timeout_seconds <= 0:
        raise argparse.ArgumentTypeError("timeout seconds must be positive")
    return timeout_seconds


def invoke_openrouter(
    *,
    api_key: str | None,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    provider_preferences: dict[str, object] | None = None,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    response_path: Path,
) -> tuple[int, str, PersistedResponse]:
    """Call OpenRouter directly and persist source-safe request and raw response evidence."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    if not api_key:
        return 127, "OPENROUTER_API_KEY is not configured", blank
    payload: dict[str, object] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "messages": [
            {"role": "user", "content": openrouter_packet(prompt, files, response_contract)}
        ],
    }
    if schema := response_schema(response_contract):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": response_contract, "strict": True, "schema": schema},
        }
    if provider_preferences:
        payload["provider"] = provider_preferences
    request_path.write_bytes(canonical_json(payload) + b"\n")
    started = time.monotonic()
    curl_bin = os.environ.get("CURL_BIN", "curl")
    curl_config = (
        f'header = "Authorization: Bearer {api_key}"\nheader = "Content-Type: application/json"\n'
    ).encode()
    command = [
        curl_bin,
        "--config",
        "-",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout_seconds),
        "--connect-timeout",
        str(timeout_seconds),
        "--request",
        "POST",
        "--data-binary",
        f"@{request_path}",
        "--output",
        str(response_path),
        "--write-out",
        "%{http_code}",
        "https://openrouter.ai/api/v1/chat/completions",
    ]
    try:
        completed = subprocess.run(
            command,
            input=curl_config,
            capture_output=True,
            check=False,
            timeout=timeout_seconds + 1,
        )
    except FileNotFoundError:
        return 127, f"OpenRouter transport executable not found: {curl_bin}", blank
    except OSError as error:
        return 127, f"OpenRouter transport could not execute: {error}", blank
    except subprocess.TimeoutExpired:
        return 124, "review attempt timed out", blank
    raw_response = response_path.read_bytes() if response_path.is_file() else b""
    if completed.returncode == 28:
        return 124, "review attempt timed out", blank
    http_status_text = completed.stdout.decode(errors="replace").strip()
    http_status = int(http_status_text) if http_status_text.isdigit() else None
    if completed.returncode != 0:
        message = raw_response.decode(errors="replace") or completed.stderr.decode(errors="replace")
        status = http_status if http_status is not None and http_status >= 400 else 502
        return status, message or "OpenRouter transport failed", blank
    if http_status is None:
        return 502, "OpenRouter transport did not report an HTTP status", blank
    if http_status >= 400:
        return http_status, raw_response.decode(errors="replace"), blank
    try:
        payload_response = json.loads(raw_response)
    except json.JSONDecodeError:
        return 502, "OpenRouter returned invalid JSON", blank
    if not isinstance(payload_response, dict):
        return 502, "OpenRouter returned a non-object response", blank
    provider_error = payload_response.get("error")
    if provider_error is not None:
        error_payload = provider_error if isinstance(provider_error, dict) else {}
        error_code = error_payload.get("code")
        error_message = error_payload.get("message")
        status = error_code if isinstance(error_code, int) and 400 <= error_code <= 599 else 502
        message = (
            error_message if isinstance(error_message, str) else "OpenRouter returned an error"
        )
        return status, message, blank
    choices = payload_response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return 502, "OpenRouter returned malformed choices", blank
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    usage = payload_response.get("usage")
    usage_values = usage if isinstance(usage, dict) else {}
    response_id = payload_response.get("id")
    response_model = payload_response.get("model")
    response_provider = payload_response.get("provider")
    return (
        0,
        "",
        PersistedResponse(
            conversation_id=response_id if isinstance(response_id, str) else "",
            cost_usd=numeric_value(usage_values.get("cost")),
            duration_ms=round((time.monotonic() - started) * 1000),
            input_tokens=token_value(usage_values.get("prompt_tokens")),
            model=response_model if isinstance(response_model, str) else "",
            output_tokens=token_value(usage_values.get("completion_tokens")),
            provider=response_provider if isinstance(response_provider, str) else None,
            response=content if isinstance(content, str) else "",
        ),
    )


def invoke_agy(
    *,
    agy_bin: str,
    prompt: str,
    model: str,
    files: list[Path],
    max_output_tokens: int,
    response_contract: str,
    timeout_seconds: int,
    request_path: Path,
    response_path: Path,
) -> tuple[int, str, PersistedResponse]:
    """Run a native Antigravity model in an empty sandbox with durable JSON evidence."""
    blank = PersistedResponse("", None, None, None, "", None, None, "")
    request_payload: dict[str, object] = {
        "command": "agy",
        "maxOutputTokens": max_output_tokens,
        "model": model,
        "prompt": openrouter_packet(prompt, files, response_contract),
        "responseContract": response_contract,
        "sandbox": True,
    }
    request_path.write_bytes(canonical_json(request_payload) + b"\n")
    command = [
        agy_bin,
        "--model",
        model,
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout_seconds}s",
        "--disable-slash-commands",
        "--sandbox",
    ]
    if schema := response_schema(response_contract):
        command.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
    command.extend(["--print", str(request_payload["prompt"])])
    try:
        with tempfile.TemporaryDirectory(prefix="reviewctl-agy-") as sandbox:
            process = subprocess.Popen(
                command,
                cwd=sandbox,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                return 124, "review attempt timed out", blank
    except OSError as error:
        return 127, str(error), blank
    response_path.write_bytes(stdout)
    stderr_text = stderr.decode(errors="replace")
    if process.returncode != 0:
        return process.returncode, stderr_text, blank
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return 502, "agy returned invalid JSON", blank
    if not isinstance(payload, dict):
        return 502, "agy returned a non-object response", blank
    status = payload.get("status")
    if status != "SUCCESS":
        return 502, f"agy returned status {status}", blank
    usage = payload.get("usage")
    usage_values = usage if isinstance(usage, dict) else {}
    duration_seconds = numeric_value(payload.get("duration_seconds"))
    conversation_id = payload.get("conversation_id")
    response = payload.get("response")
    return (
        0,
        "",
        PersistedResponse(
            conversation_id=conversation_id if isinstance(conversation_id, str) else "",
            cost_usd=None,
            duration_ms=round(duration_seconds * 1000) if duration_seconds is not None else None,
            input_tokens=token_value(usage_values.get("input_tokens")),
            model=model,
            output_tokens=token_value(usage_values.get("output_tokens")),
            provider="google-antigravity",
            response=response if isinstance(response, str) else "",
        ),
    )


def codex_prompt(prompt: str, response_contract: str) -> str:
    """Add the output contract Codex must satisfy without expanding source scope."""
    if response_contract == "findings-json":
        contract = (
            "Read the frozen files in the current working directory before reviewing. "
            "Return only JSON matching the supplied findings schema. "
            "Use approved only when there are no findings, and changes-requested only when "
            "findings is non-empty. List every frozen snapshot you actually reviewed in "
            "reviewedFiles; do not emit a verdict if you cannot read a file. The runner, not "
            "you, records the authoritative source hashes."
        )
    elif response_contract == "product-review-json":
        contract = (
            "Read the frozen briefing files in the current working directory before designing. "
            "Return only JSON matching the supplied product-design schema. List every frozen "
            "snapshot you actually reviewed in reviewedFiles; do not return a design if you cannot "
            "read a file. The runner records the authoritative source hashes."
        )
    elif response_contract == "product-judge-json":
        contract = (
            "Read the frozen briefing and anonymous candidate response in the current working "
            "directory. Return only JSON matching the supplied council-judgment schema. List every "
            "frozen snapshot you actually reviewed in reviewedFiles. The runner records the "
            "authoritative source hashes."
        )
    else:
        contract = "Return a complete verdict beginning with VERDICT: and ending with punctuation."
    return f"{prompt}\n\n{contract} Read only the supplied review files."


def invoke_codex(
    *,
    codex_bin: str,
    prompt: str,
    model: str,
    response_contract: str,
    source_roots: list[Path] | None,
    timeout_seconds: int,
    workspace: Path,
) -> tuple[int, str, PersistedResponse]:
    """Run Codex against the isolated snapshots and recover its final response."""
    isolation: CodexIsolation | None = None
    try:
        if source_roots:
            isolation_context = codex_isolation(source_roots)
            isolation = isolation_context.__enter__()
        else:
            isolation_context = None
    except RuntimeError as error:
        return (
            127,
            str(error),
            PersistedResponse("", None, None, None, "", None, "openai-codex", ""),
        )

    temporary_root = isolation.home if isolation else workspace
    output_path = temporary_root / "codex-response.md"
    schema_path: Path | None = None
    command = [
        codex_bin,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "-C",
        str(workspace),
        "--output-last-message",
        str(output_path),
    ]
    if schema := response_schema(response_contract, codex=source_roots is not None):
        schema_path = output_path.with_name("codex-response.schema.json")
        schema_path.write_bytes(canonical_json(schema))
        command.extend(["--output-schema", str(schema_path)])
    command.append(codex_prompt(prompt, response_contract))
    if isolation:
        command = ["sandbox-exec", "-f", str(isolation.profile), *command]

    started = time.monotonic()
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=isolation.environment if isolation else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            exit_code = process.returncode
            stderr_text = stderr.decode(errors="replace")
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            stdout = b""
            exit_code = 124
            stderr_text = "review attempt timed out"
            timed_out = True
        transport_output = f"{stdout.decode(errors='replace')}\n{stderr_text}"
        session = re.search(r"session id:\s*([^\s]+)", transport_output)
        resolved_model = re.search(r"^model:\s*([^\s]+)", transport_output, flags=re.MULTILINE)
        return (
            exit_code,
            stderr_text,
            PersistedResponse(
                conversation_id=session.group(1) if session else "",
                cost_usd=None,
                duration_ms=round((time.monotonic() - started) * 1000),
                input_tokens=None,
                model=resolved_model.group(1) if resolved_model else "",
                output_tokens=None,
                provider="openai-codex",
                response=(
                    output_path.read_text() if output_path.is_file() and not timed_out else ""
                ),
            ),
        )
    finally:
        output_path.unlink(missing_ok=True)
        if schema_path:
            schema_path.unlink(missing_ok=True)
        if isolation:
            assert isolation_context is not None
            isolation_context.__exit__(None, None, None)


def load_response(database: Path) -> PersistedResponse | None:
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(responses)")}
            required = {"response", "conversation_id", "model"}
            if not required <= columns:
                return None
            input_tokens = "input_tokens" if "input_tokens" in columns else "NULL"
            output_tokens = "output_tokens" if "output_tokens" in columns else "NULL"
            duration_ms = "duration_ms" if "duration_ms" in columns else "NULL"
            response_json = "response_json" if "response_json" in columns else "NULL"
            row = connection.execute(
                "SELECT response, conversation_id, model, "
                f"{input_tokens}, {output_tokens}, {duration_ms}, {response_json} "
                "FROM responses ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        transport = json.loads(row[6]) if row[6] else {}
    except json.JSONDecodeError:
        transport = {}
    usage = transport.get("usage", {}) if isinstance(transport, dict) else {}
    cost = usage.get("cost") if isinstance(usage, dict) else None
    provider = transport.get("provider") if isinstance(transport, dict) else None
    return PersistedResponse(
        response=row[0] or "",
        conversation_id=row[1] or "",
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        duration_ms=row[5] if isinstance(row[5], int) else None,
        model=row[2] or "",
        input_tokens=row[3],
        output_tokens=row[4],
        provider=provider if isinstance(provider, str) else None,
    )


def response_is_complete(response: str) -> bool:
    stripped = response.strip()
    return len(stripped) >= 20 and "VERDICT" in stripped.upper() and stripped[-1] in ".]}"


def non_empty_strings(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def exact_object(value: object, fields: set[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == fields
        and all(isinstance(value[field], str) and value[field].strip() for field in fields)
    )


def exact_object_list(value: object, fields: set[str]) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(exact_object(item, fields) for item in value)
    )


def valid_architecture(value: object) -> bool:
    fields = {"boundary", "owns", "commands", "events", "readModels"}
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict)
            and set(item) == fields
            and isinstance(item["boundary"], str)
            and bool(item["boundary"].strip())
            and isinstance(item["owns"], str)
            and bool(item["owns"].strip())
            and non_empty_strings(item["commands"], allow_empty=True)
            and non_empty_strings(item["events"], allow_empty=True)
            and non_empty_strings(item["readModels"], allow_empty=True)
            for item in value
        )
    )


def valid_constraint_checks(value: object) -> bool:
    fields = {"constraintId", "disposition", "rationale"}
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict)
            and set(item) == fields
            and isinstance(item["constraintId"], str)
            and bool(item["constraintId"].strip())
            and item["disposition"] in PRODUCT_CONSTRAINT_DISPOSITIONS
            and isinstance(item["rationale"], str)
            and bool(item["rationale"].strip())
            for item in value
        )
    )


def validate_product_review(value: dict[str, Any]) -> dict[str, Any] | None:
    fields = set(PRODUCT_REVIEW_SCHEMA["required"])
    if (
        set(value) != fields
        or not isinstance(value["summary"], str)
        or not value["summary"].strip()
    ):
        return None
    required_lists = ("userJobs", "mvp", "nonGoals", "risks", "acceptanceTests")
    if not all(non_empty_strings(value[field]) for field in required_lists):
        return None
    if not non_empty_strings(value["openQuestions"], allow_empty=True):
        return None
    if not exact_object_list(value["interactionFlow"], {"actor", "action", "outcome"}):
        return None
    if not exact_object_list(value["domainEntities"], {"name", "purpose"}):
        return None
    if not exact_object_list(value["stateTransitions"], {"from", "to", "guard"}):
        return None
    if not valid_architecture(value["architecture"]):
        return None
    if not exact_object_list(value["operationalControls"], {"control", "approach"}):
        return None
    return value if valid_constraint_checks(value["constraintChecks"]) else None


def validate_product_judge(value: dict[str, Any]) -> dict[str, Any] | None:
    if set(value) != {"scores", "hardConstraintViolations", "rationale"}:
        return None
    scores = value["scores"]
    if (
        not isinstance(scores, dict)
        or set(scores) != PRODUCT_SCORE_FIELDS
        or not all(
            isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 4
            for score in scores.values()
        )
    ):
        return None
    if not non_empty_strings(value["hardConstraintViolations"], allow_empty=True):
        return None
    return value if isinstance(value["rationale"], str) and value["rationale"].strip() else None


def validate_read_proof(value: dict[str, Any], expected_file_hashes: dict[str, str]) -> bool:
    reviewed_files = value.get("reviewedFiles")
    if not isinstance(reviewed_files, list):
        return False
    reviewed_paths: set[str] = set()
    for reviewed in reviewed_files:
        if not isinstance(reviewed, str) or not reviewed:
            return False
        # Codex receives a private frozen snapshot and may report its absolute
        # sandbox path. Frozen inputs have unique basenames, so normalize that
        # path form before comparing the declared files to receipt provenance.
        snapshot_name = Path(reviewed).name
        proof_path = reviewed if reviewed in expected_file_hashes else snapshot_name
        if proof_path in reviewed_paths:
            return False
        # The model declares only the frozen snapshots it reviewed. The runner
        # preserves the immutable SHA-256 values in the receipt provenance.
        reviewed_paths.add(proof_path)
    return reviewed_paths == set(expected_file_hashes)


def validate_review_response(
    response: str,
    contract: str,
    *,
    expected_file_hashes: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if contract == "verdict":
        return (
            {"verdict": "unstructured", "findings": []} if response_is_complete(response) else None
        )
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if contract == "product-review-json":
        if expected_file_hashes is not None:
            expected_fields = {*PRODUCT_REVIEW_SCHEMA["required"], "reviewedFiles"}
            if set(value) != expected_fields or not validate_read_proof(
                value, expected_file_hashes
            ):
                return None
            value = {key: item for key, item in value.items() if key != "reviewedFiles"}
        return validate_product_review(value)
    if contract == "product-judge-json":
        if expected_file_hashes is not None:
            expected_fields = {*PRODUCT_JUDGE_SCHEMA["required"], "reviewedFiles"}
            if set(value) != expected_fields or not validate_read_proof(
                value, expected_file_hashes
            ):
                return None
            value = {key: item for key, item in value.items() if key != "reviewedFiles"}
        return validate_product_judge(value)
    if contract != "findings-json" or not isinstance(value.get("verdict"), str):
        return None
    if value["verdict"] not in REVIEW_VERDICTS:
        return None
    findings = value.get("findings")
    if not isinstance(findings, list):
        return None
    for finding in findings:
        if not isinstance(finding, dict) or not FINDING_FIELDS <= finding.keys():
            return None
        if not all(
            isinstance(finding[field], str) and finding[field].strip()
            for field in FINDING_FIELDS - {"line"}
        ):
            return None
        if finding["severity"] not in FINDING_SEVERITIES:
            return None
        if not isinstance(finding["line"], int) or finding["line"] < 1:
            return None
        if expected_file_hashes is not None and finding["path"] not in expected_file_hashes:
            return None
    if (value["verdict"] == "approved") != (not findings):
        return None
    if expected_file_hashes is None:
        if value.get("reviewedFiles") is not None or set(value) != {"verdict", "findings"}:
            return None
        return {"verdict": value["verdict"], "findings": findings}
    if set(value) != {
        "verdict",
        "findings",
        "reviewedFiles",
    } or not validate_read_proof(value, expected_file_hashes):
        return None
    return {
        "verdict": value["verdict"],
        "findings": findings,
        "reviewedFiles": value["reviewedFiles"],
    }


def review_validation_error(
    response: str,
    contract: str,
    *,
    expected_file_hashes: dict[str, str] | None = None,
) -> str | None:
    """Explain a rejected structured response without changing the acceptance contract."""
    if (
        validate_review_response(response, contract, expected_file_hashes=expected_file_hashes)
        is not None
    ):
        return None
    if contract == "verdict":
        return "verdict: response is incomplete"
    try:
        value = json.loads(response)
    except json.JSONDecodeError:
        return f"{contract}: invalid JSON"
    if not isinstance(value, dict):
        return f"{contract}: top-level response must be an object"

    if expected_file_hashes is not None:
        if contract == "product-review-json":
            expected_fields = {*PRODUCT_REVIEW_SCHEMA["required"], "reviewedFiles"}
        elif contract == "product-judge-json":
            expected_fields = {*PRODUCT_JUDGE_SCHEMA["required"], "reviewedFiles"}
        else:
            expected_fields = {"verdict", "findings", "reviewedFiles"}
        if set(value) != expected_fields:
            return f"{contract}: response fields do not match the required schema"
        if not validate_read_proof(value, expected_file_hashes):
            return f"{contract}: reviewedFiles proof does not match frozen inputs"

    if contract == "findings-json":
        verdict = value.get("verdict")
        if not isinstance(verdict, str):
            return "findings-json: verdict must be a string"
        if verdict not in REVIEW_VERDICTS:
            return (
                f"findings-json: invalid verdict {verdict!r}; "
                "expected approved or changes-requested"
            )
        return "findings-json: findings do not satisfy the required schema or verdict invariant"
    return f"{contract}: response does not satisfy the required schema"


def seal(path: Path, contents: bytes, recipient: str) -> str:
    target = path.with_suffix(path.suffix + ".age")
    result = subprocess.run(
        ["age", "-r", recipient, "-o", str(target)],
        input=contents,
        text=False,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "age failed")
    return target.name


def run_review(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    prompt, files = validate_request(parser, args)
    review_prompt = packet_prompt(prompt, files, args.response_contract)
    transport = getattr(args, "transport", "llm")
    try:
        provider_preferences = provider_preferences_from_args(args)
    except ValueError as error:
        parser.error(str(error))
    if provider_preferences and transport != "openrouter":
        parser.error("provider preferences require --transport openrouter")
    policy_digest: str | None = None
    if args.source_class == "proprietary":
        if transport in {"openrouter", "agy"}:
            print(
                "reviewctl: direct OpenRouter or native Antigravity transport is synthetic-only",
                file=sys.stderr,
            )
            return 3
        if args.response_contract != "findings-json":
            print(
                "reviewctl: proprietary source requires --response-contract findings-json",
                file=sys.stderr,
            )
            return 3
        if not args.policy:
            print("reviewctl: proprietary source requires --policy", file=sys.stderr)
            return 3
        policy = load_policy(args.policy)
        denied = [model for model in args.models if not source_allowed(policy, model)]
        if denied:
            print(
                f"reviewctl: models are synthetic-only for proprietary source: {', '.join(denied)}",
                file=sys.stderr,
            )
            return 3
        policy_digest = policy_sha256(args.policy)
    artifact_root = Path(args.artifact_root)
    turn_dir = review_root(artifact_root, args.review_id)
    attempts_dir = turn_dir / "attempts"
    attempts_dir.mkdir()
    codex_source_roots = (
        review_source_roots(files)
        if (transport == "codex" and args.source_class == "proprietary")
        else None
    )
    snapshots_context = frozen_review_files(files)
    source_files, snapshots = snapshots_context.__enter__()
    snapshot_hashes = {file.name: sha256_bytes(file.read_bytes()) for file in snapshots}
    source = {
        "files": source_files,
        "git": git_metadata(Path.cwd()),
    }
    attempts: list[dict[str, Any]] = []
    accepted: PersistedResponse | None = None
    accepted_review: dict[str, Any] | None = None
    accepted_attempt: int | None = None

    max_attempts = getattr(args, "max_attempts", 1)
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        parser.error("max attempts must be an integer from 1 to 3")
    number = 0
    for model in args.models:
        for _ in range(max_attempts):
            number += 1
            transport_model = (
                model.removeprefix("openrouter/") if transport == "openrouter" else model
            )
            attempt_dir = attempts_dir / f"{number:02d}"
            attempt_dir.mkdir()
            database: Path | None = None
            request_path: Path | None = None
            response_path: Path | None = None
            if transport == "codex":
                exit_code, stderr, persisted = invoke_codex(
                    codex_bin=os.environ.get("CODEX_BIN", "codex"),
                    prompt=review_prompt,
                    model=model,
                    response_contract=args.response_contract,
                    source_roots=codex_source_roots,
                    timeout_seconds=args.timeout_seconds,
                    workspace=snapshots[0].parent,
                )
                # Codex writes its transient response inside the isolated
                # sandbox, which is removed after the attempt. Persist a copy
                # in the caller-controlled evidence root so a rejected output
                # remains diagnosable without weakening the contract gate.
                response_path = attempt_dir / "response.md"
                response_path.write_text(persisted.response)
            elif transport == "openrouter":
                request_path = attempt_dir / "request.json"
                response_path = attempt_dir / "response.json"
                exit_code, stderr, persisted = invoke_openrouter(
                    api_key=os.environ.get("OPENROUTER_API_KEY"),
                    prompt=review_prompt,
                    model=transport_model,
                    files=snapshots,
                    max_output_tokens=args.max_output_tokens,
                    provider_preferences=provider_preferences,
                    response_contract=args.response_contract,
                    timeout_seconds=args.timeout_seconds,
                    request_path=request_path,
                    response_path=response_path,
                )
            elif transport == "agy":
                request_path = attempt_dir / "request.json"
                response_path = attempt_dir / "response.json"
                exit_code, stderr, persisted = invoke_agy(
                    agy_bin=os.environ.get("AGY_BIN", "agy"),
                    prompt=review_prompt,
                    model=transport_model,
                    files=snapshots,
                    max_output_tokens=args.max_output_tokens,
                    response_contract=args.response_contract,
                    timeout_seconds=args.timeout_seconds,
                    request_path=request_path,
                    response_path=response_path,
                )
            else:
                database = attempt_dir / "transport.sqlite3"
                exit_code, stderr = invoke_llm(
                    llm_bin=os.environ.get("LLM_BIN", "llm"),
                    prompt=review_prompt,
                    model=model,
                    database=database,
                    files=snapshots,
                    max_output_tokens=args.max_output_tokens,
                    response_contract=args.response_contract,
                    timeout_seconds=args.timeout_seconds,
                )
                persisted = load_response(database)
            review = (
                validate_review_response(
                    persisted.response,
                    args.response_contract,
                    expected_file_hashes=(
                        snapshot_hashes
                        if transport == "codex" and args.source_class == "proprietary"
                        else None
                    ),
                )
                if persisted is not None
                else None
            )
            validation_error = (
                review_validation_error(
                    persisted.response,
                    args.response_contract,
                    expected_file_hashes=(
                        snapshot_hashes
                        if transport == "codex" and args.source_class == "proprietary"
                        else None
                    ),
                )
                if persisted is not None and review is None
                else None
            )
            if exit_code == 124:
                result = "timeout"
            elif exit_code != 0:
                result = "transport-failed"
            elif persisted is None:
                result = "missing-response"
            elif persisted.model != transport_model:
                result = "model-mismatch"
            elif not resolved_provider_matches(provider_preferences, persisted.provider):
                result = "provider-mismatch"
            elif not persisted.response.strip():
                result = "empty"
            elif not persisted.conversation_id:
                result = "missing-conversation"
            elif review is None:
                result = "incomplete"
            else:
                result = "accepted"

            attempt = {
                "database": str(database) if database else None,
                "evidence": {
                    "request": str(request_path) if request_path else None,
                    "response": str(response_path) if response_path else None,
                },
                "exitCode": exit_code,
                "isolation": ("macos-source-root-deny" if codex_source_roots else None),
                "model": {"requested": model, "resolved": persisted.model if persisted else None},
                "provider": {
                    "requested": provider_preferences.get("only", [])
                    if provider_preferences
                    else [],
                    "resolved": persisted.provider if persisted else None,
                },
                "providerPreferences": provider_preferences,
                "result": result,
                "validationError": validation_error,
                "transport": transport,
                "stderrSha256": sha256_bytes(stderr.encode()),
                "costUsd": persisted.cost_usd if persisted else None,
                "durationMs": persisted.duration_ms if persisted else None,
                "tokens": {
                    "input": persisted.input_tokens if persisted else None,
                    "output": persisted.output_tokens if persisted else None,
                },
                "conversationId": persisted.conversation_id if persisted else None,
                "findings": review.get("findings", []) if review else [],
            }
            attempts.append(attempt)
            (attempt_dir / "attempt.json").write_bytes(canonical_json(attempt) + b"\n")
            if result == "accepted":
                accepted = persisted
                accepted_review = review
                accepted_attempt = number
                break
            if result not in RETRIABLE_REVIEW_RESULTS:
                break
        if accepted is not None:
            break

    receipt: dict[str, Any] = {
        "acceptedAttempt": accepted_attempt,
        "attempts": attempts,
        "createdAt": utc_now(),
        "model": {
            "requested": args.models,
            "resolved": accepted.model if accepted else None,
        },
        "policy": {"sha256": policy_digest},
        "prompt": {
            "sha256": sha256_bytes(prompt.encode()),
            "characters": len(prompt),
            "packetSha256": sha256_bytes(review_prompt.encode()),
        },
        "result": "accepted" if accepted else "unavailable",
        "reviewContract": args.response_contract,
        "reviewId": args.review_id,
        "source": source,
        "tool": {"name": "reviewctl", "version": __version__},
        "transport": transport,
        "providerPreferences": provider_preferences,
    }
    if accepted:
        receipt["response"] = {
            "sha256": sha256_bytes(accepted.response.encode()),
            "characters": len(accepted.response),
            "conversationId": accepted.conversation_id,
            "costUsd": accepted.cost_usd,
            "durationMs": accepted.duration_ms,
            "provider": accepted.provider,
        }
        if args.response_contract == "findings-json":
            receipt["findings"] = accepted_review["findings"] if accepted_review else []
            receipt["verdict"] = accepted_review["verdict"] if accepted_review else "unstructured"
        elif args.response_contract in {"product-review-json", "product-judge-json"}:
            receipt["review"] = accepted_review
    try:
        if args.seal_to:
            request_payload = canonical_json(
                {"models": args.models, "prompt": review_prompt, "source": source}
            )
            receipt["sealed"] = {
                "request": seal(turn_dir / "request.json", request_payload, args.seal_to),
                "response": seal(
                    turn_dir / "response.md",
                    (accepted.response if accepted else "").encode(),
                    args.seal_to,
                ),
            }

        receipt["sha256"] = sha256_bytes(canonical_json(receipt))
        (turn_dir / "receipt.json").write_bytes(canonical_json(receipt) + b"\n")
        print(turn_dir)
        return 0 if accepted else 1
    finally:
        snapshots_context.__exit__(None, None, None)
        for database in attempts_dir.glob("*/transport.sqlite3"):
            database.unlink(missing_ok=True)


def valid_receipt(receipt: dict[str, Any]) -> bool:
    """Verify the hash embedded in an in-memory receipt without mutating it."""
    recorded = receipt.get("sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "sha256"}
    return isinstance(recorded, str) and recorded == sha256_bytes(canonical_json(unsigned))


def verify_receipt(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    receipt = json.loads(receipt_path.read_text())
    valid = isinstance(receipt, dict) and valid_receipt(receipt)
    print(json.dumps({"receipt": str(receipt_path), "valid": valid}, sort_keys=True))
    return 0 if valid else 1


def policy_check(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    model = policy.get("models", {}).get(args.model, {})
    source_allowed = bool(model.get("source_allowed", False))
    decision = {
        "model": args.model,
        "sourceAllowed": source_allowed,
        "syntheticOnly": not source_allowed,
        "zdr": model.get("zdr", "unknown"),
        "dataCollection": model.get("data_collection", "unknown"),
    }
    print(json.dumps(decision, sort_keys=True))
    return 0 if source_allowed else 3


def estimate_tokens(prompt: str, files: list[Path]) -> int:
    return max(1, (len(prompt.encode()) + sum(file.stat().st_size for file in files) + 3) // 4)


def score_findings(
    *, expected: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> dict[str, int | float]:
    """Score a bounded review against an adjudicated location and severity rubric."""
    matched_expected: set[int] = set()
    line_accurate = 0
    for finding in findings:
        filename = Path(str(finding["path"])).name
        same_line = [
            index
            for index, item in enumerate(expected)
            if int(item.get("line_start", item.get("line")))
            <= finding["line"]
            <= int(item.get("line_end", item.get("line")))
            and (
                Path(str(item["path"])).name == filename
                or (isinstance(item.get("symbol"), str) and item["symbol"] in str(finding["path"]))
            )
        ]
        if same_line:
            line_accurate += 1
        for index in same_line:
            if (
                index not in matched_expected
                and FINDING_SEVERITY_RANK[str(finding["severity"]).lower()]
                >= FINDING_SEVERITY_RANK[str(expected[index]["severity"]).lower()]
            ):
                matched_expected.add(index)
                break
    matched = len(matched_expected)
    return {
        "expected": len(expected),
        "matched": matched,
        "falsePositives": len(findings) - matched,
        "lineAccurate": line_accurate,
        "precision": matched / len(findings) if findings else 1.0,
        "recall": matched / len(expected) if expected else 1.0,
    }


def tournament_report_path(plan: dict[str, Any], plan_path: Path) -> Path:
    configured = plan.get("artifact_root")
    root = Path(configured) if configured else Path("tournament-artifacts")
    if not root.is_absolute():
        root = plan_path.parent / root
    root.mkdir(parents=True, exist_ok=True)
    return root / "tournament.json"


def write_tournament_report(
    path: Path,
    *,
    budget: float,
    estimated_spend: float,
    actual_spend: float,
    result: str,
    runs: list[dict[str, Any]],
) -> None:
    """Atomically persist the current tournament state after every attempt."""
    payload = {
        "actualSpendUsd": actual_spend,
        "budgetUsd": budget,
        "estimatedSpendUsd": estimated_spend,
        "result": result,
        "runs": runs,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(canonical_json(payload) + b"\n")
    temporary.replace(path)


def receipt_attempt_cost(receipt: dict[str, Any]) -> float | None:
    """Return total provider spend for all recorded attempts in one review receipt."""
    attempts = receipt.get("attempts")
    if not isinstance(attempts, list):
        return None
    costs = [
        float(cost)
        for attempt in attempts
        if isinstance(attempt, dict)
        and isinstance((cost := attempt.get("costUsd")), int | float)
        and not isinstance(cost, bool)
    ]
    return sum(costs) if costs else None


def select_tournament_cases(
    parser: argparse.ArgumentParser, plan: dict[str, Any], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Select named cases and an optional phase without silently widening the corpus."""
    cases = plan.get("cases", [])
    if not isinstance(cases, list):
        parser.error("tournament plan cases must be a list")
    stage = getattr(args, "stage", None)
    if stage is not None:
        cases = [case for case in cases if isinstance(case, dict) and case.get("stage") == stage]
        if not cases:
            parser.error("tournament plan does not contain the requested --stage")
    if args.case_ids:
        selected = set(args.case_ids)
        cases = [case for case in cases if isinstance(case, dict) and case.get("id") in selected]
        if len(cases) != len(selected):
            parser.error("tournament plan does not contain every requested --case")
    return cases


def tournament_case_files(case: dict[str, Any], plan_path: Path) -> list[Path]:
    files = case.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ValueError("tournament case requires a string files list")
    return [
        (Path(item) if Path(item).is_absolute() else plan_path.parent / item).resolve()
        for item in files
    ]


def run_candidate_tournament(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    plan: dict[str, Any],
    plan_path: Path,
    budget: float,
    cases: list[dict[str, Any]],
    candidates: list[TournamentCandidate],
    max_output_tokens: int,
) -> int:
    """Run a mixed-transport synthetic cohort while charging only metered candidates."""
    response_contract = str(plan.get("response_contract", "product-review-json"))
    if response_contract not in {"product-review-json", "product-judge-json"}:
        parser.error("candidate tournaments require a product response contract")
    if not cases:
        parser.error("tournament plan requires candidates and cases")
    max_attempts = plan.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        parser.error("candidate tournament max_attempts must be an integer from 1 to 3")
    timeout_seconds = plan.get("timeout_seconds", 90)
    assert isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool)
    assert timeout_seconds > 0
    report_path = tournament_report_path(plan, plan_path)
    runs: list[dict[str, Any]] = []
    estimated_spend = 0.0
    actual_spend = 0.0
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            parser.error("tournament cases require an id")
        files = tournament_case_files(case, plan_path)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            parser.error("tournament cases require a prompt")
        input_tokens = estimate_tokens(packet_prompt(prompt, files, response_contract), files)
        for candidate in candidates:
            candidate_max_output_tokens = candidate.max_output_tokens or max_output_tokens
            estimate: float | None = None
            if candidate.cost_mode == "metered":
                assert candidate.pricing is not None
                estimate = (
                    input_tokens * candidate.pricing[0] / 1_000_000
                    + candidate_max_output_tokens * candidate.pricing[1] / 1_000_000
                ) * max_attempts
                if estimated_spend + estimate > budget:
                    write_tournament_report(
                        report_path,
                        budget=budget,
                        estimated_spend=estimated_spend,
                        actual_spend=actual_spend,
                        result="budget-exhausted",
                        runs=runs,
                    )
                    print(report_path)
                    return 4
                estimated_spend += estimate
            case_root = report_path.parent / "receipts"
            namespace = argparse.Namespace(
                artifact_root=str(case_root),
                files=[str(file) for file in files],
                max_output_tokens=candidate_max_output_tokens,
                max_attempts=max_attempts,
                models=[candidate.model],
                policy=None,
                prompt=prompt,
                prompt_file=None,
                provider_allow_fallbacks=(
                    candidate.provider_preferences.get("allow_fallbacks")
                    if candidate.provider_preferences
                    else None
                ),
                provider_data_collection=(
                    candidate.provider_preferences.get("data_collection")
                    if candidate.provider_preferences
                    else None
                ),
                provider_require_parameters=(
                    candidate.provider_preferences.get("require_parameters")
                    if candidate.provider_preferences
                    else None
                ),
                provider_only=(
                    candidate.provider_preferences.get("only", [])
                    if candidate.provider_preferences
                    else []
                ),
                provider_order=(
                    candidate.provider_preferences.get("order", [])
                    if candidate.provider_preferences
                    else []
                ),
                provider_sort=(
                    candidate.provider_preferences.get("sort")
                    if candidate.provider_preferences
                    else None
                ),
                review_id=f"tournament.{case['id']}.{candidate.identifier}",
                response_contract=response_contract,
                seal_to=None,
                source_class="synthetic",
                timeout_seconds=timeout_seconds,
                transport=candidate.transport,
            )
            before = set(case_root.glob("**/receipt.json")) if case_root.exists() else set()
            exit_code = run_review(parser, namespace)
            receipt_paths = sorted(set(case_root.glob("**/receipt.json")) - before)
            receipt_path = receipt_paths[0]
            receipt = json.loads(receipt_path.read_text())
            actual_cost = receipt_attempt_cost(receipt)
            if candidate.cost_mode == "metered" and actual_cost is not None:
                actual_spend += actual_cost
            runs.append(
                {
                    "actualCostUsd": actual_cost,
                    "candidate": candidate.identifier,
                    "case": case["id"],
                    "councilEligible": candidate.council_eligible,
                    "costMode": candidate.cost_mode,
                    "estimatedCostUsd": estimate,
                    "exitCode": exit_code,
                    "family": candidate.family,
                    "maxOutputTokens": candidate_max_output_tokens,
                    "model": candidate.model,
                    "receipt": str(receipt_path),
                    "result": str(receipt["result"]),
                    "transport": candidate.transport,
                }
            )
            write_tournament_report(
                report_path,
                budget=budget,
                estimated_spend=estimated_spend,
                actual_spend=actual_spend,
                result="running",
                runs=runs,
            )
    write_tournament_report(
        report_path,
        budget=budget,
        estimated_spend=estimated_spend,
        actual_spend=actual_spend,
        result="completed",
        runs=runs,
    )
    print(report_path)
    return 0


def run_tournament(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    plan = load_policy(str(plan_path))
    budget = float(plan.get("budget_usd", 0))
    max_output_tokens = int(plan.get("max_output_tokens", 4096))
    timeout_seconds = plan.get("timeout_seconds", 90)
    if budget <= 0 or max_output_tokens <= 0:
        parser.error("tournament plan requires positive budget_usd and max_output_tokens")
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        parser.error("tournament timeout_seconds must be a positive integer")
    cases = select_tournament_cases(parser, plan, args)
    if "candidates" in plan:
        try:
            candidates = parse_tournament_candidates(plan)
        except ValueError as error:
            parser.error(str(error))
        return run_candidate_tournament(
            parser,
            args,
            plan=plan,
            plan_path=plan_path,
            budget=budget,
            cases=cases,
            candidates=candidates,
            max_output_tokens=max_output_tokens,
        )
    transport = str(plan.get("transport", "llm"))
    try:
        provider_preferences = normalize_provider_preferences(plan.get("provider"))
    except ValueError as error:
        parser.error(str(error))
    if provider_preferences and transport != "openrouter":
        parser.error("tournament provider preferences require transport = openrouter")
    if transport not in {"llm", "codex", "openrouter", "agy"}:
        parser.error("tournament plan requires a supported transport")
    if transport == "agy":
        parser.error("agy does not support tournament budgets because it cannot cap output tokens")
    models = plan.get("models", {})
    if not models or not cases:
        parser.error("tournament plan requires models and cases")
    legacy_max_attempts = plan.get("max_attempts", 1)
    if not isinstance(legacy_max_attempts, int) or not 1 <= legacy_max_attempts <= 3:
        parser.error("tournament plan max_attempts must be an integer from 1 to 3")

    report_path = tournament_report_path(plan, plan_path)
    runs: list[dict[str, Any]] = []
    estimated_spend = 0.0
    actual_spend = 0.0
    for case in cases:
        files = [
            (Path(item) if Path(item).is_absolute() else plan_path.parent / item).resolve()
            for item in case["files"]
        ]
        prompt = str(case["prompt"])
        input_tokens = estimate_tokens(packet_prompt(prompt, files), files)
        for model, pricing in models.items():
            if not isinstance(pricing, dict):
                parser.error("tournament model pricing must be an object")
            raw_candidate_max_output_tokens = pricing.get("max_output_tokens")
            if raw_candidate_max_output_tokens is not None and (
                not isinstance(raw_candidate_max_output_tokens, int)
                or isinstance(raw_candidate_max_output_tokens, bool)
                or raw_candidate_max_output_tokens <= 0
            ):
                parser.error("tournament model max_output_tokens must be a positive integer")
            candidate_max_output_tokens = raw_candidate_max_output_tokens or max_output_tokens
            estimate = (
                input_tokens * float(pricing["input_per_million_usd"]) / 1_000_000
                + candidate_max_output_tokens * float(pricing["output_per_million_usd"]) / 1_000_000
            ) * legacy_max_attempts
            if estimated_spend + estimate > budget:
                write_tournament_report(
                    report_path,
                    budget=budget,
                    estimated_spend=estimated_spend,
                    actual_spend=actual_spend,
                    result="budget-exhausted",
                    runs=runs,
                )
                print(report_path)
                return 4
            estimated_spend += estimate
            case_root = report_path.parent / "receipts"
            namespace = argparse.Namespace(
                artifact_root=str(case_root),
                files=[str(file) for file in files],
                max_attempts=legacy_max_attempts,
                max_output_tokens=candidate_max_output_tokens,
                models=[model],
                policy=None,
                prompt=prompt,
                prompt_file=None,
                provider_allow_fallbacks=(
                    provider_preferences.get("allow_fallbacks") if provider_preferences else None
                ),
                provider_data_collection=(
                    provider_preferences.get("data_collection") if provider_preferences else None
                ),
                provider_require_parameters=(
                    provider_preferences.get("require_parameters") if provider_preferences else None
                ),
                provider_only=(
                    provider_preferences.get("only", []) if provider_preferences else []
                ),
                provider_order=(
                    provider_preferences.get("order", []) if provider_preferences else []
                ),
                provider_sort=(provider_preferences.get("sort") if provider_preferences else None),
                review_id=f"tournament.{case['id']}.{model.replace('/', '-')}",
                response_contract=str(plan.get("response_contract", "verdict")),
                seal_to=None,
                source_class="synthetic",
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
            before = set(case_root.glob("**/receipt.json")) if case_root.exists() else set()
            result = run_review(parser, namespace)
            after = set(case_root.glob("**/receipt.json"))
            receipt_paths = sorted(after - before)
            receipt_path = receipt_paths[0]
            receipt = json.loads(receipt_path.read_text())
            receipt_result = str(receipt["result"])
            actual_cost = receipt_attempt_cost(receipt)
            if actual_cost is None:
                cost = receipt.get("response", {}).get("costUsd")
                actual_cost = float(cost) if isinstance(cost, int | float) else None
            if actual_cost is not None:
                actual_spend += actual_cost
            findings = receipt.get("findings", [])
            runs.append(
                {
                    "case": case["id"],
                    "actualCostUsd": actual_cost,
                    "estimatedCostUsd": estimate,
                    "exitCode": result,
                    "maxOutputTokens": candidate_max_output_tokens,
                    "model": model,
                    "receipt": str(receipt_path),
                    "result": receipt_result,
                    "score": score_findings(
                        expected=list(case.get("expected_findings", [])),
                        findings=findings if isinstance(findings, list) else [],
                    ),
                }
            )
            write_tournament_report(
                report_path,
                budget=budget,
                estimated_spend=estimated_spend,
                actual_spend=actual_spend,
                result="running",
                runs=runs,
            )
    write_tournament_report(
        report_path,
        budget=budget,
        estimated_spend=estimated_spend,
        actual_spend=actual_spend,
        result="completed",
        runs=runs,
    )
    print(report_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reviewctl")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="execute bounded isolated review attempts")
    run.add_argument("--review-id", required=True)
    run.add_argument("--prompt")
    run.add_argument("--prompt-file")
    run.add_argument("--model", dest="models", action="append", default=[])
    run.add_argument("--file", dest="files", action="append", default=[])
    run.add_argument("--artifact-root", default="~/.cache/reviewctl")
    run.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=90)
    run.add_argument("--max-output-tokens", type=int, default=4096)
    run.add_argument("--max-attempts", type=int, default=1)
    run.add_argument("--policy")
    run.add_argument("--provider-only", action="append", default=[])
    run.add_argument("--provider-order", action="append", default=[])
    run.add_argument(
        "--no-provider-fallbacks",
        dest="provider_allow_fallbacks",
        action="store_false",
        default=None,
    )
    run.add_argument("--provider-data-collection", choices=("allow", "deny"))
    run.add_argument(
        "--provider-require-parameters",
        dest="provider_require_parameters",
        action="store_true",
        default=None,
    )
    run.add_argument("--provider-sort", choices=("price", "throughput", "latency"))
    run.add_argument("--seal-to")
    run.add_argument("--source-class", choices=("proprietary", "synthetic"), default="synthetic")
    run.add_argument("--response-contract", choices=sorted(RESPONSE_CONTRACTS), default="verdict")
    run.add_argument("--transport", choices=("llm", "codex", "openrouter", "agy"), default="llm")
    run.set_defaults(handler=lambda namespace: run_review(parser, namespace))

    verify = commands.add_parser("verify", help="verify a receipt hash")
    verify.add_argument("receipt")
    verify.set_defaults(handler=verify_receipt)

    policy = commands.add_parser("policy-check", help="check a model privacy profile")
    policy.add_argument("--policy", required=True)
    policy.add_argument("--model", required=True)
    policy.set_defaults(handler=policy_check)

    tournament = commands.add_parser("tournament", help="run a bounded synthetic model tournament")
    tournament.add_argument("--plan", required=True)
    tournament.add_argument("--case", dest="case_ids", action="append", default=[])
    tournament.add_argument("--stage")
    tournament.set_defaults(handler=lambda namespace: run_tournament(parser, namespace))

    provider_preflight = commands.add_parser(
        "provider-preflight", help="snapshot and validate pinned OpenRouter tournament providers"
    )
    provider_preflight.add_argument("--plan", required=True)
    provider_preflight.add_argument("--timeout-seconds", type=positive_timeout_seconds, default=30)
    provider_preflight.set_defaults(
        handler=lambda namespace: write_provider_preflight(parser, namespace)
    )

    blind_package = commands.add_parser(
        "blind-package", help="separate anonymous product responses from candidate identities"
    )
    blind_package.add_argument("--report", required=True)
    blind_package.add_argument("--output", required=True)
    blind_package.add_argument("--mapping-output", required=True)
    blind_package.set_defaults(
        handler=lambda namespace: write_blind_product_package(parser, namespace)
    )

    council_plan = commands.add_parser(
        "council-plan", help="assign blinded product proposals to independent reviewers"
    )
    council_plan.add_argument("--plan", required=True)
    council_plan.add_argument("--blind-package", required=True)
    council_plan.add_argument("--mapping", required=True)
    council_plan.add_argument("--output", required=True)
    council_plan.set_defaults(
        handler=lambda namespace: write_product_council_plan(parser, namespace)
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(args.handler(args))
    except RuntimeError as error:
        print(f"reviewctl: {error}", file=sys.stderr)
        raise SystemExit(1) from error
