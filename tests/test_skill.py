"""SKILL.md: the first thing an agent reads, and the last thing to go stale.

These tests are deliberately about *content*. A skill document that no longer
matches the tools is worse than none, because a model believes it.
"""

from __future__ import annotations

from conftest import FakeGitHub, connected

from applace import skill
from applace.db import Connection
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]


def test_the_document_ships_with_the_package() -> None:
    text = skill.text()
    assert text.startswith("---\nname: applace")
    assert "write_files" in text


def test_the_document_names_the_tools_it_tells_an_agent_to_call(home: Home) -> None:
    """If a tool is renamed, this fails here rather than in front of a model."""
    import asyncio

    from applace.server import build_server

    paths, _ = home
    text = skill.text()
    tools = {tool.name for tool in asyncio.run(build_server(paths).list_tools())}
    for name in ("get_skill", "list_stacks", "create_app", "read_files",
                 "write_files", "screenshot_app", "set_env", "deploy_app",
                 "rollback_app"):
        assert name in tools, f"{name} is in SKILL.md but not a tool"
        assert name in text, f"{name} is a tool but not in SKILL.md"


def test_the_document_says_the_things_that_cost_a_round_trip() -> None:
    text = skill.text()
    assert "whole files" in text          # there is no patch format
    assert "Fix red before" in text       # D4's consequence for the agent
    assert "never see a value" in text or "never see the value" in text  # D8
    assert "diverged" in text             # D13


def test_describe_says_what_this_machine_does(home: Home, fake_github: FakeGitHub) -> None:
    paths, _ = home
    described = skill.describe(paths)
    assert described["github"] is None
    assert "fake" in [stack["name"] for stack in described["stacks"]]
    assert described["policy"]["summary"].startswith("any dependency")
    assert described["home"] == str(paths.home)

    connected(paths, fake_github)
    described = skill.describe(paths)
    assert described["github"] is not None
    assert described["github"]["org"] == "acme"
    assert "pushed" in described["github"]["note"]


def test_describe_reports_a_broken_policy_instead_of_hiding_it(home: Home) -> None:
    paths, _ = home
    paths.policy.write_text("exposure:\n  public_repositories: sometimes\n", encoding="utf-8")
    assert "must be one of" in skill.describe(paths)["policy"]["error"]


def test_a_tightened_policy_is_part_of_what_the_agent_is_told(home: Home) -> None:
    paths, _ = home
    paths.policy.write_text(
        "dependencies:\n  allow: ['react']\n  registry: https://npm.acme.internal\n",
        encoding="utf-8",
    )
    described = skill.describe(paths)
    assert "only react may be added" in described["policy"]["summary"]
    assert described["policy"]["exposure"]["public_repositories"] == "confirm"


def test_the_document_teaches_the_deploy_rule_that_an_agent_cannot_guess(
    home: Home, fake_vercel: object
) -> None:
    """D6 has to be in the document, not only in the refusal it produces."""
    from conftest import FakeVercel, on_vercel

    paths, _ = home
    text = skill.text()
    assert "deploy_app" in text and "rollback_app" in text
    assert "a human said yes" in text

    described = skill.describe(paths)
    assert described["deploy"]["targets"] == ["local"]
    assert described["deploy"]["production"] == "confirm"

    assert isinstance(fake_vercel, FakeVercel)
    on_vercel(paths, fake_vercel)
    assert skill.describe(paths)["deploy"]["targets"] == ["local", "vercel"]
