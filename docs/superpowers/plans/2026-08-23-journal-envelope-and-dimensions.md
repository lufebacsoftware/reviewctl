# Journal Envelope and Review Dimensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the local project journal a portable, verifiable envelope and record versioned review dimensions so later aggregation and federation do not require a data-model rewrite.

**Architecture:** Keep JSONL as the canonical append-only stream. Existing legacy events remain readable; new events carry a versioned envelope with project/origin identity, contiguous local sequence, previous-line digest, and event digest. A private local identity file supplies an origin identity while the project configuration supplies a portable project identity. Review dimensions are normalized at the project API boundary, recorded in receipts and journal facts, and queried from the rebuildable projection; they do not change the findings response contract yet.

**Tech Stack:** Python 3.11+, TOML, JSONL, SHA-256, POSIX `fcntl.flock`, argparse, pytest, Ruff.

---

## Contract decisions

- `ProjectId` is non-secret and portable only when explicitly declared. `reviewctl init` writes it in `reviewctl.toml` as `project.id`; an existing project without one receives a deterministic local fallback, but `doctor` marks it non-portable and sharing/migration requires an explicit ID before a non-empty journal is moved between machines.
- `OriginId` is non-secret but machine-local. It is stored in `.reviewctl/identity.json` with mode `0600` and is never required to be committed.
- New journal events contain `schemaVersion = 1`, `projectId`, `originId`, `sequence`, `previousEventSha256`, and `eventSha256`. `eventSha256` covers the canonical event with `eventSha256` removed; the newline is not part of the digest.
- Sequence numbers are contiguous per local journal. Appends lock the journal, reread the head, calculate the next sequence, and fsync the complete line before releasing the lock. If a POSIX lock primitive is unavailable or cannot be acquired, the operation returns `journal_unavailable` and never appends without continuity evidence.
- Legacy events without an envelope remain readable. The first versioned event starts a new contiguous sequence and links to the canonical digest of the preceding parsed legacy event: sorted compact JSON, UTF-8, with no trailing newline. Verification reports legacy history as `compatibility: legacy-prefix`, not as a failure.
- Invalid JSON, mixed project/origin identity, sequence gaps, previous-digest mismatches, or event-digest mismatches produce `journal_corrupt` and never produce a partial accepted projection.
- Common dimensions are `correctness`, `architecture`, `security`, `privacy`, `financial`, `fiscal`, `release`, `public-api`, and `ui-accessibility`. A project may add names only as `custom.<slug>`, with a maximum length of 64 characters and at most 32 dimensions per review; custom names cannot redefine common names.
- Dimension order is canonical sorted order. Duplicate names, empty names, and non-string values are configuration/request errors.
- Dimensions are metadata in this slice. Contract coverage and model qualification remain future layers; an observed review does not claim a dimension was semantically satisfied merely because it was requested.

### Task 1: Add failing tests for identity and journal envelope

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_journal.py`
- Modify: `tests/test_cli_front_door.py`

- [x] **Step 1: Test project identity and local origin creation**

```python
def test_init_writes_portable_project_id_and_local_origin(tmp_path: Path) -> None:
    assert run_cli(["init", "--project", str(tmp_path)]) == 0

    config = load_config(tmp_path / "reviewctl.toml", user_path=None)
    identity = json.loads((tmp_path / ".reviewctl/identity.json").read_text())

    assert config.project.project_id.startswith("project-")
    assert identity["projectId"] == config.project.project_id
    assert identity["originId"].startswith("origin-")
    assert (tmp_path / ".reviewctl/identity.json").stat().st_mode & 0o777 == 0o600
```

- [x] **Step 2: Test the versioned envelope and contiguous sequence**

```python
def test_new_events_have_identity_sequence_and_continuity(tmp_path: Path) -> None:
    journal = ProjectJournal(
        tmp_path / "journal.jsonl", project_id="project-1", origin_id="origin-1"
    )

    first = journal.append({"type": "review_started", "reviewId": "r1"})
    second = journal.append({"type": "review_finished", "reviewId": "r1"})

    assert first["schemaVersion"] == 1
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["projectId"] == second["projectId"] == "project-1"
    assert first["originId"] == second["originId"] == "origin-1"
    assert second["previousEventSha256"] == first["eventSha256"]
    assert journal.verify() == []
