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

- `scoreSchemaVersion`: integer `2`. Reports without this field use the legacy
  severity-gated scoring semantics.
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
- `qualifiedPrecision`: `severityQualified / len(findings)`, or `1.0` when
  there are no findings. This preserves the former severity-gated precision.
- `qualifiedRecall`: `severityQualified / len(expected)`, or `1.0` when there
  are no expected findings. This preserves the former severity-gated recall.

The scorer computes two independent maximum-cardinality bipartite matchings:

1. Detection edges connect a reported finding to every expected finding whose
   existing filename-or-symbol and line-range predicate it satisfies.
2. Qualification edges are the subset of detection edges where the reported
   severity meets or exceeds the expected minimum.

`matched` and `severityQualified` are the respective matching cardinalities.
This makes overlapping ranges and co-located rubric entries independent of
input order and guarantees `severityQualified <= matched`. The current
location predicate itself is unchanged. A duplicate that does not establish a
new detection match still counts toward `falsePositives`, but remains eligible
to qualify an expected defect in the independent severity matching.

The following invariants are normative:

- `matched <= lineAccurate <= len(findings)`;
- `severityQualified <= matched <= len(expected)`;
- `matched + falsePositives == len(findings)`;
- `qualifiedPrecision <= precision`;
- `qualifiedRecall <= recall`.

## Example

For one expected critical outbox defect and two reported high findings at that
same adjudicated location:

```json
{
  "scoreSchemaVersion": 2,
  "expected": 1,
  "matched": 1,
  "severityQualified": 0,
  "falsePositives": 1,
  "lineAccurate": 2,
  "precision": 0.5,
  "recall": 1.0,
  "qualifiedPrecision": 0.0,
  "qualifiedRecall": 0.0
}
```

The first finding demonstrates detection. The second is a duplicate for
scoring purposes. Neither qualifies the rubric's critical severity.

## Compatibility

The existing keys keep their types. Their documented meaning becomes
internally consistent with their names: `matched`, `precision`, and `recall`
measure detection, while the new keys expose severity qualification.

Consumers that need the old minimum-severity gate must switch from `precision`
and `recall` to `qualifiedPrecision` and `qualifiedRecall`. New score objects
declare `scoreSchemaVersion = 2`; historical objects without the field are
version 1. Historical and version-2 tournament scores are not directly
comparable.

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
2. Add a later-critical-duplicate case to prove a duplicate may qualify an
   already detected expected defect.
3. Add overlapping-range and co-located-severity cases whose greedy matching
   cardinality is lower than the maximum.
4. Cover the four empty/non-empty expected/findings quadrants and assert all
   invariants.
5. Update all four existing direct `score_findings()` tests to assert detection
   and qualification separately while preserving wrong-filename, accepted
   range, and upward-severity behavior.
6. Run the focused scorer tests, then the full test suite and formatting/lint
   checks.
7. Re-score the preserved Muse outbox findings with the corrected function and
   verify the expected metrics without mutating the original tournament
   artifact.

## Success criteria

- A real location match below minimum severity is never counted as a false
  positive solely because of severity.
- Severity underestimation remains visible and cannot satisfy
  `qualifiedRecall`.
- Duplicate findings do not inflate distinct defect recall.
- Maximum matching prevents rubric or report ordering from changing metric
  cardinalities.
- Version-2 output exposes both detection and legacy-equivalent qualified
  precision/recall.
- Existing clean-case and location-safety behavior remains intact.
- No unrelated dirty work enters the implementation commits.
