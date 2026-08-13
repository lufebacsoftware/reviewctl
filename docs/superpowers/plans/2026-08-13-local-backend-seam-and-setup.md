# Local Backend Seam and Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `run_review`'s transport dispatch branches with a typed local backend registry and expose read-only setup discovery without changing existing review, receipt, or route behavior.

**Architecture:** A new `reviewctl.backends` module owns provider-neutral execution types, backend descriptors, capabilities, and registry behavior. Existing invocation functions remain in `cli.py` during this compatibility slice and are wrapped by five registered adapters; `run_review` crosses one backend interface. A new `reviewctl.setup` module observes local executables and renders stable topology JSON without installing tools, authenticating, calling an LLM, or claiming conformance.

**Tech Stack:** Python 3.12+, standard-library dataclasses/enum/pathlib/shutil/subprocess/importlib.metadata, argparse, pytest, Ruff, existing receipt format.

---

## Scope and sequence

This is implementation phase 1 of the approved global best-match design. The later plans are:

1. typed partial contract evaluation, valid-fragment extraction, fallback, and deterministic consolidation;
2. Cursor and Claude native backends plus source-isolation conformance, followed by other qualified candidates;
3. project journals and signed manual `ReviewExchangeBundle` export/import;
4. advisory global instructions and rollout evidence;
5. editable `ChangeAttempt` execution as a separate non-approval workflow.

This plan does **not** add a new LLM backend, change best-match ordering, make a support claim, implement federation, or alter the meaning of existing receipts. Existing names `llm`, `openrouter`, `agy`, `pi`, and `codex` remain public compatibility names.

Baseline before implementation: commit `8f17714`, 272 tests passing, Ruff passing.

## File map

- Create `src/reviewctl/backends.py`: execution value types, capabilities, descriptors, registry, and stable setup serialization.
- Create `src/reviewctl/setup.py`: executable discovery, bounded version probing, topology/check results, and output rendering.
- Create `tests/test_backends.py`: direct type, registry, identity, and serialization tests.
- Create `tests/test_setup.py`: hermetic local discovery and setup-result tests.
- Modify `src/reviewctl/cli.py`: compatibility re-export of `PersistedResponse`, five backend wrappers, registry construction, one dispatch call, setup CLI, and `help-llm` additions.
- Modify `tests/test_run.py`: dispatch compatibility, evidence-path preservation, parser, setup CLI, and receipt regression tests.
- Modify `docs/HELP-LLM.md`: local backend discovery and qualification language.
- Modify `docs/ARCHITECTURE.md`: backend seam ownership and distinction between availability and conformance.

### Task 1: Add provider-neutral backend contracts

**Files:**
- Create: `src/reviewctl/backends.py`
- Create: `tests/test_backends.py`
- Modify: `src/reviewctl/cli.py:249-260`

- [ ] **Step 1: Write failing type and registry tests**

Create `tests/test_backends.py` with:

