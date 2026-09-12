"""Secrets never enter the model's context (D8).

The tests worth having here are the ones that check a *negative*: that no tool
result, no report and no log carries a value, and that the value still reaches
the process that needs it.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from applace import env
from applace.apps import app_detail, create_app, declare_env, require_app
from applace.db import Connection
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]

SECRET = "sk-live-do-not-print-me"


@pytest.fixture
def made(home: Home) -> tuple[ApplacePaths, Connection, str]:
    paths, conn = home
    create_app(paths, conn, name="Env App", stack_name="fake", install=False)
    return paths, conn, "env-app"


def test_declaring_a_variable_tells_the_agent_who_supplies_the_value(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    result = declare_env(
        paths, conn, slug, name="TEST_API_BASE_URL", description="Where the API is"
    )
    assert result["name"] == "TEST_API_BASE_URL"
    assert result["set"] is False
    assert result["exposed"] is True
    assert result["instruction"] == "applace env set env-app TEST_API_BASE_URL"
    assert "never see the value" in result["note"]


def test_a_name_without_the_stacks_prefix_is_a_warning_not_a_refusal(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    """It is a real variable for the build; it just never reaches the browser."""
    paths, conn, slug = made
    result = declare_env(paths, conn, slug, name="API_BASE_URL")
    assert result["exposed"] is False
    assert "TEST_" in result["warning"]


def test_a_name_that_is_not_a_variable_name_is_refused(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    with pytest.raises(env.EnvError) as raised:
        declare_env(paths, conn, slug, name="my key")
    assert "usable variable name" in str(raised.value)


def test_a_value_lives_outside_the_repository_and_is_readable_by_nobody_else(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    path = env.set_value(paths, slug, "TEST_TOKEN", SECRET)

    assert path == paths.env_file(slug)
    assert paths.apps not in path.parents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert SECRET in path.read_text(encoding="utf-8")


def test_the_value_never_appears_in_anything_an_agent_reads(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    """The whole of D8 in one assertion, against every report there is."""
    paths, conn, slug = made
    declare_env(paths, conn, slug, name="TEST_TOKEN", description="The API token")
    env.set_value(paths, slug, "TEST_TOKEN", SECRET)

    declared = declare_env(paths, conn, slug, name="TEST_TOKEN")
    detail = app_detail(paths, conn, require_app(conn, slug))
    assert SECRET not in repr(declared)
    assert SECRET not in repr(detail)

    # And the state the agent does get is the useful one.
    assert declared["set"] is True
    assert "note" not in declared
    assert detail["env"] == [
        {
            "name": "TEST_TOKEN",
            "description": "The API token",
            "set": True,
            "exposed": True,
        }
    ]
    assert detail["env_missing"] == []


def test_get_app_names_the_variables_nobody_has_answered_yet(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    declare_env(paths, conn, slug, name="TEST_ONE")
    declare_env(paths, conn, slug, name="TEST_TWO")
    env.set_value(paths, slug, "TEST_ONE", "here")

    detail = app_detail(paths, conn, require_app(conn, slug))
    assert detail["env_missing"] == ["TEST_TWO"]


def test_values_round_trip_and_can_be_removed(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, _, slug = made
    env.set_value(paths, slug, "TEST_ONE", "first")
    env.set_value(paths, slug, "TEST_TWO", "second value with spaces")
    assert env.values_for(paths, slug) == {
        "TEST_ONE": "first",
        "TEST_TWO": "second value with spaces",
    }

    assert env.unset_value(paths, slug, "TEST_ONE") is True
    assert env.unset_value(paths, slug, "TEST_ONE") is False
    assert env.values_for(paths, slug) == {"TEST_TWO": "second value with spaces"}


def test_a_multi_line_value_is_refused_rather_than_written_ambiguously(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, _, slug = made
    with pytest.raises(env.EnvError) as raised:
        env.set_value(paths, slug, "TEST_KEY", "-----BEGIN KEY-----\nabc\n")
    assert "more than one line" in str(raised.value)


def test_an_app_with_no_values_reads_as_empty(made: tuple[ApplacePaths, Connection, str]) -> None:
    paths, _, slug = made
    assert env.values_for(paths, slug) == {}


def test_the_declaration_survives_the_app_and_dies_with_it(
    made: tuple[ApplacePaths, Connection, str],
) -> None:
    paths, conn, slug = made
    app_id = str(require_app(conn, slug)["id"])
    declare_env(paths, conn, slug, name="TEST_ONE", description="one")
    # Re-declaring without a description keeps the one that was there.
    declare_env(paths, conn, slug, name="TEST_ONE")
    detail = app_detail(paths, conn, require_app(conn, slug))
    assert detail["env"][0]["description"] == "one"

    conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    conn.commit()
    from applace.db import list_env

    assert list_env(conn, app_id) == []


def test_the_preview_gets_the_values_and_the_log_does_not(
    made: tuple[ApplacePaths, Connection, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value goes into the process. That is the only place it goes."""
    import applace.preview as preview_module
    from applace.apps import start_preview

    paths, conn, slug = made
    env.set_value(paths, slug, "TEST_TOKEN", SECRET)
    seen: dict[str, str] = {}

    def fake_spawn(
        command: str, *, cwd: Path, log_path: Path, environment: dict[str, str] | None = None
    ) -> int:
        seen.update(environment or {})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ready\n", encoding="utf-8")
        return 4242

    monkeypatch.setattr(preview_module, "_spawn", fake_spawn)
    monkeypatch.setattr(preview_module, "_wait_until_ready", lambda url, pid: True)
    monkeypatch.setattr(preview_module, "_matches", lambda row, root: True)
    monkeypatch.setattr(preview_module, "responds", lambda url, timeout=1.0: True)

    running = start_preview(paths, conn, slug)
    assert seen["TEST_TOKEN"] == SECRET
    assert SECRET not in running.log.read_text(encoding="utf-8")
    assert SECRET not in repr(running.as_dict())
