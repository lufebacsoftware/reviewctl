# reviewctl — GitHub PR Review Roadmap

**Status:** Implemented through Phase 2 locally; real GitHub canary pending
**Date:** 2026-08-24
**Scope:** Local-first GitHub pull-request review and controlled publication
**Dependency decision:** Pi remains an optional execution adapter. GitHub is an
input/output adapter. Neither becomes the authority for review evidence.

The Phase 0–2 code path is now available as `reviewctl github review`: it
freezes a local-first PR snapshot, reuses the existing Pi-backed project
review, persists a dry-run publication plan, and supports explicit
comment-only publication. Real GitHub mutation remains a separately approved
canary because the repository/PR target and credential scope are deployment
inputs, not unit-test fixtures.

## 1. Product direction

The useful product loop is:

```text
GitHub PR
  -> immutable PR context and head SHA
  -> bounded source snapshot
  -> reviewctl contract + policy
  -> Pi transport (or another qualified backend)
  -> receipt + project journal
  -> dry-run publication plan
  -> explicit GitHub review comments
```

This turns `reviewctl` from a local review command into a reusable review
control plane for GitHub workflows. The important distinction is that GitHub
publishes a review decision; it does not define whether the review was valid.

The first product should publish discussion comments only. It must not
automatically approve or request changes. Those actions require a later policy
gate and a separate explicit decision.

## 2. Ownership and seams

### `reviewctl` core

Owns:

- PR snapshot identity: repository, pull number, base SHA, head SHA, and diff
  digest;
- source classification and privacy policy;
- review dimensions and response contract;
- Pi/backend selection and bounded fallback;
- finding identity, validation, lifecycle, receipts, and journal events;
- stale-head, duplicate-publication, and acceptance gates;
- a deterministic `ReviewPublicationPlan`.

### `GitHubSource` adapter

Reads a pull request and returns a provider-neutral `PullRequestSnapshot`:

```text
repository      OWNER/REPO
pullNumber      integer
baseSha         immutable commit SHA
headSha         immutable commit SHA
changedFiles    bounded file snapshots with paths and SHA-256 digests
diff            normalized diff and digest
visibility      public/private/unknown
sourceEvidence  sanitized request/response locators
```

The adapter must not send source to a model. It only obtains the exact review
input. The initial implementation should require a local checkout whose HEAD
matches the PR head SHA. Remote materialization can be a later adapter; this
keeps the first path local-first and makes source provenance easy to verify.

### `GitHubPublisher` adapter

Consumes only a validated `ReviewPublicationPlan` and returns a
`PublicationResult`:

```text
publicationKey
headSha
publishedCommentIds
skippedFindingIds
summaryCommentId
status
diagnostic
```

The publisher must not receive credentials in the plan, must not recompute
finding identity, and must not decide whether a finding is valid. It only maps
validated findings to GitHub's review-comment representation.

The recommended first adapter is a local `gh api` implementation because it
reuses the user's existing GitHub authentication without adding a runtime
Python SDK. The adapter must record sanitized endpoint/request metadata, never
tokens or authorization headers. A direct REST or GitHub App adapter can be
added later for CI and hosted operation.

## 3. CLI shape

The proposed front door is:

```bash
reviewctl github review \
  --repo OWNER/REPO \
  --pr 123 \
  --project . \
  --profile default \
  --dimension security \
  --format json
```

The command is dry-run by default. It produces the frozen snapshot, formal
review receipt, journal events, and a publication plan without external writes.

Publishing requires an explicit flag:

```bash
reviewctl github review \
  --repo OWNER/REPO --pr 123 --project . \
  --publish --format json
```

The first release supports only `--publish-event comment`. `approve` and
`request-changes` are rejected as unsupported rather than inferred from a
model verdict. A future policy-controlled command may add those events after
human or organization authorization.

## 4. Review and publication flow

1. Resolve the PR and obtain `baseSha`, `headSha`, repository visibility, and
   the changed-file diff.
