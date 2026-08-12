# Global Best-Match Review Design

**Status:** Proposed

## Purpose

Make `reviewctl` the global review mechanism used by coding agents on the local
workstation and Amélia without turning temporary model or transport failures
into a universal development blockade.

The system selects the best applicable review lane, makes a bounded best-effort
attempt, retains useful fragments from incomplete responses, invokes qualified
fallback reviewers when necessary, and presents one consolidated review without
erasing the original evidence.

This design adopts concepts associated with typed LLM contracts, but it does not
adopt BAML or another contract runtime.

## Current State

`reviewctl` is installed locally and on Amélia as a `uv` tool. On Amélia the
executable exists below the user's `uv` tool directory, but a normal SSH shell
does not include the corresponding user-local binary directory in `PATH`.

Coding-agent tasks and several project instruction files already use or mention
`reviewctl`. Adoption is inconsistent:

- some projects point to the reusable project-integration guide;
- some still permit older review transports;
- the global Amélia Codex instructions do not explain discovery or recovery;
- agents do not necessarily know to call `reviewctl help-llm --format json`;
- the current contract evaluation is primarily complete-or-invalid and cannot
  promote valid fragments from an incomplete response into a fallback round.

The rollout must therefore stabilize discovery, contracts, errors, and evidence
semantics before making the global instruction enforceable.

## Design Principles

### Best match

Select the most appropriate authorized lane from the change dimensions, source
classification, project rules, policy, reviewer capabilities, and current
qualification evidence. Model rosters and provider measurements remain in the
private evidence store, not in project instructions or this repository.

### Best effort

Attempt the best match first. If it is unavailable or incomplete, continue
through bounded authorized fallbacks. Exhaustion produces an honest unavailable
result, not an approval and not necessarily a universal block.

### Fail honest

Receipt integrity, accepted review, consolidated coverage, and human merge or
deployment authorization are separate facts. No unavailable, incomplete, or
unverified result may be presented as approval.

### Preserve and reuse evidence

An incomplete response may contain useful findings or dimension coverage. The
system retains the raw attempt and extracts only contract-valid fragments for
reuse. It never edits the original response or silently fills its fields.

### Consolidate without erasing provenance

Users and agents receive one effective review view. Every consolidated finding,
coverage claim, confirmation, and disagreement identifies its source attempts.
Original attempts remain append-only evidence.

## Review Classification

A project instruction file owns only project-specific information:

- domain invariants and canonical standards;
- source-data and privacy boundaries;
- verification commands;
- dimensions that trigger review;
- dimensions that require explicit human authorization when assurance degrades.

Global instructions own the common behavior:

- use `reviewctl` for material review;
- discover the tool and inspect machine-readable help;
- select best match through policy rather than a copied roster;
- require persisted evidence;
- follow fallbacks for unavailable or incomplete attempts;
- distinguish accepted, degraded, unavailable, and waived outcomes.

## Agent Compatibility

The review semantics are agent-neutral. Cursor, Codex, Pi, and Claude consume
the same behavioral contract through their native instruction-discovery
mechanisms; they do not receive different definitions of acceptance, fallback,
or waiver.

### Canonical instruction contract

`reviewctl` owns one concise canonical instruction fragment containing:

- material-review triggers and best-effort behavior;
- the command for machine-readable discovery;
- accepted, degraded, unavailable, and waiver semantics;
- the requirement to persist and verify evidence;
- the prohibition on treating model output as self-validating approval;
- the pointer to project-specific dimensions and verification commands.

Agent adapters may change file syntax, imports, or installation paths, but not
those semantics. Generated fragments include a contract version and digest so
drift can be detected.

The canonical fragment is a reviewctl-owned artifact, not whichever generated
agent file happened to be installed first. A project keeps its domain-specific
instructions in `AGENTS.md`; it references or embeds the versioned common
fragment without becoming the owner of global review semantics.

### Compatibility matrix

