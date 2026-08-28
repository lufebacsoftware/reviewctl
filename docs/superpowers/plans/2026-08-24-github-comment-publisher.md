# reviewctl GitHub comment publisher — Phase 2 implementation plan

**Date:** 2026-08-24\
**Roadmap:** `docs/superpowers/specs/2026-08-24-github-pi-review-roadmap.md`\
**Prerequisite:** Phase 0–1 commit `0bdf813`\
**Scope:** explicit, comment-only GitHub publication from a verified dry-run plan\
**Out of scope:** approvals, request-changes, checks, Actions, GitHub Apps, and automatic republishing/re-anchoring.

## Objective

Turn a verified `ReviewPublicationPlan` into one grouped GitHub `COMMENT`
review only when the caller explicitly requests publication:

```text
accepted verified receipt + executable plan
  -> reconcile existing review comments and review bodies
  -> re-fetch current PR head
  -> one grouped COMMENT POST anchored to frozen head SHA
  -> post-check head and append-only journal facts
```

`GitHubPublisher` is an output adapter. It does not receive credentials, does
not parse model output, does not recompute finding IDs, and does not change
finding lifecycle state.

## Safety boundary

- Dry-run remains the default; `--publish` is the only write gate.
- The only supported event is `comment`; the CLI must reject approval and
  request-changes rather than infer them from a verdict.
- Credentials remain in `gh`; plans, results, diagnostics, and journal events
  contain no token or authorization header.
- A stale head before POST produces no write.
- A head race after POST produces `stale_head_race`, preserves observed IDs,
  and never reports a current publication or approval. There is no automatic
  retry.
- Reconciliation is fail-closed. A malformed, truncated, or budget-exceeded
  page set produces no POST.

## Task 1 — Add the publisher contracts and test doubles (test first)

### Files

- Create `tests/test_github_publisher.py`.
- Create `src/reviewctl/github_publisher.py`.
- Extend `src/reviewctl/errors.py` with stable publisher exit classes.

### Tests before implementation

1. An in-memory runner returns paginated review-comment and review-body pages;
   the publisher must exhaust both sets and skip markers already present.
2. A plan with one inline and one summary-only item produces one grouped
   `COMMENT` request with `commit_id == plan.head_sha`, valid right-side inline
   fields, and the summary item in the review body.
3. A stale prepublish head returns `github_publication_stale_head` and emits no
   POST.
4. A page that cannot be parsed, never terminates, or exceeds the request/page
   budget returns `publication_reconciliation_incomplete` and emits no POST.
5. A post-publication head change returns `github_publication_stale_head_race`
   with observed IDs and no retry.
6. A second run with the same markers emits no duplicate POST; a later head
   still reconciles the stable marker and emits no second thread by default.
7. Diagnostics and persisted result objects contain no fake credential or raw
   command stderr.

### Implementation seam

Define `PublicationResult` as a JSON-safe immutable value with:

- `publication_key`, `head_sha`, `status`;
- `published_comment_ids`, `skipped_finding_ids`, `summary_comment_id`;
- optional safe `Diagnostic` and an observed-head field.

Define a `GitHubPublisher` backed by the existing injectable command runner.
Use `gh api` only as the credential boundary; use no Python GitHub SDK and no
token environment forwarding in the result.

## Task 2 — Implement bounded reconciliation and grouped comment POST

### Tests first

1. Assert each endpoint is paginated with a finite page size and finite page
   count; an exact page-size response must request the next page so exhaustion
   is proven.
2. Assert markers are matched by the stable marker in both review-comment
   bodies and top-level review bodies. The head line is metadata only and is
   not part of identity.
3. Assert only missing items are sent after reconciliation. Inline targets
   carry path, positive line, and `RIGHT` side; summary-only findings remain in
   the body and are never attached to an arbitrary line.
4. Assert the POST response is parsed conservatively and malformed responses
   return a typed failure without claiming success.

### Implementation

- List `/repos/{owner}/{repo}/pulls/{number}/comments` and
  `/repos/{owner}/{repo}/pulls/{number}/reviews` to exhaustion under bounded
  page/request budgets.
- Fetch the current head before reconciliation and again immediately before
  POST. Compare both against `plan.head_sha`.
- Build one `COMMENT` review request with the frozen `commit_id`. Include
  summary-only item bodies in `body`; include only valid inline items in the
  `comments` array.
- Parse returned review/comment IDs and then perform one post-publication head
  check. Classify a changed head as a race even if GitHub accepted the POST.
- Never retry an ambiguous POST in the same operation. The next invocation
  relies on marker reconciliation.

## Task 3 — Wire the explicit CLI and append-only journal facts

### Files

- Extend `src/reviewctl/project_cli.py` with `--publish` and
  `--publish-event comment` under `github review`.
- Extend `tests/test_github_cli.py` with dry-run, publish, stale, and duplicate
  cases.
- Update `docs/GITHUB-REVIEWS.md` and `docs/HANDOFF.md`.

### Tests first

1. Without `--publish`, assert no publisher call and a `github_publication_planned`
   journal event only.
2. With `--publish --publish-event comment`, assert publisher receives only the
   plan and result facts; credentials are not passed through the CLI.
3. Assert publication lifecycle events are append-only:
   `github_publication_started`, `github_comment_published`,
   `github_comment_skipped_duplicate`, `github_publication_failed`, and
   `github_publication_stale_head`/`github_publication_stale_head_race`.
4. Assert partial, invalid, or unverified receipts cannot call the publisher.

### Implementation

- Keep `--publish` opt-in and the default output explicitly labeled `dry-run`.
- Record plan and publication facts after the formal review; do not rewrite the
  existing receipt or turn a GitHub event into a finding status transition.
- Return stable exit codes from the typed diagnostic.

## Task 4 — Verify and adversarially review

1. Run focused publisher/CLI tests, lint, and `git diff --check`.
2. Run `uv run pytest -q`.
3. Verify a generated review receipt and journal continuity offline.
4. Submit only the publisher and focused tests through formal `reviewctl`
   review. Treat model output as advisory and reproduce material findings.
5. Use a fake runner for all unit tests. A real sandbox PR smoke is an explicit
   external-operation check; do not run it without a target repository/PR.

## Exit gate

Phase 2 is complete only when dry-run is unchanged, explicit comment publication
is grouped and idempotent, all reconciliation is fail-closed, stale heads and
post-POST races are observable, no approval/request-changes path exists, and
the complete suite plus verified receipt/journal checks pass.
