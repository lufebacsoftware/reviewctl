# Tournament scoring semantics design

Date: 2026-08-27

## Problem

`score_findings()` currently uses one `matched` counter for two different
questions:

1. Did the reviewer locate an adjudicated defect?
2. Did the reviewer assign at least the rubric's minimum severity?

A line-accurate finding below the minimum severity fails the second check and
is therefore also counted as a false positive. This makes `precision` and
`recall` describe severity qualification rather than defect detection, while
`falsePositives` labels a real located defect as invented.

The failure was reproduced in the value-model preflight: two high-severity
findings at the adjudicated outbox location were reported as two false
positives and zero recall because the rubric minimum was critical.

## Intended semantics

Tournament output will distinguish location detection from severity
qualification:

- `matched`: number of distinct expected findings detected at an adjudicated
  filename or symbol and line range, regardless of reported severity.
- `severityQualified`: number of distinct expected findings for which at least
  one location-accurate reported finding meets or exceeds the rubric minimum.
  This is evaluated independently of which duplicate first established the
  location match, so a later higher-severity duplicate can qualify the defect.
- `falsePositives`: reported findings that do not detect any adjudicated
  location, plus additional duplicate findings after an adjudicated finding
  has already been matched once.
- `lineAccurate`: reported findings at any adjudicated filename or symbol and
  line range. This remains a per-reported-finding count and may therefore be
  greater than `matched` when a model duplicates a defect.
- `precision`: `matched / len(findings)`, or `1.0` when there are no findings.
- `recall`: `matched / len(expected)`, or `1.0` when there are no expected
  findings.
- `qualifiedRecall`: `severityQualified / len(expected)`, or `1.0` when there
  are no expected findings.

One reported finding can establish at most one new location match, and one
expected finding can be location-matched at most once. Severity qualification
also assigns each reported finding to at most one not-yet-qualified expected
finding. When multiple rubric entries share a location, both assignments remain
deterministic in rubric order.

## Example

For one expected critical outbox defect and two reported high findings at that
same adjudicated location:

```json
{
  "expected": 1,
  "matched": 1,
  "severityQualified": 0,
  "falsePositives": 1,
  "lineAccurate": 2,
  "precision": 0.5,
  "recall": 1.0,
  "qualifiedRecall": 0.0
}
```

The first finding demonstrates detection. The second is a duplicate for
scoring purposes. Neither qualifies the rubric's critical severity.

## Compatibility

The existing keys keep their types. Their documented meaning becomes
internally consistent with their names: `matched`, `precision`, and `recall`
measure detection, while the new keys expose severity qualification.

Consumers that need the old minimum-severity gate must switch from `recall` to
`qualifiedRecall`. Because tournament reports are private qualification
artifacts rather than a stable public interchange contract, no schema-version
change is introduced in this bounded correction.

Existing receipts are immutable and will not be rewritten. Rerunning a
tournament creates a new report with the corrected metrics.

## Implementation boundary

Change only:

- `score_findings()` in `src/reviewctl/cli.py`;
- focused scoring tests in `tests/test_run.py`;
- scoring documentation in `docs/TOURNAMENT.md`.

Do not change OpenRouter transport, provider routing, reasoning controls,
timeouts, candidate budgets, receipt validation, or existing evidence.

## Test strategy

1. Add a regression test for one critical expected finding and two high
   findings at the same location. Verify the exact example above and observe
   it fail against the current implementation.
2. Update the existing mixed scoring test to assert detection and qualified
   recall separately.
3. Preserve tests for wrong filenames, accepted line ranges, and upward
   severity escalation.
4. Run the focused scorer tests, then the full test suite and formatting/lint
   checks.
5. Re-score the preserved Muse outbox findings with the corrected function and
   verify the expected metrics without mutating the original tournament
   artifact.

## Success criteria

- A real location match below minimum severity is never counted as a false
  positive solely because of severity.
- Severity underestimation remains visible and cannot satisfy
  `qualifiedRecall`.
- Duplicate findings do not inflate distinct defect recall.
- Existing clean-case and location-safety behavior remains intact.
- No unrelated dirty work enters the implementation commits.
