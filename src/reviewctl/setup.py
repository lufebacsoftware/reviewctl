"""Read-only discovery of locally installed review backends."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from reviewctl.backends import BackendDescriptor, BackendRegistry, DiscoveryKind

_MAX_OUTPUT_LENGTH = 500
_SENSITIVE_LABEL = (
    r"(?:authorization|credentials?|tokens?|api[-_]?keys?|client[-_]?secret|"
    r"password|passwd|private[-_]?key)"
)
_QUOTED_SENSITIVE_VALUE = re.compile(
    rf'(?i)("\b{_SENSITIVE_LABEL}\b"\s*:\s*)"(?:\\.|[^"\\])*"'
)
_SENSITIVE_VALUE = re.compile(
    rf"(?im)(\b{_SENSITIVE_LABEL}\b)(\s*[:=]\s*)[^\r\n]*"
)
_SENSITIVE_ENV_VALUE = re.compile(
    r"(?im)(\b[A-Z][A-Z0-9_]*(?:AUTHORIZATION|CREDENTIALS?|TOKENS?|API_KEY|"
    r"CLIENT_SECRET|PASSWORD|PASSWD|PRIVATE_KEY)[A-Z0-9_]*)(\s*=\s*)[^\r\n]*"
)

ExecutableLookup = Callable[[str, str | None], str | None]


@dataclass(frozen=True)
class BackendInstallation:
    name: str
    requested_executable: str | None
    resolved_executable: str | None
    version: str | None
    availability: str
    qualification: str
    diagnostics: tuple[str, ...]
    probe_performed: bool


@dataclass(frozen=True)
class LocalExecutionTopology:
    schema_version: int
    local_only: bool
    model_probe_performed: bool
    backends: tuple[BackendInstallation, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _bounded_output(value: str) -> str:
    redacted = _SENSITIVE_ENV_VALUE.sub(r"\1\2[REDACTED]", value)
    redacted = _QUOTED_SENSITIVE_VALUE.sub(r'\1"[REDACTED]"', redacted)
    redacted = _SENSITIVE_VALUE.sub(r"\1\2[REDACTED]", redacted)
    return redacted[:_MAX_OUTPUT_LENGTH]


def _combined_output(stdout: object, stderr: object) -> str:
    values = []
    for output in (stdout, stderr):
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if isinstance(output, str) and output.strip():
            values.append(output.strip())
    return "\n".join(values)


def probe_version(executable: str, environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    child_environment = {
        key: environ[key] for key in ("PATH", "SYSTEMROOT") if key in environ
    }
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=child_environment,
        )
    except subprocess.TimeoutExpired as error:
        output = _combined_output(error.stdout, error.stderr)
        diagnostic = "version probe timed out after 5 seconds"
        if output:
            diagnostic = f"{diagnostic}: {output}"
        return None, _bounded_output(diagnostic)
    except OSError as error:
        return None, _bounded_output(f"version probe failed: {error}")

    output = _combined_output(completed.stdout, completed.stderr)
    if completed.returncode == 0:
        if output:
            return _bounded_output(output), None
        return None, "version probe produced no output"
    diagnostic = f"version probe exited with status {completed.returncode}"
    if output:
        diagnostic = f"{diagnostic}: {output}"
    return None, _bounded_output(diagnostic)


def _which(executable: str, path: str | None) -> str | None:
    return shutil.which(executable, path=path)


def discover_backend(
    descriptor: BackendDescriptor,
    *,
    environ: Mapping[str, str],
    which: ExecutableLookup = _which,
    probe: Callable[[str, Mapping[str, str]], tuple[str | None, str | None]] = probe_version,
) -> BackendInstallation:
    if descriptor.discovery_kind is DiscoveryKind.REMOTE_API:
        return BackendInstallation(
            descriptor.name,
            None,
            None,
            None,
            "not-applicable",
            descriptor.qualification,
            (),
            False,
        )

    requested = environ.get(descriptor.executable_env, descriptor.default_executable)
    resolved = which(requested, environ.get("PATH"))
    serialized_requested = _bounded_output(requested)
    if resolved is None:
        diagnostic = _bounded_output(f"executable not found: {requested}")
        return BackendInstallation(
            descriptor.name,
            serialized_requested,
            None,
            None,
            "missing",
            descriptor.qualification,
            (diagnostic,),
            False,
        )

    probe_environment = {key: environ[key] for key in ("PATH", "SYSTEMROOT") if key in environ}
    version, diagnostic = probe(resolved, probe_environment)
    return BackendInstallation(
        descriptor.name,
        serialized_requested,
        _bounded_output(resolved),
        _bounded_output(version) if version else None,
        "available" if version else "unverified",
        descriptor.qualification,
        (_bounded_output(diagnostic),) if diagnostic else (),
        True,
    )


def discover_topology(
    registry: BackendRegistry,
    *,
    environ: Mapping[str, str],
    which: ExecutableLookup = _which,
    probe: Callable[[str, Mapping[str, str]], tuple[str | None, str | None]] = probe_version,
) -> LocalExecutionTopology:
    backends = tuple(
        discover_backend(descriptor, environ=environ, which=which, probe=probe)
        for descriptor in registry.descriptors()
    )
    return LocalExecutionTopology(
        schema_version=1,
        local_only=True,
        model_probe_performed=False,
        backends=backends,
    )