| Agent | Global instructions | Project instructions | Adapter rule |
| --- | --- | --- | --- |
| Codex | `~/.codex/AGENTS.md` or the active `CODEX_HOME` equivalent | `AGENTS.md` / `AGENTS.override.md` hierarchy | Use the canonical Markdown fragment directly; preserve Codex's root-to-cwd precedence. |
| Cursor | Cursor User Rules | Root `AGENTS.md` for portable rules; `.cursor/rules/*.mdc` only for Cursor-specific scoping | Keep common review semantics in `AGENTS.md`; generate an MDC wrapper only when globs or Cursor-only attachment behavior are required. |
| Pi | `~/.pi/agent/AGENTS.md` | `AGENTS.md` or `CLAUDE.md` discovered from parent directories and cwd | Use the canonical Markdown fragment; an optional Pi extension may add UX but is not required for correctness. |
| Claude Code | `~/.claude/CLAUDE.md` or managed organization instructions | `CLAUDE.md`, preferably importing `@AGENTS.md`, or a symlink when no Claude-only additions are needed | Keep `AGENTS.md` canonical at project scope and make `CLAUDE.md` a short import or pointer, not a divergent copy. |

Codex officially composes global and project `AGENTS.md` guidance in precedence
order. Cursor supports project rules and root `AGENTS.md`, with User Rules for
global behavior. Pi loads global and project `AGENTS.md`/`CLAUDE.md` context.
Claude Code reads `CLAUDE.md`, not `AGENTS.md` directly, and officially
recommends importing or linking `AGENTS.md` when sharing instructions. These
assumptions are based on the current primary documentation:

