from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_public_package_uses_apache_license() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert project["license"] == "Apache-2.0"
    assert (ROOT / "LICENSE").read_text().startswith("Apache License\nVersion 2.0")


def test_public_tree_excludes_private_review_evidence() -> None:
    forbidden_fragments = tuple(
        "".join(parts)
        for parts in (
            ("/users/", "luis", "fernando/"),
            ("extras", "pibank"),
            ("piavi", "org"),
            ("pay", "global"),
            ("sites", "pay"),
        )
    )
    forbidden_directories = {"receipts", "sealed", "council"}

    for path in ROOT.rglob("*"):
        relative_path = path.relative_to(ROOT)
        if path.is_dir():
            assert path.name.lower() not in forbidden_directories
            continue
        if "__pycache__" in relative_path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if any(part.startswith(".") and part != ".github" for part in relative_path.parts):
            continue
        contents = path.read_text(errors="ignore").lower()
        for fragment in forbidden_fragments:
            assert fragment not in contents, f"{fragment!r} leaked through {relative_path}"


def test_project_integration_assigns_rosters_to_private_evidence_store() -> None:
    guide = (ROOT / "docs" / "PROJECT-INTEGRATION.md").read_text()

    assert "model auditions, operating rosters, and tournament evidence" not in guide
    assert "current operating roster is private evidence" in guide


def test_architecture_defines_backend_seam_and_controller_ownership() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    normalized = " ".join(architecture.lower().split())

    for contract in ("BackendRequest", "BackendExecution", "BackendCapabilities"):
        assert contract in architecture
    for boundary in (
        "local-only setup",
        "availability is not qualification",
        "adapters do not decide acceptance",
    ):
        assert boundary in normalized
    for controller_responsibility in (
        "policy",
        "contract evaluation",
        "acceptance",
        "fallback",
        "receipt",
    ):
        assert controller_responsibility in normalized


def test_architecture_keeps_legacy_adapters_unqualified_until_conformance() -> None:
    architecture = " ".join(
        (ROOT / "docs" / "ARCHITECTURE.md").read_text().lower().split()
    )

    for adapter in ("llm", "openrouter", "agy", "pi", "codex"):
        assert f"`{adapter}`" in architecture
    assert "five legacy compatibility adapters are explicitly unqualified" in architecture
    assert "setup observes only executable presence and version" in architecture
    assert "never authenticates, calls a model, or writes configuration" in architecture
    assert "the next gate is backend conformance" in architecture
    assert "before cursor, claude code, or another native backend can be added" in architecture
    assert "baml-inspired typed boundary" in architecture
    assert "native and has no baml dependency" in architecture

    for product in ("cursor", "claude"):
        unsupported_claim = " ".join((product, "is", "supported"))
        assert unsupported_claim not in architecture


def test_release_workflow_builds_and_publishes_verified_artifacts() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert "tags:" in workflow
    assert '"v*"' in workflow
    assert "uv build" in workflow
    assert "(cd dist && sha256sum * > SHA256SUMS)" in workflow
    assert "uv tool install" in workflow
    assert "actions/checkout@v6" in workflow
    assert "actions/attest-build-provenance@v4" in workflow
    assert "softprops/action-gh-release@v3" in workflow
    assert "@v2" not in workflow
    assert "\n            dist/*\n" not in workflow
    assert "dist/*.whl" in workflow
    assert "dist/*.tar.gz" in workflow
    assert "dist/SHA256SUMS" in workflow
