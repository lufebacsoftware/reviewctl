# Finding Lifecycle Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the project journal a usable append-only finding ledger with stable finding identities, reconstructible status projections, and an audited CLI status transition.

**Architecture:** Keep JSONL as the canonical append-only event stream. Treat legacy `type = "finding"` events as observation events for backward compatibility, add explicit `finding_observed` and `finding_status_changed` events, and rebuild one current finding projection per stable `findingId`. The API derives the stable identity from the normalized finding semantics; the CLI appends status events and never edits prior journal lines.

**Tech Stack:** Python 3.11+, dataclasses, JSONL, `hashlib`, argparse, pytest, Ruff.

---

### Task 1: Define the journal projection and lifecycle contract with failing tests

**Files:**
- Modify: `tests/test_journal.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_cli_front_door.py`

- [x] **Step 1: Add a test that repeated observations collapse to one current finding**

```python
def test_findings_projection_collapses_repeated_observations(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r2",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    findings = journal.findings()

    assert len(findings) == 1
    assert findings[0]["findingId"] == "f1"
    assert findings[0]["firstReviewId"] == "r1"
    assert findings[0]["lastReviewId"] == "r2"
    assert findings[0]["observations"] == 2


def test_finding_status_change_is_projected_without_rewriting_journal(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )
    before = journal.path.read_bytes()
    journal.append(
        {
            "type": "finding_status_changed",
            "findingId": "f1",
            "from": "open",
            "to": "fixed",
            "reason": "patched in commit abc123",
        }
    )

    finding = journal.findings()[0]
    assert finding["status"] == "fixed"
    assert finding["statusReason"] == "patched in commit abc123"
    assert journal.path.read_bytes().startswith(before)


def test_invalid_finding_status_transition_is_rejected(tmp_path: Path) -> None:
    journal = ProjectJournal(tmp_path / "journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "r1",
            "findingId": "f1",
            "status": "open",
            "path": "src/a.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    with pytest.raises(ValueError, match="invalid finding status transition"):
        journal.append(
            {
                "type": "finding_status_changed",
                "findingId": "f1",
                "from": "open",
                "to": "verified",
            }
        )
```

Import `pytest` in the test module. The test intentionally exercises the journal interface before its projection implementation exists.

- [x] **Step 2: Add a test that API observations reuse a stable identity**

```python
def test_client_reuses_finding_identity_across_reviews(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    response = (
        '{"verdict":"approved","findings":[{"severity":"high",'
        '"path":"app.py","line":1,"title":"Handle failure",'
        '"evidence":"e","reproduction":"r"}]}'
    )
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport(response)})

    first = client.review(ReviewRequest(prompt="review one"))
    second = client.review(ReviewRequest(prompt="review two"))

    assert first.status == "accepted"
    assert second.status == "accepted"
    findings = client.journal().findings()
    assert len(findings) == 1
    assert findings[0]["observations"] == 2
    assert findings[0]["firstReviewId"] == first.review_id
    assert findings[0]["lastReviewId"] == second.review_id
```

- [x] **Step 3: Add a test for the CLI status command**

```python
def test_findings_set_status_appends_a_status_event(tmp_path: Path, capsys) -> None:
    journal = ProjectJournal(tmp_path / ".reviewctl/journal.jsonl")
    journal.append(
        {
            "type": "finding_observed",
            "reviewId": "review-1",
            "findingId": "finding-1",
            "status": "open",
            "path": "src/app.py",
            "message": "Handle the error",
            "severity": "high",
        }
    )

    assert run_cli(
        [
            "findings",
            "set-status",
            "--project",
            str(tmp_path),
            "--id",
            "finding-1",
            "--status",
            "fixed",
            "--reason",
            "patched",
            "--format",
            "json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["findingId"] == "finding-1"
    assert payload["status"] == "fixed"
    assert payload["statusReason"] == "patched"
    assert json.loads((tmp_path / ".reviewctl/journal.jsonl").read_text().splitlines()[-1])["type"] == "finding_status_changed"
```

- [x] **Step 4: Run only the new tests and verify the intended RED failure**

Run:

