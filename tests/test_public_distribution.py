from __future__ import annotations

import tomllib
from pathlib import Path

import reviewctl

ROOT = Path(__file__).parents[1]
PUBLIC_RECEIPT_FIXTURES = Path("tests/fixtures/receipts")
REVIEWED_PUBLIC_RECEIPT_FIXTURES = {
    "accepted-findings-v1.json",
    "legacy-digest-only.json",
    "unavailable-findings-v1.json",
}


def is_reviewed_public_receipt_fixture(relative_path: Path, *, is_dir: bool) -> bool:
    if is_dir:
        return relative_path == PUBLIC_RECEIPT_FIXTURES
    return (
        relative_path.parent == PUBLIC_RECEIPT_FIXTURES
        and relative_path.name in REVIEWED_PUBLIC_RECEIPT_FIXTURES
    )


def test_runtime_version_matches_project_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert reviewctl.__version__ == project["version"]


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
        if PUBLIC_RECEIPT_FIXTURES in relative_path.parents:
            assert is_reviewed_public_receipt_fixture(relative_path, is_dir=path.is_dir()), (
                f"unreviewed public receipt fixture: {relative_path}"
            )
        if path.is_dir():
            assert (
                is_reviewed_public_receipt_fixture(relative_path, is_dir=True)
                or path.name.lower() not in forbidden_directories
            )
            continue
        if "__pycache__" in relative_path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if any(part.startswith(".") and part != ".github" for part in relative_path.parts):
            continue
        contents = path.read_text(errors="ignore").lower()
        for fragment in forbidden_fragments:
            assert fragment not in contents, f"{fragment!r} leaked through {relative_path}"


def test_public_receipt_fixture_guard_rejects_unreviewed_paths() -> None:
    for filename in REVIEWED_PUBLIC_RECEIPT_FIXTURES:
        assert is_reviewed_public_receipt_fixture(PUBLIC_RECEIPT_FIXTURES / filename, is_dir=False)
    for relative_path, is_dir in (
        (PUBLIC_RECEIPT_FIXTURES / "unexpected.json", False),
        (PUBLIC_RECEIPT_FIXTURES / "nested", True),
        (PUBLIC_RECEIPT_FIXTURES / "nested" / "accepted-findings-v1.json", False),
    ):
        assert not is_reviewed_public_receipt_fixture(relative_path, is_dir=is_dir)


def test_project_integration_assigns_rosters_to_private_evidence_store() -> None:
    guide = (ROOT / "docs" / "PROJECT-INTEGRATION.md").read_text()

    assert "model auditions, operating rosters, and tournament evidence" not in guide
    assert "current operating roster is private evidence" in guide


def test_architecture_defines_backend_seam_and_controller_ownership() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    normalized = " ".join(architecture.lower().split())

    for contract in ("BackendRequest", "BackendExecution", "BackendCapabilities"):
        assert contract in architecture
    for invariant in (
        (
            "reviewctl keeps the controller, adapter process, evidence handling, "
            "and setup diagnostics local."
        ),
        "provider or model execution may be remote for `remote_api` backends.",
        "local reviewctl execution does not enable remote controller or adapter dispatch.",
        (
            "federation remains deferred and separate from backend execution and "
            "setup synchronization."
        ),
        (
            "backend adapters only invoke a backend and persist its observed evidence; "
            "adapters do not decide acceptance."
        ),
        (
            "the controller alone owns policy, contract evaluation, acceptance, fallback, "
            "and receipt construction."
        ),
    ):
        assert invariant in normalized

    for forbidden_reversal in (
        "provider or model execution is always local",
        "remote_api backends make the controller remote",
        "federation is part of backend setup",
        "adapters may decide acceptance",
    ):
        assert forbidden_reversal not in normalized


def test_architecture_keeps_legacy_adapters_unqualified_until_conformance() -> None:
    architecture = " ".join((ROOT / "docs" / "ARCHITECTURE.md").read_text().lower().split())

    for adapter in ("llm", "openrouter", "agy", "pi", "codex"):
        assert f"`{adapter}`" in architecture
    assert "five legacy compatibility adapters are explicitly unqualified" in architecture
    assert (
        "setup diagnostics observe only executable presence and version for registered "
        "executable backends."
    ) in architecture
    assert (
        "setup diagnostics never authenticate, call a model or provider, or write configuration."
    ) in architecture
    assert "the next gate is backend conformance" in architecture
    assert "before cursor, claude code, or another native backend can be added" in architecture
    assert "baml-inspired typed boundary" in architecture
    assert "native and has no baml dependency" in architecture

    for forbidden_reversal in (
        "setup diagnostics may write configuration",
        "setup diagnostics may authenticate",
        "setup diagnostics may call a model",
        "availability is qualification",
    ):
        assert forbidden_reversal not in architecture

    for product in ("cursor", "claude"):
        unsupported_claim = " ".join((product, "is", "supported"))
        assert unsupported_claim not in architecture