```python
from pathlib import Path

import pytest

from reviewctl.backends import (
    BackendCapabilities,
    BackendDescriptor,
    BackendEvidence,
    BackendExecution,
    BackendFamily,
    BackendRegistry,
    BackendRequest,
    DiscoveryKind,
    PersistedResponse,
    ReadOnlyCapability,
    SourceIsolation,
)


def descriptor(name: str) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        family=BackendFamily.AGENT_CLI,
        discovery_kind=DiscoveryKind.EXECUTABLE,
        executable_env=f"{name.upper()}_BIN",
        default_executable=name,
        capabilities=BackendCapabilities(
            review_read_only=ReadOnlyCapability.ADVISORY,
            editable_execution=False,
            structured_output=True,
            resolved_model_identity=True,
            resolved_provider_identity=False,
            conversation_identity=True,
            usage_reporting=False,
            timeout_control=True,
            tool_control=False,
            source_isolation=SourceIsolation.UNAVAILABLE,
        ),
        qualification="unqualified",
    )


def test_registry_requires_unique_known_backends(tmp_path: Path) -> None:
    response = PersistedResponse("turn", None, 1, None, "model", None, None, "ok")

    def execute(request: BackendRequest) -> BackendExecution:
        assert request.attempt_dir == tmp_path
        return BackendExecution(0, "", response, BackendEvidence())

    registry = BackendRegistry()
    registry.register(descriptor("cursor"), execute)
    assert registry.require("cursor").descriptor.name == "cursor"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(descriptor("cursor"), execute)
    with pytest.raises(KeyError, match="unknown backend"):
        registry.require("missing")


def test_execution_types_are_immutable(tmp_path: Path) -> None:
    request = BackendRequest(
        prompt="Review",
        model="model",
        response_contract="findings-json",
        files=(tmp_path / "source.py",),
        attempt_dir=tmp_path,
        timeout_seconds=30,
        max_output_tokens=4096,
        source_class="synthetic",
        source_roots=(),
        provider_preferences=None,
    )
    with pytest.raises(Exception):
        request.model = "changed"  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `uv run pytest tests/test_backends.py -q`

Expected: collection fails because `reviewctl.backends` does not exist.

- [ ] **Step 3: Implement the backend contracts and registry**

Create `src/reviewctl/backends.py` with frozen dataclasses and `StrEnum` values matching the approved spec. The complete public surface for this phase is:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class BackendFamily(StrEnum):
    AGENT_CLI = "agent-cli"
    PROVIDER_GATEWAY = "provider-gateway"
    GENERIC_MODEL_CLI = "generic-model-cli"
    AGENT_PROTOCOL = "agent-protocol"


class DiscoveryKind(StrEnum):
    EXECUTABLE = "executable"
    REMOTE_API = "remote-api"


class ReadOnlyCapability(StrEnum):
    ENFORCED = "enforced"
    SANDBOXED = "sandboxed"
    ADVISORY = "advisory"
    UNSUPPORTED = "unsupported"


class SourceIsolation(StrEnum):
    BACKEND_ENFORCED = "backend-enforced"
    EXTERNAL_SANDBOX = "external-sandbox"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BackendCapabilities:
    review_read_only: ReadOnlyCapability
    editable_execution: bool
    structured_output: bool
    resolved_model_identity: bool
    resolved_provider_identity: bool
    conversation_identity: bool
    usage_reporting: bool
    timeout_control: bool
    tool_control: bool
    source_isolation: SourceIsolation


@dataclass(frozen=True)
class BackendDescriptor:
    name: str
    family: BackendFamily
    discovery_kind: DiscoveryKind
    executable_env: str
    default_executable: str
    capabilities: BackendCapabilities
    qualification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersistedResponse:
    conversation_id: str
    cost_usd: float | None
    duration_ms: int | None
    input_tokens: int | None
    model: str
    output_tokens: int | None
    provider: str | None
    response: str


@dataclass(frozen=True)
class BackendEvidence:
    database: Path | None = None
    request: Path | None = None
    response: Path | None = None
    session: Path | None = None
    final_response: Path | None = None
    stderr: Path | None = None


@dataclass(frozen=True)
class BackendRequest:
    prompt: str
    model: str
    response_contract: str
    files: tuple[Path, ...]
    attempt_dir: Path
    timeout_seconds: int
    max_output_tokens: int
    source_class: str
    source_roots: tuple[Path, ...]
    provider_preferences: dict[str, object] | None


@dataclass(frozen=True)
class BackendExecution:
    exit_code: int
    diagnostic: str
    response: PersistedResponse | None
    evidence: BackendEvidence


BackendExecutor = Callable[[BackendRequest], BackendExecution]


@dataclass(frozen=True)
class RegisteredBackend:
    descriptor: BackendDescriptor
    execute: BackendExecutor


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, RegisteredBackend] = {}

    def register(self, descriptor: BackendDescriptor, execute: BackendExecutor) -> None:
        if descriptor.name in self._backends:
            raise ValueError(f"backend {descriptor.name!r} is already registered")
        self._backends[descriptor.name] = RegisteredBackend(descriptor, execute)

    def require(self, name: str) -> RegisteredBackend:
        try:
            return self._backends[name]
        except KeyError as error:
            raise KeyError(f"unknown backend {name!r}") from error

    def descriptors(self) -> tuple[BackendDescriptor, ...]:
        return tuple(self._backends[name].descriptor for name in sorted(self._backends))
```

Move `PersistedResponse` ownership from `cli.py` to this module and import it into `cli.py`, preserving `reviewctl.cli.PersistedResponse` as a compatibility alias.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_backends.py tests/test_run.py::test_transport_return_annotations_match_runtime_tuple_shapes -q
uv run ruff check src/reviewctl/backends.py tests/test_backends.py src/reviewctl/cli.py
git add src/reviewctl/backends.py tests/test_backends.py src/reviewctl/cli.py
git commit -m "feat: define local backend execution seam"
```

Expected: direct contract tests pass and existing annotation compatibility remains green.

### Task 2: Register the five existing execution adapters

**Files:**
- Modify: `src/reviewctl/cli.py:1358-2392`
- Modify: `tests/test_backends.py`

- [ ] **Step 1: Write failing registry inventory tests**

Add:

```python
from reviewctl import cli


