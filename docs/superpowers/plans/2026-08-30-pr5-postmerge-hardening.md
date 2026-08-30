# PR #5 Post-Merge Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove durable project-review source copies, prevent project checkpoints from being verified as canonical receipts, make generated projects inert, and make Antigravity reject malformed present structured output.

**Architecture:** Keep the project API and canonical runner separate. The project API stages frozen source bytes in a fresh system temporary directory per transport attempt and emits an explicitly marked compatibility checkpoint; global verification only rejects project-shaped artifacts and never dispatches to the project digest checker. Project initialization has no route, while Antigravity distinguishes an absent legacy field from a present field with the wrong JSON type.

**Tech Stack:** Python 3.14, pytest, Ruff, stdlib `tempfile`, existing `ArtifactStore`, existing receipt V1/V2 validators.

---

## File map

- `src/reviewctl/api.py`: project review orchestration, temporary source snapshots, checkpoint marker, internal checkpoint digest verification.
- `src/reviewctl/cli.py`: global receipt classification and Antigravity response selection.
- `src/reviewctl/project_cli.py`: roster-free, local-only generated project template.
- `tests/test_api.py`: project lifecycle, source snapshot, fallback, cleanup, and direct-checkpoint tests.
- `tests/test_run.py`: global receipt routing and Antigravity transport tests.
- `tests/test_cli_front_door.py`: generated configuration and inert review behavior.
- `docs/EVIDENCE.md`: receipt/checkpoint authority and temporary-source guarantees.
- `docs/PROJECT-INTEGRATION.md`: explicit route-configuration step after inert initialization.

### Task 1: Generate an inert project and preserve the early refusal

**Files:**
- Modify: `src/reviewctl/project_cli.py:42-58,238-244`
- Modify: `tests/test_cli_front_door.py:1080-1110`
- Modify: `docs/PROJECT-INTEGRATION.md`

- [x] **Step 1: Replace the old init expectation with failing private/sensitive tests**

```python
@pytest.mark.parametrize("mode", ["private", "sensitive"])
def test_init_generates_an_inert_roster_free_project(
    mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(["init", "--project", str(tmp_path), "--mode", mode]) == 0
    contents = (tmp_path / "reviewctl.toml").read_text()
    profile = load_config(tmp_path, user_path=None).profile("default")
    assert profile.routes == ()
    assert profile.execution == "local"
    for forbidden in ("openrouter", "ox-alpha", "pi:", 'execution = "remote"'):
        assert forbidden not in contents


def test_fresh_inert_project_refuses_without_attempt_or_transport(tmp_path: Path) -> None:
    assert project_cli.init_project(
        SimpleNamespace(project=str(tmp_path), mode="private", force=False)
    ) == 0

    class UnreachableTransport:
        def execute(self, request: BackendRequest) -> BackendExecution:
            raise AssertionError("an empty route must not invoke a transport")

    client = ReviewClient.from_project(tmp_path, transports={"pi": UnreachableTransport()})
    before = tuple(client.journal().events())
    result = client.review(ReviewRequest(prompt="review"))
    assert result.status == "route_invalid"
    assert result.receipt_path == Path()
    assert tuple(client.journal().events()) == before
    assert not (tmp_path / ".reviewctl" / "reviews").exists()
```

- [x] **Step 2: Run the focused tests and confirm RED**

```bash
uv run --python 3.14 pytest -q \
  tests/test_cli_front_door.py::test_init_generates_an_inert_roster_free_project \
  tests/test_cli_front_door.py::test_fresh_inert_project_refuses_without_attempt_or_transport
```

Expected: the init test fails because the private template still names Ox and uses remote execution; the existing early refusal remains green.

- [x] **Step 3: Make the template unconditionally inert**

```toml
[profiles.default]
routes = []
dimensions = ["correctness"]
response_contract = "findings-json"
execution = "local"
tools = "none"
timeout_seconds = 300
max_output_tokens = 8000
```

