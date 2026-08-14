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

## Execution Backends

Agent compatibility and execution backends are separate concerns. Cursor,
Codex, Pi, or Claude may invoke `reviewctl`; independently, `reviewctl` may use
one of those tools as the bounded runner that reaches an LLM.

The current `llm`, `openrouter`, `agy`, `pi`, `codex`, and `kiro` transports are
adapters behind one execution-backend seam rather than permanent branches in
the orchestration CLI. The backend families are:

| Family | Current or planned adapters | Purpose |
| --- | --- | --- |
| Agent CLI | Current: `pi`, `codex`, `agy`, `kiro`; planned candidates: `claude`, `cursor` | Reuse an installed agent, its authentication, subscriptions, and model access. |
| Provider gateway | `openrouter`, `openai-compatible` | Call a provider or local gateway directly when request and response identity are observable. |
| Generic model CLI | `llm` | Preserve the existing generic plugin-based route. |
| Agent protocol | `acp` | Future adapter for agents exposing a stable Agent Client Protocol server. |

Antigravity remains the `agy` adapter name until a compatibility migration is
designed. Gemini CLI, GitHub Copilot CLI, OpenCode, Aider, or another popular
runner are candidates, not automatically supported backends. A candidate must
first prove bounded noninteractive execution, durable output, timeout control,
model/backend identity to the extent claimed, and source-containment behavior.

### Backend interface

Every adapter consumes one provider-neutral request and returns one observed
result:

```text
BackendRequest
├── preparedContract
├── frozenPacket
├── requestedModel
├── timeout
├── executionMode: review | change
└── toolPolicy

BackendResult
├── exitStatus
├── rawResponse
├── conversationIdentity
├── requestedIdentity
├── observedIdentity
├── usage
├── mutationObservation
└── retainedEvidence
```

The adapter does not decide acceptance, fallback, waiver, or consolidation.
Those remain reviewctl core semantics.

### Backend capabilities

Discovery produces claims that selection can evaluate:

```text
BackendCapabilities
├── reviewReadOnly: enforced | sandboxed | advisory | unsupported
├── editableExecution: supported | unsupported
├── structuredOutput
├── resolvedModelIdentity
├── resolvedProviderIdentity
├── conversationIdentity
├── usageReporting
├── timeoutControl
├── toolControl
└── sourceIsolation: backend-enforced | external-sandbox | unavailable
```

Capabilities are qualified facts bound to adapter version, runner version,
machine-local setup, synthetic fixture, and observation time. An absent or
opaque model/provider identity lowers assurance; it is never filled by guess.

Pi is the preferred broker when it can reach the requested provider/model and
preserve the required identity, limits, contract, and evidence. A native
adapter remains justified when it can reuse a subscription or login Pi cannot,
exposes a capability Pi lacks, or provides stronger evidence. Pi is therefore
preferred by capability match, not a mandatory dependency.

Kiro is the current concrete example: its native adapter is justified by local
subscription access unavailable through Pi or OpenRouter. It reads Kiro's
runtime-owned inventory with `kiro-cli chat --list-models --format json` and
requires an exact returned model ID; it does not publish or infer a static
roster. The adapter is registered but unqualified, reports advisory read-only
and tool control, and declares `sourceIsolation: unavailable`.

### Review and editable execution

Qualified merge-gate formal attempts are non-editable and never run a backend
against the source checkout. For qualified merge-gate backends, reviewctl makes
every original source root inaccessible through a qualified backend-native
boundary or an OS/container sandbox after preparing a separate staging area
that contains only the frozen packet. Merely changing cwd or instructing the
model not to write is not source isolation.

The staging area is filesystem-read-only when the platform and backend support
enforcement; otherwise it is a disposable writable copy inside the sandbox.
Write and shell tools are also disabled where supported, but behavioral
instructions alone are insufficient.

reviewctl hashes the staged packet before and after execution. An unexpected
mutation produces `source-mutated`, discards the staging area, and prevents
acceptance or fragment promotion from that attempt. A backend with
`reviewReadOnly: advisory` or `sourceIsolation: unavailable` is ineligible for
qualified merge-gate review. reviewctl still allows the `run` transport to
invoke such an advisory attempt and persist its observed evidence; that is the
current behavior, not a claim that every formal attempt has merge-gate
isolation. An advisory formal attempt such as unqualified Kiro with
`sourceIsolation: unavailable` is not an isolation guarantee. It cannot become
qualified merge-gate evidence until a qualified external sandbox denies all
original source roots and organization qualification proves the boundary.
Availability and a valid receipt do not qualify its model or strengthen its
declared isolation.

