from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
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

    def unexpected_which(executable: str, path: str | None) -> str | None:
        raise AssertionError(f"which called for {executable} in {path}")

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
    which_calls: list[tuple[str, str | None]] = []
    probe_calls: list[tuple[str, dict[str, str]]] = []

    def fake_which(executable: str, path: str | None) -> str | None:
        which_calls.append((executable, path))
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
    assert which_calls == [(expected_requested, environ.get("PATH", ""))]
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
        which=lambda executable, path: None,
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
        which=lambda executable, path: "/safe/bin/agy",
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
                "encoding": "utf-8",
                "errors": "replace",
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
        which=lambda executable, path: f"/bin/{executable}",
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
            "version probe exited with status 2: "
            "token: [REDACTED]\nAuthorization: [REDACTED]\n"
            + "x" * 420,
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


def _write_python_executable(directory: Path, name: str, source: str) -> Path:
    payload = directory / f"{name}.py"
    payload.write_text(source, encoding="utf-8")
    python_executable = str(Path(sys.executable).resolve())
    if os.name == "nt":
        launcher = directory / f"{name}.cmd"
        command = subprocess.list2cmdline([python_executable, str(payload)])
        launcher.write_text(f"@echo off\r\n{command} %*\r\n", encoding="utf-8")
        return launcher
    launcher = directory / name
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(python_executable)} {shlex.quote(str(payload))} \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _real_child_environment(path: str | None = None) -> dict[str, str]:
    environ = {"PATH": path if path is not None else os.environ.get("PATH", "")}
    if "SYSTEMROOT" in os.environ:
        environ["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environ


def test_discovery_uses_supplied_path_instead_of_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_dir = tmp_path / "ambient"
    supplied_dir = tmp_path / "supplied"
    ambient_dir.mkdir()
    supplied_dir.mkdir()
    ambient = _write_python_executable(ambient_dir, "review-probe", "print('ambient 1.0')\n")
    supplied = _write_python_executable(supplied_dir, "review-probe", "print('supplied 2.0')\n")
    monkeypatch.setenv("PATH", str(ambient_dir))

    installation = discover_backend(
        descriptor("probe", default_executable=supplied.name),
        environ=_real_child_environment(str(supplied_dir)),
    )

    assert installation.resolved_executable != str(ambient)
    assert installation.resolved_executable == str(supplied)
    assert installation.version == "supplied 2.0"


def test_discovery_without_supplied_path_does_not_search_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_dir = tmp_path / "ambient"
    ambient_dir.mkdir()
    ambient = _write_python_executable(ambient_dir, "ambient-probe", "print('ambient 1.0')\n")
    monkeypatch.setenv("PATH", str(ambient_dir))
    probe_called = False

    def fake_probe(
        executable: str, environ: dict[str, str]
    ) -> tuple[str | None, str | None]:
        nonlocal probe_called
        probe_called = True
        return executable, repr(environ)

    installation = discover_backend(
        descriptor("probe", default_executable=ambient.name),
        environ={},
        probe=fake_probe,
    )

    assert installation == BackendInstallation(
        "probe",
        ambient.name,
        None,
        None,
        "missing",
        "unqualified",
        (f"executable not found: {ambient.name}",),
        False,
    )
    assert probe_called is False


def test_probe_version_real_child_receives_only_allowed_environment(tmp_path: Path) -> None:
    executable = _write_python_executable(
        tmp_path,
        "environment-probe",
        "import os\n"
        "if 'DISCOVERY_SECRET' in os.environ:\n"
        "    print('secret leaked')\n"
        "    raise SystemExit(9)\n"
        "print('isolated 1.0')\n",
    )

    child_environment = _real_child_environment()
    child_environment["DISCOVERY_SECRET"] = "do-not-leak"
    result = probe_version(str(executable), child_environment)

    assert result == ("isolated 1.0", None)


def test_probe_version_real_child_combines_stdout_stderr_and_nonzero_status(
    tmp_path: Path,
) -> None:
    executable = _write_python_executable(
        tmp_path,
        "failing-probe",
        "import sys\n"
        "print('stdout detail')\n"
        "print('stderr detail', file=sys.stderr)\n"
        "raise SystemExit(7)\n",
    )

    assert probe_version(str(executable), {}) == (
        None,
        "version probe exited with status 7: stdout detail\nstderr detail",
    )


def test_probe_version_replaces_invalid_utf8_from_real_child(tmp_path: Path) -> None:
    executable = _write_python_executable(
        tmp_path,
        "invalid-output",
        "import os\nos.write(1, b'\\xffversion 1.0\\n')\n",
    )

    version, diagnostic = probe_version(str(executable), {})

    assert version == "�version 1.0"
    assert diagnostic is None


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, (None, "version probe produced no output")),
        (4, (None, "version probe exited with status 4")),
    ],
)
def test_probe_version_reports_empty_output_consistently(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: tuple[str | None, str | None],
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, "", ""),
    )

    assert probe_version("tool", {}) == expected


