"""Small append-only project journal and finding projection."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None

from reviewctl.dimensions import normalize_dimensions
from reviewctl.errors import Diagnostic, JournalOperationError
from reviewctl.identity import confine_project_state_path

FINDING_STATUSES = frozenset({"open", "disputed", "fixed", "verified", "dismissed"})
FINDING_TRANSITIONS = {
    "open": frozenset({"disputed", "fixed", "dismissed"}),
    "disputed": frozenset({"open", "fixed", "dismissed"}),
    "fixed": frozenset({"open", "disputed", "verified", "dismissed"}),
    "verified": frozenset({"open", "dismissed"}),
    "dismissed": frozenset({"open"}),
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _event_digest(event: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in event.items() if key != "eventSha256"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


class ProjectJournal:
    """Append JSON events and rebuild simple finding views from them."""

    def __init__(
        self,
        path: Path,
        *,
        project_id: str | None = None,
        origin_id: str | None = None,
    ) -> None:
        if (project_id is None) != (origin_id is None):
            raise ValueError("project_id and origin_id must be provided together")
        self.path = confine_project_state_path(path)
        self.project_id = project_id
        self.origin_id = origin_id
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("journal event requires a string type")
        normalized = dict(event)
        self._validate_event(normalized)
        normalized.setdefault("eventId", secrets.token_hex(16))
        normalized.setdefault("at", _now())
        normalized.setdefault("reviewId", "")
        if not isinstance(normalized["eventId"], str) or not isinstance(
            normalized["reviewId"], str
        ):
            raise ValueError("journal event identifiers must be strings")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with self._exclusive_lock(descriptor):
                events, diagnostic = self._read_descriptor(descriptor)
                if diagnostic is not None:
                    raise JournalOperationError(diagnostic)
                violations = self._verify_events(events)
                if violations:
                    raise JournalOperationError(
                        Diagnostic("journal_corrupt", "; ".join(violations))
                    )
                self._validate_event(normalized, events=events)
                normalized = self._with_envelope(normalized, events)
                original_size = os.fstat(descriptor).st_size
                line = json.dumps(
                    normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                )
                remaining = memoryview((line + "\n").encode("utf-8"))
                try:
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written == 0:
                            raise OSError(f"could not finish appending journal {self.path}")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                except BaseException:
                    try:
                        os.ftruncate(descriptor, original_size)
                        os.fsync(descriptor)
                    except BaseException:
                        pass
                    raise
        finally:
            primary = sys.exc_info()[1]
            if primary is None:
                os.close(descriptor)
            else:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
        return normalized

    @contextmanager
    def _exclusive_lock(self, descriptor: int):
        if fcntl is None:
            raise JournalOperationError(
                Diagnostic(
                    "journal_unavailable",
                    "this platform has no supported journal lock primitive",
                    retryable=True,
                    next="run reviewctl on a POSIX filesystem with advisory locking",
                )
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise JournalOperationError(
                Diagnostic(
                    "journal_unavailable",
                    f"could not lock the project journal: {error}",
                    retryable=True,
                    next="check filesystem locking and retry",
                )
            ) from error
        try:
            yield
        finally:
            primary = sys.exc_info()[1]
            if primary is None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except BaseException:
                    pass

    def _with_envelope(self, event: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        identities = self._journal_identity(events)
        if (
            identities[0] is not None
            and self.project_id is not None
            and identities[0] != self.project_id
        ) or (
            identities[1] is not None
            and self.origin_id is not None
            and identities[1] != self.origin_id
        ):
            raise JournalOperationError(
                Diagnostic(
                    "journal_corrupt",
                    "configured journal identity does not match the existing journal head",
                    next="use the journal's project and origin identity or migrate explicitly",
                )
            )
        project_id = self.project_id or identities[0]
        origin_id = self.origin_id or identities[1]
        if project_id is None or origin_id is None:
            return event
        versioned = {
            key: value
            for key, value in event.items()
            if key
            not in {
                "schemaVersion",
                "projectId",
                "originId",
                "sequence",
                "previousEventSha256",
                "eventSha256",
            }
        }
        versioned.update(
            {
                "schemaVersion": 1,
                "projectId": project_id,
                "originId": origin_id,
            }
        )
        versioned_events = [item for item in events if item.get("schemaVersion") == 1]
        previous = events[-1] if events else None
        if versioned_events:
            sequence = int(versioned_events[-1]["sequence"]) + 1
            previous_digest = versioned_events[-1]["eventSha256"]
        else:
            sequence = 1
            previous_digest = _event_digest(previous) if previous is not None else None
        versioned["sequence"] = sequence
        versioned["previousEventSha256"] = previous_digest
        versioned["eventSha256"] = _event_digest(versioned)
        return versioned

    def _journal_identity(self, events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        for event in events:
            if event.get("schemaVersion") == 1:
                return event.get("projectId"), event.get("originId")
        return self.project_id, self.origin_id

    def _validate_event(
        self, event: dict[str, Any], *, events: list[dict[str, Any]] | None = None
    ) -> None:
        event_type = event["type"]
        if event_type in {"finding", "finding_observed"}:
            status = event.get("status", "open")
            if status not in FINDING_STATUSES:
                raise ValueError(f"unsupported finding status: {status}")
            return
        if event_type != "finding_status_changed":
            return
        finding_id = event.get("findingId")
        source = event.get("from")
        target = event.get("to")
        if not isinstance(finding_id, str) or not finding_id:
            raise ValueError("finding status change requires findingId")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("finding status change requires from and to statuses")
        if source not in FINDING_STATUSES or target not in FINDING_STATUSES:
            raise ValueError("unsupported finding status transition")
        if events is None:
            current = self.finding(finding_id)
        else:
            current = next(
                (
                    finding
                    for finding in self._project(events)
                    if finding.get("findingId") == finding_id
                ),
                None,
            )
        if current is None:
            raise ValueError(f"finding not found: {finding_id}")
        if current.get("status") != source or target not in FINDING_TRANSITIONS[source]:
            raise ValueError(f"invalid finding status transition: {source} -> {target}")

    def append_status_change(
        self, finding_id: str, status: str, *, reason: str = ""
    ) -> dict[str, Any]:
        current = self.finding(finding_id)
        if current is None:
            raise JournalOperationError(
                Diagnostic(
                    "invalid_request",
                    f"finding not found: {finding_id}",
                    next="check the finding id returned by reviewctl findings",
                )
            )
        if status not in FINDING_STATUSES:
            raise JournalOperationError(
                Diagnostic(
                    "invalid_request",
                    f"unsupported finding status: {status}",
                    next="choose open, disputed, fixed, verified, or dismissed",
                )
            )
        source = current["status"]
        if status not in FINDING_TRANSITIONS[source]:
            raise JournalOperationError(
                Diagnostic(
                    "invalid_request",
                    f"invalid finding status transition: {source} -> {status}",
                    next="follow the documented finding lifecycle",
                )
            )
        event: dict[str, Any] = {
            "type": "finding_status_changed",
            "findingId": finding_id,
            "from": source,
            "to": status,
        }
        if reason.strip():
            event["reason"] = reason.strip()
        return self.append(event)

    def read_with_diagnostic(self) -> tuple[list[dict[str, Any]], Diagnostic | None]:
        if not self.path.is_file():
            return [], None
        try:
            raw = self.path.read_bytes()
        except (OSError, UnicodeDecodeError) as error:
            return [], Diagnostic("journal_corrupt", f"could not read journal: {error}")
        events, diagnostic = self._parse_bytes(raw)
        if diagnostic is not None:
            return events, diagnostic
        violations = self._verify_events(events)
        if violations:
            return events, Diagnostic("journal_corrupt", "; ".join(violations))
        return events, None

    def _read_descriptor(self, descriptor: int) -> tuple[list[dict[str, Any]], Diagnostic | None]:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            remaining = os.fstat(descriptor).st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    return [], Diagnostic("journal_corrupt", "could not read the complete journal")
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError as error:
            return [], Diagnostic("journal_corrupt", f"could not read journal: {error}")
        return self._parse_bytes(raw)

    @staticmethod
    def _parse_bytes(raw: bytes) -> tuple[list[dict[str, Any]], Diagnostic | None]:
        events: list[dict[str, Any]] = []
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            return [], Diagnostic("journal_corrupt", f"could not decode journal: {error}")
        for index, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                return events, Diagnostic(
                    "journal_corrupt",
                    f"invalid journal event at line {index}: {error.msg}",
                )
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                return events, Diagnostic(
                    "journal_corrupt", f"journal event at line {index} is not an object"
                )
            events.append(event)
        return events, None

    def _verify_events(self, events: list[dict[str, Any]]) -> list[str]:
        violations: list[str] = []
        expected_project, expected_origin = self.project_id, self.origin_id
        legacy_prefix = True
        versioned_count = 0
        previous_versioned: dict[str, Any] | None = None
        previous_event: dict[str, Any] | None = None
        for index, event in enumerate(events, start=1):
            if "schemaVersion" not in event:
                if not legacy_prefix:
                    violations.append(f"legacy event after versioned event at line {index}")
                previous_event = event
                continue
            legacy_prefix = False
            if expected_project is None:
                expected_project = event.get("projectId")
            if expected_origin is None:
                expected_origin = event.get("originId")
            if event.get("schemaVersion") != 1:
                violations.append(f"unsupported schema version at line {index}")
            if event.get("projectId") != expected_project:
                violations.append(f"project identity mismatch at line {index}")
            if event.get("originId") != expected_origin:
                violations.append(f"origin identity mismatch at line {index}")
            sequence = event.get("sequence")
            expected_sequence = versioned_count + 1
            if sequence != expected_sequence:
                violations.append(
                    "sequence mismatch at line "
                    f"{index}: expected {expected_sequence}, got {sequence}"
                )
            expected_previous = (
                previous_versioned.get("eventSha256")
                if previous_versioned is not None
                else (_event_digest(previous_event) if previous_event is not None else None)
            )
            if event.get("previousEventSha256") != expected_previous:
                violations.append(f"previous event digest mismatch at line {index}")
            recorded_digest = event.get("eventSha256")
            if not isinstance(recorded_digest, str) or recorded_digest != _event_digest(event):
                violations.append(f"event digest mismatch at line {index}")
            versioned_count += 1
            previous_versioned = event
            previous_event = event
        return violations

    def verify(self) -> list[str]:
        """Return structural journal violations without changing journal bytes."""
        if not self.path.is_file():
            return []
        try:
            events, diagnostic = self._parse_bytes(self.path.read_bytes())
        except OSError as error:
            return [f"could not read journal: {error}"]
        violations = self._verify_events(events)
        if diagnostic is not None:
            violations.insert(0, diagnostic.message)
        return violations

    def head_sequence(self) -> int:
        events, diagnostic = self.read_with_diagnostic()
        if diagnostic is not None:
            raise JournalOperationError(diagnostic)
        versioned = [event for event in events if event.get("schemaVersion") == 1]
        return int(versioned[-1]["sequence"]) if versioned else 0

    def compatibility(self) -> str:
        events, diagnostic = self.read_with_diagnostic()
        if diagnostic is not None and not events:
            return "invalid"
        if any(event.get("schemaVersion") == 1 for event in events):
            return (
                "legacy-prefix"
                if any(event.get("schemaVersion") != 1 for event in events)
                else "versioned"
            )
        return "legacy" if events else "empty"

    def events(self) -> list[dict[str, Any]]:
        events, _diagnostic = self.read_with_diagnostic()
        return events

    @staticmethod
    def _project(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        projected: dict[str, dict[str, Any]] = {}
        for event in events:
            event_type = event.get("type")
            if event_type in {"finding", "finding_observed"}:
                finding_id = event.get("findingId")
                if not isinstance(finding_id, str) or not finding_id:
                    continue
                status = event.get("status", "open")
                if status not in FINDING_STATUSES:
                    continue
                current = projected.get(finding_id)
                if current is None:
                    current = {
                        key: value
                        for key, value in event.items()
                        if key not in {"eventId", "at", "type"}
                    }
                    current["type"] = "finding"
                    current["status"] = status
                    current["firstReviewId"] = event.get("reviewId", "")
                    current["lastReviewId"] = event.get("reviewId", "")
                    current["firstObservedAt"] = event.get("at")
                    current["lastObservedAt"] = event.get("at")
                    current["observations"] = 1
                    current["dimensions"] = ProjectJournal._event_dimensions(event)
                    current["observationVariants"] = [ProjectJournal._observation_variant(event)]
                    projected[finding_id] = current
                    continue
                for key, value in event.items():
                    if key not in {
                        "eventId",
                        "at",
                        "type",
                        "status",
                        "reviewId",
                        "dimensions",
                    }:
                        current[key] = value
                current["dimensions"] = sorted(
                    set(current.get("dimensions", []))
                    | set(ProjectJournal._event_dimensions(event))
                )
                variant = ProjectJournal._observation_variant(event)
                if variant not in current["observationVariants"]:
                    current["observationVariants"].append(variant)
                current["lastReviewId"] = event.get("reviewId", "")
                current["lastObservedAt"] = event.get("at")
                current["observations"] += 1
                continue
            if event_type != "finding_status_changed":
                continue
            finding_id = event.get("findingId")
            if not isinstance(finding_id, str) or not finding_id:
                raise ValueError("invalid finding status event: missing findingId")
            current = projected.get(finding_id)
            if current is None:
                raise ValueError(f"invalid finding status event: unknown finding {finding_id}")
            if event.get("from") != current.get("status"):
                raise ValueError(
                    "invalid finding status transition source: "
                    f"{event.get('from')} -> {event.get('to')}"
                )
            source = current["status"]
            target = event.get("to")
            if target not in FINDING_TRANSITIONS.get(source, frozenset()):
                raise ValueError(f"invalid finding status transition: {source} -> {target}")
            current["status"] = target
            current["statusChangedAt"] = event.get("at")
            if event.get("reason"):
                current["statusReason"] = event["reason"]
            else:
                current.pop("statusReason", None)
        return list(projected.values())

    @staticmethod
    def _observation_variant(event: dict[str, Any]) -> dict[str, Any]:
        variant = {
            key: event[key]
            for key in ("path", "line", "message", "severity", "evidence", "reproduction")
            if key in event
        }
        dimensions = ProjectJournal._event_dimensions(event)
        if dimensions:
            variant["dimensions"] = dimensions
        return variant

    @staticmethod
    def _event_dimensions(event: dict[str, Any]) -> list[str]:
        try:
            return list(
                normalize_dimensions(event.get("dimensions", []), label="journal dimensions")
            )
        except ValueError as error:
            raise ValueError(str(error)) from error

    def findings_with_diagnostic(
        self, *, status: str | None = None, dimension: str | None = None
    ) -> tuple[list[dict[str, Any]], Diagnostic | None]:
        events, diagnostic = self.read_with_diagnostic()
        if diagnostic is not None:
            return [], diagnostic
        selected_dimension: str | None = None
        if dimension is not None:
            try:
                selected_dimension = normalize_dimensions([dimension], label="finding dimension")[0]
            except ValueError as error:
                return [], Diagnostic("invalid_request", str(error))
        try:
            findings = self._project(events)
        except ValueError as error:
            return [], Diagnostic("journal_corrupt", str(error))
        if status is not None:
            findings = [finding for finding in findings if finding.get("status") == status]
        if selected_dimension is not None:
            findings = [
                finding
                for finding in findings
                if selected_dimension in finding.get("dimensions", [])
            ]
        return findings, diagnostic

    def finding(self, finding_id: str) -> dict[str, Any] | None:
        findings, _diagnostic = self.findings_with_diagnostic()
        return next(
            (finding for finding in findings if finding.get("findingId") == finding_id),
            None,
        )

    def findings(
        self, *, status: str | None = None, dimension: str | None = None
    ) -> list[dict[str, Any]]:
        findings, diagnostic = self.findings_with_diagnostic(status=status, dimension=dimension)
        if diagnostic is not None:
            raise JournalOperationError(diagnostic)
        return findings
