# Formal Range Review Implementation Plan

> **Status:** Implemented on `fix/transport-bugs`; verify the exact commit before
> using the aggregate as merge-gate evidence.

**Goal:** Consume one immutable range manifest, run the existing formal review
transport once per bounded patch chunk, and produce an aggregate that cannot be
accepted when any chunk or receipt is missing, reordered, mixed, or tampered.

**Architecture:** `reviewctl.range_review` owns manifest/aggregate identity and
pure verification. The CLI writes private temporary patch and context files and
invokes `reviewctl run` sequentially for each chunk. The child run persists a
normal schema-v2 receipt with `extension.rangeReview`; `range-verify` validates
that receipt and the exact persisted file/source hashes before accepting the
aggregate.

## Contract

- `range-review --repository ... --base ... --head ... --output ...` remains a
  planning-only deterministic manifest builder.
- `range-review --manifest MANIFEST --review-id ID --prompt ... --model MODEL
  --aggregate-output OUTPUT` runs formal chunk reviews. `--prompt-file` may be
  used instead of `--prompt`; transport, source class, policy, timeout, token,
  and retry settings are forwarded to each child `reviewctl run`.
- Every child receipt embeds the frozen manifest digest, range identity, chunk
  index/count, and patch digest in `extension.rangeReview`.
- The aggregate is `result: accepted` only when every chunk has an accepted,
  verified schema-v2 receipt. A child failure is persisted as `incomplete` and
  the command exits non-zero.
- `range-verify --manifest MANIFEST --aggregate AGGREGATE` is the only command
  that can classify the aggregate as valid formal evidence.

## Safety and limits

- Manifest patches are decoded and checked against their SHA-256 before model
  invocation; the Git range is never recomputed during formal execution.
- Child stdout/stderr capture is bounded and each child receives a unique review
  id, so one failed route cannot silently replace another chunk.
- Missing, duplicate, reordered, mixed, stale, or empty chunks fail closed.
- The implementation intentionally runs sequentially. Parallel transport and
  multi-model range routing require a separate design and review.

## Verification

- Unit tests cover identity, source hashes, receipt files, missing/reordered/
  mixed chunks, and empty ranges.
- CLI tests cover deterministic manifest creation, successful formal execution,
  persisted child identity, aggregate verification, and incomplete execution.
- Required checks: `uv run pytest -q`, `uv run ruff check src tests`, and
  `git diff --check`.
