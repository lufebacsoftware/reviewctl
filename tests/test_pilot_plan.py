from __future__ import annotations

from pathlib import Path

REPOSITORY = Path(__file__).parents[1]


def test_public_synthetic_fixture_sets_are_present_and_source_neutral() -> None:
    fixture_root = REPOSITORY / "tournaments" / "fixtures"
    expected = {"distributed", "financial", "pilot", "product"}

    assert expected <= {path.name for path in fixture_root.iterdir() if path.is_dir()}
    for fixture in fixture_root.glob("**/*"):
        if fixture.is_file():
            contents = fixture.read_text().lower()
            for parts in (("open", "bancor"), ("pay", "global")):
                assert "".join(parts) not in contents


def test_public_documentation_does_not_contain_local_artifact_paths() -> None:
    for document in (REPOSITORY / "docs").glob("*.md"):
        contents = document.read_text()
        assert "/users/" not in contents.lower()
        assert "/private/" not in contents.lower()
