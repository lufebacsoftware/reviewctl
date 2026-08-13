"""Read-only discovery of locally installed review backends."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from reviewctl.backends import BackendDescriptor, BackendRegistry, DiscoveryKind

_MAX_OUTPUT_LENGTH = 500
_SENSITIVE_VALUE = re.compile(
    r"(?im)(\b(?:authorization|credentials?|tokens?|api[-_]?keys?)\b)(\s*[:=]\s*)[^\r\n]*"
)
_SENSITIVE_ENV_VALUE = re.compile(
    r"(?m)(\b[A-Z][A-Z0-9_]*(?:AUTHORIZATION|CREDENTIAL|TOKEN|API_KEY)[A-Z0-9_]*)(\s*=\s*)[^\r\n]*"
)


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
    redacted = _SENSITIVE_VALUE.sub(r"\1\2[REDACTED]", value)
    redacted = _SENSITIVE_ENV_VALUE.sub(r"\1\2[REDACTED]", redacted)
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
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=dict(environ),
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
    if output:
        return None, _bounded_output(output)
    return None, _bounded_output(f"version probe exited with status {completed.returncode}")


def discover_backend(
    descriptor: BackendDescriptor,
    *,
    environ: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
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
    resolved = which(requested)
    if resolved is None:
        diagnostic = _bounded_output(f"executable not found: {requested}")
        return BackendInstallation(
            descriptor.name,
            requested,
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
        requested,
        resolved,
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
    which: Callable[[str], str | None] = shutil.which,
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
