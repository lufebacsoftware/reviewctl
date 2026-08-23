from __future__ import annotations

import json
from pathlib import Path

from reviewctl.backends import BackendRequest
from reviewctl.pi_transport import PiProcessResult, PiTransport


def request(
    tmp_path: Path, *, prompt: str = "private prompt", tools: str = "none"
) -> BackendRequest:
    return BackendRequest(
        prompt=prompt,
        model="openrouter/stealth/ox-alpha",
        response_contract="findings-json",
        files=(),
        attempt_dir=tmp_path / "attempt",
        timeout_seconds=30,
        max_output_tokens=8000,
        source_class="private",
        source_roots=(tmp_path,),
        provider_preferences=None,
        tools=tools,
    )


def assistant_stream(text: str, *, output: int = 20) -> bytes:
    events = [
        {"type": "session", "id": "session-1"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "openrouter",
                "model": "stealth/ox-alpha",
                "usage": {"input": 10, "output": output, "cost": {"total": 0.01}},
                "content": [{"type": "text", "text": text}],
            },
        },
    ]
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


class FakeRunner:
    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.last_command: list[str] = []
        self.last_stdin = ""

    def __call__(self, command, *, input_text, timeout_seconds, cwd):
        self.last_command = command
        self.last_stdin = input_text
        return PiProcessResult(returncode=0, stdout=self.stdout, stderr=b"", timed_out=False)


def test_pi_request_uses_exact_model_and_no_tools_by_default(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('{"verdict":"approved","findings":[]}'))
    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert "--model" in runner.last_command
    assert "openrouter/stealth/ox-alpha" in runner.last_command
    assert "--no-tools" in runner.last_command
    assert "--thinking" in runner.last_command
    assert "minimal" in runner.last_command
    assert "private prompt" not in runner.last_command
    assert runner.last_stdin == "private prompt"
    assert execution.response is not None
    assert execution.response.model == "openrouter/stealth/ox-alpha"


def test_pi_empty_response_preserves_usage_and_diagnostic(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream("", output=8000))
    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert execution.response is not None
    assert execution.response.response == ""
    assert execution.response.output_tokens == 8000
    assert execution.diagnostic == "empty_response"
    assert execution.evidence.response is not None


def test_pi_transport_normalizes_one_json_markdown_fence(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('```json\n{"verdict":"approved","findings":[]}\n```'))

    execution = PiTransport(run_process=runner).execute(request(tmp_path))

    assert execution.response is not None
    assert execution.response.response == '{"verdict":"approved","findings":[]}'


def test_pi_read_only_profile_enables_only_read_tools(tmp_path: Path) -> None:
    runner = FakeRunner(assistant_stream('{"verdict":"approved","findings":[]}'))

    PiTransport(run_process=runner).execute(request(tmp_path, tools="read-only"))

    assert "--tools" in runner.last_command
    assert "read,grep,find,ls" in runner.last_command
    assert "--no-tools" not in runner.last_command


def test_pi_transport_does_not_claim_unenforced_token_cap() -> None:
    capabilities = PiTransport.capabilities()

    assert capabilities.output_token_limit_enforced is False


def test_pi_transport_preserves_timeout_as_transport_diagnostic(tmp_path: Path) -> None:
    class TimeoutRunner(FakeRunner):
        def __call__(self, command, *, input_text, timeout_seconds, cwd):
            self.last_command = command
            self.last_stdin = input_text
            return PiProcessResult(
                returncode=124,
                stdout=b"",
                stderr=b"timed out",
                timed_out=True,
            )

    execution = PiTransport(run_process=TimeoutRunner(b"")).execute(request(tmp_path))

    assert execution.response is None
    assert "timed out" in execution.diagnostic
