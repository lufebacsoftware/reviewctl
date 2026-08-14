# Review Evidence Architecture

`reviewctl` is a neutral review evidence control plane. It owns contracts,
attempt execution, and receipts. It does not own an organization's model roster,
a project's canonical history, or a federation peer's trust policy.

## Ownership planes

1. **Contract plane — reviewctl.** Typed inputs and outputs, schemas, output
   instructions, exact decoding, semantic validation, normalization, versions,
   and contract identity.
2. **Attempt and receipt plane — reviewctl.** Frozen-input provenance,
   invocation manifests, observed transport facts, response identity, attempt
   outcomes, canonical receipts, sealing, and verification.
3. **Project evidence plane — project owner.** One logical
   `ProjectReviewJournal` per stable project identity, containing imported
   receipts, finding lifecycle events, adjudications, waivers, fixes, and
   verification observations.
4. **Qualification plane — organization.** Model/provider qualification,
   tournaments, corpora, capability periods, policies, budgets, and council
   decisions.
5. **Federation and aggregate plane — participating authorities.** Signed,
   sanitized exports, compatible dimensions, idempotent imports, and
   reproducible projections. It is not canonical storage for private origin
   evidence.

## Backend execution boundary

reviewctl keeps the controller, adapter process, evidence handling, and setup
diagnostics local. Provider or model execution may be remote for `REMOTE_API`
backends. Local reviewctl execution does not enable remote controller or adapter
dispatch. Federation remains deferred and separate from backend execution and
setup synchronization.

The BAML-inspired typed boundary is native and has no BAML dependency. The
concepts are adopted natively, with no BAML runtime dependency; BAML is not a
library, service, generator, or runtime requirement.

The boundary has three provider-neutral types:

- **BackendRequest** carries the prepared prompt, frozen source paths, requested
  model, limits, and attempt location into an adapter.
- **BackendExecution** returns only observed transport outcome, response, and
  persisted evidence locations.
- **BackendCapabilities** declares what an adapter can report or enforce; it is
  not proof that the adapter is qualified for review.

Backend adapters only invoke a backend and persist its observed evidence;
adapters do not decide acceptance. The controller alone owns policy, contract
evaluation, acceptance, fallback, and receipt construction.

The five legacy compatibility adapters are explicitly unqualified: `llm`,
`openrouter`, `agy`, `pi`, and `codex`. They preserve existing route behavior
without claiming conformance. Availability is not qualification.

Setup diagnostics observe only executable presence and version for registered
executable backends. Setup diagnostics never authenticate, call a model or
provider, or write configuration. The next gate is backend conformance before
Cursor, Claude Code, or another native backend can be added.

Registration and discovery are not support or qualification claims. Adapters
may be registered while remaining unqualified, and availability is not
qualification. Public documentation therefore contains no operating model
roster, prices, provider-specific invocation commands, or credentials. It does
not claim Cursor or Claude Code support.

## Partial review and bounded completion

An eligible typed response evaluates as **complete, incomplete, or invalid**.
Transport, timeout, model, provider, empty-response, and conversation pre-gates
run before contract evaluation. Rejected responses never promote fragments.
Only a response that passes those gates and is incomplete with independently
valid findings may contribute promoted fragments.

The controller can use those findings as bounded input to a later attempt. The
completion context is bound to the target contract, never contains the raw
response, and never inherits approval. It carries only revalidated typed
fragments, their provenance, and the target contract's missing-field manifest.
Absence is not a dispute: a reviewer that does not repeat an earlier finding
has not contradicted or resolved it.

maxAttempts applies independently to each route. Retrying one route does not
consume another route's allowance. A same-route retry and a route fallback are
recorded as different relationships.

acceptedAttempt names a real complete accepted attempt. A partial response,
promoted fragment, fallback, or consolidated projection can never occupy that
field or manufacture approval. The legacy verdict and findings remain the view
of that accepted attempt. The consolidated view preserves partial or
unconfirmed findings with provenance; approval is stricter because any such
finding prevents the consolidated view from claiming approval.

