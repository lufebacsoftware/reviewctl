# reviewctl GitHub review — Phase 0–1 implementation plan

**Date:** 2026-08-24\
**Roadmap:** `docs/superpowers/specs/2026-08-24-github-pi-review-roadmap.md`\
**Scope:** provider-neutral contracts, deterministic dry-run publication plan, and local-first GitHub PR source\
**Out of scope:** GitHub writes, review approvals, request-changes, GitHub Actions, remote checkout materialization, and a second Pi orchestration path.

## Objective

Make a GitHub pull-request review a bounded input to the existing review flow:

```text
PR metadata + local checkout at head SHA
  -> immutable PullRequestSnapshot
  -> existing ReviewClient/Pi receipt
  -> deterministic ReviewPublicationPlan
```

The command is read-only and dry-run by construction. `reviewctl` remains the
authority for source identity, policy, contract evaluation, receipt validity,
finding identity, and journal facts. GitHub is only an adapter.

## Safety and repository boundary

- Preserve the pre-existing dirty changes in `README.md`,
  `docs/HELP-LLM.md`, `src/reviewctl/cli.py`, and `tests/test_run.py`.
- Do not add a GitHub SDK or new runtime dependency.
- Never log tokens, authorization headers, raw private source, or raw provider
  responses in the GitHub adapter artifacts.
- Require local `HEAD == pullRequest.headSha` before source materialization.
- Refuse `visibility=unknown`, missing metadata, ambiguous checkout, deleted
  or binary files, and an unbounded file set with typed diagnostics.
- Do not claim a publication or approval from a dry-run plan.

## Task 1 — Establish baseline and commit the plan

1. Record branch, exact HEAD, dirty paths, Python version, and the existing test
   command.
2. Run the complete baseline suite with `uv run pytest -q`.
3. Add this plan and commit only the plan file. Do not stage unrelated changes.

Expected verification:

```text
uv run pytest -q
git diff --check
```

## Task 2 — Add provider-neutral GitHub contracts (test first)

### Files

- Create `tests/test_github_contracts.py`.
- Create `src/reviewctl/github.py`.

### Test-first slices

1. Write a failing test for immutable PR references and changed-file
   snapshots. The test must prove normalized repository identity, SHA-256
   content digests, UTF-8 text boundaries, and rejection of path traversal.
2. Write a failing test for deterministic snapshot digests. Construct the same
   snapshot twice with different mapping insertion order and assert equal
   canonical digest; change the head SHA or a file digest and assert a changed
   digest.
3. Write a failing test for a publication plan. A complete accepted review
   produces stable finding markers, inline targets only for lines present in
   the diff, and summary-only items for findings outside the diff.
4. Write a failing test proving an incomplete/partial review cannot produce an
   executable publication plan.

### Implementation

Define frozen, JSON-safe dataclasses and pure functions:

- `PullRequestRef(repository, number)`;
- `ChangedFileSnapshot(path, status, sha256, size, text, old_path=None)`;
- `PullRequestSnapshot(ref, base_sha, head_sha, visibility, changed_files,
  diff, diff_sha256, snapshot_sha256, evidence)`;
- `PublicationTarget(path, line, side)`;
- `PublicationItem(finding_id, marker, body, target=None)`;
- `ReviewPublicationPlan(review_id, repository, pull_number, head_sha,
  snapshot_sha256, items, executable, reason)`;
- `canonical_sha256(value)` and `build_publication_plan(...)`.

The module must provide no network behavior. Stable finding identity is the
existing `Finding` identity from `reviewctl.api`; the GitHub marker identity is
`project + repository + pull number + finding ID` and deliberately excludes
the head SHA. A head SHA remains plan metadata.

## Task 3 — Implement the local-first GitHub source adapter (test first)

### Files

- Create `tests/test_github_source.py`.
- Extend `src/reviewctl/github.py` only if the public seam remains coherent.

### Test-first slices

1. Fake the `gh api` and `git` process boundaries and assert the adapter reads
   only PR metadata/diff and local committed files; it does not send source to
   any transport.
2. Assert a matching local checkout produces a snapshot with the exact base and
   head SHA, changed-file statuses, content digests, and normalized diff
   digest.
