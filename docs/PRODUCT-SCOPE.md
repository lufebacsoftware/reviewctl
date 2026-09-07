# Product scope and viability decision

**Status:** decision record, 2026-09-01

## Decision

Keep `reviewctl`, but narrow it to a local-first evidence control plane for
bounded model reviews. Do not replace the core with a hosted reviewer or a
generic model runner. Do not expand the core into a model marketplace,
qualification authority, federation service, or change-writing agent.

The durable product boundary is:

```text
frozen source packet
        -> typed response contract
        -> bounded transport attempts
        -> controller-owned acceptance
        -> verifiable receipt
        -> append-only project journal
```

The value is the evidence boundary, not a claim that one model finds better
bugs than another. A receipt must make it possible to distinguish what was
requested, what was observed, what was accepted by the contract, and what is
eligible for an organization's decision.

## What belongs where

### `reviewctl` core

- bounded file selection and frozen-input provenance;
- versioned response contracts and semantic validation;
- transport-neutral attempt orchestration and fallback;
- receipt construction and offline integrity verification;
- explicit policy decisions and source-class handling;
- project journal facts, finding identity, and lifecycle projections.

### Extensions

- backend adapters, each with a common conformance contract;
- comment-only publishers such as GitHub integrations;
- import/export of opaque, reviewctl-defined evidence bundles;
- organization-supplied qualification evidence.

### Deferred or separate products

Model tournaments, councils, provider procurement, federation, editable
execution, and external evidence stores are useful experiments but are not the
core review loop. They must not redefine acceptance, receipts, or project
history. Sentrux belongs in this optional carrier/integration layer, after the
local loop is reliable.

## Independent review consensus

Independent advisory reviews converged on the same decision:

1. Keep the receipt, contract, provenance, and journal design.
2. Replace the current breadth with one demonstrable workflow.
3. Treat registered or available backends as advisory until they pass the same
   conformance suite and an organization qualifies them.
4. Freeze federation, tournament/council tooling, and new transports until the
   local loop is boringly reliable.
5. Make `findings-json` the primary merge-gate contract; retain other contracts
   for compatibility or explicitly non-gating product work.

The opinions differed on which backend should become the first gate lane. That
is an evidence question, not a model-quality question. At this decision point,
the lane with an accepted and verified formal receipt is the provisional
candidate; Pi remains exploratory until its bounded capture path is proven.

## Current blockers

These are gates for product readiness, not optional polish.

### 1. Acceptance has three distinct meanings

The system must expose separate, verifiable conclusions:

```text
integrity_valid
contract_valid
attempt_accepted
policy_allowed
backend_qualified
merge_gate_eligible
```

`result: accepted` means only that an attempt passed its declared controller
checks. It must not silently imply organizational qualification or merge-gate
eligibility.

### 2. Policy language and enforcement must agree

Documentation currently describes policy both as optional metadata and as a
preflight privacy gate. The implementation and receipt need one explicit model:
which policies are descriptive, which are enforced before source transmission,
and how an absent, stale, or waived decision appears in evidence.

### 3. Every advertised backend needs the same conformance bar

Each backend used in a gate must exercise, at minimum:

- identity observability;
- timeout and retry behavior;
- empty and malformed response handling;
- credential and diagnostic redaction;
- tool and working-directory boundaries;
- output-limit capability and whether it is actually enforced;
- frozen-input and source-isolation behavior.

Availability, a successful process exit, or a structurally valid receipt is not
qualification.

### 4. Pi bounded capture needs a real canary

Formal Pi attempts with high reasoning settings currently fail with
`Pi transport output exceeded bounded capture`. This is a transport/capture
failure, not evidence that the model response is invalid. The implementation
must separate event-stream capture from final-response evidence, enforce limits
without truncating a valid final response, and record truncation honestly.

### 5. Journal and artifact recovery needs proof

Test interruption between receipt and journal publication, projection rebuild,
concurrent writers, orphaned artifacts, permissions, retention, and a full
disk. The recovery behavior must be deterministic and documented before the
journal is treated as operational authority.

### 6. The quality gate must be meaningful

The configured branch-coverage gate is currently 99.98% because one CLI branch
is untested. Either cover it or change the declared threshold deliberately; an
absolute gate that is knowingly red undermines the evidence model.

## Canonical workflow

The default supported workflow should be one engine behind one vocabulary:

```text
init
  -> doctor
  -> review (bounded packet, explicit contract and policy)
  -> findings (stable identity and lifecycle)
  -> verify (receipt and journal integrity)
```

`run` may remain a compatibility entry point, but it must use the same contract,
policy, acceptance, receipt, and journal semantics. The project workflow should
not be described as intrinsically Pi-backed; Pi is one adapter.

## Viability gates and roadmap

### P0 — correctness closure

- cover the remaining CLI branch and keep the 100% gate green;
- preserve the exact Pi request manifest in backend evidence;
- normalize configuration values consistently across API and CLI;
- define and verify the six-state decision model above;
- close the policy enforcement contradiction;
- fix Pi bounded capture and add a subprocess canary.

### P1 — one trustworthy operating loop

- choose one provisional gate lane based on accepted, verified evidence;
- keep other transports explicitly advisory until conformance passes;
- run the complete lifecycle on at least two real repositories repeatedly;
- test crash recovery, retention, permissions, and disk exhaustion;
- publish a capability matrix without embedding model rosters or prices.

### P2 — product convergence

- unify `review` and `run` behind one engine;
- make `findings-json` the primary gate contract;
- keep `document` and product contracts for bounded non-gating work;
- align README, help, architecture, and handoff claims with shipped behavior.

### P3 — narrow integrations

- add one comment-only publisher with stale-head and stable-finding handling;
- consider Reviewdog or Danger as publication helpers, not evidence authorities;
- define a versioned bundle export/import interface;
- evaluate Sentrux only as an optional carrier of opaque reviewctl evidence.

### Explicitly deferred

Federation, signed exchange, tournaments, councils, roster ownership, and
editable execution remain separate research or extension tracks until P1 is
complete. No new transport should be added merely because it is available.

## Replace-or-continue rule

Continue investing if the P0/P1 gates produce accepted, verifiable, actionable
findings in a repeatable local workflow. Reconsider replacement only if the
project cannot converge on one front door, one policy model, uniform eligibility,
and a common backend conformance bar without retaining overlapping products in
the same CLI.

If the actual goal is only hosted pull-request comments, compose an existing
review/comment tool instead. That is a different product from reviewctl's
evidence-control-plane goal.
