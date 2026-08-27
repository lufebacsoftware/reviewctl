# Reviewctl Main Regularization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standardize reviewctl on Python 3.14 and restore the existing 100% statement and branch
coverage gate without weakening policy or disturbing the primary checkout.

**Architecture:** Work only on `chore/regularize-main` in the isolated `regularize-main`
worktree. First align all runtime/tooling declarations, then make the existing formatter delta a
separate mechanical commit, then close coverage by owner surface using tests only. Production
changes are allowed only when a new test proves a concrete defect.

**Tech Stack:** Python 3.14, uv, pytest 8, pytest-cov/coverage.py, Ruff `py314`, GitHub Actions.

---

## File Map

- Runtime/tooling ownership: `.python-version`, `pyproject.toml`, `uv.lock`,
  `.github/workflows/ci.yml`, `.github/workflows/release.yml`,
  `tests/test_public_distribution.py`.
- Mechanical format baseline: the 12 files listed in Task 2 only.
- API/front door coverage: `tests/test_api.py`, `tests/test_artifacts.py`,
  `tests/test_config.py`, new `tests/test_dimensions.py`, `tests/test_cli_front_door.py`.
- GitHub/journal coverage: `tests/test_github_contracts.py`, `tests/test_github_source.py`,
  `tests/test_github_publisher.py`, new `tests/test_identity.py`, `tests/test_journal.py`,
  `tests/test_github_cli.py`.
- Contract/transport coverage: `tests/test_contracts.py`, `tests/test_pi_transport.py`,
  `tests/test_review_flow.py`.
- CLI orchestration coverage: `tests/test_run.py`.

## Task 1: Make Python 3.14 a Tested Repository Contract

**Files:**

- Create: `.python-version`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Test: `tests/test_public_distribution.py`

- [ ] **Step 1: Add the failing cross-file contract test**

Add a test that reads the repository files and requires one exact version policy:

```python
def test_python_tooling_targets_314_consistently() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()

    assert project["project"]["requires-python"] == ">=3.14"
    assert project["tool"]["ruff"]["target-version"] == "py314"
    assert lock["requires-python"] == ">=3.14"
    assert (ROOT / ".python-version").read_text() == "3.14\n"
    assert 'python-version: "3.14"' in ci
    assert 'python-version: "3.14"' in release
    assert 'python-version: "3.12"' not in ci + release
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --python 3.14 pytest tests/test_public_distribution.py::test_python_tooling_targets_314_consistently -q
```

Expected: failure because the current package, Ruff, lock, workflows, and `.python-version` do
not yet agree on 3.14.

- [ ] **Step 3: Apply the minimal version migration**

Make these exact changes:

```toml
# pyproject.toml
requires-python = ">=3.14"

[tool.ruff]
target-version = "py314"
```

Set both workflow `python-version` values to `"3.14"`, create `.python-version` with `3.14`, and
regenerate only lock metadata with:

```bash
uv lock --python 3.14
```

- [ ] **Step 4: Verify GREEN and the existing runtime baseline**

Run:

```bash
uv sync --locked --python 3.14
uv run --python 3.14 pytest tests/test_public_distribution.py -q
uv run --python 3.14 pytest -q
uv build
```

Expected: the contract test and all 1,379 pre-regularization tests pass; build exits zero.

- [ ] **Step 5: Commit the migration**

```bash
git add .python-version pyproject.toml uv.lock .github/workflows/ci.yml \
  .github/workflows/release.yml tests/test_public_distribution.py
git commit -m "chore: require Python 3.14"
```

## Task 2: Isolate the Mechanical Ruff Format Delta

**Files:**

- Modify: `src/reviewctl/api.py`
- Modify: `src/reviewctl/cli.py`
- Modify: `src/reviewctl/contracts.py`
- Modify: `src/reviewctl/github.py`
- Modify: `src/reviewctl/github_publisher.py`
- Modify: `src/reviewctl/identity.py`
- Modify: `src/reviewctl/journal.py`
- Modify: `src/reviewctl/pi_transport.py`
- Modify: `src/reviewctl/project_cli.py`
- Modify: `src/reviewctl/review_flow.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_cli_front_door.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_github_cli.py`
- Modify: `tests/test_github_source.py`

- [ ] **Step 1: Reproduce the exact format failure**

```bash
uv run --python 3.14 ruff format --check .
```

Expected: exactly the 15 files above are reported.

- [ ] **Step 2: Format only those files**

