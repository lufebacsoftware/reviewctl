from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from reviewctl.backends import (
    BackendCapabilities,
    BackendDescriptor,
    BackendFamily,
    BackendRegistry,
    DiscoveryKind,
    ReadOnlyCapability,
    SourceIsolation,
)
from reviewctl.setup import (
    BackendInstallation,
    LocalExecutionTopology,
    discover_backend,
    discover_topology,
    probe_version,
)


def descriptor(
    name: str,
    *,
    discovery_kind: DiscoveryKind = DiscoveryKind.EXECUTABLE,
    executable_env: str | None = None,
    default_executable: str | None = None,
) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        family=BackendFamily.AGENT_CLI,
        discovery_kind=discovery_kind,
        executable_env=executable_env if executable_env is not None else f"{name.upper()}_BIN",
        default_executable=default_executable if default_executable is not None else name,
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


def unused_execute(request: object) -> None:
    del request


def test_remote_api_discovery_does_not_inspect_credentials_or_call_local_probes() -> None:
    remote = descriptor(
        "openrouter",
        discovery_kind=DiscoveryKind.REMOTE_API,
        executable_env="",
        default_executable="",
    )
    secret = "credential-value-that-must-not-appear"

    def unexpected_which(executable: str) -> str | None:
        raise AssertionError(f"which called for {executable}")

    def unexpected_probe(
        executable: str, environ: dict[str, str]
    ) -> tuple[str | None, str | None]:
        raise AssertionError(f"probe called for {executable} with {environ}")

    installation = discover_backend(
        remote,
        environ={"OPENROUTER_API_KEY": secret},
        which=unexpected_which,
        probe=unexpected_probe,
    )

    assert installation == BackendInstallation(
        name="openrouter",
        requested_executable=None,
        resolved_executable=None,
        version=None,
        availability="not-applicable",
        qualification="unqualified",
        diagnostics=(),
        probe_performed=False,
    )
    assert secret not in repr(installation)


@pytest.mark.parametrize(
    ("environ", "expected_requested"),
    [
        ({"CODEX_BIN": "codex-nightly"}, "codex-nightly"),
        ({}, "codex"),
    ],
)
def test_executable_discovery_resolves_override_or_default(
    environ: dict[str, str], expected_requested: str
) -> None:
    which_calls: list[str] = []
    probe_calls: list[tuple[str, dict[str, str]]] = []

    def fake_which(executable: str) -> str | None:
        which_calls.append(executable)
        return f"/tools/{executable}"

    def fake_probe(
        executable: str, probe_environ: dict[str, str]
    ) -> tuple[str | None, str | None]:
        probe_calls.append((executable, probe_environ))
        return "codex 1.2.3", None

    installation = discover_backend(
        descriptor("codex"),
        environ=environ,
        which=fake_which,
        probe=fake_probe,
    )

    assert installation == BackendInstallation(
        "codex",
        expected_requested,
        f"/tools/{expected_requested}",
        "codex 1.2.3",
        "available",
        "unqualified",
        (),
        True,
    )
    assert which_calls == [expected_requested]
    assert probe_calls == [(f"/tools/{expected_requested}", {})]


def test_missing_executable_is_distinct_from_qualification_and_is_not_probed() -> None:
    probe_called = False

    def fake_probe(
        executable: str, environ: dict[str, str]
    ) -> tuple[str | None, str | None]:
        nonlocal probe_called
        probe_called = True
        return executable, repr(environ)

    installation = discover_backend(
        descriptor("pi"),
        environ={},
        which=lambda executable: None,
        probe=fake_probe,
    )

    assert installation == BackendInstallation(
        "pi", "pi", None, None, "missing", "unqualified", ("executable not found: pi",), False
    )
    assert probe_called is False


def test_discovery_probe_receives_only_path_and_systemroot() -> None:
    probe_calls: list[tuple[str, dict[str, str]]] = []

    def fake_probe(
        executable: str, environ: dict[str, str]
    ) -> tuple[str | None, str | None]:
        probe_calls.append((executable, environ))
        return None, "version unavailable"

    installation = discover_backend(
        descriptor("agy"),
        environ={
            "PATH": "/safe/bin",
            "SYSTEMROOT": "C:\\Windows",
            "OPENAI_API_KEY": "must-not-reach-child",
            "AUTHORIZATION": "Bearer must-not-reach-child",
        },
        which=lambda executable: "/safe/bin/agy",
        probe=fake_probe,
    )

    assert probe_calls == [("/safe/bin/agy", {"PATH": "/safe/bin", "SYSTEMROOT": "C:\\Windows"})]
    assert installation.availability == "unverified"
    assert installation.qualification == "unqualified"
    assert installation.diagnostics == ("version unavailable",)
    assert installation.probe_performed is True


