# PR #5 Post-Merge Hardening Design

## Goal

Close the four evidence and transport defects that survived PR #5 without reopening the
regularization line or performing a broad receipt migration:

1. project-review sources must not remain in durable attempt artifacts;
2. global `reviewctl verify` must not treat project checkpoints as canonical receipts;
3. generated project configuration must not embed an operating model roster or enable remote
   execution by default; and
4. Antigravity must reject a present but malformed `structured_output` instead of silently using
   a different response field.

The already-correct large-packet behavior remains a regression gate: Antigravity packets stay out
of a single argv element and use the current process-lifetime sandbox file.

## Recovered State

- PR #5 head `1aa2ed46b7903c1ed0b2addbe317abe9f17e7550` was merged as
  `4279bd7232c30b2a22967bf264c123a4c18aad67`.
- The corrective branch starts from current `origin/main` at
  `dab0bcc2620d963c89d82efce7ea34f6fb7207ef` in an isolated clean worktree.
- The Python 3.14 baseline passes the complete test suite, Ruff check, and Ruff format check.
- Post-merge source inspection, two independent review axes, and a persisted Sol review all
  reproduced the first three defects. The spec review additionally reproduced the malformed
  `structured_output` fallback.
- The Sol receipt is corroborating evidence only: its bounded excerpts lived outside a Git
  checkout and therefore its `source.git.head` is null.

## Considered Approaches

### Bounded demotion and hardening — selected

Keep the project API's existing artifact shape for compatibility, explicitly mark it as a
project checkpoint, retain its internal digest check, and make global `reviewctl verify` reject
both marked and historical project-checkpoint shapes. Separately fix source lifetime, template
defaults, and Antigravity shape validation.

This approach closes the misleading merge-grade path without pretending that a digest-only
checkpoint has acquired V2 structure. It produces a small correction that can be tested against
the exact defects.

### Migrate the project API to canonical receipt V2 now

This would eventually provide one receipt format, but it requires binding project journal state,
contract evaluation, source provenance, attempts, fallback relationships, and accepted-attempt
semantics to the canonical V2 builder and verifier. That is a separate compatibility project, not
a safe post-merge correction.

### Document the current behavior and leave it unchanged

This is rejected. Documentation cannot make durable source retention private, remove an operating
roster from generated projects, or prevent a weaker verifier from being selected by input data.

## Design

### 1. Process-lifetime project source snapshots

`ReviewClient.review` continues to read, validate, hash, and retain source bytes in memory before
the first model attempt. For every attempt that reaches a transport, it creates a private
`TemporaryDirectory` outside `.reviewctl`, writes the already-frozen bytes there through the
confined artifact writer, and passes only those temporary paths to the backend.

The temporary directory surrounds `transport.execute` and is removed when that call returns or
raises. Accepted, refused, partial, fallback, timeout, and exception paths therefore share the
same cleanup boundary. No `attempt-XX/source` directory is created.

`ReviewRequest.source_root` remains an input-validation and logical-path boundary, not a bypass
around snapshotting. Even externally materialized GitHub sources are copied from the frozen bytes
into the per-attempt temporary directory. The backend request retains the original project and
external roots for sandbox denial and adds the temporary root so transports can validate the
paths they receive. This prevents later mutations of the original source from changing the bytes
reviewed.

Durable artifacts retain only source names, hashes, packet metadata, model output, diagnostics,
and the project checkpoint.

### 2. Project checkpoint versus canonical receipt

The project API continues writing `receipt.json` and returning `ReviewResult.receipt_path` to
avoid breaking callers. New artifacts add explicit fields:

```json
{
  "artifactKind": "project-review-checkpoint",
  "projectCheckpointSchemaVersion": 1
}
```

`verify_project_receipt` remains a compatibility function for internal checkpoint integrity. It
checks the explicit marker on new checkpoints, accepts historical unmarked checkpoints for direct
legacy inspection, rejects malformed JSON and digest mismatches, and—when supplied—binds the
digest to the `ReviewResult` produced in the same process. Its docstring and user documentation
must say that it is not canonical receipt verification.

Global `reviewctl verify` no longer delegates to `verify_project_receipt`. Before legacy V1 or V2
handling, it rejects:

- a marked project checkpoint; and
- the historical project-checkpoint signature (`reviewId`, `configDigest`, `projectId`, and
  `originId` without `receiptSchemaVersion`).

