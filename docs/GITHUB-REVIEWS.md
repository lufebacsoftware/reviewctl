# GitHub pull-request reviews

The first GitHub integration is local-first and read-only. It resolves pull-
request metadata and the diff through the local `gh` authentication, verifies
that the checkout is at the exact pull-request head commit, reads bounded file
content from that commit, and then reuses the configured project review flow.
Pi remains a transport; it does not own GitHub identity, acceptance, receipts,
or publication.

## Dry-run command

```bash
reviewctl github review \
  --repo OWNER/REPO \
  --pr 123 \
  --project . \
  --profile default \
  --dimension security \
  --format json
```

The command never writes to GitHub. It produces the normal local review
receipt and a deterministic `publicationPlan` for inspection. A successful
plan is persisted as `publication-plan.json` beside the receipt; it is not a
published comment, an approval, or a request for changes.

To request the first supported external side effect explicitly:

```bash
reviewctl github review \
  --repo OWNER/REPO \
  --pr 123 \
  --project . \
  --publish \
  --publish-event comment \
  --format json
```

This can submit one grouped `COMMENT` review only after the receipt is
accepted and verified. The publisher reconciles stable finding markers in
existing review comments and review bodies, rechecks the head immediately
before and after the POST, and records stale-head races without retrying. It
does not support `approve` or `request-changes`.

The project profile controls the transport and privacy policy. The command
does not create a GitHub-specific Pi path or bypass the existing fallback,
contract, journal, and receipt behavior.

## What is frozen

The snapshot records:

- normalized `owner/repository` and pull-request number;
- base and head commit SHA;
- public/private visibility;
- changed-file status, path, size, and SHA-256 digest;
- normalized diff digest;
- sanitized source-operation evidence.

The source context copied into `packet.json`, `receipt.json`, and the
`review_started` journal event contains these identities and digests only. It
does not duplicate raw diff, source content, credentials, or provider output.
The bounded changed-file contents and diff are private review input used while
the request is running; temporary materialized files are removed afterward.

## Fail-closed diagnostics

The local source adapter refuses to continue when:

- the checkout `HEAD` differs from the PR head SHA (`github_checkout_stale`);
- GitHub does not prove public/private visibility (`github_visibility_unknown`);
- metadata, paths, source encoding, file count, diff size, or file size exceed
  the bounded contract;
- `gh` or `git` fails, times out, or returns malformed data.

Diagnostics are typed and safe for an LLM or automation to consume. They do
not include command stderr, authorization headers, prompts, raw source, or
raw provider responses. Retry only after inspecting the diagnostic and fixing
the source condition; do not treat an unavailable receipt as approval.

## Publication boundary

The plan maps a finding to an inline target only when the path and right-side
line are present in the frozen diff; other findings remain summary-only. The
comment publisher consumes this plan rather than recomputing finding identity
or deciding whether a review was accepted. It fails closed if reconciliation
cannot prove pagination exhaustion, and it never changes finding lifecycle
state.
