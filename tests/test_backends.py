from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from reviewctl import backends, cli
from reviewctl.backends import (
    BackendCapabilities,
    BackendDescriptor,
    BackendEvidence,
    BackendExecution,
    BackendFamily,
    BackendRegistry,
    BackendRequest,
    DiscoveryKind,
    PersistedResponse,
    ReadOnlyCapability,
    SourceIsolation,
)


def descriptor(name: str) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        family=BackendFamily.AGENT_CLI,
        discovery_kind=DiscoveryKind.EXECUTABLE,
        executable_env=f"{name.upper()}_BIN",
        default_executable=name,
        capabilities=BackendCapabilities(
            review_read_only=ReadOnlyCapability.ADVISORY,
            editable_execution=False,
            structured_output=True,
            resolved_model_identity=True,
            resolved_provider_identity=False,
            conversation_identity=True,
            usage_reporting=False,
            timeout_control=True,
            tool_control=False,
            source_isolation=SourceIsolation.UNAVAILABLE,
        ),
        qualification="unqualified",
    )


def test_registry_requires_unique_known_backends_and_sorts_descriptors(tmp_path: Path) -> None:
    response = PersistedResponse("turn", None, 1, None, "model", None, None, "ok")

    def execute(request: BackendRequest) -> BackendExecution:
        assert request.attempt_dir == tmp_path
        return BackendExecution(0, "", response, BackendEvidence())

    registry = BackendRegistry()
    registry.register(descriptor("cursor"), execute)
    registry.register(descriptor("claude"), execute)

    assert registry.require("cursor").descriptor.name == "cursor"
    assert tuple(item.name for item in registry.descriptors()) == ("claude", "cursor")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(descriptor("cursor"), execute)
    with pytest.raises(KeyError, match="unknown backend"):
        registry.require("missing")


def test_backend_request_is_immutable(tmp_path: Path) -> None:
    request = BackendRequest(
        prompt="Review",
        model="model",
        response_contract="findings-json",
        files=(tmp_path / "source.py",),
        attempt_dir=tmp_path,
        timeout_seconds=30,
        max_output_tokens=4096,
        source_class="synthetic",
        source_roots=(),
        provider_preferences=None,
    )

    with pytest.raises(FrozenInstanceError):
        request.model = "changed"  # type: ignore[misc]


def test_descriptor_serialization_uses_enum_values() -> None:
    assert descriptor("cursor").to_dict() == {
        "name": "cursor",
        "family": "agent-cli",
        "discovery_kind": "executable",
        "executable_env": "CURSOR_BIN",
        "default_executable": "cursor",
        "capabilities": {
            "review_read_only": "advisory",
            "editable_execution": False,
            "structured_output": True,
            "resolved_model_identity": True,
            "resolved_provider_identity": False,
            "conversation_identity": True,
            "usage_reporting": False,
            "timeout_control": True,
            "tool_control": False,
            "source_isolation": "unavailable",
        },
        "qualification": "unqualified",
    }


def test_cli_persisted_response_is_backend_contract() -> None:
    assert cli.PersistedResponse is backends.PersistedResponse