The violation is `project-checkpoint-not-review-receipt`. Canonical V2 receipts continue through
`validate_v2_receipt`; historical generic V1 receipts retain their documented digest-only path.
This is classification, not authentication: neither checkpoint nor receipt digest becomes a
signature or trust root.

The project CLI may still report an accepted model result after its same-process checkpoint check,
but its help and evidence documentation must not describe that checkpoint as merge-grade. A future
design may migrate this API to canonical V2.

### 3. Fail-closed project template

`PROJECT_TEMPLATE` uses:

```toml
routes = []
execution = "local"
```

for every privacy mode. It contains no model, provider, route, price, score, or qualification.
Organization policy or an explicit project-local configuration step supplies the operating route.

An empty route list must not trigger `routes[0]`. `ReviewClient.review` returns a controlled
`route_invalid` result and project checkpoint explaining that the selected profile has no routes.
It performs no transport call and cannot be interpreted as approval.

### 4. Strict Antigravity structured output

Antigravity keeps the current exact JSON parser, duplicate-key rejection, finite-number checks,
bounded output capture, durable raw stdout, and sandbox-file packet transport.

Response selection becomes explicit:

- if `structured_output` is absent, use the legacy `response` field;
- if it is present and is a JSON object, serialize that object canonically;
- if it is present with any other value—including `null`, scalar, or array—return controlled
  status 502 with `agy returned invalid structured output`.

No malformed structured output can be accepted by falling back to another field. Raw provider
stdout remains durable before validation, and no finding is promoted from the rejected attempt.

## Error and Compatibility Boundaries

- Temporary-source cleanup is mandatory even if a transport raises `OSError`, `UnicodeError`, or
  `ValueError`.
- Temporary-source cleanup failure must not expose source by moving it into durable artifacts; the
  standard-library temporary-directory failure propagates as a controlled transport failure where
  possible.
- Existing project checkpoint paths and `ReviewResult` fields remain available.
- Old project checkpoints remain directly digest-checkable through `verify_project_receipt`, but
  global `reviewctl verify` rejects them as noncanonical.
- Canonical V1 and V2 behavior outside the project-checkpoint signature is unchanged.
- Antigravity's current sandbox-file input mechanism is unchanged.

## Test Strategy

Every production change follows red-green-refactor.

1. **Source lifetime:** a fake transport observes exact frozen bytes inside a private temporary
   directory; after success, nonzero execution, fallback, and exception, every observed path is
   absent and no durable `source` directory exists. An original-file mutation does not alter the
   staged bytes.
2. **Checkpoint classification:** a self-consistent marked checkpoint and a historical unmarked
   project checkpoint both fail global verification with
   `project-checkpoint-not-review-receipt`; direct internal verification still detects tampering
   and expected-digest mismatch; canonical V1 and V2 controls retain their existing outcomes.
3. **Template:** all init modes generate empty routes and local execution with no provider/model
   text. Reviewing immediately after initialization returns `route_invalid`, writes no accepted
   attempt, and invokes no transport.
4. **Antigravity:** separate `null`, string, number, array, and boolean structured outputs fail
   closed while preserving raw response evidence. An absent structured-output field still permits
   legacy response fallback, and a valid object remains canonical.
5. **Large packet regression:** retain the existing assertion that the packet is absent from argv,
   available through the sandbox file, and removed with the sandbox.

Focused tests run after each red-green pair. Final verification runs the complete Python 3.14
suite with 100% statement and branch coverage, Ruff check, Ruff format check, package build,
`git diff --check`, and a clean-wheel test. The exact candidate SHA then requires green GitHub CI
and a persisted, verified, commit-bound substantive review before merge.

## Non-Goals

- No full project-API migration to receipt V2.
- No signing, organizational trust root, or remote attestation.
- No model qualification or roster changes in the repository.
- No changes to tournament, range review, Juliet, or consuming product semantics.
- No redesign of transport diagnostics already shown to persist in the canonical attempt receipt.

## Acceptance

The correction is acceptable only when all four defects have mutation-resistant tests, the large
packet regression remains green, no source bytes remain below durable attempt artifacts, global
verification refuses project checkpoints, default initialization is inert, and malformed present
Antigravity structured output cannot produce an accepted response.
