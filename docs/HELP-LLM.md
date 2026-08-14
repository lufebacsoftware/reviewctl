# reviewctl Help for LLMs

Use `reviewctl explore` for resumable Pi conversations and product or
architecture exploration. Use `reviewctl run` for a bounded formal review.

## Exploration

```bash
reviewctl explore start --id ID --model MODEL --cwd PATH --prompt "QUESTION"
reviewctl explore resume --id ID --prompt "NEXT QUESTION"
reviewctl explore show --id ID
reviewctl explore promote --id ID --output PATH
```

Exploration sessions are resumable. Every turn retains `request.md` and
`turn.json`; Pi events, responses, stderr, and session state exist only when Pi
actually produces the corresponding content. Runner failures such as a missing
executable are recorded in `turn.json:diagnostic`. A response is exploratory
working material, not an approval. The default tool set is read-only:
`read,grep,find,ls`.
Selecting `bash`, `edit`, or `write` explicitly expands that boundary and must
be a deliberate local decision.

## Formal review

```bash
reviewctl run --review-id ID --transport TRANSPORT --model MODEL \
  --prompt-file FILE --file SOURCE
reviewctl verify RECEIPT.json
```

For formal routes, `MODEL` must be qualified by the organization's private
policy and evidence store. This public guide intentionally contains no model
roster, prices, provider-specific invocation commands, or credentials.

A formal result requires `receipt.result` to be `accepted`, `acceptedAttempt`
to name the accepted attempt, successful receipt verification, and independent
checking of material findings. A missing, empty, unavailable, rejected, or
unverified receipt is not an approval. Hash verification alone proves receipt
integrity, not acceptance.

For machine-readable guidance:

```bash
reviewctl help-llm --format json
```

## Partial review results

Contract evaluation happens only after the transport, timeout, model,
provider, empty-response, and conversation pre-gates succeed. Its status is:

- `complete`: the whole typed contract is valid; only a complete eligible
  attempt can become `acceptedAttempt`.
- `incomplete`: the whole contract is not valid, but one or more complete
  findings can be preserved for bounded fallback.
- `invalid`: no finding can be safely preserved, or contract evaluation could
  not complete. It never promotes fragments.

Fallback receives revalidated fragments and a completion manifest bound to the
target contract. It never receives the raw response, inherits approval, or
treats absence as a dispute. The legacy view remains bound to the real accepted
attempt. The consolidated view keeps partial or unconfirmed findings visible,
so it can be stricter than the legacy approval.

`maxAttempts` is a per-route limit. A route fallback starts the destination
route's own bounded allowance; it does not extend either route indefinitely.

## Local backend setup

Setup diagnostics are local, read-only, and non-qualifying. They inspect the
current machine's registered backend executables with version-only probes.
Setup diagnostics observe only executable presence and version for registered
executable backends. Setup diagnostics never authenticate, call a model or
provider, or write configuration. They do not create files. Diagnostic values
are bounded and credential-shaped values are redacted.

```bash
reviewctl setup discover --format json
reviewctl setup show --format json
reviewctl setup check --backend NAME --format json
```

`discover` and `show` print the same observed topology and return success.
`check` accepts repeatable `--backend` options; without them it checks every
local executable backend. Availability is not qualification. An executable can
be `available` while its review qualification remains `unqualified`.

Remote API backends may execute providers or models remotely, but setup never
credential-probes them. They have local availability `not-applicable` and do not
fail an unfiltered check of local executables. Explicitly selecting one reports
its non-qualifying state and exits `1`. Missing or unverified selected
executables also exit `1`.

## Kiro backend

Kiro is supported by `run`, routes, and tournaments as a registered native
agent-CLI adapter, but it remains unqualified. Availability and a valid receipt
do not qualify a model. The organization owns qualification and its private
operating evidence.

The executable defaults to `kiro-cli`; set `KIRO_BIN` to override it. This
version-only check is local discovery and does not call a model or provider:

```bash
reviewctl setup check --backend kiro
```

Kiro owns the current runtime model inventory. Query the installed CLI, choose
an exact returned model ID, and pass it explicitly:

