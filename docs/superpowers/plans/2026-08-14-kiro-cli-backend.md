# Kiro CLI Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Kiro CLI as a native, locally discovered, unqualified `reviewctl` backend that can use the user's Kiro model access without OpenRouter.

**Architecture:** `reviewctl` will inline the frozen packet into one noninteractive Kiro prompt, run `kiro-cli` from a fresh empty temporary directory with no tools pre-trusted, retain raw/final output plus redacted diagnostics, and recover the Kiro session identifier from that same directory. The adapter reports only observed facts: requested model identity is not promoted to resolved provider/model identity, availability is not qualification, and the model inventory remains runtime-owned by `kiro-cli chat --list-models` rather than copied into this repository.

**Tech Stack:** Python 3.11+, `subprocess`, existing `BackendRegistry`, native review contracts, pytest fake executables, Ruff.

---

### Task 1: Register Kiro without overstating capabilities

**Files:**
- Modify: `src/reviewctl/review_flow.py`
- Modify: `src/reviewctl/cli.py`
- Modify: `tests/test_backends.py`
- Modify: `tests/test_run.py`
- Modify: `tests/test_setup.py`

- [ ] **Step 1: Write failing inventory and parser tests**

Add `kiro` to the exact backend inventory, route/parser choices, receipt transport set, executable override matrix, and setup topology expectations. Assert this descriptor exactly:

```python
BackendDescriptor(
    name="kiro",
    family=BackendFamily.AGENT_CLI,
    discovery_kind=DiscoveryKind.EXECUTABLE,
    executable_env="KIRO_BIN",
    default_executable="kiro-cli",
    capabilities=BackendCapabilities(
        review_read_only=ReadOnlyCapability.ADVISORY,
        editable_execution=False,
        structured_output=False,
        resolved_model_identity=False,
        resolved_provider_identity=False,
        conversation_identity=True,
        usage_reporting=False,
        timeout_control=True,
        tool_control=True,
        source_isolation=SourceIsolation.UNAVAILABLE,
    ),
    qualification="unqualified",
)
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
uv run pytest -q tests/test_backends.py tests/test_setup.py tests/test_run.py -k 'inventory or descriptor or route_transports or transport_choices or executable_override or topology'
```

Expected: failures because `kiro` is absent.

- [ ] **Step 3: Add the minimal registry and CLI wiring**

Add `kiro` to `ROUTE_TRANSPORTS`, `SUPPORTED_REVIEW_TRANSPORTS`, route validation text, `--transport` choices, and `build_backend_registry()`. Register `execute_kiro_backend`; it may initially raise only until Task 2 supplies its tested implementation.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/reviewctl/review_flow.py src/reviewctl/cli.py tests/test_backends.py tests/test_run.py tests/test_setup.py
git commit -m "feat: register kiro backend"
```

### Task 2: Implement isolated Kiro invocation and evidence

**Files:**
- Modify: `src/reviewctl/cli.py`
- Modify: `tests/test_backends.py`
- Modify: `tests/test_run.py`

- [ ] **Step 1: Write failing fake-executable tests**

Create `write_fake_kiro()` in `tests/test_run.py`. It must record argv, cwd, selected environment keys and prompt; emit ANSI-decorated `> RESPONSE` plus a credits footer; implement `chat --list-sessions --format json`; optionally sleep, emit empty output, fail, or return malformed session JSON. Tests must prove:

```python
assert command[:2] == [str(fake_kiro), "chat"]
assert "--no-interactive" in command
assert "--trust-tools=" in command
assert command[command.index("--model") + 1] == "requested-model"
assert Path(observed_cwd).is_relative_to(Path("/private/tmp")) or observed_cwd != str(source.parent)
assert original_source_path not in prompt
assert "--- BEGIN source.py ---" in prompt
```

Also assert exact response extraction, session recovery, timeout `124`, missing executable `127`, empty output preservation, diagnostic redaction, and no ambient API/token variables in the child environment.

- [ ] **Step 2: Run Kiro invocation tests and confirm RED**

Run:

```bash
uv run pytest -q tests/test_run.py -k 'kiro' tests/test_backends.py -k 'kiro'
```

Expected: failures because `invoke_kiro`, normalization, and backend evidence do not exist.

- [ ] **Step 3: Implement the minimal adapter**

Implement these focused helpers in `src/reviewctl/cli.py` before the process wrapper:

```python
def kiro_process_environment(source: Mapping[str, str]) -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "HOME", "TMPDIR", "TMP", "TEMP", "LANG",
               "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR")
    return {key: source[key] for key in allowed if key in source}


def normalize_kiro_output(stdout: bytes) -> str:
    text = ANSI_ESCAPE.sub("", stdout.decode(errors="replace")).replace("\r", "")
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("> ")), None)
    if start is None:
        return ""
    lines[start] = lines[start][2:]
    lines = lines[start:]
    while lines and (not lines[-1].strip() or lines[-1].strip().startswith("▸ Credits:")):
        lines.pop()
    return "\n".join(lines).strip()


