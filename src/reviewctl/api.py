"""Small public API for project-scoped reviews."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from reviewctl.artifacts import ArtifactStore
from reviewctl.backends import BackendExecution, BackendRequest
from reviewctl.config import ReviewConfig, load_config
from reviewctl.contracts import (
    ContractContext,
    EvaluationContext,
    EvaluationStatus,
    get_contract,
)
from reviewctl.dimensions import DIMENSION_SCHEMA_VERSION, merge_dimensions, normalize_dimensions
from reviewctl.errors import ConfigError, Diagnostic
from reviewctl.identity import ProjectIdentityStore
from reviewctl.journal import ProjectJournal


class ReviewTransport(Protocol):
    def execute(self, request: BackendRequest) -> BackendExecution: ...


_REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_CONTEXT_BYTES = 32 * 1024


def _source_opener(path: str, flags: int) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("this platform cannot open review sources without following symlinks")
    return os.open(path, flags | no_follow | getattr(os, "O_NONBLOCK", 0))


def _read_source_bytes(path: Path) -> bytes | None:
    with open(path, "rb", opener=_source_opener) as stream:
        source = os.fstat(stream.fileno())
        if not stat.S_ISREG(source.st_mode):
            raise OSError("review source is not a regular file")
        if source.st_size > MAX_SOURCE_BYTES:
            return None
        return stream.read(MAX_SOURCE_BYTES + 1)


@dataclass(frozen=True)
class Finding:
    severity: str
    path: str
    line: int | None
    title: str
    evidence: str
    reproduction: str

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> Finding:
        required = ("severity", "path", "title", "evidence", "reproduction")
        if not all(key in value for key in required):
            raise ValueError("finding is missing a required field")
        line = value.get("line")
        if line is not None and type(line) is not int:
            raise ValueError("finding line must be an integer or null")
        return cls(
            severity=str(value["severity"]),
            path=str(value["path"]),
            line=line,
            title=str(value["title"]),
            evidence=str(value["evidence"]),
            reproduction=str(value["reproduction"]),
        )


@dataclass(frozen=True)
class ReviewRequest:
    prompt: str
    files: tuple[Path, ...] = ()
    profile: str = "default"
    review_id: str | None = None
    dimensions: tuple[str, ...] = ()
    source_context: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ReviewResult:
    status: str
    review_id: str
    receipt_path: Path
    findings: tuple[Finding, ...]
    diagnostic: Diagnostic | None = None


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _finding_id(finding: Finding) -> str:
    identity = {
        "path": finding.path,
        "line": finding.line,
        "title": finding.title,
        "reproduction": finding.reproduction,
    }
    canonical = json.dumps(
        identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return "finding-" + _digest(canonical)[:24]


def finding_id(finding: Finding) -> str:
    """Return the stable identity used by the project journal and adapters."""
    return _finding_id(finding)


def _review_id(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def _execution_diagnostic(execution: BackendExecution) -> Diagnostic:
    details = execution.diagnostic.strip() if isinstance(execution.diagnostic, str) else ""
    if execution.exit_code == 124 or "timed out" in details.lower():
        return Diagnostic("timeout", "review transport timed out", retryable=True)
    if execution.exit_code != 0:
        return Diagnostic(
            "transport_unavailable",
            "review transport failed",
            retryable=True,
        )
    return Diagnostic("empty_response", "review transport returned no usable response")


def _cost_valid(cost: Any) -> bool:
    if cost is None:
        return True
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        return False
    try:
        return math.isfinite(cost) and cost >= 0
    except OverflowError:
        return False


def _response_metadata_valid(response: Any) -> bool:
    cost_valid = _cost_valid(response.cost_usd)
    counts_valid = all(
        value is None or (type(value) is int and value >= 0)
        for value in (response.duration_ms, response.input_tokens, response.output_tokens)
    )
    provider = response.provider
    provider_valid = provider is None or (isinstance(provider, str) and bool(provider.strip()))
    return cost_valid and counts_valid and provider_valid


def _reject_nonstandard_json_number(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _normalize_source_context(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("review source context must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("review source context must contain JSON-safe values") from error
    if not isinstance(normalized, dict):
        raise ValueError("review source context must be an object")
    if len(encoded.encode("utf-8")) > MAX_SOURCE_CONTEXT_BYTES:
        raise ValueError(f"review source context exceeds the {MAX_SOURCE_CONTEXT_BYTES} byte limit")
    return normalized


class ReviewClient:
    """Deep project review interface used by the CLI and integrations."""

    def __init__(
        self,
        project_dir: Path,
        config: ReviewConfig,
        transports: Mapping[str, ReviewTransport],
        *,
        project_id: str | None = None,
        origin_id: str | None = None,
    ) -> None:
        self.project_dir = project_dir.expanduser().resolve()
        self.config = config
        self.transports = transports
        self.review_root = self.project_dir / ".reviewctl" / "reviews"
        self._journal = ProjectJournal(
            self.project_dir / ".reviewctl" / "journal.jsonl",
            project_id=project_id,
            origin_id=origin_id,
        )

    @classmethod
    def from_project(
        cls,
        project_dir: Path,
        *,
        transports: Mapping[str, ReviewTransport] | None = None,
    ) -> ReviewClient:
        project_dir = project_dir.expanduser().resolve()
        config = load_config(project_dir)
        identity = ProjectIdentityStore(project_dir).ensure(config.project.project_id)
        if transports is None:
            from reviewctl.pi_transport import PiTransport

            transports = {"pi": PiTransport()}
        return cls(
            project_dir,
            config,
            transports,
            project_id=identity.project_id,
            origin_id=identity.origin_id,
        )

    def journal(self) -> ProjectJournal:
        return self._journal

    def findings(self, *, status: str | None = None) -> list[dict[str, Any]]:
        return self._journal.findings(status=status)

    def review(self, request: ReviewRequest) -> ReviewResult:
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            diagnostic = Diagnostic("invalid_request", "review prompt must not be empty")
            return ReviewResult(
                "invalid_request", request.review_id or "invalid", Path(), (), diagnostic
            )
        try:
            profile = self.config.profile(request.profile)
        except ConfigError as error:
            diagnostic = Diagnostic("config_invalid", str(error))
            return ReviewResult(
                "config_invalid", request.review_id or "invalid", Path(), (), diagnostic
            )
        try:
            request_dimensions = normalize_dimensions(request.dimensions, label="review dimensions")
        except ValueError as error:
            diagnostic = Diagnostic("invalid_request", str(error))
            return ReviewResult(
                "invalid_request", request.review_id or "invalid", Path(), (), diagnostic
            )
        dimensions = merge_dimensions(
            self.config.project.required_dimensions,
            profile.dimensions,
            request_dimensions,
        )
        if self.config.project.privacy_mode == "sensitive" and profile.execution != "local":
            diagnostic = Diagnostic(
                "privacy_denied",
                f"sensitive project cannot use remote profile {profile.name!r}",
                next="select a local profile or change the project privacy mode",
            )
            return ReviewResult(
                "privacy_denied", request.review_id or "invalid", Path(), (), diagnostic
            )
        if (
            self.config.project.privacy_mode == "private"
            and profile.execution == "remote"
            and profile.tools == "read-only"
            and any(route.transport == "pi" for route in profile.parsed_routes)
        ):
            diagnostic = Diagnostic(
                "privacy_denied",
                "private remote Pi profiles cannot use read-only tools",
                next="select tools=none or use a local profile",
            )
            return ReviewResult(
                "privacy_denied", request.review_id or "invalid", Path(), (), diagnostic
            )
        if not profile.routes:
            diagnostic = Diagnostic(
                "route_invalid",
                f"profile {profile.name!r} has no routes",
                next="configure a Pi route in reviewctl.toml",
            )
            return ReviewResult(
                "route_invalid", request.review_id or "invalid", Path(), (), diagnostic
            )
        if request.review_id is not None and (
            not isinstance(request.review_id, str)
            or not _REVIEW_ID.fullmatch(request.review_id.strip())
        ):
            diagnostic = Diagnostic(
                "invalid_request",
                "review id must contain only letters, numbers, dot, dash, or underscore",
                next="choose a simple review id without path separators",
            )
            return ReviewResult("invalid_request", "invalid", Path(), (), diagnostic)
        review_id = _review_id(request.review_id)
        attempt_root = self.review_root / review_id
        if attempt_root.exists():
            diagnostic = Diagnostic(
                "invalid_request",
                f"review id already exists: {review_id}",
                next="choose a new --review-id or omit it",
            )
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        try:
            source_context = _normalize_source_context(request.source_context)
        except ValueError as error:
            diagnostic = Diagnostic("invalid_request", str(error))
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        source_files: list[Path] = []
        source_contents: list[bytes] = []
        source_digests: dict[Path, str] = {}
        for requested_path in request.files:
            candidate = requested_path.expanduser()
            if not candidate.is_absolute():
                candidate = self.project_dir / candidate
            path = candidate.resolve()
            try:
                path.relative_to(self.project_dir)
            except ValueError:
                diagnostic = Diagnostic(
                    "privacy_denied",
                    f"review file is outside the project: {requested_path}",
                    next="select an explicit file below the project directory",
                )
                return ReviewResult("privacy_denied", review_id, Path(), (), diagnostic)
            if not path.is_file():
                diagnostic = Diagnostic(
                    "invalid_request",
                    f"review file does not exist: {requested_path}",
                    next="check the path and retry",
                )
                return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
            try:
                source_bytes = _read_source_bytes(path)
                if source_bytes is None or len(source_bytes) > MAX_SOURCE_BYTES:
                    diagnostic = Diagnostic(
                        "invalid_request",
                        f"review file exceeds the {MAX_SOURCE_BYTES} byte limit: {requested_path}",
                        next="select a smaller bounded file or split the review",
                    )
                    return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
                source_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError) as error:
                diagnostic = Diagnostic(
                    "invalid_request",
                    f"review file is not readable UTF-8 text: {requested_path}",
                    next="select a readable text file",
                )
                if isinstance(error, OSError):
                    diagnostic = Diagnostic(
                        "invalid_request",
                        f"review file could not be read: {requested_path}",
                        next="check the file permissions and retry",
                    )
                return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
            source_files.append(path)
            source_contents.append(source_bytes)
            source_digests[path] = _digest(source_bytes)
        if len({path.name for path in source_files}) != len(source_files):
            diagnostic = Diagnostic(
                "invalid_request",
                "review files must have unique basenames",
                next="select files with distinct names",
            )
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        artifacts = ArtifactStore(attempt_root)
        source_files_tuple = tuple(source_files)
        try:
            contract = get_contract(profile.response_contract)
            context = ContractContext(
                file_names=tuple(sorted({path.name for path in source_files_tuple}))
            )
            prepared = contract.prepare(context)
        except (KeyError, TypeError, ValueError) as error:
            diagnostic = Diagnostic(
                "contract_failed",
                f"could not prepare response contract: {error}",
            )
            receipt_path = self._write_receipt(
                artifacts,
                review_id=review_id,
                route="",
                status="contract_failed",
                diagnostic=diagnostic,
                dimensions=dimensions,
                source_context=source_context,
            )
            return ReviewResult("contract_failed", review_id, receipt_path, (), diagnostic)
        routes = tuple(
            route for route in profile.parsed_routes for _ in range(profile.max_attempts)
        )
        packet = {
            "promptDigest": _digest(request.prompt.encode()),
            "contractDigest": prepared.digest,
            "projectId": self.config.project.project_id,
            "originId": self._journal.origin_id,
            "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
            "dimensions": list(dimensions),
            "files": [
                {"name": path.name, "path": str(path), "sha256": source_digests[path]}
                for path in source_files_tuple
            ],
        }
        if source_context is not None:
            packet["sourceContext"] = source_context
        packet_bytes = json.dumps(packet, ensure_ascii=True, sort_keys=True).encode()
        packet_digest = _digest(packet_bytes)
        artifacts.write_bytes("packet.json", packet_bytes + b"\n")
        started_event: dict[str, Any] = {
            "type": "review_started",
            "reviewId": review_id,
            "route": f"{routes[0].transport}:{routes[0].model}",
            "packetDigest": packet_digest,
            "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
            "dimensions": list(dimensions),
        }
        if source_context is not None:
            started_event["sourceContext"] = source_context
        self._journal.append(started_event)
        attempts: list[dict[str, Any]] = []
        fallback_relationships: list[dict[str, Any]] = []
        partial_findings: list[Finding] = []
        last_diagnostic: Diagnostic | None = None
        last_usage: Any = None
        last_usage_route: str | None = None
        saw_partial = False

        def record_attempt(attempt: dict[str, Any]) -> None:
            attempts.append(attempt)
            self._journal.append(
                {
                    "type": "review_attempt",
                    "reviewId": review_id,
                    **attempt,
                }
            )

        for index, route in enumerate(routes, start=1):
            route_label = f"{route.transport}:{route.model}"
            transport = self.transports.get(route.transport)
            attempt_artifacts = ArtifactStore(attempt_root / f"attempt-{index:02d}")
            if transport is None:
                diagnostic = Diagnostic(
                    "transport_unavailable",
                    f"transport {route.transport!r} is not registered",
                    retryable=True,
                )
                record_attempt({"attempt": index, "route": route_label, "status": diagnostic.code})
                last_diagnostic = diagnostic
                if index < len(routes):
                    fallback_relationships.append(
                        {
                            "from": route_label,
                            "to": f"{routes[index].transport}:{routes[index].model}",
                            "reason": diagnostic.code,
                        }
                    )
                continue
            prompt = request.prompt + "\n\n" + prepared.output_instructions
            if partial_findings:
                prompt += (
                    "\n\nA prior bounded attempt was incomplete. Preserve these validated "
                    "findings if they remain relevant and return one complete response: "
                    + json.dumps([asdict(finding) for finding in partial_findings], sort_keys=True)
                )
            source_artifacts = ArtifactStore(attempt_artifacts.root / "source")
            transport_files_tuple = tuple(
                source_artifacts.write_bytes(path.name, contents)
                for path, contents in zip(source_files_tuple, source_contents, strict=True)
            )
            backend_request = BackendRequest(
                prompt=prompt,
                model=route.model,
                response_contract=profile.response_contract,
                files=transport_files_tuple,
                attempt_dir=attempt_root / f"attempt-{index:02d}",
                timeout_seconds=profile.timeout_seconds,
                max_output_tokens=profile.max_output_tokens or 0,
                source_class=self.config.project.privacy_mode,
                source_roots=(self.project_dir,),
                provider_preferences=None,
                tools=profile.tools,
            )
            try:
                execution = transport.execute(backend_request)
            except OSError, UnicodeError, ValueError:
                diagnostic = Diagnostic(
                    "transport_unavailable", "review transport failed", retryable=True
                )
                record_attempt(
                    {
                        "attempt": index,
                        "route": route_label,
                        "status": diagnostic.code,
                    }
                )
                last_diagnostic = diagnostic
                if index < len(routes):
                    fallback_relationships.append(
                        {
                            "from": route_label,
                            "to": f"{routes[index].transport}:{routes[index].model}",
                            "reason": diagnostic.code,
                        }
                    )
                continue
            if (
                execution.exit_code != 0
                or execution.response is None
                or not execution.response.response.strip()
            ):
                diagnostic = _execution_diagnostic(execution)
                record_attempt({"attempt": index, "route": route_label, "status": diagnostic.code})
                last_diagnostic = diagnostic
                if index < len(routes):
                    fallback_relationships.append(
                        {
                            "from": route_label,
                            "to": f"{routes[index].transport}:{routes[index].model}",
                            "reason": diagnostic.code,
                        }
                    )
                continue
            if (
                not isinstance(execution.response.conversation_id, str)
                or not execution.response.conversation_id.strip()
                or execution.response.model != route.model
                or not _response_metadata_valid(execution.response)
            ):
                diagnostic = Diagnostic(
                    "transport_unavailable",
                    "review transport returned invalid response identity or usage metadata",
                    retryable=True,
                )
                record_attempt({"attempt": index, "route": route_label, "status": diagnostic.code})
                last_diagnostic = diagnostic
                if index < len(routes):
                    fallback_relationships.append(
                        {
                            "from": route_label,
                            "to": f"{routes[index].transport}:{routes[index].model}",
                            "reason": diagnostic.code,
                        }
                    )
                continue
            last_usage = execution.response
            last_usage_route = route_label
            attempt_artifacts.write_text("response.md", execution.response.response)
            try:
                evaluation = contract.evaluate(
                    execution.response.response,
                    prepared,
                    context,
                    evidence=EvaluationContext(packet_digest=packet_digest),
                )
            except (TypeError, ValueError) as error:
                evaluation = None
                diagnostic = Diagnostic("contract_failed", str(error))
            else:
                diagnostic = Diagnostic(
                    "contract_failed",
                    "review response did not satisfy the selected contract",
                )
            if evaluation is not None and evaluation.status is EvaluationStatus.COMPLETE:
                try:
                    current_findings = tuple(
                        Finding.from_value(value)
                        for value in evaluation.value.get("findings", [])
                        if isinstance(value, Mapping)
                    )
                except (KeyError, TypeError, ValueError) as error:
                    evaluation = None
                    diagnostic = Diagnostic("contract_failed", str(error))
                else:
                    findings = self._merge_findings(partial_findings, current_findings)
                    record_attempt({"attempt": index, "route": route_label, "status": "accepted"})
                    self._record_findings(review_id, findings, dimensions=dimensions)
                    receipt_path = self._write_receipt(
                        artifacts,
                        review_id=review_id,
                        route=route_label,
                        status="accepted",
                        packet_digest=packet_digest,
                        usage=execution.response,
                        findings=[self._finding_payload(finding) for finding in findings],
                        dimensions=dimensions,
                        attempts=attempts,
                        fallback_relationships=fallback_relationships,
                        source_context=source_context,
                    )
                    self._journal.append(
                        {
                            "type": "review_finished",
                            "reviewId": review_id,
                            "status": "accepted",
                            "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
                            "dimensions": list(dimensions),
                        }
                    )
                    return ReviewResult("accepted", review_id, receipt_path, findings)
            if evaluation is not None and evaluation.status is EvaluationStatus.INCOMPLETE:
                try:
                    retained = tuple(
                        Finding.from_value(fragment.value)
                        for fragment in evaluation.valid_fragments
                        if isinstance(fragment.value, Mapping)
                    )
                except (KeyError, TypeError, ValueError) as error:
                    evaluation = None
                    diagnostic = Diagnostic("contract_failed", str(error))
                    status = "contract_failed"
                else:
                    saw_partial = True
                    partial_findings = list(self._merge_findings(partial_findings, retained))
                    diagnostic = Diagnostic(
                        "contract_failed",
                        "review response was incomplete; validated findings were retained",
                        retryable=True,
                    )
                    status = "partial"
            else:
                status = "contract_failed"
            record_attempt(
                {
                    "attempt": index,
                    "route": route_label,
                    "status": status,
                    "violations": list(evaluation.violations) if evaluation is not None else [],
                }
            )
            last_diagnostic = diagnostic
            if index < len(routes):
                fallback_relationships.append(
                    {
                        "from": route_label,
                        "to": f"{routes[index].transport}:{routes[index].model}",
                        "reason": status,
                    }
                )
        if saw_partial:
            status = "partial"
            diagnostic = Diagnostic(
                "contract_failed",
                "all review attempts were incomplete; validated findings were retained",
                retryable=True,
                next="run another completion review with a fallback route",
            )
        else:
            status = last_diagnostic.code if last_diagnostic is not None else "route_invalid"
            diagnostic = last_diagnostic or Diagnostic(
                "route_invalid", f"profile {profile.name!r} has no routes"
            )
        findings = self._merge_findings(partial_findings)
        self._record_findings(review_id, findings, dimensions=dimensions)
        receipt_path = self._write_receipt(
            artifacts,
            review_id=review_id,
            route=last_usage_route or (attempts[-1]["route"] if attempts else ""),
            status=status,
            packet_digest=packet_digest,
            diagnostic=diagnostic,
            usage=last_usage,
            findings=[self._finding_payload(finding) for finding in findings],
            dimensions=dimensions,
            attempts=attempts,
            fallback_relationships=fallback_relationships,
            source_context=source_context,
        )
        self._journal.append(
            {
                "type": "review_finished",
                "reviewId": review_id,
                "status": status,
                "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
                "dimensions": list(dimensions),
            }
        )
        return ReviewResult(status, review_id, receipt_path, findings, diagnostic)

    @staticmethod
    def _merge_findings(*groups: Sequence[Finding]) -> tuple[Finding, ...]:
        merged: list[Finding] = []
        positions: dict[str, int] = {}
        for group in groups:
            for finding in group:
                identity = _finding_id(finding)
                if identity in positions:
                    merged[positions[identity]] = finding
                else:
                    positions[identity] = len(merged)
                    merged.append(finding)
        return tuple(merged)

    def _record_findings(
        self, review_id: str, findings: Sequence[Finding], *, dimensions: Sequence[str]
    ) -> None:
        for finding in findings:
            self._journal.append(
                {
                    "type": "finding_observed",
                    "reviewId": review_id,
                    "findingId": _finding_id(finding),
                    "status": "open",
                    "path": finding.path,
                    "line": finding.line,
                    "message": finding.title,
                    "severity": finding.severity,
                    "evidence": finding.evidence,
                    "reproduction": finding.reproduction,
                    "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
                    "dimensions": list(dimensions),
                }
            )

    @staticmethod
    def _finding_payload(finding: Finding) -> dict[str, Any]:
        return {
            **asdict(finding),
            "findingId": _finding_id(finding),
        }

    def _write_receipt(
        self,
        artifacts: ArtifactStore,
        *,
        review_id: str,
        route: str,
        status: str,
        packet_digest: str | None = None,
        diagnostic: Diagnostic | None = None,
        usage: Any = None,
        findings: Sequence[dict[str, Any]] = (),
        violations: Sequence[str] = (),
        attempts: Sequence[dict[str, Any]] = (),
        fallback_relationships: Sequence[dict[str, Any]] = (),
        dimensions: Sequence[str] = (),
        source_context: Mapping[str, Any] | None = None,
    ) -> Path:
        receipt: dict[str, Any] = {
            "reviewId": review_id,
            "route": route,
            "status": status,
            "configDigest": self.config.digest,
            "projectId": self.config.project.project_id,
            "originId": self._journal.origin_id,
            "journalSequence": self._journal.head_sequence(),
            "privacyMode": self.config.project.privacy_mode,
            "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
            "dimensions": list(dimensions),
            "dimensionCoverage": {
                "requested": list(dimensions),
                "observed": [],
                "unresolved": list(dimensions),
            },
            "at": _now(),
            "findings": list(findings),
            "violations": list(violations),
            "attempts": list(attempts),
            "fallbackRelationships": list(fallback_relationships),
        }
        if source_context is not None:
            receipt["sourceContext"] = dict(source_context)
        if packet_digest is not None:
            receipt["packetDigest"] = packet_digest
        if diagnostic is not None:
            receipt["diagnostic"] = diagnostic.to_dict()
        if usage is not None:
            receipt["usage"] = {
                "costUsd": usage.cost_usd,
                "durationMs": usage.duration_ms,
                "inputTokens": usage.input_tokens,
                "outputTokens": usage.output_tokens,
                "model": usage.model,
                "provider": usage.provider,
            }
        unsigned = json.dumps(
            receipt,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        receipt["sha256"] = _digest(unsigned)
        contents = json.dumps(
            receipt, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False
        ).encode()
        return artifacts.write_bytes("receipt.json", contents + b"\n")


def verify_project_receipt(path: Path) -> Diagnostic | None:
    """Verify the digest of a receipt produced by the project API."""
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json_number,
            parse_float=_parse_finite_json_float,
        )
    except (OSError, UnicodeError, ValueError) as error:
        return Diagnostic("receipt_invalid", f"could not read receipt: {error}")
    if not isinstance(value, dict) or not isinstance(value.get("sha256"), str):
        return Diagnostic("receipt_invalid", "receipt is missing its sha256 digest")
    recorded = value["sha256"]
    unsigned = {key: item for key, item in value.items() if key != "sha256"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if recorded != _digest(canonical):
        return Diagnostic("receipt_invalid", "receipt digest does not match its contents")
    return None