def test_probe_version_uses_exact_safe_subprocess_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout="tool 1.2.3\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_version("/safe/tool", {"PATH": "/safe/bin"}) == ("tool 1.2.3", None)
    assert calls == [
        (
            ["/safe/tool", "--version"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
                "env": {"PATH": "/safe/bin"},
            },
        )
    ]


def test_topology_is_stably_sorted_and_serializes_with_asdict() -> None:
    registry = BackendRegistry()
    registry.register(descriptor("pi"), unused_execute)
    registry.register(
        descriptor(
            "openrouter",
            discovery_kind=DiscoveryKind.REMOTE_API,
            executable_env="",
            default_executable="",
        ),
        unused_execute,
    )
    registry.register(descriptor("codex"), unused_execute)

    topology = discover_topology(
        registry,
        environ={},
        which=lambda executable: f"/bin/{executable}",
        probe=lambda executable, environ: (f"{executable} 1.0", None),
    )

    assert topology == LocalExecutionTopology(
        schema_version=1,
        local_only=True,
        model_probe_performed=False,
        backends=(
            BackendInstallation(
                "codex",
                "codex",
                "/bin/codex",
                "/bin/codex 1.0",
                "available",
                "unqualified",
                (),
                True,
            ),
            BackendInstallation(
                "openrouter", None, None, None, "not-applicable", "unqualified", (), False
            ),
            BackendInstallation(
                "pi", "pi", "/bin/pi", "/bin/pi 1.0", "available", "unqualified", (), True
            ),
        ),
    )
    assert topology.to_dict() == {
        "schema_version": 1,
        "local_only": True,
        "model_probe_performed": False,
        "backends": (
            {
                "name": "codex",
                "requested_executable": "codex",
                "resolved_executable": "/bin/codex",
                "version": "/bin/codex 1.0",
                "availability": "available",
                "qualification": "unqualified",
                "diagnostics": (),
                "probe_performed": True,
            },
            {
                "name": "openrouter",
                "requested_executable": None,
                "resolved_executable": None,
                "version": None,
                "availability": "not-applicable",
                "qualification": "unqualified",
                "diagnostics": (),
                "probe_performed": False,
            },
            {
                "name": "pi",
                "requested_executable": "pi",
                "resolved_executable": "/bin/pi",
                "version": "/bin/pi 1.0",
                "availability": "available",
                "qualification": "unqualified",
                "diagnostics": (),
                "probe_performed": True,
            },
        ),
    }
    with pytest.raises(FrozenInstanceError):
        topology.local_only = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected_version", "expected_diagnostic"),
    [
        (0, "tool 1.0\n", "credential=super-secret\n", "tool 1.0\ncredential=[REDACTED]", None),
        (
            2,
            "token: super-secret\n",
            "Authorization: Bearer also-secret\n" + "x" * 600,
            None,
            "token: [REDACTED]\nAuthorization: [REDACTED]\n" + "x" * 456,
        ),
    ],
)
def test_probe_version_redacts_and_truncates_success_and_failure_output(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    expected_version: str | None,
    expected_diagnostic: str | None,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, stdout, stderr),
    )

    version, diagnostic = probe_version("tool", {})

    assert version == expected_version
    assert diagnostic == expected_diagnostic
    assert len(version or diagnostic or "") <= 500
    assert "super-secret" not in (version or "") + (diagnostic or "")
    assert "also-secret" not in (version or "") + (diagnostic or "")


@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (
            subprocess.TimeoutExpired(["tool", "--version"], 5),
            "version probe timed out after 5 seconds",
        ),
        (OSError("credential=super-secret " + "x" * 600), "credential=[REDACTED]"),
    ],
)
def test_probe_version_bounds_timeout_and_os_error_diagnostics(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, expected_text: str
) -> None:
    def raise_error(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", raise_error)

    version, diagnostic = probe_version("tool", {})

    assert version is None
    assert diagnostic is not None
    assert expected_text in diagnostic
    assert len(diagnostic) <= 500
    assert "super-secret" not in diagnostic
