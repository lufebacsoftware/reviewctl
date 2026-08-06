# Pi and reviewctl Boundary

## Decision

`pi` is the interactive exploration surface. `reviewctl` is the formal review
and evidence surface. The two tools may use the same model providers, but an
interactive `pi` response is never a review approval or merge-gate receipt.

## Responsibilities

`pi` owns short-lived or resumable conversations used to explore a design,
inspect a checkout, run a REPL, and test a proposed command flow. Each thread
uses its own session directory. The interactive lane may select providers and
models supported by the local `pi` installation, but provider availability is
not evidence of qualification.

`reviewctl` owns the bounded review request, frozen source packet, transport
selection, privacy policy, response contract, persisted receipt, verification,
and optional Age-sealed request/response archive. Its artifact root is the
canonical archive for formal review evidence.

## Promotion flow

1. Use `pi` to explore the question without writing into the review artifact
   root.
2. Convert the agreed question into a stable prompt file and choose the exact
   source files to attach.
3. Run `reviewctl run` with the applicable transport and response contract.
4. Run `reviewctl verify` against the persisted receipt.
5. Independently verify every material finding against the source and tests.

The promotion step is deliberate. It prevents a conversational answer from
silently becoming an approval and gives the formal review a frozen input and
an auditable output.

## Provider boundary

`pi` may expose direct Google, OpenAI, or OpenRouter models when configured.
`agy` remains a separate `reviewctl` transport; invoking it from a `pi` shell
tool is exploratory and does not turn the resulting conversation into a
`reviewctl` receipt. Formal reviews use the transport and policy recorded by
`reviewctl`.

## Failure and privacy rules

- Never write `pi` session files below a `reviewctl` artifact root.
- Never attach a `pi` transcript as if it were a frozen source file.
- A provider failure in `pi` is an interactive failure, not a review result.
- A missing, unavailable, malformed, or unverified `reviewctl` receipt is not
  an approval.
- Proprietary source follows the organization's policy; an interactive model
  cannot widen that policy.
