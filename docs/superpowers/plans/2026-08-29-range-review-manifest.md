# Range Review Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed, deterministic `reviewctl range-review` manifest phase that freezes a `base..head` Git range and emits bounded, auditable chunks before any model request.

**Architecture:** A focused `reviewctl.range_review` module owns Git identity resolution, canonical diff capture, and deterministic chunk manifests. The CLI only validates arguments, writes the manifest as a private artifact, and prints its path; it does not imply that a model reviewed the range. Full chunk transports and aggregate receipts remain a later phase and must consume this immutable manifest.

**Tech Stack:** Python 3.14, `argparse`, `subprocess` with argument arrays, SHA-256, canonical JSON, pytest, Ruff.

---

### Task 1: Define the range identity and chunk manifest API

**Files:**
- Create: `src/reviewctl/range_review.py`
- Test: `tests/test_range_review.py`

- [ ] **Step 1: Write the failing unit tests** for a three-file diff: the builder must record repository root, exact base/head, merge base, `base..head`, context lines, `chunkingVersion`, canonical diff digest, ordered chunks, and a digest for each chunk. Add tests that reject a missing commit, a non-positive context value, an empty range when `allow_empty` is false, and a chunk whose payload exceeds the configured byte limit.

- [ ] **Step 2: Run the focused tests and confirm RED** with `uv run pytest -q tests/test_range_review.py`.

- [ ] **Step 3: Implement the minimal pure API.** Define `RangeReviewError`, immutable `RangeIdentity`, `RangeChunk`, and `RangeManifest` dataclasses plus `build_range_manifest(repository, base, head, context_lines=3, max_chunk_bytes=128*1024, allow_empty=False)`. Resolve each revision using `git rev-parse --verify <revision>^{commit}`, compute `git merge-base`, and capture one canonical `git diff --binary --full-index --no-ext-diff --unified=<context> <base> <head>` byte stream. Hash the exact bytes with SHA-256, split only at complete `diff --git` file sections, reject a single section larger than `max_chunk_bytes`, and assign stable zero-based indices and SHA-256 chunk IDs. Sort no paths after Git emits them; the canonical command output is the ordering authority.

- [ ] **Step 4: Run the focused tests and confirm GREEN** with the same pytest command.

- [ ] **Step 5: Commit** `git add src/reviewctl/range_review.py tests/test_range_review.py && git commit -m "feat: add deterministic range manifest builder"`.

### Task 2: Expose a manifest-only CLI mode

**Files:**
- Modify: `src/reviewctl/cli.py` near parser construction and command handlers
- Modify: `tests/test_run.py` for subprocess and failure-mode coverage

- [ ] **Step 1: Write failing CLI tests** asserting `reviewctl range-review --repository <repo> --base <sha> --head <sha> --output <path>` writes canonical JSON with `status: "manifest-created"`, the exact identity fields, and no `receipt` or approval field. Add tests for changed head and oversized-file failures; the command must return a non-zero status and not write a misleading manifest.

- [ ] **Step 2: Run those tests and confirm RED** with `uv run pytest -q tests/test_run.py -k range_review`.

- [ ] **Step 3: Implement `write_range_manifest` and parser arguments.** Validate repository as a directory, require `--base`, `--head`, and `--output`, accept `--context-lines` and `--max-chunk-bytes` with positive-integer validation, call the module builder, add `generatedAt` only outside the hashed identity, and write via the existing confined/private writer. Emit the output path only on success. Map builder errors to an actionable `reviewctl: ...` diagnostic and never invoke a model transport.

- [ ] **Step 4: Run targeted tests and confirm GREEN** with `uv run pytest -q tests/test_run.py -k range_review`.

- [ ] **Step 5: Commit** `git add src/reviewctl/cli.py tests/test_run.py && git commit -m "feat: expose range review manifest command"`.

### Task 3: Lock the contract with fixtures and documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-08-29-reviewctl-operating-modes.md`
- Create: `tests/fixtures/range-review/README.md`
- Test: `tests/test_range_review.py`

- [ ] **Step 1: Add fixture-driven tests** for a three-commit range, an unchanged empty range, and a changed-head retry. Assert that two invocations over the same commits have equal identity and chunk digests even if their output files differ.

- [ ] **Step 2: Update the operating-modes spec** to mark only “manifest builder” as implemented, retain full per-chunk transport and aggregate receipt verification as pending, and document that `status: manifest-created` is planning evidence—not review evidence.

- [ ] **Step 3: Run `uv run pytest -q tests/test_range_review.py tests/test_run.py -k range_review` and `uv run ruff check src tests`**.

- [ ] **Step 4: Commit** `git add docs/superpowers/specs/2026-08-29-reviewctl-operating-modes.md tests/fixtures/range-review tests/test_range_review.py && git commit -m "docs: define range manifest evidence boundary"`.

### Task 4: Verify the exact change and stop before model routing

**Files:**
- No additional source changes unless a review finding is independently reproduced.

- [ ] **Step 1: Run the complete checks:** `uv run pytest -q`, `uv run ruff check src tests`, and `git diff --check`.

- [ ] **Step 2: Run an exact-head Codex review through the workspace `reviewctl` checkout**, attaching only the new module, focused tests, CLI excerpt, and spec; persist and verify the receipt with `reviewctl verify`.

- [ ] **Step 3: Adjudicate findings against source/tests.** Fix only reproduced issues, rerun the complete checks, and repeat the exact-head review if the commit changes.

- [ ] **Step 4: Report the manifest command and evidence, explicitly leaving chunk model execution and aggregate approval unimplemented.**

