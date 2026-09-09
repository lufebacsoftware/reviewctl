"""Small public API for project-scoped reviews."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

from reviewctl.artifacts import ArtifactStore
from reviewctl.backends import BackendExecution, BackendRequest
from reviewctl.config import ReviewConfig, load_config
from reviewctl.contracts import (
    ContractContext,
    EvaluationContext,
    EvaluationStatus,
    exact_json_object,
    get_contract,
)
from reviewctl.dimensions import DIMENSION_SCHEMA_VERSION, merge_dimensions, normalize_dimensions
from reviewctl.errors import ConfigError, Diagnostic
from reviewctl.filesystem import (
    confined_directory_descriptor,
    confined_relative_regular_descriptor,
    read_confined_text,
)
from reviewctl.identity import ProjectIdentityStore
from reviewctl.journal import ProjectJournal


class ReviewTransport(Protocol):
    def execute(self, request: BackendRequest) -> BackendExecution: ...


_REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PROJECT_CHECKPOINT_KIND = "project-review-checkpoint"
PROJECT_CHECKPOINT_SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_FILES = 100
MAX_SOURCE_SET_BYTES = 8 * 1024 * 1024
MAX_SOURCE_CONTEXT_BYTES = 32 * 1024
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


def _read_source_bytes(
    path: Path,
    *,
    project_dir: Path | None = None,
    expected_root_identity: tuple[int, int] | None = None,
) -> bytes | None:
    root = project_dir or path.parent
    relative = path.relative_to(root)
    if not _OPEN_SUPPORTS_DIR_FD:
        raise OSError("this platform cannot open review sources without following symlinks")
    try:
        if expected_root_identity is None:
            root_metadata = os.stat(root, follow_symlinks=False)
            expected_root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        with confined_directory_descriptor(root) as root_descriptor:
            root_metadata = os.fstat(root_descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != expected_root_identity:
                raise OSError("review source root identity changed")
            with confined_relative_regular_descriptor(
                root_descriptor, relative, os.O_RDONLY
            ) as descriptor:
                source = os.fstat(descriptor)
                if source.st_size > MAX_SOURCE_BYTES:
                    return None
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    return stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        if "platform cannot confine" in str(error):
            raise OSError(
                "this platform cannot open review sources without following symlinks"
            ) from error
        raise


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
    source_names: tuple[str, ...] = ()
    source_root: Path | None = None


@dataclass(frozen=True)
class ReviewResult:
    status: str
    review_id: str
    receipt_path: Path
    findings: tuple[Finding, ...]
    diagnostic: Diagnostic | None = None
    receipt_sha256: str | None = None


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _logical_source_name(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
        or "\\" in value
    ):
        raise ValueError("review source names must be printable project-relative paths")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or candidate.as_posix() != value
        or any(part in {".", ".."} for part in candidate.parts)
    ):
        raise ValueError("review source names must be normalized project-relative paths")
    return value


def _model_file_name(source_name: str) -> str:
    return quote(source_name, safe="")


def _restore_source_paths(
    findings: Sequence[Finding], source_paths: Mapping[str, str]
) -> tuple[Finding, ...]:
    return tuple(
        replace(finding, path=source_paths.get(finding.path, finding.path)) for finding in findings
    )


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
        expected_project_identity: tuple[int, int] | None = None,
    ) -> None:
        self.project_dir = Path(os.path.abspath(project_dir.expanduser()))
        try:
            with confined_directory_descriptor(
                self.project_dir, expected_identity=expected_project_identity
            ) as project_descriptor:
                project_metadata = os.fstat(project_descriptor)
                self._project_identity = (project_metadata.st_dev, project_metadata.st_ino)
        except OSError as error:
            raise ValueError(f"project directory identity changed: {self.project_dir}") from error
        self.config = config
        self.transports = transports
        self.review_root = self.project_dir / ".reviewctl" / "reviews"
        self._journal = ProjectJournal(
            self.project_dir / ".reviewctl" / "journal.jsonl",
            project_id=project_id,
            origin_id=origin_id,
            expected_project_identity=self._project_identity,
        )

    @classmethod
    def from_project(
        cls,
        project_dir: Path,
        *,
        transports: Mapping[str, ReviewTransport] | None = None,
    ) -> ReviewClient:
        project_dir = Path(os.path.abspath(project_dir.expanduser()))
        try:
            with confined_directory_descriptor(project_dir) as project_descriptor:
                project_metadata = os.fstat(project_descriptor)
                project_identity = (project_metadata.st_dev, project_metadata.st_ino)
                config = load_config(project_dir, expected_project_identity=project_identity)
                identity = ProjectIdentityStore(
                    project_dir, expected_project_identity=project_identity
                ).ensure(config.project.project_id)
                if transports is None:
                    from reviewctl.codex_project_transport import CodexProjectTransport
                    from reviewctl.pi_transport import PiTransport

                    transports = {
                        "codex": CodexProjectTransport(project_dir),
                        "pi": PiTransport(),
                    }
                return cls(
                    project_dir,
                    config,
                    transports,
                    project_id=identity.project_id,
                    origin_id=identity.origin_id,
                    expected_project_identity=project_identity,
                )
        except FileNotFoundError as error:
            raise ValueError(
                f"review requires an existing project directory: {project_dir}"
            ) from error
        except OSError as error:
            raise ValueError(f"review requires a safe project directory: {project_dir}") from error

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
        if len(request.files) > MAX_SOURCE_FILES:
            diagnostic = Diagnostic(
                "invalid_request",
                f"review source set exceeds the {MAX_SOURCE_FILES} file limit",
                next="select a smaller bounded source set or split the review",
            )
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        if request.source_names and len(request.source_names) != len(request.files):
            diagnostic = Diagnostic(
                "invalid_request",
                "review source names must align one-to-one with review files",
                next="supply one project-relative source name for every file",
            )
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        try:
            requested_source_names = tuple(
                _logical_source_name(value) for value in request.source_names
            )
        except ValueError as error:
            diagnostic = Diagnostic("invalid_request", str(error))
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        source_files: list[Path] = []
        source_contents: list[bytes] = []
        source_names: list[str] = []
        source_digests: dict[Path, str] = {}
        source_set_bytes = 0
        source_root = self.project_dir.resolve()
        source_root_identity = self._project_identity
        if request.source_root is not None:
            source_root = Path(os.path.abspath(request.source_root.expanduser())).resolve()
            try:
                with confined_directory_descriptor(source_root) as source_descriptor:
                    source_metadata = os.fstat(source_descriptor)
                    source_root_identity = (source_metadata.st_dev, source_metadata.st_ino)
            except OSError:
                diagnostic = Diagnostic(
                    "privacy_denied",
                    f"review source root is not a safe directory: {request.source_root}",
                    next="select a confined temporary source root",
                )
                return ReviewResult("privacy_denied", review_id, Path(), (), diagnostic)
        for requested_path in request.files:
            candidate = requested_path.expanduser()
            if not candidate.is_absolute():
                candidate = source_root / candidate
            path = candidate.resolve()
            try:
                path.relative_to(source_root)
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
                source_bytes = _read_source_bytes(
                    path,
                    project_dir=source_root,
                    expected_root_identity=source_root_identity,
                )
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
            source_set_bytes += len(source_bytes)
            if source_set_bytes > MAX_SOURCE_SET_BYTES:
                diagnostic = Diagnostic(
                    "invalid_request",
                    "review source set exceeds the "
                    f"{MAX_SOURCE_SET_BYTES} aggregate source byte limit",
                    next="select a smaller bounded source set or split the review",
                )
                return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
            source_files.append(path)
            source_contents.append(source_bytes)
            source_name = (
                requested_source_names[len(source_files) - 1]
                if requested_source_names
                else _logical_source_name(path.relative_to(source_root).as_posix())
            )
            source_names.append(_model_file_name(source_name))
            source_digests[path] = _digest(source_bytes)
        if len(set(source_names)) != len(source_names):
            diagnostic = Diagnostic(
                "invalid_request",
                "review files must identify unique project-relative paths",
                next="remove duplicate source paths",
            )
            return ReviewResult("invalid_request", review_id, Path(), (), diagnostic)
        artifacts = ArtifactStore(attempt_root)
        source_files_tuple = tuple(source_files)
        source_paths_by_name = {
            name: (
                requested_source_names[index]
                if requested_source_names
                else path.relative_to(source_root).as_posix()
            )
            for index, (name, path) in enumerate(zip(source_names, source_files_tuple, strict=True))
        }
        routes = tuple(
            route for route in profile.parsed_routes for _ in range(profile.max_attempts)
        )
        try:
            contract = get_contract(profile.response_contract)
            # Bind the packet to the first attempt's contract.  Codex adds a
            # reviewedFiles declaration for its source-root sandbox; later
            # fallback attempts record their own contract digest below.
            context = ContractContext(
                file_names=tuple(sorted(source_names)),
                review_declaration_required=routes[0].transport == "codex",
            )
            prepared = contract.prepare(context)
        except (KeyError, TypeError, ValueError) as error:
            diagnostic = Diagnostic(
                "contract_failed",
                f"could not prepare response contract: {error}",
            )
            receipt_path, receipt_sha256 = self._write_receipt(
                artifacts,
                review_id=review_id,
                route="",
                status="contract_failed",
                diagnostic=diagnostic,
                dimensions=dimensions,
                source_context=source_context,
            )
            return ReviewResult(
                "contract_failed", review_id, receipt_path, (), diagnostic, receipt_sha256
            )
        packet = {
            "promptDigest": _digest(request.prompt.encode()),
            "contractDigest": prepared.digest,
            "projectId": self.config.project.project_id,
            "originId": self._journal.origin_id,
            "dimensionSchemaVersion": DIMENSION_SCHEMA_VERSION,
            "dimensions": list(dimensions),
            "files": [
                {
                    "name": name,
                    "path": source_paths_by_name[name],
                    "sha256": source_digests[path],
                }
                for name, path in zip(source_names, source_files_tuple, strict=True)
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
            attempt.setdefault("contractDigest", attempt_prepared.digest)
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
            attempt_context = replace(
                context,
                review_declaration_required=route.transport == "codex",
            )
            attempt_prepared = contract.prepare(attempt_context)
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
            prompt = request.prompt + "\n\n" + attempt_prepared.output_instructions
            if partial_findings:
                prompt += (
                    "\n\nA prior bounded attempt was incomplete. Preserve these validated "
                    "findings if they remain relevant and return one complete response: "
                    + json.dumps([asdict(finding) for finding in partial_findings], sort_keys=True)
                )
            temporary_root: Path | None = None
            try:
                with tempfile.TemporaryDirectory(prefix="reviewctl-project-source-") as directory:
                    temporary_root = Path(directory).resolve()
                    source_artifacts = ArtifactStore(temporary_root)
                    transport_files_tuple = tuple(
                        source_artifacts.write_bytes(name, contents)
                        for name, contents in zip(source_names, source_contents, strict=True)
                    )
                    transport_source_roots = (self.project_dir,)
                    if source_root != self.project_dir.resolve():
                        transport_source_roots += (source_root,)
                    transport_source_roots += (temporary_root,)
                    backend_request = BackendRequest(
                        prompt=prompt,
                        model=route.model,
                        response_contract=profile.response_contract,
                        files=transport_files_tuple,
                        attempt_dir=attempt_artifacts.root,
                        timeout_seconds=profile.timeout_seconds,
                        max_output_tokens=profile.max_output_tokens or 0,
                        source_class=self.config.project.privacy_mode,
                        source_roots=transport_source_roots,
                        provider_preferences=None,
                        tools=profile.tools,
                        thinking=profile.thinking,
                    )
                    execution = transport.execute(backend_request)
            except OSError, UnicodeError, ValueError:
                if temporary_root is not None and temporary_root.exists():
                    try:
                        shutil.rmtree(temporary_root)
                    except OSError as cleanup_error:
                        raise RuntimeError("temporary source cleanup failed") from cleanup_error
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
                    attempt_prepared,
                    attempt_context,
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
                    findings = _restore_source_paths(
                        self._merge_findings(partial_findings, current_findings),
                        source_paths_by_name,
                    )
                    record_attempt({"attempt": index, "route": route_label, "status": "accepted"})
                    self._record_findings(review_id, findings, dimensions=dimensions)
                    receipt_path, receipt_sha256 = self._write_receipt(
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
                    return ReviewResult(
                        "accepted", review_id, receipt_path, findings, None, receipt_sha256
                    )
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
        findings = _restore_source_paths(
            self._merge_findings(partial_findings), source_paths_by_name
        )
        self._record_findings(review_id, findings, dimensions=dimensions)
        receipt_path, receipt_sha256 = self._write_receipt(
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
        return ReviewResult(status, review_id, receipt_path, findings, diagnostic, receipt_sha256)

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
    ) -> tuple[Path, str]:
        receipt: dict[str, Any] = {
            "artifactKind": PROJECT_CHECKPOINT_KIND,
            "projectCheckpointSchemaVersion": PROJECT_CHECKPOINT_SCHEMA_VERSION,
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
        receipt_sha256 = _digest(unsigned)
        receipt["sha256"] = receipt_sha256
        contents = json.dumps(
            receipt, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False
        ).encode()
        return artifacts.write_bytes("receipt.json", contents + b"\n"), receipt_sha256


def verify_project_receipt(path: Path, *, expected_sha256: str | None = None) -> Diagnostic | None:
    """Check project checkpoint integrity; this is not canonical receipt verification."""
    try:
        value = json.loads(
            read_confined_text(path),
            object_pairs_hook=exact_json_object,
            parse_constant=_reject_nonstandard_json_number,
            parse_float=_parse_finite_json_float,
        )
    except (OSError, UnicodeError, ValueError) as error:
        return Diagnostic("receipt_invalid", f"could not read receipt: {error}")
    if not isinstance(value, dict) or not isinstance(value.get("sha256"), str):
        return Diagnostic("receipt_invalid", "receipt is missing its sha256 digest")
    marker_present = "artifactKind" in value or "projectCheckpointSchemaVersion" in value
    if marker_present and (
        value.get("artifactKind") != PROJECT_CHECKPOINT_KIND
        or type(value.get("projectCheckpointSchemaVersion")) is not int
        or value["projectCheckpointSchemaVersion"] != PROJECT_CHECKPOINT_SCHEMA_VERSION
    ):
        return Diagnostic("receipt_invalid", "project checkpoint marker is invalid")
    recorded = value["sha256"]
    if expected_sha256 is not None and recorded != expected_sha256:
        return Diagnostic(
            "receipt_invalid", "receipt digest does not match the accepted review result"
        )
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
