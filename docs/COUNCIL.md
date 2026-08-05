# Review Council Policy

## Ownership and boundaries

`reviewctl` is a neutral CLI. It contains no consumer-specific defaults.
Each organization owns:

- a policy file with approved model capabilities and privacy status;
- its OpenRouter credentials and provider restrictions;
- a private evidence repository or encrypted evidence storage;
- the people who can approve waivers and decrypt retained review payloads.

The shared Obsidian vault is an index of sanitized council decisions. It must not contain source,
prompts, raw model responses, secrets, PII, vulnerability reproduction payloads, or proprietary
architecture details.

## Default review lanes

| Change type | Required independent review |
| --- | --- |
| Normal code PR | A source-capable code-review lane plus local verification |
| Financial logic, persistence, migrations, identity, security, release, concurrency | A source-capable code lane plus an independently qualified domain lane |
| UI | Code lane plus a qualified visual lane with real screenshots |

An organization policy selects models, retries, and escalation rules. Critical
changes remain blocked until the required capability is available or an approved
waiver records the replacement and reason.

No model finding is evidence by itself. The integrator must reproduce it, reject it with a concrete
reason, or implement and test the correction.

For product and architecture tournaments, the council receives blinded structured proposals. Every
proposal has one native Codex judge and one judge from a different model family; no model judges its own
family. Their scores recommend finalists, while the human adjudicator assigns
final roles and records the reason. Store that plan and its results in the
organization's private evidence repository.

Codex is the OpenAI review lane. `reviewctl --transport codex` runs it against frozen snapshots,
records the session identifier and outcome in the same receipt format as `llm`, and deletes the
plaintext final message after extraction. On macOS proprietary reviews it also denies the original
source roots through `sandbox-exec`; this is a targeted checkout boundary, not a whole-host isolation
claim. The organization policy must explicitly permit the exact Codex model for proprietary source.
Codex remains independent from `reviewctl`'s OpenRouter transport and does not relax the source policy
below.

## Privacy transition

The policy defaults external candidates to synthetic-only. Before a proprietary product packet is sent,
the organization must record its decision from the current OpenRouter endpoint compatibility matrix,
including Zero Data Retention and data-collection status, then set `source_allowed = true` for the
selected model. The preferred production profile is ZDR plus no data collection. The policy remains
organization-owned, so a team can authorize or revoke a model without changing the CLI.