```bash
uv run --python 3.14 ruff format \
  src/reviewctl/api.py src/reviewctl/cli.py src/reviewctl/contracts.py \
  src/reviewctl/github.py src/reviewctl/github_publisher.py \
  src/reviewctl/identity.py src/reviewctl/journal.py src/reviewctl/pi_transport.py \
  src/reviewctl/project_cli.py src/reviewctl/review_flow.py tests/test_api.py tests/test_cli_front_door.py \
  tests/test_config.py tests/test_github_cli.py tests/test_github_source.py
```

- [ ] **Step 3: Verify formatter-only safety**

```bash
uv run --python 3.14 ruff check .
uv run --python 3.14 ruff format --check .
uv run --python 3.14 pytest -q
git diff --check
```

Expected: all commands pass and no file outside the list changed.

- [ ] **Step 4: Commit the mechanical delta**

```bash
git add src/reviewctl/api.py src/reviewctl/cli.py src/reviewctl/contracts.py \
  src/reviewctl/github.py src/reviewctl/github_publisher.py \
  src/reviewctl/identity.py src/reviewctl/journal.py src/reviewctl/pi_transport.py \
  src/reviewctl/project_cli.py src/reviewctl/review_flow.py tests/test_api.py tests/test_cli_front_door.py \
  tests/test_config.py tests/test_github_cli.py tests/test_github_source.py
git commit -m "style: restore Ruff format baseline"
```

## Task 3: Close Contract and Pi Transport Coverage

**Files:**

- Test: `tests/test_contracts.py`
- Test: `tests/test_pi_transport.py`
- Production files remain unchanged unless a test proves a defect.

- [ ] **Step 1: Capture RED module coverage**

```bash
uv run --python 3.14 pytest tests/test_contracts.py tests/test_pi_transport.py \
  --cov=reviewctl.contracts --cov=reviewctl.pi_transport --cov-branch \
  --cov-report=term-missing --cov-fail-under=100
```

Expected: tests pass behaviorally, coverage fails. Baseline gaps are 9 statements/7 branches in
`contracts.py` and 35 statements/16 branches in `pi_transport.py`.

- [ ] **Step 2: Add exact scalar/container contract cases**

Add tests named:

```text
test_require_string_json_object_keys_rejects_tuple_subclass
test_has_exact_json_scalar_types_rejects_string_integer_float_and_container_subclasses
test_findings_required_fields_requires_exact_boolean
test_contains_surrogate_detects_dictionary_keys
test_findings_contract_contains_prepared_and_json_serialization_failures
```

Use small hostile subclasses whose conversion/equality methods raise, existing
`ContractContext` fixtures, and monkeypatch `canonical_json` only for the two contained exception
paths. Assert stable `prepared-contract` or `invalid-json` violations rather than merely calling
the line.

- [ ] **Step 3: Add process and response-shape Pi cases**

Add tests named:

```text
test_run_process_returns_success
test_run_process_terminates_then_kills_a_timed_out_group
test_run_process_tolerates_a_missing_process_group
test_run_process_maps_communicate_oserror_to_127
test_text_blocks_accepts_string_and_rejects_nonlist
test_normalize_response_preserves_noncanonical_fence
test_usage_rejects_nonmapping_and_invalid_numbers
test_persisted_response_skips_malformed_events_and_reads_agent_end
test_pi_transport_attaches_source_files_to_packet
test_pi_transport_uses_default_process_failure_diagnostic
```

Use a fake `Popen` object for deterministic communicate/timeout/kill/wait assertions and the
existing injected `run_process` seam for `PiTransport`. The tests must assert returned bytes,
timeout flags, command packet, persisted provider/model, and diagnostic.

- [ ] **Step 4: Verify GREEN and commit**

```bash
uv run --python 3.14 pytest tests/test_contracts.py tests/test_pi_transport.py \
  --cov=reviewctl.contracts --cov=reviewctl.pi_transport --cov-branch \
  --cov-report=term-missing --cov-fail-under=100
uv run --python 3.14 ruff check tests/test_contracts.py tests/test_pi_transport.py
git add tests/test_contracts.py tests/test_pi_transport.py
git commit -m "test: complete contract and Pi transport coverage"
```

Expected: both modules report 100% statement and branch coverage.

## Task 4: Close Review Flow Coverage

**Files:**

- Test: `tests/test_review_flow.py`
- Production: `src/reviewctl/review_flow.py` only if a test proves a defect.

- [ ] **Step 1: Capture RED coverage**