def test_builtin_registry_preserves_all_legacy_transport_names() -> None:
    registry = cli.build_backend_registry()
    assert [item.name for item in registry.descriptors()] == [
        "agy",
        "codex",
        "llm",
        "openrouter",
        "pi",
    ]
    assert all(item.qualification == "unqualified" for item in registry.descriptors())
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_backends.py::test_builtin_registry_preserves_all_legacy_transport_names -q`

Expected: failure because `build_backend_registry` is absent.

- [ ] **Step 3: Add exact adapter wrappers and registry construction**

In `cli.py`, add one wrapper per existing transport. Each wrapper owns its evidence filenames and returns `BackendExecution`; do not classify acceptance here. Use these exact evidence mappings:

```python
def execute_llm_backend(request: BackendRequest) -> BackendExecution:
    database = request.attempt_dir / "transport.sqlite3"
    exit_code, diagnostic = invoke_llm(
        llm_bin=os.environ.get("LLM_BIN", "llm"),
        prompt=request.prompt,
        model=request.model,
        database=database,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
    )
    return BackendExecution(
        exit_code, diagnostic, load_response(database), BackendEvidence(database=database)
    )


def execute_codex_backend(request: BackendRequest) -> BackendExecution:
    exit_code, diagnostic, response = invoke_codex(
        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        prompt=request.prompt,
        model=request.model,
        response_contract=request.response_contract,
        source_roots=list(request.source_roots) or None,
        timeout_seconds=request.timeout_seconds,
        workspace=request.files[0].parent,
    )
    response_path = request.attempt_dir / "response.md"
    response_path.write_text(response.response)
    return BackendExecution(
        exit_code, diagnostic, response, BackendEvidence(response=response_path)
    )


def execute_openrouter_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"
    exit_code, diagnostic, response = invoke_openrouter(
        api_key=os.environ.get("OPENROUTER_API_KEY"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        provider_preferences=request.provider_preferences,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        response_path=response_path,
    )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(request=request_path, response=response_path),
    )


def execute_agy_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "response.json"
    exit_code, diagnostic, response = invoke_agy(
        agy_bin=os.environ.get("AGY_BIN", "agy"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        response_path=response_path,
    )
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(request=request_path, response=response_path),
    )


def execute_pi_backend(request: BackendRequest) -> BackendExecution:
    request_path = request.attempt_dir / "request.json"
    response_path = request.attempt_dir / "events.jsonl"
    session_path = request.attempt_dir / "session.jsonl"
    final_path = request.attempt_dir / "response.md"
    stderr_path = request.attempt_dir / "stderr.log"
    exit_code, diagnostic, response = invoke_pi(
        pi_bin=os.environ.get("PI_BIN", "pi"),
        prompt=request.prompt,
        model=request.model,
        files=list(request.files),
        max_output_tokens=request.max_output_tokens,
        response_contract=request.response_contract,
        timeout_seconds=request.timeout_seconds,
        request_path=request_path,
        response_path=response_path,
        session_path=session_path,
        diagnostic_path=stderr_path,
    )
    if response.response:
        final_path.write_text(response.response)
    return BackendExecution(
        exit_code,
        diagnostic,
        response,
        BackendEvidence(
            request=request_path,
            response=response_path,
            session=session_path,
            final_response=final_path,
            stderr=stderr_path,
        ),
    )