```bash
kiro-cli chat --list-models --format json
reviewctl run --review-id ID --transport kiro --model MODEL_ID \
  --prompt-file FILE --file SOURCE
reviewctl run --review-id ID --route kiro:MODEL_ID \
  --prompt-file FILE --file SOURCE
```

Illustrative example (current at writing; check
`kiro-cli chat --list-models --format json` before use):

```bash
reviewctl run --review-id ID --route kiro:claude-sonnet-5 \
  --prompt-file FILE --file SOURCE
```

The supported selection forms are `--transport kiro --model MODEL_ID` and
`--route kiro:MODEL_ID`. `auto` is rejected because the resolved identity is
unobservable. Do not copy the returned model roster, prices, credits, or
provider commands into repository or project instruction documents.

The adapter reuses the user's local Kiro subscription and login. It does not use
OpenRouter and does not inherit ambient provider, AWS, or API-token variables.
It uses a disposable empty working directory, reduced environment, built-in
`kiro_default` agent, no pre-trusted tools, an inline frozen packet, one total
timeout, private evidence, a dynamic model check, and session recovery. Those
controls are advisory read-only and tool controls with source isolation
unavailable; they are not OS sandbox enforcement.

Proprietary Kiro source requires an explicit policy allowing the exact Kiro
model before bytes are sent. Synthetic runs do not require that policy. Potzal
and federation remain unrelated and optional.

Handle Kiro agent errors as follows:

- Unknown or unlisted model: rerun
  `kiro-cli chat --list-models --format json` and select the exact returned ID.
- `auto`: select an explicit model ID.
- Missing or malformed session or inventory: inspect the attempt evidence and
  do not treat the result as approval.
- Missing executable: run `reviewctl setup check --backend kiro` and correct
  `KIRO_BIN` or the local installation.

## Diagnose failures

Errors are actionable for LLMs. Use the receipt fields instead of guessing or
retrying blindly:

- Incomplete: inspect `completionRequest`, `fallbackRelationships`, and
  `rawResponse`.
- If completion fails with `original prompt collides with completion framing`,
  remove the reserved `<reviewctl-completion-context>` marker (opening or
  closing form) from the original prompt and start a fresh bounded review.
- Invalid: inspect `violations`, `evaluationError`, and `rawResponse`.
- Accepted: inspect both the legacy and consolidated views, then run
  `reviewctl verify`.

`rawResponse` identifies the retained bytes by durable absolute path, SHA-256, and
character count. A missing response and a present empty response are different
facts.

Do not retry blindly. A failed formal run normally still prints its artifact
directory and persists `receipt.json` plus `attempts/NN/attempt.json`.

Read these fields in order:

1. `result` identifies delivery or acceptance failure, such as `timeout`,
   `transport-failed`, `model-mismatch`, `provider-mismatch`, `empty`,
   `missing-conversation`, or `incomplete`.
2. `diagnostic` is a bounded, redacted transport message. Never reconstruct or
   request credentials from a redacted value.
3. `validationError` explains the stable CLI contract boundary.
4. `contractEvaluation.violations` gives machine-readable `findings-json`
   violations such as `invalid-json`, `review-declaration`, `finding-path`,
   `verdict-invariant`, or `prepared-contract`.
5. `evidence` names the retained request, response, Pi event stream, session, or
   stderr files that exist for that attempt.

Exit status `1` means unavailable or invalid evidence, not approval. Exit status
`2` means the command, config, policy, or local input was rejected before a
review could run. Correct the named cause, create a new receipt, and run:

```bash
reviewctl verify RECEIPT.json
```

Never edit an old receipt to make it pass verification.

Receipt v1 verification remains digest-only. Receipt v2 verification also
checks its structure offline, including attempt identity, fallback provenance,
promotion, accepted-attempt binding, and consolidation. Verification does not
call a model or provider.

reviewctl is local-first. Registered adapters can still be unqualified;
availability is not qualification. Cursor and Claude Code are not claimed as
supported backends. BAML is an architectural inspiration only, with no runtime
dependency. Editable formal execution, project evidence-store integration,
and federation are deferred. Federation is optional future work, and Potzal is
not a dependency.
