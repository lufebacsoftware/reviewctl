from __future__ import annotations

import pytest

from reviewctl.dimensions import merge_dimensions, normalize_dimensions


@pytest.mark.parametrize(
    "value",
    ["security", b"security", 7, object()],
)
def test_normalize_dimensions_rejects_non_array_inputs(value: object) -> None:
    with pytest.raises(ValueError, match="array"):
        normalize_dimensions(value)  # type: ignore[arg-type]


def test_normalize_dimensions_uses_default_for_none() -> None:
    assert normalize_dimensions(None, default=("security",)) == ("security",)


@pytest.mark.parametrize(
    "value",
    [
        ["security"] * 33,
        [""],
        [7],
        ["x" * 65],
        ["not-valid"],
        ["custom."],
        ["security", "security"],
    ],
)
def test_normalize_dimensions_rejects_invalid_values(value: list[object]) -> None:
    with pytest.raises(ValueError, match="dimension"):
        normalize_dimensions(value)


def test_normalize_dimensions_accepts_common_and_custom_names_in_order() -> None:
    assert normalize_dimensions([" custom.audit ", "security", "architecture"]) == (
        "architecture",
        "custom.audit",
        "security",
    )


def test_merge_dimensions_unions_groups_in_canonical_order() -> None:
    assert merge_dimensions(("security",), ("correctness", "security")) == (
        "correctness",
        "security",
    )