def test_architecture_documents_the_unqualified_kiro_backend_boundary() -> None:
    architecture = " ".join((ROOT / "docs" / "ARCHITECTURE.md").read_text().lower().split())

    for statement in (
        "kiro is a registered native agent-cli adapter",
        "remains unqualified",
        "`kiro-cli`",
        "`kiro_bin`",
        "`reviewctl setup check --backend kiro`",
        "version-only local discovery",
        "`kiro-cli chat --list-models --format json`",
        "availability and a valid receipt do not qualify a model",
        "disposable controlled working directory",
        "reduced environment",
        "`reviewctl_readonly`",
        "no tools, allowed tools, mcp servers, inherited mcp configuration, or resources",
        "exact configuration and digest are retained",
        "inline frozen packet",
        "one total timeout",
        "mode `0600`",
        "dynamic model check",
        "session recovery",
        "initial adapter accepts only `findings-json`",
        "fail before artifacts or source transmission",
        "forced to `term=dumb` with standard no-color settings",
        "only a boundary at byte zero is removed",
        "invalid utf-8 or ansi remaining inside the json payload fails the attempt",
        "`extension.mergegateeligible: false`",
        "any merge gate must reject that explicit indicator",
        "receipt verification rejects an accepted kiro receipt if either value is absent "
        "or changed",
        "legacy schema-v1 receipts cannot claim kiro",
        "verification also requires `extension.kirounresolvedidentitywaiver: true`",
        "advisory read-only",
        "`sourceisolation: unavailable`",
        "not os sandbox enforcement",
    ):
        assert statement in architecture


def test_help_documents_kiro_selection_policy_and_failure_recovery() -> None:
    help_text = " ".join((ROOT / "docs" / "HELP-LLM.md").read_text().lower().split())

    for guidance in (
        "`--transport kiro --model model_id`",
        "`--route kiro:model_id`",
        "`auto` is rejected",
        "currently supports only `--response-contract findings-json`",
        "cannot be separated from kiro ui framing without rewriting possible model content",
        "forces a dumb, no-color terminal",
        "a banner or later prompt-like line is not a response boundary",
        "rejects invalid utf-8 or ansi inside the json payload instead of repairing it",
        '`extension.backendqualification = "unqualified"`',
        "`extension.mergegateeligible = false`",
        "merge automation must reject that flag",
        "`reviewctl verify` rejects a kiro receipt if either value is missing or changed",
        "legacy schema-v1 receipts that claim the kiro transport fail verification",
        "`extension.kirounresolvedidentitywaiver = true`",
        "does not use openrouter",
        "does not inherit ambient provider, aws, or api-token variables",
        "proprietary kiro source requires both policy decisions",
        "source_allowed = true",
        "allow_unresolved_identity = true",
        "private policy may authorize a transport's runtime-owned model inventory",
        'an exact `[models."model_id"]` entry overrides its transport default',
        "does not authorize openrouter or make a backend a merge gate",
        "records that waiver in the receipt",
        "does not qualify the backend or prove which model executed",
        "synthetic runs require neither the policy nor the waiver",
        "rerun `kiro-cli chat --list-models --format json`",
        "inspect the attempt evidence and do not treat the result as approval",
        "run `reviewctl setup check --backend kiro`",
        "supported by `run`, routes, and tournaments",
    ):
        assert guidance in help_text


def test_project_guidance_keeps_kiro_rosters_out_of_instruction_files() -> None:
    guide = " ".join((ROOT / "docs" / "PROJECT-INTEGRATION.md").read_text().lower().split())

    assert "projects may state when review is required and which commands to run" in guide
    assert "must not embed kiro model tables" in guide