- [Codex custom instructions](https://developers.openai.com/codex/guides/agents-md)
- [Cursor rules](https://docs.cursor.com/context/rules-for-ai)
- [Pi coding-agent context files](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md#context-files)
- [Claude Code memory and AGENTS.md compatibility](https://code.claude.com/docs/en/memory#agentsmd)

### Instruction rendering and drift checks

Add a read-only instruction interface:

```text
reviewctl instructions render --agent codex|cursor|pi|claude \
  --scope global|project --format markdown|json
reviewctl instructions check --agent codex|cursor|pi|claude \
  --scope global|project
```

`render` prints the adapter fragment but does not overwrite user instruction
files. Installation remains an explicit, reviewable operation during rollout.
`check` reports the expected location, whether the canonical contract digest is
present, conflicting or stale review rules, and the command needed to repair
the integration. It never reports private model rosters or credentials.

Its JSON result is stable and typed:

```text
AgentIntegrationStatus
├── agent
├── scope
├── expectedLocations[]
├── discoveredLocations[]
├── contractVersion
├── expectedDigest
├── discoveredDigests[]
├── status: current | missing | stale | conflicting | unsupported
├── conflicts[]
└── remediation[]
```

`remediation` is declarative: it identifies the content and destination but
does not silently modify user or managed organization instructions.

At project scope, `check` also reports whether `AGENTS.md` and `CLAUDE.md`
diverge. A Claude import of `@AGENTS.md` or a valid symlink is compatible. A
second independently maintained review policy is drift.

### Invocation compatibility

All four agents invoke the same `reviewctl` executable and consume the same JSON
schemas. Agent-specific adapters do not call providers directly. When shell
execution is available, they use the CLI. A future MCP or native tool adapter
may wrap the CLI, but it must preserve the request, result, receipt, fragment,
and consolidation types exactly.

Instructions are behavioral guidance, not a technical sandbox. During the
advisory rollout no agent-specific hook may turn a best-effort unavailable
result into a hard global block. Later enforcement hooks are allowed only for
the critical dimensions and authorization rules defined by policy.

Initial globally recognized dimensions are:

- correctness;
- architecture;
- security;
- privacy;
- financial or accounting behavior;
- fiscal or regulatory behavior;
- release and deployment risk;
- public API compatibility;
- user-interface interaction and accessibility.

Projects may add dimensions but must not redefine the common result semantics.

## Review Plan

Best-match selection produces a typed plan before any model is invoked:

```text
ReviewPlan
├── packetIdentity
├── sourceClass
├── requiredDimensions[]
├── routes[]
├── fallbackLimit
├── independenceRequirement
└── criticalAuthorizationRequirement
```

The organization's private policy and qualification evidence produce the
ordered routes. The coding agent supplies the bounded change, project triggers,
and source classification; it does not invent a model roster. The plan and its
policy digest are persisted so selection can be audited independently from the
responses.

## Contract Evaluation

Contract evaluation becomes a structured partial-evaluation interface:

```text
ContractEvaluation
├── status: complete | incomplete | invalid
├── completeValue
├── validFragments[]
├── violations[]
├── coverage
└── completionRequest
```

### Complete

The response satisfies the whole prepared contract. It can become an accepted
attempt after transport, model, provider, conversation, and policy gates pass.

### Incomplete

The response fails the whole contract but contains one or more independently
valid typed fragments. Examples include a valid finding with a missing review
declaration, or valid architecture coverage with the required security
dimension absent.

An incomplete attempt is never accepted. Its valid fragments may participate in
fallback context and consolidation.

### Invalid

No fragment can be safely promoted. Raw evidence and violations remain
diagnosable, but no content from the response is supplied as trusted fallback
context.

### Valid fragments

Each fragment records:

- a stable fragment identifier;
- fragment type and normalized value;
- dimensions covered;
- source attempt and response digest;
- validation status and applicable violations;
- finding path or source scope when relevant.

Fragment validation uses the same strict JSON, path, severity, and scope rules
as complete contract validation. A syntactically parseable value is not
automatically a valid fragment.

### Completion request

The evaluator produces a machine-readable gap manifest containing:

- missing required fields;
- uncovered required dimensions;
- invalid fragments that require replacement;
- disagreements already visible among prior attempts;
- the same prepared contract identity and frozen packet identity.

It contains no inferred approval and no credentials or redacted secret values.

## Fallback Execution

Fallback is an ordered, bounded attempt chain.

```text
best-match reviewer
  ├── complete and accepted ──> consolidate
  ├── incomplete ─────────────> fragment extraction ──> fallback
  └── unavailable/invalid ────> fallback
```

The fallback reviewer receives:

- the exact same frozen packet and commit or diff identity;
- the same prepared response contract;
- valid fragments from earlier attempts;
- the machine-readable completion request;
- an instruction to independently verify, confirm, dispute, or replace inherited
  fragments and complete missing dimensions.

The fallback does not receive an earlier raw response by default. This reduces
anchoring and prompt-injection propagation while preserving validated work.
Explicit synthesis workflows may attach raw prior responses as bounded evidence.

Every fallback attempt records:

- the attempt it follows;
- the reason for fallback;
- whether it replaces an invalid result or complements an incomplete one;
- which fragments it confirmed, disputed, replaced, or added;
- its own provider, model, policy, request, response, and receipt evidence.

Fallback selection prefers an independently qualified provider or account. If
policy permits only a same-provider fallback, the chain records reduced
independence as degraded assurance.

## Consolidation

Consolidation is a derived, reproducible view over immutable attempts.

```text
ConsolidatedReview
├── effectiveVerdict
├── assurance
├── coverageByDimension
├── findings[]
├── disagreements[]
├── unresolvedGaps[]
└── sourceAttempts[]
```

### Deterministic consolidation

The local consolidator performs operations that do not require model judgment:

- union findings with exact provenance;
- group findings by a stable normalized fingerprint;
- preserve all severities and source paths;
- mark confirmations and disputes;
- calculate dimension coverage;
- expose unresolved gaps and disagreements.

It does not silently choose between contradictory findings or downgrade a
severity.

### Model-assisted consolidation

If semantic deduplication or adjudication requires model judgment, the
consolidator is another formal reviewer. It receives a bounded package, produces
its own typed response, and appears as another attempt with its own receipt.

### Effective verdict

- `approved`: at least one complete accepted attempt exists, required coverage
  is complete, no unresolved material finding exists, and the receipt verifies;
- `changes-requested`: a material finding remains active in any valid fragment
  or complete attempt;
- `needs-adjudication`: valid attempts materially disagree;
- `unavailable`: bounded routes were exhausted without complete coverage.

`assurance` is independently `standard` or `degraded`. A review completed
through an allowed lower-assurance fallback can therefore be
`effectiveVerdict: approved` with `assurance: degraded`; human output must show
both. It never silently appears as an ordinary approval.

## Project Journals and Federation

Attempts, fragment evaluations, fallback relationships, consolidation, and
human authorization are appended as new records in the owning project's review
journal. Consolidation is a projection over those records, not a mutable row
that replaces prior evidence.

Each project review store indexes at least:

- project and repository identity;
- immutable commit or diff identity;
- review dimensions;
- contract and policy digests;
- reviewer and route identity;
- attempt, fragment, consolidation, and authorization relationships;
- effective verdict and assurance;
- timestamps and evidence digests.

Projects may export signed federation bundles containing the permitted records
and aggregates. Importers verify signatures and append imported facts under the
origin project's namespace. Global views aggregate by dimensions and digests;
they do not rewrite an origin journal or require a central mutable database.

Private source, credentials, raw responses, model rosters, and organization-only
qualification evidence remain subject to export policy and are not implied by a
federation bundle.

## Waivers and Critical Changes

A waiver is a substitution record, not an absence of review.

The ordinary waiver path requires:

1. a recorded unavailable or incomplete best-match attempt;
2. an accepted and verified fallback review over the same frozen packet;
3. a reason for substitution and the assurance difference;
4. consolidated coverage and unresolved gaps.

For security, financial, fiscal, privacy, and production-release dimensions, a
degraded result additionally requires explicit human authorization before merge
or deployment. The authorization references the consolidated review and is
stored outside the immutable model response.

An emergency no-review waiver is outside the normal automatic path. It requires
an explicit project or organizational rule and human authorization, and its
result remains unavailable rather than approved.

## CLI and Machine Interface

The first implementation should deepen existing commands rather than add a
parallel review client.

### `reviewctl run`

`run` executes the ordered best-match/fallback chain and writes one receipt
containing all attempts, fragment evaluations, fallback relationships, and the
consolidated review.

Default human output is concise:

```text
changes-requested (degraded)
2 reviewers, 1 fallback, 7/7 dimensions covered
3 active findings, 1 disagreement
receipt: <path>
```

The full JSON receipt remains authoritative. A compatibility period preserves
existing top-level `result`, `acceptedAttempt`, and attempt fields.

### `reviewctl verify`

Verification checks hashes and structural consistency for attempts,
relationships, fragments, and consolidation. It still proves integrity rather
than independently proving that findings are true or that deployment is
authorized.

### `reviewctl help-llm`

The JSON output becomes the stable agent-discovery contract and includes:

- installed version and schema version;
- command and result types;
- exit-code semantics;
- best-match and fallback behavior;
- partial-evaluation and consolidation types;
- recovery steps based only on evidence locators that exist;
- global instruction snippets, compatibility status, and project-integration
  pointers.

Schema changes are additive during stabilization. Breaking changes require a
new schema version and compatibility tests.

### Discovery and diagnostics

Add `reviewctl doctor --format human|json` as a read-only diagnostic surface
that reports without exposing secrets:

- executable and version;
- config and policy discoverability;
- available transports and required binaries;
- evidence-root writability;
- whether the user-local binary directory is in `PATH`;
- detected Cursor, Codex, Pi, and Claude instruction locations and contract
  digests;
- machine-readable remediation for local and Amélia installations.

`help-llm` points agents to `doctor --format json` when discovery or environment
checks fail.

## Global Instruction Rollout

### Phase 0: Baseline

Inventory current local and Amélia installations, paths, versions, global
instructions for all four supported agents, project pointers, and representative
tasks. Record baseline CLI and receipt fixtures.

### Phase 1: Stabilize

Implement additive result types, partial evaluation, fallback relationships,
consolidation, diagnostics, and compatibility tests. Do not change global agent
requirements yet.

### Phase 2: Document

Update:

- `HELP-LLM.md` and generated machine help;
- installation and Amélia discovery guidance;
- Cursor, Codex, Pi, and Claude compatibility and instruction-rendering
  guidance;
- the reusable project-integration template;
- migration guidance from direct `llm-review` or copied model rosters.

### Phase 3: Advisory global instruction

Install the same short global instruction locally and on Amélia:

- material reviews use `reviewctl`;
- agents query machine help when invocation or recovery is uncertain;
- agents attempt best match and bounded fallbacks;
- degraded or unavailable outcomes are reported honestly;
- critical degraded outcomes require human authorization.

Render the instruction through each agent adapter and verify that the agent
loads the expected contract digest. Do not hand-maintain four semantic copies.

During this phase, failures are collected as stabilization evidence rather than
treated as universal blockers.

### Phase 4: Enforced global instruction

After representative local and Amélia tasks pass, require a persisted
consolidated result or an explicitly authorized waiver for material changes.
Project-specific hard gates remain stricter where defined.

## Compatibility

- Existing single-attempt receipts continue to verify.
- Existing ordered routes retain their order and bounded-attempt semantics.
- Existing consumers may ignore additive fragment and consolidation fields.
- `acceptedAttempt` continues to identify a complete accepted attempt, never a
  synthetic merge of incomplete attempts.
- Consolidated findings may include valid fragments from earlier incomplete
  attempts, each with provenance.
- Project instructions continue to reference the reusable integration guide and
  do not embed current model rosters or provider prices.

## Error Handling for Agents

Every expected failure must have:

- a stable result or violation code;
- a concise human diagnostic;
- a machine-readable next action;
- evidence locators only for artifacts that exist;
- documentation in generated `help-llm` JSON;
- no traceback for ordinary invocation, filesystem, policy, or transport
  failures.

Unknown internal failures may retain a traceback for developers, but the CLI
must persist the bounded attempt state first when safe to do so.

## Security and Privacy

- Fallbacks must satisfy the same source-classification and provider policy as
  the original attempt.
- A fallback never broadens the authorized source boundary implicitly.
- Only validated fragments enter inherited fallback context.
- Raw prior responses are excluded by default.
- Diagnostics remain bounded and redacted.
- Credentials, current rosters, prices, and private qualification evidence do
  not enter project instructions or public receipts.
- Global instructions name behavior and authority, not provider secrets or
  model tables.

## Testing Strategy

### Contract tests

- complete, incomplete, and invalid evaluations;
- valid-fragment extraction and strict rejection of malformed fragments;
- coverage and completion-request generation;
- stable fragment identity and provenance.

### Fallback tests

- unavailable best match followed by accepted fallback;
- incomplete attempt followed by complementing fallback;
- confirmation, dispute, replacement, and addition of fragments;
- same-provider degraded assurance;
- route exhaustion and bounded retries;
- identical frozen packet and contract identity across attempts.

### Consolidation tests

- deterministic union and exact deduplication;
- material findings from incomplete attempts remain active;
- disagreements remain visible;
- no approved verdict with unresolved material findings or coverage gaps;
- model-assisted consolidation appears as a separate attempt.

### Compatibility tests

- old receipts still verify;
- existing top-level fields retain meaning;
- `help-llm` schema changes are additive;
- human CLI output remains script-safe where currently documented.

### Environment tests

- local `uv` tool installation and discovery;
- Amélia installation with and without the user-local binary directory in
  `PATH`;
- global instruction discovery by Codex;
- global and project instruction discovery by Cursor, Codex, Pi, and Claude;
- rendered adapter fragments carry the same semantic contract version and
  digest;
- Claude imports or links the canonical project `AGENTS.md` without divergence;
- Cursor MDC wrappers, when present, contain only Cursor-specific scope metadata
  and a reference to the canonical contract rather than a second semantic copy;
- each agent can run `help-llm --format json`, `doctor --format json`, and a
  synthetic best-match/fallback smoke test;
- representative project reviews with project-specific triggers;
- no secrets in diagnostics, receipts, logs, or instruction files.

## Acceptance Criteria

The design is ready for global enforcement only when:

1. local and Amélia installations expose the same supported CLI and help schema;
2. representative Cursor, Codex, Pi, and Claude tasks discover and invoke
   `reviewctl` without hard-coded repository paths or divergent semantics;
3. incomplete responses contribute validated fragments to a later fallback;
4. consolidated output retains exact provenance and disagreements;
5. no incomplete attempt is accepted;
6. degraded assurance is visible and critical changes require human
   authorization;
7. old receipts verify and existing project integrations remain compatible;
8. expected errors provide machine-readable recovery without tracebacks;
9. the full test suite, lint, public-distribution checks, and formal review pass;
10. the advisory rollout produces no unresolved high-impact workflow defects.

Compatibility means equivalent review behavior and evidence, not identical
agent UX. Failure of one agent adapter does not authorize changing receipt
semantics for that agent.

## Non-Goals

- adopting BAML or another external typed-contract runtime;
- centralizing private model rosters in the public repository;
- allowing models to approve merges or deployments autonomously;
- treating fallback consensus as proof that a finding is true;
- replacing project tests, source inspection, runtime evidence, or human
  authorization;
- deploying a networked review service in this phase.
