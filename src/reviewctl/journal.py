"""Small append-only project journal and finding projection."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reviewctl.errors import Diagnostic


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

    def findings(self, *, status: str | None = None) -> list[dict[str, Any]]:
        findings = [event for event in self.events() if event.get("type") == "finding"]
        if status is None:
            return findings
        return [finding for finding in findings if finding.get("status") == status]
