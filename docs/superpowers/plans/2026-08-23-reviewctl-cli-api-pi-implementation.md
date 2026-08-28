# reviewctl CLI, API, and Pi Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Turn reviewctl into a readable open source tool with a small Python API, project configuration, a thin CLI, and Pi as the primary execution transport while preserving the current \`run\` compatibility path.

**Architecture:** Add a deep \`ReviewClient\` interface above focused configuration, engine, journal, artifact, and transport modules. The CLI becomes presentation and argument translation only. Pi is the first adapter at the transport seam; GitHub and other integrations consume the same client later.

**Tech Stack:** Python 3.12, standard-library \`tomllib\`, existing dataclasses and contracts, Pi 0.83 JSON mode, pytest, Ruff, existing Apache-2.0 package.

---

## Execution policy

Pi reviews through openrouter/stealth/ox-alpha and
openrouter/meta/muse-spark-1.2-contributor are advisory and cannot block a
local implementation. A task is accepted only after its focused tests, receipt
verification, git diff --check, and relevant Ruff checks pass.

The checkout already contains user changes in README.md, docs/HELP-LLM.md,
src/reviewctl/cli.py, tests/test_run.py, and docs/HANDOFF.md. Do not overwrite
or stage those changes accidentally. Before each commit, inspect git diff and
stage only the intended hunks. The implementation may modify those files only
through narrow, reviewed patches that preserve their existing changes.

The first implementation slice stops after configuration, public API,
artifact/journal boundaries, Pi transport, and the new CLI front door work
locally. GitHub, hosted execution, and federation are separate follow-up
slices.

## File map

Create these focused modules:

- \`src/reviewctl/config.py\`: project and user TOML loading, precedence,
  profiles, privacy floor, and configuration digest.
- \`src/reviewctl/errors.py\`: stable error codes, retryability, and CLI exit
  mapping.
- \`src/reviewctl/api.py\`: public \`ReviewClient\`, \`ReviewRequest\`, and
  \`ReviewResult\` interface.
- \`src/reviewctl/engine.py\`: orchestration behind the public API; no argparse.
- \`src/reviewctl/artifacts.py\`: private artifact directories and writers.
- \`src/reviewctl/journal.py\`: append-only review and finding events plus
  read-only projections.
- \`src/reviewctl/pi_transport.py\`: Pi invocation and JSON event extraction at
  the existing backend seam.

Modify these files:

- \`src/reviewctl/backends.py\`: expose the transport capability contract
  needed by \`engine.py\` without adding provider-specific behavior.
- \`src/reviewctl/cli.py\`: retain old handlers, add thin \`init\`, \`review\`,
  \`status\`, \`findings\`, and \`doctor\` handlers, and delegate review work to
  \`ReviewClient\`.
- \`src/reviewctl/__init__.py\`: export only the stable public API and version.
- \`README.md\`, \`docs/PI-INTEGRATION.md\`, and \`docs/HELP-LLM.md\`: document
  the new path while keeping compatibility guidance for \`run\`.

Create focused tests:

- \`tests/test_config.py\`
- \`tests/test_api.py\`
- \`tests/test_artifacts.py\`
- \`tests/test_journal.py\`
- \`tests/test_pi_transport.py\`
- \`tests/test_cli_front_door.py\`

Do not move the entire 5,000-line CLI in one change. Each task must leave the
old command path testable.

### Task 0: Record the baseline and protect the working tree

**Files:**

- Test: current repository state only

- [ ] **Step 1: Record the existing diff and baseline**

Run:

    git status --short
    git diff --check
    uv run pytest -q
    uv run ruff check src tests

Expected: the commands report the current state without modifying any user
file. Record failures before implementation; do not attribute them to this
plan.

- [ ] **Step 2: Select the execution workspace**

If an isolated worktree is authorized, create it from the current branch and
carry only the intended user changes explicitly. If execution continues in the
current checkout, do not use broad git add -A or formatters that rewrite
unrelated files. Every commit in this plan must list exact paths and be checked
with git diff --cached before committing.

### Task 1: Add project configuration and profile resolution

**Files:**

- Create: \`src/reviewctl/config.py\`
- Create: \`src/reviewctl/errors.py\`
- Create: \`tests/test_config.py\`
- Create: \`examples/reviewctl.toml\`
- Modify: \`src/reviewctl/cli.py\` only where the new loader is imported

- [ ] **Step 1: Write failing configuration tests**

Add tests for:

    def test_project_config_wins_over_user_profile(tmp_path):
        user = tmp_path / "user.toml"
        project = tmp_path / "reviewctl.toml"
        user.write_text(
            '[profiles.default]\n'
            'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
        )
        project.write_text(
            '[project]\n'
            'visibility = "private"\n'
            'privacy_mode = "private"\n'
            '[profiles.default]\n'
            'routes = ["pi:openrouter/meta/muse-spark-1.2-contributor"]\n'
        )
        config = load_config(project, user_path=user)
        assert config.profile("default").routes == (
            "pi:openrouter/meta/muse-spark-1.2-contributor",
        )

    def test_sensitive_privacy_floor_cannot_be_weakened(tmp_path):
        project = tmp_path / "reviewctl.toml"
        project.write_text(
            '[project]\n'
            'privacy_mode = "sensitive"\n'
            '[profiles.default]\n'
            'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
            'execution = "remote"\n'
        )
        with pytest.raises(ConfigError, match="sensitive"):
            load_config(project, user_path=None)

    def test_user_stricter_privacy_cannot_be_weakened_by_project(tmp_path):
        user = tmp_path / "user.toml"
        project = tmp_path / "reviewctl.toml"
        user.write_text('[project]\nprivacy_mode = "sensitive"\n')
        project.write_text('[project]\nprivacy_mode = "personal"\n')
        config = load_config(project, user_path=user)
        assert config.project.privacy_mode == "sensitive"

    def test_invalid_route_is_rejected_at_load(tmp_path):
        project = tmp_path / "reviewctl.toml"
        project.write_text('[profiles.default]\nroutes = ["missing-colon"]\n')
        with pytest.raises(ConfigError, match="route"):
            load_config(project, user_path=None)

    def test_invalid_limits_and_privacy_mode_are_rejected_at_load(tmp_path):
        project = tmp_path / "reviewctl.toml"
        project.write_text(
            '[project]\nprivacy_mode = "unknown"\n'
            '[profiles.default]\n'
            'timeout_seconds = 0\n'
            'max_output_tokens = -1\n'
        )
        with pytest.raises(ConfigError):
            load_config(project, user_path=None)

    def test_missing_project_config_uses_safe_defaults(tmp_path):
        config = load_config(tmp_path / "missing.toml", user_path=None)
        assert config.project.privacy_mode == "private"
        assert config.profile("default").routes == ()

    def test_config_digest_changes_when_bytes_change(tmp_path):
        path = tmp_path / "reviewctl.toml"
        path.write_text('[project]\nprivacy_mode = "private"\n')
        first = load_config(path)
        path.write_text('[project]\nprivacy_mode = "personal"\n')
        second = load_config(path)
        assert first.digest != second.digest

Run:

    uv run pytest -q tests/test_config.py

Expected: FAIL because \`reviewctl.config\` and its public names do not exist.

- [ ] **Step 2: Implement the minimal typed configuration module**

Define these public types:

    class ConfigError(ValueError):
        pass

    @dataclass(frozen=True)
    class ProjectSettings:
        name: str
        visibility: str
        privacy_mode: str

    @dataclass(frozen=True)
    class ReviewProfile:
        name: str
        routes: tuple[str, ...]
        response_contract: str
        timeout_seconds: int
        max_attempts: int
        max_output_tokens: int | None
        execution: str
        tools: str

    @dataclass(frozen=True)
    class ReviewConfig:
        project: ProjectSettings
        profiles: Mapping[str, ReviewProfile]
        path: Path | None
        digest: str

        def profile(self, name: str) -> ReviewProfile:
            ...

    def load_config(
        project_path: Path,
        *,
        user_path: Path | None = DEFAULT_USER_CONFIG,
    ) -> ReviewConfig:
        ...

Use only \`tomllib\`, \`hashlib\`, and dataclasses. Accept \`personal\`,
\`private\`, and \`sensitive\`; reject unknown privacy modes, empty route
entries, malformed route strings, non-positive timeouts, negative output
limits, and malformed TOML during \`load_config\`. A sensitive project may
only use a profile with \`execution = \"local\"\`. A Pi route using an
OpenRouter model must declare \`execution = \"remote\"\`. Validate this at load
time so a loaded configuration is always internally valid; \`profile()\` is a
pure lookup.

Route parsing belongs in \`config.py\` and returns a typed
\`Route(transport, model)\`. Reject a missing separator, an empty transport,
an empty model, and an unknown transport name before execution.

The raw TOML byte digest is retained as provenance. It is not a semantic
configuration identity exposed in the user-facing API.

- [ ] **Step 3: Run focused tests**

Run:

    uv run pytest -q tests/test_config.py

Expected: PASS with all configuration tests passing.

- [ ] **Step 4: Add the public example**

Write \`examples/reviewctl.toml\` with a private project, a Pi profile using
the OpenRouter model IDs passed through Pi, a \`findings-json\` contract, one
attempt, and no tools. Do not include credentials or static provider prices.

- [ ] **Step 5: Commit only the configuration slice**

Run:

    git add src/reviewctl/config.py tests/test_config.py examples/reviewctl.toml
    git commit -m "feat: add project review configuration"

Expected: one commit containing only the new configuration slice.

### Task 2: Define the transport contract, public API, and engine seam

**Files:**

- Create: \`src/reviewctl/api.py\`
- Create: \`src/reviewctl/engine.py\`
- Create: \`tests/test_api.py\`
- Modify: \`src/reviewctl/backends.py\`
- Modify: \`src/reviewctl/__init__.py\`

- [ ] **Step 1: Write failing API tests**

Pin the existing provider-neutral types in \`backends.py\` before adding the
engine. The engine consumes \`BackendRequest\`, \`BackendExecution\`, and
\`BackendCapabilities\`; no API test may invent a second transport contract.
Use a fake transport instead of a provider process:

    class FakeTransport:
        def __init__(self, response=None):
            self.response = response or (
                '[{"severity":"minor","path":"a.py","message":"notice"}]'
            )

        def execute(self, request):
            return BackendExecution(
                exit_code=0,
                diagnostic=None,
                response=PersistedResponse(
                    conversation_id="fake-1",
                    cost_usd=0.0,
                    duration_ms=1,
                    input_tokens=1,
                    model="fake/model",
                    output_tokens=1,
                    provider="fake",
                    response=self.response,
                ),
                evidence=BackendEvidence(),
            )

    def test_client_review_returns_typed_result(tmp_path):
        write_default_config(tmp_path)
        client = ReviewClient.from_project(
            tmp_path,
            transports={"fake": FakeTransport()},
        )
        result = client.review(
            ReviewRequest(prompt="review", files=(tmp_path / "a.py",))
        )
        assert result.status == "accepted"
        assert result.findings[0].path == "a.py"

    def test_client_does_not_require_cli_parser(tmp_path):
        write_default_config(tmp_path)
        client = ReviewClient.from_project(tmp_path, transports={"fake": FakeTransport()})
        assert client.config.project.privacy_mode == "private"

    def test_malformed_findings_payload_is_contract_failed(tmp_path):
        write_default_config(tmp_path)
        client = ReviewClient.from_project(
            tmp_path,
            transports={"fake": FakeTransport(response="not-json")},
        )
        result = client.review(ReviewRequest(prompt="review"))
        assert result.status == "contract_failed"
        assert result.diagnostic.code == "contract_failed"

Run:

    uv run pytest -q tests/test_api.py

Expected: FAIL because the public API and engine do not exist.

- [ ] **Step 2: Implement the small API**

Define:

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

    class ReviewClient:
        @classmethod
        def from_project(cls, project_dir, *, transports=None): ...

        def review(self, request: ReviewRequest) -> ReviewResult: ...
        def findings(self, *, status=None): ...
        def journal(self): ...

    def verify_receipt(receipt_path: Path) -> VerificationResult: ...

The API delegates to \`ReviewEngine\`; it does not parse arguments, read
environment variables, or invoke Pi directly. Keep the fake transport test
working through the same transport interface used by Pi. Contract decoding
belongs to the existing \`contracts.py\` module; \`engine.py\` only coordinates
it and maps the result to a stable outcome.

Define a \`Diagnostic\` dataclass in \`errors.py\` with \`code\`, \`message\`,
\`retryable\`, \`next\`, and safe \`artifacts\` fields. Change
\`BackendExecution.diagnostic\` from a string to \`Diagnostic | None\` while
preserving a compatibility serialization for existing receipts.

Define these stable error codes in \`errors.py\` and use them in API results and
CLI JSON errors:

    invalid_request
    config_invalid
    route_invalid
    privacy_denied
    transport_unavailable
    timeout
    empty_response
    contract_failed
    receipt_invalid

Map \`invalid_request\`, \`config_invalid\`, and \`route_invalid\` to exit
code 2; \`privacy_denied\` to 4; \`transport_unavailable\`, \`timeout\`,
\`empty_response\`, and \`contract_failed\` to 3; and \`receipt_invalid\` to 5.
Findings do not alter the review status; \`--fail-on\` is a separate CLI
policy.

- [ ] **Step 3: Run focused tests and Ruff**

Run:

    uv run pytest -q tests/test_api.py tests/test_config.py
    uv run ruff check src/reviewctl/api.py src/reviewctl/engine.py src/reviewctl/config.py tests/test_api.py tests/test_config.py

Expected: both commands exit 0.

- [ ] **Step 4: Export only stable names**

Update \`src/reviewctl/__init__.py\` to export \`ReviewClient\`,
\`ReviewRequest\`, \`ReviewResult\`, and the package version. Do not export
parser helpers or provider-specific functions.

- [ ] **Step 5: Commit the API seam**

Run:

    git add src/reviewctl/api.py src/reviewctl/engine.py src/reviewctl/__init__.py tests/test_api.py
    git commit -m "feat: add public review client API"

### Task 3: Centralize private artifacts and journal events

**Files:**

- Create: \`src/reviewctl/artifacts.py\`
- Create: \`src/reviewctl/journal.py\`
- Create: \`tests/test_artifacts.py\`
- Create: \`tests/test_journal.py\`

- [ ] **Step 1: Write failing artifact and journal tests**

Test that:

    def test_sensitive_artifact_is_private(tmp_path):
        artifact = ArtifactStore(tmp_path / "review")
        path = artifact.write_text("request.json", "private prompt")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_journal_appends_and_reads_events(tmp_path):
        journal = ProjectJournal(tmp_path / "journal.jsonl")
        journal.append({"type": "review_started", "reviewId": "r1"})
        journal.append({"type": "finding", "reviewId": "r1", "status": "open"})
        assert [event["type"] for event in journal.events()] == [
            "review_started",
            "finding",
        ]

    def test_journal_never_rewrites_previous_events(tmp_path):
        journal = ProjectJournal(tmp_path / "journal.jsonl")
        journal.append({"type": "review_started", "reviewId": "r1"})
        before = journal.path.read_bytes()
        journal.append({"type": "review_finished", "reviewId": "r1"})
        assert journal.path.read_bytes().startswith(before)

    def test_artifact_rejects_path_traversal(tmp_path):
        artifact = ArtifactStore(tmp_path / "review")
        with pytest.raises(ValueError, match="path"):
            artifact.write_text("../escape", "not allowed")

    def test_truncated_journal_line_is_reported(tmp_path):
        path = tmp_path / "journal.jsonl"
        path.write_text('{"type":"review_started"}\n{"type":')
        journal = ProjectJournal(path)
        events, diagnostic = journal.read_with_diagnostic()
        assert len(events) == 1
        assert diagnostic.code == "journal_corrupt"

Run:

    uv run pytest -q tests/test_artifacts.py tests/test_journal.py

Expected: FAIL because the modules do not exist.

- [ ] **Step 2: Implement the private artifact writer**

Implement \`ArtifactStore.write_bytes(name, data)\` and
\`ArtifactStore.write_text(name, text)\`. Create parent directories with
private mode, open new files using \`os.open\` with \`O_CREAT | O_EXCL\` and
mode \`0o600\`, then call \`os.fchmod\` so the result is independent of the
process umask. Reject path traversal where the resolved target escapes the
store root. Receipts may later be copied to a user-selected public output path
explicitly; raw artifacts never use that path implicitly.

- [ ] **Step 3: Implement the append-only journal**

Implement \`ProjectJournal.append(event)\`, \`events()\`,
\`read_with_diagnostic()\`, and \`findings(status=None)\`. Each event must
contain a type, event ID, timestamp, and review ID. Appending must use one JSON
object per line and preserve existing bytes. Invalid or truncated lines are
reported as a diagnostic rather than silently discarded. Concurrent writers
are unsupported in this slice and documented as such; the journal does not
pretend to serialize cross-process appends.

- [ ] **Step 4: Run tests and commit**

Run:

    uv run pytest -q tests/test_artifacts.py tests/test_journal.py
    uv run ruff check src/reviewctl/artifacts.py src/reviewctl/journal.py tests/test_artifacts.py tests/test_journal.py

Expected: PASS and no Ruff findings.

Commit:

    git add src/reviewctl/artifacts.py src/reviewctl/journal.py tests/test_artifacts.py tests/test_journal.py
    git commit -m "feat: add private artifacts and project journal"

### Task 4: Make Pi a first-class transport

**Files:**

- Create: \`src/reviewctl/pi_transport.py\`
- Create: \`tests/test_pi_transport.py\`
- Modify: \`src/reviewctl/backends.py\`
- Modify: \`src/reviewctl/engine.py\`

- [ ] **Step 1: Write failing Pi adapter tests**

Mock the process runner and test the observed contract:

    def test_pi_request_uses_exact_model_and_no_tools_by_default(tmp_path, fake_runner):
        transport = PiTransport(run_process=fake_runner)
        execution = transport.execute(
            backend_request(
                model="openrouter/stealth/ox-alpha",
                prompt="private prompt",
                tools="none",
            )
        )
        command = fake_runner.last_command
        assert "--model" in command
        assert "openrouter/stealth/ox-alpha" in command
        assert "--no-tools" in command
        assert "private prompt" not in command
        assert fake_runner.last_stdin == "private prompt"
        assert execution.response.response

    def test_pi_empty_response_preserves_usage_and_diagnostic(tmp_path, fake_runner):
        fake_runner.stdout = agent_end_with_empty_content(input_tokens=10, output_tokens=8000)
        execution = PiTransport(run_process=fake_runner).execute(
            backend_request(model="openrouter/meta/muse-spark-1.2-contributor")
        )
        assert execution.response is None
        assert execution.diagnostic.code == "empty_response"
        assert execution.evidence.response is not None

    def test_pi_timeout_is_distinct_from_empty_response(tmp_path, fake_runner):
        fake_runner.timed_out = True
        execution = PiTransport(run_process=fake_runner).execute(
            backend_request(model="openrouter/stealth/ox-alpha")
        )
        assert execution.response is None
        assert execution.diagnostic.code == "timeout"

    def test_pi_transport_does_not_claim_unenforced_token_cap():
        capabilities = PiTransport.capabilities()
        assert capabilities.output_token_limit_enforced is False

Run:

    uv run pytest -q tests/test_pi_transport.py

Expected: FAIL until the adapter is separated from the CLI implementation.

- [ ] **Step 2: Move the Pi invocation behind the adapter**

Use the existing Pi JSON-mode behavior, but keep the adapter interface small:

    class PiTransport:
        @classmethod
        def capabilities(cls) -> BackendCapabilities: ...
        def execute(self, request: BackendRequest) -> BackendExecution: ...

The adapter must:

- use the exact requested model string;
- create a disposable session per attempt by passing the attempt-local session
  path explicitly to Pi's JSON-mode command;
- disable tools for the formal default;
- apply the process timeout;
- persist only observed event, session, stderr, and final-response artifacts;
- extract final text from Pi message events;
- distinguish empty text from transport failure;
- retain observed usage even when no usable final text exists;
- record that Pi's requested token cap is not enforced by the process;
- never pass source or prompts in a command argument when stdin is available.

Extend the provider-neutral capability/request types with typed fields
\`output_token_limit_enforced: bool\` and \`tools: str\`. The Pi adapter sets
the former to false and the latter to the actual requested mode; it never
fabricates enforcement.

Keep the existing compatibility \`invoke_pi\` behavior until all old tests pass;
the new adapter may delegate to shared process helpers during migration.

- [ ] **Step 3: Register Pi without duplicating provider logic**

Register \`pi\` in the existing backend registry. The registry must know only
the transport name and capabilities; model/provider selection remains in the
route string and Pi.

- [ ] **Step 4: Run focused and regression tests**

Run:

    uv run pytest -q tests/test_pi_transport.py tests/test_backends.py tests/test_run.py

Expected: PASS. If an existing test encodes the old empty-response behavior,
update only that test's expectation to the documented outcome and preserve the
receipt field that explains the change.

- [ ] **Step 5: Commit the Pi transport**

Run:

    git add src/reviewctl/pi_transport.py src/reviewctl/backends.py src/reviewctl/engine.py tests/test_pi_transport.py
    git commit -m "feat: make pi a first class review transport"

### Task 5: Add the thin CLI front door

**Files:**

- Create: \`tests/test_cli_front_door.py\`
- Modify: \`src/reviewctl/cli.py\`
- Modify: \`README.md\`
- Modify: \`docs/PI-INTEGRATION.md\`
- Modify: \`docs/HELP-LLM.md\`

- [ ] **Step 1: Write failing CLI tests**

Test parser delegation with a fake client:

    def test_review_command_uses_project_profile(tmp_path, monkeypatch, capsys):
        write_default_config(tmp_path)
        calls = []

        class FakeClient:
            @classmethod
            def from_project(cls, path):
                return cls()

            def review(self, request):
                calls.append(request)
                return fake_result(status="accepted")

        monkeypatch.setattr(cli, "ReviewClient", FakeClient)
        assert cli.main(["review", "--project", str(tmp_path), "--prompt", "review"]) == 0
        assert calls[0].profile == "default"
        assert "accepted" in capsys.readouterr().out

    def test_json_error_has_stable_code(tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "ReviewClient", failing_client("privacy_denied"))
        assert cli.main(["review", "--project", str(tmp_path), "--format", "json"]) == 4
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "privacy_denied"

    def test_init_creates_idempotent_private_project_config(tmp_path):
        assert cli.main(["init", "--project", str(tmp_path), "--mode", "private"]) == 0
        first = (tmp_path / "reviewctl.toml").read_bytes()
        assert cli.main(["init", "--project", str(tmp_path), "--mode", "private"]) == 0
        assert (tmp_path / "reviewctl.toml").read_bytes() == first

    def test_doctor_does_not_print_credentials_or_source(tmp_path, monkeypatch, capsys):
        write_default_config(tmp_path)
        monkeypatch.setenv("OPENROUTER_API_KEY", "secret-do-not-print")
        assert cli.main(["doctor", "--project", str(tmp_path)]) == 0
        output = capsys.readouterr().out
        assert "secret-do-not-print" not in output
        assert "source contents" not in output

Run:

    uv run pytest -q tests/test_cli_front_door.py

Expected: FAIL because the new commands do not exist.

- [ ] **Step 2: Add thin command handlers**

Add \`init\`, \`review\`, \`status\`, \`findings\`, and \`doctor\` parsers. Each
handler converts argparse values to a public API call and formats the result.
No handler may parse TOML, invoke a subprocess, create receipt directories, or
evaluate a contract directly.

The first vertical acceptance path is:

    reviewctl init --mode private
    reviewctl review --profile pi-review --prompt "review" --file src/example.py
    reviewctl findings --status open

The init command is idempotent and refuses to overwrite an existing project
configuration unless the user passes an explicit replacement option. The
review command reads prompt-file content in the CLI, then passes a fully
materialized ReviewRequest to the API. Prompt text and source bytes never enter
the parser's diagnostic output.

The \`review\` command accepts:

    reviewctl review --project PATH --profile NAME --prompt TEXT --file PATH

It also accepts \`--prompt-file\`, repeated \`--file\`, \`--format\`, and
\`--fail-on\`. The existing \`run\` parser and handler remain available.

- [ ] **Step 3: Add LLM-safe errors and doctor output**

Implement stable JSON errors with the codes in the product specification.
\`doctor\` must show the selected profile, transport, model identity, privacy
mode, tools policy, timeout, token-cap enforcement, and artifact root without
printing credentials or source. It must report whether the configured route is
local or remote and whether Pi's output token limit is enforced.

- [ ] **Step 4: Update user-facing documentation**

Make the new path primary:

    reviewctl init --mode private
    reviewctl review --profile pi-review --prompt-file review.md --file src/example.py
    reviewctl findings --status open

Keep a short compatibility section explaining that \`run\` remains supported
while callers migrate.

- [ ] **Step 5: Run CLI and regression verification**

Run:

    uv run pytest -q tests/test_cli_front_door.py tests/test_run.py tests/test_public_distribution.py
    uv run ruff check src tests

Expected: PASS with no public-documentation test regressions.

- [ ] **Step 6: Commit the CLI front door**

Run:

    git add src/reviewctl/cli.py tests/test_cli_front_door.py README.md docs/PI-INTEGRATION.md docs/HELP-LLM.md
    git commit -m "feat: add configuration-first review CLI"

### Task 6: End-to-end local verification and handoff

**Files:**

- Modify: \`docs/HANDOFF.md\`
- Modify: \`docs/ARCHITECTURE.md\`
- Modify: \`docs/PI-INTEGRATION.md\`
- Test: all existing tests

- [ ] **Step 1: Run the complete local verification**

Run:

    git diff --check
    uv run ruff check src tests
    uv run pytest -q

Expected: all commands exit 0; pytest prints a complete summary with zero
failures. If the suite is interrupted before its summary, do not report it as
passing.

- [ ] **Step 2: Exercise a synthetic Pi-shaped review**

Run the fake transport integration path with a temporary project and confirm:

- configuration digest appears in the receipt;
- packet digest and selected files are recorded;
- result status and findings are present;
- journal has review start and completion events;
- \`reviewctl verify\` accepts the receipt;
- raw artifacts are mode \`0600\`.

Do not send repository source to an external model for this verification.

- [ ] **Step 3: Update the handoff**

Document the new public commands, Python API, configuration precedence, Pi
transport limitations, compatibility status of \`run\`, and the exact
verification commands. State GitHub integration as the next separate slice,
not as a completed capability.

- [ ] **Step 4: Commit the verified handoff**

Run:

    git add docs/HANDOFF.md docs/ARCHITECTURE.md docs/PI-INTEGRATION.md
    git commit -m "docs: hand off configuration-first review tool"

## Follow-up slices after the first implementation

These are intentionally not part of the first implementation plan:

1. GitHub Action that creates a bounded \`ReviewRequest\` and publishes a
   \`ReviewResult\`.
2. Pi skill or extension that invokes the same local API without duplicating
   transport logic.
3. Cursor, Kiro, Codex, and other harness integrations through the filesystem
   journal protocol.
4. Signed sanitized journal bundles for multi-machine collaboration.
5. Optional hosted or static runners that do not become canonical storage.

Each follow-up needs its own design and focused verification rather than being
added to the first CLI/API refactor.
