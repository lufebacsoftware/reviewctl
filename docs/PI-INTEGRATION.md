# Pi and reviewctl

Use `pi` as the interactive workbench and `reviewctl` as the formal review and
evidence archive. They can use the same provider, but they have different
contracts.

## Division of responsibility

`pi` is for short exploratory conversations and resumable threads. Use it to
understand a design, inspect a checkout, run a REPL, try a report, or shape a
review question. Keep each thread in a dedicated session directory outside the
`reviewctl` artifact root.

`reviewctl` is for a bounded review. It freezes the selected source files,
applies the source and provider policy, invokes the selected transport, stores
the request and response evidence, and writes a verifiable receipt. Its
artifact root is the archive for formal reviews; use `--seal-to` when the
request and response must also be encrypted with Age.

An interactive `pi` transcript is never an approval, a merge decision, or a
formal review receipt. The transcript may inform the prompt, but the formal
question must be promoted through `reviewctl`.

## Promotion workflow

### 1. Explore with pi

Start a named, isolated thread. Limit tools to the smallest set needed for the
question, and do not point the session directory at a review artifact root.

```bash
pi \
  --name accounting-design \
  --session-dir "$TMPDIR/pi-accounting-design" \
  --tools read,grep,find,ls,bash \
  --no-approve \
  "Explore the proposed accounting change. Do not edit files or create commits.
   Separate observations from recommendations and suggest a bounded review question."
```

For a later turn, resume the same thread with `pi --continue` or select it
with `--session`. Treat the conversation as working notes, not evidence.

### 2. Freeze the formal request

Write the agreed question to a prompt file outside the interactive session
directory. Attach only the source and focused tests needed for that question.
Do not attach the entire `pi` transcript as a substitute for source files.

### 3. Run and archive the review

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
