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

For formal Pi routes, `MODEL` must be provider-qualified, for example
`openrouter/google/gemini-2.5-flash`. Unqualified Pi model names are rejected
because they cannot bind the expected provider identity.

A formal result requires `receipt.result` to be `accepted`, `acceptedAttempt`
to name the accepted attempt, successful receipt verification, and independent
checking of material findings. A missing, empty, unavailable, rejected, or
unverified receipt is not an approval. Hash verification alone proves receipt
integrity, not acceptance.

For machine-readable guidance:

```bash
reviewctl help-llm --format json
```

## Local backend setup

Setup diagnostics are local, read-only, and non-qualifying. They inspect the
current machine's registered backend executables with version-only probes. They
do not call models or providers, probe credentials, log in, write configuration,
or create files. Diagnostic values are bounded and credential-shaped values are
redacted.

```bash
reviewctl setup discover --format json
reviewctl setup show --format json
reviewctl setup check --backend NAME --format json
```

`discover` and `show` print the same observed topology and return success.
`check` accepts repeatable `--backend` options; without them it checks every
local executable backend. Availability is not qualification: an executable can
be `available` while its review qualification remains `unqualified`.

Remote API backends have local availability `not-applicable` and are never
credential-probed by setup. They do not fail an unfiltered check of local
executables. Explicitly selecting one reports its non-qualifying state and exits
`1`. Missing or unverified selected executables also exit `1`.

## Diagnose failures

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
