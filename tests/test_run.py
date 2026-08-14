from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from reviewctl import cli, review_flow
from reviewctl.setup import BackendInstallation, LocalExecutionTopology

REPOSITORY = Path(__file__).parents[1]
V1_RECEIPT_FIXTURES = REPOSITORY / "tests" / "fixtures" / "receipts"


def run_cli(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "reviewctl", *arguments],
        cwd=REPOSITORY,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def write_fake_python_executable(path: Path, name: str, source: str) -> Path:
    """Create a coverage-neutral stdlib fake without disabling CLI coverage."""
    executable = path / name
    payload = path / f".{name}.py"
    payload.write_text(source)
    executable.write_text(
        "#!/bin/sh\n"
        "unset COVERAGE_PROCESS_CONFIG COVERAGE_PROCESS_START COV_CORE_SOURCE COV_CORE_CONFIG\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(payload))} "$@"\n'
    )
    executable.chmod(0o755)
    return executable


def write_fake_llm(path: Path) -> Path:
    return write_fake_python_executable(
        path,
        "llm",
        """import json
import os
import sqlite3
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
database = Path(arguments[arguments.index('-d') + 1])
model = arguments[arguments.index('-m') + 1]
database.parent.mkdir(parents=True, exist_ok=True)
connection = sqlite3.connect(database)
connection.execute(
    'CREATE TABLE responses ('
    'id INTEGER PRIMARY KEY, response TEXT, conversation_id TEXT, model TEXT, '
    'input_tokens INTEGER, output_tokens INTEGER)'
)
if model == 'timeout':
    child = database.with_suffix('.child')
    child.write_text(str(__import__('os').getpid()))
    time.sleep(60)
elif model == 'empty':
    connection.execute("INSERT INTO responses VALUES (1, '', 'empty-conversation', 'empty', 1, 1)")
elif model == 'wrong-model':
    connection.execute(
        "INSERT INTO responses VALUES "
        "(1, 'VERDICT: approved', 'wrong-conversation', 'other-model', 1, 1)"
    )
elif model == 'missing':
    pass
elif model == 'no-conversation':
    connection.execute(
        "INSERT INTO responses VALUES "
        "(1, 'VERDICT: approved.', '', 'no-conversation', 1, 1)"
    )
elif model == 'incomplete':
    connection.execute(
        "INSERT INTO responses VALUES "
        "(1, 'VERDICT: truncated', 'truncated-conversation', 'incomplete', 1, 1)"
    )
elif model == 'failure':
    connection.close()
    sys.exit(17)
else:
    response = 'VERDICT: approved\\n1. No blocking findings.'
    if model == 'documented':
        response = os.environ.get('LLM_DOCUMENT_RESPONSE', '# Document\\n\\nGenerated.')
    if '--schema' in arguments:
        response = os.environ.get('LLM_SCHEMA_RESPONSE', json.dumps({
            'verdict': 'approved',
            'findings': [],
        }))
    connection.execute(
        "INSERT INTO responses VALUES (1, ?, 'good-conversation', ?, 10, 20)",
        (response, model),
    )
if model == 'costed':
    connection.execute('ALTER TABLE responses ADD COLUMN response_json TEXT')
    connection.execute(
        "UPDATE responses SET response_json = ?",
        ('{"usage": {"cost": 0.125}, "provider": "Test Provider"}',),
    )
connection.commit()
""",
    )


def mock_openrouter_curl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes = b"",
    returncode: int = 0,
    status: int | str = 200,
    stderr: bytes = b"",
) -> dict[str, object]:
    """Replace curl while preserving the request and response-file contract."""
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        captured["timeout"] = kwargs["timeout"]
        config = command[command.index("--config") + 1]
        captured["config"] = kwargs["input"].decode() if config == "-" else Path(config).read_text()
        response_path = Path(command[command.index("--output") + 1])
        if body:
            response_path.write_bytes(body)
        return subprocess.CompletedProcess(
            command,
            returncode=returncode,
            stdout=str(status).encode(),
            stderr=stderr,
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return captured


def write_fake_codex(
    path: Path,
    *,
    arguments_log: Path | None = None,
    response: str | None = None,
    skip_read_proof: bool = False,
    session_on_stderr: bool = False,
    resolved_model: str | None = None,
    sleep: bool = False,
    write_before_sleep: bool = False,
) -> Path:
    configuration = {
        "arguments_log": str(arguments_log) if arguments_log else None,
        "response": response,
        "skip_read_proof": skip_read_proof,
        "session_on_stderr": session_on_stderr,
        "resolved_model": resolved_model,
        "sleep": sleep,
        "write_before_sleep": write_before_sleep,
    }
    return write_fake_python_executable(
        path,
        "codex",
        """import json
import sys
import time
from pathlib import Path

configuration = __CONFIGURATION__
arguments = sys.argv[1:]
output = Path(arguments[arguments.index('--output-last-message') + 1])
model = arguments[arguments.index('--model') + 1]
if log := configuration['arguments_log']:
    Path(log).write_text(json.dumps(arguments))
response = configuration['response'] or 'VERDICT: approved without blocking findings.'
if '--output-schema' in arguments:
    if response == 'VERDICT: approved without blocking findings.':
        response = json.dumps({'verdict': 'approved', 'findings': [], 'reviewedFiles': []})
    payload = json.loads(response)
    schema = json.loads(Path(arguments[arguments.index('--output-schema') + 1]).read_text())
    requires_read_proof = 'reviewedFiles' in schema.get('required', [])
    if (
        requires_read_proof
        and not payload.get('reviewedFiles')
        and not configuration['skip_read_proof']
    ):
        workspace = Path(arguments[arguments.index('-C') + 1])
        payload['reviewedFiles'] = [
            path.name
            for path in workspace.iterdir()
            if path.is_file()
            and path.name not in {
                'codex-findings.schema.json',
                'codex-response.schema.json',
                'codex-response.md',
            }
        ]
    response = json.dumps(payload)
if configuration['write_before_sleep']:
    output.write_text(response)
if configuration['sleep']:
    time.sleep(60)
output.write_text(response)
print(
    'session id: codex-conversation',
    file=sys.stderr if configuration['session_on_stderr'] else sys.stdout,
)
print(f"model: {configuration['resolved_model'] or model}")
""".replace("__CONFIGURATION__", repr(configuration)),
    )


def write_fake_sandbox_exec(path: Path) -> Path:
    return write_fake_python_executable(
        path,
        "sandbox-exec",
        """import os
import sys

os.execvp(sys.argv[3], sys.argv[3:])
""",
    )


def write_fake_age(path: Path) -> Path:
    return write_fake_python_executable(
        path,
        "age",
        """import os
import sys
from pathlib import Path

if os.environ.get('AGE_FAIL'):
    print('invalid recipient', file=sys.stderr)
    raise SystemExit(1)
if '-d' in sys.argv:
    sys.stdout.buffer.write(Path(sys.argv[-1]).read_bytes())
else:
    target = Path(sys.argv[sys.argv.index('-o') + 1])
    target.write_bytes(sys.stdin.buffer.read())
""",
    )


def write_fake_agy(path: Path) -> Path:
    return write_fake_python_executable(
        path,
        "agy",
        """import json
import os
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
if log := os.environ.get('AGY_ARGUMENTS_LOG'):
    Path(log).write_text(json.dumps(arguments))
payload = {
    'conversation_id': 'agy-conversation',
    'status': os.environ.get('AGY_STATUS', 'SUCCESS'),
    'response': os.environ.get(
        'AGY_RESPONSE',
        json.dumps({'verdict': 'approved', 'findings': []}),
    ),
    'duration_seconds': 1.25,
    'usage': {'input_tokens': 10, 'output_tokens': 20},
}
if delay := os.environ.get('AGY_SLEEP'):
    time.sleep(float(delay))
if exit_code := os.environ.get('AGY_EXIT'):
    sys.exit(int(exit_code))
if os.environ.get('AGY_INVALID_JSON'):
    print('{')
elif os.environ.get('AGY_LIST'):
    print('[]')
else:
    print(json.dumps(payload))
""",
    )


def write_fake_pi(path: Path) -> Path:
    return write_fake_python_executable(
        path,
        "pi",
        """import json
import os
import signal
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
if log := os.environ.get('PI_ARGUMENTS_LOG'):
    Path(log).write_text(json.dumps(arguments))
model = arguments[arguments.index('--model') + 1]
session = Path(arguments[arguments.index('--session') + 1])
if os.environ.get('PI_EMPTY_SESSION'):
    session.touch()
else:
    session.write_text(json.dumps({
        'type': 'session',
        'version': 3,
        'id': 'pi-session',
        'cwd': str(Path.cwd()),
    }) + '\\n')
response = json.dumps({'verdict': 'approved', 'findings': []})
if model == 'empty' or model.endswith('/empty'):
    content = []
else:
    content = [{'type': 'text', 'text': response}]
message = {
    'role': 'assistant',
    'content': content,
    'model': os.environ.get('PI_RESOLVED_MODEL', model),
    'usage': {
        'input': 12,
        'output': 34,
        'cost': {'total': 0.02},
    },
}
if not os.environ.get('PI_OMIT_PROVIDER'):
    message['provider'] = os.environ.get('PI_PROVIDER', 'openrouter')
events = [
    {'type': 'session', 'version': 3, 'id': 'pi-session'},
    {'type': 'agent_start'},
    {'type': 'message_end', 'message': message},
    {'type': 'agent_end', 'messages': [message]},
]
if not os.environ.get('PI_SILENT'):
    print('\\n'.join(json.dumps(event) for event in events))
    sys.stdout.flush()
if diagnostic := os.environ.get('PI_STDERR'):
    print(diagnostic, file=sys.stderr, flush=True)
if termination_diagnostic := os.environ.get('PI_TERM_STDERR'):
    def report_termination(signum, frame):
        print(termination_diagnostic, file=sys.stderr, flush=True)
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, report_termination)
if delay := os.environ.get('PI_SLEEP'):
    time.sleep(float(delay))
if model == 'failure' or model.endswith('/failure'):
    print('provider failed after retries', file=sys.stderr)
    raise SystemExit(17)
""",
    )


def write_fake_kiro(
    path: Path,
    *,
    inventory_mode: str = "valid",
    stage_delays: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Path:
    observations = path / "kiro-observations.jsonl"
    return write_fake_python_executable(
        path,
        "kiro-cli",
        f"""import json
import os
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
observation = {{
    "argv": arguments,
    "cwd": str(Path.cwd().resolve()),
    "entries": sorted(item.name for item in Path.cwd().iterdir()),
    "environment": dict(os.environ),
}}
with Path({str(observations)!r}).open("a") as stream:
    stream.write(json.dumps(observation) + "\\n")

if arguments == ["chat", "--list-models", "--format", "json"]:
    time.sleep({stage_delays[0]!r})
    mode = {inventory_mode!r}
    if mode == "malformed":
        print("{{")
    elif mode == "nonzero":
        print("inventory failed", file=sys.stderr)
        raise SystemExit(19)
    elif mode == "duplicate":
        print(json.dumps({{
            "models": [
                {{"model_id": "claude-sonnet-5"}},
                {{"model_id": "claude-sonnet-5"}},
            ],
            "default_model": "claude-sonnet-5",
        }}))
    elif mode == "bad-default":
        print(json.dumps({{
            "models": [{{"model_id": "claude-sonnet-5"}}],
            "default_model": "missing",
        }}))
    else:
        print(json.dumps({{
            "models": [
                {{"model_id": "claude-sonnet-5"}},
                {{"model_id": "quiet"}},
                {{"model_id": "empty"}},
                {{"model_id": "nonzero"}},
                {{"model_id": "timeout"}},
                {{"model_id": "malformed-session"}},
                {{"model_id": "absent-session"}},
                {{"model_id": "nonzero-session"}},
                {{"model_id": "timeout-session"}},
            ],
            "default_model": "claude-sonnet-5",
        }}))
    raise SystemExit(0)

if arguments == ["chat", "--list-sessions", "--format", "json"]:
    time.sleep({stage_delays[2]!r})
    marker = Path.cwd() / ".selected-model"
    model = marker.read_text() if marker.is_file() else ""
    if model == "malformed-session":
        print("{{")
    elif model == "absent-session":
        print("[]")
    elif model == "nonzero-session":
        print("session inventory failed", file=sys.stderr)
        raise SystemExit(23)
    elif model == "timeout-session":
        time.sleep(60)
    else:
        print(json.dumps([{{
            "cwd": str(Path.cwd().resolve()),
            "sessions": [{{"sessionId": "123e4567-e89b-12d3-a456-426614174000"}}],
        }}]))
    raise SystemExit(0)

model = arguments[arguments.index("--model") + 1]
time.sleep({stage_delays[1]!r})
(Path.cwd() / ".selected-model").write_text(model)
if model == "timeout":
    time.sleep(60)
if model == "nonzero":
    print("token=super-secret-token-value", file=sys.stderr)
    raise SystemExit(17)
if model == "empty":
    raise SystemExit(0)
response = json.dumps({{"verdict": "approved", "findings": []}})
sys.stdout.write("\\x1b[36mKiro CLI\\x1b[0m\\n> " + response + "\\n\\n▸ Credits: 0.25\\n")
if model != "quiet":
    print("token=super-secret-token-value", file=sys.stderr)
""",
    )


def review_arguments(tmp_path: Path, *models: str) -> list[str]:
    prompt = tmp_path / "prompt.md"
    source = tmp_path / "source.py"
    prompt.write_text("Review this bounded change. Return VERDICT and numbered findings.")
    source.write_text("def example() -> None: pass\n")
    return [
        "run",
        "--review-id",
        "packet-1",
        "--prompt-file",
        str(prompt),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--timeout-seconds",
        "5",
        *(argument for model in models for argument in ("--model", model)),
        "--file",
        str(source),
    ]


def test_transport_return_annotations_match_runtime_tuple_shapes() -> None:
    assert cli.invoke_openrouter.__annotations__["return"] == ("tuple[int, str, PersistedResponse]")
    assert cli.invoke_pi_exploration.__annotations__["return"] == (
        "tuple[int, str, str, PersistedResponse]"
    )


def test_receipt_transport_allowlist_matches_registered_backend_descriptors() -> None:
    registered = {descriptor.name for descriptor in cli.build_backend_registry().descriptors()}

    assert review_flow.SUPPORTED_REVIEW_TRANSPORTS == frozenset(registered)


def test_route_parser_accepts_kiro_and_lists_it_in_validation_errors() -> None:
    assert cli.parse_route("kiro:requested-model") == cli.ReviewRoute(
        transport="kiro", model="requested-model"
    )

    with pytest.raises(
        ValueError,
        match=(
            "^routes must use transport:model with transport in "
            "llm, codex, openrouter, agy, kiro, pi$"
        ),
    ):
        cli.parse_route("unknown:model")


def test_run_transport_choices_accept_kiro() -> None:
    namespace = cli.build_parser().parse_args(
        ["run", "--review-id", "kiro-choice", "--model", "requested-model", "--transport", "kiro"]
    )

    assert namespace.transport == "kiro"


def test_kiro_is_an_account_included_tournament_transport() -> None:
    candidate = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "kiro-seat",
                    "family": "kiro",
                    "model": "claude-sonnet-5",
                    "transport": "kiro",
                    "cost_mode": "account-included",
                }
            ]
        }
    )[0]

    assert candidate.transport == "kiro"


def test_run_rejects_kiro_auto_before_creating_artifacts(tmp_path: Path) -> None:
    result = run_cli(
        *review_arguments(tmp_path),
        "--transport",
        "kiro",
        "--model",
        "auto",
    )

    assert result.returncode == 2
    assert "kiro review model auto" in result.stderr.lower()
    assert not (tmp_path / "artifacts").exists()


def test_proprietary_kiro_requires_an_authorizing_policy_for_every_kiro_route(
    tmp_path: Path,
) -> None:
    fake_kiro = write_fake_kiro(tmp_path)
    arguments = [
        *review_arguments(tmp_path),
        "--transport",
        "kiro",
        "--model",
        "claude-sonnet-5",
        "--source-class",
        "proprietary",
    ]

    missing = run_cli(*arguments, env={"KIRO_BIN": str(fake_kiro)})
    assert missing.returncode == 2
    assert "proprietary kiro reviews require --policy" in missing.stderr.lower()

    policy = tmp_path / "denied.toml"
    policy.write_text('[models."claude-sonnet-5"]\nsource_allowed = false\n')
    denied = run_cli(*arguments, "--policy", str(policy), env={"KIRO_BIN": str(fake_kiro)})
    assert denied.returncode == 2
    assert "does not allow kiro model claude-sonnet-5" in denied.stderr.lower()
    assert not (tmp_path / "kiro-observations.jsonl").exists()


def test_proprietary_kiro_policy_checks_each_kiro_route(tmp_path: Path) -> None:
    policy = tmp_path / "partial.toml"
    policy.write_text('[models."claude-sonnet-5"]\nsource_allowed = true\n')

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "kiro:claude-sonnet-5",
        "--route",
        "kiro:empty",
        "--source-class",
        "proprietary",
        "--policy",
        str(policy),
    )

    assert result.returncode == 2
    assert "does not allow kiro model empty" in result.stderr.lower()
    assert not (tmp_path / "artifacts").exists()


