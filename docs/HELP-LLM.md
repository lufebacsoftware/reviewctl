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

Exploration sessions are resumable and retain prompts, Pi events, responses,
diagnostics, and session state. Their response is exploratory working material,
not an approval. The default tool set is read-only: `read,grep,find,ls`.
Selecting `bash`, `edit`, or `write` explicitly expands that boundary and must
be a deliberate local decision.

## Formal review

```bash
reviewctl run --review-id ID --transport TRANSPORT --model MODEL \
  --prompt-file FILE --file SOURCE
reviewctl verify RECEIPT.json
```

A formal result requires a non-empty persisted receipt, successful
verification, and independent checking of material findings. A missing,
empty, unavailable, or unverified receipt is not an approval.

For machine-readable guidance:

```bash
reviewctl help-llm --format json
```

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
