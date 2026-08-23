# reviewctl Open Source Tool, CLI, API, and Pi Transport Design

**Status:** Draft for user review  
**Date:** 2026-08-23  
**Scope:** Product direction and first implementation boundary  
**License:** Keep the repository's existing Apache-2.0 license

## 1. Product decision

reviewctl will be an open source tool for durable, project-scoped AI review
work. It is not a model marketplace, hosted inference service, or second
interactive agent. Pi is the first-class execution transport because it
already provides the model/provider access and interactive runtime that users
want.

The product owns the durable layer around a review:

- selecting and freezing the review packet;
- applying one-time project privacy settings;
- executing a bounded request through a transport;
- recording provenance, usage, outcome, and artifacts;
- maintaining a project review journal;
- preserving findings through \`open\`, \`disputed\`, \`fixed\`, \`verified\`, and
  \`dismissed\` states;
- consolidating multiple review passes without manufacturing approval.

The product does not own provider authentication, model routing across every
vendor, or the interactive conversation UX. Those remain transport or harness
responsibilities.

## 2. User problem and promise

The first user is a developer or technical lead working in a private or
experimental repository who wants a second pair of eyes without losing the
result in a chat transcript.

The product promise is:

> reviewctl keeps AI review work attached to the project, records what was
> reviewed and what happened to each finding, and makes privacy a project
> setting rather than a per-run ceremony.

reviewctl does not promise that an LLM is correct, that consensus proves truth,
or that a receipt is a merge approval. A receipt is evidence of an execution;
source, tests, and human decisions remain authoritative.

## 3. Ownership and seams

The external seam is a small review engine interface. Adapters implement the
transport seam; callers do not need to know how Pi, OpenRouter, Kiro, or a
future GitHub runner works.

    CLI / Python API / Pi command / CI integration
                        |
                  ReviewClient
                        |
            ReviewEngine + ProjectJournal
                        |
                  Transport seam
                        |
                 Pi adapter first

### reviewctl owns

- \`ReviewRequest\` and \`ReviewResult\` interfaces;
- project configuration and configuration precedence;
- privacy decision and packet boundaries;
- contract preparation and response evaluation;
- attempt state and fallback relationships;
- artifact permissions and receipt verification;
- journal events and finding lifecycle.

### Pi owns

- model and provider credentials;
- model invocation and conversation runtime;
- interactive tools when the caller explicitly enables them;
- provider-specific usage and pricing observations;
- the raw response stream.

Formal review attempts through Pi use a clean bounded session. They do not
inherit an arbitrary interactive conversation, and they do not receive
unrestricted tools unless a project profile explicitly permits that mode.

### The project owns

- \`reviewctl.toml\` or an equivalent project configuration;
- the project privacy mode;
- the named review profiles;
- the journal location and whether generated artifacts are committed or
  ignored.

### External integrations own

GitHub, CI, editors, and future hosted runners only translate their native
event into a \`ReviewRequest\` and present a \`ReviewResult\`. They do not become
alternate authorities for review semantics.

## 4. Configuration-first interface

The normal user should not repeat transport, model, timeout, contract, and
privacy flags on every invocation. A project configuration is the default
source of operational choices.

Example \`reviewctl.toml\`:

    [project]
    name = "example-project"
    visibility = "private"
    privacy_mode = "private"

    [defaults]
    profile = "pi-review"

    [profiles.pi-review]
    routes = [
      "pi:openrouter/stealth/ox-alpha",
      "pi:openrouter/meta/muse-spark-1.2-contributor",
    ]
    response_contract = "findings-json"
    timeout_seconds = 300
    max_attempts = 1
    max_output_tokens = 8000
    tools = "none"

    [profiles.pi-exploration]
    routes = ["pi:openrouter/stealth/ox-alpha"]
    response_contract = "document"
    timeout_seconds = 180
    max_attempts = 1
    tools = "read-only"

Configuration precedence is:

1. explicit CLI option;
2. project \`reviewctl.toml\`;
3. user configuration at \`~/.config/reviewctl/config.toml\`;
4. safe built-in defaults.

The project privacy floor cannot be weakened by an ordinary CLI override. A
\`sensitive\` project cannot be changed to external execution by a profile.
Operational values such as model, timeout, contract, and output path may be
overridden and are recorded in the receipt.

Provider credentials, API keys, and private model inventories never belong in
the project configuration. They remain in the harness or environment owned
by the user.

## 5. Privacy model

Repository visibility and data sensitivity are separate values.

    personal  external review allowed by project default
    private   only explicit frozen packets may leave the project
    sensitive local-only by default
    public    no special exfiltration restriction

\`private\` does not mean every review is blocked. It means reviewctl must know
exactly which files or diff are being sent. \`sensitive\` is the stricter mode.

The privacy decision is made once when the project is initialized or its
configuration changes. A normal run does not perform a new policy ceremony.
The receipt records the effective mode, destination, transport, and packet
digest.

## 6. Public Python API

The public Python surface should be deliberately small and stable. Parser
internals, adapter-specific functions, and receipt construction helpers are
not public API.

    from pathlib import Path

    from reviewctl.api import ReviewClient, ReviewRequest

    client = ReviewClient.from_project(Path("."))
    result = client.review(
        ReviewRequest(
            prompt="Review the current change for correctness and regressions.",
            files=(Path("src/example.py"), Path("tests/test_example.py")),
            profile="pi-review",
        )
    )

    print(result.status)
    print(result.receipt_path)
    for finding in result.findings:
        print(finding.severity, finding.path, finding.message)

The initial public types are:

    @dataclass(frozen=True)
    class ReviewRequest:
        prompt: str
        files: tuple[Path, ...] = ()
        profile: str = "default"
        review_id: str | None = None

    @dataclass(frozen=True)
    class ReviewResult:
        status: str
        review_id: str
        receipt_path: Path
        findings: tuple[Finding, ...]
        diagnostic: Diagnostic | None

`Finding` is the existing normalized review finding shape: severity, path,
location when available, message, and optional evidence. `Diagnostic` is a
transport or orchestration problem with a stable error code, human message,
retryability, and safe artifact references.

\`ReviewClient\` loads project configuration, prepares the packet, invokes the
selected transport, evaluates the response, and appends journal facts. A
caller does not need separate functions for policy, route parsing, artifact
paths, or receipt assembly.

The API also exposes read-only project operations through the same client:

    client.findings(status="open")
    client.journal()
    client.verify(receipt_path)

## 7. CLI surface

The CLI becomes a thin interface over the public API. The primary commands are:

    reviewctl init [--mode personal|private|sensitive]
    reviewctl review [--profile NAME] [--file PATH ...] [--prompt TEXT]
    reviewctl status
    reviewctl findings [--status STATUS]
    reviewctl verify RECEIPT
    reviewctl doctor [--profile NAME]

The existing \`reviewctl run\` remains as a compatibility command during the
migration. New documentation and integrations use \`review\`, not \`run\`.

Machine-readable output is available on every primary command:

    --format text
    --format json

Stable exit meanings are:

    0  completed review; findings may still exist
    1  completed review with a caller-selected failure condition
    2  invalid command or configuration
    3  transport unavailable, timeout, or empty result
    4  privacy or policy denied execution
    5  receipt verification failed

The \`--fail-on\` option controls whether findings affect the process exit code;
a successful review with findings is not itself a transport failure.

Every machine-readable error includes:

    {
      "error": {
        "code": "privacy_denied",
        "message": "private project requires an explicit frozen packet",
        "retryable": false,
        "next": "select files or change the project profile",
        "artifacts": []
      }
    }

Errors never include prompts, source, credentials, or raw provider responses.

## 8. Review flow and outcomes

The core flow is:

    load project config
      → classify privacy
      → freeze packet
      → reserve bounded attempt
      → invoke Pi transport
      → extract response
      → evaluate contract
      → append journal events
      → settle receipt

Every attempt ends in one of these states:

    accepted
    partial
    empty
    timeout
    contract_failed
    transport_failed
    policy_denied

An \`empty\` result preserves observed usage and diagnostics and is not retried
indefinitely. A \`partial\` result may contribute validated findings to a later
completion attempt, but can never become an accepted review by itself.

## 9. Project journal and artifacts

The first journal implementation is local and inspectable. It may use JSONL
events and content-addressed artifact files without requiring a database.
Derived views are rebuildable.

Each review stores:

    review/review-id/
      packet.json
      response.md
      findings.json
      receipt.json
      diagnostics.json

Raw prompts, packets, sessions, and provider streams use private file
permissions. A receipt contains metadata and digests rather than an implicit
copy of sensitive source.

The journal is the future interchange format. Sharing and federation may later
export signed, sanitized event bundles, but no hosted service is required for
local use.

## 10. Implementation shape

The current \`cli.py\` is not the long-term module boundary. The first refactor
should create focused modules without changing review semantics:

    src/reviewctl/
      api.py          public ReviewClient, ReviewRequest, ReviewResult
      config.py       TOML loading, precedence, profile resolution, digest
      engine.py       bounded review orchestration
      journal.py      project events and finding projections
      artifacts.py    private artifact paths and writers
      transports.py   transport interface and registry
      pi_transport.py Pi adapter implementation
      contracts.py    typed response contracts
      review_flow.py  partials, fallback, consolidation, verification helpers
      cli.py          argument parsing and command presentation only

The transport interface remains small:

    class ReviewTransport(Protocol):
        def execute(self, request: BackendRequest) -> BackendExecution: ...

Pi is the first real adapter. Direct OpenRouter and existing native adapters
remain available only where they provide a capability Pi cannot provide or
where compatibility requires them. They do not get new product-specific UX
until the Pi path is stable.

## 11. Future integration boundary

GitHub, CI, editor commands, and a future static or hosted runner consume the
same \`ReviewClient\` and configuration model. Their responsibilities are:

1. create a bounded request from their event;
2. select a project profile;
3. invoke reviewctl;
4. render the result in their native surface.

They do not define a second journal or second receipt format. A GitHub Action
is therefore a later distribution target, not the first product architecture.

## 12. Explicit non-goals for the first implementation

The first implementation will not add:

- a hosted account or billing system;
- a central model proxy;
- a GitHub App or webhook server;
- federation or Potzal integration;
- a model qualification marketplace;
- a dashboard;
- automatic merge approval;
- a rewrite of every existing transport;
- a new runtime dependency for TOML or provider access.

These remain possible consumers of the open protocol after the local tool has
demonstrated repeated value.

## 13. Verification requirements

The first implementation is complete only when the following are demonstrated:

- a project can be initialized with one privacy mode and one Pi profile;
- \`reviewctl review\` runs through the Pi transport and produces a receipt;
- the Python API produces the same result as the CLI;
- configuration precedence is tested and recorded in the receipt;
- private projects send only the explicit frozen packet;
- empty, timeout, and partial Pi results are distinct and documented;
- artifacts use private permissions;
- \`--format json\` is stable enough for automation;
- \`reviewctl verify\` validates the produced receipt;
- existing compatibility tests for \`run\` remain green;
- a clean repository run passes the full test suite and Ruff.

## 14. Success and change criteria

The product is useful when the same project can be reviewed repeatedly from Pi
and the user can answer, without reopening old chats:

- what was reviewed;
- which model and packet were used;
- which findings remain open;
- which findings were fixed or verified;
- what a second review added or contradicted;
- what the execution cost and outcome were.

If users only run one-off reviews and never consult the journal, the project
should shrink toward a simpler Pi skill rather than grow more federation or
orchestration infrastructure.
