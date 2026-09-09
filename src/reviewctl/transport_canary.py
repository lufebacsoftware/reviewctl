"""Provider-backed synthetic canary packet and report contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CANARY_SOURCE_NAME = "canary.py"
CANARY_SOURCE = "def add(left: int, right: int) -> int:\n    return left + right\n"


def canary_prompt() -> str:
    """Require a minimal valid findings response over one frozen synthetic file."""
    return (
        "Review the supplied synthetic file. Return only this exact JSON object: "
        f'{{"verdict":"approved","findings":[],"reviewedFiles":["{CANARY_SOURCE_NAME}"]}}.'
    )


def build_canary_report(
    *,
    profile: Mapping[str, object],
    routes: Sequence[Mapping[str, str]],
    receipt_path: Path,
    receipt: Mapping[str, Any],
    exit_code: int,
) -> dict[str, object]:
    """Build the stable summary that points to one canonical canary receipt."""
    return {
        "acceptedAttempt": receipt.get("acceptedAttempt"),
        "exitCode": exit_code,
        "kind": "transport-canary",
        "profile": dict(profile),
        "receipt": {
            "path": str(receipt_path),
            "sha256": receipt.get("sha256"),
        },
        "result": receipt.get("result"),
        "routes": [dict(route) for route in routes],
        "version": 1,
    }