2. Verify the local checkout is at `headSha`; refuse to review a stale or
   ambiguous checkout.
3. Freeze bounded changed files, classify them, and create the packet digest.
4. Run the normal `ReviewClient` flow. Pi is selected through the existing
   profile/route system and remains only the transport.
5. Require a complete accepted receipt before creating a publication plan.
   Partial findings remain journal evidence but are not published automatically.
6. Map each finding to the PR diff. Inline publication is allowed only when the
   path and line are present on the current diff side. Findings outside the
   diff become summary-only items; they are not attached to an arbitrary line.
7. Re-fetch the PR head immediately before publishing. If it differs from the
   frozen `headSha`, return `stale_head` and publish nothing. GitHub does not
   provide a documented atomic compare-and-post guard for this operation, so a
   race after this check is possible: the POST remains anchored to the frozen
   `commit_id`, then a post-publication head check must classify the result as
   `stale_head_race`, ineligible as a current publication, and never an approval
   or request for changes.
8. Reconcile existing comments using a stable finding marker, then publish only
   the missing comments. Record the result in the local journal. If the head
   changes after the final check, do not retry automatically.

GitHub's pull-request review API supports one review with a summary and inline
comments, tied to a `commit_id`; line comments require valid diff positioning or
line-side information. The implementation must therefore retain the exact
head SHA and diff mapping rather than publishing from only `path` and `line`.
See [GitHub pull-request reviews](https://docs.github.com/en/rest/pulls/reviews)
and [pull-request review comments](https://docs.github.com/en/rest/pulls/comments).

## 5. Idempotency and lifecycle

Every published body includes a machine-readable marker whose identity excludes
the head SHA, for example:

```text
<!-- reviewctl:project=project-abc repo=OWNER/REPO pr=123 finding=finding-abc123 -->
reviewctl-head: SHA
```

Before publishing, the adapter lists all relevant existing review comments and
matches this marker by repository, pull request, and stable finding ID. A retry
after an ambiguous network result therefore skips an already-created comment
instead of duplicating it. On a later head, an existing marker is still the
same finding: the default policy records it as reconciled and does not create a
second inline thread. A future explicit `--republish` policy may update or
re-anchor it after a human decision. The head SHA remains metadata, not part of
the deduplication key. The local journal records append-only facts such as:

- `github_publication_planned`;
- `github_publication_started`;
- `github_comment_published`;
- `github_comment_skipped_duplicate`;
- `github_publication_failed`;
- `github_publication_stale_head`.

Comment reconciliation must paginate to exhaustion under a bounded request and
time budget. If pagination is truncated, the budget is exceeded, or GitHub
cannot prove that the relevant comment set was fully read, publication fails
closed with `publication_reconciliation_incomplete`; it must not guess that a
finding is new.

Publication is a side effect after acceptance, not part of acceptance. A
successful GitHub comment never changes a finding from `open` to `verified` and
never manufactures approval.

## 6. Privacy and safety gates

- Default is dry-run; `--publish` is the only external write gate.
- No prompt, raw source, credentials, provider response, or private artifact
  path is placed in a GitHub comment.
- Comment bodies contain a concise finding, path/line context, severity, and a
  reference to the review ID/head SHA. Sensitive evidence stays local.
- Public/private repository classification is captured before source transfer
  and checked against the project policy. `unknown` is a privacy failure in the
  first implementation: source materialization and model transfer stop until
  an explicit later policy supports that state.
- A missing GitHub credential, insufficient permission, stale head, invalid diff
  line, or unresolved repository identity produces a typed diagnostic and no
  partial publication unless the plan explicitly permits summary fallback.
- The publisher never approves or requests changes in the initial release.
- The receipt records what was planned, what was actually published, and what
  was skipped; it does not claim that GitHub displayed or notified every user
  beyond the API response observed.

## 7. Phased roadmap

### Phase 0 — Contract and dry-run fixture

- Define `PullRequestSnapshot`, `ReviewPublicationPlan`, and
  `PublicationResult` as provider-neutral contracts.
- Add synthetic PR/diff fixtures, including renamed files, deleted files,
  multi-hunk diffs, generated files, and findings outside the diff.
- Add `reviewctl github review` dry-run output with no network writes.
- Verify stable snapshot, diff, finding, and publication digests.

Exit gate: deterministic plans from the same snapshot; no GitHub dependency in
unit tests; no external mutation.

### Phase 1 — Local GitHub source adapter with Pi

- Read PR metadata/diff through `gh api` or an equivalent local adapter.
- Require a matching local checkout at `headSha`.
- Materialize only bounded changed files into the existing review packet.
- Reuse `ReviewClient` and the existing Pi profile; do not create a parallel Pi
  orchestration path.
- Persist PR context and snapshot digests in the packet, receipt, and journal.

Exit gate: a real read-only PR can produce a verified local receipt and plan;
the test can prove exactly which commit and files were reviewed.

### Phase 2 — Comment-only GitHub publisher

- Implement marker-based reconciliation.
- Publish one grouped `COMMENT` review with valid inline comments and a summary
  for findings that cannot be anchored.
- Add stale-head recheck immediately before publish.
- Append publication lifecycle events and make retries idempotent.
- Keep dry-run as the default and require `--publish`.

Exit gate: a sandbox/test repository can receive comments once, rerun on the
same head without duplicates, reconcile a later head without creating a second
thread by default, and refuse a changed head detected before the POST. A race
after the final check is observable as `stale_head_race` and is never reported
as a current or approval publication.

### Phase 3 — GitHub Actions and checks

- Add a thin GitHub Actions wrapper that invokes the same CLI and stores the
  receipt as an artifact.
- Add a check-run adapter only after the authentication model is explicit.
  GitHub documents check-run creation as requiring a GitHub App, so this is a
  separate deployment/authentication decision, not an incidental extension of
  local `gh` publishing. See [GitHub check runs](https://docs.github.com/en/rest/checks/runs).
- Make check status reflect receipt/publication facts, not model verdict alone.

Exit gate: CI can rerun safely, expose receipt provenance, and distinguish
review failure, publication failure, and stale source.

### Phase 4 — Governance and optional exchange

- Add explicit policy for `approve` and `request-changes` with human/organization
  authorization.
- Add project-level publication permissions and comment retention rules.
- Later add signed export/import, cursors, conflict quarantine, and multi-origin
  aggregation. Potzal may carry opaque bundles but remains optional and cannot
  own reviewctl semantics.

## 8. Non-goals for the first implementation

- Pi extensions, Pi-owned GitHub tools, or interactive editor mutation.
- Automatic code edits or commits.
- Automatic approval/request-changes based on an LLM verdict.
- A GitHub-specific database or a hosted review server.
- Federation, signatures, or cross-machine merge in the first GitHub path.

## 9. Verification strategy

- Unit-test pure diff-to-line mapping and marker reconciliation.
- Fake the `GitHubSource` and `GitHubPublisher` seams; do not require network
  access for normal tests.
- Test stale-head, duplicate retry, invalid line, permission failure, privacy
  denial, partial review, and receipt corruption.
- Run a bounded read-only canary against a real PR, then a separately approved
  comment canary in a disposable/test repository.
- Require `reviewctl verify` on the receipt before treating publication as
  evidence.
- Review the implementation through persisted `reviewctl` receipts; model
  findings remain advisory until reproduced in source/tests/runtime.

## 10. Architectural decision

Adopt GitHub as an optional source and publication adapter around the existing
local-first review control plane. Keep Pi behind the existing backend seam.
Start with local checkout + `gh` metadata + dry-run/comment-only publication;
defer check runs, approval events, editable execution, and federation until the
smaller path has stable receipts and idempotent behavior.
