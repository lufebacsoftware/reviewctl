"""Codex transport for the project-scoped review API.

Project reviews materialize their source snapshots below the project state
directory.  Codex's source-root sandbox denies reads from that project, so
this adapter copies the already validated snapshots to a private temporary
workspace before invoking the canonical Codex backend.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

from reviewctl.backends import (
    BackendCapabilities,
    BackendExecution,
    BackendRequest,
    ReadOnlyCapability,
    SourceIsolation,
)
from reviewctl.filesystem import read_confined_bytes


def _write_private(path: Path, contents: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


class CodexProjectTransport:
    """Run the canonical Codex backend without exposing the project checkout."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(os.path.abspath(project_dir.expanduser())).resolve()

    @classmethod
    def capabilities(cls) -> BackendCapabilities:
        return BackendCapabilities(
            review_read_only=ReadOnlyCapability.SANDBOXED,
            editable_execution=False,
            structured_output=True,
            resolved_model_identity=True,
            resolved_provider_identity=False,
            conversation_identity=True,
            usage_reporting=False,
            timeout_control=True,
            tool_control=True,
            source_isolation=SourceIsolation.EXTERNAL_SANDBOX,
        )

    def _stage_sources(self, request: BackendRequest, root: Path) -> tuple[Path, ...]:
        try:
            root.relative_to(self.project_dir)
        except ValueError:
            pass
        else:
            raise OSError("Codex staging workspace must be outside the reviewed project")

        staged: list[Path] = []
        names: set[str] = set()
        allowed_roots = tuple(Path(path).resolve() for path in request.source_roots)
        for source in request.files:
            resolved_source = source.resolve()
            if not any(
                resolved_source.is_relative_to(allowed_root) for allowed_root in allowed_roots
            ):
                raise ValueError("Codex project sources must stay below an allowed root")
            name = source.name
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("Codex project sources must have safe basenames")
            if name in names:
                raise ValueError("Codex project sources must have unique basenames")
            names.add(name)
            target = root / name
            _write_private(target, read_confined_bytes(source))
            staged.append(target)
        return tuple(staged)

    @staticmethod
    def _normalize_response(response: str, files: tuple[Path, ...]) -> str:
        """Map logical paths returned by Codex to project artifact basenames."""

        def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            value = json.loads(response, object_pairs_hook=exact_object)
        except TypeError, ValueError:
            return response
        if not isinstance(value, dict):
            return response
        encoded_by_logical = {unquote(path.name): path.name for path in files}
        encoded_names = {path.name for path in files}
        changed = False

        def normalize_path(path: object) -> object:
            nonlocal changed
            if not isinstance(path, str):
                return path
            candidate = Path(path)
            if candidate.is_absolute():
                if candidate not in files:
                    return path
                logical = unquote(candidate.name)
            elif path in encoded_names:
                return path
            else:
                logical = path
            encoded = encoded_by_logical.get(logical)
            if encoded is not None and encoded != path:
                changed = True
                return encoded
            return path

        findings = value.get("findings")
        if isinstance(findings, list):
            for finding in findings:
                if isinstance(finding, dict) and "path" in finding:
                    finding["path"] = normalize_path(finding["path"])
        reviewed_files = value.get("reviewedFiles")
        if isinstance(reviewed_files, list):
            value["reviewedFiles"] = [normalize_path(path) for path in reviewed_files]
        if not changed:
            return response
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def execute(self, request: BackendRequest) -> BackendExecution:
        # Import lazily: reviewctl.cli imports project_cli, which imports the
        # public API that constructs this transport.
        from dataclasses import replace

        from reviewctl.cli import execute_codex_backend

        # The findings contract accepts absolute reviewedFiles paths only for
        # immutable snapshots with this reserved prefix.
        with tempfile.TemporaryDirectory(prefix="reviewctl-input-") as directory:
            root = Path(directory).resolve()
            staged = self._stage_sources(request, root)
            backend_request = replace(
                request,
                files=staged,
                # The canonical backend reserves response.md itself. Keep
                # that transient evidence outside the project because the
                # project API persists its normalized response afterward.
                attempt_dir=root / "attempt",
            )
            backend_request.attempt_dir.mkdir(mode=0o700)
            execution = execute_codex_backend(backend_request)
            if execution.response is None:
                return execution
            response = self._normalize_response(execution.response.response, staged)
            if response == execution.response.response:
                return execution
            return replace(
                execution,
                response=replace(execution.response, response=response),
            )