```bash
uv run --python 3.14 pytest tests/test_review_flow.py --cov=reviewctl.review_flow \
  --cov-branch --cov-report=term-missing --cov-fail-under=100
```

Expected: 37 statements and 26 branches remain uncovered.

- [ ] **Step 2: Add focused trust-boundary tests**

Extend the existing receipt mutation helpers and add exact cases for:

```text
test_completion_manifest_rejects_invalid_prepared_and_packet_digests
test_ordered_coverage_partition_rejects_wrong_container_and_nontext_fields
test_coverage_violation_rejects_unknown_rules_and_impossible_fragments
test_validated_promoted_finding_contains_prepare_errors
test_completion_context_rejects_duplicate_or_unsorted_provenance
test_promote_contract_fragments_contains_invalid_context_and_duplicate_ids
test_build_completion_context_rejects_reproduced_invalid_context
test_consolidate_contains_hostile_context
test_receipt_helpers_reject_nonmapping_review_context_and_unknown_fallback
test_validate_v2_receipt_rejects_malformed_contract_evaluation_relations
test_validate_v2_receipt_rejects_backend_qualification_and_result_contradictions
```

Each mutation must assert the precise violation token returned by `validate_v2_receipt`. Reuse
the existing valid v2 receipt factory and alter one field per test so the branch cause is
isolated.

- [ ] **Step 3: Verify GREEN and commit**

```bash
uv run --python 3.14 pytest tests/test_review_flow.py --cov=reviewctl.review_flow \
  --cov-branch --cov-report=term-missing --cov-fail-under=100
uv run --python 3.14 ruff check tests/test_review_flow.py
git add tests/test_review_flow.py
git commit -m "test: complete review flow coverage"
```

## Task 5: Close API, Configuration, Dimension, and Artifact Coverage

**Files:**

- Test: `tests/test_api.py`
- Test: `tests/test_config.py`
- Create: `tests/test_dimensions.py`
- Test: `tests/test_artifacts.py`

- [ ] **Step 1: Capture RED coverage**

```bash
uv run --python 3.14 pytest tests/test_api.py tests/test_config.py \
  tests/test_dimensions.py tests/test_artifacts.py \
  --cov=reviewctl.api --cov=reviewctl.config --cov=reviewctl.dimensions \
  --cov=reviewctl.artifacts --cov-branch --cov-report=term-missing --cov-fail-under=100
```

Before `tests/test_dimensions.py` exists, run without that path. Expected gaps: API 70
statements/28 branches, config 22/18, dimensions 6/4, artifacts 2/2.

- [ ] **Step 2: Add API validation and failure-path cases**

Add tests covering these exact scenarios:

```text
Finding.from_value missing field and non-integer line
explicit review id and duplicate review directory
timeout and clean empty execution diagnostics
non-mapping, JSON-unsafe, non-object-decoded, and oversized source context
empty prompt, missing profile, sensitive remote profile, and profile without routes
relative file success; outside, missing, non-UTF8, read error, and post-stat growth failures
unknown contract receipt; unregistered transport; transport exception and final empty response
contract evaluation exception; malformed complete and incomplete findings
all attempts incomplete; duplicate finding merge
unreadable project receipt and missing receipt digest
```

Use existing `FakeTransport`/`QueueTransport`, direct deliberately inconsistent `ReviewConfig`
objects for the defense-in-depth guards, and monkeypatch `json.loads`, `Path.stat`, or
`Path.read_bytes` only for branches impossible through a valid upstream parser.

- [ ] **Step 3: Add config, dimensions, and artifact cases**

Add the following exact cases:

```text
missing profile; non-string route; malformed TOML; non-table parser result
non-table merge/profile/project/profiles; invalid string, integer, attempts, project id
invalid routes, execution, tools, privacy, visibility, and required dimensions
null output-token limit and stricter project privacy floor
dimension input as string, non-iterable, too large, blank/non-string, or overlong
absolute artifact path and zero-progress os.write
```

The zero-progress artifact test must assert `OSError` and no falsely successful artifact.

- [ ] **Step 4: Verify GREEN and commit**

```bash
uv run --python 3.14 pytest tests/test_api.py tests/test_config.py \
  tests/test_dimensions.py tests/test_artifacts.py \
  --cov=reviewctl.api --cov=reviewctl.config --cov=reviewctl.dimensions \
  --cov=reviewctl.artifacts --cov-branch --cov-report=term-missing --cov-fail-under=100
uv run --python 3.14 ruff check tests/test_api.py tests/test_config.py \
  tests/test_dimensions.py tests/test_artifacts.py
git add tests/test_api.py tests/test_config.py tests/test_dimensions.py tests/test_artifacts.py
git commit -m "test: complete API and configuration coverage"
```

