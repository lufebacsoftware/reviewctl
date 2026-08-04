# reviewctl

Evidence-backed, organization-neutral control plane for bounded LLM code review.

`reviewctl` is owned as one shared tool, but it never owns a product's evidence, credentials,
or policy. Each organization runs the same CLI against its own policy file and private receipt
repository. It uses `llm` as a transport, not as the source of review governance.

## Install

```bash
uv tool install git+https://github.com/lufebacsoftware/reviewctl.git
```

For local development, run `uv sync --all-groups` followed by `uv run pytest`.

## Optional runtime tools

The core CLI is Python-only and does **not** require Node.js. Transports and
evidence features call external tools only when selected:

| Feature | External requirement |
| --- | --- |
| `llm` transport | `llm` with the configured provider plugin |
| Codex transport | Codex CLI; macOS `sandbox-exec` for proprietary-source isolation |
| OpenRouter direct transport | `curl` and `OPENROUTER_API_KEY` |
| Gemini/Antigravity transport | `agy` |
| Sealed evidence | `age` |

Missing optional tools produce a typed unavailable attempt; they never count
as an accepted review.

## Run a review

```bash
reviewctl run \
  --review-id payment-idempotency \
  --prompt-file review-request.md \
  --model openrouter/moonshotai/kimi-k2.7-code \
  --file src/posting.py \
  --file tests/test_posting.py \
  --file migrations/004_posting.sql \
  --source-class proprietary \
  --policy org-policy.toml \
  --response-contract findings-json \
  --seal-to age1auditrecipient...
```

Each model attempt receives a fresh SQLite log database. The runner snapshots each selected file
before invoking a model and records its digest in the receipt provenance. A model declares only the
frozen snapshot paths it reviewed; it never supplies an unverifiable hash. A receipt is accepted only
when the persisted response is non-empty, complete, tied to the requested model, and tied to a
persisted conversation. Finding paths must name an attached file exactly. With `--seal-to`, the exact
request and response are encrypted with Age; the visible receipt contains only hashes, provenance,
result metadata, and structured findings.

## Commands

```bash
reviewctl verify receipt.json
reviewctl policy-check --policy org-policy.toml --model <model-id>
reviewctl tournament --plan /path/to/organization-tournament.toml
reviewctl tournament --plan /path/to/organization-tournament.toml --case <case-id>
reviewctl provider-preflight --plan /path/to/provider-comparison.toml
reviewctl blind-package --report organization-tournament-artifacts/tournament.json \
  --output council/public-proposals.json --mapping-output council/restricted-identity-map.json
reviewctl council-plan --plan /path/to/organization-tournament.toml \
  --blind-package council/public-proposals.json --mapping council/restricted-identity-map.json \
  --output council/assignments.json
```

Use Codex through the same receipt flow when the organization policy permits the selected model:

```bash
reviewctl run \
  --review-id bounded-change-codex \
  --transport codex \
  --model <approved-codex-model> \
  --prompt-file review-request.md \
  --file src/posting.py \
  --source-class proprietary \
  --policy org-policy.toml \
  --response-contract findings-json
```

For proprietary reviews on macOS, the Codex transport creates an ephemeral `CODEX_HOME`, copies only
the required Codex authentication file, and uses `sandbox-exec` to deny reads from the original source
roots. Codex works from frozen snapshots rather than the checkout. This is a targeted source boundary,
not a claim that an LLM process is isolated from every host system path. The transport records the
Codex session identifier and removes the plaintext final message after extracting its hash and
validated findings. Codex `*-pro` availability depends on the organization's Codex account; use a
successful synthetic qualification before making a role mandatory.

The tournament command accepts only synthetic cases by policy. It estimates the maximum spend from
the assembled packet and attached file bytes before each request, then stops before crossing the
configured budget. A candidate may override the plan's `max_output_tokens`; the runner reserves that
candidate's effective cap and records it as `maxOutputTokens` alongside the estimate and actual provider
cost when available. This allows reasoning-first models to receive enough completion budget without
raising every candidate's spend ceiling.

For an isolated OpenRouter provider comparison, run `provider-preflight` immediately before the
tournament. It snapshots the live endpoint catalog, checks the pinned provider, declared price, active
status, `response_format`, and `structured_outputs` support, then records the snapshot beside the
receipts. The tournament still attests the resolved provider on every response; catalog metadata is not
proof that a provider can complete a particular structured request.

OpenAI/Codex reviews are an approved independent lane, run through the organization's Codex account
and retained in that organization's evidence repository. `reviewctl` currently controls the portable
`llm` packet transport; it does not route proprietary source through OpenRouter by default.

Read [the council policy](docs/COUNCIL.md), [evidence contract](docs/EVIDENCE.md),
[project-instruction integration guide](docs/PROJECT-INTEGRATION.md), and
[tournament guide](docs/TOURNAMENT.md) before adding the tool to CI. Model
qualification, operating rosters, provider experiments, and retained receipts
belong to the organization's private evidence store.