3. Assert stale `HEAD`, unknown visibility, malformed metadata, path traversal,
   unsupported binary content, and oversized input fail closed with stable
   diagnostic codes and actionable messages.
4. Assert all subprocess failures redact command credentials and retain only
   sanitized endpoint/exit metadata.

### Implementation

Add a narrow `GitHubSource` protocol and a local implementation backed by
injectable runners:

- `gh api repos/{owner}/{repo}/pulls/{number}` resolves PR metadata;
- `gh api repos/{owner}/{repo}/pulls/{number}.diff` resolves the review diff;
- local `git` commands verify `HEAD == headSha` and read only bounded changed
  files from that commit;
- the adapter returns a provider-neutral `PullRequestSnapshot` and never calls
  Pi or any model.

The initial implementation may use `subprocess.run` behind a tiny runner seam,
with one total timeout per source resolution. Do not add a Python GitHub client.
The source evidence records sanitized operation names and digests, not raw
responses or credentials.

## Task 4 — Connect the snapshot to the existing review flow and dry-run CLI

### Files

- Extend `src/reviewctl/api.py` with an optional typed source context on
  `ReviewRequest`, and persist its sanitized representation in `packet.json`,
  the receipt, and `review_started` journal event.
- Extend `src/reviewctl/project_cli.py` with
  `github review --repo --pr --project --profile --dimension --format`.
- Create `tests/test_github_cli.py`.
- Update `docs/PI-INTEGRATION.md`, `docs/HANDOFF.md`, and add a focused
  `docs/GITHUB-REVIEWS.md` describing dry-run behavior and diagnostics.

### Test-first slices

1. Assert the new command defaults to dry-run and passes only the bounded
   snapshot files plus typed context to `ReviewClient`.
2. Assert the receipt and packet contain repository, PR number, base/head SHA,
   snapshot digest, and diff digest, while no raw diff or source content is
   duplicated in the context metadata.
3. Assert a verified accepted receipt creates a deterministic plan and prints
   it in JSON; partial, timeout, contract-failed, or privacy-denied reviews
   produce no executable plan.
4. Assert no `--publish` option exists in this phase; any future write path
   must be a separate explicit command/flag and cannot be inferred from a
   model verdict.

### Implementation

- Reuse `ReviewClient.from_project` and the configured profile, so Pi remains
  the existing transport rather than a parallel GitHub-specific runner.
- Build the review prompt from the snapshot's bounded changed files and a
  concise diff-aware instruction; do not put raw provider output into GitHub
  artifacts.
- Store only sanitized PR context in the packet/receipt/journal. Existing
  receipt digest verification must continue to pass after the extension.
- Add a JSON payload with `snapshot`, `review`, and `publicationPlan` fields;
  text output can remain compact and operational.

## Task 5 — Verify and adversarially review the first cut

1. Run focused contract/source/CLI tests.
2. Run `ruff check` on changed Python files and `git diff --check`.
3. Run the complete suite with `uv run pytest -q`.
4. Run `reviewctl verify` against a generated accepted receipt and verify the
   packet/source digests independently.
5. Submit the bounded implementation and tests through the configured
   `reviewctl` external review transport (Ox-alpha first; an additional model
   is advisory only). Reproduce every material finding locally before changing
   code.
6. Commit the verified Phase 0–1 slice without staging unrelated files.

## Exit gate

Phase 0–1 is complete only when:

- a synthetic snapshot produces a byte-stable publication plan;
- a real read-only PR can be resolved through `gh` with a local checkout at its
  exact head SHA;
- the normal Pi-backed review produces an accepted receipt whose packet and
  receipt both identify the PR snapshot;
- `reviewctl verify` passes for that receipt;
- stale/unknown/private unsafe cases fail closed with documented diagnostics;
- dry-run creates no GitHub mutation; and
- the complete test suite and adversarial review are green or any remaining
  issue is explicitly documented as a blocker for Phase 2.

## Follow-up plan after this gate

Create `docs/superpowers/plans/2026-08-24-github-comment-publisher.md` only
after this phase is committed. It will cover marker reconciliation,
pagination/truncation, stale-head precheck and race classification, grouped
comment-only publication, append-only publication events, and idempotent
retries. It must not be folded into the local source implementation merely to
make the first command appear complete.
