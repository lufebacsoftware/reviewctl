# Model Tournament

## Goal

The tournament selects review roles from measured behavior, not vendor claims, popularity, or a single
benchmark. Coding indices and public leaderboards are discovery inputs only.

## Corpus rules

The pilot contains eight synthetic cases: duplicate posting, commodity equilibrium, replay protection,
outbox leases, unsafe migrations, schema parity, and two clean diffs. Each case has
an adjudicated expected finding set before any model runs.

Models receive the same bounded packet and a JSON findings contract. Scores reward precise,
reproducible findings and penalize false blockers on clean cases. Metrics include weighted recall,
precision, line accuracy, JSON compliance, transport success, latency, token use, and cost.

## Direct OpenRouter transport

Set `transport = "openrouter"` in a tournament plan to call the OpenRouter chat-completions endpoint
directly. A policy file is optional metadata: when supplied, its SHA-256 is included in the receipt but
does not block a selected model or transport. The runner, rather than the model, records the frozen
source hashes. It persists a sanitized canonical request (never its authorization header), the raw
provider response, resolved model, provider, usage, and cost in the attempt receipt.

The portable findings schema contains only `verdict` and `findings`. A response with findings must use
`changes-requested`; `approved` requires an empty array. Finding severity is a closed vocabulary:
`critical`, `high`, `medium`, `low`, or `info`. Native Codex keeps separate source-read proof through its
isolated tool trace. Do not make an OpenRouter model calculate file hashes as a proxy for having read an
attachment.

### Reproducible provider routing

Direct OpenRouter reviews can pin routing in a tournament plan:

```toml
transport = "openrouter"
provider = { only = ["provider-a"], allow_fallbacks = false, require_parameters = true, data_collection = "deny" }
```

The equivalent `run` flags are `--provider-only`, `--provider-order`,
`--no-provider-fallbacks`, `--provider-require-parameters`, `--provider-data-collection`, and
`--provider-sort`. The runner persists the requested policy in the receipt, records the actual provider
returned by OpenRouter, and rejects a result whose resolved provider does not match a pinned `only`
provider. Omit all of these fields to retain OpenRouter's normal routing behavior. A tournament-wide
provider policy applies to every listed model, so use a dedicated plan when a provider hosts only one
candidate.

Before a pinned provider tournament, run:

```bash
reviewctl provider-preflight --plan /path/to/provider-comparison.toml
reviewctl tournament --plan /path/to/provider-comparison.toml --stage filter
```

`provider-preflight` writes `provider-preflight.json` under the plan's artifact root. It snapshots live
endpoint metadata and rejects a route that is missing, inactive, lacks `response_format` or
`structured_outputs`, or differs from the plan's declared per-million-token price. It is an availability
and contract-metadata check, not a quality approval or a substitute for a real structured request: retain
the response receipt and require the intended number of accepted cases before promoting a provider.

### Candidate output budgets

`max_output_tokens` is required at the plan level and may be overridden by an
individual candidate. Use an override only when a model's reasoning budget
would otherwise prevent a structured final response. `reviewctl` uses the
effective value both in the provider request and in the reserved maximum-cost
calculation. Every run persists `requestedMaxOutputTokens`, the effective
`maxOutputTokens`, and `outputTokenLimitEnforced`; this keeps transports such as
Codex, Pi, and Gemini from being mistaken for hard output-token caps.

Do not place a model that needs a pinned host in a shared multi-model plan. Use
a dedicated plan for each provider experiment so a fallback cannot silently
change the evidence set. Candidate-specific budgets, provider choices, and
results belong to private evidence, not this repository.

## Native Gemini through Antigravity

Use `reviewctl run --transport agy --model <native-model-id>` for a bounded synthetic review
through the local Antigravity CLI. The runner embeds frozen synthetic fragments in the prompt, launches
`agy` from an empty temporary directory with its sandbox enabled, disables slash-command expansion, and
persists the sanitized request, raw CLI JSON, conversation ID, duration, and token counters. It rejects
proprietary source before launching the CLI.

`agy` does not expose a provider cost in its documented print-mode protocol. It is therefore a native
qualitative lane. Legacy homogeneous tournaments reject it because their single budget must be fully
enforceable. Mixed product tournaments may include it as an explicitly `subscription` candidate: it is
recorded, never billed to the metered OpenRouter total, and never used to distort recurring-cost
rankings. Its selected model is recorded from the invocation; unlike direct OpenRouter, Antigravity does
not currently echo a provider-resolved model identifier, so that field is not independent provider
attestation.

The public fixtures do not announce their defects in source comments or
docstrings. The organization-owned rubric must define a finding by source
filename or adjudicated symbol and range, with a minimum severity. This favors
verifiability over semantic fuzziness while preserving conservative escalation.
A human council reviews title quality, reasoning, lower-severity near-misses,
and disputed extra findings before promoting a model.

`reviewctl` appends the permitted basenames to each packet and requires the model to use one of them in
the `path` field. It rejects duplicate basenames at input time. The original prompt and the assembled
packet have separate hashes in the receipt; the budget estimate includes that assembled packet and the
attached source bytes.

The public repository includes neutral synthetic fixtures. A qualification plan,
its provider snapshot, and results belong to the organization's private evidence
repository. Synthetic qualification does not authorize source-bearing review.

## Budget and progression

Every request reserves its maximum output cost before execution. The report
includes actual provider cost when available and becomes input to the next
round. Do not broaden the corpus or promote a model until the private pilot
report is reviewed.

Run a single named case with
`reviewctl tournament --plan /path/to/organization-tournament.toml --case <id>`.
A later full run uses the same plan without `--case`. The organization policy
sets timeout and retry rules.

The candidate pool is organization-owned. A premium or reasoning variant is not
promoted merely because it costs more: the qualification corpus must show a
material gain in recall, precision, or adjudication quality.