```bash
uv run pytest -q tests/test_journal.py tests/test_api.py::test_client_reuses_finding_identity_across_reviews tests/test_cli_front_door.py::test_findings_set_status_appends_a_status_event
```

Expected: FAIL because the new projection, stable identity, and `findings set-status` interface are not implemented yet.

### Task 2: Implement the append-only journal projection

**Files:**
- Modify: `src/reviewctl/journal.py`
- Modify: `src/reviewctl/errors.py`
- Test: `tests/test_journal.py`

- [x] **Step 1: Define the supported statuses and transition map**

Add module constants:

```python
FINDING_STATUSES = frozenset({"open", "disputed", "fixed", "verified", "dismissed"})
FINDING_TRANSITIONS = {
    "open": frozenset({"disputed", "fixed", "dismissed"}),
    "disputed": frozenset({"open", "fixed", "dismissed"}),
    "fixed": frozenset({"open", "disputed", "verified", "dismissed"}),
    "verified": frozenset({"open", "dismissed"}),
    "dismissed": frozenset({"open"}),
}
```

Add `finding_status(finding_id)` to return the current projected status or `None`, and add `append_status_change(finding_id, status, reason="")` that appends a `finding_status_changed` event only after validating the source finding, target status, and transition.

- [x] **Step 2: Replace raw-event filtering with a projection reducer**

Implement `findings()` by reading events once and reducing all legacy `finding`, `finding_observed`, and `finding_status_changed` events into a dictionary keyed by `findingId`. For an observation, preserve the first observation fields and update `lastReviewId`, `lastObservedAt`, and `observations`. For a status event, update `status`, `statusChangedAt`, and optional `statusReason`. Return findings in first-observation order and filter status after reduction.

Legacy `type = "finding"` events remain accepted as open observations. Events missing a `findingId` are ignored by the projection and reported only through the existing journal corruption diagnostic if their structure is invalid; do not break old journals that contain non-projecting events.

- [x] **Step 3: Verify journal tests pass and preserve legacy behavior**

Run:

```bash
uv run pytest -q tests/test_journal.py
```

Expected: all journal tests pass, including the existing legacy `finding` projection test and the new RED tests.

### Task 3: Give API findings stable identities and record observations

**Files:**
- Modify: `src/reviewctl/api.py`
- Test: `tests/test_api.py`

- [x] **Step 1: Add a private stable identity helper**

Canonicalize only the semantic identity fields (`path`, `line`, `title`, and `reproduction`) with sorted JSON and derive:

```python
def _finding_id(finding: Finding) -> str:
    identity = {"path": finding.path, "line": finding.line, "title": finding.title, "reproduction": finding.reproduction}
    return "finding-" + _digest(json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode())[:24]
```

This keeps evidence and severity changes attached to the same issue while keeping the identity opaque and deterministic inside a project journal.

- [x] **Step 2: Change `_record_findings` to append `finding_observed` events**

Use `_finding_id` for each normalized `Finding`, include `evidence`, `reproduction`, `line`, and `title` in the event, and set `status = "open"` only for a finding first seen by the projection. Re-observation must not reset a finding that is already `fixed`, `verified`, `dismissed`, or `disputed`; the journal reducer owns that rule.

- [x] **Step 3: Include stable IDs in receipts without changing the public `Finding` dataclass**

Serialize receipt findings as the current finding fields plus `findingId`. Keep `ReviewResult.findings` unchanged so existing Python callers do not need to change.

- [x] **Step 4: Run API tests and the full journal/API subset**

Run:

```bash
uv run pytest -q tests/test_journal.py tests/test_api.py
```

Expected: all tests pass, including stable identity reuse and receipt verification.

### Task 4: Add the CLI lifecycle command

**Files:**
- Modify: `src/reviewctl/project_cli.py`
- Modify: `src/reviewctl/cli.py` only if parser dispatch requires it
- Modify: `src/reviewctl/errors.py`
- Test: `tests/test_cli_front_door.py`

- [x] **Step 1: Add `findings set-status` as a nested subcommand**

Keep `reviewctl findings --status ...` backward compatible and add:

