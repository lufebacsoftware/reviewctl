from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewctl.cli import run_cli
from reviewctl.config import ConfigError, load_config, parse_route


def test_project_config_wins_over_user_profile(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text(
        '[profiles.default]\n'
        'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
        'execution = "remote"\n'
    )
    project.write_text(
        '[project]\n'
        'visibility = "private"\n'
        'privacy_mode = "private"\n'
        '[profiles.default]\n'
        'routes = ["pi:openrouter/meta/muse-spark-1.2-contributor"]\n'
        'execution = "remote"\n'
    )

    config = load_config(project, user_path=user)

    assert config.profile("default").routes == (
        "pi:openrouter/meta/muse-spark-1.2-contributor",
    )


def test_user_stricter_privacy_cannot_be_weakened_by_project(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text('[project]\nprivacy_mode = "sensitive"\n')
    project.write_text('[project]\nprivacy_mode = "personal"\n')

    config = load_config(project, user_path=user)

    assert config.project.privacy_mode == "sensitive"


def test_sensitive_remote_profile_is_rejected_at_load(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        '[project]\n'
        'privacy_mode = "sensitive"\n'
        '[profiles.default]\n'
        'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
        'execution = "remote"\n'
    )

    with pytest.raises(ConfigError, match="sensitive"):
        load_config(project, user_path=None)


def test_local_profile_rejects_explicit_openrouter_route(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        '[project]\nprivacy_mode = "sensitive"\n'
        '[profiles.default]\n'
        'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
        'execution = "local"\n'
    )

    with pytest.raises(ConfigError, match="remote"):
        load_config(project, user_path=None)


def test_invalid_route_is_rejected_at_load(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text('[profiles.default]\nroutes = ["missing-colon"]\n')

    with pytest.raises(ConfigError, match="route"):
        load_config(project, user_path=None)


def test_invalid_limits_and_privacy_mode_are_rejected_at_load(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        '[project]\nprivacy_mode = "unknown"\n'
        '[profiles.default]\n'
        'timeout_seconds = 0\n'
        'max_output_tokens = -1\n'
    )

    with pytest.raises(ConfigError):
        load_config(project, user_path=None)


def test_missing_project_config_uses_safe_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml", user_path=None)

    assert config.project.privacy_mode == "private"
    assert config.profile("default").routes == ()


def test_config_digest_changes_when_project_bytes_change(tmp_path: Path) -> None:
    path = tmp_path / "reviewctl.toml"
    path.write_text('[project]\nprivacy_mode = "private"\n')
    first = load_config(path, user_path=None)
    path.write_text('[project]\nprivacy_mode = "personal"\n')
    second = load_config(path, user_path=None)

    assert first.digest != second.digest


def test_parse_route_requires_known_transport_and_model() -> None:
    assert parse_route("pi:openrouter/stealth/ox-alpha").transport == "pi"
    assert parse_route("pi:openrouter/stealth/ox-alpha").model == "openrouter/stealth/ox-alpha"
    with pytest.raises(ConfigError, match="route"):
        parse_route("missing-colon")
    with pytest.raises(ConfigError, match="route"):
        parse_route("pi:")


def test_init_writes_portable_project_id_and_local_origin(tmp_path: Path) -> None:
    assert run_cli(["init", "--project", str(tmp_path)]) == 0

    config = load_config(tmp_path / "reviewctl.toml", user_path=None)
    identity = json.loads((tmp_path / ".reviewctl/identity.json").read_text())

    assert config.project.project_id.startswith("project-")
    assert config.project.portable_project_id is True
    assert identity["projectId"] == config.project.project_id
    assert identity["originId"].startswith("origin-")
    assert (tmp_path / ".reviewctl/identity.json").stat().st_mode & 0o777 == 0o600
