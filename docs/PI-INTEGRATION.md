# Pi and reviewctl

Use `pi` as the interactive workbench and `reviewctl` as the formal review and
evidence archive. They can use the same provider, but they have different
contracts. `reviewctl` also exposes a `pi` transport for bounded, headless
runs; that transport is the only way a Pi response can enter the formal
receipt flow.

## Division of responsibility

`pi` is for exploratory conversations and resumable threads. Use the integrated
`reviewctl explore` commands to understand a design, inspect a checkout, run a
REPL, try a report, or shape a review question. Keep these sessions in the
exploration root, outside the formal `reviewctl` artifact root.

`reviewctl` is for a bounded review. It freezes the selected source files,
applies the source and provider policy, invokes the selected transport, stores
the request and response evidence, and writes a verifiable receipt. Its
artifact root is the archive for formal reviews; use `--seal-to` when the
request and response must also be encrypted with Age. The `pi` transport runs
with tools, extensions, skills, prompt templates, and context-file discovery
disabled; it archives Pi's JSON event stream, session file, stderr, and
extracted final response for every attempt, including failures and empty
responses.

An interactive `pi` transcript is never an approval, a merge decision, or a
formal review receipt. The transcript may inform the prompt, but the formal
question must be promoted through `reviewctl`. A `reviewctl --route pi:...`
attempt is formal only because `reviewctl` freezes the inputs, validates the
response contract, persists the attempt, and produces a receipt that still
requires `reviewctl verify`.

## Promotion workflow

### 1. Explore with reviewctl and pi

Start a named, resumable thread. The default tool set is read-only repository
inspection. Add `bash`, `edit`, or `write` only as a deliberate local execution
decision; the session is not a merge gate.

```bash
reviewctl explore start \
  --id accounting-design \
  --model openai-codex/gpt-5.6-sol \
  --cwd ~/Code/workspaces/ledger \
  --prompt "Explore the proposed accounting change. Do not edit files or create commits. \
Separate observations from recommendations and suggest a bounded review question."
```

Continue the same conversation:

```bash
reviewctl explore resume \
  --id accounting-design \
  --prompt "Inspect the relevant tests and refine the bounded review question."
```

`reviewctl explore show` prints the manifest, and each turn is retained under
`~/.cache/reviewctl/explorations/<id>/turns/`. The session JSONL is the Pi
conversation state; the per-turn event stream and response are diagnostic
working material.

### 2. Promote the formal request

When the exploratory question is ready, create a handoff package:

```bash
reviewctl explore promote \
  --id accounting-design \
  --output review-handoffs/accounting-design
```

This creates `prompt.md`, the latest exploratory response as `exploration.md`,
and a manifest. Attach only the source and focused tests needed for the formal
question; do not treat the exploratory response as a source of truth.

### 3. Run and archive the formal review

```bash
reviewctl run \
  --review-id bounded-accounting-change \
  --prompt-file review-request.md \
  --transport openrouter \
  --model openrouter/google/gemini-3.5-flash \
  --file src/accounting.clj \
  --file test/accounting_test.clj \
  --source-class proprietary \
  --policy org-policy.toml \
  --response-contract findings-json \
  --artifact-root review-artifacts \
  --seal-to age1auditrecipient...
```

For a headless Pi attempt or an ordered Pi fallback, select the transport
explicitly:

```bash
reviewctl run \
  --review-id bounded-accounting-change-pi \
  --route pi:openrouter/google/gemini-2.5-flash \
  --prompt-file review-request.md \
  --file src/accounting.clj \
  --file test/accounting_test.clj \
  --source-class proprietary \
  --response-contract findings-json
```

Pi's interactive session directory is not reused. Each attempt gets an
isolated session inside its review artifact directory, and an empty or failed
Pi process is recorded as unavailable rather than discarded or treated as an
approval.

Pi's current CLI has no output-token limit option. The request artifact records
`requestedMaxOutputTokens` together with `outputTokenLimitEnforced: false` so
automation cannot mistake the requested value for an enforced budget cap.

The same flow may use `--transport agy` for a bounded synthetic product
review, or the approved Codex transport for proprietary source. Do not infer
authorization from the model selected in `pi`; the formal transport and
policy are recorded by `reviewctl`.

### 4. Verify before using the result

```bash
reviewctl verify review-artifacts/bounded-accounting-change/*/receipt.json
```

Only a non-empty, accepted, verified receipt is a formal review result. Check
the frozen file manifest, commit or diff identity, tests, and every material
finding independently before merging.

## Provider notes

`pi` may expose direct Google, OpenAI, or OpenRouter models depending on local
credentials and installation. `agy` is a separate `reviewctl` transport. A
shell command or extension invoked from `pi` can call `agy`, but that output
remains interactive until `reviewctl` runs the formal request and persists its
own receipt.

If a provider is unavailable in `pi`, continue the thread only as exploration.
If a formal `reviewctl` attempt is unavailable, record that receipt as
unavailable; it is not an approval. A fallback across transports must remain
visible in the review artifacts and must not cross the organization's source
policy boundary.
