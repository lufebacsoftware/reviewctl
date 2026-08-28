"""Read-only discovery of locally installed review backends."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from reviewctl.backends import BackendDescriptor, BackendRegistry, DiscoveryKind

try:
    import resource
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    resource = None

_MAX_OUTPUT_LENGTH = 500
_MAX_CAPTURE_BYTES = 4096
_WINDOWS_EXECUTABLE_EXTENSIONS = (".COM", ".EXE", ".BAT", ".CMD")
_CREDENTIAL_SUFFIX = (
    r"(?:access[-_]token|auth[-_]token|refresh[-_]token|id[-_]token|"
    r"secret[-_]access[-_]key|access[-_]key[-_]id|client[-_]secret|"
    r"api[-_]key|private[-_]key|password|passwd|authorization|credentials?|"
    r"tokens?|secrets?)"
)
_SENSITIVE_LABEL = rf"(?:(?:[a-z0-9]+[-_])+)?{_CREDENTIAL_SUFFIX}"
_DELIMITED_SENSITIVE_LABEL = rf"(?<![a-z0-9_-]){_SENSITIVE_LABEL}(?![a-z0-9_-])"
_URI_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@")
_BEARER_CREDENTIAL = re.compile(r"(?i)(\bbearer)(\s+)[^\s,;\"']+")
_RECOGNIZABLE_SK_KEY = re.compile(r"(?<![a-zA-Z0-9_-])(sk-)[a-zA-Z0-9_-]{16,}(?![a-zA-Z0-9_-])")
_QUOTED_SENSITIVE_VALUE = re.compile(rf'(?i)("\b{_SENSITIVE_LABEL}\b"\s*:\s*)"(?:\\.|[^"\\])*"')
_SENSITIVE_VALUE = re.compile(rf"(?im)({_DELIMITED_SENSITIVE_LABEL})(\s*[:=]\s*)[^\r\n]*")

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
    redacted = _URI_USERINFO.sub(r"\1[REDACTED]@", value)
    redacted = _QUOTED_SENSITIVE_VALUE.sub(r'\1"[REDACTED]"', redacted)
    redacted = _BEARER_CREDENTIAL.sub(r"\1\2[REDACTED]", redacted)
    redacted = _SENSITIVE_VALUE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _RECOGNIZABLE_SK_KEY.sub(r"\1[REDACTED]", redacted)
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
    child_environment = {key: environ[key] for key in ("PATH", "SYSTEMROOT") if key in environ}
    if resource is None:
        return None, "bounded version probe unsupported on this platform"

    def limit_output_files() -> None:
        resource.setrlimit(  # pragma: no cover - runs only in the pre-exec child
            resource.RLIMIT_FSIZE,
            (_MAX_CAPTURE_BYTES + 1, _MAX_CAPTURE_BYTES + 1),
        )

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                completed = subprocess.run(
                    [executable, "--version"],
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=5,
                    check=False,
                    env=child_environment,
                    preexec_fn=limit_output_files,
                )
                completed_stdout = completed.stdout
                completed_stderr = completed.stderr
                timed_out = False
            except subprocess.TimeoutExpired as error:
                completed_stdout = error.stdout
                completed_stderr = error.stderr
                timed_out = True

            def bounded_output(value: object, stream) -> tuple[bytes | str, bool]:
                if not isinstance(value, bytes | str):
                    stream.seek(0)
                    value = stream.read(_MAX_CAPTURE_BYTES + 1)
                return value[:_MAX_CAPTURE_BYTES], len(value) > _MAX_CAPTURE_BYTES

            stdout, stdout_truncated = bounded_output(completed_stdout, stdout_file)
            stderr, stderr_truncated = bounded_output(completed_stderr, stderr_file)
        if stdout_truncated or stderr_truncated:
            return None, "version probe output exceeded bounded capture"
        output = _combined_output(stdout, stderr)
        if timed_out:
            diagnostic = "version probe timed out after 5 seconds"
            if output:
                diagnostic = f"{diagnostic}: {output}"
            return None, _bounded_output(diagnostic)
    except subprocess.SubprocessError as error:
        return None, _bounded_output(f"bounded version probe failed: {error}")
    except OSError as error:
        return None, _bounded_output(f"version probe failed: {error}")

    if completed.returncode == 0:
        if output:
            return _bounded_output(output), None
        return None, "version probe produced no output"
    diagnostic = f"version probe exited with status {completed.returncode}"
    if output:
        diagnostic = f"{diagnostic}: {output}"
    return None, _bounded_output(diagnostic)


def _is_executable(path: str) -> bool:
    return os.access(path, os.X_OK)


def _which(
    executable: str,
    path: str | None,
    *,
    windows: bool | None = None,
    is_file: Callable[[str], bool] = os.path.isfile,
    is_executable: Callable[[str], bool] = _is_executable,
) -> str | None:
    """Resolve only direct paths or directories explicitly listed in ``path``.

    Empty path entries are ignored. Relative entries intentionally resolve from the
    current directory because the caller explicitly supplied those entries.
    """
    if windows is None:
        windows = os.name == "nt"
    path_module = ntpath if windows else posixpath

    def executable_candidates(candidate: str) -> tuple[str, ...]:
        if not windows:
            return (candidate,)
        extension = path_module.splitext(candidate)[1]
        if extension:
            return (candidate,) if extension.upper() in _WINDOWS_EXECUTABLE_EXTENSIONS else ()
        return tuple(candidate + extension for extension in _WINDOWS_EXECUTABLE_EXTENSIONS)

    directories: tuple[str | None, ...]
    if path_module.dirname(executable):
        directories = (None,)
    elif path:
        separator = ";" if windows else ":"
        directories = tuple(directory for directory in path.split(separator) if directory)
    else:
        directories = ()

    for directory in directories:
        base_candidate = (
            executable if directory is None else path_module.join(directory, executable)
        )
        for candidate in executable_candidates(base_candidate):
            if is_file(candidate) and (windows or is_executable(candidate)):
                return candidate
    return None


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
    resolved = which(requested, environ.get("PATH", ""))
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
