# reviewctl operating modes

Status: manifest phase implemented. This document records the boundary between
the modes that exist today and the still-pending formal range-review transport.
`reviewctl range-review` currently freezes a planning-only manifest; it does
not invoke a model or produce approval evidence.

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
| `range-review` | `reviewctl range-review` (manifest phase) | None yet; no model is invoked | Repository, `base`, `head`, bounded context and chunk limits | Planning-only manifest with frozen patch chunks | Not review evidence; formal evidence requires the pending per-chunk and aggregate receipts |
| `tournament` | `reviewctl tournament` | Synthetic candidate routes | Synthetic cases, candidate roster, token and spend budget | Private tournament report and receipts | Qualification/decision support, not source approval |

Pi output can inform a later prompt, but it cannot be promoted into approval
without a new formal run over frozen source. A model name, a successful process
exit, or a valid receipt signature is not itself a correctness judgment.

## Proposed `range-review` contract

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
silently consolidating it.

The diff is computed once from the frozen repository state. A retry may change
transport or model, but it may not recompute the range. A changed `headSha`,
`baseSha`, diff digest, chunking version, or context policy starts a new review
ID. The aggregate must retain the exact reviewed head even when the branch has
advanced afterwards.

The implemented manifest phase uses a deterministic patch splitter with hard
byte/file limits and writes the manifest before any request. It does not try
to infer a semantic dependency graph or silently widen a chunk when a file is
large. The manifest stores the exact chunk bytes so later transports do not
recompute the range.

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
2. Add a bounded chunk packet writer and a receipt contract carrying the range
   identity on every attempt.
3. Add aggregate verification that is fail-closed for missing or mixed chunks.
4. Add CLI documentation and fixtures for a three-commit range, a changed head,
   a repeated chunk, and an oversized file.
5. Only then add model-specific range routing or parallel chunk execution.

Until those steps land, use `reviewctl run` with an explicit frozen file set and
record the reviewed commit separately; do not imply that a delayed commit range
has been reviewed as one formal unit.
