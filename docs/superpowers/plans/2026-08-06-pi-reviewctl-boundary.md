# Pi and reviewctl Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document and test a clean boundary where `pi` is interactive and `reviewctl` owns formal reviews and their archived evidence.

**Architecture:** Keep `pi` optional and outside the receipt contract. Add one integration guide that promotes a stable prompt and bounded source packet into `reviewctl`; update the public project-integration documentation to point to that guide. Use documentation tests to prevent the two channels from being conflated.

**Tech Stack:** Markdown, Python, pytest, existing `reviewctl` CLI and receipt verifier.

---

### Task 1: Guard the boundary with documentation tests

**Files:**
- Modify: `tests/test_pilot_plan.py`

- [ ] **Step 1: Write the failing tests**

Add tests that read `docs/PI-INTEGRATION.md` and assert it names `pi` as interactive, `reviewctl` as the formal evidence owner, requires `reviewctl verify`, and forbids treating a `pi` transcript as approval.

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/test_pilot_plan.py -q`

Expected: FAIL because `docs/PI-INTEGRATION.md` does not exist yet.

### Task 2: Publish the integration contract

**Files:**
- Create: `docs/PI-INTEGRATION.md`
- Modify: `README.md`
- Modify: `docs/PROJECT-INTEGRATION.md`

- [ ] **Step 1: Add the integration guide**

Document isolated `pi` session directories, a stable prompt handoff, bounded `--file` attachments, persisted `reviewctl` receipts, `reviewctl verify`, and optional `--seal-to` archival. State explicitly that `pi` output is exploratory only.

- [ ] **Step 2: Link the guide from public docs**

Add the guide to the README documentation links and the project-integration guide without adding model rosters, prices, or private paths.

- [ ] **Step 3: Run the focused tests**

Run: `uv run pytest tests/test_pilot_plan.py -q`

Expected: PASS.

### Task 3: Verify the repository and commit

**Files:**
- No additional files.

- [ ] **Step 1: Run the complete verification suite**

Run: `uv run pytest -q && uv run ruff check . && git diff --check`

Expected: 211 or more tests pass, Ruff reports no errors, and `git diff --check` is empty.

- [ ] **Step 2: Commit the bounded change**

Run: `git add docs README.md tests/test_pilot_plan.py && git commit -m "docs: define pi and reviewctl boundary"`
