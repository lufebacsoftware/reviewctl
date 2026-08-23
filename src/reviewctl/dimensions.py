"""Versioned, bounded review-dimension names."""

from __future__ import annotations

import re
from collections.abc import Iterable

DIMENSION_SCHEMA_VERSION = 1
MAX_DIMENSIONS = 32
MAX_DIMENSION_NAME_LENGTH = 64
COMMON_DIMENSIONS = frozenset(
    {
        "architecture",
        "correctness",
        "financial",
        "fiscal",
        "privacy",
        "public-api",
        "release",
        "security",
        "ui-accessibility",
    }
)
_CUSTOM_DIMENSION = re.compile(r"^custom\.[a-z0-9][a-z0-9._-]*$")


def normalize_dimensions(
    values: Iterable[object] | None,
    *,
    label: str = "dimensions",
    default: Iterable[object] = (),
) -> tuple[str, ...]:
    """Validate and return dimensions in canonical order."""
    candidate = default if values is None else values
    if isinstance(candidate, (str, bytes)):
        raise ValueError(f"{label} must be an array of strings")
    try:
        raw_values = list(candidate)
    except TypeError as error:
        raise ValueError(f"{label} must be an array of strings") from error
    if len(raw_values) > MAX_DIMENSIONS:
        raise ValueError(f"{label} must contain at most {MAX_DIMENSIONS} dimensions")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must contain only non-empty strings")
        name = value.strip()
        if len(name) > MAX_DIMENSION_NAME_LENGTH:
            raise ValueError(
                f"{label} dimension names must be at most {MAX_DIMENSION_NAME_LENGTH} characters"
            )
        if name not in COMMON_DIMENSIONS and not _CUSTOM_DIMENSION.fullmatch(name):
            raise ValueError(
                f"{label} contains invalid dimension {name!r}; use a common name or custom.<slug>"
            )
        if name in seen:
            raise ValueError(f"{label} contains duplicate dimension {name!r}")
        seen.add(name)
        normalized.append(name)
    return tuple(sorted(normalized))


def merge_dimensions(*groups: Iterable[str]) -> tuple[str, ...]:
    """Union already-validated dimension groups in canonical order."""
    return tuple(sorted({dimension for group in groups for dimension in group}))
