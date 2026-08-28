# Partial Review Fallback and Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Every production change follows RED-GREEN-REFACTOR and receives spec review before quality review.

**Goal:** Preserve independently valid findings from incomplete `findings-json` responses, pass only validated fragments and a typed gap manifest to bounded later attempts, and record a deterministic provenance-complete consolidation without ever manufacturing approval.

**Architecture:** `reviewctl.contracts` classifies exact JSON as complete, incomplete, or invalid and extracts only whole findings that satisfy the existing strict validator. A new pure `reviewctl.review_flow` module owns promotion eligibility, completion context, fragment provenance, and deterministic consolidation. `run_review` keeps transport and acceptance gates authoritative, persists every raw response, and adds v2 receipt fields while retaining legacy top-level meanings. Receipt v2 verification reproduces fragment identities, references, accepted-attempt invariants, and consolidation; historical v1 receipts keep digest-only verification.

**Tech Stack:** Python 3.12+, stdlib dataclasses/enum/hashlib/json/pathlib, pytest, Ruff, existing native contract and receipt code.

---

## Non-negotiable invariants

1. `acceptedAttempt` names one real, contract-complete attempt that passed every existing transport, identity, conversation, and provider gate. It never names consolidation.
2. Top-level legacy `verdict` and `findings` remain the accepted attempt's value. Additive `consolidatedReview` never silently changes their meaning.
3. Invalid JSON, duplicate keys, non-object payloads, timeouts, transport failures, model/provider mismatches, empty responses, and missing conversations promote no fragments.
4. A verdict is never a fragment. In particular, partial output can never contribute `approved`.
5. Fragment validation reuses the exact current finding validator. No regex repair, truncated-JSON salvage, field-level repair, or inferred path/severity is allowed.
6. Fallback receives the same frozen packet, validated fragments, and a machine-readable gap manifest; raw prior output is not embedded by default.
7. Absence of a repeated finding is not a dispute. Only identical fingerprints are confirmations in this contract version.
8. Routes and `maxAttempts` keep their current bounded semantics: `maxAttempts` per route. Every transition is recorded explicitly as retry or route fallback.
9. Existing receipt fields and v1 verification remain compatible. New v2 consistency checks fail closed.
10. BAML remains inspiration only; no BAML dependency, generated client, or provider-owned routing is introduced.

## File map

- Modify `src/reviewctl/contracts.py`: statuses, strict fragment extraction, coverage, completion request.
- Create `src/reviewctl/review_flow.py`: promotion, provenance, fallback context, prompt rendering, consolidation, v2 structural validation.
- Modify `src/reviewctl/backends.py`: add raw response evidence path only if needed by the generic preservation helper.
- Modify `src/reviewctl/cli.py`: preserve raw attempts, render fallback prompts, serialize v2 receipt, structural verify.
- Modify `tests/test_contracts.py`: exact complete/incomplete/invalid and fragment tests.
- Create `tests/test_review_flow.py`: pure flow, identity, ordering, consolidation, verifier tests.
- Modify `tests/test_backends.py` and `tests/test_run.py`: evidence, integration, compatibility, fallback tests.
- Modify `docs/ARCHITECTURE.md`, `docs/EVIDENCE.md`, `docs/HELP-LLM.md`: public semantics and agent error handling.

### Task 1: Freeze legacy behavior and repair version identity

**Files:** `src/reviewctl/__init__.py`, `tests/test_run.py`, `tests/test_public_distribution.py`

- [ ] Add a failing assertion that package runtime version equals `pyproject.toml`; update `__version__` from `0.3.1` to `0.3.2` only.
- [ ] Add legacy receipt fixtures for one accepted findings run and one invalid-JSON unavailable run. Assert current top-level fields, attempt result, per-route `maxAttempts`, and digest-only `verify` behavior.
- [ ] Assert two routes with `maxAttempts=2` permit at most four executions and preserve route order.
- [ ] Run focused RED/GREEN, full suite, Ruff, diff check.
- [ ] Commit: `test: freeze legacy receipt and attempt semantics`

### Task 2: Add typed partial contract evaluation

**Files:** `src/reviewctl/contracts.py`, `tests/test_contracts.py`

- [ ] Introduce exact additive public types:

```python
class EvaluationStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"

class FragmentKind(StrEnum):
    FINDING = "finding"

@dataclass(frozen=True)
class EvaluationContext:
    packet_digest: str | None = None

@dataclass(frozen=True)
class ContractFragment:
    fragment_id: str
    fingerprint: str
    kind: FragmentKind
    value: dict[str, Any]
    payload_digest: str
    scope: tuple[str, ...]

@dataclass(frozen=True)
class ContractCoverage:
    required_fields: tuple[str, ...]
    covered_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]

@dataclass(frozen=True)
class ContractCompletionRequest:
    prepared_digest: str
    packet_digest: str | None
    missing_fields: tuple[str, ...]
    invalid_fragment_indexes: tuple[int, ...]
    violations: tuple[str, ...]
```