def test_run_uses_kiro_without_policy_for_synthetic_source_and_hides_unresolved_identity(
    tmp_path: Path,
) -> None:
    fake_kiro = write_fake_kiro(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--transport",
        "kiro",
        "--model",
        "claude-sonnet-5",
        "--response-contract",
        "findings-json",
        env={"KIRO_BIN": str(fake_kiro)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert receipt["model"] == {"requested": ["claude-sonnet-5"], "resolved": None}
    assert receipt["response"]["provider"] is None
    assert attempt["model"] == {"requested": "claude-sonnet-5", "resolved": None}
    assert attempt["provider"] == {"requested": [], "resolved": None}
    assert attempt["result"] == "accepted"
    assert attempt["evidence"]["request"].endswith("request.json")
    assert attempt["evidence"]["response"].endswith("response.log")
    assert attempt["evidence"]["session"].endswith("session.json")
    assert attempt["evidence"]["finalResponse"].endswith("response.md")
    assert attempt["evidence"]["stderr"].endswith("stderr.log")
    assert Path(attempt["evidence"]["finalResponse"]).read_text() == (
        '{"verdict": "approved", "findings": []}'
    )


def test_empty_kiro_response_records_zero_byte_stderr_evidence(tmp_path: Path) -> None:
    fake_kiro = write_fake_kiro(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--transport",
        "kiro",
        "--model",
        "empty",
        env={"KIRO_BIN": str(fake_kiro)},
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    stderr_path = Path(attempt["evidence"]["stderr"])
    assert attempt["result"] == "empty"
    assert stderr_path.name == "stderr.log"
    assert stderr_path.read_bytes() == b""


def test_kiro_attempt_artifacts_are_private_including_empty_stderr(tmp_path: Path) -> None:
    fake_kiro = write_fake_kiro(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--transport",
        "kiro",
        "--model",
        "quiet",
        "--response-contract",
        "findings-json",
        env={"KIRO_BIN": str(fake_kiro)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt_dir = Path(receipt["attempts"][0]["evidence"]["request"]).parent
    artifacts = [
        attempt_dir / name
        for name in (
            "request.json",
            "models.json",
            "response.log",
            "session.json",
            "response.md",
            "stderr.log",
        )
    ]
    assert {stat.S_IMODE(path.stat().st_mode) for path in artifacts} == {0o600}
    assert (attempt_dir / "stderr.log").read_bytes() == b""


def test_proprietary_kiro_runs_with_an_authorizing_policy(tmp_path: Path) -> None:
    fake_kiro = write_fake_kiro(tmp_path)
    policy = tmp_path / "allowed.toml"
    policy.write_text('[models."claude-sonnet-5"]\nsource_allowed = true\n')

    result = run_cli(
        *review_arguments(tmp_path),
        "--transport",
        "kiro",
        "--model",
        "claude-sonnet-5",
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        "--policy",
        str(policy),
        env={"KIRO_BIN": str(fake_kiro)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["policy"]["sha256"] == cli.sha256_bytes(policy.read_bytes())


def test_receipt_contract_allowlist_matches_cli_contract_choices() -> None:
    assert review_flow.SUPPORTED_RESPONSE_CONTRACTS == frozenset(cli.RESPONSE_CONTRACTS)


def test_run_dispatches_frozen_packet_through_registered_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    response_json = '{"verdict":"approved","findings":[]}'
    captured: list[cli.BackendRequest] = []
    registry = cli.BackendRegistry()
    descriptor = cli.build_backend_registry().require("llm").descriptor

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        captured.append(request)
        evidence_path = request.attempt_dir / "response.md"
        evidence_path.write_text(response_json)
        return cli.BackendExecution(
            0,
            "",
            cli.PersistedResponse("fake-turn", None, 1, 10, "accepted", 2, None, response_json),
            cli.BackendEvidence(response=evidence_path),
        )

    registry.register(descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    arguments = [
        *review_arguments(tmp_path, "accepted"),
        "--transport",
        "llm",
        "--response-contract",
        "findings-json",
    ]
    namespace = cli.build_parser().parse_args(arguments)

    previous_umask = os.umask(0o022)
    try:
        assert namespace.handler(namespace) == 0
    finally:
        os.umask(previous_umask)
    assert len(captured) == 1
    request = captured[0]
    original_source = tmp_path / "source.py"
    assert request.model == "accepted"
    assert request.response_contract == "findings-json"
    assert request.files
    assert original_source not in request.files
    assert all(path.parent.name.startswith("reviewctl-input-") for path in request.files)
    receipt = json.loads((Path(capsys.readouterr().out.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    evidence_path = request.attempt_dir / "response.md"
    assert receipt["transport"] == "llm"
    assert receipt["acceptedAttempt"] == 1
    assert attempt["result"] == "accepted"
    assert attempt["evidence"]["response"] == str(evidence_path)
    assert evidence_path.read_text() == response_json
    raw_response = attempt["rawResponse"]
    raw_response_path = Path(raw_response["path"])
    assert raw_response_path == request.attempt_dir / "raw-response.txt"
    assert raw_response_path != evidence_path
    assert raw_response_path.read_text() == response_json
    assert stat.S_IMODE(raw_response_path.stat().st_mode) == 0o600
    assert raw_response["sha256"] == cli.sha256_bytes(response_json.encode())
    assert raw_response["characters"] == len(response_json)


def test_run_rejects_raw_response_evidence_collision_without_overwriting_adapter_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response_json = '{"verdict":"approved","findings":[]}'
    native_bytes = b"adapter-native-evidence"
    collision_path: Path | None = None
    registry = cli.BackendRegistry()
    descriptor = cli.build_backend_registry().require("llm").descriptor

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        nonlocal collision_path
        collision_path = request.attempt_dir / "raw-response.txt"
        collision_path.write_bytes(native_bytes)
        return cli.BackendExecution(
            0,
            "",
            cli.PersistedResponse("conversation", None, 1, 10, "accepted", 2, None, response_json),
            cli.BackendEvidence(response=collision_path),
        )

    registry.register(descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    namespace = cli.build_parser().parse_args(
        [
            *review_arguments(tmp_path, "accepted"),
            "--transport",
            "llm",
            "--response-contract",
            "findings-json",
        ]
    )

    with pytest.raises(
        RuntimeError,
        match=r"raw response evidence collision at .*raw-response\.txt.*different evidence path",
    ):
        namespace.handler(namespace)

    assert collision_path is not None
    assert collision_path.read_bytes() == native_bytes


@pytest.mark.parametrize(
    ("response_text", "resolved_model", "exit_code", "expected_result"),
    [
        ("not-json", "accepted", 0, "incomplete"),
        ('{"verdict":"approved"}', "accepted", 0, "incomplete"),
        ('{"verdict":"approved","findings":[]}', "other-model", 0, "model-mismatch"),
        ('{"verdict":"approved","findings":[]}', "accepted", 17, "transport-failed"),
        ("", "accepted", 0, "empty"),
        ("\ud800", "accepted", 0, "incomplete"),
        (None, None, 0, "missing-response"),
    ],
    ids=[
        "invalid-json",
        "contract-incomplete",
        "model-mismatch",
        "transport-failure-with-payload",
        "empty-response",
        "non-scalar-unicode",
        "no-response",
    ],
)
def test_run_preserves_every_present_raw_backend_response(
    response_text: str | None,
    resolved_model: str | None,
    exit_code: int,
    expected_result: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = cli.BackendRegistry()
    descriptor = cli.build_backend_registry().require("llm").descriptor

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        persisted = (
            cli.PersistedResponse(
                "conversation", None, 1, 10, resolved_model or "", 2, None, response_text
            )
            if response_text is not None
            else None
        )
        return cli.BackendExecution(
            exit_code, "failed" if exit_code else "", persisted, cli.BackendEvidence()
        )

    registry.register(descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    namespace = cli.build_parser().parse_args(
        [
            *review_arguments(tmp_path, "accepted"),
            "--transport",
            "llm",
            "--response-contract",
            "findings-json",
        ]
    )

    assert namespace.handler(namespace) == 1
    turn = Path(capsys.readouterr().out.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == expected_result
    assert json.loads((turn / "attempts" / "01" / "attempt.json").read_text()) == attempt
    raw_response = attempt["rawResponse"]
    durable_path = turn / "attempts" / "01" / "raw-response.txt"
    if response_text is None:
        assert raw_response is None
        assert not durable_path.exists()
    else:
        response_bytes = response_text.encode(errors="surrogatepass")
        assert raw_response == {
            "path": str(durable_path),
            "sha256": cli.sha256_bytes(response_bytes),
            "characters": len(response_text),
        }
        assert durable_path.read_bytes() == response_bytes


def _run_registered_findings_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    responses: list[dict[str, object]],
    *,
    models: tuple[str, ...] = ("accepted",),
    max_attempts: int = 2,
    response_contract: str = "findings-json",
) -> tuple[int, dict[str, object], list[cli.BackendRequest]]:
    captured: list[cli.BackendRequest] = []
    registry = cli.BackendRegistry()
    descriptor = cli.build_backend_registry().require("llm").descriptor

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        captured.append(request)
        item = responses[len(captured) - 1]
        response = item.get("response")
        persisted = (
            cli.PersistedResponse(
                str(item.get("conversation", "conversation")),
                None,
                1,
                10,
                str(item.get("model", request.model)),
                2,
                item.get("provider") if isinstance(item.get("provider"), str) else None,
                response,
            )
            if isinstance(response, str)
            else None
        )
        return cli.BackendExecution(
            int(item.get("exit_code", 0)),
            str(item.get("diagnostic", "")),
            persisted,
            cli.BackendEvidence(),
        )

    registry.register(descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    namespace = cli.build_parser().parse_args(
        [
            *review_arguments(tmp_path, *models),
            "--transport",
            "llm",
            "--response-contract",
            response_contract,
            "--max-attempts",
            str(max_attempts),
        ]
    )

    return_code = namespace.handler(namespace)
    turn = Path(capsys.readouterr().out.strip())
    return return_code, json.loads((turn / "receipt.json").read_text()), captured


def test_partial_findings_complete_with_typed_same_route_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = {
        "severity": "high",
        "path": "source.py",
        "line": 1,
        "title": "Partial finding",
        "evidence": "The first response identified this evidence.",
        "reproduction": "Inspect source.py line 1.",
    }
    raw_partial_marker = "RAW-PRIOR-RESPONSE-MUST-NOT-BE-INJECTED"
    partial = json.dumps({"findings": [finding], "untrusted": raw_partial_marker})
    complete = json.dumps({"verdict": "approved", "findings": []})

    return_code, receipt, requests = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": partial}, {"response": complete}],
    )

    assert return_code == 0
    assert len(requests) == 2
    first_prompt, second_prompt = (request.prompt for request in requests)
    assert raw_partial_marker not in second_prompt
    assert partial not in second_prompt
    assert "<reviewctl-completion-context>" in second_prompt
    assert finding["title"] in second_prompt
    assert receipt["prompt"]["packetSha256"] == cli.sha256_bytes(first_prompt.encode())
    assert receipt["attempts"][0]["attemptRequestSha256"] == cli.sha256_bytes(first_prompt.encode())
    assert receipt["attempts"][1]["attemptRequestSha256"] == cli.sha256_bytes(
        second_prompt.encode()
    )
    assert receipt["acceptedAttempt"] == 2
    assert [attempt["result"] for attempt in receipt["attempts"]] == [
        "incomplete",
        "accepted",
    ]
    first_evaluation = receipt["attempts"][0]["contractEvaluation"]
    assert first_evaluation["status"] == "incomplete"
    assert (
        first_evaluation["completionRequest"]["packetDigest"] == receipt["prompt"]["packetSha256"]
    )
    promoted = receipt["attempts"][0]["promotedFragments"]
    assert len(promoted) == 1
    assert receipt["fallbackRelationships"] == [
        {
            "fromAttempt": 1,
            "toAttempt": 2,
            "kind": "retry",
            "reason": "contract-incomplete",
            "promotedFragmentIds": [promoted[0]["fragmentId"]],
        }
    ]
    assert receipt["verdict"] == "approved"
    assert receipt["findings"] == []
    assert receipt["consolidatedReview"]["status"] == "accepted"
    assert receipt["consolidatedReview"]["verdict"] == "approved"
    assert receipt["consolidatedReview"]["approved"] is False
    assert receipt["consolidatedReview"]["acceptedAttempt"] == 2
    assert len(receipt["consolidatedReview"]["findings"]) == 1
    assert cli.validate_v2_receipt(receipt) == ()
    log = Path(receipt["logging"]["path"]).read_text()
    assert '"event":"attempt_retry"' in log
    assert '"from_attempt":1' in log
    assert '"to_attempt":2' in log
    assert '"event":"route_fallback"' not in log


def test_partial_then_invalid_preserves_only_consolidated_partial_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = {
        "severity": "medium",
        "path": "source.py",
        "line": 1,
        "title": "Useful partial finding",
        "evidence": "The partial response contains bounded evidence.",
        "reproduction": "Inspect the source.",
    }
    partial = json.dumps({"findings": [finding]})

    return_code, receipt, requests = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": partial}, {"response": "not json"}],
    )

    assert return_code == 1
    assert len(requests) == 2
    assert receipt["result"] == "unavailable"
    assert receipt["acceptedAttempt"] is None
    assert [attempt["contractEvaluation"]["status"] for attempt in receipt["attempts"]] == [
        "incomplete",
        "invalid",
    ]
    assert receipt["attempts"][1]["promotedFragments"] == []
    assert receipt["consolidatedReview"]["status"] == "unavailable"
    assert receipt["consolidatedReview"]["approved"] is False
    assert len(receipt["consolidatedReview"]["findings"]) == 1
    assert cli.validate_v2_receipt(receipt) == ()


def test_partial_findings_follow_the_actual_route_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = {
        "severity": "low",
        "path": "source.py",
        "line": 1,
        "title": "Cross-route finding",
        "evidence": "The first route returned useful evidence.",
        "reproduction": "Inspect source.py.",
    }
    partial = json.dumps({"findings": [finding]})
    complete = json.dumps({"verdict": "changes-requested", "findings": [finding]})

    return_code, receipt, requests = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": partial}, {"response": complete}],
        models=("route-one", "route-two"),
        max_attempts=1,
    )

    assert return_code == 0
    assert [request.model for request in requests] == ["route-one", "route-two"]
    encoded_context = (
        requests[1]
        .prompt.split("<reviewctl-completion-context>\n", 1)[1]
        .split("\n</reviewctl-completion-context>", 1)[0]
    )
    assert json.loads(encoded_context)["fileNames"] == ["source.py"]
    assert receipt["fallbackRelationships"][0]["kind"] == "route-fallback"
    assert receipt["fallbackRelationships"][0]["reason"] == "contract-incomplete"
    log = Path(receipt["logging"]["path"]).read_text()
    assert '"event":"route_fallback"' in log
    assert '"event":"attempt_retry"' not in log


@pytest.mark.parametrize("response_contract", ["findings-json", "verdict"])
@pytest.mark.parametrize(
    ("response_overrides", "expected_gate", "reject_provider"),
    [
        ({"response": "partial", "exit_code": 124}, "timeout", False),
        ({"response": "partial", "exit_code": 17}, "transport-failed", False),
        ({"response": None}, "missing-response", False),
        ({"response": "partial", "model": "wrong-model"}, "model-mismatch", False),
        ({"response": "partial", "provider": "wrong"}, "provider-mismatch", True),
        ({"response": ""}, "empty", False),
        ({"response": "partial", "conversation": ""}, "missing-conversation", False),
    ],
    ids=["timeout", "exit", "missing", "model", "provider", "empty", "conversation"],
)
def test_precontract_gates_never_invoke_native_or_legacy_evaluation(
    response_contract: str,
    response_overrides: dict[str, object],
    expected_gate: str,
    reject_provider: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if reject_provider:
        monkeypatch.setattr(cli, "resolved_provider_matches", lambda *_: False)

    if response_contract == "findings-json":
        real_contract = cli.get_contract("findings-json")

        class ExplodingContract:
            name = real_contract.name
            version = real_contract.version

            def prepare(self, context: object) -> object:
                return real_contract.prepare(context)

            def evaluate(self, *_: object, **__: object) -> object:
                raise AssertionError("native contract evaluation crossed a pre-gate")

        monkeypatch.setattr(cli, "get_contract", lambda _: ExplodingContract())
    else:

        def explode(*_: object, **__: object) -> object:
            raise AssertionError("legacy validation crossed a pre-gate")

        monkeypatch.setattr(cli, "validate_review_response", explode)
        monkeypatch.setattr(cli, "review_validation_error", explode)

    return_code, receipt, requests = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [response_overrides],
        max_attempts=1,
        response_contract=response_contract,
    )

    assert return_code == 1
    assert len(requests) == 1
    attempt = receipt["attempts"][0]
    assert attempt["result"] == expected_gate
    if response_contract == "findings-json":
        assert attempt["promotedFragments"] == []
    else:
        assert "promotedFragments" not in attempt
    assert attempt["validationError"] is None
    assert "contractEvaluation" not in attempt
    if response_overrides["response"] is None:
        assert attempt["rawResponse"] is None
    else:
        raw_response = attempt["rawResponse"]
        assert Path(raw_response["path"]).is_file()
        assert raw_response["characters"] == len(str(response_overrides["response"]))
    if response_contract == "findings-json":
        assert receipt["fallbackRelationships"] == []
        assert receipt["consolidatedReview"]["findings"] == []


def test_provider_mismatch_never_promotes_partial_findings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = {
        "severity": "high",
        "path": "source.py",
        "line": 1,
        "title": "Wrong provider partial",
        "evidence": "This payload came from a rejected provider.",
        "reproduction": "Inspect provider resolution.",
    }
    monkeypatch.setattr(cli, "resolved_provider_matches", lambda *_: False)

    return_code, receipt, requests = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": json.dumps({"findings": [finding]}), "provider": "wrong"}],
        max_attempts=1,
    )

    assert return_code == 1
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "provider-mismatch"
    assert attempt["promotedFragments"] == []
    assert receipt["consolidatedReview"]["findings"] == []


def test_native_data_decode_failure_emits_stable_invalid_attempt_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile = '{"oversized":' + ("9" * 5000) + "}"

    return_code, receipt, _ = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": hostile}],
        max_attempts=1,
    )

    assert return_code == 1
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "incomplete"
    assert attempt["validationError"] == "findings-json: invalid JSON"
    assert "evaluationError" not in attempt
    assert attempt["contractEvaluation"]["status"] == "invalid"
    assert attempt["contractEvaluation"]["violations"] == ["invalid-json"]
    assert attempt["promotedFragments"] == []
    raw_response = attempt["rawResponse"]
    assert Path(raw_response["path"]).read_text() == hostile
    assert raw_response["sha256"] == cli.sha256_bytes(hostile.encode())
    assert receipt["acceptedAttempt"] is None
    assert receipt["result"] == "unavailable"
    assert cli.validate_v2_receipt(receipt) == ()


def test_native_value_error_is_data_invalid_but_runtime_error_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_contract = cli.get_contract("findings-json")

    class RaisingContract:
        name = real_contract.name
        version = real_contract.version

        def __init__(self, error: Exception) -> None:
            self.error = error

        def prepare(self, context: object) -> object:
            return real_contract.prepare(context)

        def evaluate(self, *_: object, **__: object) -> object:
            raise self.error

    monkeypatch.setattr(cli, "get_contract", lambda _: RaisingContract(ValueError("hostile")))
    return_code, receipt, _ = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": "eligible response"}],
        max_attempts=1,
    )
    assert return_code == 1
    assert receipt["attempts"][0]["evaluationError"]["type"] == "ValueError"
    assert receipt["attempts"][0]["promotedFragments"] == []
    assert "contractEvaluation" not in receipt["attempts"][0]
    assert cli.validate_v2_receipt(receipt) == ()

    monkeypatch.setattr(cli, "get_contract", lambda _: RaisingContract(RuntimeError("bug")))
    other = tmp_path / "runtime-error"
    other.mkdir()
    with pytest.raises(RuntimeError, match="bug"):
        _run_registered_findings_sequence(
            monkeypatch,
            other,
            capsys,
            [{"response": "eligible response"}],
            max_attempts=1,
        )


@pytest.mark.parametrize(
    ("source_transport", "target_transport"),
    [
        ("codex", "llm"),
        ("llm", "codex"),
    ],
)
def test_completion_prompt_uses_target_route_contract_context(
    source_transport: str,
    target_transport: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = {
        "severity": "high",
        "path": "source.py",
        "line": 1,
        "title": "Target-bound context",
        "evidence": "The first route found bounded evidence.",
        "reproduction": "Inspect source.py line 1.",
    }
    raw_marker = "RAW-SOURCE-PAYLOAD-MUST-NOT-CROSS"
    partial = json.dumps({"findings": [finding], "untrusted": raw_marker})
    target_review: dict[str, object] = {
        "verdict": "changes-requested",
        "findings": [finding],
    }
    if target_transport == "codex":
        target_review["reviewedFiles"] = ["source.py"]
    responses = [partial, json.dumps(target_review)]
    captured: list[cli.BackendRequest] = []
    real_registry = cli.build_backend_registry()
    registry = cli.BackendRegistry()

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        captured.append(request)
        response = responses[len(captured) - 1]
        return cli.BackendExecution(
            0,
            "",
            cli.PersistedResponse("conversation", None, 1, 10, request.model, 2, None, response),
            cli.BackendEvidence(),
        )

    for transport in {source_transport, target_transport}:
        registry.register(real_registry.require(transport).descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    namespace = cli.build_parser().parse_args(
        [
            *review_arguments(tmp_path),
            "--route",
            f"{source_transport}:source-model",
            "--route",
            f"{target_transport}:target-model",
            "--response-contract",
            "findings-json",
            "--source-class",
            "proprietary",
            "--max-attempts",
            "1",
        ]
    )

    assert namespace.handler(namespace) == 0
    receipt = json.loads((Path(capsys.readouterr().out.strip()) / "receipt.json").read_text())
    assert len(captured) == 2
    first_prompt, target_prompt = (request.prompt for request in captured)
    source_contract_context = cli.ContractContext(
        file_names=("source.py",),
        review_declaration_required=source_transport == "codex",
    )
    source_prepared = cli.get_contract("findings-json").prepare(source_contract_context)

    assert target_prompt == first_prompt
    assert "<reviewctl-completion-context>" not in target_prompt
    assert receipt["prompt"]["packetSha256"] == cli.sha256_bytes(first_prompt.encode())
    assert receipt["attempts"][1]["attemptRequestSha256"] == cli.sha256_bytes(
        target_prompt.encode()
    )
    assert raw_marker not in target_prompt
    assert partial not in target_prompt
    first_attempt = receipt["attempts"][0]
    assert first_attempt["contractEvaluation"]["preparedSha256"] == source_prepared.digest
    assert (
        first_attempt["promotedFragments"][0]["payloadDigest"]
        == first_attempt["rawResponse"]["sha256"]
    )
    assert first_attempt["promotedFragments"][0]["preparedDigest"] == source_prepared.digest
    assert first_attempt["promotedFragments"][0]["contractContext"] == {
        "fileNames": ["source.py"],
        "reviewDeclarationRequired": source_transport == "codex",
    }
    assert receipt["fallbackRelationships"][0]["promotedFragmentIds"] == []


def test_max_attempts_applies_per_route_in_order_for_retriable_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    executions: list[str] = []
    registry = cli.BackendRegistry()
    descriptor = cli.build_backend_registry().require("llm").descriptor

    def execute(request: cli.BackendRequest) -> cli.BackendExecution:
        executions.append(request.model)
        return cli.BackendExecution(0, "", None, cli.BackendEvidence())

    registry.register(descriptor, execute)
    monkeypatch.setattr(cli, "build_backend_registry", lambda: registry)
    arguments = [
        *review_arguments(tmp_path, "route1", "route2"),
        "--max-attempts",
        "2",
    ]
    namespace = cli.build_parser().parse_args(arguments)

    assert namespace.handler(namespace) == 1
    receipt = json.loads((Path(capsys.readouterr().out.strip()) / "receipt.json").read_text())
    expected_routes = [
        {"model": "route1", "transport": "llm"},
        {"model": "route2", "transport": "llm"},
    ]
    expected_execution_order = ["route1", "route1", "route2", "route2"]
    assert receipt["executionSettings"]["maxAttempts"] == 2
    assert receipt["routes"] == expected_routes
    assert executions == expected_execution_order
    assert len(executions) <= len(expected_routes) * 2
    assert [attempt["route"] for attempt in receipt["attempts"]] == [
        expected_routes[0],
        expected_routes[0],
        expected_routes[1],
        expected_routes[1],
    ]
    assert [attempt["result"] for attempt in receipt["attempts"]] == ["missing-response"] * 4


def test_uses_a_separate_database_for_each_attempt_and_accepts_matching_model(
    tmp_path: Path,
) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path, "empty", "accepted"), env={"LLM_BIN": str(fake_llm)}
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert [attempt["result"] for attempt in receipt["attempts"]] == ["empty", "accepted"]
    assert receipt["acceptedAttempt"] == 2
    assert receipt["model"]["resolved"] == "accepted"
    assert receipt["attempts"][0]["database"] != receipt["attempts"][1]["database"]
    for attempt, expected_response in zip(
        receipt["attempts"],
        ["", "VERDICT: approved\n1. No blocking findings."],
        strict=True,
    ):
        assert not Path(attempt["database"]).exists()
        raw_response = attempt["rawResponse"]
        raw_response_path = Path(raw_response["path"])
        assert raw_response_path.is_file()
        assert raw_response_path.read_text() == expected_response
        assert raw_response["sha256"] == cli.sha256_bytes(expected_response.encode())
        assert raw_response["characters"] == len(expected_response)


def test_ordered_routes_fallback_after_antigravity_quota_failure(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "agy:gemini-3.6-flash-high",
        "--route",
        "llm:accepted",
        env={
            "AGY_BIN": str(fake_agy),
            "AGY_STATUS": "QUOTA_EXCEEDED",
            "LLM_BIN": str(fake_llm),
        },
    )

    assert result.returncode == 0, result.stderr
    turn = Path(result.stdout.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    assert [attempt["result"] for attempt in receipt["attempts"]] == [
        "transport-failed",
        "accepted",
    ]
    assert [attempt["route"] for attempt in receipt["attempts"]] == [
        {"model": "gemini-3.6-flash-high", "transport": "agy"},
        {"model": "accepted", "transport": "llm"},
    ]
    assert receipt["transport"] == "routed"
    log = Path(receipt["logging"]["path"])
    contents = log.read_text()
    assert '"event":"route_fallback"' in contents
    assert '"reason":"transport-failed"' in contents


def test_pi_transport_archives_events_session_and_final_response(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/accepted",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(fake_pi)},
    )

    assert result.returncode == 0, result.stderr
    turn = Path(result.stdout.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert receipt["transport"] == "pi"
    assert receipt["response"]["conversationId"] == "pi-session"
    assert receipt["response"]["provider"] == "openrouter"
    assert attempt["costUsd"] == 0.02
    assert Path(attempt["evidence"]["request"]).is_file()
    assert Path(attempt["evidence"]["response"]).read_text()
    assert Path(attempt["evidence"]["session"]).is_file()
    assert Path(attempt["evidence"]["finalResponse"]).read_text() == (
        '{"verdict": "approved", "findings": []}'
    )
    assert attempt["evidence"]["stderr"] is None


def test_pi_transport_preserves_failed_event_stream(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/failure",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(fake_pi)},
    )

    assert result.returncode == 1
    turn = Path(result.stdout.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "transport-failed"
    assert "provider failed after retries" in attempt["diagnostic"]
    assert Path(attempt["evidence"]["response"]).read_text()
    assert Path(attempt["evidence"]["session"]).is_file()
    assert "provider failed after retries" in Path(attempt["evidence"]["stderr"]).read_text()


def test_pi_timeout_drains_diagnostics_emitted_during_termination(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)
    arguments = review_arguments(tmp_path)
    arguments[arguments.index("--timeout-seconds") + 1] = "1"

    result = run_cli(
        *arguments,
        "--route",
        "pi:openrouter/accepted",
        "--response-contract",
        "findings-json",
        env={
            "PI_BIN": str(fake_pi),
            "PI_SLEEP": "3",
            "PI_TERM_STDERR": "diagnostic emitted during termination",
        },
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "timeout"
    assert (
        "diagnostic emitted during termination" in Path(attempt["evidence"]["stderr"]).read_text()
    )


def test_missing_pi_binary_does_not_claim_nonexistent_evidence(tmp_path: Path) -> None:
    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/accepted",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(tmp_path / "missing-pi")},
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "transport-failed"
    assert Path(attempt["evidence"]["request"]).is_file()
    assert attempt["evidence"]["response"] is None
    assert attempt["evidence"]["session"] is None
    assert attempt["evidence"]["finalResponse"] is None
    assert attempt["evidence"]["stderr"] is None


def test_silent_pi_process_does_not_claim_empty_event_or_stderr_evidence(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/silent",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(fake_pi), "PI_SILENT": "1"},
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "model-mismatch"
    assert attempt["evidence"]["response"] is None
    assert attempt["evidence"]["stderr"] is None


def test_pi_transport_does_not_claim_an_empty_session_file(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/accepted",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(fake_pi), "PI_EMPTY_SESSION": "1"},
    )

    assert result.returncode == 0
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "accepted"
    assert attempt["evidence"]["session"] is None


def test_pi_metadata_normalization_preserves_provider_qualified_routes() -> None:
    assert cli.pi_resolved_model("google/gemini-2.5-flash", "google", "gemini-2.5-flash") == (
        "google/gemini-2.5-flash"
    )
    assert (
        cli.pi_resolved_model(
            "openrouter/google/gemini-2.5-flash", "openrouter", "google/gemini-2.5-flash"
        )
        == "openrouter/google/gemini-2.5-flash"
    )
    assert (
        cli.pi_resolved_model("openrouter/google/gemini-2.5-flash", "google", "gemini-2.5-flash")
        == "google/gemini-2.5-flash"
    )
    assert (
        cli.pi_resolved_model(
            "openrouter/google/gemini-2.5-flash", None, "openrouter/google/gemini-2.5-flash"
        )
        == ""
    )
    assert (
        cli.pi_resolved_model(
            "openrouter/google/gemini-2.5-flash", "openrouter/google", "gemini-2.5-flash"
        )
        == ""
    )


def test_pi_transport_rejects_a_non_atomic_observed_provider(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/google/gemini-2.5-flash",
        "--response-contract",
        "findings-json",
        env={
            "PI_BIN": str(fake_pi),
            "PI_PROVIDER": "openrouter/google",
            "PI_RESOLVED_MODEL": "gemini-2.5-flash",
        },
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "model-mismatch"
    assert attempt["model"]["resolved"] == ""
    assert attempt["provider"]["resolved"] == "openrouter/google"


def test_pi_transport_rejects_an_observed_provider_route_mismatch(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/google/gemini-2.5-flash",
        "--response-contract",
        "findings-json",
        env={
            "PI_BIN": str(fake_pi),
            "PI_PROVIDER": "google",
            "PI_RESOLVED_MODEL": "gemini-2.5-flash",
        },
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["attempts"][0]["result"] == "model-mismatch"
    assert receipt["attempts"][0]["model"] == {
        "requested": "openrouter/google/gemini-2.5-flash",
        "resolved": "google/gemini-2.5-flash",
    }


def test_pi_transport_rejects_a_missing_observed_provider(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/google/gemini-2.5-flash",
        "--response-contract",
        "findings-json",
        env={
            "PI_BIN": str(fake_pi),
            "PI_OMIT_PROVIDER": "1",
            "PI_RESOLVED_MODEL": "openrouter/google/gemini-2.5-flash",
        },
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert attempt["result"] == "model-mismatch"
    assert attempt["model"] == {
        "requested": "openrouter/google/gemini-2.5-flash",
        "resolved": "",
    }
    assert attempt["provider"]["resolved"] is None


def test_formal_pi_transport_requires_provider_qualified_model_identity(tmp_path: Path) -> None:
    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:gemini-2.5-flash",
        "--response-contract",
        "findings-json",
    )

    assert result.returncode == 2
    assert "pi review models must use provider/model identity" in result.stderr


def test_pi_response_normalization_only_removes_one_json_fence() -> None:
    fenced = '```json\n{"verdict":"approved","findings":[]}\n```'

    assert cli.normalize_pi_response(fenced, "findings-json") == (
        '{"verdict":"approved","findings":[]}'
    )
    assert cli.normalize_pi_response("```json\n[]\n```", "findings-json") == "```json\n[]\n```"
    assert (
        cli.normalize_pi_response('```json\n{"verdict":NaN}\n```', "findings-json")
        == '```json\n{"verdict":NaN}\n```'
    )
    assert cli.normalize_pi_response(fenced, "document") == fenced


def test_explore_start_creates_a_named_resumable_pi_session(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"
    arguments_log = tmp_path / "pi-arguments.json"

    result = run_cli(
        "explore",
        "start",
        "--id",
        "ledger-ideas",
        "--model",
        "accepted",
        "--prompt",
        "Explore the product direction.",
        "--exploration-root",
        str(exploration_root),
        env={"PI_BIN": str(fake_pi), "PI_ARGUMENTS_LOG": str(arguments_log)},
    )

    assert result.returncode == 0, result.stderr
    session_root = exploration_root / "ledger-ideas"
    manifest = json.loads((session_root / "manifest.json").read_text())
    assert manifest["id"] == "ledger-ideas"
    assert manifest["model"] == "accepted"
    assert manifest["turns"] == 1
    arguments = json.loads(arguments_log.read_text())
    assert "--tools" in arguments
    assert arguments[arguments.index("--tools") + 1] == "read,grep,find,ls"
    assert "--no-approve" in arguments
    assert "--approve" not in arguments
    assert "--no-tools" not in arguments
    assert (session_root / "session.jsonl").is_file()
    assert (session_root / "turns" / "001" / "request.md").read_text() == (
        "Explore the product direction."
    )
    assert (session_root / "turns" / "001" / "events.jsonl").is_file()
    assert (session_root / "turns" / "001" / "response.md").is_file()


def test_explore_resume_uses_the_same_pi_session_and_appends_a_turn(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"
    common = ["--exploration-root", str(exploration_root)]

    started = run_cli(
        "explore",
        "start",
        "--id",
        "ledger-thread",
        "--model",
        "accepted",
        "--prompt",
        "Start the thread.",
        *common,
        env={"PI_BIN": str(fake_pi)},
    )
    assert started.returncode == 0, started.stderr

    resumed = run_cli(
        "explore",
        "resume",
        "--id",
        "ledger-thread",
        "--prompt",
        "Continue the thread.",
        *common,
        env={"PI_BIN": str(fake_pi)},
    )

    assert resumed.returncode == 0, resumed.stderr
    session_root = exploration_root / "ledger-thread"
    manifest = json.loads((session_root / "manifest.json").read_text())
    assert manifest["turns"] == 2
    assert (session_root / "turns" / "002" / "request.md").read_text() == ("Continue the thread.")
    assert manifest["session"] == str(session_root / "session.jsonl")


def test_explore_resume_can_explicitly_revoke_persisted_tools(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"
    arguments_log = tmp_path / "pi-resume-arguments.json"
    common = ["--exploration-root", str(exploration_root)]

    started = run_cli(
        "explore",
        "start",
        "--id",
        "capability-thread",
        "--model",
        "accepted",
        "--tools",
        "read,grep,find,ls,write",
        "--prompt",
        "Start with write access.",
        *common,
        env={"PI_BIN": str(fake_pi)},
    )
    assert started.returncode == 0, started.stderr

    resumed = run_cli(
        "explore",
        "resume",
        "--id",
        "capability-thread",
        "--tools",
        "read,grep,find,ls",
        "--prompt",
        "Continue read-only.",
        *common,
        env={"PI_BIN": str(fake_pi), "PI_ARGUMENTS_LOG": str(arguments_log)},
    )

    assert resumed.returncode == 0, resumed.stderr
    arguments = json.loads(arguments_log.read_text())
    assert arguments[arguments.index("--tools") + 1] == "read,grep,find,ls"
    manifest = json.loads((exploration_root / "capability-thread" / "manifest.json").read_text())
    assert manifest["tools"] == "read,grep,find,ls"


def test_exploration_timeout_retains_partial_diagnostics_and_observed_metadata(
    tmp_path: Path,
) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"

    result = run_cli(
        "explore",
        "start",
        "--id",
        "slow-thread",
        "--model",
        "accepted",
        "--prompt",
        "Explore slowly.",
        "--timeout-seconds",
        "1",
        "--exploration-root",
        str(exploration_root),
        env={
            "PI_BIN": str(fake_pi),
            "PI_SLEEP": "3",
            "PI_STDERR": "partial warning",
            "PI_TERM_STDERR": "exploration shutdown diagnostic",
        },
    )

    assert result.returncode == 1
    turn = Path(result.stdout.strip())
    metadata = json.loads((turn / "turn.json").read_text())
    assert metadata["conversationId"] == "pi-session"
    assert metadata["model"] == "accepted"
    assert metadata["provider"] == "openrouter"
    assert metadata["durationMs"] >= 1000
    assert "partial warning" in (turn / "stderr.log").read_text()
    assert "exploration shutdown diagnostic" in (turn / "stderr.log").read_text()
    assert "timed out" in metadata["diagnostic"]


@pytest.mark.parametrize(
    ("environment", "expected_diagnostic"),
    [
        ({"PI_SILENT": "1"}, ""),
        ({"PI_BIN": "missing"}, "Pi exploration executable not found"),
    ],
)
def test_exploration_does_not_manufacture_transport_artifacts(
    tmp_path: Path,
    environment: dict[str, str],
    expected_diagnostic: str,
) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"
    env = {"PI_BIN": str(fake_pi), **environment}
    if environment.get("PI_BIN") == "missing":
        env["PI_BIN"] = str(tmp_path / "missing-pi")

    result = run_cli(
        "explore",
        "start",
        "--id",
        "unavailable-thread",
        "--model",
        "accepted",
        "--prompt",
        "Explore without output.",
        "--exploration-root",
        str(exploration_root),
        env=env,
    )

    assert result.returncode == 1
    turn = Path(result.stdout.strip())
    metadata = json.loads((turn / "turn.json").read_text())
    assert metadata["status"] == "unavailable"
    assert expected_diagnostic in metadata["diagnostic"]
    manifest = json.loads((exploration_root / "unavailable-thread" / "manifest.json").read_text())
    expected_session = (
        None
        if environment.get("PI_BIN") == "missing"
        else str(exploration_root / "unavailable-thread" / "session.jsonl")
    )
    assert manifest["session"] == expected_session
    assert not (turn / "events.jsonl").exists()
    assert not (turn / "response.md").exists()
    assert not (turn / "stderr.log").exists()


def test_explore_show_and_promote_publish_working_material_not_an_approval(
    tmp_path: Path,
) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"
    start = run_cli(
        "explore",
        "start",
        "--id",
        "product-notes",
        "--model",
        "accepted",
        "--prompt",
        "Explore this idea.",
        "--exploration-root",
        str(exploration_root),
        env={"PI_BIN": str(fake_pi)},
    )
    assert start.returncode == 0, start.stderr

    shown = run_cli(
        "explore",
        "show",
        "--id",
        "product-notes",
        "--exploration-root",
        str(exploration_root),
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["turns"] == 1

    output = tmp_path / "promotion"
    promoted = run_cli(
        "explore",
        "promote",
        "--id",
        "product-notes",
        "--exploration-root",
        str(exploration_root),
        "--output",
        str(output),
    )

    assert promoted.returncode == 0, promoted.stderr
    assert (output / "exploration.md").read_text()
    prompt = (output / "prompt.md").read_text()
    assert "exploratory working material" in prompt
    assert "not an approval" in prompt
    assert json.loads((output / "manifest.json").read_text())["id"] == "product-notes"


def test_explore_promote_rejects_an_existing_output_file_without_traceback(
    tmp_path: Path,
) -> None:
    fake_pi = write_fake_pi(tmp_path)
    exploration_root = tmp_path / "explorations"
    started = run_cli(
        "explore",
        "start",
        "--id",
        "promotion-file",
        "--model",
        "accepted",
        "--prompt",
        "Explore this idea.",
        "--exploration-root",
        str(exploration_root),
        env={"PI_BIN": str(fake_pi)},
    )
    assert started.returncode == 0, started.stderr
    output = tmp_path / "existing-output"
    output.write_text("keep me")

    promoted = run_cli(
        "explore",
        "promote",
        "--id",
        "promotion-file",
        "--output",
        str(output),
        "--exploration-root",
        str(exploration_root),
    )

    assert promoted.returncode == 2
    assert "promotion output is not a directory" in promoted.stderr
    assert "Traceback" not in promoted.stderr
    assert output.read_text() == "keep me"


def test_help_llm_describes_the_exploration_and_formal_review_boundary() -> None:
    result = run_cli("help-llm")

    assert result.returncode == 0, result.stderr
    assert "reviewctl explore start" in result.stdout
    assert "reviewctl explore resume" in result.stdout
    assert "only when Pi produces them" in result.stdout
    assert "turn.json:diagnostic" in result.stdout
    assert "not an approval" in result.stdout
    assert "reviewctl run" in result.stdout


def test_help_llm_json_is_machine_readable() -> None:
    result = run_cli("help-llm", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["tool"] == "reviewctl"
    assert "explore" in payload["commands"]
    assert payload["commands"]["explore"]["promote"]["approval"] == "never"
    assert payload["commands"]["run"]["approval"] == (
        "only when receipt.result is accepted, acceptedAttempt names the accepted attempt, "
        "receipt verification succeeds, and material findings are independently checked"
    )
    assert payload["errors"]["exitCodes"]["1"]["meaning"] == "unavailable-or-invalid"
    assert payload["errors"]["exitCodes"]["0"]["next"] == (
        "follow the selected command's next step; only run creates a receipt"
    )
    assert payload["errors"]["attemptResults"]["incomplete"]["inspect"] == [
        "attempt.json:contractEvaluation.completionRequest",
        "attempt.json:promotedFragments",
        "receipt.json:fallbackRelationships",
        "attempt.json:rawResponse",
    ]
    assert payload["errors"]["attemptResults"]["transport-failed"]["inspect"] == [
        "attempt.json:exitCode",
        "attempt.json:diagnostic",
        "attempt.json:evidence.stderr when non-null",
    ]
    assert payload["errors"]["contractViolations"]["prepared-contract"] == (
        "prepared contract identity or packet context did not authenticate"
    )
    assert payload["errors"]["redaction"] == (
        "diagnostics are bounded and credential-shaped values are redacted"
    )
    assert payload["commands"]["setup"] == {
        "discover": "reviewctl setup discover --format json",
        "show": "reviewctl setup show --format json",
        "check": "reviewctl setup check --backend NAME --format json",
    }
    assert payload["backendSemantics"] == {
        "availabilityIsNotQualification": True,
        "setupIsLocalOnly": True,
        "setupCallsModels": False,
    }
    assert payload["nextActions"] == {
        "incomplete": {
            "inspect": [
                "attempt.json:contractEvaluation.completionRequest",
                "attempt.json:promotedFragments",
                "receipt.json:fallbackRelationships",
                "attempt.json:rawResponse",
            ]
        },
        "invalid": {
            "inspect": [
                "attempt.json:contractEvaluation.violations",
                "attempt.json:evaluationError",
                "attempt.json:rawResponse",
            ]
        },
        "accepted": {
            "inspect": [
                "receipt.json:verdict",
                "receipt.json:findings",
                "receipt.json:consolidatedReview",
            ],
            "run": "reviewctl verify RECEIPT.json",
        },
    }


def setup_topology(*installations: BackendInstallation) -> LocalExecutionTopology:
    return LocalExecutionTopology(
        schema_version=1,
        local_only=True,
        model_probe_performed=False,
        backends=installations,
    )


def setup_installation(
    name: str,
    availability: str,
    *,
    probe_performed: bool = True,
) -> BackendInstallation:
    executable = None if availability == "not-applicable" else name
    resolved = f"/tools/{name}" if availability in {"available", "unverified"} else None
    version = f"{name} 1.2.3" if availability == "available" else None
    diagnostics = () if availability in {"available", "not-applicable"} else ("local diagnostic",)
    return BackendInstallation(
        name=name,
        requested_executable=executable,
        resolved_executable=resolved,
        version=version,
        availability=availability,
        qualification="unqualified",
        diagnostics=diagnostics,
        probe_performed=probe_performed,
    )


def invoke_setup(
    monkeypatch: pytest.MonkeyPatch,
    topology: LocalExecutionTopology,
    *arguments: str,
) -> int:
    monkeypatch.setattr(cli, "discover_topology", lambda registry, environ: topology, raising=False)
    parser = cli.build_parser()
    namespace = parser.parse_args(["setup", *arguments])
    return namespace.handler(namespace)


def test_setup_discover_and_show_return_the_same_stable_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    topology = setup_topology(
        setup_installation("codex", "available"),
        setup_installation("openrouter", "not-applicable", probe_performed=False),
    )

    outputs = []
    for command in ("discover", "show"):
        assert invoke_setup(monkeypatch, topology, command, "--format", "json") == 0
        outputs.append(capsys.readouterr().out)

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == {
        "schemaVersion": 1,
        "localOnly": True,
        "modelProbePerformed": False,
        "backends": [
            {
                "name": "codex",
                "requestedExecutable": "codex",
                "resolvedExecutable": "/tools/codex",
                "version": "codex 1.2.3",
                "availability": "available",
                "qualification": "unqualified",
                "diagnostics": [],
                "probePerformed": True,
            },
            {
                "name": "openrouter",
                "requestedExecutable": None,
                "resolvedExecutable": None,
                "version": None,
                "availability": "not-applicable",
                "qualification": "unqualified",
                "diagnostics": [],
                "probePerformed": False,
            },
        ],
    }


def test_setup_human_output_is_concise_and_distinguishes_backend_states(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    topology = setup_topology(
        setup_installation("codex", "available"),
        setup_installation("openrouter", "not-applicable", probe_performed=False),
    )

    assert invoke_setup(monkeypatch, topology, "show", "--format", "human") == 0

    output = capsys.readouterr().out
    assert "local-only: yes" in output
    assert "model probes: no" in output
    assert "codex: availability=available qualification=unqualified" in output
    assert "openrouter: availability=not-applicable qualification=unqualified" in output


def test_setup_human_output_escapes_terminal_controls_without_changing_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    version = "codex 1.2\x1b[31m red\x1b[0m\r\nforged\tfield\x9b2J"
    diagnostic = "warning\x1b]52;c;Y2xpcA==\x07\r\nforged\tline\x00\x80"
    topology = setup_topology(
        BackendInstallation(
            name="codex",
            requested_executable="codex",
            resolved_executable="/tools/codex",
            version=version,
            availability="available",
            qualification="unqualified",
            diagnostics=(diagnostic,),
            probe_performed=True,
        )
    )

    cli.print_setup_topology(topology, "human")

    human_output = capsys.readouterr().out
    assert human_output.splitlines() == [
        "local-only: yes",
        "model probes: no",
        (
            "codex: availability=available qualification=unqualified "
            "version=codex 1.2\\x1b[31m red\\x1b[0m\\r\\nforged\\tfield\\x9b2J"
        ),
        "  diagnostic: warning\\x1b]52;c;Y2xpcA==\\x07\\r\\nforged\\tline\\x00\\x80",
    ]
    assert not any(
        (ord(character) < 32 and character != "\n") or 0x7F <= ord(character) <= 0x9F
        for character in human_output
    )

    cli.print_setup_topology(topology, "json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["backends"][0]["version"] == version
    assert payload["backends"][0]["diagnostics"] == [diagnostic]


def test_setup_human_text_escapes_all_surrogate_code_points() -> None:
    value = "readable \ud800 \udfff surrogateescape \udc80 \udcff"

    assert cli.sanitize_setup_human_text(value) == (
        "readable \\ud800 \\udfff surrogateescape \\udc80 \\udcff"
    )


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "environb"),
    reason="requires POSIX bytes environment support",
)
def test_setup_human_subprocess_does_not_recreate_raw_environment_control_byte() -> None:
    environment = os.environb.copy()
    environment[b"PATH"] = b""
    environment[b"CODEX_BIN"] = b"missing-\x9b-tool"

    result = subprocess.run(
        [
            os.fsencode(sys.executable),
            b"-m",
            b"reviewctl",
            b"setup",
            b"discover",
            b"--format",
            b"human",
        ],
        cwd=os.fsencode(REPOSITORY),
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert b"missing-\\udc9b-tool" in result.stdout
    assert not any((byte < 0x20 and byte != 0x0A) or 0x7F <= byte <= 0x9F for byte in result.stdout)


@pytest.mark.parametrize(
    ("name", "availability", "probe_performed", "expected_exit"),
    [
        ("codex", "available", True, 0),
        ("codex", "missing", False, 1),
        ("codex", "unverified", True, 1),
        ("openrouter", "not-applicable", False, 1),
    ],
)
def test_setup_check_explicit_backend_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    availability: str,
    probe_performed: bool,
    expected_exit: int,
) -> None:
    topology = setup_topology(
        setup_installation(name, availability, probe_performed=probe_performed)
    )

    assert (
        invoke_setup(monkeypatch, topology, "check", "--backend", name, "--format", "json")
        == expected_exit
    )

    payload = json.loads(capsys.readouterr().out)
    assert [backend["name"] for backend in payload["backends"]] == [name]
    assert payload["backends"][0]["availability"] == availability
    assert payload["backends"][0]["qualification"] == "unqualified"
    assert payload["modelProbePerformed"] is False
    assert payload["backends"][0]["probePerformed"] is probe_performed


def test_setup_check_all_ignores_remote_not_applicable_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    topology = setup_topology(
        setup_installation("codex", "available"),
        setup_installation("openrouter", "not-applicable", probe_performed=False),
        setup_installation("pi", "available"),
    )

    assert invoke_setup(monkeypatch, topology, "check", "--format", "human") == 0
    assert (
        "openrouter: availability=not-applicable qualification=unqualified"
        in capsys.readouterr().out
    )


def test_setup_check_repeatable_selection_filters_output_and_checks_every_selected_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    topology = setup_topology(
        setup_installation("agy", "available"),
        setup_installation("codex", "missing", probe_performed=False),
        setup_installation("pi", "available"),
    )

    assert (
        invoke_setup(
            monkeypatch,
            topology,
            "check",
            "--backend",
            "agy",
            "--backend",
            "codex",
            "--format",
            "json",
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert [backend["name"] for backend in payload["backends"]] == ["agy", "codex"]


def test_setup_check_all_fails_when_any_local_executable_is_not_available(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    topology = setup_topology(
        setup_installation("codex", "available"),
        setup_installation("llm", "unverified"),
        setup_installation("openrouter", "not-applicable", probe_performed=False),
    )

    assert invoke_setup(monkeypatch, topology, "check", "--format", "human") == 1
    assert "llm: availability=unverified qualification=unqualified" in capsys.readouterr().out


def test_setup_does_not_print_credential_shaped_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "credential-value-that-must-not-appear"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    topology = setup_topology(
        setup_installation("openrouter", "not-applicable", probe_performed=False)
    )

    assert invoke_setup(monkeypatch, topology, "discover", "--format", "json") == 0

    output = capsys.readouterr().out
    assert "OPENROUTER_API_KEY" not in output
    assert secret not in output


@pytest.mark.parametrize(
    "arguments",
    [
        ("setup",),
        ("setup", "--help"),
        ("setup", "discover", "--help"),
        ("setup", "show", "--help"),
        ("setup", "check", "--help"),
    ],
)
def test_setup_parser_requires_and_documents_subcommands(arguments: tuple[str, ...]) -> None:
    result = run_cli(*arguments)

    if arguments == ("setup",):
        assert result.returncode == 2
        assert "the following arguments are required" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "--format" in result.stdout
        if arguments[-2:] == ("check", "--help"):
            assert "--backend" in result.stdout


def test_setup_check_rejects_unknown_backend_as_invocation_error() -> None:
    result = run_cli("setup", "check", "--backend", "unknown")

    assert result.returncode == 2
    assert "invalid choice: 'unknown'" in result.stderr


def test_help_llm_document_describes_non_qualifying_local_setup_diagnostics() -> None:
    document = (REPOSITORY / "docs" / "HELP-LLM.md").read_text().lower()

    for command in (
        "reviewctl setup discover --format json",
        "reviewctl setup show --format json",
        "reviewctl setup check --backend name --format json",
    ):
        assert command in document
    for boundary in ("local", "read-only", "redacted", "non-qualifying"):
        assert boundary in document


def test_help_llm_markdown_explains_how_to_diagnose_a_failed_attempt() -> None:
    result = run_cli("help-llm")

    assert result.returncode == 0, result.stderr
    assert "## Diagnose failures" in result.stdout
    assert "contractEvaluation.violations" in result.stdout
    assert "reviewctl verify RECEIPT.json" in result.stdout
    assert "Do not retry blindly" in result.stdout


def test_route_profile_loads_ordered_fallback_and_records_config_digest(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    fake_llm = write_fake_llm(tmp_path)
    config = tmp_path / "reviewctl.toml"
    config.write_text(
        "[profiles.gemini]\n"
        'routes = ["agy:gemini-3.6-flash-high", "llm:accepted"]\n'
        "timeout_seconds = 600\n"
        "max_attempts = 2\n"
    )

    result = run_cli(
        *review_arguments(tmp_path),
        "--profile",
        "gemini",
        "--config",
        str(config),
        env={
            "AGY_BIN": str(fake_agy),
            "AGY_STATUS": "QUOTA_EXCEEDED",
            "LLM_BIN": str(fake_llm),
        },
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["routeProfile"]["name"] == "gemini"
    assert receipt["routeProfile"]["path"] == str(config.resolve())
    assert receipt["routeProfile"]["sha256"] == cli.sha256_bytes(config.read_bytes())
    assert receipt["routeProfile"]["settings"] == {
        "timeout_seconds": 600,
        "max_attempts": 2,
    }
    assert receipt["executionSettings"] == {
        "timeoutSeconds": 5,
        "maxAttempts": 2,
    }
    assert receipt["routes"] == [
        {"model": "gemini-3.6-flash-high", "transport": "agy"},
        {"model": "accepted", "transport": "llm"},
    ]


def test_route_profile_applies_execution_settings_when_cli_omits_them(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    config = tmp_path / "reviewctl.toml"
    config.write_text(
        '[profiles.code]\nroutes = ["llm:accepted"]\ntimeout_seconds = 600\nmax_attempts = 2\n'
    )
    arguments = review_arguments(tmp_path)
    arguments.remove("--timeout-seconds")
    arguments.remove("5")

    result = run_cli(
        *arguments,
        "--profile",
        "code",
        "--config",
        str(config),
        env={"LLM_BIN": str(fake_llm)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["executionSettings"] == {
        "timeoutSeconds": 600,
        "maxAttempts": 2,
    }


def test_direct_transport_applies_configured_transport_defaults(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    config = tmp_path / "reviewctl.toml"
    config.write_text("[defaults.llm]\ntimeout_seconds = 600\nmax_attempts = 2\n")
    arguments = review_arguments(tmp_path, "accepted")
    arguments.remove("--timeout-seconds")
    arguments.remove("5")

    result = run_cli(
        *arguments,
        "--config",
        str(config),
        env={"LLM_BIN": str(fake_llm)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["executionSettings"] == {
        "timeoutSeconds": 600,
        "maxAttempts": 2,
    }
    assert receipt["executionConfig"]["path"] == str(config.resolve())


def test_mixed_routes_do_not_apply_the_first_transports_defaults(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    fake_pi = write_fake_pi(tmp_path)
    config = tmp_path / "reviewctl.toml"
    config.write_text(
        "[profiles.mixed]\n"
        'routes = ["agy:gemini-3.6-flash-high", "pi:openrouter/accepted"]\n'
        "[defaults.agy]\n"
        "timeout_seconds = 111\n"
        "[defaults.pi]\n"
        "timeout_seconds = 222\n"
    )
    arguments = review_arguments(tmp_path)
    arguments.remove("--timeout-seconds")
    arguments.remove("5")

    result = run_cli(
        *arguments,
        "--profile",
        "mixed",
        "--config",
        str(config),
        "--response-contract",
        "findings-json",
        env={
            "AGY_BIN": str(fake_agy),
            "AGY_STATUS": "QUOTA_EXCEEDED",
            "PI_BIN": str(fake_pi),
        },
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["executionSettings"]["timeoutSeconds"] == 90
    assert receipt["executionConfig"]["path"] == str(config.resolve())


def test_pi_request_marks_output_token_limit_as_unenforced(tmp_path: Path) -> None:
    fake_pi = write_fake_pi(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "pi:openrouter/accepted",
        "--max-output-tokens",
        "1",
        "--response-contract",
        "findings-json",
        env={"PI_BIN": str(fake_pi)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    request = json.loads(Path(receipt["attempts"][0]["evidence"]["request"]).read_text())
    assert request["requestedMaxOutputTokens"] == 1
    assert request["outputTokenLimitEnforced"] is False
    assert "maxOutputTokens" not in request


def test_route_profile_cannot_be_combined_with_explicit_model(tmp_path: Path) -> None:
    config = tmp_path / "reviewctl.toml"
    config.write_text('[profiles.gemini]\nroutes = ["llm:accepted"]\n')

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--profile",
        "gemini",
        "--config",
        str(config),
    )

    assert result.returncode == 2
    assert "use --profile or --model/--route, not both" in result.stderr


@pytest.mark.parametrize(
    ("config_text", "profile"),
    [
        (None, "missing"),
        ("not = [valid", "broken"),
        ("[profiles.empty]\n", "empty"),
        ('[profiles.invalid]\nroutes = ["bad-route"]\n', "invalid"),
        (
            '[profiles.invalid-timeout]\nroutes = ["llm:accepted"]\ntimeout_seconds = 0\n',
            "invalid-timeout",
        ),
        (
            '[profiles.invalid-attempts]\nroutes = ["llm:accepted"]\nmax_attempts = 4\n',
            "invalid-attempts",
        ),
    ],
)
def test_route_profile_rejects_unusable_configurations(
    tmp_path: Path, config_text: str | None, profile: str
) -> None:
    config = tmp_path / f"{profile}.toml"
    if config_text is not None:
        config.write_text(config_text)

    with pytest.raises(SystemExit) as error:
        cli.load_route_profile(cli.build_parser(), str(config), profile)

    assert error.value.code == 2


def test_route_resolution_rejects_conflicting_or_malformed_routes() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as conflict:
        cli.review_routes(
            parser,
            argparse.Namespace(routes=["llm:accepted"], models=["other"], profile=None),
        )
    assert conflict.value.code == 2

    with pytest.raises(SystemExit) as malformed:
        cli.review_routes(
            parser,
            argparse.Namespace(routes=["not-a-route"], models=[], profile=None),
        )
    assert malformed.value.code == 2


def test_run_rejects_provider_preferences_on_non_openrouter_route(tmp_path: Path) -> None:
    result = run_cli(
        *review_arguments(tmp_path),
        "--route",
        "llm:accepted",
        "--provider-sort",
        "price",
    )

    assert result.returncode == 2
    assert "provider preferences require at least one openrouter route" in result.stderr


def test_accepted_response_can_be_written_as_a_document(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    output = tmp_path / "docs" / "review.md"

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--output-file",
        str(output),
        env={"LLM_BIN": str(fake_llm)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert output.read_text() == "VERDICT: approved\n1. No blocking findings."
    assert receipt["output"] == {
        "path": str(output.resolve()),
        "sha256": cli.sha256_bytes(output.read_bytes()),
        "characters": len(output.read_text()),
    }


def test_document_contract_accepts_markdown_without_a_verdict(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    output = tmp_path / "docs" / "architecture.md"
    prompt = tmp_path / "document-prompt.md"
    prompt.write_text("Write a concise architecture note.")

    result = run_cli(
        "run",
        "--review-id",
        "document-contract",
        "--prompt-file",
        str(prompt),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--model",
        "documented",
        "--response-contract",
        "document",
        "--output-file",
        str(output),
        env={
            "LLM_BIN": str(fake_llm),
            "LLM_DOCUMENT_RESPONSE": "# Architecture\n\nA bounded document.",
        },
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text() == "# Architecture\n\nA bounded document."


def test_runtime_diagnostics_rotate_and_redact_credentials(tmp_path: Path) -> None:
    log = tmp_path / "reviewctl.log"
    logger = cli.configure_runtime_logger(log, max_bytes=180, backup_count=2)
    for index in range(20):
        cli.log_event(
            logger,
            "attempt_finished",
            diagnostic=f"Authorization: Bearer secret-{index} " + ("x" * 80),
        )
    for handler in logger.handlers:
        handler.flush()

    assert log.is_file()
    assert (tmp_path / "reviewctl.log.1").is_file()
    assert "secret-" not in "".join(path.read_text() for path in tmp_path.glob("reviewctl.log*"))


def test_rejects_an_invalid_run_attempt_limit(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--max-attempts",
        "0",
        env={"LLM_BIN": str(fake_llm)},
    )

    assert result.returncode == 2
    assert "max attempts must be an integer from 1 to 3" in result.stderr


def test_codex_transport_uses_an_isolated_snapshot_and_receipt(tmp_path: Path) -> None:
    fake_codex_root = tmp_path.parent / "codex-bin"
    fake_codex_root.mkdir()
    arguments_log = tmp_path.parent / "codex-arguments.json"
    fake_codex = write_fake_codex(fake_codex_root, arguments_log=arguments_log)

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--source-class",
        "proprietary",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 0, result.stderr
    turn = Path(result.stdout.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    arguments = json.loads(arguments_log.read_text())
    workspace = arguments[arguments.index("-C") + 1]
    assert receipt["model"]["resolved"] == "gpt-5.6-terra"
    assert receipt["response"]["conversationId"] == "codex-conversation"
    assert receipt["response"]["provider"] is None
    assert receipt["attempts"][0]["provider"]["resolved"] is None
    assert "--ignore-user-config" in arguments
    assert "--ignore-rules" in arguments
    assert "--ephemeral" in arguments
    assert "--dangerously-bypass-approvals-and-sandbox" in arguments
    assert "--sandbox" not in arguments
    assert Path(workspace).name.startswith("reviewctl-input-")
    assert not list(turn.glob("**/codex-response.md"))
    response_path = Path(receipt["attempts"][0]["evidence"]["response"])
    assert response_path.is_file()
    assert response_path.read_text()
    verified = run_cli("verify", str(turn / "receipt.json"))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["violations"] == []


def test_codex_transport_enforces_the_structured_findings_contract(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [
                {
                    "severity": "high",
                    "path": "source.py",
                    "line": 1,
                    "title": "Idempotency defect",
                    "evidence": "The synthetic fixture admits a duplicate.",
                    "reproduction": "Submit it twice.",
                }
            ],
            "reviewedFiles": ["source.py"],
        }
    )
    fake_codex_root = tmp_path.parent / "structured-codex-bin"
    fake_codex_root.mkdir()
    fake_codex = write_fake_codex(fake_codex_root, response=response)

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["sourceClass"] == "proprietary"
    assert receipt["findings"][0]["path"] == "source.py"
    evaluation = receipt["attempts"][0]["contractEvaluation"]
    assert evaluation["contractContext"] == {
        "fileNames": ["source.py"],
        "reviewDeclarationRequired": True,
    }
    assert evaluation["coverage"]["requiredFields"] == [
        "verdict",
        "findings",
        "reviewedFiles",
    ]
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr


def test_generated_v2_receipt_canonicalizes_reversed_review_declaration(
    tmp_path: Path,
) -> None:
    extra_source = tmp_path / "alpha.py"
    extra_source.write_text("def alpha() -> None: pass\n")
    response = json.dumps(
        {
            "verdict": "approved",
            "findings": [],
            "reviewedFiles": ["source.py", "alpha.py"],
        }
    )
    fake_codex_root = tmp_path.parent / "ordered-declaration-codex-bin"
    fake_codex_root.mkdir()
    fake_codex = write_fake_codex(fake_codex_root, response=response)
    arguments = review_arguments(tmp_path, "gpt-5.6-terra")
    arguments.extend(("--file", str(extra_source)))

    result = run_cli(
        *arguments,
        "--transport",
        "codex",
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    normalized = receipt["attempts"][0]["contractEvaluation"]["normalizedValue"]
    assert normalized["reviewedFiles"] == ["alpha.py", "source.py"]
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["violations"] == []


def test_generated_v2_receipt_with_unicode_findings_verifies(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [
                {
                    "severity": "high",
                    "path": "source.py",
                    "line": 1,
                    "title": "Condición inválida",
                    "evidence": "La revisión encontró una condición inválida.",
                    "reproduction": "Ejecuta el caso límite otra vez.",
                }
            ],
            "reviewedFiles": ["source.py"],
        }
    )
    fake_codex_root = tmp_path.parent / "unicode-codex-bin"
    fake_codex_root.mkdir()
    fake_codex = write_fake_codex(fake_codex_root, response=response)

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["findings"][0]["evidence"].startswith("La revisión")
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["violations"] == []


def test_codex_transport_reads_the_session_identifier_from_stderr(tmp_path: Path) -> None:
    fake_codex = write_fake_codex(tmp_path, session_on_stderr=True)

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["response"]["conversationId"] == "codex-conversation"


def test_codex_transport_rejects_a_model_substituted_by_the_provider(tmp_path: Path) -> None:
    fake_codex = write_fake_codex(tmp_path, resolved_model="gpt-5.6-luna")

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["attempts"][0]["result"] == "model-mismatch"
    assert receipt["attempts"][0]["model"]["resolved"] == "gpt-5.6-luna"


def test_findings_schema_is_portable_for_external_reviewers() -> None:
    assert cli.FINDINGS_SCHEMA["additionalProperties"] is False
    assert cli.FINDINGS_SCHEMA["properties"]["findings"]["items"]["additionalProperties"] is False
    assert cli.FINDINGS_SCHEMA["properties"]["verdict"]["enum"] == [
        "approved",
        "changes-requested",
    ]
    assert cli.FINDINGS_SCHEMA["required"] == ["verdict", "findings"]
    assert "reviewedFiles" not in cli.FINDINGS_SCHEMA["properties"]


def test_findings_schema_and_transport_instructions_delegate_to_native_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contexts: list[object] = []

    class StubContract:
        def prepare(self, context: object) -> SimpleNamespace:
            contexts.append(context)
            return SimpleNamespace(
                schema={"native": context.review_declaration_required},  # type: ignore[attr-defined]
                output_instructions=(
                    "NATIVE CONTRACT DECLARATION"
                    if context.review_declaration_required  # type: ignore[attr-defined]
                    else "NATIVE CONTRACT PORTABLE"
                ),
            )

    monkeypatch.setattr(cli, "get_contract", lambda name: StubContract())
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    assert cli.response_schema("findings-json") == {"native": False}
    assert cli.response_schema("findings-json", codex=True) == {"native": True}
    assert "NATIVE CONTRACT PORTABLE" in cli.openrouter_packet("Review", [source])
    assert "NATIVE CONTRACT DECLARATION" in cli.codex_prompt("Review", "findings-json")
    assert len(contexts) == 4


@pytest.mark.parametrize(
    "response",
    [
        json.dumps({"verdict": "unavailable", "findings": []}),
        json.dumps({"verdict": "approved", "findings": [{"path": "source.py"}]}),
        json.dumps({"verdict": "changes-requested", "findings": []}),
    ],
)
def test_findings_contract_rejects_non_verdicts_and_incoherent_findings(response: str) -> None:
    assert cli.validate_review_response(response, "findings-json") is None


def test_codex_receipt_records_why_a_structured_response_is_rejected(tmp_path: Path) -> None:
    malformed = json.dumps(
        {
            "verdict": "changes_requested",
            "findings": [
                {
                    "severity": "high",
                    "path": "source.py",
                    "line": 1,
                    "title": "Wrong verdict spelling",
                    "evidence": "The producer used an underscore.",
                    "reproduction": "Return changes_requested.",
                }
            ],
        }
    )
    fake_codex = write_fake_codex(tmp_path, response=malformed)

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--source-class",
        "synthetic",
        "--response-contract",
        "findings-json",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["attempts"][0]["result"] == "incomplete"
    assert receipt["attempts"][0]["validationError"] == (
        "findings-json: invalid verdict 'changes_requested'; expected approved or changes-requested"
    )


def test_synthetic_codex_review_does_not_require_a_read_proof(tmp_path: Path) -> None:
    arguments_log = tmp_path / "codex-arguments.json"
    response = json.dumps({"verdict": "approved", "findings": []})
    fake_codex = write_fake_codex(
        tmp_path,
        arguments_log=arguments_log,
        response=response,
        skip_read_proof=True,
    )

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--response-contract",
        "findings-json",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    arguments = json.loads(arguments_log.read_text())
    assert arguments[arguments.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in arguments
    assert receipt["result"] == "accepted"
    assert receipt["findings"] == []
    assert receipt["sourceClass"] == "synthetic"
    assert receipt["attempts"][0]["contractEvaluation"]["contractContext"] == {
        "fileNames": ["source.py"],
        "reviewDeclarationRequired": False,
    }
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr


def test_findings_contract_rejects_a_severity_outside_the_shared_taxonomy() -> None:
    response = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [
                {
                    "severity": "error",
                    "path": "source.py",
                    "line": 1,
                    "title": "Wrong severity vocabulary",
                    "evidence": "The contract is scored against named levels.",
                    "reproduction": "Return error rather than high.",
                }
            ],
        }
    )

    assert cli.validate_review_response(response, "findings-json") is None


def test_findings_schema_exposes_the_shared_severity_taxonomy() -> None:
    severity = cli.FINDINGS_SCHEMA["properties"]["findings"]["items"]["properties"]["severity"]

    assert severity == {"type": "string", "enum": ["critical", "high", "info", "low", "medium"]}


def test_findings_contract_accepts_an_external_response_without_read_proof() -> None:
    response = json.dumps({"verdict": "approved", "findings": []})

    assert cli.validate_review_response(response, "findings-json") == {
        "verdict": "approved",
        "findings": [],
    }


def test_review_validation_error_explains_every_structured_contract_boundary() -> None:
    """Rejected receipts must explain the contract failure without accepting bad output."""
    expected_file_hashes = {"brief.md": "a" * 64}
    approved = json.dumps({"verdict": "approved", "findings": []})
    product = product_review_payload()
    product["reviewedFiles"] = ["invented.md"]
    invalid_product = product_review_payload()
    invalid_product["summary"] = ""
    invalid_product["reviewedFiles"] = ["brief.md"]
    judge = {
        "scores": {
            "delivery": 4,
            "domainIntegrity": 4,
            "operationalCorrectness": 4,
            "problemFidelity": 4,
            "scopeDiscipline": 4,
        },
        "hardConstraintViolations": [],
        "rationale": "The proposal is complete.",
        "reviewedFiles": ["invented.md"],
    }

    assert cli.review_validation_error(approved, "findings-json") is None
    assert cli.review_validation_error("", "document") == (
        "document: response is empty or shorter than 20 characters"
    )
    assert cli.review_validation_error("[]", "findings-json") == (
        "findings-json: top-level response must be an object"
    )
    assert (
        cli.review_validation_error(
            json.dumps(product), "product-review-json", expected_file_hashes=expected_file_hashes
        )
        == "product-review-json: reviewedFiles proof does not match frozen inputs"
    )
    assert (
        cli.review_validation_error(
            json.dumps(judge), "product-judge-json", expected_file_hashes=expected_file_hashes
        )
        == "product-judge-json: reviewedFiles proof does not match frozen inputs"
    )
    assert (
        cli.review_validation_error(
            json.dumps(invalid_product),
            "product-review-json",
            expected_file_hashes=expected_file_hashes,
        )
        == "product-review-json: response does not satisfy the required schema"
    )
    assert (
        cli.review_validation_error(json.dumps({"verdict": 4, "findings": []}), "findings-json")
        == "findings-json: verdict must be a string"
    )
    assert (
        cli.review_validation_error(
            approved, "findings-json", expected_file_hashes=expected_file_hashes
        )
        == "findings-json: response fields do not match the required schema"
    )
    assert (
        cli.review_validation_error(
            json.dumps({"verdict": "changes-requested", "findings": []}), "findings-json"
        )
        == "findings-json: findings do not satisfy the required schema or verdict invariant"
    )


def product_review_payload() -> dict[str, object]:
    """Return the smallest complete product-design response for contract tests."""
    return {
        "summary": "A deterministic flow-design workspace for accounting mappings.",
        "userJobs": ["Model a business event before it can post."],
        "mvp": ["Versioned mapping simulation."],
        "nonGoals": ["Do not execute provider effects from simulation."],
        "interactionFlow": [
            {"actor": "operator", "action": "simulate", "outcome": "explain trace"}
        ],
        "domainEntities": [
            {"name": "MappingVersion", "purpose": "Immutable accounting rule definition."}
        ],
        "stateTransitions": [{"from": "draft", "to": "approved", "guard": "fixtures pass"}],
        "architecture": [
            {
                "boundary": "runtime",
                "owns": "posting receipt",
                "commands": ["executeMapping"],
                "events": ["posting.recorded"],
                "readModels": ["mapping explain trace"],
            }
        ],
        "operationalControls": [{"control": "idempotency", "approach": "event identity is unique"}],
        "constraintChecks": [
            {
                "constraintId": "simulation-no-side-effects",
                "disposition": "satisfied",
                "rationale": "Simulation emits no provider command.",
            }
        ],
        "risks": ["A stale mapping version could be promoted."],
        "acceptanceTests": ["Simulation cannot create a posting receipt."],
        "openQuestions": [],
    }


def write_verified_receipt(path: Path, payload: dict[str, object]) -> None:
    """Write a receipt accepted by the same integrity check as production evidence."""
    payload["sha256"] = cli.sha256_bytes(cli.canonical_json(payload))
    path.write_text(json.dumps(payload))


def test_product_review_contract_accepts_a_complete_structured_design() -> None:
    payload = product_review_payload()

    assert cli.validate_review_response(json.dumps(payload), "product-review-json") == payload


def test_product_review_schema_requires_the_same_non_empty_sections_as_the_validator() -> None:
    properties = cli.PRODUCT_REVIEW_SCHEMA["properties"]

    for field in (
        "userJobs",
        "mvp",
        "nonGoals",
        "interactionFlow",
        "domainEntities",
        "stateTransitions",
        "architecture",
        "operationalControls",
        "constraintChecks",
        "risks",
        "acceptanceTests",
    ):
        assert properties[field]["minItems"] == 1
    assert "minItems" not in properties["openQuestions"]


def test_product_review_contract_allows_a_passive_architecture_boundary() -> None:
    payload = product_review_payload()
    payload["architecture"].append(  # type: ignore[union-attr]
        {
            "boundary": "data store",
            "owns": "durable records",
            "commands": [],
            "events": [],
            "readModels": [],
        }
    )

    assert cli.validate_product_review(payload) == payload


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("constraintChecks"),
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update(
            {
                "constraintChecks": [
                    {
                        "constraintId": "simulation-no-side-effects",
                        "disposition": "ignored",
                        "rationale": "No decision was made.",
                    }
                ]
            }
        ),
    ],
)
def test_product_review_contract_rejects_incomplete_or_incoherent_designs(
    mutation: object,
) -> None:
    payload = product_review_payload()
    mutation(payload)  # type: ignore[operator]

    assert cli.validate_review_response(json.dumps(payload), "product-review-json") is None


def test_product_judge_contract_requires_scores_and_explicit_hard_violations() -> None:
    payload = {
        "scores": {
            "delivery": 4,
            "domainIntegrity": 4,
            "operationalCorrectness": 4,
            "problemFidelity": 4,
            "scopeDiscipline": 4,
        },
        "hardConstraintViolations": [],
        "rationale": "The design satisfies every stated invariant.",
    }

    assert cli.validate_review_response(json.dumps(payload), "product-judge-json") == payload
    payload["scores"] = {"problemFidelity": 5}
    assert cli.validate_review_response(json.dumps(payload), "product-judge-json") is None


def test_usage_synthetic_prompt_only_product_review(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Design the product from the synthetic briefing.")
    payload = product_review_payload()

    result = run_cli(
        "run",
        "--review-id",
        "product.packet",
        "--prompt-file",
        str(prompt),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--model",
        "gemini-3.6-flash-high",
        "--transport",
        "agy",
        "--response-contract",
        "product-review-json",
        env={"AGY_BIN": str(fake_agy), "AGY_RESPONSE": json.dumps(payload)},
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["receiptSchemaVersion"] == 2
    assert receipt["contract"] == {
        "name": "product-review-json",
        "version": "legacy-1",
    }
    assert receipt["attempts"][0]["number"] == 1
    assert receipt["review"] == payload
    output = receipt["attempts"][0]["contractOutput"]
    assert output == {
        "name": "product-review-json",
        "version": "legacy-1",
        "status": "complete",
        "normalizedSha256": cli.sha256_bytes(cli.canonical_json(receipt["review"])),
        "contractContext": {
            "fileNames": ["prompt.md"],
            "reviewDeclarationRequired": False,
        },
    }
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == {
        "receipt": str(receipt_path),
        "valid": True,
        "violations": [],
    }


@pytest.mark.parametrize(
    "contract", ["verdict", "document", "product-review-json", "product-judge-json"]
)
def test_generated_unavailable_non_findings_v2_receipt_verifies(
    tmp_path: Path, contract: str
) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path, "missing"),
        "--response-contract",
        contract,
        env={"LLM_BIN": str(fake_llm)},
    )

    assert result.returncode == 1
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["receiptSchemaVersion"] == 2
    assert receipt["result"] == "unavailable"
    assert receipt["acceptedAttempt"] is None
    assert receipt["contract"] == {"name": contract, "version": "legacy-1"}
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["violations"] == []
    assert "findings" not in receipt


def test_parses_mixed_tournament_candidates_and_preserves_cost_modes() -> None:
    candidates = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "flash",
                    "family": "deepseek",
                    "model": "openrouter/deepseek/deepseek-v4-flash-0731",
                    "transport": "openrouter",
                    "cost_mode": "metered",
                    "pricing": {"input_per_million_usd": 0.09, "output_per_million_usd": 0.18},
                    "max_output_tokens": 32000,
                },
                {
                    "id": "codex-terra",
                    "family": "openai",
                    "model": "gpt-5.6-terra",
                    "transport": "codex",
                    "cost_mode": "account-included",
                },
                {
                    "id": "gemini-high",
                    "family": "gemini",
                    "model": "gemini-3.6-flash-high",
                    "transport": "agy",
                    "cost_mode": "subscription",
                },
            ]
        }
    )

    assert [candidate.identifier for candidate in candidates] == [
        "flash",
        "codex-terra",
        "gemini-high",
    ]
    assert candidates[0].pricing == (0.09, 0.18)
    assert candidates[0].max_output_tokens == 32000
    assert all(candidate.council_eligible for candidate in candidates)
    assert candidates[1].cost_mode == "account-included"
    assert candidates[2].transport == "agy"


@pytest.mark.parametrize(
    "candidate",
    [
        {
            "id": "missing-pricing",
            "family": "test",
            "model": "openrouter/test",
            "transport": "openrouter",
            "cost_mode": "metered",
        },
        {
            "id": "invalid-provider",
            "family": "test",
            "model": "gpt-5.6-terra",
            "transport": "codex",
            "cost_mode": "account-included",
            "provider": {"only": ["test"]},
        },
    ],
)
def test_rejects_tournament_candidates_with_incompatible_cost_or_provider_policy(
    candidate: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        cli.parse_tournament_candidates({"candidates": [candidate]})


@pytest.mark.parametrize("max_output_tokens", [0, -1, True, "12000"])
def test_rejects_invalid_candidate_output_token_caps(max_output_tokens: object) -> None:
    with pytest.raises(ValueError, match="max_output_tokens must be a positive integer"):
        cli.parse_tournament_candidates(
            {
                "candidates": [
                    {
                        "id": "flash",
                        "family": "deepseek",
                        "model": "openrouter/deepseek/test",
                        "transport": "openrouter",
                        "cost_mode": "metered",
                        "pricing": {"input_per_million_usd": 0.09, "output_per_million_usd": 0.18},
                        "max_output_tokens": max_output_tokens,
                    }
                ]
            }
        )


def test_product_tournament_runs_mixed_native_and_metered_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_llm = write_fake_llm(tmp_path)
    fake_agy = write_fake_agy(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("Synthetic product brief.\n")
    payload = json.dumps(product_review_payload())
    fake_codex = write_fake_codex(tmp_path, response=payload)
    tournament = tmp_path / "product.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 200
response_contract = "product-review-json"
artifact_root = "{tmp_path / "artifacts"}"

[[candidates]]
id = "metered"
family = "deepseek"
model = "openrouter/deepseek/test"
transport = "openrouter"
cost_mode = "metered"
pricing = {{ input_per_million_usd = 0.1, output_per_million_usd = 0.2 }}
max_output_tokens = 400

[[candidates]]
id = "codex"
family = "openai"
model = "gpt-5.6-terra"
transport = "codex"
cost_mode = "account-included"

[[candidates]]
id = "gemini"
family = "gemini"
model = "gemini-3.6-flash-high"
transport = "agy"
cost_mode = "subscription"

[[cases]]
id = "flow"
stage = "filter"
prompt = "Design this product from the synthetic brief."
files = ["{brief}"]
'''
    )

    observed_max_output_tokens: list[object] = []

    def fake_openrouter(**arguments: object) -> tuple[int, str, cli.PersistedResponse]:
        observed_max_output_tokens.append(arguments["max_output_tokens"])
        return (
            0,
            "",
            cli.PersistedResponse(
                conversation_id="openrouter-conversation",
                cost_usd=0.125,
                duration_ms=10,
                input_tokens=20,
                model=str(arguments["model"]),
                output_tokens=30,
                provider="test-provider",
                response=payload,
            ),
        )

    monkeypatch.setattr(cli, "invoke_openrouter", fake_openrouter)
    monkeypatch.setenv("LLM_BIN", str(fake_llm))
    monkeypatch.setenv("LLM_SCHEMA_RESPONSE", payload)
    monkeypatch.setenv("CODEX_BIN", str(fake_codex))
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.setenv("AGY_RESPONSE", payload)
    parser = cli.build_parser()
    args = parser.parse_args(["tournament", "--plan", str(tournament)])

    assert cli.run_tournament(parser, args) == 0

    report = json.loads((tmp_path / "artifacts" / "tournament.json").read_text())
    assert [run["candidate"] for run in report["runs"]] == ["metered", "codex", "gemini"]
    assert [run["costMode"] for run in report["runs"]] == [
        "metered",
        "account-included",
        "subscription",
    ]
    assert report["actualSpendUsd"] == 0.125
    assert observed_max_output_tokens == [400]
    assert report["runs"][0]["maxOutputTokens"] == 400
    assert report["runs"][1]["maxOutputTokens"] == 200
    assert report["runs"][2]["maxOutputTokens"] == 200
    assert report["runs"][0]["estimatedCostUsd"] == pytest.approx(0.0000859)
    assert report["runs"][1]["estimatedCostUsd"] is None
    assert report["runs"][2]["estimatedCostUsd"] is None


def test_product_tournament_retries_only_an_incomplete_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("Synthetic product brief.\n")
    payload = json.dumps(product_review_payload())
    tournament = tmp_path / "product.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 200
max_attempts = 2
response_contract = "product-review-json"
artifact_root = "{tmp_path / "artifacts"}"

[[candidates]]
id = "metered"
family = "deepseek"
model = "openrouter/deepseek/test"
transport = "openrouter"
cost_mode = "metered"
pricing = {{ input_per_million_usd = 0.1, output_per_million_usd = 0.2 }}

[[cases]]
id = "flow"
stage = "filter"
prompt = "Design this product from the synthetic brief."
files = ["{brief}"]
'''
    )
    calls = 0

    def fake_openrouter(**arguments: object) -> tuple[int, str, cli.PersistedResponse]:
        nonlocal calls
        calls += 1
        return (
            0,
            "",
            cli.PersistedResponse(
                conversation_id=f"conversation-{calls}",
                cost_usd=0.125,
                duration_ms=10,
                input_tokens=20,
                model=str(arguments["model"]),
                output_tokens=30,
                provider="test-provider",
                response="not-json" if calls == 1 else payload,
            ),
        )

    monkeypatch.setattr(cli, "invoke_openrouter", fake_openrouter)
    parser = cli.build_parser()
    args = parser.parse_args(["tournament", "--plan", str(tournament)])

    assert cli.run_tournament(parser, args) == 0

    report = json.loads((tmp_path / "artifacts" / "tournament.json").read_text())
    receipt = json.loads(Path(report["runs"][0]["receipt"]).read_text())
    assert calls == 2
    assert report["runs"][0]["result"] == "accepted"
    assert report["actualSpendUsd"] == 0.25
    assert [attempt["result"] for attempt in receipt["attempts"]] == ["incomplete", "accepted"]


def test_builds_a_blind_product_package_without_model_identity(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    write_verified_receipt(
        receipt,
        {
            "result": "accepted",
            "review": product_review_payload(),
            "response": {"sha256": "a" * 64},
        },
    )
    report = {
        "runs": [
            {
                "candidate": "deepseek-flash",
                "case": "flow",
                "family": "deepseek",
                "model": "openrouter/deepseek/deepseek-v4-flash-0731",
                "receipt": str(receipt),
                "result": "accepted",
            }
        ]
    }

    package, mapping = cli.build_blind_product_package(report, salt=b"test-salt")

    assert package["entries"][0]["case"] == "flow"
    assert package["entries"][0]["response"] == product_review_payload()
    assert "deepseek" not in json.dumps(package).lower()
    blind_id = package["entries"][0]["blindId"]
    assert mapping[blind_id]["candidate"] == "deepseek-flash"


def test_blind_package_excludes_qualified_controls_from_council_proposals(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"review": product_review_payload()}))
    package, mapping = cli.build_blind_product_package(
        {
            "runs": [
                {
                    "candidate": "codex-terra",
                    "case": "flow",
                    "councilEligible": False,
                    "receipt": str(receipt),
                    "result": "accepted",
                }
            ]
        },
        salt=b"test-salt",
    )

    assert package["entries"] == []
    assert mapping == {}


def test_blind_package_requires_a_verified_accepted_receipt(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"result": "accepted", "review": product_review_payload()}))

    with pytest.raises(ValueError, match="verified accepted receipt"):
        cli.build_blind_product_package(
            {
                "runs": [
                    {
                        "candidate": "deepseek-flash",
                        "case": "flow",
                        "receipt": str(receipt),
                        "result": "accepted",
                    }
                ]
            },
            salt=b"test-salt",
        )


def test_selects_two_non_self_council_judges_with_one_codex() -> None:
    candidates = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "candidate",
                    "family": "deepseek",
                    "model": "openrouter/deepseek/test",
                    "transport": "openrouter",
                    "cost_mode": "metered",
                    "pricing": {"input_per_million_usd": 0.1, "output_per_million_usd": 0.2},
                },
                {
                    "id": "codex-terra",
                    "family": "openai",
                    "model": "gpt-5.6-terra",
                    "transport": "codex",
                    "cost_mode": "account-included",
                },
                {
                    "id": "gemini",
                    "family": "gemini",
                    "model": "gemini-3.6-flash-high",
                    "transport": "agy",
                    "cost_mode": "subscription",
                },
            ]
        }
    )

    judges = cli.select_council_judges(candidates, candidates[0])

    assert [judge.identifier for judge in judges] == ["codex-terra", "gemini"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"openQuestions": [1]}),
        lambda payload: payload.update({"interactionFlow": []}),
        lambda payload: payload.update({"domainEntities": [{"name": "Only a name"}]}),
        lambda payload: payload.update({"stateTransitions": [{"from": "draft"}]}),
        lambda payload: payload.update({"architecture": [{"boundary": "runtime"}]}),
        lambda payload: payload.update({"operationalControls": [{"control": "idempotency"}]}),
        lambda payload: payload.update({"constraintChecks": []}),
    ],
)
def test_product_review_contract_rejects_every_incomplete_nested_section(mutation: object) -> None:
    payload = product_review_payload()
    mutation(payload)  # type: ignore[operator]

    assert cli.validate_product_review(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": {}, "hardConstraintViolations": [], "rationale": "Reason."},
        {
            "scores": {
                "delivery": 1,
                "domainIntegrity": 1,
                "operationalCorrectness": 1,
                "problemFidelity": 1,
                "scopeDiscipline": True,
            },
            "hardConstraintViolations": [],
            "rationale": "Reason.",
        },
        {
            "scores": {
                "delivery": 1,
                "domainIntegrity": 1,
                "operationalCorrectness": 1,
                "problemFidelity": 1,
                "scopeDiscipline": 1,
            },
            "hardConstraintViolations": [1],
            "rationale": "Reason.",
        },
    ],
)
def test_product_judge_contract_rejects_invalid_scores_and_violation_lists(
    payload: dict[str, object],
) -> None:
    assert cli.validate_product_judge(payload) is None


def test_product_contracts_require_valid_codex_read_proof() -> None:
    product = product_review_payload()
    product["reviewedFiles"] = ["brief.md"]
    assert (
        cli.validate_review_response(
            json.dumps(product), "product-review-json", expected_file_hashes={"brief.md": "a" * 64}
        )
        == product_review_payload()
    )
    product["reviewedFiles"] = []
    assert (
        cli.validate_review_response(
            json.dumps(product), "product-review-json", expected_file_hashes={"brief.md": "a" * 64}
        )
        is None
    )


def test_product_prompts_describe_each_transport_contract(tmp_path: Path) -> None:
    source = tmp_path / "brief.md"
    source.write_text("Synthetic briefing.\n")
    assert "non-negotiable" in cli.openrouter_packet("Design", [source], "product-review-json")
    assert "council-judgment" in cli.openrouter_packet("Judge", [source], "product-judge-json")
    assert "anonymous candidate" in cli.codex_prompt("Judge", "product-judge-json")


def test_document_prompts_are_explicit_for_each_transport(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("A bounded documentation source.\n")

    assert "coherent Markdown document" in cli.packet_prompt("Document this.", [source], "document")
    assert "Markdown document" in cli.openrouter_packet("Document this.", [source], "document")
    assert "no JSON wrapper" in cli.codex_prompt("Document this.", "document")


def test_source_policy_defaults_to_denied() -> None:
    assert cli.source_allowed({}, "unlisted-model") is False


@pytest.mark.parametrize(
    "plan",
    [
        {},
        {"candidates": ["not-an-object"]},
        {
            "candidates": [
                {
                    "id": "one",
                    "family": "",
                    "model": "m",
                    "transport": "llm",
                    "cost_mode": "subscription",
                }
            ]
        },
        {
            "candidates": [
                {
                    "id": "one",
                    "family": "x",
                    "model": "m",
                    "transport": "llm",
                    "cost_mode": "subscription",
                    "council_eligible": "false",
                }
            ]
        },
        {
            "candidates": [
                {
                    "id": "bad id",
                    "family": "x",
                    "model": "m",
                    "transport": "llm",
                    "cost_mode": "subscription",
                }
            ]
        },
        {
            "candidates": [
                {
                    "id": "one",
                    "family": "x",
                    "model": "m",
                    "transport": "bad",
                    "cost_mode": "subscription",
                }
            ]
        },
        {
            "candidates": [
                {
                    "id": "one",
                    "family": "x",
                    "model": "m",
                    "transport": "llm",
                    "cost_mode": "subscription",
                    "provider": {"bad": "x"},
                }
            ]
        },
        {
            "candidates": [
                {
                    "id": "one",
                    "family": "x",
                    "model": "m",
                    "transport": "llm",
                    "cost_mode": "subscription",
                    "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                }
            ]
        },
    ],
)
def test_candidate_parser_rejects_all_invalid_shapes(plan: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        cli.parse_tournament_candidates(plan)


def test_council_and_blind_package_reject_incomplete_evidence(tmp_path: Path) -> None:
    candidates = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "same",
                    "family": "one",
                    "model": "m",
                    "transport": "codex",
                    "cost_mode": "account-included",
                }
            ]
        }
    )
    with pytest.raises(ValueError):
        cli.select_council_judges(candidates, candidates[0])
    with pytest.raises(ValueError):
        cli.build_blind_product_package({"runs": []}, salt=b"")
    with pytest.raises(ValueError):
        cli.build_blind_product_package({"runs": "invalid"}, salt=b"salt")
    with pytest.raises(ValueError):
        cli.build_blind_product_package(
            {"runs": [{"result": "accepted", "candidate": "x", "case": "flow", "receipt": ""}]},
            salt=b"salt",
        )
    receipt = tmp_path / "receipt.json"
    write_verified_receipt(receipt, {"result": "accepted", "review": None})
    with pytest.raises(ValueError):
        cli.build_blind_product_package(
            {
                "runs": [
                    {
                        "result": "accepted",
                        "candidate": "x",
                        "case": "flow",
                        "receipt": str(receipt),
                    }
                ]
            },
            salt=b"salt",
        )


def test_product_helper_guards_cover_invalid_prices_proofs_and_collisions(tmp_path: Path) -> None:
    assert cli.receipt_attempt_cost({}) is None
    assert cli.receipt_attempt_cost({"attempts": [{"costUsd": 0.1}, {"costUsd": True}]}) == 0.1
    assert (
        cli.parse_candidate_pricing({"input_per_million_usd": -1, "output_per_million_usd": 1})
        is None
    )
    assert (
        cli.parse_candidate_pricing({"input_per_million_usd": True, "output_per_million_usd": 1})
        is None
    )
    payload = product_review_payload()
    payload["mvp"] = []
    assert cli.validate_product_review(payload) is None
    judge = {
        "scores": {
            "delivery": 1,
            "domainIntegrity": 1,
            "operationalCorrectness": 1,
            "problemFidelity": 1,
            "scopeDiscipline": 1,
        },
        "hardConstraintViolations": [],
        "rationale": "Reason.",
        "extra": "not allowed",
    }
    assert cli.validate_product_judge(judge) is None
    judge.pop("extra")
    judge["reviewedFiles"] = ["invented.md"]
    assert (
        cli.validate_review_response(
            json.dumps(judge), "product-judge-json", expected_file_hashes={"brief.md": "b" * 64}
        )
        is None
    )
    receipt = tmp_path / "receipt.json"
    write_verified_receipt(
        receipt,
        {
            "result": "accepted",
            "review": product_review_payload(),
            "response": {"sha256": "a" * 64},
        },
    )
    run = {"result": "accepted", "candidate": "x", "case": "flow", "receipt": str(receipt)}
    with pytest.raises(ValueError, match="collision"):
        cli.build_blind_product_package({"runs": [run, run]}, salt=b"salt")
    package, _ = cli.build_blind_product_package(
        {"runs": [{"result": "unavailable"}, run]}, salt=b"salt"
    )
    assert len(package["entries"]) == 1


def test_product_tournament_rejects_invalid_stage_case_and_contract_shapes(tmp_path: Path) -> None:
    parser = cli.build_parser()
    args = __import__("argparse").Namespace(case_ids=[], stage="deep")
    with pytest.raises(SystemExit):
        cli.select_tournament_cases(parser, {"cases": "invalid"}, args)
    with pytest.raises(SystemExit):
        cli.select_tournament_cases(parser, {"cases": []}, args)
    with pytest.raises(ValueError):
        cli.tournament_case_files({"files": [1]}, tmp_path)

    candidate = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "codex",
                    "family": "openai",
                    "model": "gpt-5.6-terra",
                    "transport": "codex",
                    "cost_mode": "account-included",
                }
            ]
        }
    )
    empty_args = __import__("argparse").Namespace()
    with pytest.raises(SystemExit):
        cli.run_candidate_tournament(
            parser,
            empty_args,
            plan={"response_contract": "findings-json"},
            plan_path=tmp_path / "missing.toml",
            budget=1,
            cases=[],
            candidates=candidate,
            max_output_tokens=1,
        )
    with pytest.raises(SystemExit):
        cli.run_candidate_tournament(
            parser,
            empty_args,
            plan={"response_contract": "product-review-json", "max_attempts": 0},
            plan_path=tmp_path / "product.toml",
            budget=1,
            cases=[{"id": "flow", "prompt": "Design.", "files": []}],
            candidates=candidate,
            max_output_tokens=1,
        )


def test_product_tournament_last_guard_branches_and_budget(tmp_path: Path) -> None:
    parser = cli.build_parser()
    args = __import__("argparse").Namespace(case_ids=[], stage="filter")
    case = {"id": "flow", "stage": "filter", "prompt": "Design.", "files": []}
    assert cli.select_tournament_cases(parser, {"cases": [case]}, args) == [case]
    judge = {
        "scores": {
            "delivery": 1,
            "domainIntegrity": 1,
            "operationalCorrectness": 1,
            "problemFidelity": 1,
            "scopeDiscipline": 1,
        },
        "hardConstraintViolations": [],
        "rationale": "Reason.",
        "reviewedFiles": ["brief.md"],
    }
    assert cli.validate_review_response(
        json.dumps(judge), "product-judge-json", expected_file_hashes={"brief.md": "a" * 64}
    ) == {key: value for key, value in judge.items() if key != "reviewedFiles"}
    assert cli.validate_review_response("{}", "unknown-contract") is None

    source = tmp_path / "brief.md"
    source.write_text("Synthetic.\n")
    metered = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "metered",
                    "family": "x",
                    "model": "openrouter/x",
                    "transport": "openrouter",
                    "cost_mode": "metered",
                    "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                }
            ]
        }
    )
    empty_args = __import__("argparse").Namespace()
    with pytest.raises(SystemExit):
        cli.run_candidate_tournament(
            parser,
            empty_args,
            plan={"response_contract": "product-review-json"},
            plan_path=tmp_path / "product.toml",
            budget=1,
            cases=[{"prompt": "Design.", "files": [str(source)]}],
            candidates=metered,
            max_output_tokens=1,
        )
    with pytest.raises(SystemExit):
        cli.run_candidate_tournament(
            parser,
            empty_args,
            plan={"response_contract": "product-review-json"},
            plan_path=tmp_path / "product.toml",
            budget=1,
            cases=[],
            candidates=metered,
            max_output_tokens=1,
        )
    with pytest.raises(SystemExit):
        cli.run_candidate_tournament(
            parser,
            empty_args,
            plan={"response_contract": "product-review-json"},
            plan_path=tmp_path / "product.toml",
            budget=1,
            cases=[{"id": "flow", "prompt": "", "files": [str(source)]}],
            candidates=metered,
            max_output_tokens=1,
        )
    assert (
        cli.run_candidate_tournament(
            parser,
            empty_args,
            plan={
                "response_contract": "product-review-json",
                "artifact_root": str(tmp_path / "budget-artifacts"),
            },
            plan_path=tmp_path / "product.toml",
            budget=0.00000001,
            cases=[{"id": "flow", "prompt": "Design.", "files": [str(source)]}],
            candidates=metered,
            max_output_tokens=1,
        )
        == 4
    )
    invalid_plan = tmp_path / "invalid-candidates.toml"
    invalid_plan.write_text('budget_usd = 1\n[[candidates]]\nid = "x"\n')
    with pytest.raises(SystemExit):
        cli.run_tournament(parser, parser.parse_args(["tournament", "--plan", str(invalid_plan)]))


def test_blind_package_command_separates_public_responses_from_private_mapping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = tmp_path / "receipt.json"
    write_verified_receipt(
        receipt,
        {"result": "accepted", "review": product_review_payload()},
    )
    report = tmp_path / "tournament.json"
    report.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "candidate": "deepseek-flash",
                        "case": "flow-design-lab",
                        "receipt": str(receipt),
                        "result": "accepted",
                    }
                ]
            }
        )
    )
    package_path = tmp_path / "public" / "blind.json"
    mapping_path = tmp_path / "private" / "blind-map.json"
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "blind-package",
            "--report",
            str(report),
            "--output",
            str(package_path),
            "--mapping-output",
            str(mapping_path),
        ]
    )

    assert args.handler(args) == 0
    assert Path(capsys.readouterr().out.strip()) == package_path
    package = json.loads(package_path.read_text())
    mapping = json.loads(mapping_path.read_text())
    assert package["format"] == "reviewctl.product-blind.v1"
    assert "deepseek-flash" not in package_path.read_text()
    assert package["entries"][0]["case"] == "flow-design-lab"
    assert mapping[package["entries"][0]["blindId"]]["candidate"] == "deepseek-flash"
    assert mapping_path.stat().st_mode & 0o777 == 0o600


def test_blind_package_command_rejects_invalid_or_unsafe_artifacts(tmp_path: Path) -> None:
    parser = cli.build_parser()
    output = tmp_path / "output.json"
    mapping = tmp_path / "mapping.json"

    def arguments(report: Path, *, mapping_output: Path = mapping) -> object:
        return __import__("argparse").Namespace(
            report=str(report), output=str(output), mapping_output=str(mapping_output)
        )

    with pytest.raises(SystemExit):
        cli.write_blind_product_package(parser, arguments(tmp_path / "missing.json"))

    report = tmp_path / "report.json"
    report.write_text("{}")
    with pytest.raises(SystemExit):
        cli.write_blind_product_package(parser, arguments(report, mapping_output=output))

    report.write_text("not json")
    with pytest.raises(SystemExit):
        cli.write_blind_product_package(parser, arguments(report))

    report.write_text("[]")
    with pytest.raises(SystemExit):
        cli.write_blind_product_package(parser, arguments(report))

    report.write_text(json.dumps({"runs": "not-a-list"}))
    with pytest.raises(SystemExit):
        cli.write_blind_product_package(parser, arguments(report))


def test_council_plan_assigns_non_self_codex_and_external_judges(tmp_path: Path) -> None:
    candidates = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "candidate-a",
                    "family": "deepseek",
                    "model": "openrouter/deepseek/example",
                    "transport": "openrouter",
                    "cost_mode": "metered",
                    "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                },
                {
                    "id": "codex-terra",
                    "family": "openai-codex",
                    "model": "gpt-5.6-terra",
                    "transport": "codex",
                    "cost_mode": "account-included",
                },
                {
                    "id": "glm",
                    "family": "glm",
                    "model": "openrouter/z-ai/glm-5.2",
                    "transport": "openrouter",
                    "cost_mode": "metered",
                    "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                },
            ]
        }
    )
    package = {
        "format": "reviewctl.product-blind.v1",
        "entries": [{"blindId": "blind-1", "case": "flow", "response": {}}],
    }
    mapping = {"blind-1": {"candidate": "candidate-a", "case": "flow", "receipt": "x"}}
    expected = {
        "entries": [
            {
                "blindId": "blind-1",
                "case": "flow",
                "judges": [
                    {"id": "codex-terra", "model": "gpt-5.6-terra", "transport": "codex"},
                    {
                        "id": "glm",
                        "model": "openrouter/z-ai/glm-5.2",
                        "transport": "openrouter",
                    },
                ],
            }
        ],
        "format": "reviewctl.product-council-plan.v1",
        "responseContract": "product-judge-json",
    }

    assert cli.build_product_council_plan(candidates, package, mapping) == expected


def test_council_plan_command_persists_only_blind_assignments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text(
        """
[[candidates]]
id = "candidate-a"
family = "deepseek"
model = "openrouter/deepseek/example"
transport = "openrouter"
cost_mode = "metered"
pricing = { input_per_million_usd = 1, output_per_million_usd = 1 }

[[candidates]]
id = "codex-terra"
family = "openai-codex"
model = "gpt-5.6-terra"
transport = "codex"
cost_mode = "account-included"

[[candidates]]
id = "glm"
family = "glm"
model = "openrouter/z-ai/glm-5.2"
transport = "openrouter"
cost_mode = "metered"
pricing = { input_per_million_usd = 1, output_per_million_usd = 1 }
"""
    )
    blind = tmp_path / "blind.json"
    blind.write_text(
        json.dumps(
            {
                "format": "reviewctl.product-blind.v1",
                "entries": [{"blindId": "blind-1", "case": "flow", "response": {}}],
            }
        )
    )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps({"blind-1": {"candidate": "candidate-a", "case": "flow", "receipt": "x"}})
    )
    output = tmp_path / "public" / "council.json"
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "council-plan",
            "--plan",
            str(plan),
            "--blind-package",
            str(blind),
            "--mapping",
            str(mapping),
            "--output",
            str(output),
        ]
    )

    assert args.handler(args) == 0
    assert Path(capsys.readouterr().out.strip()) == output
    saved = json.loads(output.read_text())
    assert saved["entries"][0]["judges"][0]["id"] == "codex-terra"
    assert "candidate-a" not in output.read_text()


def test_candidate_parser_enforces_an_optional_blended_price_cap() -> None:
    plan = {
        "max_blended_price_usd": 2,
        "candidates": [
            {
                "id": "too-expensive",
                "family": "x",
                "model": "openrouter/x",
                "transport": "openrouter",
                "cost_mode": "metered",
                "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 5},
            }
        ],
    }

    with pytest.raises(ValueError, match="blended price"):
        cli.parse_tournament_candidates(plan)
    with pytest.raises(ValueError, match="positive number"):
        cli.parse_tournament_candidates({"max_blended_price_usd": 0, "candidates": []})


def test_product_council_plan_rejects_tampered_or_unknown_identity_data() -> None:
    candidates = cli.parse_tournament_candidates(
        {
            "candidates": [
                {
                    "id": "candidate-a",
                    "family": "deepseek",
                    "model": "openrouter/deepseek/example",
                    "transport": "openrouter",
                    "cost_mode": "metered",
                    "pricing": {"input_per_million_usd": 1, "output_per_million_usd": 1},
                }
            ]
        }
    )
    valid_entry = {"blindId": "blind-1", "case": "flow"}
    valid_mapping = {"blind-1": {"candidate": "candidate-a", "case": "flow"}}
    invalid_inputs = [
        ({"format": "wrong", "entries": []}, valid_mapping),
        ({"format": "reviewctl.product-blind.v1", "entries": {}}, valid_mapping),
        ({"format": "reviewctl.product-blind.v1", "entries": ["wrong"]}, valid_mapping),
        ({"format": "reviewctl.product-blind.v1", "entries": [{"case": "flow"}]}, valid_mapping),
        ({"format": "reviewctl.product-blind.v1", "entries": [valid_entry]}, {}),
        (
            {"format": "reviewctl.product-blind.v1", "entries": [valid_entry]},
            {"blind-1": {"candidate": "candidate-a", "case": "other"}},
        ),
        (
            {"format": "reviewctl.product-blind.v1", "entries": [valid_entry]},
            {"blind-1": {"candidate": "unknown", "case": "flow"}},
        ),
    ]

    for package, mapping in invalid_inputs:
        with pytest.raises(ValueError):
            cli.build_product_council_plan(candidates, package, mapping)


def test_council_plan_command_rejects_missing_overwriting_or_invalid_inputs(tmp_path: Path) -> None:
    parser = cli.build_parser()
    plan = tmp_path / "plan.toml"
    plan.write_text(
        """
[[candidates]]
id = "candidate-a"
family = "deepseek"
model = "openrouter/deepseek/example"
transport = "openrouter"
cost_mode = "metered"
pricing = { input_per_million_usd = 1, output_per_million_usd = 1 }
"""
    )
    blind = tmp_path / "blind.json"
    blind.write_text("[]")
    mapping = tmp_path / "mapping.json"
    mapping.write_text("{}")
    output = tmp_path / "council.json"

    def arguments(
        *, plan_path: Path = plan, blind_path: Path = blind, output_path: Path = output
    ) -> object:
        return __import__("argparse").Namespace(
            plan=str(plan_path),
            blind_package=str(blind_path),
            mapping=str(mapping),
            output=str(output_path),
        )

    with pytest.raises(SystemExit):
        cli.write_product_council_plan(parser, arguments(plan_path=tmp_path / "missing.toml"))
    with pytest.raises(SystemExit):
        cli.write_product_council_plan(parser, arguments(output_path=blind))
    with pytest.raises(SystemExit):
        cli.write_product_council_plan(parser, arguments())


def test_findings_contract_rejects_an_unrequested_external_read_proof() -> None:
    response = json.dumps(
        {
            "verdict": "approved",
            "findings": [],
            "reviewedFiles": ["source.py"],
        }
    )

    assert cli.validate_review_response(response, "findings-json") is None


def test_findings_contract_binds_snapshot_names_while_the_runner_owns_hashes() -> None:
    valid = {
        "verdict": "approved",
        "findings": [],
        "reviewedFiles": ["source.py"],
    }
    assert (
        cli.validate_review_response(
            json.dumps(valid), "findings-json", expected_file_hashes={"source.py": "a" * 64}
        )
        is not None
    )

    valid["reviewedFiles"][0] = "/private/tmp/reviewctl-input-abc/source.py"
    assert (
        cli.validate_review_response(
            json.dumps(valid), "findings-json", expected_file_hashes={"source.py": "a" * 64}
        )
        is not None
    )

    valid["reviewedFiles"][0] = " source.py "
    assert (
        cli.validate_review_response(
            json.dumps(valid), "findings-json", expected_file_hashes={"source.py": "a" * 64}
        )
        is not None
    )


def test_findings_contract_accepts_unique_snapshot_basename_from_sandbox_path() -> None:
    response = {
        "verdict": "approved",
        "findings": [],
        "reviewedFiles": ["/private/tmp/reviewctl-input-abc/source.py"],
    }

    assert (
        cli.validate_review_response(
            json.dumps(response),
            "findings-json",
            expected_file_hashes={"source.py": "a" * 64},
        )
        is not None
    )


def test_legacy_product_read_proof_rejects_arbitrary_relative_paths() -> None:
    assert not cli.validate_read_proof({"reviewedFiles": ["../source.py"]}, {"source.py": "a" * 64})


@pytest.mark.parametrize(
    "reviewed_files",
    [
        "not-a-list",
        ["not-an-object"],
        [1],
        ["   "],
        ["source.py", "source.py"],
        ["source.py", "/private/tmp/reviewctl-input-abc/source.py"],
    ],
)
def test_findings_contract_rejects_malformed_or_duplicate_read_proofs(
    reviewed_files: object,
) -> None:
    response = {
        "verdict": "approved",
        "findings": [],
        "reviewedFiles": reviewed_files,
    }
    assert (
        cli.validate_review_response(
            json.dumps(response),
            "findings-json",
            expected_file_hashes={"source.py": "a" * 64},
        )
        is None
    )


def test_codex_isolation_denies_the_original_source_root_and_uses_a_minimal_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"test"}')
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed-home"))
    monkeypatch.setenv("PATH", "/opt/reviewctl/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setenv("CODEX_CA_CERTIFICATES", str(tmp_path / "codex-ca.pem"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ARBITRARY_SECRET", "arbitrary-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-secret@example.test")

    with cli.codex_isolation([source_root], auth_path=auth) as isolation:
        profile = isolation.profile.read_text()

        assert f'(deny file-read* (subpath "{source_root}"))' in profile
        assert f'(deny file-write* (subpath "{source_root}"))' in profile
        assert f'(deny file-write* (subpath "{cli.account_home()}"))' in profile
        assert str(tmp_path / "spoofed-home") not in profile
        assert isolation.home.joinpath("auth.json").read_text() == auth.read_text()
        assert isolation.environment["CODEX_HOME"] == str(isolation.home)
        assert isolation.environment["HOME"] == str(isolation.home)
        assert isolation.environment["TMPDIR"] == str(isolation.home)
        assert isolation.environment["PATH"] == "/opt/reviewctl/bin"
        assert isolation.environment["LANG"] == "en_US.UTF-8"
        assert isolation.environment["SSL_CERT_FILE"] == str(tmp_path / "ca.pem")
        assert isolation.environment["CODEX_CA_CERTIFICATES"] == str(tmp_path / "codex-ca.pem")
        assert "CODEX_AUTH_FILE" not in isolation.environment
        assert "AWS_SECRET_ACCESS_KEY" not in isolation.environment
        assert "OPENAI_API_KEY" not in isolation.environment
        assert "ARBITRARY_SECRET" not in isolation.environment
        assert "HTTPS_PROXY" not in isolation.environment


def test_invoke_codex_passes_an_explicit_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        def communicate(self, timeout: int) -> tuple[bytes, bytes]:
            command = captured["command"]
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text("VERDICT: approved.")
            return b"session id: test-session\nmodel: gpt-5.6-terra\n", b""

    def popen(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return Process()

    codex_home = tmp_path / "codex-home"
    auth_file = tmp_path / "auth.json"
    monkeypatch.setenv("PATH", "/opt/reviewctl/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "account-home"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
    monkeypatch.setenv("CODEX_CA_CERTIFICATES", str(tmp_path / "codex-ca.pem"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ARBITRARY_SECRET", "arbitrary-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-secret@example.test")
    monkeypatch.setattr(cli.subprocess, "Popen", popen)

    exit_code, _, response = cli.invoke_codex(
        codex_bin="/opt/reviewctl/bin/codex",
        prompt="Review the synthetic fixture.",
        model="gpt-5.6-terra",
        response_contract="verdict",
        source_roots=None,
        timeout_seconds=1,
        workspace=tmp_path,
    )

    environment = captured["environment"]
    assert environment is not None
    assert environment["PATH"] == "/opt/reviewctl/bin"
    assert environment["HOME"] == str(tmp_path / "account-home")
    assert environment["CODEX_HOME"] == str(codex_home)
    assert environment["CODEX_AUTH_FILE"] == str(auth_file)
    assert environment["CODEX_CA_CERTIFICATES"] == str(tmp_path / "codex-ca.pem")
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "ARBITRARY_SECRET" not in environment
    assert "HTTPS_PROXY" not in environment
    assert exit_code == 0
    assert response.response == "VERDICT: approved."


@pytest.mark.skipif(cli.shutil.which("sandbox-exec") is None, reason="macOS integration")
def test_macos_sandbox_profile_denies_the_original_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    secret = source_root / "proprietary.py"
    secret.write_text("secret = 'never reveal'\n")
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"test"}')

    with cli.codex_isolation([source_root], auth_path=auth) as isolation:
        denied = subprocess.run(
            ["sandbox-exec", "-f", str(isolation.profile), "/bin/cat", str(secret)],
            text=True,
            capture_output=True,
            check=False,
        )

    assert denied.returncode != 0
    assert "never reveal" not in denied.stdout


def test_codex_isolation_rejects_missing_sandbox_or_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"test"}')

    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="sandbox-exec"):
        with cli.codex_isolation([source_root], auth_path=auth):
            pass

    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    with pytest.raises(RuntimeError, match="auth file"):
        with cli.codex_isolation([source_root], auth_path=tmp_path / "missing.json"):
            pass


@pytest.mark.parametrize(
    ("home_value", "auth_value"),
    [(None, None), ("", "")],
    ids=["home-unset", "home-empty"],
)
def test_codex_isolation_resolves_default_auth_from_account_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    home_value: str | None,
    auth_value: str | None,
) -> None:
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    login_home = tmp_path / "login-home"
    auth = login_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"access_token":"account-auth"}')
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(cli, "account_home", lambda: login_home)
    monkeypatch.setattr(cli.Path, "expanduser", lambda _: tmp_path / "untrusted-auth.json")
    if home_value is None:
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", home_value)
    if auth_value is None:
        monkeypatch.delenv("CODEX_AUTH_FILE", raising=False)
    else:
        monkeypatch.setenv("CODEX_AUTH_FILE", auth_value)

    with cli.codex_isolation([source_root]) as isolation:
        assert isolation.home.joinpath("auth.json").read_text() == auth.read_text()


def test_codex_transport_fails_closed_when_proprietary_isolation_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_AUTH_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/sandbox-exec")

    exit_code, error, response = cli.invoke_codex(
        codex_bin="must-not-run",
        prompt="Review only the supplied file.",
        model="gpt-5.6-terra",
        response_contract="verdict",
        source_roots=[tmp_path],
        timeout_seconds=1,
        workspace=tmp_path,
    )

    assert exit_code == 127
    assert "auth file" in error
    assert response.response == ""


def test_review_source_roots_uses_git_root_or_file_parent(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    tracked = repository / "src" / "entry.py"
    tracked.parent.mkdir()
    tracked.write_text("pass\n")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    detached = tmp_path / "detached" / "entry.py"
    detached.parent.mkdir()
    detached.write_text("pass\n")

    assert cli.review_source_roots([tracked, detached, tracked]) == [repository, detached.parent]


def test_source_git_metadata_uses_the_reviewed_checkout_not_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "reviewed-repository"
    repository.mkdir()
    source = repository / "src" / "entry.py"
    source.parent.mkdir()
    source.write_text("pass\n")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "add", str(source.relative_to(repository))],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.email=reviewctl@example.test",
            "-c",
            "user.name=Reviewctl Test",
            "commit",
            "-qm",
            "add source",
        ],
        check=True,
    )
    expected_head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    monkeypatch.chdir(tmp_path)

    assert cli.source_git_metadata([source]) == {
        "head": expected_head,
        "remote": None,
        "repositoryRoot": str(repository),
    }


