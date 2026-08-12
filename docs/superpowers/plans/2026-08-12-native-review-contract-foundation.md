# Native Review Contract Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish reviewctl's honest evidence vocabulary and replace the scattered `findings-json` implementation with one native typed contract while preserving current CLI behavior and receipt verification.

**Architecture:** A focused `reviewctl.contracts` module owns contract preparation, canonical identity, exact JSON decoding, semantic validation, and normalized output. The CLI remains the orchestration and acceptance boundary: transports consume a prepared contract, and an attempt is accepted only after transport checks and contract evaluation both succeed. Existing public CLI helpers remain as compatibility wrappers during this slice.

**Tech Stack:** Python 3.12+, standard library dataclasses/hashlib/json, pytest, Ruff, existing canonical JSON receipt format.

---

## File map

- Create `src/reviewctl/contracts.py`: native contract value objects, registry, `findings-json` preparation and evaluation.
- Create `tests/test_contracts.py`: direct behavioral tests for preparation, identity, exact decoding, normalization, and semantic violations.
- Modify `src/reviewctl/cli.py`: compatibility exports/wrappers, transport use of prepared schemas/instructions, and receipt evidence from contract evaluation.
- Modify `tests/test_run.py`: integration coverage for prepared transport contracts and receipt fields.
- Create `docs/ARCHITECTURE.md`: ownership planes, canonical terms, identity, observability, retention, and trust boundaries.
- Create `docs/adr/0001-append-only-journals.md`: normative journal/projection decision.
- Create `docs/adr/0002-signed-federation-bundles.md`: normative federation decision.
- Modify `docs/EVIDENCE.md`: distinguish snapshot integrity, model declaration, invocation observability, and reasoning limits.
- Modify `README.md`: link the architecture and evidence vocabulary without duplicating organization-owned rosters.

### Task 1: Normative evidence foundation

**Files:**
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/adr/0001-append-only-journals.md`
- Create: `docs/adr/0002-signed-federation-bundles.md`
- Modify: `docs/EVIDENCE.md`
- Modify: `README.md`

- [ ] **Step 1: Add the architecture vocabulary and two ADRs**

Document the five ownership planes and canonical terms from the approved system plan. State that journals are append-only, projections are disposable, project identity is organization-declared rather than path-derived, and federation transfers signed sanitized facts rather than database access.

- [ ] **Step 2: Correct the evidence claims**

Replace the claim that a model returns file SHA-256 values with these explicit levels:

```text
snapshotIntegrity: reviewctl hashes the exact frozen bytes locally
reviewDeclaration: a required model response declares frozen basenames and reviewctl compares the set
invocationManifest: reviewctl records what it asked a transport to invoke
providerRequestObserved: only true when reviewctl directly observes the provider-native request
```

State that none of these proves cognition or correct reasoning.

- [ ] **Step 3: Check terminology and commit**

Run:

```bash
rg -n "model.*SHA-256|proof.*correctly reading|write-only database" README.md docs
uv run pytest tests/test_public_distribution.py -q
git add README.md docs
git commit -m "docs: define review evidence architecture"
```

Expected: no misleading hash/read-proof claims, focused tests pass, one documentation commit.

### Task 2: Prepare a native findings contract

**Files:**
- Create: `tests/test_contracts.py`
- Create: `src/reviewctl/contracts.py`

- [ ] **Step 1: Write failing preparation tests**

Add tests that call `get_contract("findings-json").prepare(...)` and assert:

```python
assert prepared.name == "findings-json"
assert prepared.version == "1"
assert prepared.schema["required"] == ["verdict", "findings"]
assert prepared.digest == sha256(canonical_json(prepared.identity_material)).hexdigest()
assert "changes-requested" in prepared.output_instructions
```

Add a second test with `review_declaration_required=True` asserting that `reviewedFiles` is required without mutating the portable schema.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_contracts.py -q`.

Expected: collection fails because `reviewctl.contracts` does not exist.

- [ ] **Step 3: Implement the minimum contract preparation API**

Create frozen `ContractContext` and `PreparedContract` dataclasses, immutable-by-convention schema construction, canonical JSON hashing local to the module, `FindingsJsonContract.prepare`, and a registry-backed `get_contract`. Keep the module dependency-free and reject unknown contract names with `KeyError`.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_contracts.py -q
uv run ruff check src/reviewctl/contracts.py tests/test_contracts.py
git add src/reviewctl/contracts.py tests/test_contracts.py
git commit -m "feat: prepare native findings contract"
```

Expected: preparation tests and Ruff pass.

### Task 3: Evaluate exact findings responses

**Files:**
- Modify: `tests/test_contracts.py`
- Modify: `src/reviewctl/contracts.py`

- [ ] **Step 1: Write failing evaluation tests**

Cover exact JSON only, top-level exact fields, exact six finding fields, known severity, positive non-boolean line, non-blank strings, packet path membership, exact reviewed-file declaration, and the verdict/findings invariant. Assert that a successful `ContractEvaluation` exposes normalized value, payload SHA-256, normalized SHA-256, and no violations; rejected evaluations expose stable violation codes and no normalized value.

- [ ] **Step 2: Verify RED**

Run `uv run pytest tests/test_contracts.py -q`.

Expected: failures because `evaluate` and `ContractEvaluation` are absent.

- [ ] **Step 3: Implement exact decode and semantic validation**

Use `json.loads` without fence stripping or repair. Return violation codes such as `invalid-json`, `top-level-not-object`, `response-fields`, `review-declaration`, `verdict`, `findings-shape`, `finding-fields`, `finding-value`, `finding-path`, and `verdict-invariant`. Canonicalize only validated normalized values before hashing.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_contracts.py -q
uv run ruff check src/reviewctl/contracts.py tests/test_contracts.py
git add src/reviewctl/contracts.py tests/test_contracts.py
git commit -m "feat: evaluate exact findings responses"
```