```

- [x] **Step 3: Test tamper, gap, and identity detection**

```python
def test_journal_verify_reports_tamper_gap_and_identity_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = ProjectJournal(path, project_id="project-1", origin_id="origin-1")
    journal.append({"type": "review_started", "reviewId": "r1"})
    journal.append({"type": "review_finished", "reviewId": "r1"})

    lines = path.read_text().splitlines()
    second = json.loads(lines[1])
    second["sequence"] = 4
    lines[1] = json.dumps(second, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    violations = journal.verify()

    assert any("sequence" in violation for violation in violations)
    assert any("event digest" in violation for violation in violations)
```

- [x] **Step 4: Run the new tests and verify RED**

```bash
uv run pytest -q tests/test_config.py::test_init_writes_portable_project_id_and_local_origin tests/test_journal.py::test_new_events_have_identity_sequence_and_continuity tests/test_journal.py::test_journal_verify_reports_tamper_gap_and_identity_mismatch
```

Expected: failures because the identity store, envelope fields, and `verify()` interface do not exist yet.

### Task 2: Implement stable project/origin identity

**Files:**
- Create: `src/reviewctl/identity.py`
- Modify: `src/reviewctl/config.py`
- Modify: `src/reviewctl/setup.py` only if setup discovery needs identity metadata
- Modify: `src/reviewctl/project_cli.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli_front_door.py`

- [x] **Step 1: Add `ProjectSettings.project_id` and validate explicit IDs**

Accept `project.id` in TOML, validate the portable identifier with the same safe ASCII rule used for review IDs, and preserve existing configs by deriving a local `project-<sha256>` fallback from the resolved project configuration path. Mark the fallback in doctor output as `portableProjectId: false`; explicit IDs report `true`. Never silently rewrite a non-empty journal from a local fallback to an explicit ID.

- [x] **Step 2: Add `ProjectIdentityStore`**

Implement a small module that creates `.reviewctl/identity.json` once, with `projectId`, `originId`, `createdAt`, and `schemaVersion`. Use private directory/file modes and atomic temporary-file replacement. If an existing identity has a different explicit project ID, return `journal_corrupt` rather than silently changing origin history.

- [x] **Step 3: Make `init` write the project ID and create the local identity**

Generate `project-<24 hex chars>` only for a new template, keep `--force` explicit, and create the identity after writing the config. Keep credentials and model names out of the identity file.

- [x] **Step 4: Run identity/config tests and Ruff**

```bash
uv run pytest -q tests/test_config.py tests/test_cli_front_door.py
uv run ruff check src/reviewctl/identity.py src/reviewctl/config.py src/reviewctl/project_cli.py tests/test_config.py tests/test_cli_front_door.py
```

### Task 3: Implement and verify the journal envelope

**Files:**
- Modify: `src/reviewctl/journal.py`
- Modify: `src/reviewctl/api.py`
- Modify: `src/reviewctl/errors.py`
- Modify: `tests/test_journal.py`
- Modify: `tests/test_api.py`

- [x] **Step 1: Add canonical event and envelope digest helpers**

Normalize an event after assigning `eventId`, `at`, and `reviewId`. Set `schemaVersion`, identity, sequence, and previous digest. Compute `eventSha256` from sorted compact JSON with only `eventSha256` omitted. Do not hash the trailing newline. The event returned by `append` must exactly match the persisted event.

- [x] **Step 2: Serialize appends under a journal lock**

Open the journal with append/create flags, lock the descriptor with POSIX `fcntl.flock`, reread the final non-empty line, calculate the next sequence and previous digest, write one complete UTF-8 line, fsync, and unlock in `finally`. A missing or failed lock returns `journal_unavailable` rather than writing without continuity evidence.

- [x] **Step 3: Verify structural continuity during reads**

Add `ProjectJournal.verify() -> list[str]` and make `read_with_diagnostic()` call it after JSON parsing. Verify schema version, project/origin identity, contiguous sequence, previous digest, and event digest for versioned events. Accept a legacy prefix before the first versioned event and report it as a compatibility fact rather than a violation.

- [x] **Step 4: Pass project/origin identity from `ReviewClient`**

Load the identity store in `ReviewClient.from_project` and construct `ProjectJournal` with its IDs. Review receipts and packet metadata record the project/origin IDs and the journal head sequence, but never include the origin identity as a secret or provider credential.

- [x] **Step 5: Run journal/API tests**

```bash
uv run pytest -q tests/test_journal.py tests/test_api.py
```

### Task 4: Add the journal verification CLI

**Files:**
- Modify: `src/reviewctl/project_cli.py`
- Modify: `src/reviewctl/cli.py` only if top-level dispatch requires it
- Modify: `tests/test_cli_front_door.py`
- Modify: `docs/HELP-LLM.md`

- [x] **Step 1: Add `reviewctl journal verify`**

Expose a read-only command that prints `{valid, projectId, originId, sequence, compatibility, violations}` as JSON. Return exit 0 for a valid legacy-prefix or versioned journal and exit 5 for corruption. It must not repair, truncate, or rewrite the journal.

- [x] **Step 2: Test valid, legacy, tampered, and sequence-gap journals**

The tests must assert that the command reports violations without changing the journal bytes.

- [ ] **Step 3: Document the command and diagnostics**

Explain that checksum/continuity proves local journal integrity, not authorship; signatures remain a later federation layer.

### Task 5: Add versioned review dimensions

**Files:**
- Modify: `src/reviewctl/config.py`
- Modify: `src/reviewctl/api.py`
- Modify: `src/reviewctl/project_cli.py`
- Modify: `src/reviewctl/journal.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_cli_front_door.py`
- Modify: `README.md`
- Modify: `docs/HELP-LLM.md`
- Modify: `docs/HANDOFF.md`

- [x] **Step 1: Normalize dimensions at the config boundary**

Add `dimensions = ["correctness"]` to a profile, allow `ReviewRequest.dimensions` to override it only by adding explicit names, and canonicalize to sorted unique strings. Reject duplicates, empty values, invalid names, names not prefixed with `custom.` outside the common set, more than 32 dimensions, and attempts to remove a project-required dimension.

- [x] **Step 2: Record dimensions in receipts and journal events**

Persist `dimensions`, `dimensionSchemaVersion = 1`, and `dimensionCoverage = {requested, observed: [], unresolved: requested}`. Do not call requested dimensions satisfied based only on a model response in this slice.

- [x] **Step 3: Add dimension filters to the project findings/status views**

Support `reviewctl findings --dimension security` and `reviewctl status --dimension security`. Filtering must use journal metadata and remain deterministic after projection rebuild.

- [x] **Step 4: Test configuration precedence, receipts, and aggregation**

Cover project-required dimensions, profile additions, duplicate rejection, stable sorted serialization, and filtering across two reviews.

### Task 6: External review, canaries, and closure

**Files:**
- Review: the complete diff from `18e1022`
- Spec: `docs/ARCHITECTURE.md`, `docs/adr/0001-append-only-journals.md`, and this plan
- Evidence: receipts under the configured private review artifact root

- [x] **Step 1: Run Ox-alpha, Muse, and Qwen 3.8 reviews**

Use bounded `reviewctl` receipts with at most three attached source files per round. Use `pi:openrouter/stealth/ox-alpha`, `pi:openrouter/meta/muse-spark-1.2-contributor`, and `pi:openrouter/qwen/qwen3.8-max` when the local catalog and route are available. A timeout or custom-model warning is recorded as unavailable/advisory, never approval.

- [x] **Step 2: Reproduce every concrete finding locally**

For each external finding, write a failing regression test, fix the root cause, rerun the focused test, then run the full suite. Do not merge model consensus without repository evidence.

- [x] **Step 3: Run final verification and a two-machine simulation**

```bash
uv run pytest -q
uv run ruff check src tests
git diff --check
```

Simulate two origin journals for one ProjectId, verify each locally, and confirm the current implementation does not claim federation or signatures.

- [x] **Step 4: Update handoff and commit only bounded hunks**

Document implemented envelope/dimension behavior, known limitations, model receipt paths, and the remaining signed export/import work. Preserve pre-existing unrelated changes in the dirty checkout.

## Self-review

- This plan is split into one foundational envelope slice and one metadata/dimension slice.
- It preserves legacy journal reads and does not introduce a database or Potzal dependency.
- It distinguishes integrity/continuity from signatures and identity/authorship.
- It records requested dimensions without falsely claiming semantic coverage.
- Every behavior change has a failing-test step, a focused verification step, and a final full-suite gate.