def test_source_git_metadata_rejects_files_from_multiple_checkouts(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    sources = []
    for repository in (first, second):
        repository.mkdir()
        source = repository / "entry.py"
        source.write_text("pass\n")
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        sources.append(source)

    assert cli.source_git_metadata(sources) == {
        "head": None,
        "remote": None,
        "repositoryRoot": None,
    }


def test_codex_transport_applies_source_root_isolation_for_proprietary_reviews(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_codex = write_fake_codex(tools)
    write_fake_sandbox_exec(tools)
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    source = source_root / "source.py"
    source.write_text("def example() -> None: pass\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review this bounded change. Return VERDICT and numbered findings.")
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"test"}')
    policy = tmp_path / "policy.toml"
    policy.write_text('[models."gpt-5.6-terra"]\nsource_allowed = true\n')

    result = run_cli(
        "run",
        "--review-id",
        "isolated-proprietary",
        "--prompt-file",
        str(prompt),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--model",
        "gpt-5.6-terra",
        "--file",
        str(source),
        "--source-class",
        "proprietary",
        "--policy",
        str(policy),
        "--transport",
        "codex",
        "--response-contract",
        "findings-json",
        env={
            "CODEX_AUTH_FILE": str(auth),
            "CODEX_BIN": str(fake_codex),
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["attempts"][0]["isolation"] == "macos-source-root-deny"


def test_codex_transport_times_out_without_retaining_a_raw_response(tmp_path: Path) -> None:
    fake_codex = write_fake_codex(tmp_path, sleep=True)

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--timeout-seconds",
        "1",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 1
    turn = Path(result.stdout.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    assert receipt["attempts"][0]["result"] == "timeout"
    assert not list(turn.glob("**/codex-response.md"))


def test_codex_timeout_discards_a_partial_response_written_before_termination(
    tmp_path: Path,
) -> None:
    partial = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [
                {
                    "severity": "high",
                    "path": "source.py",
                    "line": 1,
                    "title": "Partial finding",
                    "evidence": "Must never become evidence.",
                    "reproduction": "Timeout after writing.",
                }
            ],
        }
    )
    fake_codex = write_fake_codex(
        tmp_path,
        response=partial,
        sleep=True,
        write_before_sleep=True,
    )

    result = run_cli(
        *review_arguments(tmp_path, "gpt-5.6-terra"),
        "--transport",
        "codex",
        "--response-contract",
        "findings-json",
        "--timeout-seconds",
        "1",
        env={"CODEX_BIN": str(fake_codex)},
    )

    assert result.returncode == 1
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["attempts"][0]["result"] == "timeout"
    assert receipt["attempts"][0]["findings"] == []


def test_rejects_response_recorded_for_a_different_model(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(*review_arguments(tmp_path, "wrong-model"), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 1
    receipt_path = next((tmp_path / "artifacts" / "packet-1").glob("*/receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["attempts"][0]["result"] == "model-mismatch"
    assert receipt["result"] == "unavailable"


def test_rejects_route_escaping_review_id(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    source = tmp_path / "source.py"
    prompt.write_text("Review")
    source.write_text("pass\n")

    result = run_cli(
        "run",
        "--review-id",
        "..",
        "--prompt-file",
        str(prompt),
        "--file",
        str(source),
    )

    assert result.returncode == 2
    assert "invalid review id" in result.stderr


def test_timeout_terminates_the_attempt_process_group(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(*review_arguments(tmp_path, "timeout"), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 1
    receipt_path = next((tmp_path / "artifacts" / "packet-1").glob("*/receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["attempts"][0]["result"] == "timeout"
    child_marker = Path(receipt["attempts"][0]["database"]).with_suffix(".child")
    if child_marker.exists():
        process_id = int(child_marker.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)


def test_proprietary_source_records_a_non_authorizing_policy_without_blocking(
    tmp_path: Path,
) -> None:
    fake_llm = write_fake_llm(tmp_path)
    policy = tmp_path / "policy.toml"
    policy.write_text(
        """[models.accepted]
source_allowed = false
zdr = "unknown"
data_collection = "unknown"
"""
    )

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--policy",
        str(policy),
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        env={"LLM_BIN": str(fake_llm)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["policy"]["sha256"] == cli.sha256_bytes(policy.read_bytes())


def test_proprietary_source_allows_an_optional_policy(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    without_policy = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        env={"LLM_BIN": str(fake_llm)},
    )
    assert without_policy.returncode == 0, without_policy.stderr
    without_policy_receipt = json.loads(
        (Path(without_policy.stdout.strip()) / "receipt.json").read_text()
    )
    assert without_policy_receipt["policy"] == {"sha256": None}

    policy = tmp_path / "allowed.toml"
    policy.write_text("[models.accepted]\nsource_allowed = true\n")
    with_policy = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--source-class",
        "proprietary",
        "--response-contract",
        "findings-json",
        "--policy",
        str(policy),
        env={"LLM_BIN": str(fake_llm)},
    )
    assert with_policy.returncode == 0, with_policy.stderr
    receipt = json.loads((Path(with_policy.stdout.strip()) / "receipt.json").read_text())
    assert receipt["policy"]["sha256"] == cli.sha256_bytes(policy.read_bytes())


def test_proprietary_source_allows_the_selected_response_contract(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    policy = tmp_path / "allowed.toml"
    policy.write_text("[models.accepted]\nsource_allowed = true\n")
    payload = product_review_payload()

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--source-class",
        "proprietary",
        "--policy",
        str(policy),
        "--response-contract",
        "product-review-json",
        env={"LLM_BIN": str(fake_llm), "LLM_SCHEMA_RESPONSE": json.dumps(payload)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["review"] == payload


def test_rejects_duplicate_file_basenames_before_creating_artifacts(tmp_path: Path) -> None:
    first = tmp_path / "first" / "entry.py"
    second = tmp_path / "second" / "entry.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("pass\n")
    second.write_text("pass\n")

    result = run_cli(
        "run",
        "--review-id",
        "duplicate-basename",
        "--prompt",
        "Review this synthetic packet.",
        "--model",
        "accepted",
        "--file",
        str(first),
        "--file",
        str(second),
        "--artifact-root",
        str(tmp_path / "artifacts"),
    )

    assert result.returncode == 2
    assert "unique basenames" in result.stderr
    assert not (tmp_path / "artifacts").exists()


def test_rejects_non_printable_file_basename_before_creating_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source.py\n"
    source.write_text("pass\n")

    result = run_cli(
        "run",
        "--review-id",
        "unsafe-basename",
        "--prompt",
        "Review this synthetic packet.",
        "--model",
        "accepted",
        "--file",
        str(source),
        "--artifact-root",
        str(tmp_path / "artifacts"),
    )

    assert result.returncode == 2
    assert "safe printable basenames" in result.stderr
    assert not (tmp_path / "artifacts").exists()


def test_freezes_source_bytes_before_the_model_receives_them(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("before\n")

    with cli.frozen_review_files([source]) as (provenance, snapshots):
        source.write_text("after\n")

        assert snapshots[0].read_text() == "before\n"
        assert provenance == [
            {
                "name": "source.py",
                "path": str(source),
                "sha256": cli.sha256_bytes(b"before\n"),
            }
        ]


def test_tournament_budgets_the_assembled_packet_not_only_the_user_prompt(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    tournament = tmp_path / "packet-budget.toml"
    tournament.write_text(
        f'''budget_usd = 20
max_output_tokens = 1
artifact_root = "{tmp_path / "packet-artifacts"}"

[models.accepted]
input_per_million_usd = 1000000
output_per_million_usd = 0

[[cases]]
id = "packet-cost"
prompt = "X"
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 4
    report = json.loads((tmp_path / "packet-artifacts" / "tournament.json").read_text())
    assert report["result"] == "budget-exhausted"
    assert report["runs"] == []


def test_tournament_stops_before_the_budget_is_exceeded(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "synthetic.py"
    source.write_text("def synthetic_example() -> None: pass\n")
    tournament = tmp_path / "tournament.toml"
    tournament.write_text(
        f"""budget_usd = 0.000001
artifact_root = "{tmp_path / "tournament-artifacts"}"

[models.accepted]
input_per_million_usd = 1
output_per_million_usd = 1

[[cases]]
id = "synthetic-case"
prompt = "Review this synthetic case. Return VERDICT and numbered findings."
files = ["{source}"]
"""
    )

    result = run_cli("tournament", "--plan", str(tournament), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 4
    report = json.loads((tmp_path / "tournament-artifacts" / "tournament.json").read_text())
    assert report["result"] == "budget-exhausted"
    assert report["runs"] == []


def test_legacy_tournament_honors_a_per_model_output_cap(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "synthetic.py"
    source.write_text("def synthetic_example() -> None: pass\n")
    tournament = tmp_path / "tournament.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 5
artifact_root = "{tmp_path / "tournament-artifacts"}"

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0
max_output_tokens = 300

[[cases]]
id = "synthetic-case"
prompt = "Review this synthetic case. Return VERDICT and numbered findings."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "tournament-artifacts" / "tournament.json").read_text())
    assert report["runs"][0]["maxOutputTokens"] == 300


def test_legacy_tournament_defaults_to_one_attempt_and_reserves_declared_retries(
    tmp_path: Path,
) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "tournament.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 2
max_attempts = 2
artifact_root = "{tmp_path / "tournament-artifacts"}"

[models.empty]
input_per_million_usd = 0
output_per_million_usd = 1

[[cases]]
id = "synthetic-case"
prompt = "Review."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 0
    report = json.loads((tmp_path / "tournament-artifacts" / "tournament.json").read_text())
    receipt = json.loads(Path(report["runs"][0]["receipt"]).read_text())
    assert len(receipt["attempts"]) == 2
    assert report["runs"][0]["estimatedCostUsd"] == pytest.approx(0.000004)


@pytest.mark.parametrize("max_attempts", ["0", "4", '"2"'])
def test_legacy_tournament_rejects_invalid_attempt_limits(
    tmp_path: Path, max_attempts: str
) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "invalid-tournament.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 5
max_attempts = {max_attempts}

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "synthetic-case"
prompt = "Review."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2
    assert "max_attempts must be an integer from 1 to 3" in result.stderr


@pytest.mark.parametrize("max_output_tokens", ["0", "true", '"300"'])
def test_legacy_tournament_rejects_an_invalid_per_model_output_cap(
    tmp_path: Path, max_output_tokens: str
) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "invalid-tournament.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 5

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0
max_output_tokens = {max_output_tokens}

[[cases]]
id = "synthetic-case"
prompt = "Review."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2
    assert "model max_output_tokens must be a positive integer" in result.stderr


def test_legacy_tournament_rejects_a_non_object_model_pricing(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "invalid-tournament.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 5

[models]
accepted = "not-pricing"

[[cases]]
id = "synthetic-case"
prompt = "Review."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2
    assert "model pricing must be an object" in result.stderr


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("failure", "transport-failed"),
        ("missing", "missing-response"),
        ("no-conversation", "missing-conversation"),
        ("incomplete", "incomplete"),
    ],
)
def test_classifies_all_unacceptable_persisted_response_states(
    tmp_path: Path, model: str, expected: str
) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(*review_arguments(tmp_path, model), env={"LLM_BIN": str(fake_llm)})

    assert result.returncode == 1
    receipt_path = next((tmp_path / "artifacts" / "packet-1").glob("*/receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["attempts"][0]["result"] == expected


@pytest.mark.parametrize(
    "invalid",
    [
        {"models": []},
        {"files": []},
        {"prompt": "inline", "prompt_file": "also-present"},
        {"prompt": None, "prompt_file": None},
    ],
)
def test_validate_request_rejects_missing_or_ambiguous_fields(
    tmp_path: Path, invalid: dict[str, object]
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    values = {
        "review_id": "packet.1",
        "models": ["accepted"],
        "files": [str(source)],
        "prompt": "inline",
        "prompt_file": None,
    }
    values.update(invalid)

    with pytest.raises(SystemExit) as error:
        cli.validate_request(cli.build_parser(), __import__("argparse").Namespace(**values))

    assert error.value.code == 2


def test_validate_request_rejects_blank_missing_and_oversized_fragments(tmp_path: Path) -> None:
    assert cli.MAX_FRAGMENT_BYTES == 128 * 1024

    missing = tmp_path / "missing.py"
    oversized = tmp_path / "oversized.py"
    oversized.write_bytes(b"x" * (cli.MAX_FRAGMENT_BYTES + 1))
    parser = cli.build_parser()
    for prompt, files in [
        ("   ", [tmp_path / "present.py"]),
        ("valid", [missing]),
        ("valid", [oversized]),
    ]:
        if files[0].name == "present.py":
            files[0].write_text("pass\n")
        namespace = __import__("argparse").Namespace(
            review_id="packet.1",
            models=["accepted"],
            files=[str(file) for file in files],
            prompt=prompt,
            prompt_file=None,
        )
        with pytest.raises(SystemExit):
            cli.validate_request(parser, namespace)

    files = [tmp_path / f"source-{number}.py" for number in range(cli.MAX_FILES + 1)]
    for file in files:
        file.write_text("pass\n")
    namespace = __import__("argparse").Namespace(
        review_id="packet.1",
        models=["accepted"],
        files=[str(file) for file in files],
        prompt="valid",
        prompt_file=None,
    )
    with pytest.raises(SystemExit):
        cli.validate_request(parser, namespace)


def test_load_response_handles_missing_invalid_empty_and_optional_token_columns(
    tmp_path: Path,
) -> None:
    assert cli.load_response(tmp_path / "missing.sqlite3") is None
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_text("not a database")
    assert cli.load_response(invalid) is None

    incomplete = tmp_path / "incomplete.sqlite3"
    with closing(sqlite3.connect(incomplete)) as connection:
        connection.execute("CREATE TABLE responses (response TEXT)")
    assert cli.load_response(incomplete) is None

    empty = tmp_path / "empty.sqlite3"
    with closing(sqlite3.connect(empty)) as connection:
        connection.execute(
            "CREATE TABLE responses (response TEXT, conversation_id TEXT, model TEXT)"
        )
    assert cli.load_response(empty) is None

    valid = tmp_path / "valid.sqlite3"
    with closing(sqlite3.connect(valid)) as connection:
        connection.execute(
            "CREATE TABLE responses (response TEXT, conversation_id TEXT, model TEXT)"
        )
        connection.execute("INSERT INTO responses VALUES ('VERDICT: approved.', 'c1', 'model')")
        connection.commit()
    response = cli.load_response(valid)
    assert response == cli.PersistedResponse(
        conversation_id="c1",
        cost_usd=None,
        duration_ms=None,
        input_tokens=None,
        model="model",
        output_tokens=None,
        provider=None,
        response="VERDICT: approved.",
    )


def test_load_response_closes_the_read_only_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "tracked.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE responses (response TEXT, conversation_id TEXT, model TEXT)"
        )
        connection.execute("INSERT INTO responses VALUES ('VERDICT: approved.', 'c1', 'model')")
        connection.commit()

    real_connect = sqlite3.connect
    closed = False

    class TrackingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __enter__(self) -> TrackingConnection:
            self.connection.__enter__()
            return self

        def __exit__(self, *arguments: object) -> None:
            self.connection.__exit__(*arguments)

        def __getattr__(self, name: str) -> object:
            return getattr(self.connection, name)

        def close(self) -> None:
            nonlocal closed
            closed = True
            self.connection.close()

    monkeypatch.setattr(
        cli.sqlite3,
        "connect",
        lambda *arguments, **keywords: TrackingConnection(real_connect(*arguments, **keywords)),
    )

    assert cli.load_response(database) is not None
    assert closed is True


def test_load_response_extracts_provider_cost_and_duration_when_llm_persists_usage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "usage.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE responses (response TEXT, conversation_id TEXT, model TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER, response_json TEXT)"
        )
        connection.execute(
            "INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "VERDICT: approved.",
                "c1",
                "model",
                20,
                40,
                300,
                json.dumps({"provider": "Example", "usage": {"cost": 0.123}}),
            ),
        )
        connection.commit()

    response = cli.load_response(database)

    assert response is not None
    assert response.cost_usd == 0.123
    assert response.duration_ms == 300
    assert response.provider == "Example"


def test_load_response_tolerates_malformed_transport_metadata(tmp_path: Path) -> None:
    database = tmp_path / "malformed-usage.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE responses (response TEXT, conversation_id TEXT, model TEXT, "
            "response_json TEXT)"
        )
        connection.execute(
            "INSERT INTO responses VALUES ('VERDICT: approved.', 'c1', 'model', '{')"
        )
        connection.commit()

    response = cli.load_response(database)

    assert response is not None
    assert response.cost_usd is None
    assert response.provider is None


def test_terminate_process_group_handles_missing_and_stubborn_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingProcess:
        pid = 1

        def wait(self, timeout: int | None = None) -> None:
            raise AssertionError("wait should not run after ProcessLookupError")

    monkeypatch.setattr(cli.os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    cli.terminate_process_group(MissingProcess())

    signals: list[int] = []

    class StubbornProcess:
        pid = 2
        calls = 0

        def wait(self, timeout: int | None = None) -> None:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("llm", timeout)

    monkeypatch.setattr(cli.os, "killpg", lambda _pid, value: signals.append(value))
    cli.terminate_process_group(StubbornProcess())
    assert signals == [cli.signal.SIGTERM, cli.signal.SIGKILL]


def test_seal_failure_and_cli_runtime_error_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_fake_age(tmp_path)
    monkeypatch.setenv("AGE_FAIL", "1")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(RuntimeError):
        cli.seal(tmp_path / "request.json", b"payload", "not-an-age-recipient")

    fake_llm = write_fake_llm(tmp_path)
    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--seal-to",
        "not-an-age-recipient",
        env={"LLM_BIN": str(fake_llm)},
    )
    assert result.returncode == 1
    assert result.stderr.startswith("reviewctl:")
    assert not list((tmp_path / "artifacts").glob("**/*.sqlite3"))


@pytest.mark.parametrize(
    ("response", "complete"),
    [
        ("", False),
        ("VERDICT: approved", True),
        ("VERDICT: APPROVED", True),
        ("VERDICT: short.", False),
        ("VERDICT: enough text without ending", False),
        ("VERDICT: enough text with terminal punctuation.", True),
    ],
)
def test_response_completeness_contract(response: str, complete: bool) -> None:
    assert cli.response_is_complete(response) is complete


def age_recipient(tmp_path: Path) -> tuple[Path, str]:
    key = tmp_path / "audit.agekey"
    key.write_text("test identity")
    return key, "age1testrecipient"


def test_sealed_receipt_keeps_request_and_response_out_of_plaintext_artifacts(
    tmp_path: Path,
) -> None:
    fake_llm = write_fake_llm(tmp_path)
    fake_age = write_fake_age(tmp_path)
    key, recipient = age_recipient(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--seal-to",
        recipient,
        env={
            "LLM_BIN": str(fake_llm),
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    turn = Path(result.stdout.strip())
    receipt = json.loads((turn / "receipt.json").read_text())
    assert (turn / receipt["sealed"]["request"]).is_file()
    assert (turn / receipt["sealed"]["response"]).is_file()
    assert not list(turn.glob("**/*.sqlite3"))
    decrypted = subprocess.run(
        [str(fake_age), "-d", "-i", str(key), str(turn / receipt["sealed"]["request"])],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Review this bounded change" in decrypted.stdout


def test_verification_detects_tampered_receipt(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    result = run_cli(*review_arguments(tmp_path, "accepted"), env={"LLM_BIN": str(fake_llm)})
    receipt_path = Path(result.stdout.strip()) / "receipt.json"

    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0
    receipt = json.loads(receipt_path.read_text())
    receipt["reviewId"] = "tampered"
    receipt_path.write_text(json.dumps(receipt))
    tampered = run_cli("verify", str(receipt_path))
    assert tampered.returncode == 1


@pytest.mark.parametrize(
    ("filename", "expected_digest"),
    [
        (
            "accepted-findings-v1.json",
            "bba561e2704d9a54bc4b911cc71c7071676ba298789fe047bb1971ed9e0a33c8",
        ),
        (
            "unavailable-findings-v1.json",
            "03c939fdee8aaf58ec2be137c6df5d13e72163b2ff254ab32cf85f7ed0555c06",
        ),
        (
            "legacy-digest-only.json",
            "d96f6cf66e34cb13404e69c6a5515eadb2354fff65729d03ebedf0a800e1b057",
        ),
    ],
)
def test_immutable_v1_receipt_fixtures_verify_by_embedded_digest(
    filename: str, expected_digest: str
) -> None:
    fixture_path = V1_RECEIPT_FIXTURES / filename
    receipt = json.loads(fixture_path.read_text())

    assert "receiptSchemaVersion" not in receipt
    assert fixture_path.read_bytes() == cli.canonical_json(receipt) + b"\n"
    assert receipt["sha256"] == expected_digest
    serialized = fixture_path.read_text().lower()
    for forbidden in ("/users/", "/home/", "api_key", "bearer ", "password", "sk-"):
        assert forbidden not in serialized
    assert cli.valid_receipt(receipt) is True
    verified = run_cli("verify", str(fixture_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


def test_legacy_digest_only_fixture_is_a_compatibility_routing_sentinel() -> None:
    receipt = json.loads((V1_RECEIPT_FIXTURES / "legacy-digest-only.json").read_text())

    assert receipt["fixturePurpose"] == "compatibility-routing-sentinel-not-a-valid-review"
    assert receipt["result"] == "accepted"
    assert receipt["acceptedAttempt"] == 99
    assert len(receipt["attempts"]) == 1
    assert cli.valid_receipt(receipt) is True


def test_findings_receipt_binds_native_contract_evaluation(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    complete_response = {
        "verdict": "changes-requested",
        "findings": [
            {
                "severity": "high",
                "path": "source.py",
                "line": 1,
                "title": "Example finding",
                "evidence": "The bounded source contains the example.",
                "reproduction": "Inspect source.py line 1.",
            }
        ],
    }

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--response-contract",
        "findings-json",
        env={
            "LLM_BIN": str(fake_llm),
            "LLM_SCHEMA_RESPONSE": json.dumps(complete_response),
        },
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    attempt = receipt["attempts"][0]
    evaluation = attempt["contractEvaluation"]
    assert receipt["receiptSchemaVersion"] == 2
    assert receipt["sourceClass"] == "synthetic"
    assert receipt["source"]["files"][0]["name"] == "source.py"
    assert attempt["number"] == 1
    assert attempt["routeIndex"] == 0
    assert receipt["fallbackRelationships"] == []
    assert receipt["consolidatedReview"]["status"] == "accepted"
    assert receipt["acceptedAttempt"] == 1
    assert receipt["result"] == "accepted"
    assert attempt["result"] == "accepted"
    assert receipt["verdict"] == complete_response["verdict"]
    assert receipt["findings"] == complete_response["findings"]
    assert receipt["reviewContract"] == "findings-json"
    assert receipt["contract"] == {"name": "findings-json", "version": "1"}
    assert set(evaluation) == {
        "name",
        "version",
        "preparedSha256",
        "payloadSha256",
        "normalizedSha256",
        "normalizedValue",
        "contractContext",
        "violations",
        "status",
        "fragments",
        "coverage",
        "completionRequest",
    }
    assert (evaluation["name"], evaluation["version"]) == ("findings-json", "1")
    assert evaluation["status"] == "complete"
    assert evaluation["contractContext"] == {
        "fileNames": ["source.py"],
        "reviewDeclarationRequired": False,
    }
    assert evaluation["normalizedValue"] == complete_response
    assert evaluation["normalizedSha256"] == cli.sha256_bytes(cli.canonical_json(complete_response))
    assert evaluation["completionRequest"] is None
    assert evaluation["violations"] == []
    for field in ("preparedSha256", "payloadSha256", "normalizedSha256"):
        assert len(evaluation[field]) == 64
        int(evaluation[field], 16)
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


def test_generated_complete_duplicate_findings_receipt_self_verifies(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    duplicate = {
        "severity": "high",
        "path": "source.py",
        "line": 1,
        "title": "Duplicate finding",
        "evidence": "The same finding is intentionally repeated.",
        "reproduction": "Inspect source.py line 1.",
    }
    response = {
        "verdict": "changes-requested",
        "findings": [duplicate, duplicate],
    }

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--response-contract",
        "findings-json",
        env={"LLM_BIN": str(fake_llm), "LLM_SCHEMA_RESPONSE": json.dumps(response)},
    )

    assert result.returncode == 0, result.stderr
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    evaluation = receipt["attempts"][0]["contractEvaluation"]
    assert len(evaluation["fragments"]) == 1
    assert evaluation["normalizedValue"]["findings"] == [duplicate]
    assert receipt["findings"] == [duplicate]
    assert receipt["attempts"][0]["promotedFragments"] == []
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["violations"] == []


def test_generated_incomplete_duplicate_findings_promote_once_and_self_verify(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    duplicate = {
        "severity": "medium",
        "path": "source.py",
        "line": 1,
        "title": "Useful duplicate",
        "evidence": "The partial response repeats one bounded finding.",
        "reproduction": "Inspect source.py.",
    }
    response = json.dumps({"findings": [duplicate, duplicate]})

    return_code, receipt, _ = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": response}],
        max_attempts=1,
    )

    assert return_code == 1
    attempt = receipt["attempts"][0]
    assert len(attempt["contractEvaluation"]["fragments"]) == 1
    assert len(attempt["promotedFragments"]) == 1
    assert cli.validate_v2_receipt(receipt) == ()


def test_generated_mixed_valid_and_invalid_findings_self_verify(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid = {
        "severity": "medium",
        "path": "source.py",
        "line": 1,
        "title": "Useful sibling",
        "evidence": "One sibling remains valid evidence.",
        "reproduction": "Inspect source.py.",
    }
    invalid = {**valid, "severity": "urgent"}
    response = json.dumps({"verdict": "changes-requested", "findings": [invalid, valid]})

    return_code, receipt, _ = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": response}],
        max_attempts=1,
    )

    assert return_code == 1
    attempt = receipt["attempts"][0]
    evaluation = attempt["contractEvaluation"]
    assert evaluation["status"] == "incomplete"
    assert len(evaluation["fragments"]) == 1
    assert evaluation["coverage"]["coveredFields"] == ["verdict"]
    assert evaluation["coverage"]["missingFields"] == ["findings"]
    assert len(attempt["promotedFragments"]) == 1
    assert cli.validate_v2_receipt(receipt) == ()


def test_repeated_partial_payload_promotes_identity_only_once_across_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = {
        "severity": "low",
        "path": "source.py",
        "line": 1,
        "title": "Repeated partial",
        "evidence": "Both attempts return the same typed evidence.",
        "reproduction": "Inspect source.py.",
    }
    response = json.dumps({"findings": [finding]})

    return_code, receipt, requests = _run_registered_findings_sequence(
        monkeypatch,
        tmp_path,
        capsys,
        [{"response": response}, {"response": response}, {"response": "not json"}],
        max_attempts=3,
    )

    assert return_code == 1
    assert [len(attempt["promotedFragments"]) for attempt in receipt["attempts"]] == [1, 1, 0]
    first, second = (attempt["promotedFragments"][0] for attempt in receipt["attempts"][:2])
    assert first["fragmentId"] == second["fragmentId"]
    assert [first["sourceAttempt"], second["sourceAttempt"]] == [1, 2]
    encoded_context = (
        requests[2]
        .prompt.split("<reviewctl-completion-context>\n", 1)[1]
        .split("\n</reviewctl-completion-context>", 1)[0]
    )
    context = json.loads(encoded_context)
    assert len(context["findings"]) == 1
    assert [source["attempt"] for source in context["findings"][0]["sources"]] == [1, 2]
    assert receipt["fallbackRelationships"][1]["promotedFragmentIds"] == [first["fragmentId"]]
    assert [
        source["attempt"] for source in receipt["consolidatedReview"]["findings"][0]["sources"]
    ] == [1, 2]
    assert cli.validate_v2_receipt(receipt) == ()

    wrong_source = deepcopy(receipt)
    wrong_source["fallbackRelationships"][1]["fromAttempt"] = 1
    wrong_source.pop("sha256")
    wrong_source["sha256"] = hashlib.sha256(cli.canonical_json(wrong_source)).hexdigest()
    assert "fallback-relationships" in cli.validate_v2_receipt(wrong_source)

    omitted_context = deepcopy(receipt)
    omitted_context["fallbackRelationships"][1]["promotedFragmentIds"] = []
    omitted_context.pop("sha256")
    omitted_context["sha256"] = hashlib.sha256(cli.canonical_json(omitted_context)).hexdigest()
    assert "fallback-relationships" in cli.validate_v2_receipt(omitted_context)

    injected_context = deepcopy(receipt)
    injected_context["fallbackRelationships"][1]["promotedFragmentIds"].append("0" * 64)
    injected_context["fallbackRelationships"][1]["promotedFragmentIds"].sort()
    injected_context.pop("sha256")
    injected_context["sha256"] = hashlib.sha256(cli.canonical_json(injected_context)).hexdigest()
    assert "fallback-relationships" in cli.validate_v2_receipt(injected_context)


def test_invalid_json_findings_receipt_retains_rejected_contract_evaluation(
    tmp_path: Path,
) -> None:
    fake_llm = write_fake_llm(tmp_path)

    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--response-contract",
        "findings-json",
        env={"LLM_BIN": str(fake_llm), "LLM_SCHEMA_RESPONSE": "not json"},
    )

    assert result.returncode == 1
    receipt_path = Path(result.stdout.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    attempt = receipt["attempts"][0]
    evaluation = attempt["contractEvaluation"]
    assert receipt["receiptSchemaVersion"] == 2
    assert attempt["number"] == 1
    assert receipt["result"] == "unavailable"
    assert receipt["acceptedAttempt"] is None
    assert attempt["result"] == "incomplete"
    assert evaluation["normalizedSha256"] is None
    assert evaluation["normalizedValue"] is None
    assert evaluation["violations"] == ["invalid-json"]
    verified = run_cli("verify", str(receipt_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


@pytest.mark.parametrize(
    ("contents", "expected_violation"),
    [
        ("{", "json-receipt"),
        ('{"value":NaN}', "json-receipt"),
        ('{"value":Infinity}', "json-receipt"),
        ('{"value":-Infinity}', "json-receipt"),
        ('{"receiptSchemaVersion":2,"attempts":[],"attempts":[]}', "json-receipt"),
        ("[]", "receipt-object"),
        ('{"receiptSchemaVersion":3}', "receipt-schema-version"),
        ('{"receiptSchemaVersion":2,"attempts":[null,true]}', "attempts"),
    ],
)
def test_verify_reports_malformed_and_hostile_receipts_without_traceback(
    tmp_path: Path, contents: str, expected_violation: str
) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(contents)

    verified = run_cli("verify", str(receipt_path))

    assert verified.returncode == 1
    assert "Traceback" not in verified.stderr
    result = json.loads(verified.stdout)
    assert result["valid"] is False
    assert expected_violation in result["violations"]


def test_verify_reports_huge_json_integer_without_traceback(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"receiptSchemaVersion":' + "9" * 5000 + "}")

    verified = run_cli("verify", str(receipt_path))

    assert verified.returncode == 1
    assert "Traceback" not in verified.stderr
    assert json.loads(verified.stdout)["violations"] == ["json-receipt"]


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_cli_canonical_json_rejects_non_finite_numbers(constant: float) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        cli.canonical_json({"extension.example": constant})


def test_cli_canonical_json_rejects_non_string_object_keys() -> None:
    with pytest.raises(ValueError, match="object keys must be strings"):
        cli.canonical_json({"extension.example": {1: "hostile"}})


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_numeric_value_rejects_non_finite_transport_metadata(constant: float) -> None:
    assert cli.numeric_value(constant) is None


def test_numeric_value_rejects_oversized_transport_metadata_without_exception() -> None:
    assert cli.numeric_value(10**4000) is None


@pytest.mark.parametrize(("value", "expected"), [(1, 1.0), (1.25, 1.25)])
def test_numeric_value_preserves_finite_transport_metadata(
    value: int | float, expected: float
) -> None:
    assert cli.numeric_value(value) == expected


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_valid_receipt_fails_closed_for_non_finite_extension(constant: float) -> None:
    receipt = {"extension.example": constant, "sha256": "0" * 64}

    assert cli.valid_receipt(receipt) is False


def test_policy_check_and_tournament_complete_when_under_budget(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    policy = tmp_path / "policy.toml"
    policy.write_text(
        """[models.accepted]
source_allowed = true
zdr = "yes"
data_collection = "deny"
"""
    )
    allowed = run_cli("policy-check", "--policy", str(policy), "--model", "accepted")
    denied = run_cli("policy-check", "--policy", str(policy), "--model", "unknown")
    enforced_denied = run_cli(
        "policy-check", "--policy", str(policy), "--model", "unknown", "--enforce"
    )
    assert allowed.returncode == 0
    assert json.loads(allowed.stdout)["sourceAllowed"] is True
    assert denied.returncode == 0
    assert json.loads(denied.stdout)["mode"] == "advisory"
    assert enforced_denied.returncode == 3

    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "complete.toml"
    tournament.write_text(
        f"""budget_usd = 1
artifact_root = "{tmp_path / "complete-artifacts"}"
max_output_tokens = 8

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "case"
prompt = "Review synthetic source. Return VERDICT and numbered findings."
files = ["{source}"]
"""
    )
    completed = run_cli("tournament", "--plan", str(tournament), env={"LLM_BIN": str(fake_llm)})
    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "complete-artifacts" / "tournament.json").read_text())
    assert report["result"] == "completed"
    assert report["actualSpendUsd"] == 0
    assert report["runs"][0]["result"] == "accepted"
    assert report["runs"][0]["exitCode"] == 0


def test_tournament_uses_the_configured_direct_openrouter_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "openrouter.toml"
    tournament.write_text(
        f'''budget_usd = 1
artifact_root = "{tmp_path / "openrouter-artifacts"}"
max_output_tokens = 8
transport = "openrouter"
provider = {{ only = ["ionstream"], allow_fallbacks = false }}

[models.test-model]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "case"
prompt = "Review synthetic source. Return JSON."
files = ["{source}"]
'''
    )
    transports: list[tuple[str, list[str], bool | None]] = []

    def fake_run_review(parser: object, args: object) -> int:
        transport = args.transport
        artifact_root = Path(args.artifact_root)
        turn = artifact_root / "turn"
        turn.mkdir(parents=True)
        (turn / "receipt.json").write_text(
            json.dumps(
                {
                    "result": "accepted",
                    "findings": [],
                    "response": {"costUsd": 0.0},
                    "transport": transport,
                }
            )
        )
        transports.append((transport, args.provider_only, args.provider_allow_fallbacks))
        return 0

    monkeypatch.setattr(cli, "run_review", fake_run_review)
    parser = cli.build_parser()
    args = parser.parse_args(["tournament", "--plan", str(tournament)])

    assert args.handler(args) == 0
    assert transports == [("openrouter", ["ionstream"], False)]


def test_tournament_rejects_an_unsupported_configured_transport(tmp_path: Path) -> None:
    tournament = tmp_path / "invalid-transport.toml"
    tournament.write_text(
        """budget_usd = 1
max_output_tokens = 8
transport = "carrier-pigeon"

[models.test]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "case"
prompt = "Review."
files = ["source.py"]
"""
    )

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2
    assert "supported transport" in result.stderr


@pytest.mark.parametrize(
    "plan_body",
    [
        """response_contract = "product-review-json"
[[candidates]]
id = "candidate"
family = "test"
model = "test"
transport = "codex"
cost_mode = "account-included"
""",
        """[models.test]
input_per_million_usd = 0
output_per_million_usd = 0
""",
    ],
)
def test_tournament_rejects_non_positive_plan_timeout(tmp_path: Path, plan_body: str) -> None:
    tournament = tmp_path / "timeout.toml"
    tournament.write_text(
        f"""budget_usd = 1
max_output_tokens = 8
timeout_seconds = 0
{plan_body}
[[cases]]
id = "case"
prompt = "Review."
files = []
"""
    )
    parser = cli.build_parser()
    args = parser.parse_args(["tournament", "--plan", str(tournament)])

    with pytest.raises(SystemExit):
        cli.run_tournament(parser, args)


def test_tournament_records_actual_provider_cost_from_review_receipt(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "costed.toml"
    tournament.write_text(
        f'''budget_usd = 1
artifact_root = "{tmp_path / "costed-artifacts"}"
max_output_tokens = 8

[models.costed]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "case"
prompt = "Review synthetic source. Return VERDICT and numbered findings."
files = ["{source}"]
'''
    )

    completed = run_cli("tournament", "--plan", str(tournament), env={"LLM_BIN": str(fake_llm)})

    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "costed-artifacts" / "tournament.json").read_text())
    assert report["actualSpendUsd"] == 0.125
    assert report["runs"][0]["actualCostUsd"] == 0.125


def test_legacy_tournament_counts_cost_from_every_retry_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "legacy-retries.toml"
    tournament.write_text(
        f'''budget_usd = 1
artifact_root = "{tmp_path / "artifacts"}"
max_output_tokens = 8
max_attempts = 2

[models.test]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "case"
prompt = "Review synthetic source."
files = ["{source}"]
'''
    )

    def fake_run_review(parser: object, args: object) -> int:
        turn = Path(args.artifact_root) / "turn"
        turn.mkdir(parents=True)
        (turn / "receipt.json").write_text(
            json.dumps(
                {
                    "result": "accepted",
                    "findings": [],
                    "attempts": [{"costUsd": 0.1}, {"costUsd": 0.2}],
                    "response": {"costUsd": 0.2},
                }
            )
        )
        return 0

    monkeypatch.setattr(cli, "run_review", fake_run_review)
    parser = cli.build_parser()
    args = parser.parse_args(["tournament", "--plan", str(tournament)])

    assert cli.run_tournament(parser, args) == 0
    report = json.loads((tmp_path / "artifacts" / "tournament.json").read_text())
    assert report["actualSpendUsd"] == pytest.approx(0.3)
    assert report["runs"][0]["actualCostUsd"] == pytest.approx(0.3)


def test_scores_findings_by_adjudicated_file_line_and_severity() -> None:
    score = cli.score_findings(
        expected=[
            {"path": "commodity_equilibrium.py", "line": 8, "severity": "critical"},
            {
                "path": "outbox_lease.py",
                "line": 3,
                "severity": "high",
                "symbol": "claim_next",
            },
        ],
        findings=[
            {
                "path": "/packet/commodity_equilibrium.py",
                "line": 8,
                "severity": "CRITICAL",
            },
            {"path": "def claim_next(...)", "line": 3, "severity": "medium"},
            {"path": "clean_audit.py", "line": 1, "severity": "high"},
        ],
    )

    assert score == {
        "expected": 2,
        "matched": 1,
        "falsePositives": 2,
        "lineAccurate": 2,
        "precision": 1 / 3,
        "recall": 1 / 2,
    }


def test_score_requires_a_filename_when_the_rubric_has_no_symbol() -> None:
    score = cli.score_findings(
        expected=[{"path": "unsafe_migration.sql", "line": 2, "severity": "critical"}],
        findings=[{"path": "unrelated", "line": 2, "severity": "critical"}],
    )

    assert score["lineAccurate"] == 0
    assert score["matched"] == 0


def test_score_accepts_a_finding_within_an_adjudicated_source_range() -> None:
    score = cli.score_findings(
        expected=[
            {
                "path": "commodity_equilibrium.py",
                "line_start": 4,
                "line_end": 8,
                "severity": "high",
            }
        ],
        findings=[{"path": "commodity_equilibrium.py", "line": 4, "severity": "HIGH"}],
    )

    assert score["matched"] == 1
    assert score["lineAccurate"] == 1


def test_score_accepts_a_more_severe_finding_at_the_adjudicated_location() -> None:
    score = cli.score_findings(
        expected=[{"path": "commodity_equilibrium.py", "line": 4, "severity": "high"}],
        findings=[{"path": "commodity_equilibrium.py", "line": 4, "severity": "critical"}],
    )

    assert score["matched"] == 1
    assert score["falsePositives"] == 0
    assert score["precision"] == 1
    assert score["recall"] == 1


def test_packet_prompt_requires_reviewers_to_use_supplied_file_basenames(tmp_path: Path) -> None:
    source = tmp_path / "accounting_rule.py"
    source.write_text("pass\n")

    prompt = cli.packet_prompt("Review the synthetic rule.", [source])

    assert "accounting_rule.py" in prompt
    assert "exact supplied basename" in prompt
    assert "only defects demonstrable" in prompt


def test_writes_an_incremental_tournament_checkpoint(tmp_path: Path) -> None:
    report_path = tmp_path / "tournament.json"

    cli.write_tournament_report(
        report_path,
        budget=50,
        estimated_spend=0.2,
        actual_spend=0.1,
        result="running",
        runs=[{"case": "synthetic-case", "model": "model"}],
    )

    assert json.loads(report_path.read_text()) == {
        "actualSpendUsd": 0.1,
        "budgetUsd": 50,
        "estimatedSpendUsd": 0.2,
        "result": "running",
        "runs": [{"case": "synthetic-case", "model": "model"}],
    }


def test_tournament_can_limit_execution_to_named_cases(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "cases.toml"
    tournament.write_text(
        f'''budget_usd = 1
artifact_root = "{tmp_path / "case-artifacts"}"
max_output_tokens = 8

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "first"
prompt = "Review synthetic source. Return VERDICT and numbered findings."
files = ["{source}"]

[[cases]]
id = "second"
prompt = "Review synthetic source. Return VERDICT and numbered findings."
files = ["{source}"]
'''
    )

    completed = run_cli(
        "tournament",
        "--plan",
        str(tournament),
        "--case",
        "second",
        env={"LLM_BIN": str(fake_llm)},
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "case-artifacts" / "tournament.json").read_text())
    assert [run["case"] for run in report["runs"]] == ["second"]


def test_tournament_rejects_unknown_named_case(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "case.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 8

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "known"
prompt = "Review synthetic source. Return VERDICT and numbered findings."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament), "--case", "missing")

    assert result.returncode == 2
    assert "does not contain every requested --case" in result.stderr


def test_tournament_rejects_agy_without_an_enforceable_output_budget(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.py"
    source.write_text("pass\n")
    tournament = tmp_path / "agy.toml"
    tournament.write_text(
        f'''budget_usd = 1
max_output_tokens = 8
transport = "agy"

[models.gemini]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "synthetic"
prompt = "Review synthetic source. Return JSON."
files = ["{source}"]
'''
    )

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2
    assert "cannot cap output tokens" in result.stderr


@pytest.mark.parametrize(
    ("preferences", "message"),
    [
        (["ionstream"], "unsupported fields"),
        ({"only": []}, "non-empty string list"),
        ({"order": ["ionstream", 2]}, "non-empty string list"),
        ({"allow_fallbacks": "false"}, "must be boolean"),
        ({"require_parameters": "true"}, "must be boolean"),
        ({"data_collection": "unknown"}, "must be allow or deny"),
        ({"sort": "random"}, "must be price, throughput, or latency"),
    ],
)
def test_normalize_provider_preferences_rejects_invalid_values(
    preferences: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        cli.normalize_provider_preferences(preferences)


def test_normalize_provider_preferences_preserves_complete_policy() -> None:
    assert cli.normalize_provider_preferences(
        {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "only": ["ionstream"],
            "order": ["ionstream", "deepinfra"],
            "sort": "throughput",
        }
    ) == {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "only": ["ionstream"],
        "order": ["ionstream", "deepinfra"],
        "sort": "throughput",
    }


def test_assess_pinned_provider_endpoint_requires_an_active_json_route_at_declared_price() -> None:
    candidate = cli.TournamentCandidate(
        council_eligible=True,
        family="deepseek",
        identifier="flash-cloudflare",
        model="openrouter/deepseek/deepseek-v4-flash-0731",
        cost_mode="metered",
        pricing=(0.14, 0.28),
        provider_preferences={
            "allow_fallbacks": False,
            "only": ["cloudflare"],
            "require_parameters": True,
        },
        transport="openrouter",
    )
    healthy = {
        "provider_name": "Cloudflare",
        "pricing": {"prompt": "0.00000014", "completion": "0.00000028"},
        "status": 0,
        "supported_parameters": ["response_format", "structured_outputs"],
    }

    assert cli.assess_pinned_provider_endpoint(candidate, [healthy]) == {
        "candidate": "flash-cloudflare",
        "model": "deepseek/deepseek-v4-flash-0731",
        "provider": "cloudflare",
        "result": "accepted",
        "reasons": [],
    }

    response_format_only = {
        **healthy,
        "supported_parameters": ["response_format"],
    }
    assert cli.assess_pinned_provider_endpoint(candidate, [response_format_only]) == {
        "candidate": "flash-cloudflare",
        "model": "deepseek/deepseek-v4-flash-0731",
        "provider": "cloudflare",
        "result": "rejected",
        "reasons": ["missing-structured-outputs"],
    }

    unavailable = {
        **healthy,
        "pricing": {"prompt": "0.00000010", "completion": "0.00000020"},
        "status": -2,
        "supported_parameters": [],
    }
    assert cli.assess_pinned_provider_endpoint(candidate, [unavailable]) == {
        "candidate": "flash-cloudflare",
        "model": "deepseek/deepseek-v4-flash-0731",
        "provider": "cloudflare",
        "result": "rejected",
        "reasons": [
            "inactive",
            "missing-response-format",
            "missing-structured-outputs",
            "price-mismatch",
        ],
    }


def test_provider_endpoint_helpers_cover_unpinned_invalid_and_missing_routes() -> None:
    candidate = cli.TournamentCandidate(
        council_eligible=True,
        family="deepseek",
        identifier="flash-cloudflare",
        model="openrouter/deepseek/deepseek-v4-flash-0731",
        cost_mode="metered",
        pricing=(0.14, 0.28),
        provider_preferences={"only": ["cloudflare"]},
        transport="openrouter",
    )

    assert cli.resolved_provider_matches({"sort": "price"}, None)
    assert cli.endpoint_price_per_million({"pricing": {"prompt": "not-a-number"}}, "prompt") is None
    assert cli.assess_pinned_provider_endpoint(candidate, []) == {
        "candidate": "flash-cloudflare",
        "model": "deepseek/deepseek-v4-flash-0731",
        "provider": "cloudflare",
        "result": "rejected",
        "reasons": ["provider-not-found"],
    }
    invalid_candidate = cli.TournamentCandidate(
        council_eligible=True,
        family="deepseek",
        identifier="not-pinned",
        model="openrouter/deepseek/deepseek-v4-flash-0731",
        cost_mode="metered",
        pricing=(0.14, 0.28),
        provider_preferences={"only": ["cloudflare", "deepinfra"]},
        transport="openrouter",
    )
    with pytest.raises(ValueError, match="exactly one pinned"):
        cli.assess_pinned_provider_endpoint(invalid_candidate, [])


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, (127, "OPENROUTER_API_KEY is not configured", None)),
        (
            cli.urlerror.HTTPError("https://example.test", 429, "rate", {}, BytesIO(b"exhausted")),
            (429, "exhausted", None),
        ),
        (cli.urlerror.URLError("offline"), (502, "offline", None)),
        (TimeoutError(), (124, "provider preflight timed out", None)),
        (b"{", (502, "OpenRouter returned invalid endpoint JSON", None)),
        (b"[]", (502, "OpenRouter returned an invalid endpoint response", None)),
    ],
)
def test_fetch_openrouter_model_endpoints_handles_missing_and_invalid_transport_states(
    monkeypatch: pytest.MonkeyPatch, outcome: object, expected: tuple[object, ...]
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *unused: object) -> None:
            return None

        def read(self) -> bytes:
            assert isinstance(outcome, bytes)
            return outcome

    def fake_urlopen(*unused: object, **ignored: object) -> FakeResponse:
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse()

    monkeypatch.setattr(cli.urlrequest, "urlopen", fake_urlopen)
    api_key = None if outcome is None else "test-key"

    assert (
        cli.fetch_openrouter_model_endpoints(
            api_key=api_key,
            model="deepseek/test model",
            timeout_seconds=1,
        )
        == expected
    )


def test_fetch_openrouter_model_endpoints_reads_a_wrapped_endpoint_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *unused: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"data":{"endpoints":[]}}'

    def fake_urlopen(request: object, *, timeout: int) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(cli.urlrequest, "urlopen", fake_urlopen)

    assert cli.fetch_openrouter_model_endpoints(
        api_key="test-key", model="deepseek/test model", timeout_seconds=7
    ) == (0, "", {"endpoints": []})
    request = captured["request"]
    assert request.full_url.endswith("deepseek/test%20model/endpoints")
    assert request.get_header("Authorization") == "Bearer test-key"
    assert captured["timeout"] == 7


def test_provider_preflight_writes_a_snapshot_and_blocks_a_rejected_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "matrix.toml"
    plan.write_text(
        f'''artifact_root = "{tmp_path / "artifacts"}"

[[candidates]]
id = "flash-cloudflare"
family = "deepseek"
model = "openrouter/deepseek/deepseek-v4-flash-0731"
transport = "openrouter"
cost_mode = "metered"
pricing = {{ input_per_million_usd = 0.14, output_per_million_usd = 0.28 }}
provider = {{ only = ["cloudflare"], allow_fallbacks = false, require_parameters = true }}
'''
    )

    def fake_endpoints(**arguments: object) -> tuple[int, str, dict[str, object] | None]:
        assert arguments["model"] == "deepseek/deepseek-v4-flash-0731"
        return (
            0,
            "",
            {
                "endpoints": [
                    {
                        "provider_name": "Cloudflare",
                        "pricing": {"prompt": "0.00000014", "completion": "0.00000028"},
                        "status": 0,
                        "supported_parameters": ["response_format", "structured_outputs"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli, "fetch_openrouter_model_endpoints", fake_endpoints)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    parser = cli.build_parser()
    args = parser.parse_args(["provider-preflight", "--plan", str(plan)])

    assert args.handler(args) == 0
    output = Path(capsys.readouterr().out.strip())
    snapshot = json.loads(output.read_text())
    assert snapshot["result"] == "accepted"
    assert snapshot["assessments"][0]["provider"] == "cloudflare"
    assert (
        snapshot["endpoints"]["deepseek/deepseek-v4-flash-0731"]["endpoints"][0]["provider_name"]
        == "Cloudflare"
    )


def test_provider_preflight_records_fetch_failure_and_reuses_a_model_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = tmp_path / "matrix.toml"
    plan.write_text(
        f'''artifact_root = "{tmp_path / "artifacts"}"

[[candidates]]
id = "flash-cloudflare"
family = "deepseek"
model = "openrouter/deepseek/deepseek-v4-flash-0731"
transport = "openrouter"
cost_mode = "metered"
pricing = {{ input_per_million_usd = 0.14, output_per_million_usd = 0.28 }}
provider = {{ only = ["cloudflare"] }}

[[candidates]]
id = "flash-deepinfra"
family = "deepseek"
model = "openrouter/deepseek/deepseek-v4-flash-0731"
transport = "openrouter"
cost_mode = "metered"
pricing = {{ input_per_million_usd = 0.09, output_per_million_usd = 0.18 }}
provider = {{ only = ["deepinfra"] }}
'''
    )
    calls = 0

    def fake_endpoints(**unused: object) -> tuple[int, str, dict[str, object] | None]:
        nonlocal calls
        calls += 1
        return 502, "offline", None

    monkeypatch.setattr(cli, "fetch_openrouter_model_endpoints", fake_endpoints)
    parser = cli.build_parser()
    args = parser.parse_args(["provider-preflight", "--plan", str(plan)])

    assert args.handler(args) == 4
    snapshot = json.loads(Path(capsys.readouterr().out.strip()).read_text())
    assert calls == 1
    assert snapshot["result"] == "rejected"
    assert [item["reasons"] for item in snapshot["assessments"]] == [
        ["endpoint-fetch-failed"],
        ["endpoint-fetch-failed"],
    ]


def test_provider_preflight_rejects_missing_plan_invalid_plan_and_invalid_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = run_cli("provider-preflight", "--plan", str(tmp_path / "missing.toml"))
    assert missing.returncode == 2
    assert "does not exist" in missing.stderr

    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[[candidates]]\nid = "bad"\n')
    result = run_cli("provider-preflight", "--plan", str(invalid))
    assert result.returncode == 2
    assert "requires non-empty id, family, and model" in result.stderr

    no_pins = tmp_path / "no-pins.toml"
    no_pins.write_text(
        """[[candidates]]
id = "native"
family = "gemini"
model = "gemini-3.6-flash"
transport = "agy"
cost_mode = "subscription"
"""
    )
    result = run_cli("provider-preflight", "--plan", str(no_pins))
    assert result.returncode == 2
    assert "requires pinned OpenRouter" in result.stderr

    malformed = tmp_path / "malformed.toml"
    malformed.write_text(
        f'''artifact_root = "{tmp_path / "artifacts"}"
[[candidates]]
id = "flash-cloudflare"
family = "deepseek"
model = "openrouter/deepseek/deepseek-v4-flash-0731"
transport = "openrouter"
cost_mode = "metered"
pricing = {{ input_per_million_usd = 0.14, output_per_million_usd = 0.28 }}
provider = {{ only = ["cloudflare"] }}
'''
    )

    monkeypatch.setattr(
        cli,
        "fetch_openrouter_model_endpoints",
        lambda **unused: (0, "", {"endpoints": ["not-an-object"]}),
    )
    parser = cli.build_parser()
    args = parser.parse_args(["provider-preflight", "--plan", str(malformed)])
    with pytest.raises(SystemExit):
        args.handler(args)


def test_run_rejects_provider_preferences_without_openrouter(tmp_path: Path) -> None:
    result = run_cli(*review_arguments(tmp_path, "accepted"), "--provider-only", "ionstream")

    assert result.returncode == 2
    assert "require --transport openrouter" in result.stderr


def test_run_rejects_invalid_openrouter_provider_preferences_before_request(tmp_path: Path) -> None:
    result = run_cli(
        *review_arguments(tmp_path, "accepted"),
        "--transport",
        "openrouter",
        "--provider-only",
        "",
    )

    assert result.returncode == 2
    assert "provider.only must be a non-empty string list" in result.stderr


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        ('provider = { sort = "random" }', "must be price, throughput, or latency"),
        ('provider = { only = ["ionstream"] }', "require transport = openrouter"),
    ],
)
def test_tournament_rejects_invalid_or_incompatible_provider_preferences(
    tmp_path: Path, provider: str, message: str
) -> None:
    tournament = tmp_path / "provider.toml"
    tournament.write_text(f"budget_usd = 1\nmax_output_tokens = 8\n{provider}\n")

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize(
    "contents",
    [
        "budget_usd = 0\nmax_output_tokens = 8\n",
        "budget_usd = 1\nmax_output_tokens = 0\n",
        "budget_usd = 1\nmax_output_tokens = 8\n",
    ],
)
def test_tournament_rejects_invalid_budget_or_empty_plan(tmp_path: Path, contents: str) -> None:
    tournament = tmp_path / "invalid.toml"
    tournament.write_text(contents)

    result = run_cli("tournament", "--plan", str(tournament))

    assert result.returncode == 2


def test_tournament_resolves_case_and_artifact_paths_relative_to_its_plan(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    plan_directory = tmp_path / "plan"
    plan_directory.mkdir()
    invocation_directory = tmp_path / "invocation"
    invocation_directory.mkdir()
    source = plan_directory / "case.py"
    source.write_text("pass\n")
    tournament = plan_directory / "relative.toml"
    tournament.write_text(
        """budget_usd = 1
artifact_root = "artifacts"
max_output_tokens = 8

[models.accepted]
input_per_million_usd = 0
output_per_million_usd = 0

[[cases]]
id = "relative"
prompt = "Review synthetic source. Return VERDICT and numbered findings."
files = ["case.py"]
"""
    )
    result = subprocess.run(
        [sys.executable, "-m", "reviewctl", "tournament", "--plan", str(tournament)],
        cwd=invocation_directory,
        env={**os.environ, "LLM_BIN": str(fake_llm)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (plan_directory / "artifacts" / "tournament.json").is_file()


def test_structured_contract_rejects_unparseable_or_incomplete_findings() -> None:
    assert cli.validate_review_response("VERDICT: approved.", "findings-json") is None
    assert cli.validate_review_response('{"verdict": "approved"}', "findings-json") is None
    assert (
        cli.validate_review_response(
            '{"verdict":"approved","findings":[{"severity":"high"}]}', "findings-json"
        )
        is None
    )


def test_structured_contract_extracts_a_portable_finding() -> None:
    response = cli.validate_review_response(
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [
                    {
                        "severity": "critical",
                        "path": "example.py",
                        "line": 12,
                        "title": "Idempotency key is not unique",
                        "evidence": "A second request is accepted.",
                        "reproduction": "Submit the same key twice.",
                    }
                ],
            }
        ),
        "findings-json",
    )

    assert response == {
        "verdict": "changes-requested",
        "findings": [
            {
                "severity": "critical",
                "path": "example.py",
                "line": 12,
                "title": "Idempotency key is not unique",
                "evidence": "A second request is accepted.",
                "reproduction": "Submit the same key twice.",
            }
        ],
    }


def test_structured_contract_rejects_findings_outside_the_supplied_packet() -> None:
    response = cli.validate_review_response(
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [
                    {
                        "severity": "high",
                        "path": "invented.py",
                        "line": 1,
                        "title": "Invented path",
                        "evidence": "Not in packet.",
                        "reproduction": "N/A",
                    }
                ],
                "reviewedFiles": ["source.py"],
            }
        ),
        "findings-json",
        expected_file_hashes={"source.py": "a" * 64},
    )

    assert response is None


@pytest.mark.parametrize(
    "response",
    [
        "[]",
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [
                    {
                        "severity": "",
                        "path": "src/example.py",
                        "line": 1,
                        "title": "title",
                        "evidence": "evidence",
                        "reproduction": "reproduction",
                    }
                ],
            }
        ),
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [
                    {
                        "severity": "high",
                        "path": "src/example.py",
                        "line": 0,
                        "title": "title",
                        "evidence": "evidence",
                        "reproduction": "reproduction",
                    }
                ],
            }
        ),
    ],
)
def test_structured_contract_rejects_invalid_finding_shapes(response: str) -> None:
    assert cli.validate_review_response(response, "findings-json") is None


def test_invoke_llm_attaches_schema_for_structured_contract(tmp_path: Path) -> None:
    fake_llm = write_fake_llm(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    exit_code, _ = cli.invoke_llm(
        llm_bin=str(fake_llm),
        prompt="Review",
        model="accepted",
        database=tmp_path / "structured.sqlite3",
        files=[source],
        max_output_tokens=20,
        response_contract="findings-json",
        timeout_seconds=1,
    )

    assert exit_code == 0


def test_openrouter_packet_makes_findings_verdict_semantics_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    packet = cli.openrouter_packet("Review", [source])

    assert "changes-requested" in packet
    assert "approved" in packet
    assert "exactly six fields" in packet


def test_invoke_openrouter_persists_a_portable_structured_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "idempotency.py"
    source.write_text("def post() -> None:\n    pass\n")
    captured = mock_openrouter_curl(
        monkeypatch,
        body=json.dumps(
            {
                "model": "deepseek/deepseek-v4-flash-0731",
                "provider": "Test Provider",
                "choices": [
                    {"message": {"content": json.dumps({"verdict": "approved", "findings": []})}}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost": 0.001},
            }
        ).encode(),
    )

    exit_code, error, response = cli.invoke_openrouter(
        api_key="not-persisted",
        prompt="Return JSON matching the supplied schema.",
        model="deepseek/deepseek-v4-flash-0731",
        files=[source],
        max_output_tokens=64,
        response_contract="findings-json",
        timeout_seconds=7,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 0
    assert error == ""
    assert response.model == "deepseek/deepseek-v4-flash-0731"
    assert response.provider == "Test Provider"
    assert response.cost_usd == 0.001
    assert response.response == '{"verdict": "approved", "findings": []}'
    assert json.loads((tmp_path / "request.json").read_text())["model"] == response.model
    assert json.loads((tmp_path / "response.json").read_text())["provider"] == response.provider
    command = captured["command"]
    assert "https://openrouter.ai/api/v1/chat/completions" in command
    assert "Authorization: Bearer not-persisted" not in command
    assert "Authorization: Bearer not-persisted" in captured["config"]
    assert command[command.index("--config") + 1] == "-"
    assert command[command.index("--data-binary") + 1] == f"@{tmp_path / 'request.json'}"
    assert "--fail-with-body" not in command
    assert "not-persisted" not in (tmp_path / "request.json").read_text()
    assert command[command.index("--max-time") + 1] == "7"
    assert captured["timeout"] == 8


@pytest.mark.parametrize(
    ("model", "expected_reasoning"),
    [
        ("google/gemini-3.6-flash", {"effort": "minimal"}),
        ("z-ai/glm-5.2", {"effort": "none"}),
    ],
)
def test_invoke_openrouter_configures_reasoning_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model: str,
    expected_reasoning: dict[str, object],
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(
        monkeypatch,
        body=json.dumps(
            {
                "model": model,
                "choices": [{"message": {"content": "VERDICT: approved"}}],
            }
        ).encode(),
    )

    exit_code, error, _ = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model=model,
        files=[source],
        max_output_tokens=12000,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert (exit_code, error) == (0, "")
    assert json.loads((tmp_path / "request.json").read_text())["reasoning"] == expected_reasoning


def test_invoke_openrouter_rejects_a_malformed_choice_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(monkeypatch, body=b'{"id":"turn","choices":[null]}')

    exit_code, error, response = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert (exit_code, error, response.response) == (
        502,
        "OpenRouter returned malformed choices",
        "",
    )


def test_invoke_openrouter_enforces_an_absolute_response_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    captured = mock_openrouter_curl(monkeypatch, returncode=28, status=200)

    started = time.monotonic()
    exit_code, error, response = cli.invoke_openrouter(
        api_key="not-persisted",
        prompt="Return JSON matching the supplied schema.",
        model="deepseek/test",
        files=[source],
        max_output_tokens=64,
        response_contract="findings-json",
        timeout_seconds=0.01,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 124
    assert error == "review attempt timed out"
    assert response.response == ""
    assert time.monotonic() - started < 0.15
    assert captured["command"][captured["command"].index("--max-time") + 1] == "0.01"


def test_parser_rejects_non_positive_openrouter_timeouts() -> None:
    parser = cli.build_parser()

    with pytest.raises(argparse.ArgumentTypeError):
        cli.positive_timeout_seconds("not-a-number")
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--review-id", "zero", "--timeout-seconds", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["provider-preflight", "--plan", "plan.toml", "--timeout-seconds", "-1"])


def test_invoke_openrouter_handles_missing_curl_and_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *unused, **ignored: (_ for _ in ()).throw(FileNotFoundError()),
    )
    missing = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "missing-request.json",
        response_path=tmp_path / "missing-response.json",
    )
    assert missing[0:2] == (127, "OpenRouter transport executable not found: curl")

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *unused, **ignored: (_ for _ in ()).throw(PermissionError("permission denied")),
    )
    not_executable = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "not-executable-request.json",
        response_path=tmp_path / "not-executable-response.json",
    )
    assert not_executable[0] == 127
    assert "could not execute" in not_executable[1]

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *unused, **ignored: (_ for _ in ()).throw(subprocess.TimeoutExpired("curl", 2)),
    )
    timed_out = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "timeout-request.json",
        response_path=tmp_path / "timeout-response.json",
    )
    assert timed_out[0:2] == (124, "review attempt timed out")


def test_invoke_openrouter_rejects_missing_or_error_http_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(monkeypatch, status="not-a-status")

    no_status = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "no-status-request.json",
        response_path=tmp_path / "no-status-response.json",
    )
    assert no_status[0:2] == (502, "OpenRouter transport did not report an HTTP status")

    mock_openrouter_curl(monkeypatch, status=429, body=b'{"error":"limited"}')
    http_error = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "http-request.json",
        response_path=tmp_path / "http-response.json",
    )
    assert http_error[0:2] == (429, '{"error":"limited"}')


def test_invoke_openrouter_forwards_requested_provider_preferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(
        monkeypatch,
        body=(
            b'{"id":"turn","model":"test-model","provider":"Ionstream",'
            b'"choices":[{"message":{"content":"{\\"verdict\\":\\"approved\\",\\"findings\\":[]}"}}]}'
        ),
    )
    preferences = {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "only": ["ionstream"],
        "sort": "throughput",
    }
    exit_code, _, response = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        provider_preferences=preferences,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 0
    assert response.provider == "Ionstream"
    assert json.loads((tmp_path / "request.json").read_text())["provider"] == preferences
    assert json.loads((tmp_path / "request.json").read_text())["provider"] == preferences


def test_invoke_openrouter_limits_gemini_36_flash_reasoning_for_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(
        monkeypatch,
        body=(
            b'{"id":"turn","model":"google/gemini-3.6-flash",'
            b'"choices":[{"message":{"content":"hola desde OpenRouter"}}]}'
        ),
    )

    exit_code, _, response = cli.invoke_openrouter(
        api_key="test",
        prompt="Say hello.",
        model="google/gemini-3.6-flash",
        files=[source],
        max_output_tokens=256,
        response_contract="document",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 0
    assert response.response == "hola desde OpenRouter"
    assert json.loads((tmp_path / "request.json").read_text())["reasoning"] == {"effort": "minimal"}


def test_invoke_openrouter_requires_an_api_key(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    exit_code, error, response = cli.invoke_openrouter(
        api_key=None,
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 127
    assert error == "OPENROUTER_API_KEY is not configured"
    assert response.response == ""
    assert not (tmp_path / "request.json").exists()


@pytest.mark.parametrize(
    (
        "returncode",
        "status",
        "body",
        "stderr",
        "expected_exit",
        "expected_error",
        "writes_response",
    ),
    [
        (
            22,
            429,
            b'{"error":"limited"}',
            b"HTTP 429",
            429,
            '{"error":"limited"}',
            True,
        ),
        (6, 0, b"", b"offline", 502, "offline", False),
        (28, 200, b"", b"timed out", 124, "review attempt timed out", False),
    ],
)
def test_invoke_openrouter_handles_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    status: int,
    body: bytes,
    stderr: bytes,
    expected_exit: int,
    expected_error: str,
    writes_response: bool,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(
        monkeypatch, body=body, returncode=returncode, status=status, stderr=stderr
    )
    response_path = tmp_path / "response.json"
    exit_code, error, response = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=response_path,
    )

    assert exit_code == expected_exit
    assert error == expected_error
    assert response.response == ""
    assert response_path.exists() is writes_response


@pytest.mark.parametrize("raw_response", [b"not-json", b"[]"])
def test_invoke_openrouter_rejects_unparseable_or_non_object_responses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw_response: bytes
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(monkeypatch, body=raw_response)
    exit_code, error, response = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 502
    assert error in {
        "OpenRouter returned invalid JSON",
        "OpenRouter returned a non-object response",
    }
    assert response.response == ""


def test_invoke_openrouter_classifies_an_embedded_provider_error_as_transport_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(
        monkeypatch, body=b'{"error":{"message":"The operation was aborted","code":504}}'
    )
    exit_code, error, response = cli.invoke_openrouter(
        api_key="test",
        prompt="Return JSON.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="findings-json",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 504
    assert error == "The operation was aborted"
    assert response.response == ""


def test_invoke_openrouter_omits_a_schema_for_an_unstructured_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    mock_openrouter_curl(
        monkeypatch,
        body=(
            b'{"id":"turn","model":"test-model",'
            b'"choices":[{"message":{"content":"VERDICT: approved."}}]}'
        ),
    )
    exit_code, _, _ = cli.invoke_openrouter(
        api_key="test",
        prompt="Return a verdict.",
        model="test-model",
        files=[source],
        max_output_tokens=10,
        response_contract="verdict",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 0
    assert "response_format" not in json.loads((tmp_path / "request.json").read_text())


def test_invoke_agy_persists_a_sandboxed_structured_response(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("def send() -> None: pass\n")
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"

    exit_code, error, response = cli.invoke_agy(
        agy_bin=str(fake_agy),
        prompt="Review the bounded synthetic source.",
        model="gemini-3.6-flash-medium",
        files=[source],
        max_output_tokens=123,
        response_contract="findings-json",
        timeout_seconds=7,
        request_path=request_path,
        response_path=response_path,
    )

    assert exit_code == 0
    assert error == ""
    assert response == cli.PersistedResponse(
        conversation_id="agy-conversation",
        cost_usd=None,
        duration_ms=1250,
        input_tokens=10,
        model="gemini-3.6-flash-medium",
        output_tokens=20,
        provider="google-antigravity",
        response='{"verdict": "approved", "findings": []}',
    )
    request_payload = json.loads(request_path.read_text())
    assert request_payload["command"] == "agy"
    assert request_payload["model"] == "gemini-3.6-flash-medium"
    assert request_payload["maxOutputTokens"] == 123
    assert "source.py" in request_payload["prompt"]
    assert json.loads(response_path.read_text())["conversation_id"] == "agy-conversation"


def test_invoke_agy_rejects_non_success_or_invalid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_agy = write_fake_agy(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    monkeypatch.setenv("AGY_STATUS", "FAILED")

    failed, error, response = cli.invoke_agy(
        agy_bin=str(fake_agy),
        prompt="Review synthetic source.",
        model="gemini-3.6-flash-low",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=7,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert failed == 502
    assert error == "agy returned status FAILED"
    assert response.response == ""
    monkeypatch.setenv("AGY_INVALID_JSON", "1")
    invalid, error, response = cli.invoke_agy(
        agy_bin=str(fake_agy),
        prompt="Review synthetic source.",
        model="gemini-3.6-flash-low",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=7,
        request_path=tmp_path / "invalid-request.json",
        response_path=tmp_path / "invalid-response.json",
    )

    assert invalid == 502
    assert error == "agy returned invalid JSON"
    assert response.response == ""


@pytest.mark.parametrize(
    ("environment", "expected_exit", "expected_error"),
    [
        ({"AGY_EXIT": "17"}, 17, ""),
        ({"AGY_LIST": "1"}, 502, "agy returned a non-object response"),
        ({"AGY_SLEEP": "3"}, 124, "review attempt timed out"),
    ],
)
def test_invoke_agy_handles_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    expected_exit: int,
    expected_error: str,
) -> None:
    fake_agy = write_fake_agy(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    exit_code, error, response = cli.invoke_agy(
        agy_bin=str(fake_agy),
        prompt="Review synthetic source.",
        model="gemini-3.6-flash-low",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == expected_exit
    assert error == expected_error
    assert response.response == ""


def test_invoke_agy_reports_a_missing_binary(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    exit_code, error, response = cli.invoke_agy(
        agy_bin=str(tmp_path / "missing-agy"),
        prompt="Review synthetic source.",
        model="gemini-3.6-flash-low",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=1,
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
    )

    assert exit_code == 127
    assert "No such file or directory" in error
    assert response.response == ""


def kiro_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "request_path": tmp_path / "request.json",
        "models_path": tmp_path / "models.json",
        "response_path": tmp_path / "response.log",
        "session_path": tmp_path / "session.json",
        "diagnostic_path": tmp_path / "stderr.log",
    }


def invoke_fake_kiro(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = "claude-sonnet-5",
    inventory_mode: str = "valid",
    timeout_seconds: int = 7,
) -> tuple[int, str, cli.PersistedResponse]:
    fake_kiro = write_fake_kiro(tmp_path, inventory_mode=inventory_mode)
    source = tmp_path / "source.py"
    source.write_text("def send() -> None: pass\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "must-not-leak")
    monkeypatch.setenv("SOME_TOKEN", "must-not-leak")
    return cli.invoke_kiro(
        kiro_bin=str(fake_kiro),
        prompt="Review the bounded synthetic source.",
        model=model,
        files=[source],
        max_output_tokens=123,
        response_contract="findings-json",
        timeout_seconds=timeout_seconds,
        **kiro_paths(tmp_path),
    )


def test_normalize_kiro_output_strips_only_terminal_framing() -> None:
    stdout = "\x1b[36mKiro CLI\x1b[0m\r\n> first line\r\nbody\r\n\r\n▸ Credits: 0.25\r\n".encode()

    assert cli.normalize_kiro_output(stdout) == "first line\nbody"
    assert cli.normalize_kiro_output(b"Kiro CLI\nno response marker\n") == ""


def test_kiro_process_environment_is_an_exact_allowlist() -> None:
    source = {
        "PATH": "/bin",
        "HOME": "/real/home",
        "LANG": "en_US.UTF-8",
        "SSL_CERT_FILE": "/cert.pem",
        "OPENROUTER_API_KEY": "secret",
        "AWS_SESSION_TOKEN": "secret",
        "CUSTOM_TOKEN": "secret",
    }

    assert cli.kiro_process_environment(source) == {
        "PATH": "/bin",
        "HOME": "/real/home",
        "LANG": "en_US.UTF-8",
        "SSL_CERT_FILE": "/cert.pem",
        "KIRO_LOG_NO_COLOR": "1",
    }


def test_invoke_kiro_uses_isolated_exact_commands_and_persists_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, error, response = invoke_fake_kiro(tmp_path, monkeypatch)

    assert exit_code == 0
    assert error == "token=super-secret-token-value\n"
    assert response == cli.PersistedResponse(
        "123e4567-e89b-12d3-a456-426614174000",
        None,
        response.duration_ms,
        None,
        "claude-sonnet-5",
        None,
        None,
        '{"verdict": "approved", "findings": []}',
    )
    observations = [
        json.loads(line) for line in (tmp_path / "kiro-observations.jsonl").read_text().splitlines()
    ]
    assert observations[0]["argv"] == ["chat", "--list-models", "--format", "json"]
    packet = cli.openrouter_packet(
        "Review the bounded synthetic source.", [tmp_path / "source.py"], "findings-json"
    )
    assert observations[1]["argv"] == [
        "chat",
        "--no-interactive",
        "--trust-tools=",
        "--agent",
        "kiro_default",
        "--model",
        "claude-sonnet-5",
        "--wrap",
        "never",
        packet,
    ]
    assert observations[1]["entries"] == []
    assert observations[2]["argv"] == ["chat", "--list-sessions", "--format", "json"]
    assert observations[1]["cwd"] == observations[2]["cwd"]
    assert observations[1]["cwd"] != str(tmp_path)
    assert "--- BEGIN source.py ---" in packet
    assert "def send() -> None: pass" in packet
    assert str(tmp_path / "source.py") not in packet
    for key in ("OPENROUTER_API_KEY", "AWS_ACCESS_KEY_ID", "SOME_TOKEN"):
        assert key not in observations[1]["environment"]
    expected_stdout = (
        '\x1b[36mKiro CLI\x1b[0m\n> {"verdict": "approved", "findings": []}\n\n▸ Credits: 0.25\n'
    ).encode()
    assert (tmp_path / "response.log").read_bytes() == expected_stdout
    assert (tmp_path / "stderr.log").read_text() == "[REDACTED_CREDENTIAL]\n"
    session = json.loads((tmp_path / "session.json").read_text())
    assert session[0]["sessions"][0]["sessionId"] == response.conversation_id
    models_bytes = (tmp_path / "models.json").read_bytes()
    manifest = json.loads((tmp_path / "request.json").read_text())
    assert manifest["model"] == "claude-sonnet-5"
    assert manifest["requestedMaxOutputTokens"] == 123
    assert manifest["outputTokenLimitEnforced"] is False
    assert manifest["models"] == {
        "path": str(tmp_path / "models.json"),
        "sha256": cli.sha256_bytes(models_bytes),
    }


def test_invoke_kiro_uses_one_deadline_across_all_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_kiro = write_fake_kiro(tmp_path, stage_delays=(0.4, 0.7, 0.4))
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    exit_code, error, response = cli.invoke_kiro(
        kiro_bin=str(fake_kiro),
        prompt="Review synthetic source.",
        model="claude-sonnet-5",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=1,
        **kiro_paths(tmp_path),
    )

    assert exit_code == 124
    assert error == "review attempt timed out"
    assert response.response == ""
    observations = [
        json.loads(line) for line in (tmp_path / "kiro-observations.jsonl").read_text().splitlines()
    ]
    assert [observation["argv"][:2] for observation in observations] == [
        ["chat", "--list-models"],
        ["chat", "--no-interactive"],
    ]


@pytest.mark.parametrize(
    ("inventory_mode", "model", "expected_code"),
    [
        ("malformed", "claude-sonnet-5", 502),
        ("nonzero", "claude-sonnet-5", 19),
        ("duplicate", "claude-sonnet-5", 502),
        ("bad-default", "claude-sonnet-5", 502),
        ("valid", "unlisted", 502),
        ("valid", "auto", 502),
    ],
)
def test_invoke_kiro_fails_closed_on_unobservable_model_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory_mode: str,
    model: str,
    expected_code: int,
) -> None:
    exit_code, error, response = invoke_fake_kiro(
        tmp_path, monkeypatch, model=model, inventory_mode=inventory_mode
    )

    assert exit_code == expected_code
    assert error
    assert response.response == ""
    observations = (tmp_path / "kiro-observations.jsonl").read_text().splitlines()
    assert len(observations) == 1


@pytest.mark.parametrize("model", ["malformed-session", "absent-session"])
def test_invoke_kiro_fails_closed_without_a_coherent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    exit_code, error, response = invoke_fake_kiro(tmp_path, monkeypatch, model=model)

    assert exit_code == 502
    assert "session" in error.lower()
    assert response.response == ""
    assert (tmp_path / "response.log").is_file()


@pytest.mark.parametrize(
    ("model", "timeout_seconds", "expected_code"),
    [("nonzero-session", 7, 23), ("timeout-session", 1, 124)],
)
def test_invoke_kiro_fails_closed_on_session_inventory_process_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    timeout_seconds: int,
    expected_code: int,
) -> None:
    exit_code, error, response = invoke_fake_kiro(
        tmp_path, monkeypatch, model=model, timeout_seconds=timeout_seconds
    )

    assert exit_code == expected_code
    assert error
    assert response.response == ""


@pytest.mark.parametrize(
    ("model", "timeout_seconds", "expected_code"),
    [("nonzero", 7, 17), ("timeout", 1, 124)],
)
def test_invoke_kiro_preserves_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    timeout_seconds: int,
    expected_code: int,
) -> None:
    exit_code, error, response = invoke_fake_kiro(
        tmp_path, monkeypatch, model=model, timeout_seconds=timeout_seconds
    )

    assert exit_code == expected_code
    assert error
    assert response.response == ""


def test_invoke_kiro_reports_a_missing_binary(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    exit_code, error, response = cli.invoke_kiro(
        kiro_bin=str(tmp_path / "missing-kiro"),
        prompt="Review synthetic source.",
        model="claude-sonnet-5",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=1,
        **kiro_paths(tmp_path),
    )

    assert exit_code == 127
    assert "not found" in error.lower()
    assert response.response == ""


def test_invoke_kiro_maps_operating_system_execution_errors_to_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n")

    def fail_to_execute(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "Popen", fail_to_execute)
    exit_code, error, response = cli.invoke_kiro(
        kiro_bin="kiro-cli",
        prompt="Review synthetic source.",
        model="claude-sonnet-5",
        files=[source],
        max_output_tokens=1,
        response_contract="verdict",
        timeout_seconds=1,
        **kiro_paths(tmp_path),
    )

    assert exit_code == 127
    assert "denied" in error
    assert response.response == ""


def test_invoke_kiro_preserves_empty_output_with_a_valid_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, error, response = invoke_fake_kiro(tmp_path, monkeypatch, model="empty")

    assert exit_code == 0
    assert error == ""
    assert response.conversation_id == "123e4567-e89b-12d3-a456-426614174000"
    assert response.response == ""
    assert (tmp_path / "response.log").read_bytes() == b""
    assert (tmp_path / "stderr.log").read_bytes() == b""


def test_usage_private_gemini_product_review(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Design the bounded product.")
    policy = tmp_path / "policy.toml"
    policy.write_text('[models."gemini-3.6-flash-medium"]\nsource_allowed = true\n')
    payload = product_review_payload()

    result = run_cli(
        "run",
        "--review-id",
        "agy.proprietary",
        "--prompt-file",
        str(prompt),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--model",
        "gemini-3.6-flash-medium",
        "--file",
        str(source),
        "--transport",
        "agy",
        "--source-class",
        "proprietary",
        "--response-contract",
        "product-review-json",
        "--policy",
        str(policy),
        env={"AGY_BIN": str(fake_agy), "AGY_RESPONSE": json.dumps(payload)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    assert receipt["transport"] == "agy"
    assert receipt["policy"]["sha256"] == cli.sha256_bytes(policy.read_bytes())
    request = json.loads(Path(receipt["attempts"][0]["evidence"]["request"]).read_text())
    assert request["responseContract"] == "product-review-json"


def test_run_uses_the_agy_transport_and_records_evidence(tmp_path: Path) -> None:
    fake_agy = write_fake_agy(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review the bounded synthetic source.")

    result = run_cli(
        "run",
        "--review-id",
        "agy.packet",
        "--prompt-file",
        str(prompt),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--model",
        "gemini-3.6-flash-medium",
        "--file",
        str(source),
        "--transport",
        "agy",
        "--response-contract",
        "findings-json",
        env={"AGY_BIN": str(fake_agy)},
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads((Path(result.stdout.strip()) / "receipt.json").read_text())
    attempt = receipt["attempts"][0]
    assert receipt["transport"] == "agy"
    assert receipt["response"]["conversationId"] == "agy-conversation"
    assert attempt["evidence"]["request"].endswith("request.json")
    assert attempt["evidence"]["response"].endswith("response.json")


def test_usage_private_openrouter_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text('[models."test-model"]\nsource_allowed = true\n')
    source = tmp_path / "source.py"
    source.write_text("pass\n")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review the bounded source.")

    def fake_openrouter(**kwargs: object) -> tuple[int, str, cli.PersistedResponse]:
        request_path = kwargs["request_path"]
        response_path = kwargs["response_path"]
        assert isinstance(request_path, Path)
        assert isinstance(response_path, Path)
        request_path.write_text('{"model":"test-model"}')
        response_path.write_text('{"id":"turn-test"}')
        return (
            0,
            "",
            cli.PersistedResponse(
                conversation_id="turn-test",
                cost_usd=0.001,
                duration_ms=1,
                input_tokens=2,
                model="test-model",
                output_tokens=3,
                provider="Test Provider",
                response='{"verdict":"approved","findings":[]}',
            ),
        )

    monkeypatch.setattr(cli, "invoke_openrouter", fake_openrouter)
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--review-id",
            "openrouter.proprietary",
            "--prompt-file",
            str(prompt),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--model",
            "test-model",
            "--file",
            str(source),
            "--transport",
            "openrouter",
            "--source-class",
            "proprietary",
            "--response-contract",
            "findings-json",
            "--policy",
            str(policy),
        ]
    )

    assert args.handler(args) == 0
    receipt = json.loads((Path(capsys.readouterr().out.strip()) / "receipt.json").read_text())
    assert receipt["transport"] == "openrouter"
    assert receipt["policy"]["sha256"] == cli.sha256_bytes(policy.read_bytes())


def test_run_uses_the_openrouter_transport_and_records_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = tmp_path / "prompt.md"
    source = tmp_path / "source.py"
    prompt.write_text("Review this synthetic packet.")
    source.write_text("def source() -> None: pass\n")

    def fake_openrouter(**kwargs: object) -> tuple[int, str, cli.PersistedResponse]:
        request_path = kwargs["request_path"]
        response_path = kwargs["response_path"]
        assert isinstance(request_path, Path)
        assert isinstance(response_path, Path)
        assert kwargs["model"] == "deepseek/deepseek-v4-flash-0731"
        assert kwargs["provider_preferences"] is None
        request_path.write_text('{"model":"test-model"}')
        response_path.write_text('{"id":"turn-test"}')
        return (
            0,
            "",
            cli.PersistedResponse(
                conversation_id="turn-test",
                cost_usd=0.001,
                duration_ms=1,
                input_tokens=2,
                model="deepseek/deepseek-v4-flash-0731",
                output_tokens=3,
                provider="Test Provider",
                response='{"verdict":"approved","findings":[]}',
            ),
        )

    monkeypatch.setattr(cli, "invoke_openrouter", fake_openrouter)
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--review-id",
            "openrouter.packet",
            "--prompt-file",
            str(prompt),
            "--model",
            "openrouter/deepseek/deepseek-v4-flash-0731",
            "--file",
            str(source),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--transport",
            "openrouter",
            "--response-contract",
            "findings-json",
        ]
    )

    assert args.handler(args) == 0
    receipt_path = Path(capsys.readouterr().out.strip()) / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["transport"] == "openrouter"
    assert receipt["attempts"][0]["evidence"]["request"].endswith("request.json")
    assert receipt["attempts"][0]["result"] == "accepted"
    assert receipt["attempts"][0]["provider"]["resolved"] == "Test Provider"


def test_run_rejects_a_response_from_an_unpinned_openrouter_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = tmp_path / "prompt.md"
    source = tmp_path / "source.py"
    prompt.write_text("Review this synthetic packet.")
    source.write_text("def source() -> None: pass\n")

    def fake_openrouter(**kwargs: object) -> tuple[int, str, cli.PersistedResponse]:
        request_path = kwargs["request_path"]
        response_path = kwargs["response_path"]
        assert isinstance(request_path, Path)
        assert isinstance(response_path, Path)
        request_path.write_text('{"model":"test-model"}')
        response_path.write_text('{"id":"turn-test"}')
        return (
            0,
            "",
            cli.PersistedResponse(
                conversation_id="turn-test",
                cost_usd=0.001,
                duration_ms=1,
                input_tokens=2,
                model="deepseek/test",
                output_tokens=3,
                provider="Cloudflare",
                response='{"verdict":"approved","findings":[]}',
            ),
        )

    monkeypatch.setattr(cli, "invoke_openrouter", fake_openrouter)
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--review-id",
            "pinned-provider",
            "--prompt-file",
            str(prompt),
            "--model",
            "openrouter/deepseek/test",
            "--file",
            str(source),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--transport",
            "openrouter",
            "--response-contract",
            "findings-json",
            "--provider-only",
            "deepinfra",
            "--no-provider-fallbacks",
        ]
    )

    assert args.handler(args) == 1
    receipt = json.loads((Path(capsys.readouterr().out.strip()) / "receipt.json").read_text())
    assert receipt["attempts"][0]["result"] == "provider-mismatch"
    assert receipt["attempts"][0]["provider"] == {
        "requested": ["deepinfra"],
        "resolved": "Cloudflare",
    }