def kiro_session_id(payload: bytes, cwd: Path) -> str:
    value = json.loads(payload)
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        return ""
    sessions = value[0].get("sessions")
    if value[0].get("cwd") != str(cwd.resolve()) or type(sessions) is not list or not sessions:
        return ""
    session = sessions[0]
    identifier = session.get("sessionId") if type(session) is dict else None
    return identifier if type(identifier) is str else ""
```

Add `invoke_kiro(...) -> tuple[int, str, PersistedResponse]` with the same keyword fields used by `invoke_pi`, except that it receives no session path. It must use `tempfile.TemporaryDirectory(prefix="reviewctl-kiro-")`, call:

```text
kiro-cli chat --no-interactive --trust-tools= --model MODEL --wrap never INLINE_PACKET
```

and then call `kiro-cli chat --list-sessions --format json` in the same directory. Use `openrouter_packet()` only as the existing generic inline-packet builder; do not call OpenRouter or persist any OpenRouter credential/provider configuration. Record `requestedMaxOutputTokens` with `outputTokenLimitEnforced: false`. Strip only terminal framing known from Kiro, retain raw stdout/stderr separately, and return no response if a session identity cannot be reproduced.

`execute_kiro_backend()` writes final `response.md` only when nonempty and maps request/raw output/final response/stderr into `BackendEvidence`.

- [ ] **Step 4: Make unresolved identity explicit in orchestration**

Use the selected backend descriptor in `run_review()`. Enforce model mismatch and serialize `model.resolved` only when `capabilities.resolved_model_identity` is true; enforce provider identity only when that capability is true. Kiro therefore persists its requested route while leaving resolved model/provider null rather than inventing them.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the Step 2 command, then:

```bash
uv run pytest -q tests/test_review_flow.py tests/test_backends.py tests/test_run.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/reviewctl/cli.py tests/test_backends.py tests/test_run.py
git commit -m "feat: invoke kiro reviews"
```

### Task 3: Document Kiro as available but unqualified

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/HELP-LLM.md`
- Modify: `docs/PROJECT-INTEGRATION.md`
- Modify: `docs/superpowers/specs/2026-08-12-global-best-match-review-design.md`
- Modify: `tests/test_public_distribution.py`

- [ ] **Step 1: Write failing documentation assertions**

Require public docs to state all of the following without a static model table: Kiro is a registered native adapter; `KIRO_BIN` overrides `kiro-cli`; setup availability remains non-qualifying; the current model inventory is read with `kiro-cli chat --list-models --format json`; formal Kiro runs use no pre-trusted tools and an inline frozen packet; organization policy/evidence owns qualification.

- [ ] **Step 2: Run documentation tests and confirm RED**

```bash
uv run pytest -q tests/test_public_distribution.py -k 'backend or help or project'
```

Expected: failure because Kiro guidance is absent.

- [ ] **Step 3: Add bounded documentation**

Document example routing as `--route kiro:MODEL_ID`. Select `MODEL_ID` from the runtime inventory returned by `kiro-cli chat --list-models --format json`; do not copy concrete current model names, prices, credit multipliers, provider commands, or qualification tables into repository or project instruction files.

- [ ] **Step 4: Run documentation tests and confirm GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add docs/ARCHITECTURE.md docs/HELP-LLM.md docs/PROJECT-INTEGRATION.md docs/superpowers/specs/2026-08-12-global-best-match-review-design.md tests/test_public_distribution.py
git commit -m "docs: describe kiro backend boundary"
```

### Task 4: Verify, smoke-test, and review externally

**Files:**
- Create outside repository: `<review-request.md>`
- Create outside repository: `<artifact-root>/**`

- [ ] **Step 1: Run complete local verification**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check src/reviewctl/cli.py src/reviewctl/review_flow.py tests/test_backends.py tests/test_run.py tests/test_setup.py tests/test_public_distribution.py
git diff --check
```

- [ ] **Step 2: Run one synthetic live Kiro smoke test**

Use a low-credit Kiro model discovered at runtime, a synthetic source file, `findings-json`, and no organization source policy. Verify the resulting receipt with the same checkout. This proves the installed Kiro session works but does not qualify any Kiro model.

- [ ] **Step 3: Obtain independent formal reviews**

Run two policy-approved Codex reviews through `reviewctl`, using different models, against the same commit and bounded adapter/tests/docs. Verify both receipts and fix only reproducible findings with new regression tests.

- [ ] **Step 4: Integrate without disturbing main's local Gemini edits**

Merge with reversible autostash only after both reviewers approve the same SHA. Re-run the full suite and receipt verification on `main`; preserve the existing uncommitted `README.md`, `src/reviewctl/cli.py`, and `tests/test_run.py` Gemini 3.7 edits exactly.