Delete the sensitive-mode string replacement; mode now changes only `privacy_mode`.

- [x] **Step 4: Document and verify the boundary**

Add to `docs/PROJECT-INTEGRATION.md`: initialization is deliberately inert; organization policy or an explicit project-local edit supplies a route, and the template is not an operating roster. Then run:

```bash
uv run --python 3.14 pytest -q tests/test_cli_front_door.py -k 'init or inert'
uv run --python 3.14 ruff check src/reviewctl/project_cli.py tests/test_cli_front_door.py
uv run --python 3.14 ruff format --check src/reviewctl/project_cli.py tests/test_cli_front_door.py
git diff --check
git add src/reviewctl/project_cli.py tests/test_cli_front_door.py docs/PROJECT-INTEGRATION.md
git commit -m "fix: make project initialization inert"
```

### Task 2: Stage frozen source bytes only for one transport attempt

**Files:**
- Modify: `src/reviewctl/api.py:1-20,600-680`
- Modify: `tests/test_api.py:1360-1495`

- [ ] **Step 1: Strengthen the fallback test before production changes**

Extend `test_client_freezes_source_bytes_across_fallback_attempts` with:

```python
staged_paths: list[Path] = []
seen_roots: list[tuple[Path, ...]] = []

# Inside the fake transport:
staged_paths.append(request.files[0])
seen_roots.append(request.source_roots)

# After the accepted result:
assert seen == [initial, initial]
assert staged_paths[0] != staged_paths[1]
assert all(not path.exists() for path in staged_paths)
assert all(roots[0] == tmp_path.resolve() for roots in seen_roots)
assert all(path.parent == roots[-1] for path, roots in zip(staged_paths, seen_roots, strict=True))
assert not list((tmp_path / ".reviewctl" / "reviews").glob("**/source"))
```

- [ ] **Step 2: Add unexpected-exception and cleanup-failure pressure**

```python
def test_client_cleans_snapshot_before_unexpected_transport_error(tmp_path: Path) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("secret = 1\n")
    observed: list[Path] = []

    class ExplodingTransport:
        def execute(self, request: BackendRequest) -> BackendExecution:
            observed.extend(request.files)
            raise RuntimeError("unexpected")

    client = ReviewClient.from_project(tmp_path, transports={"pi": ExplodingTransport()})
    with pytest.raises(RuntimeError, match="unexpected"):
        client.review(ReviewRequest(prompt="review", files=(source,)))
    assert observed and all(not path.exists() for path in observed)
    assert not list((tmp_path / ".reviewctl").glob("**/source"))


def test_client_never_accepts_when_snapshot_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_default_config(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("secret = 1\n")
    external = tmp_path.parent / f"{tmp_path.name}-undeletable"

    class CleanupFailure:
        def __init__(self, **kwargs: object) -> None:
            external.mkdir()
        def __enter__(self) -> str:
            return str(external)
        def __exit__(self, *args: object) -> None:
            raise OSError("cleanup refused")

    monkeypatch.setattr(api_module.tempfile, "TemporaryDirectory", CleanupFailure)
    client = ReviewClient.from_project(tmp_path, transports={"pi": FakeTransport()})
    result = client.review(ReviewRequest(prompt="review", files=(source,)))
    assert result.status == "transport_unavailable"
    assert not list((tmp_path / ".reviewctl").glob("**/source"))
    assert str(external) not in result.receipt_path.read_text()
```

- [ ] **Step 3: Run the focused tests and confirm RED**

```bash
uv run --python 3.14 pytest -q \
  tests/test_api.py::test_client_freezes_source_bytes_across_fallback_attempts \
  tests/test_api.py::test_client_cleans_snapshot_before_unexpected_transport_error \
  tests/test_api.py::test_client_never_accepts_when_snapshot_cleanup_fails
```

Expected: durable `attempt-XX/source` paths fail lifecycle assertions and `api_module.tempfile` is absent.

- [ ] **Step 4: Wrap each transport call in a new system temporary directory**

