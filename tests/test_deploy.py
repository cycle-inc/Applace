"""Deployment: the gate, the journal, and a local target that really serves.

The stack here builds for real -- `sh -c` writing an `index.html` -- because the
thing under test is that what a human opens is what the commit built, and a
no-op build would prove nothing about that.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from applace import deploy, gitrepo, preview
from applace.apps import (
    app_detail,
    create_app,
    deploy_app,
    require_app,
    rollback_app,
    stop_deployments,
)
from applace.db import Connection, list_deployments
from applace.deploy import DeployRefused
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]

# Writes the page the commit says, so a test can tell two deployments apart.
BUILD = (
    "sh -c 'mkdir -p out && "
    "printf \"<html><body><h1>%s</h1></body></html>\" \"$(cat src/main.txt)\" "
    "> out/index.html'"
)


def _stack(paths: ApplacePaths, **changes: Any) -> None:
    manifest = paths.stacks / "fake" / "stack.yaml"
    data: dict[str, Any] = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.update(changes)
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def app(home: Home) -> Any:
    paths, conn = home
    _stack(
        paths,
        commands={"install": "true", "dev": "true", "build": BUILD, "typecheck": "true"},
        deploy=["local", "vercel"],
    )
    report = create_app(paths, conn, name="Ship It", stack_name="fake", install=False)
    yield paths, conn, report.path
    try:
        stop_deployments(paths, conn, "ship-it")
    except Exception:  # pragma: no cover - the app may already be gone
        pass


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8")


def commit(root: Path, text: str, message: str) -> str:
    (root / "src" / "main.txt").write_text(text, encoding="utf-8")
    return gitrepo.commit_all(root, message).sha


# -- the local target ------------------------------------------------------


def test_a_local_deploy_serves_the_built_files_on_a_url_that_answers(app: Any) -> None:
    paths, conn, _ = app
    report = deploy_app(paths, conn, "ship-it")

    assert report.ok, report.message
    assert report.target == "local"
    assert report.environment == "preview"
    assert report.url is not None
    assert "Ship It" in fetch(report.url)


def test_a_deployment_is_a_commit_not_the_working_tree(app: Any) -> None:
    """The whole design rests on this: what is live has a sha (D1, D2)."""
    paths, conn, root = app
    head = gitrepo.head(root).sha
    (root / "src" / "main.txt").write_text("Not committed", encoding="utf-8")

    report = deploy_app(paths, conn, "ship-it")

    assert report.commit == head
    assert "Not committed" not in fetch(str(report.url))
    assert any("uncommitted" in warning for warning in report.warnings)


def test_deploying_again_replaces_the_server_rather_than_adding_one(app: Any) -> None:
    paths, conn, root = app
    first = deploy_app(paths, conn, "ship-it")
    commit(root, "Second version", "Say something else")
    second = deploy_app(paths, conn, "ship-it")

    assert "Second version" in fetch(str(second.url))
    assert preview.process_command(int(first.detail["pid"])) is None
    rows = {str(row["id"]): str(row["status"]) for row in list_deployments(conn, _id(conn))}
    assert rows[first.id] == "stopped"
    assert rows[second.id] == "live"


def test_a_red_build_is_a_failed_deployment_with_the_build_log(app: Any) -> None:
    paths, conn, _ = app
    _stack(
        paths,
        commands={"install": "true", "dev": "true", "typecheck": "true",
                  "build": "sh -c 'echo cannot resolve ./missing >&2; exit 1'"},
    )

    report = deploy_app(paths, conn, "ship-it")

    assert not report.ok
    assert report.status == "failed"
    assert report.url is None
    assert "cannot resolve ./missing" in report.detail["log"]
    row = list_deployments(conn, _id(conn))[0]
    assert str(row["status"]) == "failed"


def test_a_build_that_produces_nothing_to_serve_says_so(app: Any) -> None:
    paths, conn, _ = app
    _stack(
        paths,
        commands={"install": "true", "dev": "true", "typecheck": "true", "build": "true"},
    )

    report = deploy_app(paths, conn, "ship-it")

    assert not report.ok
    assert "index.html" in report.message


def test_stopping_an_app_takes_its_local_deployment_down(app: Any) -> None:
    paths, conn, _ = app
    report = deploy_app(paths, conn, "ship-it")

    assert stop_deployments(paths, conn, "ship-it") is True

    assert preview.process_command(int(report.detail["pid"])) is None
    assert stop_deployments(paths, conn, "ship-it") is False


# -- the gate (D6) ---------------------------------------------------------


def test_production_without_a_human_is_refused_before_anything_is_built(
    app: Any,
) -> None:
    paths, conn, _ = app
    with pytest.raises(DeployRefused) as caught:
        deploy_app(paths, conn, "ship-it", environment="production")

    assert caught.value.code == "confirm-required"
    # Refused before the journal opens: a deploy that never happened must not
    # leave a row saying it did.
    assert list_deployments(conn, _id(conn)) == []


def test_production_with_a_human_goes_through_and_is_recorded_as_confirmed(
    app: Any,
) -> None:
    paths, conn, _ = app
    report = deploy_app(paths, conn, "ship-it", environment="production", confirm=True)

    assert report.ok
    row = list_deployments(conn, _id(conn))[0]
    assert int(row["confirmed"]) == 1
    assert str(row["environment"]) == "production"


def test_a_policy_can_take_production_off_the_table_entirely(app: Any) -> None:
    paths, conn, _ = app
    paths.policy.write_text(
        "exposure:\n  production_deploys: deny\n", encoding="utf-8"
    )

    with pytest.raises(DeployRefused) as caught:
        deploy_app(paths, conn, "ship-it", environment="production", confirm=True)

    assert caught.value.code == "policy"


def test_an_allow_policy_still_needs_the_human(app: Any) -> None:
    """D6 is a property of the harness: a policy can tighten it, never loosen it."""
    paths, conn, _ = app
    paths.policy.write_text(
        "exposure:\n  production_deploys: allow\n", encoding="utf-8"
    )

    with pytest.raises(DeployRefused) as caught:
        deploy_app(paths, conn, "ship-it", environment="production")

    assert caught.value.code == "confirm-required"


def test_a_stack_that_does_not_declare_a_target_refuses_it(app: Any) -> None:
    paths, conn, _ = app
    _stack(paths, deploy=["local"])

    with pytest.raises(DeployRefused) as caught:
        deploy_app(paths, conn, "ship-it", target="vercel")

    assert caught.value.code == "unsupported-target"


def test_an_unknown_target_is_refused_with_the_list_of_real_ones(app: Any) -> None:
    paths, conn, _ = app
    with pytest.raises(DeployRefused) as caught:
        deploy_app(paths, conn, "ship-it", target="netlify")

    assert caught.value.code == "unknown-target"
    assert caught.value.detail["targets"] == ["local", "vercel"]


# -- rollback --------------------------------------------------------------


def test_rollback_puts_the_previous_commit_back_without_moving_the_worktree(
    app: Any,
) -> None:
    paths, conn, root = app
    first = deploy_app(paths, conn, "ship-it")
    commit(root, "Broken version", "Ship a regression")
    broken = deploy_app(paths, conn, "ship-it")
    assert "Broken version" in fetch(str(broken.url))

    back = rollback_app(paths, conn, "ship-it")

    assert back.commit == first.commit
    assert "Ship It" in fetch(str(back.url))
    # The rollback built an older commit in a worktree; the app itself is still
    # on the regression, which is what someone is about to fix.
    assert (root / "src" / "main.txt").read_text() == "Broken version"
    assert gitrepo.head(root).sha == broken.commit
    assert not list(paths.work.glob("*")) if paths.work.exists() else True


def test_rollback_can_name_the_commit_it_wants(app: Any) -> None:
    paths, conn, root = app
    deploy_app(paths, conn, "ship-it")
    original = gitrepo.head(root).sha
    commit(root, "Second", "Second")
    commit(root, "Third", "Third")
    deploy_app(paths, conn, "ship-it")

    back = rollback_app(paths, conn, "ship-it", commit=original[:10])

    assert back.commit == original
    assert "Ship It" in fetch(str(back.url))


def test_rollback_refuses_a_commit_that_is_not_in_the_repository(app: Any) -> None:
    paths, conn, _ = app
    deploy_app(paths, conn, "ship-it")

    with pytest.raises(DeployRefused) as caught:
        rollback_app(paths, conn, "ship-it", commit="0" * 40)

    assert caught.value.code == "unknown-commit"


def test_rollback_on_an_app_that_was_never_deployed_says_so(app: Any) -> None:
    paths, conn, _ = app
    with pytest.raises(DeployRefused) as caught:
        rollback_app(paths, conn, "ship-it")

    assert caught.value.code == "never-deployed"


# -- secrets (D8) ----------------------------------------------------------


def test_the_build_gets_the_value_and_the_report_gets_only_the_name(app: Any) -> None:
    from applace import env

    paths, conn, root = app
    env.set_value(paths, "ship-it", "TEST_API_URL", "https://api.internal/v3")
    _stack(
        paths,
        commands={
            "install": "true", "dev": "true", "typecheck": "true",
            "build": "sh -c 'mkdir -p out && printf \"<html>%s</html>\" \"$TEST_API_URL\" > out/index.html'",
        },
    )

    report = deploy_app(paths, conn, "ship-it")

    assert "https://api.internal/v3" in fetch(str(report.url))
    assert report.env_names == ["TEST_API_URL"]
    serialised = str(report.as_dict())
    assert "https://api.internal/v3" not in serialised
    log = (paths.app_logs("ship-it") / "deploy.log").read_text()
    assert "https://api.internal/v3" not in log


# -- what the app says about itself ----------------------------------------


def test_get_app_reports_what_is_live_and_from_which_commit(app: Any) -> None:
    paths, conn, _ = app
    report = deploy_app(paths, conn, "ship-it")

    detail = app_detail(paths, conn, require_app(conn, "ship-it"))

    live = detail["deployments"][0]
    assert live["status"] == "live"
    assert live["commit"] == report.commit
    assert live["url"] == report.url
    assert live["target"] == "local"


def test_the_deployment_ports_do_not_collide_with_the_previews(app: Any) -> None:
    paths, conn, _ = app
    report = deploy_app(paths, conn, "ship-it")

    assert int(report.detail["port"]) in deploy.PORT_RANGE
    assert int(report.detail["port"]) not in preview.PORT_RANGE


def _id(conn: Connection) -> str:
    return str(require_app(conn, "ship-it")["id"])


# -- as an agent sees it ---------------------------------------------------


def test_the_tool_refuses_production_and_the_refusal_is_readable(app: Any) -> None:
    from conftest import call_tool

    from applace.server import build_server

    paths, _, _ = app
    server = build_server(paths)

    refused = call_tool(server, "deploy_app", app="ship-it", environment="production")
    assert refused["ok"] is False
    assert refused["code"] == "confirm-required"
    assert "human" in refused["error"]

    shipped = call_tool(server, "deploy_app", app="ship-it")
    assert shipped["ok"] is True
    assert "Ship It" in fetch(shipped["url"])
    assert shipped["environment"] == "preview"


def test_the_tool_says_unknown_app_rather_than_raising(app: Any) -> None:
    from conftest import call_tool

    from applace.server import build_server

    paths, _, _ = app
    answer = call_tool(build_server(paths), "deploy_app", app="not-an-app")
    assert answer["code"] == "unknown-app"