- [ ] Add to the end of `ContractEvaluation`: `status`, `valid_fragments`, `coverage`, `completion_request`, with defaults preserving positional compatibility.
- [ ] Extend `evaluate(..., *, evidence: EvaluationContext | None = None)`.
- [ ] Extract one private validator returning `(normalized_finding, violation)` and use it for complete validation and fragment extraction.
- [ ] `fingerprint = sha256(canonical_json({contract, version, kind, value, scope}))`; `fragment_id = sha256(canonical_json({fingerprint, payloadDigest}))`. Orchestration adds attempt provenance later.
- [ ] Exact JSON/object is required before extraction. Valid siblings survive malformed siblings. Missing/extra top-level fields, invalid verdict, verdict invariant, and missing declaration may yield `INCOMPLETE` only when at least one finding is independently valid. With zero valid findings, status is `INVALID`.
- [ ] Existing successful fixtures become `COMPLETE`; existing violation codes remain stable and deterministic. `value` and `normalized_digest` remain null for incomplete/invalid.
- [ ] Tests: formatting changes preserve fingerprint but change fragment id; invalid path/severity/fields do not extract; approved never extracts; mixed siblings extract only valid indexes; completion request binds packet/prepared digests.
- [ ] Commit: `feat: evaluate typed partial review fragments`

### Task 3: Preserve every raw response and hash attempt evidence

**Files:** `src/reviewctl/cli.py`, `tests/test_backends.py`, `tests/test_run.py`

- [ ] Before evaluation, persist every non-null backend response as `attempts/NN/raw-response.txt`, even for `llm` and failed contract outcomes. Never overwrite adapter-native evidence.
- [ ] Add attempt evidence metadata `rawResponse: {path, sha256, characters}` only when a response exists. Empty response may be persisted with zero characters; missing response has null metadata.
- [ ] Ensure cleanup may delete transient SQLite but never the caller-controlled raw response.
- [ ] Sealing remains accepted-response compatibility behavior in this task; document that per-attempt sealing is deferred rather than silently claiming it.
- [ ] Tests cover invalid JSON, incomplete, model mismatch, transport failure with returned payload, and no response. Every claimed path must exist and hash-match.
- [ ] Commit: `feat: preserve raw response evidence per attempt`

### Task 4: Add pure promotion, fallback context, and consolidation

**Files:** `src/reviewctl/review_flow.py`, `tests/test_review_flow.py`

- [ ] Define frozen `PromotedFragment`, `FallbackRelationship`, `CompletionContext`, `ConsolidatedFinding`, and `ConsolidatedReview`.
- [ ] `PromotedFragment` contains `fragment_id`, `fingerprint`, normalized finding, `source_attempt`, `route_index`, `payload_digest`, and `raw_response_digest`.
- [ ] `promote_fragments(evaluation, *, gate_result, attempt, route_index, raw_response_digest)` returns fragments only for `gate_result == "contract-incomplete"`; every earlier transport/identity/conversation rejection returns empty.
- [ ] `build_completion_context` sorts by `(source_attempt, fragment_id)`, deduplicates prompt content by fingerprint, preserves all provenance, includes prepared/packet digests and gap manifest, and contains no raw payload/verdict.
- [ ] `render_completion_prompt(original_prompt, context)` appends canonical JSON between fixed markers and instructs independent confirm/replace/add. It must explicitly say absence is not dispute and no inherited approval exists.
- [ ] `consolidate(accepted_review, promoted_fragments, accepted_attempt)` groups by fingerprint, stable-sorts by path/line/severity/title/fingerprint, records every source, marks identical accepted findings as confirmed, otherwise partial-only/unconfirmed, and never lowers severity or infers disagreement.
- [ ] Permutation tests prove byte-identical canonical result. Duplicate content retains multiple provenance records. No accepted attempt yields `status="unavailable"`, never approved.
- [ ] Commit: `feat: build deterministic review fallback context`

### Task 5: Integrate bounded completion fallback

**Files:** `src/reviewctl/cli.py`, `tests/test_run.py`

