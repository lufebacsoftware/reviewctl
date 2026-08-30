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

Each attempt that reaches a transport gets a distinct system temporary directory outside the
project's `.reviewctl` tree. The directory surrounds `transport.execute` and is removed when that
call returns or raises. The transport result is not evaluated or accepted until the context has
exited successfully. Accepted, refused, partial, fallback, timeout, and exception paths therefore
share the same cleanup boundary, and a fallback cannot reuse an earlier attempt's source path. No
`attempt-XX/source` directory is created.

`ReviewRequest.source_root` remains an input-validation and logical-path boundary, not a bypass
around snapshotting. Even externally materialized GitHub sources are copied from the frozen bytes
into the per-attempt temporary directory. The backend request retains the original project and
external roots for sandbox denial and adds the temporary root so transports can validate the
paths they receive. The project root remains first for transports that use the first root as their
working directory. This prevents later mutations of the original source from changing the bytes
reviewed.

If operating-system cleanup of the temporary directory fails, the transport result is discarded
and the attempt becomes a controlled transport failure; it cannot become accepted. The
implementation never copies the temporary bytes or their path into `.reviewctl`. No userspace
design can guarantee physical deletion when the filesystem itself refuses removal, so that case
is an explicit residual host-security boundary rather than a successful review outcome.

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
- a recognizable historical project-checkpoint signature: `configDigest` plus any one of the
  project-only fields `projectId`, `originId`, `journalSequence`, `privacyMode`,
  `dimensionCoverage`, or `fallbackRelationships`. This is a subset test, not exact-key equality,
  and it runs regardless of extra `result` or `receiptSchemaVersion` fields.

The violation is `project-checkpoint-not-review-receipt`. Canonical V2 receipts continue through
`validate_v2_receipt`; a historical generic V1 receipt augmented only with `configDigest` retains
its documented digest-only path. Project-like fields may cause rejection but can never select the
weaker project verifier or bypass V2 validation.
This is classification, not authentication: neither checkpoint nor receipt digest becomes a
signature or trust root. An attacker can completely rewrite an unsigned checkpoint, remove every
project-only field, add a V1 shape, and recompute its digest; at that point global verification can
only apply the already-weak V1 integrity contract. The CLI and documentation must therefore never
present legacy V1 verification as provenance or merge-grade review evidence.

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

An empty route list must not trigger `routes[0]`. `ReviewClient.review` retains its current early
controlled `route_invalid` result explaining that the selected profile has no routes. Because no
attempt starts, it creates no checkpoint, review directory, or journal event and performs no
transport call; the result cannot be interpreted as approval.

### 4. Strict Antigravity structured output

Antigravity keeps the current exact JSON parser, duplicate-key rejection, finite-number checks,
bounded output capture, durable raw stdout, and sandbox-file packet transport.

Response selection becomes explicit:

- if `structured_output` is absent, use the legacy `response` field;
- if it is present and is a JSON object, serialize that object canonically;
- if it is present with any other value—including `null`, scalar, or array—return controlled
  status 502 with `agy returned invalid structured output`.

The exact outer JSON parser applies to the complete provider payload, including the nested object,
so duplicate keys and non-finite numbers are rejected before response selection. A structurally
valid object is transported canonically; the selected response contract still decides whether its
domain shape is complete and valid, producing `contract_failed` rather than a transport 502 when
appropriate. No malformed structured output can be accepted by falling back to another field.
Raw provider stdout remains durable before validation, and no finding is promoted from the
rejected attempt.

## Error and Compatibility Boundaries

- The temporary-directory context attempts cleanup after normal return and after any exception.
  Known transport `OSError`, `UnicodeError`, and `ValueError` failures become controlled attempt
  diagnostics; an unexpected exception propagates only after the cleanup boundary runs.
- Temporary-source cleanup failure invalidates the attempt and must never be masked as acceptance
  or move source bytes/path metadata into durable artifacts. Physical deletion after an
  operating-system removal failure is outside the process guarantee.
- Existing project checkpoint paths and `ReviewResult` fields remain available.
- Old project checkpoints remain directly digest-checkable through `verify_project_receipt`, but
  global `reviewctl verify` rejects them as noncanonical.
- Canonical V1 and V2 behavior outside the project-checkpoint signature is unchanged.
- Antigravity's current sandbox-file input mechanism is unchanged.

## Test Strategy

Every production change follows red-green-refactor.

1. **Source lifetime:** a fake transport observes exact frozen bytes inside a private temporary
   directory outside `.reviewctl`; the backend request retains original roots, adds that temporary
   root, and keeps the project root first. Success, nonzero execution, fallback, and exception
   paths remove every normally cleanable observed path and create no durable `source` directory.
   Fallback attempts receive distinct temporary directories. An original-file mutation does not
   alter staged bytes. An injected cleanup failure prevents acceptance and still leaves no source
   copy or temporary path below `.reviewctl`; the test does not claim the hostile filesystem
   removed the external directory.
2. **Checkpoint classification:** a self-consistent marked checkpoint and a historical unmarked
   project checkpoint both fail global verification with
   `project-checkpoint-not-review-receipt`; direct internal verification still detects tampering
   and expected-digest mismatch. Canonical V1 and V2 controls retain their existing outcomes. A
   recomputed generic V1 control augmented only with `configDigest` still follows the V1 path;
   adding any project-only field makes it a rejected checkpoint even if `result`, extra keys, or a
   schema-version key are also present. Deleting or adding one unrelated key cannot evade the
   subset classifier. A `RuntimeError` transport probe also demonstrates cleanup before the
   unexpected exception propagates.
3. **Template:** all init modes generate empty routes and local execution with no provider/model
   text. Reviewing immediately after initialization returns `route_invalid`, creates no review
   artifact or journal event, and invokes no transport.
4. **Antigravity:** separate `null`, string, number, array, and boolean structured outputs fail
   closed while preserving raw response evidence. An absent structured-output field still permits
   legacy response fallback, a valid object remains canonical, duplicate/non-finite nested values
   still fail the exact parser, and a valid JSON object with an invalid review-contract shape is
   rejected by contract evaluation rather than accepted.
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
