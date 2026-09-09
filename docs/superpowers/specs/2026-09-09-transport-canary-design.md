# Transport Canary Design

## Purpose

`reviewctl setup check` establishes only local executable availability. A
transport canary must establish whether a named local route profile can complete
one bounded, synthetic `findings-json` review and produce a verifiable receipt.

## Scope

Add `reviewctl transport-canary --profile NAME`. The command creates a
disposable synthetic source file, requests an exact approved findings response
with a required reviewed-file declaration, invokes the existing `run` control
plane once, and writes a `transport-canary.json` report beside the resulting
receipt.

The command does not alter profiles, credentials, provider guardrails, or
qualification policy. It reports observed operability for this execution only.

## Contract

The report is canonical JSON with:

- `kind: "transport-canary"` and `version: 1`;
- profile name, resolved ordered routes, and profile config provenance;
- canonical receipt path and its SHA-256;
- receipt result, accepted attempt number, and the command exit status.

The command returns zero only when the underlying synthetic review is accepted.
An unavailable or incomplete receipt is retained and reported but returns the
underlying nonzero status. A failure before receipt creation writes no report.

## Boundaries

The command reuses `run_review`; it cannot bypass frozen input assembly,
contract evaluation, policy behavior, fallback recording, receipt construction,
or runtime logging. The canary source is synthetic, so no proprietary source is
sent and no organization policy is needed for source authorization.

## Tests

Tests use the repository's fake `llm` executable and a temporary profile config.
They assert accepted and unavailable receipts, report schema/provenance, exact
exit propagation, and no report when profile validation fails.
