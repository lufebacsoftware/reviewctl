# Proprietary Transport Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow policy-authorized proprietary review packets to use every built-in review transport and response contract, while allowing synthetic prompt-only rounds without weakening receipt or response validation.

**Architecture:** `run_review` remains the single authorization boundary. It requires `source_allowed = true` for proprietary packets, then dispatches the selected transport and response contract normally. Synthetic prompt-only requests preserve an empty file list in the receipt. Tests inject the existing fake transport functions so they prove authorization reaches OpenRouter and Antigravity without making network calls.

**Tech Stack:** Python 3.11+, argparse CLI, pytest, TOML policy files.

---

## File structure

- Modify: `src/reviewctl/cli.py` - remove proprietary transport/contract hard blocks and permit synthetic prompt-only requests while preserving policy and response validation.
- Modify: `tests/test_run.py` - replace obsolete rejection assertions with authorized and denied transport tests.
- Modify: `docs/TOURNAMENT.md` - state that direct OpenRouter uses the organization policy for proprietary packets.
- Modify: `docs/COUNCIL.md` - describe policy authorization rather than an external-provider hard ban.

### Task 1: Authorize proprietary packets through the common policy gate

**Files:**
- Modify: `src/reviewctl/cli.py:1759-1783`
- Test: `tests/test_run.py:4437-4517`

- [x] **Step 1: Replace obsolete rejection tests with authorization tests**

Cover the shared policy gate and the two previously blocked transports:

```python
policy.write_text('[models."gemini-3.6-flash-medium"]\\nsource_allowed = true\\n')
result = run_cli(..., "--transport", "agy", "--source-class", "proprietary", "--policy", str(policy))
assert result.returncode == 0
```

Monkeypatch `invoke_openrouter`, write a valid `PersistedResponse`, and assert its proprietary receipt has transport `openrouter` and the policy SHA-256. Add a private `product-review-json` case and a synthetic product-review case without `--file`.

- [ ] **Step 2: Run the focused tests and verify the authorized cases fail under the current hard block**

Run:

```bash
uv run pytest tests/test_run.py -k 'agy_transport or openrouter_transport' -q
```

Expected: the newly authorized proprietary tests fail with return code `3` and the synthetic-only message.

- [x] **Step 3: Remove the transport-specific and contract-specific hard blocks**

Delete this block from `run_review`:

```python
if transport in {"openrouter", "agy"}:
    print(
        "reviewctl: direct OpenRouter or native Antigravity transport is synthetic-only",
        file=sys.stderr,
    )
    return 3
```

Leave the `--policy`, `source_allowed`, and policy-digest checks unchanged. Permit every contract already listed in `RESPONSE_CONTRACTS`; require review files only for proprietary packets.

- [x] **Step 4: Run the focused tests and verify they pass**

Run:

```bash
uv run pytest tests/test_run.py -k 'agy_transport or openrouter_transport' -q
```

Expected: PASS. Denied policies return code `3`; allowed policies invoke their fake transport and record a receipt.

- [ ] **Step 5: Commit the authorization change**

```bash
git add src/reviewctl/cli.py tests/test_run.py
git commit -m "feat: honor proprietary transport policy"
```

### Task 2: Align transport documentation with the policy gate

**Files:**
- Modify: `docs/TOURNAMENT.md:18-25`
- Modify: `docs/COUNCIL.md:46-54`

- [ ] **Step 1: Describe OpenRouter authorization accurately**

Replace the claim that direct OpenRouter is synthetic-only with text that says proprietary packets require the owning organization policy to set `source_allowed = true` for the selected model and persisted transport evidence.

- [ ] **Step 2: Describe the council privacy transition accurately**

Replace the claim that external candidates remain synthetic-only with text that says the policy defaults to deny and the organization enables a model only after recording its retention/data-collection decision.

- [ ] **Step 3: Verify documentation and the full suite**

Run:

```bash
git diff --check
uv run pytest -q
```

Expected: no whitespace errors and all tests pass.

- [ ] **Step 4: Commit the documentation alignment**

```bash
git add docs/TOURNAMENT.md docs/COUNCIL.md
git commit -m "docs: clarify proprietary transport authorization"
```

## Self-review

- Spec coverage: Task 1 implements shared policy authorization for `agy` and `openrouter`; existing `llm` and `codex` behavior remains covered by the common `run_review` gate. Task 2 removes contradictory documentation.
- Placeholder scan: no deferred steps or unspecified validation remain.
- Type consistency: no public Python types or CLI arguments change; the existing `source_allowed(policy, model)` function remains the authorization contract.