```

Add `build_backend_registry()` with descriptors for all five names using this matrix. Mark every descriptor `qualification="unqualified"`; capability declarations describe the adapter contract being preserved, not conformance or support claims.

| name | family | discovery | executable env | default | read-only | structured output | model identity | provider identity | conversation identity | usage | timeout | tool control | source isolation |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| `agy` | `AGENT_CLI` | `EXECUTABLE` | `AGY_BIN` | `agy` | `ADVISORY` | yes | yes | no | yes | no | yes | no | `UNAVAILABLE` |
| `codex` | `AGENT_CLI` | `EXECUTABLE` | `CODEX_BIN` | `codex` | `SANDBOXED` | yes | yes | no | yes | no | yes | yes | `EXTERNAL_SANDBOX` |
| `llm` | `GENERIC_MODEL_CLI` | `EXECUTABLE` | `LLM_BIN` | `llm` | `ADVISORY` | yes | yes | yes | yes | yes | yes | no | `UNAVAILABLE` |
| `openrouter` | `PROVIDER_GATEWAY` | `REMOTE_API` | empty | empty | `UNSUPPORTED` | yes | yes | yes | yes | yes | yes | no | `UNAVAILABLE` |
| `pi` | `AGENT_CLI` | `EXECUTABLE` | `PI_BIN` | `pi` | `ADVISORY` | yes | yes | yes | yes | yes | yes | no | `UNAVAILABLE` |

For `openrouter`, setup must report discovery as `not-applicable`; it must not inspect or reveal `OPENROUTER_API_KEY`. The empty executable fields are deliberate and table-tested. Do not overload a secret environment variable as an executable override.

Keep `ROUTE_TRANSPORTS` and tournament parsing unchanged in this task. Add table-driven assertions for every cell above so later backend additions cannot silently inherit capabilities.

- [ ] **Step 4: Verify wrappers without provider calls and commit**

Use existing fake-binary tests and monkeypatch low-level invokers. Run:

```bash
uv run pytest tests/test_backends.py tests/test_run.py -q
uv run ruff check .
git add src/reviewctl/cli.py tests/test_backends.py
git commit -m "refactor: register legacy review backends"
```

Expected: all existing transport tests pass; no network or paid model call occurs.

### Task 3: Replace orchestration dispatch with the registry

**Files:**
- Modify: `src/reviewctl/cli.py:2800-2960`
- Modify: `tests/test_run.py`

- [ ] **Step 1: Add a failing fake-backend integration test**

Add `test_run_dispatches_frozen_packet_through_registered_backend` next to the current transport tests. Invoke `run_review` in-process with the parser namespace so monkeypatching applies. The fake registry must capture exactly one `BackendRequest` and return:

```python
BackendExecution(
    exit_code=0,
    diagnostic="",
    response=PersistedResponse(
        conversation_id="fake-turn",
        cost_usd=None,
        duration_ms=1,
        input_tokens=10,
        model="accepted",
        output_tokens=2,
        provider=None,
        response=json.dumps({"verdict": "approved", "findings": []}),
    ),
    evidence=BackendEvidence(response=evidence_path),
)
```

Before returning, write that exact response to `evidence_path`. Assert:

```python
assert len(captured_requests) == 1
assert captured_requests[0].model == "accepted"
assert captured_requests[0].response_contract == "findings-json"
assert captured_requests[0].files
assert all(path != original_source for path in captured_requests[0].files)
assert all(path.parent.name.startswith("reviewctl-input-") for path in captured_requests[0].files)
assert receipt["transport"] == "llm"
assert receipt["acceptedAttempt"] == 1
assert receipt["attempts"][0]["result"] == "accepted"
assert Path(receipt["attempts"][0]["evidence"]["response"]) == evidence_path
```

Use the existing `review_arguments` fixture data and parser construction rather than creating a second receipt fixture vocabulary.

- [ ] **Step 2: Verify RED**

Run the new node ID. Expected: the fake is never called because `run_review` still branches directly.

- [ ] **Step 3: Dispatch once through `BackendRegistry`**

Construct the registry once before the attempt loop. Replace the five-way invocation branch with:

```python
execution = backend_registry.require(transport).execute(
    BackendRequest(
        prompt=review_prompt,
        model=transport_model,
        response_contract=args.response_contract,
        files=tuple(snapshots),
        attempt_dir=attempt_dir,
        timeout_seconds=timeout_seconds,
        max_output_tokens=args.max_output_tokens,
        source_class=args.source_class,
        source_roots=tuple(codex_source_roots or ()),
        provider_preferences=provider_preferences,
    )
)
exit_code = execution.exit_code
stderr = execution.diagnostic
persisted = execution.response
database = execution.evidence.database
request_path = execution.evidence.request
response_path = execution.evidence.response
session_path = execution.evidence.session
final_response_path = execution.evidence.final_response
diagnostic_path = execution.evidence.stderr
```

Do not move result classification, contract evaluation, policy checks, fallback logging, or receipt construction into an adapter.

- [ ] **Step 4: Verify exact receipt compatibility and commit**

Run:

```bash
uv run pytest tests/test_run.py tests/test_backends.py -q
uv run pytest
uv run ruff check .
git add src/reviewctl/cli.py tests/test_run.py
git commit -m "refactor: dispatch review attempts through backends"
```

Expected: the full suite remains green and existing receipt snapshots retain their meaning.

### Task 4: Add hermetic local setup discovery

**Files:**
- Create: `src/reviewctl/setup.py`
- Create: `tests/test_setup.py`

- [ ] **Step 1: Write failing discovery tests**

Test a fake `which` and version probe. Assert sorted stable output, environment overrides, absent executables, bounded diagnostics, no environment values, and `qualification="unqualified"`. Test that version probing receives only the resolved executable plus `--version`, a five-second timeout, and an environment mapping supplied by the caller.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_setup.py -q`