Import `tempfile`. Replace original-file passthrough and durable attempt-source creation with:

```python
try:
    with tempfile.TemporaryDirectory(prefix="reviewctl-project-source-") as directory:
        temporary_root = Path(directory)
        source_artifacts = ArtifactStore(temporary_root)
        transport_files_tuple = tuple(
            source_artifacts.write_bytes(name, contents)
            for name, contents in zip(source_names, source_contents, strict=True)
        )
        transport_source_roots = (self.project_dir,)
        if source_root != self.project_dir.resolve():
            transport_source_roots += (source_root,)
        transport_source_roots += (temporary_root,)
        backend_request = BackendRequest(
            prompt=prompt,
            model=route.model,
            response_contract=profile.response_contract,
            files=transport_files_tuple,
            attempt_dir=attempt_artifacts.root,
            timeout_seconds=profile.timeout_seconds,
            max_output_tokens=profile.max_output_tokens or 0,
            source_class=self.config.project.privacy_mode,
            source_roots=transport_source_roots,
            provider_preferences=None,
            tools=profile.tools,
        )
        execution = transport.execute(backend_request)
except (OSError, UnicodeError, ValueError):
    diagnostic = Diagnostic(
        "transport_unavailable", "review transport failed", retryable=True
    )
    record_attempt({"attempt": index, "route": route_label, "status": diagnostic.code})
    last_diagnostic = diagnostic
    if index < len(routes):
        fallback_relationships.append(
            {
                "from": route_label,
                "to": f"{routes[index].transport}:{routes[index].model}",
                "reason": diagnostic.code,
            }
        )
    continue
```

Keep response persistence and contract evaluation after successful context exit. Never persist the temporary path.

- [ ] **Step 5: Verify and commit**

```bash
uv run --python 3.14 pytest -q tests/test_api.py -k 'source or transport or fallback or cleanup'
uv run --python 3.14 ruff check src/reviewctl/api.py tests/test_api.py
uv run --python 3.14 ruff format --check src/reviewctl/api.py tests/test_api.py
git diff --check
git add src/reviewctl/api.py tests/test_api.py
git commit -m "fix: limit project source snapshots to transport lifetime"
```

### Task 3: Demote project checkpoints and remove weak global dispatch

**Files:**
- Modify: `src/reviewctl/api.py:900-1010`
- Modify: `src/reviewctl/cli.py:5720-5810`
- Modify: `tests/test_api.py:370-490,1450-1605`
- Modify: `tests/test_run.py:7600-7710,1140-1160,12835-12860`
- Modify: `tests/test_cli_front_door.py:1130-1160`
- Modify: `docs/EVIDENCE.md`

- [ ] **Step 1: Specify the marker and internal verifier**

```python
assert receipt["artifactKind"] == "project-review-checkpoint"
assert receipt["projectCheckpointSchemaVersion"] == 1
assert verify_project_receipt(
    result.receipt_path, expected_sha256=result.receipt_sha256
) is None

receipt["artifactKind"] = "canonical-review-receipt"
receipt.pop("sha256")
receipt["sha256"] = api_module._digest(
    json.dumps(
        receipt,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
)
assert verify_project_receipt(path).code == "receipt_invalid"
```

Keep a copied historical unmarked project checkpoint and assert direct digest inspection succeeds.

- [ ] **Step 2: Specify rejection-only global classification**

```python
PROJECT_ONLY_FIELDS = {
    "projectId": "project-1",
    "originId": "origin-1",
    "journalSequence": 1,
    "privacyMode": "private",
    "dimensionCoverage": {},
    "fallbackRelationships": [],
}

@pytest.mark.parametrize(("field", "value"), PROJECT_ONLY_FIELDS.items())
def test_verify_rejects_historical_project_checkpoint_shape(
    field: str, value: object, tmp_path: Path
) -> None:
    receipt = {
        "reviewId": "r",
        "configDigest": "c",
        field: value,
        "status": "accepted",
    }
    receipt["sha256"] = cli.sha256_bytes(cli.canonical_json(receipt))
    path = tmp_path / "checkpoint.json"
    path.write_bytes(cli.canonical_json(receipt) + b"\n")
    verified = run_cli("verify", str(path))
    assert verified.returncode == 5
    assert json.loads(verified.stdout)["violations"] == [
        "project-checkpoint-not-review-receipt"
    ]
```