## Task 6: Close GitHub, Identity, and Journal Coverage

**Files:**

- Test: `tests/test_github_contracts.py`
- Test: `tests/test_github_source.py`
- Test: `tests/test_github_publisher.py`
- Create: `tests/test_identity.py`
- Test: `tests/test_journal.py`

- [ ] **Step 1: Capture RED coverage**

```bash
uv run --python 3.14 pytest tests/test_github_contracts.py tests/test_github_source.py \
  tests/test_github_publisher.py tests/test_journal.py \
  --cov=reviewctl.github --cov=reviewctl.github_publisher --cov=reviewctl.identity \
  --cov=reviewctl.journal --cov-branch --cov-report=term-missing --cov-fail-under=100
```

- [ ] **Step 2: Complete GitHub source and contract validation**

Add tests for command success/timeout/OSError; invalid PR numbers, changed-file statuses/content,
snapshot SHA/visibility/diff/member types, publication target line/side, malformed GitHub metadata,
visibility fallback, added/deleted/renamed/dev-null diffs, size/file-count bounds, invalid committed
paths, snapshot construction errors, diff added-line edge cases, and missing finding IDs.

The redundant `_PATH_PART.fullmatch` defense must be exercised by a controlled monkeypatch of the
compiled matcher, not excluded, because the repository requires literal 100% branch coverage.

- [ ] **Step 3: Complete publisher and identity failure behavior**

Add publisher tests for invalid bounds, runner exception/timeout, malformed head/reconciliation
and post payloads, nontext bodies, invalid comment IDs, invalid/empty plans, and a head change
immediately before post. Assert both diagnostic and nondiagnostic `PublicationResult.to_payload`.

Create `tests/test_identity.py` covering create/reuse/modes/serialization, absent identity,
malformed/nonobject/schema-invalid content, configured-project mismatch, missing lock support,
lock acquire/unlock failure, and temporary-file cleanup when replace fails.

- [ ] **Step 4: Complete journal diagnostics and projection behavior**

Add tests for project/origin coupling, invalid event types/identifiers, descriptor and lock errors,
identity mismatch, invalid status events, unknown findings, missing/invalid status changes, empty
journal, read/UTF-8/JSON/nonobject failures, envelope mismatch, legacy-after-versioned corruption,
head/compatibility states, projection skips/mismatches/reason removal, invalid dimensions, and
`findings_with_diagnostic` propagation.

- [ ] **Step 5: Verify GREEN and commit**

```bash
uv run --python 3.14 pytest tests/test_github_contracts.py tests/test_github_source.py \
  tests/test_github_publisher.py tests/test_identity.py tests/test_journal.py \
  --cov=reviewctl.github --cov=reviewctl.github_publisher --cov=reviewctl.identity \
  --cov=reviewctl.journal --cov-branch --cov-report=term-missing --cov-fail-under=100
uv run --python 3.14 ruff check tests/test_github_contracts.py tests/test_github_source.py \
  tests/test_github_publisher.py tests/test_identity.py tests/test_journal.py
git add tests/test_github_contracts.py tests/test_github_source.py \
  tests/test_github_publisher.py tests/test_identity.py tests/test_journal.py
git commit -m "test: complete GitHub and journal coverage"
```

## Task 7: Close Project CLI and GitHub Front-Door Coverage

**Files:**

- Test: `tests/test_cli_front_door.py`
- Test: `tests/test_github_cli.py`

- [ ] **Step 1: Capture RED coverage**

```bash
uv run --python 3.14 pytest tests/test_cli_front_door.py tests/test_github_cli.py \
  --cov=reviewctl.project_cli --cov-branch --cov-report=term-missing --cov-fail-under=100
```

- [ ] **Step 2: Cover project CLI rendering and diagnostics**

Add exact cases for `_json_default`, text result rendering, init path/force/write failures,
prompt-file reading, client setup exceptions, `--fail-on`, nonaccepted status without diagnostic,
status invalid dimensions/errors/text, findings projection diagnostics, missing finding status
change, status reason presence/absence, corrupt journal JSON/text verification and identity
fallback, and doctor invalid-config/text output.

- [ ] **Step 3: Cover GitHub front-door publication outcomes**