```text
reviewctl findings set-status \
  --project . \
  --id finding-... \
  --status fixed \
  [--reason "patched in commit ..."] \
  [--format text|json]
```

The command loads the project journal, calls `append_status_change`, prints the resulting projected finding, and maps missing IDs or invalid transitions to `invalid_request` (exit 2). It must not print source, prompts, credentials, or raw model output.

- [x] **Step 2: Add JSON and text output tests**

Test accepted status change, missing finding, invalid transition, and journal append-only preservation. The JSON response must contain `findingId`, `status`, and `statusReason` when supplied.

- [x] **Step 3: Run the focused CLI tests**

Run:

```bash
uv run pytest -q tests/test_cli_front_door.py
```

Expected: all front-door tests pass.

### Task 5: Update documentation and the handoff

**Files:**
- Modify: `README.md`
- Modify: `docs/HELP-LLM.md`
- Modify: `docs/PI-INTEGRATION.md`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-08-23-reviewctl-open-source-tool-design.md`

- [x] **Step 1: Document the finding lifecycle**

Document the five statuses, the append-only status command, stable finding IDs, and the rule that repeated observations do not reopen or reset a finding.

- [x] **Step 2: Remove stale handoff instructions**

Update the handoff date and replace the old “next safe action is to inspect and commit the Kiro change” paragraph with the completed current state and the next bounded roadmap item. Do not claim federation, signatures, or non-Pi transports are complete.

- [x] **Step 3: Run documentation consistency checks**

Run:

```bash
rg -n "next safe action|2026-08-14|findings set-status|finding_status_changed|finding_observed" README.md docs
git diff --check
```

Expected: no stale handoff instruction remains, lifecycle references agree, and whitespace validation passes.

### Task 6: Review, verify, and commit the iteration

**Files:**
- Review: the complete diff from `4c9d4db`
- Test: all `tests/`
- Evidence: local receipt and review artifacts under the configured private review artifact root

- [x] **Step 1: Run the full verification suite**

```bash
uv run pytest -q
uv run ruff check src tests
git diff --check
```

Expected: zero test failures, Ruff clean, and no whitespace errors.

- [x] **Step 2: Run a local journal lifecycle canary**

Create a temporary project, initialize it, write two observations with the fake transport, change the resulting finding to `fixed`, run `findings --status fixed`, and verify that the journal contains both observation events plus one status event and that no prior line changed.

- [x] **Step 3: Run two-axis review of the diff**

Review `git diff 4c9d4db...HEAD` against `docs/superpowers/specs/2026-08-23-reviewctl-open-source-tool-design.md` and the repository standards. Separately run the requested advisory Pi reviews when the configured models are available. Record findings, fix concrete issues, and rerun the affected tests.

- [x] **Step 4: Commit the bounded iteration**

```bash
git add docs/superpowers/plans/2026-08-23-finding-lifecycle-projection.md \
  src/reviewctl/journal.py src/reviewctl/api.py src/reviewctl/project_cli.py \
  src/reviewctl/errors.py tests/test_journal.py tests/test_api.py \
  tests/test_cli_front_door.py docs/PI-INTEGRATION.md docs/HANDOFF.md \
  docs/superpowers/specs/2026-08-23-reviewctl-open-source-tool-design.md
# Stage only the lifecycle hunks from the already-dirty documentation files.
git add -p README.md docs/HELP-LLM.md
git commit -m "feat: make finding lifecycle append-only"
```

Do not stage the pre-existing unrelated changes in `README.md`, `docs/HELP-LLM.md`, `src/reviewctl/cli.py`, or `tests/test_run.py`; split those hunks before committing.

## Self-review

- The plan changes one bounded subsystem: the project journal and finding lifecycle.
- It preserves the existing JSONL journal and legacy `finding` event readers.
- No mutable database, federation transport, cryptographic signing, or new model backend is included.
- Stable IDs are based on semantic identity rather than mutable evidence/severity.
- Every behavior change has a test-first step and a full verification step.
- The existing spec already requires finding lifecycle states and a rebuildable journal; no requirement in scope is left unassigned.