Expected: all direct contract tests pass.

### Task 4: Integrate prepared contracts with CLI transports

**Files:**
- Modify: `tests/test_run.py`
- Modify: `src/reviewctl/cli.py`

- [ ] **Step 1: Write failing compatibility and transport tests**

Assert that `cli.response_schema("findings-json")` equals the portable prepared schema, Codex proprietary mode receives the declaration schema, and OpenRouter/Codex output instructions come from the same prepared contract semantics. Keep existing `cli.FINDINGS_SCHEMA`, `validate_review_response`, and `review_validation_error` behavior stable.

- [ ] **Step 2: Verify RED**

Run the new focused test node IDs with `uv run pytest ... -q` and confirm they fail because the CLI still owns the implementation.

- [ ] **Step 3: Add compatibility wrappers and transport preparation**

Import/re-export the findings constants from `reviewctl.contracts`. Make `response_schema` delegate to `prepare`; make `validate_review_response` and the findings branch of `review_validation_error` delegate to `evaluate`; and have OpenRouter/Codex use prepared output instructions. Leave document and product contracts unchanged in this slice.

- [ ] **Step 4: Verify focused and full compatibility, then commit**

Run:

```bash
uv run pytest tests/test_contracts.py tests/test_run.py -q
uv run ruff check .
git add src/reviewctl/cli.py tests/test_run.py
git commit -m "refactor: route findings through native contract"
```

Expected: all focused tests and Ruff pass with no public CLI regression.

### Task 5: Bind contract evaluation evidence into receipts

**Files:**
- Modify: `tests/test_run.py`
- Modify: `src/reviewctl/cli.py`

- [ ] **Step 1: Write failing receipt tests**

For accepted and incomplete `findings-json` attempts, assert the attempt records:

```python
{
    "name": "findings-json",
    "version": "1",
    "preparedSha256": "<64 lowercase hex>",
    "payloadSha256": "<64 lowercase hex>",
    "normalizedSha256": "<64 lowercase hex or null>",
    "violations": [],
}
```

Also assert the receipt retains the legacy `reviewContract` string, records stable contract
name/version at the top level, keeps the context-specific prepared digest on each attempt, and passes
`reviewctl verify`. A routed run may prepare more than one dialect, so no single prepared digest is
claimed at receipt level.

- [ ] **Step 2: Verify RED**

Run the new receipt tests. Expected: failure because attempts do not yet include `contractEvaluation`.

- [ ] **Step 3: Record evaluation evidence without moving the acceptance gate**

Resolve the contract once per run, prepare its context-specific dialect once per attempt, evaluate each
raw response once, derive legacy review/error wrappers from that evaluation, and add
`contractEvaluation` to structured attempts. Keep transport/model/provider/conversation/result checks
in `run_review`; contract validity alone must not imply acceptance.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_contracts.py tests/test_run.py -q
uv run pytest
uv run ruff check .
git add src/reviewctl/cli.py tests/test_run.py
git commit -m "feat: bind contract evaluation to receipts"
```

Expected: full suite and Ruff pass; existing receipts remain verifiable because verification continues to hash the canonical unsigned receipt.

### Task 6: Final verification and formal review

**Files:**
- Review: `src/reviewctl/contracts.py`
- Review: focused integration section from `src/reviewctl/cli.py`
- Review: `tests/test_contracts.py`

- [ ] **Step 1: Verify requirements and repository state**

Run:

```bash
uv run pytest
uv run ruff check .
git status --short
git log --oneline b244694..HEAD
```

Expected: zero test/lint failures and only intentionally uncommitted review artifacts, if any.

- [ ] **Step 2: Run the merge-gate review**

Freeze no more than three uniquely named files and run the workspace checkout explicitly:

```bash
uv run --project /Users/luisfernando/Code/workspaces/reviewctl reviewctl run \
  --review-id reviewctl-native-contract-foundation \
  --transport codex \
  --model gpt-5.6-sol \
  --source-class proprietary \
  --response-contract findings-json \
  --policy /Users/luisfernando/Code/workspaces/reviewctl-evidence/policies/openbancor.toml \
  --prompt-file <bounded-request.md> \
  --file src/reviewctl/contracts.py \
  --file tests/test_contracts.py \
  --file <focused-cli-review-file.py>
```

- [ ] **Step 3: Verify and independently adjudicate**

Run `uv run --project /Users/luisfernando/Code/workspaces/reviewctl reviewctl verify <receipt.json>`. Independently reproduce every material finding against source/tests. If a finding is valid, add a failing regression test before changing code, rerun the suite, and repeat the formal review against the new commit.

- [ ] **Step 4: Record final evidence**

Report the branch, exact commit SHA, full test count, lint result, receipt path, receipt verification result, and any roadmap items intentionally left for later phases.