For each representative checkpoint, add `result`, `receiptSchemaVersion`, and an unrelated key in isolation; rejection must be unchanged. Preserve a generic V1 fixture augmented only with `configDigest` as a passing control, plus existing V2 controls.

- [ ] **Step 3: Run the receipt tests and confirm RED**

```bash
uv run --python 3.14 pytest -q tests/test_api.py -k receipt
uv run --python 3.14 pytest -q tests/test_run.py -k 'receipt and (v1 or v2 or project)'
uv run --python 3.14 pytest -q tests/test_cli_front_door.py -k verify
```

Expected: marker assertions fail and global verify still accepts a self-consistent project digest.

- [ ] **Step 4: Mark checkpoints and add a rejection-only classifier**

Add marker fields in `_write_receipt`. In `verify_project_receipt`, when either marker field is present require exactly `artifactKind == "project-review-checkpoint"` and integer schema version `1`; otherwise retain direct legacy digest inspection.

In `cli.py` add:

```python
PROJECT_CHECKPOINT_KIND = "project-review-checkpoint"
PROJECT_CHECKPOINT_FIELDS = frozenset(
    {
        "projectId",
        "originId",
        "journalSequence",
        "privacyMode",
        "dimensionCoverage",
        "fallbackRelationships",
    }
)

def project_checkpoint_shape(value: object) -> bool:
    if type(value) is not dict:
        return False
    if value.get("artifactKind") == PROJECT_CHECKPOINT_KIND:
        return True
    if "projectCheckpointSchemaVersion" in value:
        return True
    return "configDigest" in value and bool(PROJECT_CHECKPOINT_FIELDS.intersection(value))
```

Call it before V1/V2 validation and emit `project-checkpoint-not-review-receipt`. Delete global delegation to `verify_project_receipt`.

- [ ] **Step 5: Correct evidence documentation**

Document in `docs/EVIDENCE.md` that project `receipt.json` is a compatibility checkpoint, `verify_project_receipt` checks internal digest consistency only, global `reviewctl verify` rejects recognizable checkpoints, and legacy V1 cannot authenticate a fully rewritten document.

- [ ] **Step 6: Verify, commit, and request a bounded security review**

```bash
uv run --python 3.14 pytest -q tests/test_api.py -k receipt
uv run --python 3.14 pytest -q tests/test_run.py -k 'receipt or verify'
uv run --python 3.14 pytest -q tests/test_cli_front_door.py -k verify
uv run --python 3.14 ruff check src/reviewctl/api.py src/reviewctl/cli.py tests/test_api.py tests/test_run.py tests/test_cli_front_door.py
uv run --python 3.14 ruff format --check src/reviewctl/api.py src/reviewctl/cli.py tests/test_api.py tests/test_run.py tests/test_cli_front_door.py
git diff --check
git add src/reviewctl/api.py src/reviewctl/cli.py tests/test_api.py tests/test_run.py tests/test_cli_front_door.py docs/EVIDENCE.md
git commit -m "fix: reject project checkpoints as review receipts"
```

Run a commit-bound `reviewctl` security/spec review over no more than three bounded source/test files. Verify its receipt and independently reproduce every material finding.

### Task 4: Fail closed on malformed present Antigravity structured output

**Files:**
- Modify: `src/reviewctl/cli.py:3135-3200`
- Modify: `tests/test_run.py:10220-10420`

- [ ] **Step 1: Add a type matrix plus absence/object controls**

