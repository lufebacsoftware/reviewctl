from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewctl.errors import exit_code_for
from reviewctl.github import (
    CommandResult,
    GitHubSourceError,
    LocalGitHubSource,
    PullRequestRef,
)

HEAD = "b" * 40
BASE = "a" * 40


class FakeRunner:
    def __init__(self, *, head: str = HEAD, metadata: dict | None = None) -> None:
        self.head = head
        self.metadata = metadata or {
            "base": {"sha": BASE},
            "head": {"sha": HEAD},
            "repository": {"visibility": "private"},
        }
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, *, cwd: Path, timeout_seconds: int) -> CommandResult:
        del cwd, timeout_seconds
        self.calls.append(tuple(command))
        if command[:2] == ["gh", "api"] and command[2].endswith("/pulls/7"):
            return CommandResult(0, json.dumps(self.metadata).encode(), b"")
        if command[:2] == ["gh", "api"] and command[2].endswith("/pulls/7.diff"):
            return CommandResult(
                0,
                b"diff --git a/src/app.py b/src/app.py\n"
                b"--- a/src/app.py\n"
                b"+++ b/src/app.py\n"
                b"@@ -1,1 +1,1 @@\n"
                b"-value = 1\n"
                b"+value = 2\n",
                b"",
            )
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return CommandResult(0, f"{self.head}\n".encode(), b"")
        if command[:2] == ["git", "show"]:
            return CommandResult(0, b"value = 2\n", b"")
        raise AssertionError(f"unexpected command: {command}")


def test_local_source_freezes_metadata_diff_and_exact_commit_content(tmp_path: Path) -> None:
    runner = FakeRunner()
    snapshot = LocalGitHubSource(tmp_path, runner=runner).resolve(
        PullRequestRef("example/project", 7)
    )

    assert snapshot.ref.repository == "example/project"
    assert snapshot.ref.number == 7
    assert snapshot.base_sha == BASE
    assert snapshot.head_sha == HEAD
    assert snapshot.visibility == "private"
    assert [item.path for item in snapshot.changed_files] == ["src/app.py"]
    assert snapshot.changed_files[0].content == "value = 2\n"
    assert snapshot.changed_files[0].sha256
    assert snapshot.diff_sha256
    assert snapshot.snapshot_sha256
    assert any(command[:2] == ("gh", "api") for command in runner.calls)
    assert ("git", "show", f"{HEAD}:src/app.py") in runner.calls


def test_source_refuses_stale_checkout(tmp_path: Path) -> None:
    runner = FakeRunner(head="c" * 40)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_checkout_stale"


def test_source_refuses_unknown_visibility_before_reading_source(tmp_path: Path) -> None:
    runner = FakeRunner(metadata={"base": {"sha": BASE}, "head": {"sha": HEAD}})

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=runner).resolve(PullRequestRef("example/project", 7))

    assert error.value.diagnostic.code == "github_visibility_unknown"
    assert not any(command[:3] == ("git", "rev-parse", "HEAD") for command in runner.calls)


def test_source_refuses_invalid_path_and_non_utf8_content(tmp_path: Path) -> None:
    class InvalidPathRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if command[:2] == ["gh", "api"] and command[2].endswith("/pulls/7.diff"):
                return CommandResult(
                    0,
                    b"--- a/../secret\n+++ b/../secret\n@@ -0,0 +1,1 @@\n+x\n",
                    b"",
                )
            return result

    with pytest.raises(GitHubSourceError) as path_error:
        LocalGitHubSource(tmp_path, runner=InvalidPathRunner()).resolve(
            PullRequestRef("example/project", 7)
        )
    assert path_error.value.diagnostic.code == "github_path_invalid"

    class BinaryRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if command[:2] == ["git", "show"]:
                return CommandResult(0, b"\xff\xfe", b"")
            return result

    with pytest.raises(GitHubSourceError) as binary_error:
        LocalGitHubSource(tmp_path, runner=BinaryRunner()).resolve(
            PullRequestRef("example/project", 7)
        )
    assert binary_error.value.diagnostic.code == "github_source_not_utf8"


def test_source_accepts_visibility_from_github_base_repository_shape(tmp_path: Path) -> None:
    metadata = {
        "base": {"sha": BASE, "repo": {"private": True}},
        "head": {"sha": HEAD},
    }
    runner = FakeRunner(metadata=metadata)

    snapshot = LocalGitHubSource(tmp_path, runner=runner).resolve(
        PullRequestRef("example/project", 7)
    )

    assert snapshot.visibility == "private"


def test_source_rejects_control_characters_in_paths(tmp_path: Path) -> None:
    class ControlPathRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            result = super().__call__(command, cwd=cwd, timeout_seconds=timeout_seconds)
            if command[:2] == ["gh", "api"] and command[2].endswith("/pulls/7.diff"):
                return CommandResult(
                    0,
                    b"--- a/src/bad\x01.py\n+++ b/src/bad\x01.py\n"
                    b"@@ -0,0 +1,1 @@\n+bad\n",
                    b"",
                )
            return result

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=ControlPathRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_path_invalid"


def test_source_failure_does_not_expose_command_stderr(tmp_path: Path) -> None:
    class FailedRunner(FakeRunner):
        def __call__(self, command, *, cwd, timeout_seconds):
            del cwd, timeout_seconds
            if command[:2] == ["gh", "api"]:
                return CommandResult(1, b"", b"Authorization: super-secret-token")
            return super().__call__(command, cwd=tmp_path, timeout_seconds=30)

    with pytest.raises(GitHubSourceError) as error:
        LocalGitHubSource(tmp_path, runner=FailedRunner()).resolve(
            PullRequestRef("example/project", 7)
        )

    assert error.value.diagnostic.code == "github_command_failed"
    assert "super-secret-token" not in error.value.diagnostic.message


def test_github_diagnostics_have_documented_exit_classes() -> None:
    assert exit_code_for("github_checkout_stale") == 2
    assert exit_code_for("github_command_failed") == 3
    assert exit_code_for("github_visibility_unknown") == 4
