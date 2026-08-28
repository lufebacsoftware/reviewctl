# Tournament Scoring Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct tournament scoring so defect detection and minimum-severity qualification are measured separately without order-dependent matching or misleading false positives.

**Architecture:** Keep the existing location predicate and add one private maximum-cardinality bipartite-matching helper. `score_findings()` builds detection edges and the severity-qualified subset, computes both cardinalities independently, and returns a version-2 score with detection and legacy-equivalent qualified ratios.

**Tech Stack:** Python 3.12, standard library only, pytest, Ruff, uv.

---

## File map

- Modify `src/reviewctl/cli.py`: private matching helper and version-2 score calculation.
- Modify `tests/test_run.py`: red-green regression matrix and updated existing scorer expectations.
- Modify `docs/TOURNAMENT.md`: version-2 metric semantics, invariants, empty conventions, and historical comparability.

Do not modify transport, receipt, provider, budget, timeout, or policy code.

### Task 1: Implement version-2 scorer with TDD

**Files:**

- Modify: `tests/test_run.py:6690-6760`
- Modify: `src/reviewctl/cli.py:4618-4658`

- [ ] **Step 1: Add the failing under-severity duplicate regression**

Add this direct scorer test near the existing scoring tests:

```python
def test_score_separates_detection_from_severity_qualification() -> None:
    score = cli.score_findings(
        expected=[{"path": "outbox_lease.py", "line_start": 1, "line_end": 7, "severity": "critical"}],
        findings=[
            {"path": "outbox_lease.py", "line": 2, "severity": "high"},
            {"path": "outbox_lease.py", "line": 5, "severity": "high"},
        ],
    )

    assert score == {
        "scoreSchemaVersion": 2,
        "expected": 1,
        "matched": 1,
        "severityQualified": 0,
        "falsePositives": 1,
        "lineAccurate": 2,
        "precision": 0.5,
        "recall": 1.0,
        "qualifiedPrecision": 0.0,
        "qualifiedRecall": 0.0,
    }
```

- [ ] **Step 2: Run the regression and observe RED**

Run:

```bash
uv run pytest tests/test_run.py::test_score_separates_detection_from_severity_qualification -q
```

Expected: FAIL because the current scorer returns `matched=0`, `falsePositives=2`, and has no version-2 qualification keys.

- [ ] **Step 3: Add the remaining matching and boundary tests while still RED**

Add focused tests with exact assertions for:

1. A high duplicate followed by a critical duplicate at one critical expected location. Expected: `matched=1`, `severityQualified=1`, `falsePositives=1`, `lineAccurate=2`, `precision=0.5`, `recall=1.0`, `qualifiedPrecision=0.5`, `qualifiedRecall=1.0`.
2. Overlap requiring an augmenting path: expected entry 0 covers lines 1-2, expected entry 1 covers line 1, finding 0 is line 1, finding 1 is line 2. All severities high. Expected: both matching cardinalities are 2 with zero false positives.
3. Co-located severity reassignment: expected entries at the same line have
   minimum severities high then critical; findings are critical then high.
   Qualification edges are `[0, 1]` then `[0]`, so maximum matching must
   qualify both while greedy first-fit would qualify only one.
4. Four empty/non-empty quadrants:
   - no expected, no findings: both precision values and both recall values are `1.0`;
   - no expected, one finding: precision values `0.0`, recall values `1.0`, one false positive;
   - one expected, no findings: precision values `1.0`, recall values `0.0`;
   - one matching expected/finding: all four ratios `1.0`.
5. Every returned score satisfies:

```python
assert score["matched"] <= score["lineAccurate"] <= len(findings)
assert score["severityQualified"] <= score["matched"] <= len(expected)
assert score["matched"] + score["falsePositives"] == len(findings)
assert score["qualifiedPrecision"] <= score["precision"]
assert score["qualifiedRecall"] <= score["recall"]
```

Update the four existing direct `score_findings()` tests so they assert `scoreSchemaVersion=2`, `severityQualified`, `qualifiedPrecision`, and `qualifiedRecall`. In the mixed test, the medium finding at the adjudicated `claim_next` location is a detection but does not meet the high minimum, so expected values become `matched=2`, `severityQualified=1`, `falsePositives=1`, `lineAccurate=2`, `precision=2/3`, `recall=1.0`, `qualifiedPrecision=1/3`, and `qualifiedRecall=0.5`.

- [ ] **Step 4: Run all direct scoring tests and confirm they fail for the intended semantic gap**

Run:

```bash
uv run pytest tests/test_run.py -q -k 'scores_findings or score_requires or score_accepts or score_separates or score_duplicate or score_uses_maximum or score_empty'
```

Expected: failures report missing version-2 keys and legacy severity-gated counts; no syntax or fixture errors.

- [ ] **Step 5: Add the minimal maximum-matching helper**

Add a private helper immediately before `score_findings()`:

