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


def test_pi_integration_assigns_interactive_and_formal_ownership() -> None:
    document = (REPOSITORY / "docs" / "PI-INTEGRATION.md").read_text().lower()

    assert "pi" in document
    assert "interactive" in document
    assert "reviewctl" in document
    assert "formal" in document
    assert "reviewctl verify" in document


def test_pi_transcript_is_not_review_evidence() -> None:
    document = (REPOSITORY / "docs" / "PI-INTEGRATION.md").read_text().lower()

    assert "never" in document
    assert "approval" in document
    assert "transcript" in document
    assert "artifact root" in document


def test_help_llm_carries_setup_and_nonqualification_invariants() -> None:
    document = " ".join(
        (REPOSITORY / "docs" / "HELP-LLM.md").read_text().lower().split()
    )

    for command in (
        "reviewctl setup discover --format json",
        "reviewctl setup show --format json",
        "reviewctl setup check --backend name --format json",
    ):
        assert command in document
    for invariant in (
        "setup diagnostics are local, read-only, and non-qualifying.",
        (
            "setup diagnostics observe only executable presence and version for registered "
            "executable backends."
        ),
        (
            "setup diagnostics never authenticate, call a model or provider, or write "
            "configuration."
        ),
        "availability is not qualification.",
        (
            "remote api backends may execute providers or models remotely, but setup never "
            "credential-probes them."
        ),
    ):
        assert invariant in document

    for forbidden_reversal in (
        "setup diagnostics may write configuration",
        "setup diagnostics may authenticate",
        "setup diagnostics may call a model",
        "availability is qualification",
        "provider or model execution is always local",
    ):
        assert forbidden_reversal not in document

    for product in ("cursor", "claude"):
        unsupported_claim = " ".join((product, "is", "supported"))
        assert unsupported_claim not in document