```python
@pytest.mark.parametrize("structured_output", [None, "text", 7, [], True])
def test_invoke_agy_rejects_present_non_object_structured_output(
    structured_output: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "conversation_id": "agy-conversation",
        "status": "SUCCESS",
        "response": '{"verdict":"approved","findings":[]}',
        "structured_output": structured_output,
    }
    monkeypatch.setenv("AGY_RAW_PAYLOAD", json.dumps(payload))
    fake_agy = write_fake_agy(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    exit_code, error, response = cli.invoke_agy(
        agy_bin=str(fake_agy),
        prompt="Review synthetic source.",
        model="gemini-3.7-flash-high",
        files=[source],
        max_output_tokens=1,
        response_contract="findings-json",
        timeout_seconds=7,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )
    assert exit_code == 502
    assert error == "agy returned invalid structured output"
    assert response.response == ""
```

Add separate controls: absent field uses legacy `response`; valid object is canonical. Retain duplicate-key, non-finite, and large sandbox-file packet tests. A runner-level malformed contract object must become `contract_failed`, not accepted.

- [ ] **Step 2: Run the matrix and confirm RED**

```bash
uv run --python 3.14 pytest -q tests/test_run.py -k 'invoke_agy and structured_output'
```

Expected: present non-dict values fall back to `response` and fail the new assertions.

- [ ] **Step 3: Distinguish absence from present invalid value**

```python
if "structured_output" not in payload:
    response = payload.get("response")
else:
    structured_output = payload["structured_output"]
    if not isinstance(structured_output, dict):
        return 502, "agy returned invalid structured output", blank
    response = canonical_json(structured_output).decode()
```

Do not alter the parser, raw response write, sandbox-file path, or contract evaluator.

- [ ] **Step 4: Verify and commit**

```bash
uv run --python 3.14 pytest -q tests/test_run.py -k agy
uv run --python 3.14 ruff check src/reviewctl/cli.py tests/test_run.py
uv run --python 3.14 ruff format --check src/reviewctl/cli.py tests/test_run.py
git diff --check
git add src/reviewctl/cli.py tests/test_run.py
git commit -m "fix: reject malformed Antigravity structured output"
```

### Task 5: Final evidence, package verification, and PR gate

**Files:**
- Modify only if verification exposes a mismatch: `README.md`, `docs/EVIDENCE.md`, `docs/PROJECT-INTEGRATION.md`

- [ ] **Step 1: Run the exact-candidate local gate**

```bash
uv run --python 3.14 pytest -q
uv run --python 3.14 ruff check .
uv run --python 3.14 ruff format --check .
uv build
git diff --check
git status --short
```

Expected: full suite reaches 100% with no failures; Ruff and build pass; tracked worktree is clean.

- [ ] **Step 2: Test the built wheel in a fresh temporary environment**

Create a temporary virtual environment, install only the wheel, run `reviewctl --help`, initialize a temporary project, assert `routes = []` and `execution = "local"`, verify canonical V1/V2 fixtures, and verify rejection of a marked checkpoint. Remove the environment afterward.

- [ ] **Step 3: Run final exact-commit review packets**

Use `reviewctl`, maximum three files per packet:

- security/evidence: project API, global verifier, focused source/receipt tests;
- configuration/compatibility: project CLI, front-door tests, integration documentation;
- transport: Antigravity implementation, focused tests, design.

Every accepted receipt must pass `reviewctl verify`. Unavailable reviewers remain unavailable. Reproduce every finding on the exact candidate SHA.

- [ ] **Step 4: Push and create or update the PR without merging**

```bash
git push -u origin codex/pr5-postmerge-hardening
gh pr create --base main --head codex/pr5-postmerge-hardening \
  --title "fix: harden review evidence and transport boundaries" \
  --body-file /tmp/reviewctl-pr5-hardening-pr.md
```

Record the exact SHA, local commands, verified receipt paths, and unavailable attempts. Wait for green GitHub CI and clean substantive review on that exact SHA before offering merge; a changed head restarts the gate.
