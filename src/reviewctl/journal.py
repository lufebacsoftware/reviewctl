"""Small append-only project journal and finding projection."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reviewctl.errors import Diagnostic, JournalOperationError

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


class ProjectJournal:
    """Append JSON events and rebuild simple finding views from them."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
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
        line = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (line + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return normalized

    def _validate_event(self, event: dict[str, Any]) -> None:
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
        current = self.finding(finding_id)
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
        events: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            return [], Diagnostic("journal_corrupt", f"could not read journal: {error}")
        for index, line in enumerate(lines, start=1):
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
                    current["observationVariants"] = [
                        ProjectJournal._observation_variant(event)
                    ]
                    projected[finding_id] = current
                    continue
                for key, value in event.items():
                    if key not in {"eventId", "at", "type", "status", "reviewId"}:
                        current[key] = value
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
                raise ValueError(
                    f"invalid finding status event: unknown finding {finding_id}"
                )
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
        return {
            key: event[key]
            for key in ("path", "line", "message", "severity", "evidence", "reproduction")
            if key in event
        }

    def findings_with_diagnostic(
        self, *, status: str | None = None
    ) -> tuple[list[dict[str, Any]], Diagnostic | None]:
        events, diagnostic = self.read_with_diagnostic()
        try:
            findings = self._project(events)
        except ValueError as error:
            return [], Diagnostic("journal_corrupt", str(error))
        if status is not None:
            findings = [finding for finding in findings if finding.get("status") == status]
        return findings, diagnostic

    def finding(self, finding_id: str) -> dict[str, Any] | None:
        findings, _diagnostic = self.findings_with_diagnostic()
        return next(
            (finding for finding in findings if finding.get("findingId") == finding_id),
            None,
        )

    def findings(self, *, status: str | None = None) -> list[dict[str, Any]]:
        findings, diagnostic = self.findings_with_diagnostic(status=status)
        if diagnostic is not None:
            raise JournalOperationError(diagnostic)
        return findings
