# OpenRouter Reasoning Effort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenRouter reasoning effort configurable per review profile and per invocation.

**Architecture:** Keep Pi's existing `thinking` setting Pi-specific. Add an OpenRouter-only profile setting, `reasoning_effort`, and `--reasoning-effort` to `reviewctl run`; the CLI flag overrides the profile setting. OpenRouter receives no reasoning field unless an effective value was selected. The existing GLM-specific hardcode is removed.

**Tech Stack:** Python 3.14, argparse, pytest, OpenRouter Responses-compatible request payloads.

---

### Task 1: Specify and validate the public override

**Files:**
- Modify: `tests/test_run.py`
- Modify: `src/reviewctl/cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add tests that prove `--reasoning-effort low` reaches `invoke_openrouter`, overrides profile `reasoning_effort = "medium"`, and rejects unsupported values before a transport call.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `uv run pytest tests/test_run.py -k reasoning_effort -q`

Expected: FAIL because `run` does not accept `--reasoning-effort` and OpenRouter has no configurable reasoning parameter.

- [ ] **Step 3: Implement the parser and precedence**

Add the `--reasoning-effort` choice argument to `run`. Resolve effective effort as explicit CLI value, then profile `reasoning_effort`, then no OpenRouter field.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `uv run pytest tests/test_run.py -k reasoning_effort -q`

Expected: PASS.

### Task 2: Remove GLM hardcoding and preserve request evidence

**Files:**
- Modify: `tests/test_run.py`
- Modify: `src/reviewctl/cli.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing payload tests**

Add tests proving GLM does not receive reasoning unless requested, and a selected effort produces `{"effort": "<value>"}` in the OpenRouter payload.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `uv run pytest tests/test_run.py -k openrouter_reasoning -q`

Expected: FAIL because GLM is forced to `max`.

- [ ] **Step 3: Implement minimal payload behavior and document it**

Remove model-name branching. Pass the effective value into `invoke_openrouter`; emit the reasoning payload only when non-null. Document precedence and recommend `low` for transport checks.

- [ ] **Step 4: Run focused and full verification**

Run: `uv run pytest tests/test_run.py -k 'reasoning_effort or openrouter_reasoning' -q && uv run pytest -q`

Expected: focused tests and full suite pass.
