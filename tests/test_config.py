from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import reviewctl.config as config_module
from reviewctl.cli import run_cli
from reviewctl.config import ConfigError, load_config, parse_route
from reviewctl.filesystem import confined_relative_regular_descriptor, read_confined_bytes


def test_project_config_wins_over_user_profile(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text(
        '[profiles.default]\nroutes = ["pi:openrouter/stealth/ox-alpha"]\nexecution = "remote"\n'
    )
    project.write_text(
        "[project]\n"
        'visibility = "private"\n'
        'privacy_mode = "private"\n'
        "[profiles.default]\n"
        'routes = ["pi:openrouter/meta/muse-spark-1.2-contributor"]\n'
        'execution = "remote"\n'
    )

    config = load_config(project, user_path=user)

    assert config.profile("default").routes == ("pi:openrouter/meta/muse-spark-1.2-contributor",)


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
        "[project]\n"
        'privacy_mode = "sensitive"\n'
        "[profiles.default]\n"
        'routes = ["pi:openrouter/stealth/ox-alpha"]\n'
        'execution = "remote"\n'
    )

    with pytest.raises(ConfigError, match="sensitive"):
        load_config(project, user_path=None)


def test_sensitive_project_rejects_remote_profile_inherited_from_user(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text(
        '[profiles.inherited]\nroutes = ["pi:openrouter/model"]\nexecution = "remote"\n'
    )
    project.write_text('[project]\nprivacy_mode = "sensitive"\n')

    with pytest.raises(ConfigError, match="sensitive"):
        load_config(project, user_path=user)


def test_sensitive_project_accepts_local_override_of_remote_user_profile(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text('[profiles.default]\nroutes = ["pi:openrouter/model"]\nexecution = "remote"\n')
    project.write_text(
        '[project]\nprivacy_mode = "sensitive"\n'
        '[profiles.default]\nroutes = []\nexecution = "local"\n'
    )

    config = load_config(project, user_path=user)

    assert config.profile("default").execution == "local"
    assert config.profile("default").routes == ()


@pytest.mark.parametrize(
    "route",
    [
        "pi:openrouter/stealth/ox-alpha",
        "pi:google/gemini-2.5-flash",
        "pi:anthropic/claude-sonnet-4",
    ],
)
def test_local_profile_rejects_provider_backed_pi_route(tmp_path: Path, route: str) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        '[project]\nprivacy_mode = "sensitive"\n'
        "[profiles.default]\n"
        f'routes = ["{route}"]\n'
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
        "[profiles.default]\n"
        "timeout_seconds = 0\n"
        "max_output_tokens = -1\n"
    )

    with pytest.raises(ConfigError):
        load_config(project, user_path=None)


def test_missing_project_config_uses_safe_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml", user_path=None)

    assert config.project.privacy_mode == "private"
    assert config.profile("default").routes == ()


def test_project_directory_without_config_uses_safe_defaults(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    config = load_config(project, user_path=None)

    assert config.project.privacy_mode == "private"
    assert config.profile("default").routes == ()
    assert config.path is None


def test_dangling_project_config_symlink_is_not_treated_as_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "reviewctl.toml").symlink_to(tmp_path / "missing.toml")

    with pytest.raises(ConfigError, match="regular file"):
        load_config(project, user_path=None)


def test_project_config_swap_to_symlink_fails_at_confined_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "reviewctl.toml"
    external = tmp_path / "external.toml"
    project.write_text('[project]\nprivacy_mode = "sensitive"\n')
    external.write_text(
        '[project]\nprivacy_mode = "personal"\n'
        '[profiles.default]\nroutes = ["pi:openrouter/model"]\nexecution = "remote"\n'
    )

    def swap_then_read(path: Path) -> bytes:
        project.unlink()
        project.symlink_to(external)
        return read_confined_bytes(path)

    monkeypatch.setattr(config_module, "read_confined_bytes", swap_then_read, raising=False)

    with pytest.raises(ConfigError, match="could not read configuration"):
        load_config(project, user_path=None)


def test_project_directory_swap_cannot_redirect_config_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / "reviewctl.toml").write_text(
        '[project]\nid = "project-one"\nprivacy_mode = "sensitive"\n'
    )
    (external / "reviewctl.toml").write_text(
        '[project]\nid = "project-one"\nprivacy_mode = "personal"\n'
    )

    def swap_during_lookup(path: Path) -> Path:
        candidate = path.expanduser()
        assert candidate.is_dir()
        candidate.rename(displaced)
        candidate.symlink_to(external, target_is_directory=True)
        return candidate.resolve() / "reviewctl.toml"

    monkeypatch.setattr(config_module, "_config_path", swap_during_lookup)

    assert load_config(project, user_path=None).project.privacy_mode == "sensitive"