Expected: collection fails because `reviewctl.setup` is absent.

- [ ] **Step 3: Implement discovery and topology serialization**

Create these frozen types and serialize them with `asdict` so JSON keys stay stable:

```python
@dataclass(frozen=True)
class BackendInstallation:
    name: str
    requested_executable: str | None
    resolved_executable: str | None
    version: str | None
    availability: str
    qualification: str
    diagnostics: tuple[str, ...]
    probe_performed: bool


@dataclass(frozen=True)
class LocalExecutionTopology:
    schema_version: int
    local_only: bool
    model_probe_performed: bool
    backends: tuple[BackendInstallation, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
```

Use `schema_version=1`, `local_only=True`, and `model_probe_performed=False`. JSON rendering uses camel-case only at the CLI boundary (`schemaVersion`, `localOnly`, `modelProbePerformed`, and the equivalent installation keys); internal Python stays snake-case. Implement:

```python
def discover_backend(
    descriptor: BackendDescriptor,
    *,
    environ: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    probe: Callable[[str, Mapping[str, str]], tuple[str | None, str | None]] = probe_version,
) -> BackendInstallation:
    if descriptor.discovery_kind is DiscoveryKind.REMOTE_API:
        return BackendInstallation(
            descriptor.name, None, None, None, "not-applicable",
            descriptor.qualification, (), False,
        )
    requested = environ.get(descriptor.executable_env, descriptor.default_executable)
    resolved = which(requested)
    if resolved is None:
        return BackendInstallation(
            descriptor.name, requested, None, None, "missing", descriptor.qualification,
            (f"executable not found: {requested}",), False,
        )
    probe_environment = {
        key: environ[key]
        for key in ("PATH", "SYSTEMROOT")
        if key in environ
    }
    version, diagnostic = probe(resolved, probe_environment)
    return BackendInstallation(
        descriptor.name,
        requested,
        resolved,
        version,
        "available" if version else "unverified",
        descriptor.qualification,
        (diagnostic,) if diagnostic else (),
        True,
    )
```

`probe_version(executable, environ)` must use `subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5, check=False, env=dict(environ))`; redact and truncate output to 500 characters. Only `PATH` and `SYSTEMROOT`, when present, may reach the child. It must not invoke login, status, prompt, model, or provider operations.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_setup.py tests/test_backends.py -q
uv run ruff check src/reviewctl/setup.py tests/test_setup.py
git add src/reviewctl/setup.py tests/test_setup.py
git commit -m "feat: discover local review backends"
```

### Task 5: Expose `reviewctl setup` and machine-readable help

**Files:**
- Modify: `src/reviewctl/cli.py:595-690,3627-3710`
- Modify: `tests/test_run.py`
- Modify: `docs/HELP-LLM.md`

- [ ] **Step 1: Write failing CLI tests**

Cover:

```text
reviewctl setup discover --format json
reviewctl setup show --format json
reviewctl setup check --backend codex --format json
```

Assert `discover` and `show` return zero with stable JSON; `check` returns zero for an available executable and one for missing/unverified. Assert JSON distinguishes `availability` from `qualification`, contains no credential-shaped environment values, and declares `probePerformed: false` for LLM/provider probes.

- [ ] **Step 2: Verify RED**

Run the new setup parser/integration node IDs. Expected: argparse rejects `setup`.

- [ ] **Step 3: Add parser and handlers**

Add one `setup` parser with required subcommands `discover`, `show`, and `check`. Each accepts `--format human|json`; `check` additionally accepts repeatable `--backend`. Unknown backend names are invocation errors. All commands build the same registry and call `reviewctl.setup`; none writes configuration or calls an LLM.

Extend `help-llm --format json` with:

```json
{
  "commands": {
    "setup": {
      "discover": "reviewctl setup discover --format json",
      "show": "reviewctl setup show --format json",
      "check": "reviewctl setup check --backend NAME --format json"
    }
  },
  "backendSemantics": {
    "availabilityIsNotQualification": true,
    "setupIsLocalOnly": true,
    "setupCallsModels": false
  }
}
```

- [ ] **Step 4: Document and commit**

Document that setup is local, read-only, redacted, and non-qualifying. Run:

```bash
uv run pytest tests/test_setup.py tests/test_run.py -q
uv run ruff check .
git add src/reviewctl/cli.py tests/test_run.py docs/HELP-LLM.md
git commit -m "feat: expose local backend setup diagnostics"
```

### Task 6: Document the seam and lock public compatibility

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `tests/test_public_distribution.py`
- Modify: `tests/test_pilot_plan.py`

- [ ] **Step 1: Add failing documentation assertions**

Add literal substring assertions that the architecture names `BackendRequest`, `BackendExecution`, `BackendCapabilities`, `local-only`, `availability is not qualification`, and `adapters do not decide acceptance`. Assert `HELP-LLM.md` names `reviewctl setup discover`, `reviewctl setup show --format json`, and `reviewctl setup check`; also assert neither document contains `Cursor is supported` nor `Claude is supported`.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_public_distribution.py tests/test_pilot_plan.py -q`