Editable execution is a separate change-producing operation:

```text
snapshot A
  -> formal ReviewAttempt
  -> consolidated findings
  -> editable ChangeAttempt
  -> patch or commit B
  -> new formal review of B
```

A `ChangeAttempt` may use Pi, Cursor, Claude, Codex, Antigravity, or another
qualified editable backend. It produces a patch/commit and execution evidence,
never a review approval. The initial stabilization phase specifies this type
and capability but does not need to implement autonomous review-fix-review
loops.

## Local Execution Setup

All execution is local to the machine running `reviewctl`. Eloísa and Amélia
discover and invoke only their own executables and credentials. Models and
provider endpoints may be remote, but reviewctl does not dispatch over SSH,
copy source to another machine, or synchronize credentials.

```text
LocalExecutionTopology
├── installationIdentity
├── actorIdentity
├── evidenceRoot
├── projectStores[]
├── backends[]
│   ├── adapter
│   ├── executable and version
│   ├── authentication mechanism/status
│   └── capabilities
└── diagnostics[]
```

Machine identity is a local installation identity, not a public hostname.
Authentication checks report only mechanism and status; credentials are never
persisted in setup output.

Add read-only discovery commands:

```text
reviewctl setup discover --format human|json
reviewctl setup check --format human|json
reviewctl setup show --format human|json
```

`discover` observes local executables and declarative configuration. `check`
performs version-only local executable discovery; it does not authenticate or
call a model or provider. `show` renders the effective topology. Discovery never
installs tools, logs in, changes global agent rules, or promotes a backend to
supported without conformance evidence.

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
├── routes[]: backend + requested model/provider + capabilities
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
  ├── complete but rejected ──> fallback without fragment promotion
  ├── incomplete ─────────────> fragment extraction ──> fallback
  └── unavailable/invalid ────> fallback
