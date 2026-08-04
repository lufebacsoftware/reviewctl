# Synthetic Evaluation Corpus

This repository includes neutral financial and distributed-systems fixtures for
testing the transport and structured-review contract. Every fixture is a small
synthetic program; it is not copied from a consumer, library, or production
system.

## Financial-integrity fixture set

| Case | Invariant | Expected finding |
| --- | --- | --- |
| Commodity equilibrium | Different commodities cannot net without a price relation. | high |
| Posting identity | One source fact creates one execution regardless of mapping version. | critical |
| Dimension requirement | An account-required dimension must be present before posting. | high |
| Clean posting | Required dimensions and source identity are enforced. | none |

## Distributed-reliability fixture set

| Case | Invariant | Expected finding |
| --- | --- | --- |
| Outbox lease | Claiming is atomic and respects an active lease. | critical |
| Envelope replay | A signed envelope binds request ID, nonce, payload hash, and expiry. | critical |
| Movement trace | Settlement needs provider evidence and reconciliation linkage. | high |
| Clean relay | A relay has an idempotent lease and persistence-before-settlement. | none |

## Boundaries

Synthetic results qualify a model's transport and reasoning behavior only. They do not authorize
source-bearing review. A real source packet uses frozen snapshots and the
owning organization's approved policy. External models remain synthetic-only
until that organization records its provider and data-retention decision.
