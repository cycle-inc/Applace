"""The GitHub connection: one organisation, answered once (D2b)."""

from __future__ import annotations

import json

import pytest
from conftest import FakeGitHub, connected

from applace import github
from applace.github import GitHubLink, NotConnected
from applace.paths import ApplacePaths


def test_a_home_with_no_connection_says_so_rather_than_guessing(
    paths: ApplacePaths,
) -> None:
    assert github.load(paths) is None
    with pytest.raises(NotConnected) as raised:
        github.require(paths)
    assert "applace github connect" in str(raised.value)


def test_connecting_is_saved_and_read_back_whole(paths: ApplacePaths) -> None:
    link = GitHubLink(org="acme", visibility="internal", prefix="app-", team="web")
    github.save(paths, link)
    assert github.load(paths) == link


def test_saving_the_connection_keeps_the_rest_of_the_config(
    paths: ApplacePaths,
) -> None:
    paths.config.write_text(json.dumps({"deploy": {"vercel": "x"}}), encoding="utf-8")
    github.save(paths, GitHubLink(org="acme"))
    data = json.loads(paths.config.read_text(encoding="utf-8"))
    assert data["deploy"] == {"vercel": "x"}
    assert data["github"]["org"] == "acme"


def test_github_com_and_an_enterprise_host_have_different_api_roots() -> None:
    assert GitHubLink(org="acme").api == "https://api.github.com"
    assert GitHubLink(org="acme", host="git.acme.dev").api == "https://git.acme.dev/api/v3"


def test_the_prefix_is_part_of_the_repository_name() -> None:
    link = GitHubLink(org="acme", prefix="lovable-")
    assert link.repo_name("team-dashboard") == "lovable-team-dashboard"
    assert link.full_name("team-dashboard") == "acme/lovable-team-dashboard"


def test_the_token_comes_from_the_environment_before_anything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLACE_GITHUB_TOKEN", "  from-applace  ")
    assert github.token(GitHubLink(org="acme")) == "from-applace"


def test_no_token_anywhere_is_an_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in github.TOKEN_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(github, "_gh_token", lambda link: None)
    with pytest.raises(github.GitHubError) as raised:
        github.token(GitHubLink(org="acme"))
    assert "gh auth login" in str(raised.value)


def test_the_gh_account_is_named_when_the_connection_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A laptop logged into two accounts hands out the wrong token otherwise."""
    seen: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "gho_x\n"

    monkeypatch.setattr(github.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        github.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), Completed())[1]
    )
    link = GitHubLink(org="acme", user="0xelight", host="github.com")
    assert github._gh_token(link) == "gho_x"
    assert seen[0] == ["gh", "auth", "token", "--hostname", "github.com", "--user", "0xelight"]


def test_creating_a_repository_asks_for_the_configured_visibility(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    link = connected(paths, fake_github, visibility="private")
    repository = github.create_repository(link, "team-dashboard", description="A board")
    assert repository.full_name == "acme/team-dashboard"
    assert repository.private is True
    assert repository.created is True
    assert repository.clone_url.startswith("file://")


def test_a_name_that_is_already_taken_is_adopted_not_refused(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    """Re-creating an app must land back on its own history, never delete it."""
    link = connected(paths, fake_github)
    first = github.create_repository(link, "team-dashboard")
    again = github.create_repository(link, "team-dashboard")
    assert again.full_name == first.full_name
    assert again.created is False


def test_a_repository_that_does_not_exist_reads_as_none(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    link = connected(paths, fake_github)
    assert github.get_repository(link, "acme/nothing-here") is None


def test_a_refusal_carries_githubs_own_message(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    link = connected(paths, fake_github)
    fake_github.refuse_creation = "Resource not accessible by personal access token"
    with pytest.raises(github.GitHubError) as raised:
        github.create_repository(link, "team-dashboard")
    assert "not allowed to create repositories in acme" in str(raised.value)
    assert "personal access token" in str(raised.value)


def test_a_configured_team_is_granted_write_access(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    link = connected(paths, fake_github, team="web")
    github.create_repository(link, "team-dashboard")
    assert ("PUT", "/orgs/acme/teams/web/repos/acme/team-dashboard") in fake_github.requests


def test_every_call_carries_the_token(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    link = connected(paths, fake_github)
    github.create_repository(link, "team-dashboard")
    assert set(fake_github.tokens) == {"t0ken-for-tests"}


def test_an_unreachable_api_is_an_error_a_human_can_act_on(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    link = GitHubLink(org="acme", api_base="http://127.0.0.1:1")
    with pytest.raises(github.GitHubError) as raised:
        github.get_repository(link, "acme/x")
    assert "could not be reached" in str(raised.value)