Add exact cases for skipped duplicates, stale head and stale-head race journal events, failed
publication with/without diagnostic, source/client/materialization/review exceptions, invalid
receipt, plan persistence error, publish requested for a nonexecutable plan, text rendering, failed
publication exit code, and nonaccepted review exit code.

- [ ] **Step 4: Verify GREEN and commit**

```bash
uv run --python 3.14 pytest tests/test_cli_front_door.py tests/test_github_cli.py \
  --cov=reviewctl.project_cli --cov-branch --cov-report=term-missing --cov-fail-under=100
uv run --python 3.14 ruff check tests/test_cli_front_door.py tests/test_github_cli.py
git add tests/test_cli_front_door.py tests/test_github_cli.py
git commit -m "test: complete project front-door coverage"
```

## Task 8: Close Core CLI Orchestration Coverage

**Files:**

- Test: `tests/test_run.py`
- Production: `src/reviewctl/cli.py` only if a test proves a defect.

- [ ] **Step 1: Capture RED CLI coverage**

```bash
uv run --python 3.14 pytest tests/test_run.py --cov=reviewctl.cli --cov-branch \
  --cov-report=term-missing --cov-fail-under=100
```

Expected: 95 statements and 73 branch destinations remain uncovered.

- [ ] **Step 2: Cover schema, config, and process cleanup**

Add tests for `codex_schema`, zero-progress exclusive writes, missing/malformed transport-default
config and invalid bounds, account-home resolution failures, reap errors, missing process groups,
and grace-period kill/reap errors.

- [ ] **Step 3: Cover Kiro and Gemini defensive paths**

Add tests for malformed Kiro inventory shapes, missing/invalid session state, unsupported contract,
inventory timeout, Gemini absent usage candidates, response/diagnostic evidence write failures,
execution OSError, and missing session/response.

- [ ] **Step 4: Cover Pi and exploration lifecycle**

Add tests for string/nonlist content, invalid usage/model resolution, malformed/nonobject persisted
events, document versus findings system prompts, execution OSError, invalid exploration IDs,
prompt exclusivity/absence/emptiness, missing/malformed manifests, duplicate start, absent model,
missing cwd, show/promote failures, no completed/empty response, and nonempty promotion output.

- [ ] **Step 5: Cover response and receipt validation tails**

Add tests for all `validate_read_proof` false branches and basename fallback, non-findings response
validation, object/field/reviewed-file proof mismatches, legacy transport declaration with malformed
attempts, receipt digest-only violation replacement, target `reviewedFiles` completion gaps, and
top-level config errors.

- [ ] **Step 6: Verify GREEN and commit**

```bash
uv run --python 3.14 pytest tests/test_run.py --cov=reviewctl.cli --cov-branch \
  --cov-report=term-missing --cov-fail-under=100
uv run --python 3.14 ruff check tests/test_run.py
git add tests/test_run.py
git commit -m "test: complete CLI orchestration coverage"
```

## Task 9: Prove the Integrated 100% Baseline

**Files:** No planned edits. Any failure returns to its owner task.

- [ ] **Step 1: Run the exact locked Python 3.14 setup**

```bash
uv sync --locked --python 3.14
python --version
```

Expected: Python 3.14.x selected and the lock is unchanged.

- [ ] **Step 2: Run every local CI/release gate freshly**

```bash
uv run --python 3.14 ruff check .
uv run --python 3.14 ruff format --check .
uv run --python 3.14 pytest --cov=reviewctl --cov-branch --cov-report=term-missing
uv build
git diff --check origin/main...HEAD
```

Expected: Ruff and build exit zero; all tests pass; coverage reports exactly 100% with
`fail_under = 100`; diff check is clean.

- [ ] **Step 3: Verify repository boundaries**

```bash
git status --short --branch
git diff --name-status 36d90ba8e83dbec27c25e2935160626e2bcb0d8e...HEAD
git -C ~/Code/workspaces/reviewctl status --short --branch
```

Expected: the regularization worktree is clean, the primary checkout still has exactly its four
pre-existing modified files, and scorer v2 files/commits are absent from this branch.

- [ ] **Step 4: Obtain substantive independent review**

Review the exact regularization base/head diff in bounded slices. Verify every material finding
against source/tests, fix Critical or Important findings, rerun Task 9, and record the final exact
head SHA. Do not use `reviewctl` itself as the sole judge of its repair.

- [ ] **Step 5: Stop at the publication boundary**

Prepare the exact branch name, head SHA, commit list, CI commands, and review result. Do not push,
open a PR, or merge until the user explicitly authorizes publication.