Expected: assertions fail until documentation is updated.

- [ ] **Step 3: Update architecture without copying a roster**

Document the seam, the five compatibility adapters, and the next conformance gate. Do not place model tables, prices, organization policy, credentials, or future support claims in public docs.

- [ ] **Step 4: Run full verification and commit**

Run:

```bash
uv run pytest
uv run ruff check .
git diff --check
git add docs/ARCHITECTURE.md tests/test_public_distribution.py tests/test_pilot_plan.py
git commit -m "docs: define backend ownership and setup boundary"
```

Expected: full suite and lint pass.

### Task 7: Formal review and phase handoff

**Files:**
- Review: `src/reviewctl/backends.py`
- Review: `src/reviewctl/setup.py`
- Review: focused dispatch changes from `src/reviewctl/cli.py`

- [ ] **Step 1: Verify repository evidence**

Run:

```bash
uv run pytest
uv run ruff check .
git diff --check
git status --short
git log --oneline 8f17714..HEAD
```

Expected: no failures and only intentional review artifacts, if any.

- [ ] **Step 2: Run a bounded formal review**

Copy the focused dispatch excerpt to one uniquely named review file so the packet remains at most three files. Run the workspace checkout explicitly with `src/reviewctl/backends.py`, `src/reviewctl/setup.py`, and that dispatch excerpt under the approved proprietary-source policy.

- [ ] **Step 3: Verify and adjudicate**

Run `reviewctl verify <receipt.json>`. Reproduce every material finding in source or tests. For each valid finding, first add a failing regression test, then fix, rerun the full suite, and repeat formal review against the new commit.

- [ ] **Step 4: Record the phase result**

Report branch, exact commit, test count, Ruff result, receipt path, receipt verification, supported-versus-unqualified backend inventory, and the explicit next plan: partial evaluation/fallback/consolidation.

## Plan self-review

- The plan preserves every current public transport name and keeps policy, contract evaluation, fallback, acceptance, and receipt semantics above the adapter boundary.
- Setup is observational only. It neither installs/authenticates nor calls a model, and remote API credentials are never treated as executable names or emitted.
- The phase intentionally ends with all adapters unqualified. Availability proves only that local wiring can be observed; conformance requires later hermetic backend tests.
- BAML remains an architectural influence, not a dependency. The already-native typed contract seam stays in `contracts.py`; partial fragments, completion prompts, fallback, and consolidation belong to the immediately following plan.
- Cursor, Claude Code, ACP, and other native routes are intentionally absent until the backend seam is stable and their source-isolation/evidence behavior has conformance tests.
- Project journals, append-only facts, dimensions, signed exchange bundles, replay protection, quarantine, idempotent import, and optional Potzal carriage are intentionally deferred to the exchange phase. `reviewctl` will own interpretation; no Potzal dependency is introduced.
- Editable execution remains a separate later workflow producing a `ChangeAttempt`, never an approval receipt.