def test_project_config_open_remains_bound_to_the_directory_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / "reviewctl.toml").write_text(
        '[project]\nid = "project-one"\nprivacy_mode = "sensitive"\n'
    )
    (external / "reviewctl.toml").write_text(
        '[project]\nid = "project-one"\nprivacy_mode = "personal"\n'
    )

    @contextmanager
    def swap_then_open(parent_descriptor: int, path: Path, flags: int, mode: int = 0o600):
        project.rename(displaced)
        project.symlink_to(external, target_is_directory=True)
        with confined_relative_regular_descriptor(
            parent_descriptor, path, flags, mode
        ) as descriptor:
            yield descriptor

    monkeypatch.setattr(config_module, "confined_relative_regular_descriptor", swap_then_open)

    assert load_config(project, user_path=None).project.privacy_mode == "sensitive"


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
    for route in ("pi:model", "pi:/model", "pi:provider/"):
        with pytest.raises(ConfigError, match="provider/model"):
            parse_route(route)


def test_review_dimensions_are_normalized_and_project_required(tmp_path: Path) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        "[project]\n"
        'required_dimensions = ["security"]\n'
        "[profiles.default]\n"
        'dimensions = ["architecture", "security"]\n'
    )

    config = load_config(project, user_path=None)

    assert config.project.required_dimensions == ("security",)
    assert config.profile("default").dimensions == ("architecture", "security")


@pytest.mark.parametrize(
    "value",
    [
        '["security", "security"]',
        '["not-a-common-dimension"]',
        '["custom." ]',
    ],
)
def test_invalid_review_dimensions_are_rejected(tmp_path: Path, value: str) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(f"[profiles.default]\ndimensions = {value}\n")

    with pytest.raises(ConfigError, match="dimension"):
        load_config(project, user_path=None)


def test_init_writes_portable_project_id_and_local_origin(tmp_path: Path) -> None:
    assert run_cli(["init", "--project", str(tmp_path)]) == 0

    config = load_config(tmp_path / "reviewctl.toml", user_path=None)
    identity = json.loads((tmp_path / ".reviewctl/identity.json").read_text())

    assert config.project.project_id.startswith("project-")
    assert config.project.portable_project_id is True
    assert identity["projectId"] == config.project.project_id
    assert identity["originId"].startswith("origin-")
    assert (tmp_path / ".reviewctl/identity.json").stat().st_mode & 0o777 == 0o600


def test_config_profile_and_route_reject_non_strings() -> None:
    config = load_config(Path("/missing/project.toml"), user_path=None)
    with pytest.raises(ConfigError, match="not configured"):
        config.profile("missing")
    with pytest.raises(ConfigError, match="string"):
        parse_route(7)  # type: ignore[arg-type]


def test_config_read_rejects_malformed_and_non_table_toml(tmp_path: Path, monkeypatch) -> None:
    malformed = tmp_path / "malformed.toml"
    malformed.write_text("[broken\n")
    with pytest.raises(ConfigError, match="could not read"):
        config_module._read(malformed)

    table = tmp_path / "table.toml"
    table.write_text("project = true\n")
    with pytest.raises(ConfigError, match="project must"):
        load_config(table, user_path=None)

    monkeypatch.setattr(config_module.tomllib, "loads", lambda raw: [])
    with pytest.raises(ConfigError, match="TOML table"):
        config_module._read(malformed)


def test_config_read_rejects_existing_non_regular_project_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "reviewctl.toml").mkdir()
    user = tmp_path / "user.toml"
    user.write_text('[profiles.default]\nroutes = ["pi:openrouter/model"]\nexecution = "remote"\n')

    with pytest.raises(ConfigError, match="regular file"):
        load_config(project, user_path=user)