def test_global_design_lists_kiro_and_justifies_the_native_adapter() -> None:
    design = " ".join(
        (ROOT / "docs" / "superpowers" / "specs" / "2026-08-12-global-best-match-review-design.md")
        .read_text()
        .lower()
        .split()
    )

    assert "`llm`, `openrouter`, `agy`, `pi`, `codex`, and `kiro`" in design
    assert "subscription access unavailable through pi or openrouter" in design
    for boundary in (
        (
            "for qualified merge-gate backends, reviewctl makes every original "
            "source root inaccessible"
        ),
        (
            "an advisory formal attempt such as unqualified kiro with "
            "`sourceisolation: unavailable` is not an isolation guarantee"
        ),
        (
            "cannot become qualified merge-gate evidence until a qualified external sandbox "
            "denies all original source roots and organization qualification proves the boundary"
        ),
        (
            "reviewctl still allows the `run` transport to invoke such an advisory attempt "
            "and persist its observed evidence"
        ),
    ):
        assert boundary in design


def test_public_kiro_guidance_does_not_publish_a_static_model_table() -> None:
    paths = (
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "HELP-LLM.md",
        ROOT / "docs" / "PROJECT-INTEGRATION.md",
        ROOT / "docs" / "superpowers" / "specs" / "2026-08-12-global-best-match-review-design.md",
        ROOT / "docs" / "superpowers" / "plans" / "2026-08-14-kiro-cli-backend.md",
    )
    lines = [line.lower() for path in paths for line in path.read_text().splitlines()]
    combined = "\n".join(lines)
    help_text = " ".join((ROOT / "docs" / "HELP-LLM.md").read_text().lower().split())

    compact_lines = [line.replace(" ", "") for line in lines]
    assert not any(
        line.startswith(("|model|", "|kiromodel|", "|price|", "|credits|"))
        for line in compact_lines
    )
    for forbidden_static_detail in (
        "kiro models:",
        "kiro model roster:",
        "price:",
        "credits per",
        "$",
    ):
        assert forbidden_static_detail not in combined
    route_tokens = {
        token.strip("`'\".,;()[]{}")
        for token in combined.split()
        if token.strip("`'\".,;()[]{}").startswith("kiro:")
    }
    assert route_tokens == {"kiro:model_id"}
    assert "claude-sonnet-" not in combined
    assert "`kiro-cli chat --list-models --format json`" in help_text
    assert "reviewctl run --review-id id --route kiro:model_id" in help_text

    plan = " ".join(
        (ROOT / "docs" / "superpowers" / "plans" / "2026-08-14-kiro-cli-backend.md")
        .read_text()
        .lower()
        .split()
    )
    assert "document example routing as `--route kiro:model_id`" in plan
    assert "select `model_id` from the runtime inventory" in plan


def test_architecture_defines_partial_review_and_consolidation_semantics() -> None:
    architecture = " ".join((ROOT / "docs" / "ARCHITECTURE.md").read_text().lower().split())

    for invariant in (
        "complete, incomplete, or invalid",
        "pre-gates run before contract evaluation",
        "rejected responses never promote fragments",
        "completion context is bound to the target contract",
        "never contains the raw response",
        "never inherits approval",
        "absence is not a dispute",
        "acceptedattempt names a real complete accepted attempt",
        "partial or unconfirmed findings",
        "approval is stricter",
        "maxattempts applies independently to each route",
        "schema v1 remains digest-only",
        "schema v2 adds offline structural verification",
    ):
        assert invariant in architecture

    for deferred in (
        "no baml runtime dependency",
        "editable execution is deferred",
        "federation is optional future work",
        "potzal is not a dependency",
    ):
        assert deferred in architecture


def test_evidence_docs_bound_digest_and_structural_verification_trust() -> None:
    evidence = " ".join((ROOT / "docs" / "EVIDENCE.md").read_text().lower().split())

    for boundary in (
        "not a digital signature",
        "not a trust root",
        "authorized to rewrite",
    ):
        assert boundary in evidence


def test_public_docs_do_not_claim_deferred_integrations_or_publish_private_rosters() -> None:
    public_docs = "\n".join(
        (ROOT / "docs" / name).read_text().lower()
        for name in ("ARCHITECTURE.md", "EVIDENCE.md", "HELP-LLM.md")
    )

    for unsupported_claim in (
        "cursor is supported",
        "claude is supported",
        "potzal is required",
        "baml is required",
    ):
        assert unsupported_claim not in public_docs
    for forbidden_detail in (
        "model roster:",
        "provider command:",
        "api key:",
        "price table:",
    ):
        assert forbidden_detail not in public_docs


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