def test_timeout_output_is_sanitized_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "timeout-secret-value"
    error = subprocess.TimeoutExpired(
        ["tool", "--version"],
        5,
        output=(f"CLIENT_SECRET={secret}\n" + "x" * 600).encode(),
        stderr=b"PASSWORD=stderr-secret\n",
    )

    def raise_timeout(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    version, diagnostic = probe_version("tool", {"PRIVATE_KEY": "child-secret"})

    assert version is None
    assert diagnostic is not None
    assert diagnostic.startswith(
        "version probe timed out after 5 seconds: CLIENT_SECRET=[REDACTED]"
    )
    assert len(diagnostic) == 500
    assert secret not in diagnostic
    assert "stderr-secret" not in diagnostic


@pytest.mark.parametrize(
    "label",
    [
        "CLIENT_SECRET",
        "password",
        "PASSWD",
        "private_key",
        "TOKEN",
        "api-key",
        "Authorization",
    ],
)
def test_discovery_sanitizes_secret_shaped_requested_executable_and_diagnostic(
    label: str,
) -> None:
    secret = "injected-secret-value"
    requested = f"/safe/tool?{label}={secret}"
    lookup_calls: list[tuple[str, str | None]] = []

    def fake_which(executable: str, path: str | None) -> str | None:
        lookup_calls.append((executable, path))
        return None

    installation = discover_backend(
        descriptor("tool"),
        environ={"TOOL_BIN": requested, "PATH": "/safe/bin"},
        which=fake_which,
    )

    assert lookup_calls == [(requested, "/safe/bin")]
    assert installation.requested_executable == f"/safe/tool?{label}=[REDACTED]"
    assert installation.diagnostics == (
        f"executable not found: /safe/tool?{label}=[REDACTED]",
    )
    assert secret not in repr(asdict(installation))


def test_discovery_sanitizes_injected_probe_results_without_changing_safe_paths() -> None:
    secret = "probe-secret-value"

    installation = discover_backend(
        descriptor("tool"),
        environ={"PATH": "/safe/bin"},
        which=lambda executable, path: "/safe/bin/tool",
        probe=lambda executable, environ: (
            f"tool 1.0 CLIENT_SECRET={secret}",
            f"PRIVATE_KEY={secret}",
        ),
    )

    assert installation.requested_executable == "tool"
    assert installation.resolved_executable == "/safe/bin/tool"
    assert installation.version == "tool 1.0 CLIENT_SECRET=[REDACTED]"
    assert installation.diagnostics == ("PRIVATE_KEY=[REDACTED]",)
    assert secret not in repr(installation)


@pytest.mark.parametrize(
    "label",
    [
        "client_secret",
        "password",
        "passwd",
        "private_key",
        "api_key",
        "token",
        "authorization",
    ],
)
def test_discovery_redacts_quoted_json_and_key_value_results(label: str) -> None:
    secret = f"{label}-must-not-survive"
    structured = json.dumps({label: secret, "safe": "visible"}) + "x" * 600
    key_value = f"{label}={secret}\n" + "y" * 600

    installation = discover_backend(
        descriptor("tool"),
        environ={"PATH": "/safe/bin"},
        which=lambda executable, path: "/safe/bin/tool",
        probe=lambda executable, environ: (structured, key_value),
    )

    serialized = asdict(installation)
    assert secret not in repr(serialized)
    assert installation.version is not None
    assert '"[REDACTED]"' in installation.version
    assert installation.diagnostics
    assert "[REDACTED]" in installation.diagnostics[0]
    assert len(installation.version) <= 500
    assert all(len(diagnostic) <= 500 for diagnostic in installation.diagnostics)