def test_config_merge_handles_nested_tables_and_bad_profile_values() -> None:
    merged = config_module._merge_tables(
        {"profiles": {"default": "not-a-table"}, "project": {"name": "base"}},
        {"profiles": {"default": {"routes": []}}, "project": {"id": "portable"}},
    )
    assert merged["profiles"]["default"] == {"routes": []}
    assert merged["project"] == {"name": "base", "id": "portable"}
    with pytest.raises(ConfigError, match="TOML table"):
        config_module._merge_tables({}, {"profiles": {"default": "bad"}})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("response_contract", '""', "non-empty"),
        ("timeout_seconds", "0", "positive"),
        ("max_attempts", "4", "at most"),
        ("max_output_tokens", "0", "positive"),
        ("execution", '"invalid"', "local or remote"),
        ("tools", '"invalid"', "none or read-only"),
        ("dimensions", '["invalid"]', "dimension"),
        ("routes", '"pi:model"', "array of strings"),
        ("routes", '["pi:model", 7]', "array of strings"),
    ],
)
def test_profile_rejects_invalid_fields(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(
        "[profiles.default]\n"
        + (f"routes = {value}\n" if field == "routes" else 'routes = ["pi:fake/model"]\n')
        + ("" if field == "routes" else f"{field} = {value}\n")
    )
    with pytest.raises(ConfigError, match=message):
        load_config(project, user_path=None)


def test_profile_accepts_null_output_tokens_and_rejects_non_table_and_openrouter_model() -> None:
    assert (
        config_module._profile(
            "default",
            {"routes": [], "max_output_tokens": None},
            "private",
            ("correctness",),
        ).max_output_tokens
        is None
    )
    with pytest.raises(ConfigError, match="TOML table"):
        config_module._profile("default", [], "private", ())
    with pytest.raises(ConfigError, match="remote"):
        config_module._profile(
            "local",
            {"routes": ["pi:openrouter/model"], "execution": "local"},
            "private",
            (),
        )


@pytest.mark.parametrize(
    ("section", "value", "message"),
    [
        ("privacy_mode", '"unknown"', "privacy"),
        ("visibility", '"hidden"', "visibility"),
        ("id", '"../bad"', "project.id"),
        ("required_dimensions", '["invalid"]', "dimension"),
    ],
)
def test_project_settings_reject_invalid_values(
    tmp_path: Path, section: str, value: str, message: str
) -> None:
    project = tmp_path / "reviewctl.toml"
    project.write_text(f"[project]\n{section} = {value}\n")
    with pytest.raises(ConfigError, match=message):
        load_config(project, user_path=None)


def test_config_rejects_invalid_user_privacy_and_profiles_table(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text('[project]\nprivacy_mode = "unknown"\n')
    project.write_text('[project]\nprivacy_mode = "private"\n')
    with pytest.raises(ConfigError, match="privacy"):
        load_config(project, user_path=user)

    project.write_text('profiles = "not-a-table"\n')
    with pytest.raises(ConfigError, match="profiles must"):
        load_config(project, user_path=None)


def test_config_keeps_stricter_project_privacy_and_checks_all_profiles(tmp_path: Path) -> None:
    user = tmp_path / "user.toml"
    project = tmp_path / "reviewctl.toml"
    user.write_text('[project]\nprivacy_mode = "private"\n')
    project.write_text(
        '[project]\nprivacy_mode = "sensitive"\n'
        '[profiles.first]\nroutes = []\nexecution = "local"\n'
        '[profiles.later]\nroutes = ["pi:fake/model"]\nexecution = "remote"\n'
    )
    with pytest.raises(ConfigError, match="sensitive"):
        load_config(project, user_path=user)


def test_config_identity_and_string_integer_helpers_cover_defaults() -> None:
    assert config_module._string(None, "name", "default") == "default"
    with pytest.raises(ConfigError, match="non-empty"):
        config_module._string(" ", "name", "default")
    assert config_module._positive_int(None, "limit", 3) == 3
    with pytest.raises(ConfigError, match="positive"):
        config_module._positive_int(True, "limit", 3)
    with pytest.raises(ConfigError, match="at most"):
        config_module._positive_int(4, "limit", 3, maximum=3)
    assert config_module._project_identity(
        "portable", project_path=Path("."), resolved_project=None
    ) == ("portable", True)