- [ ] Compute one frozen packet digest from the original packet; pass it through `EvaluationContext`.
- [ ] Separate `gate_result` from legacy attempt `result`: evaluate transport/model/provider/response/conversation gates first. Only an otherwise eligible incomplete contract becomes `contract-incomplete` and may promote fragments. Legacy serialized result remains `incomplete` for compatibility.
- [ ] On each later attempt, render a completion prompt only when promoted fragments exist. Preserve original `prompt.packetSha256`; record `attemptRequestSha256` per attempt.
- [ ] Add explicit relationship for every non-first attempt: `{fromAttempt, toAttempt, kind: retry|route-fallback, reason, promotedFragmentIds}`. Same route is retry; changed route is route-fallback.
- [ ] Fix logging so `attempt_retry` is emitted for same-route retry and `route_fallback` only for actual route transition.
- [ ] Complete-but-later-rejected responses, timeout bytes, model/provider mismatch, missing conversation, invalid evaluation, and non-native contracts promote nothing.
- [ ] Acceptance still requires `EvaluationStatus.COMPLETE` plus every existing gate. `acceptedAttempt` is unchanged.
- [ ] Tests: partial first + completing second; partial first + invalid second; model mismatch with valid JSON cannot promote; raw prior response marker/injection is absent from second prompt; maximum execution count unchanged.
- [ ] Commit: `feat: complete partial reviews with bounded fallback`

### Task 6: Add receipt v2 and structural verification

**Files:** `src/reviewctl/review_flow.py`, `src/reviewctl/cli.py`, `tests/test_review_flow.py`, `tests/test_run.py`

- [ ] New receipts set `receiptSchemaVersion: 2`; old receipts without it remain v1.
- [ ] Add per evaluation: status, fragments, coverage, completion request. Serialize attempt provenance separately as `promotedFragments`.
- [ ] Add top-level `fallbackRelationships` and `consolidatedReview`. Keep legacy result/verdict/findings unchanged.
- [ ] Implement pure `validate_v2_receipt(receipt) -> tuple[str, ...]` checking:
  - contiguous unique attempt numbers (add explicit `number` field);
  - acceptedAttempt references an accepted, complete attempt;
  - unavailable has null acceptedAttempt;
  - every relationship points backward-to-forward and valid attempts;
  - every promoted fragment fingerprint/id/digests reproduce and source attempt exists;
  - promoted IDs in relationships exist;
  - consolidation reproduces exactly from accepted review plus promoted fragments;
  - raw-response metadata has valid shape and digest syntax (filesystem existence is not required for portable offline verify);
  - top-level receipt digest remains valid.
- [ ] `verify` uses digest-only for v1 and digest+structure for v2, returning JSON `violations` on failure without traceback.
- [ ] Mutation tests independently corrupt every relationship/id/source/consolidation/accepted invariant. Historical v1 fixtures still verify.
- [ ] Commit: `feat: verify structured partial review receipts`

### Task 7: Document agent semantics and compatibility

**Files:** `docs/ARCHITECTURE.md`, `docs/EVIDENCE.md`, `docs/HELP-LLM.md`, `tests/test_public_distribution.py`, `tests/test_pilot_plan.py`

- [ ] Document complete/incomplete/invalid, promotion gate, no partial approval, raw evidence, fallback context, consolidation, and v1/v2 verification.
- [ ] Add machine-readable help next actions: incomplete with fragments -> inspect completion request/relationships; invalid -> inspect violations/raw evidence; accepted -> inspect both accepted and consolidated views.
- [ ] State explicitly that current contract cannot encode dispute by omission and that all adapters remain unqualified.
- [ ] No model roster, prices, credentials, provider commands, BAML dependency, Cursor/Claude support claim, federation, Potzal dependency, or editable execution.
- [ ] Commit: `docs: define partial review and consolidation semantics`

### Task 8: Final verification and external review

- [ ] Run `uv run pytest`, `uv run ruff check .`, `git diff --check`, and confirm clean worktree.
- [ ] Build at most three review files: contracts+flow, focused run/verifier excerpt, focused tests.
- [ ] Run formal proprietary review through the explicit worktree checkout and approved policy; persist and verify receipt.
- [ ] Reproduce every material finding, add RED regression before fixes, rerun all gates, and repeat formal review against final commit.
- [ ] Report exact commit, test count, receipt, adjudication, compatibility, and deferred gaps.

## Explicit deferrals

- New Cursor/Claude/ACP backends and backend qualification.
- Dimension-aware coverage beyond fields expressible by `findings-json` v1.
- Explicit dispute/replacement relationships requiring a richer contract.
- Per-attempt encrypted sealing, journal/federation, Potzal carriage, and editable `ChangeAttempt`.
- Using consolidated output as a merge approval independent of a complete accepted attempt.
