# reviewctl operating modes

Status: manifest and sequential formal chunk phases implemented. This document
records the boundary between planning artifacts and verified formal evidence.
`reviewctl range-review` freezes a planning-only manifest with repository and
revision arguments, or consumes that manifest for one bounded formal review per
chunk. `reviewctl range-verify` is the fail-closed aggregate gate.

## Why separate modes

`reviewctl` now has three different jobs that must not share an authority
contract accidentally:

1. Explore an idea or prepare a review with an interactive model.
2. Produce formal, frozen-input review evidence for a bounded change.
3. Compare model families on synthetic cases under an explicit budget.

Many commits may accumulate before a review. A range review adds a fourth job:
compare one immutable `base..head` range while allowing the packet to be split
into bounded chunks. It must never aggregate findings from different heads.

## Current and proposed mode matrix

| Mode | Entry point | Typical transports | Input | Output | Authority |
| --- | --- | --- | --- | --- | --- |
| `explore` | `reviewctl explore start/resume` | Pi (optionally another interactive model) | Working question and optional context | Session, notes, promoted prompt | Advisory only; no merge approval |
| `review` | `reviewctl run` | Codex, OpenRouter, Gemini, Kiro, LLM, Pi | Explicit frozen files and prompt | Verified receipt for one bounded snapshot | Merge-gate evidence only after receipt verification and project policy |
| `range-review` | `reviewctl range-review` (manifest or formal mode) | One selected `reviewctl run` transport per chunk, sequentially | Repository and revisions for planning, or an immutable manifest plus prompt/model for formal mode | Deterministic manifest, per-chunk v2 receipts, and a signed aggregate artifact | Manifest is planning-only; aggregate is formal evidence only after `range-verify` succeeds |
| `range-verify` | `reviewctl range-verify` | None | Frozen manifest, aggregate, and referenced receipts | JSON validity result and violations | Merge-gate evidence only when valid and all chunk receipts are accepted |
| `tournament` | `reviewctl tournament` | Synthetic candidate routes | Synthetic cases, candidate roster, token and spend budget | Private tournament report and receipts | Qualification/decision support, not source approval |

Pi output can inform a later prompt, but it cannot be promoted into approval
without a new formal run over frozen source. A model name, a successful process
exit, or a valid receipt signature is not itself a correctness judgment.

## `range-review` contract

The command should resolve and freeze the range before starting any model:

```text
repository identity
baseSha
headSha
mergeBaseSha (when applicable)
comparison = base..head
canonicalDiffSha256
contextLines
chunkingVersion
chunkCount
```

The manifest should contain ordered, non-overlapping chunks. Each chunk records
its index, patch digest, supplied path/basename list, and bounded line/file
limits. Every chunk receipt repeats the range identity, canonical diff digest,
chunking version, chunk count, index, and chunk digest. The aggregate receipt
must reject a missing, duplicated, reordered, or mismatched chunk rather than
silently consolidating it. Each chunk receipt carries an
`extension.rangeReview` identity containing the manifest digest, range identity,
chunk index/count, and patch digest. `range-verify` also validates each
referenced schema-v2 receipt, its persisted file digest, and its source patch
hash.

The diff is computed once from the frozen repository state. A retry may change
transport or model, but it may not recompute the range. A changed `headSha`,
`baseSha`, diff digest, chunking version, or context policy starts a new review
ID. The aggregate must retain the exact reviewed head even when the branch has
advanced afterwards.

The implemented manifest phase uses a deterministic patch splitter with hard
byte/file limits and writes the manifest before any request. The formal phase
reads those exact chunk bytes, writes private temporary patch/context files,
and invokes the existing `reviewctl run` contract sequentially. It does not try
to infer a semantic dependency graph or silently widen a chunk when a file is
large. A failed or missing child receipt persists an explicit `incomplete`
aggregate and exits non-zero; an empty range cannot become an approval.

## Routing policy

- Use Pi for short, iterative exploration and review preparation. Store its
  session artifacts separately from formal receipts.
- Use Codex through `reviewctl` for the independent merge-gate lane when the
  organization policy allows it.
- Use GLM-5.3-Flash, Muse, Qwen, or other OpenRouter models through persisted
  receipts for bounded comparison or a policy-approved formal lane. Their
  direct output remains exploratory until the receipt is verified.
- Use Gemini/Kiro/local transports only within their declared source and
  identity boundaries. A provider catalog or a process exit does not certify a
  model's findings.

## Smallest safe backlog

1. ✅ Add a range manifest builder that records repository identity, base/head,
   merge-base semantics, canonical diff digest, and deterministic chunk IDs.
2. ✅ Add a bounded chunk packet writer and a receipt extension carrying the
   range identity on every accepted or failed attempt.
3. ✅ Add aggregate verification that is fail-closed for missing or mixed
   chunks, stale receipt files, and invalid v2 receipts.
4. ✅ Add CLI execution and verification paths plus tests for a multi-file range,
   missing child output, reordered chunks, mixed identities, and oversized input.
5. Keep model-specific routing and parallel chunk execution behind a separate
   design/review. The current formal implementation intentionally uses one
   selected route sequentially so coverage and failure behavior remain auditable.

Until a range aggregate passes `reviewctl range-verify`, use `reviewctl run`
with an explicit frozen file set and record the reviewed commit separately; do
not imply that an incomplete range has been reviewed as one formal unit.
