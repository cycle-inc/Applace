"""Vercel, against a fake that behaves like the real API.

The point of every test here is D12: what Vercel builds is a commit of the app's
GitHub repository. The upload path exists and is tested, but it is tested as
what it is -- the fallback for an app that has no repository, and one that says
so out loud.
"""

from __future__ import annotations

import shutil
from typing import Any

import pytest
import yaml
from conftest import FakeGitHub, FakeVercel, connected, on_vercel

from applace import gitrepo, vercel
from applace.apps import create_app, deploy_app, require_app
from applace.db import Connection, list_deployments
from applace.deploy import DeployRefused
from applace.paths import ApplacePaths
from applace.vercel import NotConnected

Home = tuple[ApplacePaths, Connection]

BUILD = "sh -c 'mkdir -p out && printf \"<html>built</html>\" > out/index.html'"


def _stack(paths: ApplacePaths, **changes: Any) -> None:
    manifest = paths.stacks / "fake" / "stack.yaml"
    data: dict[str, Any] = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.update(changes)
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def shippable(home: Home) -> Any:
    paths, conn = home
    _stack(
        paths,
        commands={"install": "true", "dev": "true", "typecheck": "true", "build": BUILD},
        deploy=["local", "vercel"],
    )
    return paths, conn


def make_app(shippable: Any, **kwargs: Any) -> Any:
    paths, conn = shippable
    create_app(paths, conn, name="Ship It", stack_name="fake", install=False, **kwargs)
    return require_app(conn, "ship-it")


# -- the commit is what goes live (D12) ------------------------------------


def test_a_vercel_deploy_names_a_commit_of_the_apps_repository(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    row = make_app(shippable)

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    assert report.ok, report.message
    assert report.url is not None and report.url.startswith("https://")
    source = fake_vercel.last_deployment["gitSource"]
    assert source == {
        "type": "github",
        "repoId": "repo-ship-it",
        "ref": "main",
        "sha": gitrepo.head(paths.app("ship-it")).sha,
    }
    assert report.detail["how"] == "github"
    # And the project itself is attached to the repository, so a human can
    # redeploy it from Vercel with no Applace anywhere.
    assert fake_vercel.projects["ship-it"]["link"]["repo"] == "ship-it"
    assert row["github_repo"] == "acme/ship-it"


def test_the_project_is_created_once_and_reused(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)

    deploy_app(paths, conn, "ship-it", target="vercel")
    deploy_app(paths, conn, "ship-it", target="vercel")

    creations = [
        path for method, path in fake_vercel.requests
        if method == "POST" and path.startswith("/v11/projects")
    ]
    assert len(creations) == 1
    assert len(fake_vercel.deployments) == 2


def test_the_stacks_own_commands_are_what_vercel_is_told_to_run(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    """D7: the stack decides how the app is built, not Vercel's framework guess."""
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)

    deploy_app(paths, conn, "ship-it", target="vercel")

    project = fake_vercel.projects["ship-it"]
    assert project["buildCommand"] == BUILD
    assert project["outputDirectory"] == "out"


def test_production_is_the_only_deploy_that_carries_the_production_target(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)

    deploy_app(paths, conn, "ship-it", target="vercel")
    assert fake_vercel.last_deployment["target"] is None

    live = deploy_app(
        paths, conn, "ship-it", target="vercel",
        environment="production", confirm=True,
    )
    assert fake_vercel.last_deployment["target"] == "production"
    # A production URL is the alias, not the per-build hostname: it is the one
    # that still means something tomorrow.
    assert live.url == "https://ship-it.vercel.app"


def test_a_build_that_vercel_fails_comes_back_with_its_log(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)
    fake_vercel.ready_state = "ERROR"

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    assert not report.ok
    assert report.message == "the build failed on Vercel"
    assert report.detail["log"] == ["npm run build", "error"]
    row = list_deployments(conn, str(require_app(conn, "ship-it")["id"]))[0]
    assert str(row["status"]) == "failed"


def test_a_build_is_waited_for_rather_than_reported_as_started(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)
    fake_vercel.polls_before_ready = 3

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    assert report.ok
    assert report.detail["state"] == "READY"
    assert fake_vercel.last_deployment["polls"] >= 3


# -- secrets (D8) ----------------------------------------------------------


def test_values_reach_vercel_and_only_names_come_back(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    from applace import env

    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)
    env.set_value(paths, "ship-it", "TEST_API_URL", "https://api.internal/v3")

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    assert fake_vercel.env == [
        {
            "key": "TEST_API_URL",
            "value": "https://api.internal/v3",
            "type": "encrypted",
            "target": ["preview"],
        }
    ]
    assert report.detail["env"] == ["TEST_API_URL"]
    assert "https://api.internal/v3" not in str(report.as_dict())


# -- the fallback, and the refusals ----------------------------------------


def test_an_app_with_no_repository_is_uploaded_and_told_that_it_was(
    shippable: Any, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    on_vercel(paths, fake_vercel)
    make_app(shippable)

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    assert report.ok
    assert report.detail["how"] == "upload"
    assert len(fake_vercel.uploads) == 1
    assert [entry["file"] for entry in fake_vercel.last_deployment["files"]] == [
        "index.html"
    ]
    assert any("no GitHub repository" in warning for warning in report.warnings)


def test_a_commit_github_never_received_is_refused_before_vercel_is_called(
    shippable: Any, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel)
    make_app(shippable)
    # The repository goes away underneath us: the push cannot land, so Vercel
    # would be asked to build a sha that is not there.
    shutil.rmtree(fake_github.bare("acme/ship-it"))
    (paths.app("ship-it") / "src" / "main.txt").write_text("more", encoding="utf-8")
    gitrepo.commit_all(paths.app("ship-it"), "A commit GitHub never saw")

    with pytest.raises(DeployRefused) as caught:
        deploy_app(paths, conn, "ship-it", target="vercel")

    assert caught.value.code == "not-pushed"
    assert fake_vercel.deployments == {}


def test_deploying_to_vercel_before_connecting_it_says_what_to_run(
    shippable: Any, fake_github: FakeGitHub
) -> None:
    paths, conn = shippable
    connected(paths, fake_github)
    make_app(shippable)

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    # Not an exception: the deployment was attempted, the journal says it
    # failed, and the message is the one command that fixes it.
    assert not report.ok
    assert "applace vercel connect" in report.message


def test_a_missing_token_is_an_error_about_the_token(
    shippable: Any, fake_vercel: FakeVercel, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, conn = shippable
    on_vercel(paths, fake_vercel)
    make_app(shippable)
    monkeypatch.delenv("APPLACE_VERCEL_TOKEN")
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)

    report = deploy_app(paths, conn, "ship-it", target="vercel")

    assert not report.ok
    assert "vercel.com/account/tokens" in report.message


# -- the link itself -------------------------------------------------------


def test_the_link_is_saved_alongside_github_and_not_over_it(
    paths: ApplacePaths, fake_github: FakeGitHub, fake_vercel: FakeVercel
) -> None:
    connected(paths, fake_github)
    on_vercel(paths, fake_vercel, team="team_acme")

    link = vercel.load(paths)
    assert link is not None and link.team == "team_acme"
    assert "teamId=team_acme" in link.scoped("/v9/projects")
    # The GitHub connection is still there: one config file, two sections.
    from applace.github import load as github_load

    assert github_load(paths) is not None


def test_requiring_a_link_that_is_not_there_names_the_command(
    paths: ApplacePaths,
) -> None:
    with pytest.raises(NotConnected) as caught:
        vercel.require(paths)

    assert "applace vercel connect" in str(caught.value)
