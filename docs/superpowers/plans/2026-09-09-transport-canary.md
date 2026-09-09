# Transport Canary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a profile-backed synthetic transport canary that persists an auditable operability report beside its receipt.

**Architecture:** A small `transport_canary` module owns the synthetic packet and report schema. The CLI command resolves a normal named profile, delegates execution to the existing `run_review` path, discovers its new receipt through existing fingerprints, then writes the report without changing provider routes or policy.

**Tech Stack:** Python 3.14, argparse, existing reviewctl receipts, pytest, Ruff.

---

### Task 1: Define the canary packet and report contract

**Files:**
- Create: `src/reviewctl/transport_canary.py`
- Test: `tests/test_transport_canary.py`

- [ ] **Step 1: Write failing contract tests**

```python
def test_canary_prompt_requires_the_synthetic_file_declaration() -> None:
    assert '"reviewedFiles":["canary.py"]' in canary_prompt()


def test_canary_report_binds_the_receipt_and_profile() -> None:
    report = build_canary_report(...)
    assert report["kind"] == "transport-canary"
    assert report["profile"]["name"] == "llm"
```

- [ ] **Step 2: Run the test to verify RED**

Run: `uv run pytest tests/test_transport_canary.py -q`
Expected: FAIL because `reviewctl.transport_canary` does not exist.

- [ ] **Step 3: Add the minimal pure module**

```python
CANARY_SOURCE_NAME = "canary.py"

def canary_prompt() -> str: ...

def build_canary_report(... ) -> dict[str, object]: ...
```

The module must contain no subprocess or filesystem execution.

- [ ] **Step 4: Run the focused tests to verify GREEN**

Run: `uv run pytest tests/test_transport_canary.py -q`
Expected: PASS.

### Task 2: Add the command through the existing review controller

**Files:**
- Modify: `src/reviewctl/cli.py`
- Modify: `tests/test_run.py`
- Modify: `tests/test_cli_front_door.py`

- [ ] **Step 1: Write failing CLI tests**

Use a temporary TOML profile containing `llm:accepted` and `LLM_BIN` fake.
Assert that `transport-canary --profile llm` creates one receipt and one
`transport-canary.json`; its report must carry route/profile provenance and the
receipt SHA-256. Add a failing-profile case that exits with parser error and
does not create a report.

- [ ] **Step 2: Run the focused tests to verify RED**

Run: `uv run pytest tests/test_run.py tests/test_cli_front_door.py -q`
Expected: FAIL because the parser lacks `transport-canary`.

- [ ] **Step 3: Implement the command**

Add `transport-canary` with `--profile`, optional `--config`,
`--artifact-root`, `--timeout-seconds`, and `--max-output-tokens`. Build a
normal `run` namespace with synthetic source, `findings-json`, one attempt,
and `--require-reviewed-files`; call `run_review`; find the one new receipt;
write the canonical report beside it using `write_private_exclusive`.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `uv run pytest tests/test_run.py tests/test_cli_front_door.py tests/test_transport_canary.py -q`
Expected: PASS.

### Task 3: Document and validate operation

**Files:**
- Modify: `README.md`
- Modify: `docs/TESTING.md`

- [ ] **Step 1: Document the command**

Document a command such as:

```bash
reviewctl transport-canary --profile code \
  --artifact-root ~/Code/reviews/reviewctl-canaries
```

State that it is a synthetic, paid/provider-backed availability check; a valid
receipt records one execution rather than provider qualification or merge
approval.

- [ ] **Step 2: Run repository gates**

Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -m "unit or contract" --cov=reviewctl --cov-branch --cov-report=term-missing
uv build
git diff --check
```

Expected: all pass with 100% coverage.