New receipts use schema v2, which adds offline structural verification of
attempts, contract identities, fallback relationships, promoted fragments, and
consolidation. Schema v1 remains digest-only for compatibility; schema v2 adds
offline structural verification rather than changing historical v1 meaning.

## Current deployment boundary

reviewctl is local-first: its controller, policy decision, evidence assembly,
and verification run on the user's machine. Project-owned evidence stores are
a compatible future destination for receipts. Federation is optional future
work, and Potzal is not a dependency. Potzal or another store may later carry
signed bundles without owning reviewctl semantics.

Editable execution is deferred for formal review. Cursor, Claude Code, and
other interactive editors are also deferred until a backend passes explicit
conformance and organization qualification. No current receipt claims those
capabilities.

## Canonical terms

- **ReviewContract:** versioned native definition of one typed review response.
- **PreparedContract:** provider-neutral instructions and schema compiled for
  one review context, with a canonical digest.
- **ContractEvaluation:** exact decode and semantic-validation result for one
  raw attempt payload.
- **AcceptanceGate:** orchestration decision that combines transport, model,
  provider, conversation, and contract evidence. A valid contract payload alone
  is not acceptance.
- **ReviewReceipt:** immutable canonical evidence produced by one run.
- **ReviewEvent:** immutable statement appended to a project or organization
  journal.
- **ProjectReviewJournal:** canonical ordered events for one stable ProjectId.
- **QualificationJournal:** organization-owned capability and policy events.
- **EvidenceBlob:** sealed, content-addressed private artifact kept outside
  query rows.
- **Projection:** disposable query state rebuilt from canonical journals.
- **DimensionDefinition:** versioned meaning, privacy class, and compatibility
  rules for one analytical axis.
- **FederationBundle:** signed export from one authorized origin and export
  sequence range.
- **Supersession:** a new event correcting an earlier interpretation without
  rewriting history.

The canonical term is **append-only journal**, not “write-only database.” A
journal is readable for verification and replay; its derived projections may be
discarded and rebuilt.

## Identity

A project journal is bound to `OrganizationId + ProjectId`. The owning
organization assigns ProjectId. Paths, remotes, mirrors, repository renames,
and worktrees are aliases rather than project identity. A fork receives a new
ProjectId only when it becomes independently governed; lineage can name its
parent and source version.

Every future ReviewEvent must carry an EventId, exactly one project or
organization origin scope, an origin sequence, schema version, payload digest,
continuity proof, classification, actor, timestamps, and causal links. Reusing
an EventId with different bytes is invalid.

## Evidence and observability

Evidence capabilities are stated separately:

- `snapshotIntegrity`: reviewctl hashes the exact locally frozen input bytes.
- `reviewDeclaration`: where required, the model declares frozen basenames and
  reviewctl compares the declaration with the packet.
- `invocationManifest`: reviewctl records the command or sanitized request it
  assembled for a transport.
- `providerRequestObserved`: reviewctl directly observed the provider-native
  request; this is unavailable for opaque intermediary CLIs.

None of these capabilities proves cognition, attention, semantic understanding,
or correct reasoning. A finding becomes confirmed evidence only after
independent reproduction or equivalent verification.

## Retention and trust

Append-only metadata does not override privacy or legal obligations. Corrections
use supersession. Sensitive EvidenceBlobs may be cryptographically erased under
an authorized retention event while the minimum lawful audit statement remains.

Federation transfers signed, allow-listed facts rather than database access.
Trust roots bind OrganizationId to authorized signing keys, validity periods,
bundle classes, rotation, compromise, and revocation rules. A valid signature
from an unbound or unauthorized key is rejected. Published-stream continuity is
verifiable, but no bundle claims completeness over a private origin journal.

See [ADR 0001](adr/0001-append-only-journals.md),
[ADR 0002](adr/0002-signed-federation-bundles.md), and the
[evidence contract](EVIDENCE.md).
