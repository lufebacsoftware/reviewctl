from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import reviewctl.codex_project_transport as transport_module
from reviewctl.backends import BackendEvidence, BackendExecution, BackendRequest, PersistedResponse
from reviewctl.codex_project_transport import CodexProjectTransport


def request(tmp_path: Path, source: Path) -> BackendRequest:
    return BackendRequest(
        prompt="Review the supplied source.",
        model="gpt-5.6-luna",
        response_contract="findings-json",
        files=(source,),
        attempt_dir=tmp_path / "attempt",
        timeout_seconds=30,
        max_output_tokens=8000,
        source_class="private",
        source_roots=(tmp_path,),
        provider_preferences=None,
    )


def test_codex_project_transport_stages_sources_outside_project_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / ".reviewctl" / "reviews" / "review" / "attempt-01" / "source" / "src.py"
    source.parent.mkdir(parents=True)
    source.write_text("secret = 42\n")
    observed: dict[str, object] = {}

    def fake_execute(request: BackendRequest) -> BackendExecution:
        observed["files"] = request.files
        observed["source_roots"] = request.source_roots
        assert request.files[0].read_text() == "secret = 42\n"
        assert request.files[0].parent != tmp_path
        return BackendExecution(
            0,
            "",
            PersistedResponse(
                "codex-session",
                0.0,
                1,
                1,
                request.model,
                1,
                "openai-codex",
                '{"verdict":"approved","findings":[],"reviewedFiles":["src.py"]}',
            ),
            BackendEvidence(),
        )

    monkeypatch.setattr("reviewctl.cli.execute_codex_backend", fake_execute)
    execution = CodexProjectTransport(tmp_path).execute(request(tmp_path, source))

    assert execution.exit_code == 0
    assert observed["source_roots"] == (tmp_path,)
    staged = observed["files"]
    assert isinstance(staged, tuple)
    assert not staged[0].exists()


def test_codex_response_paths_are_mapped_back_to_encoded_project_names() -> None:
    response = json.dumps(
        {
            "verdict": "changes-requested",
            "findings": [
                {
                    "severity": "low",
                    "path": "src/reviewctl/cli.py",
                    "line": 12,
                    "title": "Finding",
                    "evidence": "evidence",
                    "reproduction": "reproduce",
                }
            ],
            "reviewedFiles": [
                "/tmp/reviewctl-input-abc/src%2Freviewctl%2Fcli.py",
            ],
        }
    )

    normalized = json.loads(
        CodexProjectTransport._normalize_response(
            response, (Path("/tmp/reviewctl-input-abc/src%2Freviewctl%2Fcli.py"),)
        )
    )

    assert normalized["findings"][0]["path"] == "src%2Freviewctl%2Fcli.py"
    assert normalized["reviewedFiles"] == ["src%2Freviewctl%2Fcli.py"]


def test_codex_response_does_not_accept_an_unrelated_absolute_snapshot_path() -> None:
    staged = Path("/tmp/reviewctl-input-abc/src%2Fapp.py")
    response = '{"verdict":"approved","findings":[],"reviewedFiles":["/other/src%2Fapp.py"]}'

    assert CodexProjectTransport._normalize_response(response, (staged,)) == response


def test_codex_response_preserves_exact_encoded_names_before_decoding() -> None:
    files = (
        Path("/tmp/reviewctl-input-abc/a%2Fb.py"),
        Path("/tmp/reviewctl-input-abc/a%252Fb.py"),
    )
    response = '{"verdict":"approved","findings":[],"reviewedFiles":["a%2Fb.py"]}'

    normalized = CodexProjectTransport._normalize_response(response, files)

    assert json.loads(normalized)["reviewedFiles"] == ["a%2Fb.py"]


def test_codex_capabilities_use_external_sandbox() -> None:
    capabilities = CodexProjectTransport.capabilities()

    assert capabilities.review_read_only.value == "sandboxed"
    assert capabilities.source_isolation.value == "external-sandbox"
    assert capabilities.structured_output is True


def test_private_snapshot_is_removed_when_writing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "snapshot"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(transport_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync failed"):
        transport_module._write_private(target, b"source")

    assert not target.exists()


def test_private_snapshot_ignores_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "snapshot"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    def fail_unlink(_path: Path) -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(transport_module.os, "fsync", fail_fsync)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError, match="fsync failed"):
        transport_module._write_private(target, b"source")

    assert target.exists()
    transport_module.os.unlink(target)


def test_codex_staging_rejects_project_workspace_and_outside_sources(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("value = 1\n")
    transport = CodexProjectTransport(tmp_path)

    with pytest.raises(OSError, match="outside the reviewed project"):
        transport._stage_sources(request(tmp_path, source), tmp_path)

    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside = True\n")
    stage = tmp_path.parent / "codex-stage"
    stage.mkdir()
    with pytest.raises(ValueError, match="stay below an allowed root"):
        transport._stage_sources(request(tmp_path, outside), stage)


def test_codex_staging_rejects_unsafe_and_duplicate_source_names(tmp_path: Path) -> None:
    class UnsafeSource:
        name = "."

        def resolve(self) -> Path:
            return tmp_path

    unsafe_request = replace(request(tmp_path, tmp_path / "src.py"), files=(UnsafeSource(),))
    transport = CodexProjectTransport(tmp_path)
    stage = tmp_path.parent / "codex-stage-unsafe"
    stage.mkdir()
    with pytest.raises(ValueError, match="safe basenames"):
        transport._stage_sources(unsafe_request, stage)

    first = tmp_path / "one" / "same.py"
    second = tmp_path / "two" / "same.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("one\n")
    second.write_text("two\n")
    duplicate_request = replace(request(tmp_path, first), files=(first, second))
    with pytest.raises(ValueError, match="unique basenames"):
        transport._stage_sources(duplicate_request, stage)


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        "[]",
        '{"findings": [{}, null]}',
        '{"verdict":"approved","reviewedFiles":[1]}',
        '{"verdict":"approved","findings":[],"reviewedFiles":[]}',
        '{"verdict":"approved","findings":[],"reviewedFiles":["missing.py"]}',
        '{"findings":[],"findings":[]}',
    ],
)
def test_codex_response_normalization_preserves_invalid_or_unmapped_shapes(response: str) -> None:
    assert CodexProjectTransport._normalize_response(response, ()) == response


def test_codex_project_transport_handles_empty_backend_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src.py"
    source.write_text("value = 1\n")

    def empty_execute(_request: BackendRequest) -> BackendExecution:
        return BackendExecution(1, "unavailable", None, BackendEvidence())

    monkeypatch.setattr("reviewctl.cli.execute_codex_backend", empty_execute)
    execution = CodexProjectTransport(tmp_path).execute(request(tmp_path, source))

    assert execution.response is None
    assert execution.exit_code == 1


def test_codex_project_transport_normalizes_logical_paths_after_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src%2Ffile.py"
    source.write_text("value = 1\n")

    def logical_execute(request: BackendRequest) -> BackendExecution:
        return BackendExecution(
            0,
            "",
            PersistedResponse(
                "codex-session",
                0.0,
                1,
                1,
                request.model,
                1,
                "openai-codex",
                '{"verdict":"approved","findings":[],"reviewedFiles":["src/file.py"]}',
            ),
            BackendEvidence(),
        )

    monkeypatch.setattr("reviewctl.cli.execute_codex_backend", logical_execute)
    execution = CodexProjectTransport(tmp_path).execute(request(tmp_path, source))

    assert execution.response is not None
    assert json.loads(execution.response.response)["reviewedFiles"] == ["src%2Ffile.py"]
