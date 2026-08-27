# Reviewctl Main Regularization Design

## Goal

Make the unpublished local `main` line reviewable and publishable without weakening the
repository's existing release gates, disturbing the user's dirty primary checkout, or mixing
the tournament scorer v2 change into the baseline repair. Standardize the unpublished line on
Python 3.14 before restoring the coverage gate.

## Recovered State

- The primary checkout is `main` at `36d90ba8e83dbec27c25e2935160626e2bcb0d8e` and has
  unrelated edits in `README.md`, `docs/HELP-LLM.md`, `src/reviewctl/cli.py`, and
  `tests/test_run.py`.
- `origin/main` is `b244694a8f1dd140ed1a3cb0f8b5b89e47933faf`; local `main` is 183 commits
  ahead and zero commits behind.
- The regularization branch starts from the exact local `main` head in an isolated worktree.
- The scorer v2 branch remains separate at `18c04189d5b1fb1dfcbe4dac56e344dd22b6c9a4`.

## Baseline Evidence

The commands used by GitHub CI were reproduced locally with Python 3.12.13:

- `uv run --python 3.12 ruff check .` passes.
- `uv run --python 3.12 ruff format --check .` reports 12 files requiring formatting.
- `uv run --python 3.12 pytest --cov=reviewctl --cov-branch --cov-report=term-missing`
  runs 1,379 passing tests, then fails the coverage gate at 89.43%: 499 statements are missing
  and 262 branches are partial.
- `uv build` passes.

The failure is real coverage debt, not a Python-version artifact. The public baseline already
declared `fail_under = 100`; the unpublished line added substantial source and tests while
remaining outside remote CI.

The same 1,379 tests and build also pass on Python 3.14.6, with the same 89.43% coverage result.
Python 3.14 is a stable bugfix release line and Ruff supports `py314`, so the migration does not
depend on a preview runtime or syntax target.

## Python 3.14 Baseline

Regularization raises the package minimum to Python 3.14 instead of carrying a compatibility
matrix through the unpublished release:

- set `requires-python = ">=3.14"`;
- set Ruff's `target-version = "py314"`;
- add a repository `.python-version` containing `3.14` for uv and local tooling;
- run CI and release jobs on Python 3.14;
- regenerate `uv.lock` so its declared Python requirement matches the package;
- run the 100% coverage gate on Python 3.14.

Historical plans keep their original version references because they describe the environment
in which those changes were designed. User-facing compatibility documentation must describe
3.14 as the current minimum.

## Considered Approaches

### Restore the declared gates (selected)

Format the 12 files mechanically, add focused tests for uncovered behavior, and retain
`fail_under = 100`. This requires the most work but preserves the public contract and produces
a truthful green baseline.

### Lower the global threshold and ratchet later

Setting the threshold near 89% would make CI green quickly, but it would silently weaken an
existing release control precisely while publishing a large private delta. This requires a
separate policy decision and is out of scope.

### Publish or backport only selected features

Splitting 183 historical commits into a new release topology may eventually improve review
size, but it changes the requested ownership/history problem and risks omitting coupled
security and evidence changes. It is not the baseline-repair strategy.

## Change Boundaries

1. Migrate package metadata, Ruff, uv, CI, release, and current compatibility documentation to
   Python 3.14 in one bounded infrastructure commit. Verify the pre-existing test behavior on
   3.14 before relying on the new default.
2. Apply Ruff's formatter only to the 12 files reported by the exact CI command. Keep this as a
   mechanical commit with no semantic edits.
3. Restore coverage module by module using focused tests. Prefer owner-native public behavior
   and existing test seams; test private branches only when they encode observable security or
   evidence invariants.
4. Do not add broad exclusions, `pragma: no cover`, omit rules, or threshold reductions merely
   to satisfy the percentage. Any genuinely unreachable platform guard must be justified
   separately before exclusion.
5. Do not refactor production code unless a test exposes a concrete defect or an untestable
   boundary. Such a change must follow red-green-refactor and remain separate from coverage-only
   commits.
6. Do not merge the scorer v2 commits during regularization. After the baseline is green, rebase
   or replay that bounded branch and repeat its tests and E2E.
7. Do not modify, stash, reset, or include the dirty files from the primary checkout. Do not push
   or open a PR without an explicit publication authorization.

## Work Decomposition

Coverage work can proceed in non-overlapping test files grouped by owner surface:

- API and project front door: `api.py`, `project_cli.py`, `config.py`, `dimensions.py`, and
  `artifacts.py`.
- GitHub and journal boundaries: `github.py`, `github_publisher.py`, `identity.py`, and
  `journal.py`.
- Transport and contract flow: `pi_transport.py`, `contracts.py`, and `review_flow.py`.
- CLI orchestration: `cli.py`.

Each group must first capture its own coverage report, add the smallest missing tests, and run
its focused suite before integration. Shared fixtures should be avoided unless duplication
would make the tests misleading.

## Verification and Acceptance

The regularization branch is locally ready only when all commands pass freshly on Python 3.14:

```bash
uv sync --locked --python 3.14
uv run --python 3.14 ruff check .
uv run --python 3.14 ruff format --check .
uv run --python 3.14 pytest --cov=reviewctl --cov-branch --cov-report=term-missing
uv build
git diff --check origin/main...HEAD
```

Acceptance also requires:

- exactly 100% statement and branch coverage under the retained policy;
- package metadata, lock metadata, Ruff, local uv selection, CI, and release agree on Python 3.14;
- no changes to the primary checkout's four dirty files;
- a reviewable commit structure separating formatting, coverage groups, and any proven defects;
- a recorded exact head SHA and diff inventory;
- no claim of remote readiness until that exact head receives green GitHub CI and substantive
  review.

## Publication Boundary

Regularization prepares a candidate branch; it does not publish it. The eventual GitHub step
must expose the full delta from `origin/main` for review, run CI on the exact candidate head, and
stop short of merge until the repository's exact-head review gate is satisfied. The scorer v2
change follows only after this baseline has an authoritative upstream base.
