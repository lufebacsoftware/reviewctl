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
        if path.is_dir():
            assert path.name.lower() not in forbidden_directories
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if any(part.startswith(".") and part != ".github" for part in path.parts):
            continue
        contents = path.read_text(errors="ignore").lower()
        for fragment in forbidden_fragments:
            assert fragment not in contents, f"{fragment!r} leaked through {path.relative_to(ROOT)}"


def test_project_integration_assigns_rosters_to_private_evidence_store() -> None:
    guide = (ROOT / "docs" / "PROJECT-INTEGRATION.md").read_text()

    assert "model auditions, operating rosters, and tournament evidence" not in guide
    assert "current operating roster is private evidence" in guide


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
