# GitHub Verifiable Receipt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an accepted `reviewctl github review` persist a canonical V2 receipt that global `reviewctl verify` accepts and that binds the frozen pull-request snapshot.

**Architecture:** Keep `ReviewClient.review` and its project checkpoint as the local journal/checkpoint mechanism. First strengthen an accepted checkpoint with the selected profile and an exact raw-response digest/length; these facts are unavailable today, so synthesizing a V2 receipt would otherwise forge provenance. Then add a GitHub-only promotion step that verifies the checkpoint and those bound bytes before reconstructing V2 from the frozen packet, accepted raw response and actual route/profile metadata. The V2 receipt carries the GitHub snapshot only as a typed extension, while `source`, `attempts`, contract evaluation and digest use the existing V2 schema.

**Tech Stack:** Python 3.14, pytest, existing `ReviewClient`, GitHub snapshot adapter, V2 receipt contract.

---

### Task 1: Capture the failing formal-evidence boundary

**Files:**
- Modify: `tests/test_github_cli.py`
- Test: `tests/test_cli_front_door.py`

- [ ] Add a real `ReviewClient` GitHub-review fixture that reads the receipt path from JSON output and calls `run_cli(["verify", receipt_path])`.
- [ ] Assert the receipt has `receiptSchemaVersion == 2`, `extension.githubPullRequest == snapshot().to_context()`, and `source.files` binds every changed-file SHA.
- [ ] Run `uv run pytest -q tests/test_github_cli.py -k verifiable_receipt`; it must fail because the project checkpoint is rejected as `project-checkpoint-not-review-receipt`.

### Task 2: Bind the accepted response before it can become formal evidence

**Files:**
- Modify: `src/reviewctl/api.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_github_cli.py`

- [ ] Add a failing acceptance regression showing an accepted checkpoint records the exact selected `profile` and an `acceptedResponse` object with only `sha256` and `characters`.
- [ ] Record that object only after a complete accepted contract response has been persisted. Its SHA-256 and character count must be calculated from the response bytes written to the accepted attempt artifact; unavailable, incomplete and rejected attempts must not have this field.
- [ ] Add a failing regression for an altered accepted `response.md`: the GitHub promoter must reject it even if the replacement text independently satisfies the review contract.
- [ ] Keep project checkpoint verification backward compatible for historical checkpoints, but reject an accepted new-schema checkpoint missing, malformed or mismatching profile/accepted-response binding.
- [ ] Run `uv run pytest -q tests/test_api.py tests/test_github_cli.py -k 'accepted_response or verifiable_receipt'` GREEN.

### Task 3: Promote only verified GitHub review evidence

**Files:**
- Create: `src/reviewctl/github_receipt.py`
- Modify: `src/reviewctl/project_cli.py`
- Test: `tests/test_github_cli.py`

- [ ] Create `write_github_v2_receipt(*, client, result, snapshot, receipt_path, profile_name) -> Path`. Reject a non-accepted result, missing digest, failed `verify_project_receipt`, malformed packet, missing accepted response, profile mismatch, response-hash mismatch, route mismatch, or non-complete contract as `receipt_invalid`.
- [ ] Parse the frozen `packet.json` and accepted `response.md` through confined readers. Re-evaluate the `findings-json` response using the packet's exact source names and the configured profile route; do not infer a formal receipt from checkpoint fields alone.
- [ ] Rebuild the canonical V2 source entries from the packet's names, paths and SHA-256s. Add `extension.githubPullRequest` from `snapshot.to_context()` without raw diff or source content.
- [ ] Write `github-review-receipt.json`, never overwrite `receipt.json`. Its accepted attempt must include a V2 route, requested/resolved model, raw-response evidence and serialized complete contract evaluation. Compute its digest with `contract_canonical_json`, require `validate_v2_receipt(...) == ()`, then return the new path.
- [ ] In `github_review_project`, use the promoted path only after promotion succeeds. Persist the publication plan beside it and expose the formal receipt in JSON output; retain the project checkpoint for journal compatibility.
- [ ] Run `uv run pytest -q tests/test_github_cli.py -k verifiable_receipt`; it must pass and global `reviewctl verify` must report `valid: true`.

### Task 4: Preserve the rejection boundary

**Files:**
- Modify: `tests/test_github_cli.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_cli_front_door.py`

- [ ] Add four independent regressions: a tampered promoted snapshot fails verification; a packet hash or raw response byte mismatch rejects promotion; ordinary `reviewctl review` checkpoints remain rejected globally; existing canonical `reviewctl run` V2 receipts remain accepted unchanged.
- [ ] Run each focused test RED before the minimal guard, then run `uv run pytest -q tests/test_github_cli.py tests/test_api.py tests/test_cli_front_door.py` GREEN.

### Task 5: Make the evidence contract unambiguous

**Files:**
- Modify: `docs/GITHUB-REVIEWS.md`
- Modify: `docs/EVIDENCE.md`
- Modify: `docs/PI-INTEGRATION.md`

- [ ] Document `receipt.json` as a project checkpoint verified only by `verify_project_receipt`; document `github-review-receipt.json` as canonical V2, verified with global `reviewctl verify`.
- [ ] State that publication may use only the verified canonical receipt and matching frozen PR head. Do not call a checkpoint formal, canonical, or merge-grade.
- [ ] Extend the focused JSON-output test to prove `formalReceipt` differs from the checkpoint path and global verification accepts it.

### Task 6: Full verification and integration evidence

**Files:** affected files above only

- [ ] Run `uv run pytest -q`, `uv run coverage run -m pytest`, `uv run coverage report --fail-under=100`, and `git diff --check`.
- [ ] Commit only the promoter, command integration, regressions and evidence documentation with message `fix: emit verifiable GitHub review receipts`.
- [ ] Request formal review only on that final SHA. Verify the newly emitted canonical artifact with global `reviewctl verify`; do not use a checkpoint, exploratory response or stale SHA as merge evidence.

## Coverage review

The tasks cover every requirement in issue #25: GitHub-to-verify red/green proof, frozen base/head/file/snapshot binding, accepted attempt/dimensions/findings, checkpoint distinction, existing V2 compatibility, documentation, and full coverage. They deliberately do not reclassify legacy project reviews as merge evidence.