```python
def _maximum_matching_size(edges: list[list[int]], expected_count: int) -> int:
    expected_to_finding: list[int | None] = [None] * expected_count

    def assign(finding_index: int, visited: set[int]) -> bool:
        for expected_index in edges[finding_index]:
            if expected_index in visited:
                continue
            visited.add(expected_index)
            previous = expected_to_finding[expected_index]
            if previous is None or assign(previous, visited):
                expected_to_finding[expected_index] = finding_index
                return True
        return False

    return sum(assign(index, set()) for index in range(len(edges)))
```

This is the standard augmenting-path algorithm. It returns only a cardinality; no matching identity becomes public API.

- [ ] **Step 6: Replace severity-gated matching with two independent edge sets**

Inside `score_findings()`:

1. Build `location_edges: list[list[int]]` using the existing basename-or-symbol and line-range predicate unchanged.
2. Set `line_accurate = sum(bool(edges) for edges in location_edges)`.
3. Build `qualified_edges` by filtering each finding's location edges to expected entries whose minimum severity rank is less than or equal to the finding rank.
4. Compute:

```python
matched = _maximum_matching_size(location_edges, len(expected))
severity_qualified = _maximum_matching_size(qualified_edges, len(expected))
```

5. Return exactly:

```python
{
    "scoreSchemaVersion": 2,
    "expected": len(expected),
    "matched": matched,
    "severityQualified": severity_qualified,
    "falsePositives": len(findings) - matched,
    "lineAccurate": line_accurate,
    "precision": matched / len(findings) if findings else 1.0,
    "recall": matched / len(expected) if expected else 1.0,
    "qualifiedPrecision": severity_qualified / len(findings) if findings else 1.0,
    "qualifiedRecall": severity_qualified / len(expected) if expected else 1.0,
}
```

Do not add invalid-severity coercion; inputs are contract-validated before tournament scoring.

- [ ] **Step 7: Run focused tests and observe GREEN**

Run:

```bash
uv run pytest tests/test_run.py -q -k 'scores_findings or score_requires or score_accepts or score_separates or score_duplicate or score_uses_maximum or score_empty'
```

Expected: all selected tests pass.

- [ ] **Step 8: Run formatting and lint on changed Python files**

Run:

```bash
uv run ruff format --check src/reviewctl/cli.py tests/test_run.py
uv run ruff check src/reviewctl/cli.py tests/test_run.py
```

Expected: both commands exit 0.

- [ ] **Step 9: Commit the scorer and tests**

```bash
git add src/reviewctl/cli.py tests/test_run.py
git commit -m "fix: separate tournament detection and severity scores"
```

### Task 2: Document version-2 scoring

**Files:**

- Modify: `docs/TOURNAMENT.md:88-100`

- [ ] **Step 1: Add the metric contract**

Document:

- absent `scoreSchemaVersion` means legacy version 1; new scores use version 2;
- `matched`, `precision`, and `recall` measure maximum-cardinality location detection;
- `severityQualified`, `qualifiedPrecision`, and `qualifiedRecall` apply the rubric minimum;
- `falsePositives = len(findings) - matched` and duplicates are penalized;
- `lineAccurate` is per reported finding and can exceed `matched`;
- the five normative invariants from the design;
- empty-denominator ratios are `1.0`;
- version-1 and version-2 historical results are not directly comparable;
- receipts and old artifacts are not rewritten.

- [ ] **Step 2: Verify documentation formatting and scope**

Run:

```bash
git diff --check -- docs/TOURNAMENT.md
git diff -- docs/TOURNAMENT.md
```

Expected: no whitespace errors and no changes outside tournament-scoring documentation.

- [ ] **Step 3: Commit documentation**

```bash
git add docs/TOURNAMENT.md
git commit -m "docs: explain tournament score version 2"
```

### Task 3: Verify behavior and preserved evidence

**Files:** none expected.

- [ ] **Step 1: Run the full repository gates**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=reviewctl --cov-branch --cov-report=term-missing
```

Expected: all commands exit 0 and coverage remains 100 percent.

- [ ] **Step 2: Reproduce the preserved Muse outbox score without embedding private paths**

Run this read-only probe:

```bash
uv run python - <<'PY'
from reviewctl.cli import score_findings

score = score_findings(
    expected=[
        {
            "path": "outbox_lease.py",
            "symbol": "claim_next",
            "line_start": 1,
            "line_end": 7,
            "severity": "critical",
        }
    ],
    findings=[
        {"path": "outbox_lease.py", "line": 2, "severity": "high"},
        {"path": "outbox_lease.py", "line": 5, "severity": "high"},
    ],
)
print(score)
PY
```

The private evidence store owns the real receipt-path replay. This public plan
uses the exact adjudicated locations and severities without embedding a private
filesystem path.

Expected score:

```python
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
    "qualifiedRecall": 0.0,
}
```

- [ ] **Step 3: Verify repository scope**

```bash
git status --short
git diff HEAD~2 --check
git diff HEAD~2 --stat
```

Expected: only `src/reviewctl/cli.py`, `tests/test_run.py`, and `docs/TOURNAMENT.md` differ across the two implementation commits; the worktree is clean.