```

A contract-complete response rejected by a later mutation, identity, policy,
conversation, provider, or transport gate is not an accepted value. Its raw
evidence and rejection remain persisted, but none of its content is promoted
as a valid fragment. If another authorized route remains, orchestration
restages the original frozen packet and continues; otherwise the chain ends
`unavailable`.

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
- whether it replaces an unavailable, invalid, or complete-but-rejected result,
  or complements an incomplete one;
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

The consolidator evaluates these conditions in order and selects exactly one:

1. `changes-requested` when an undisputed, unreplaced, unresolved material
   finding remains active in any valid fragment or complete attempt;
2. `needs-adjudication` when no such finding exists but valid attempts retain a
   material disagreement without an independently verified resolution;
3. `approved` when at least one complete accepted attempt exists, required
   coverage is complete, no unresolved material finding or disagreement exists,
   and the receipt verifies;
4. `unavailable` otherwise, including route exhaustion, incomplete coverage,
   or a complete response rejected by mutation, identity, policy, transport, or
   another acceptance gate.

Lower-priority conditions remain visible in findings, disagreements, gaps, and
attempt outcomes even when a higher-priority verdict is selected.

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

### Project and origin identity

A project carries a stable, non-secret `ProjectId`; repository paths,
worktrees, remotes, and machine names are aliases rather than identity. Eloísa
and Amélia may use the same `ProjectId` and `ActorId` while retaining distinct
`OriginId` values and append-only origin sequences. Two origins owned by one
actor are not two independent human reviewers.

The repository may carry only portable, non-secret identity and project review
requirements in `.reviewctl/project.toml`. Journals, raw evidence, signing keys,
backend setup, and credentials remain outside the source repository.

Each machine writes its own local journal. Imported facts preserve the original
`OriginId`, sequence, event identity, signature, and causal links. Import is
idempotent and rejects an event identity reused with different bytes.

### Portable exchange

reviewctl owns a self-contained, signed `ReviewExchangeBundle` format. It may
contain a project/case manifest, receipts, review events, consolidations,
signatures, and export-policy-permitted blobs. Omitted or sealed blobs remain
explicit; importing a summary bundle never implies possession of raw source or
responses.

### Synchronization contract

The canonical bundle manifest contains at least:

```text
ReviewExchangeBundle
├── schemaVersion
├── bundleId
├── projectId
├── originId
├── sequenceStart
├── sequenceEnd
├── previousBundleId
├── events[]
│   ├── eventId
│   ├── originSequence
│   ├── parentEventIds[]
│   ├── eventType
│   └── payloadDigest
├── objects[]
├── trustSnapshot
└── signatures[]
```

`bundleId` is the SHA-256 digest of the canonical manifest, events, objects, and
trust snapshot with the `bundleId` and `signatures` fields excluded. Adding an
allowed signature does not change bundle identity. `eventId` is the digest of
the canonical project, origin, sequence, parents, type, and payload digest with
the `eventId` field excluded. Each `ProjectId + OriginId` sequence is strictly
monotonic and contiguous. A delta bundle may start after sequence one only when
the importer already holds the preceding cursor for that exact pair and
`previousBundleId`; the first bundle uses no previous identifier.

Import behavior is deterministic:

- an existing `eventId` with identical canonical bytes is an idempotent no-op;
- an `eventId` reused with different bytes is a conflict;
- a `ProjectId + OriginId + originSequence` reused by a different event is an
  origin fork;
- a missing parent, sequence gap, or mismatched previous bundle is incomplete;
- conflict, fork, incomplete, malformed, untrusted, revoked-key, and replayed
  inputs never append review facts.

An import never edits, replaces, renumbers, resolves, or silently consolidates
an existing journal. It either appends previously unseen verified facts and a
local import record, reports an exact duplicate, or produces an explicit
quarantine/conflict result.

### Trust and replay protection

Every bundle signature records `keyId`, algorithm, signed payload digest, and
signature bytes. Local trust policy pins an organization/project trust root and
binds authorized keys to exact `ProjectId + OriginId` pairs, bundle classes,
validity periods, and allowed export classifications. Organization-wide roots
delegate project scopes explicitly; authorization for one project never implies
authorization for another. Rotation and revocation are signed trust events with
monotonic versions; a replacement key does not rewrite bundles signed by its
predecessor.

The importer verifies the canonical payload digest, signature threshold,
key/project/origin authorization, exact target-project match, validity interval,
trust-version continuity, revocation state, previous bundle, and project-origin
cursor before append. A bundle at or below the accepted cursor is permitted
only when its canonical payload and events are already known; an additional
valid signature may be recorded without re-appending facts. A different
canonical event or payload at an accepted sequence is a replay/fork conflict.

Offline verification is supported against a pinned local trust snapshot and
records that snapshot's digest and freshness time. It proves validity relative
to that snapshot, not that no newer key revocation or policy exists. Policy may
require a fresher checkpoint before importing restricted material.

### Object privacy

Every embedded or referenced object declares:

```text
ExchangeObject
├── objectId and digest
├── classification
├── disclosure: plaintext | redacted | sealed | omitted
├── allowedRecipients[]
├── visibleMetadata[]
└── ciphertextDigest
```

Export policy evaluates each object independently. Redaction creates a new
derived object with provenance to the private original digest; it never claims
byte identity with the original. Sealing binds declared recipients and retains
only ciphertext in the bundle. Omitted objects leave a typed locator/digest only
when policy permits even that metadata. Bundle-level metadata uses an explicit
allow-list so project names, paths, reviewer/model identities, findings, and
timestamps are not made visible merely because payload blobs are encrypted.

### Import state and quarantine

Inspection and import return a typed result:

```text
BundleImportResult
├── status: accepted | duplicate | quarantined | conflict
├── bundleId
├── sourceOriginId
├── importerActorId
├── importerOriginId
├── cursorBefore
├── cursorAfter
├── trustSnapshotDigest
├── appendedEventIds[]
├── reasons[]
└── quarantineLocator
```

Malformed, incomplete, untrusted, revoked, or policy-incompatible bundles enter
a non-authoritative quarantine without journal append. Quarantine bytes are
content-addressed, access-controlled, and excluded from projections. A later
retry re-verifies the original bytes under an explicit updated trust/policy
snapshot; it never mutates the quarantined artifact. Accepted imports append a
local `BundleImported` event recording which actor/origin imported each source
bundle and which facts were newly appended.

The core exchange interface is transport-neutral:

```text
EvidenceExchange
├── export(project, cursor, exportPolicy) -> SignedBundle
├── inspect(bundle) -> BundleSummary
└── import(bundle, trustPolicy) -> ImportResult
```

Initial sharing is manual file export/import. Optional later transports may
publish, fetch, and list opaque bundle bytes. Potzal is one possible artifact
transport and store: it may retain and distribute a complete
ReviewExchangeBundle as an immutable generic artifact, but reviewctl never
depends on Potzal types, catalogs, lifecycle, availability, or validity
decisions. Filesystem, removable media, a private object store, or a future
protocol adapter remain possible.

No transport decides whether a review is valid, accepted, independent,
consolidable, or waived. There is no multi-primary review database, remote
execution, or automatic synchronization in the stabilization phase.

The central ownership rule is:

> reviewctl interprets, verifies, imports, and consolidates review events.
> Potzal, when configured, only preserves, authorizes access to, and distributes
> opaque verifiable bundles.

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

Project identity and manual exchange use explicit commands:

```text
reviewctl project init --project-id ID
reviewctl project show --format human|json
reviewctl exchange export --project ID --output BUNDLE
reviewctl exchange inspect BUNDLE --format human|json
reviewctl exchange import BUNDLE [--project ID]
```

`project init` writes only the non-secret project identity after explicit user
invocation. `exchange inspect` does not import or trust a bundle. Import uses
the current repository's effective `ProjectId`; outside a project it requires
`--project`. The effective project must exactly match the signed bundle project.
Import then verifies the signature, project-origin authorization, origin
continuity, export classification, and local trust policy before appending any
event.

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
- available execution backends and required binaries;
- discovered backend capabilities and their qualification status;
- evidence-root writability;
- whether the user-local binary directory is in `PATH`;
- detected Cursor, Codex, Pi, and Claude instruction locations and contract
  digests;
- machine-readable remediation for local and Amélia installations.

`help-llm` points agents to `doctor --format json` when discovery or environment
checks fail.

## Global Instruction Rollout

### Phase 0: Baseline

Inventory current local and Amélia installations independently: paths,
versions, global instructions for all four supported agents, executable
backends, observed capabilities, project pointers, and representative tasks.
Record baseline CLI and receipt fixtures. Do not dispatch between machines.

### Phase 1: Stabilize

Implement additive result types, partial evaluation, fallback relationships,
consolidation, the backend seam, local setup diagnostics, deterministic local
journals, and manual ReviewExchangeBundle export/import. Defer transport-backed
and automatic federation. Do not change global agent requirements yet.

### Phase 2: Document

Update:

- `HELP-LLM.md` and generated machine help;
- installation and Amélia discovery guidance;
- Cursor, Codex, Pi, and Claude compatibility and instruction-rendering
  guidance;
- backend capability, read-only execution, and native-adapter guidance;
- local setup and manual ReviewExchangeBundle guidance;
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

The advisory rollout stores each workflow defect as a typed local journal
event:

```text
WorkflowDefect
├── defectId
├── severity: critical | high | medium | low
├── affectedVersion
├── reproduction
├── status: open | resolved | superseded
└── resolutionEvidence[]
```

`critical` means the defect can produce false approval, unauthorized source
disclosure, credential disclosure, or destructive mutation of the source
checkout. `high` means it can corrupt or lose durable evidence, or prevent a
claimed-supported backend from completing its documented bounded flow.
Resolution evidence names the regression test or synthetic reproduction, its
result, and the candidate version/commit. `superseded` additionally names the
replacement defect or design event.

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
- Existing transport names remain accepted aliases while their implementations
  move behind the backend seam.
- Exchange and Potzal integration are optional and cannot change local receipt
  verification.
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
- Formal review backends operate against frozen or disposable source and fail
  acceptance when an unexpected mutation is observed.
- Editable attempts cannot be promoted into review attempts or approvals.
- Exchange bundles disclose only fields and blobs permitted by export policy;
  transports receive opaque bytes and locators, never backend credentials.
- Bundle signatures, trust roots, cursors, and replay decisions belong to
  reviewctl even when bundle bytes are stored in Potzal.
- Quarantined imports are non-authoritative and excluded from all review
  projections until a later explicit verification succeeds.

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
- human CLI output remains script-safe where currently documented;
- legacy `llm`, `openrouter`, `agy`, `pi`, and `codex` invocations preserve
  their receipt meaning through backend adapters.

### Backend conformance tests

- Pi, Codex, Cursor, Claude, and Antigravity noninteractive invocation through
  their declared supported modes;
- unavailable executable, unauthenticated login, timeout, empty output, and
  malformed structured output;
- requested versus observed model/provider identity and degraded assurance;
- read-only enforcement or disposable-copy isolation and mutation detection;
- a negative source-root probe proving that an absolute-path write outside the
  staging area is denied for every formally supported backend/platform pair;
- adapter version and capability qualification bound to a synthetic fixture;
- Pi preference only when it preserves the route's required capabilities;
- editable attempts produce patches/commits but never accepted review fields.

### Exchange tests

- deterministic self-contained bundle identity;
- signature and trust-policy verification before journal append;
- idempotent repeated import and conflicting-event rejection;
- per-origin monotonic contiguous sequence, previous-bundle cursor, and
  preserved causal links;
- event-ID collision, origin-sequence fork, missing-parent, sequence-gap, and
  replay quarantine;
- cross-project substitution rejection when a valid origin key is not delegated
  to the signed and effective `ProjectId`;
- key rotation, revocation, threshold, validity-period, and stale offline trust
  snapshot behavior;
- summary-only, sealed-blob, and full-evidence export policies;
- per-object classification, redaction provenance, recipient-bound sealing,
  omission, and visible-metadata allow-list behavior;
- quarantine exclusion from projections and explicit retry under a new
  trust/policy snapshot;
- `BundleImported` provenance recording source and importing origins plus cursor
  movement;
- proof that import never updates, deletes, renumbers, or silently consolidates
  an existing journal event;
- identical consolidated projection after importing the same origin streams in
  different orders;
- filesystem transport first; optional Potzal transport treats the bundle as
  opaque and is not required for local verification.

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
- setup discovery remains machine-local and reports no secrets;
- Eloísa and Amélia may expose different backend sets under the same schema;
- representative project reviews with project-specific triggers;
- no secrets in diagnostics, receipts, logs, or instruction files.

## Acceptance Criteria

The design is ready for global enforcement only when:

1. local and Amélia installations expose the same supported CLI and help schema;
2. representative Cursor, Codex, Pi, and Claude tasks discover and invoke
   `reviewctl` without hard-coded repository paths or divergent semantics;
3. on a machine where Cursor is the only available LLM access path and its
   native execution backend has passed conformance, a human or any supported
   invoking agent can complete a bounded local review with honest identity and
   assurance fields;
4. incomplete responses contribute validated fragments to a later fallback;
5. consolidated output retains exact provenance and disagreements;
6. no incomplete attempt is accepted;
7. degraded assurance is visible and critical changes require human
   authorization;
8. old receipts verify and existing project integrations remain compatible;
9. expected errors provide machine-readable recovery without tracebacks;
10. formal review cannot mutate the reviewed source, while editable execution
    remains a separate non-approval artifact;
11. local setup on Eloísa and Amélia requires no remote dispatch or shared
    credentials;
12. a manual Eloísa-to-Amélia exchange verifies signature and trust continuity,
    advances the correct origin cursor, preserves an identical consolidated
    projection, and quarantines a tampered/replayed variant without modifying
    either authoritative journal;
13. the candidate commit passes the repository-versioned commands
    `uv run pytest` and `uv run ruff check .`; every backend claimed supported
    has a passing conformance case in that committed test suite and a receipt
    bound to the candidate commit or one of its ancestors;
14. a formal findings-json review of the candidate commit is accepted, its
    receipt verifies, and every reported material finding is resolved or
    independently rejected with recorded evidence;
15. every advisory `WorkflowDefect` classified `critical` or `high` has status
    `resolved` or `superseded` and satisfies the required `resolutionEvidence`;
    none remains open, merely acknowledged, or deferred at global enforcement.

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
- deploying a networked review service in this phase;
- dispatching review execution to another machine;
- requiring Potzal or any other federation transport for local correctness;
- automatic cross-machine synchronization in the stabilization phase;
- treating two machines owned by one actor as independent human approval.
