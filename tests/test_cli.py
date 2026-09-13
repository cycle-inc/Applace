"""The command line. A human's view of exactly what an agent can do."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeGitHub, connected
from typer.testing import CliRunner

from applace import github
from applace.cli import app
from applace.db import Connection
from applace.github import GitHubLink
from applace.paths import ApplacePaths, paths as applace_paths

runner = CliRunner()
Home = tuple[ApplacePaths, Connection]


def test_init_creates_the_home_and_lists_what_it_found(paths: ApplacePaths) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert paths.db.exists()
    assert paths.apps.is_dir()
    assert "vite-react-ts" in result.output
    assert "git" in result.output


def test_init_is_safe_to_run_twice(paths: ApplacePaths) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["init"]).exit_code == 0


def test_commands_that_need_a_home_say_so_instead_of_crashing(
    tmp_path: Path, monkeypatch: object
) -> None:
    import os

    os.environ["APPLACE_HOME"] = str(tmp_path / "nowhere")
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 1
    assert "applace init" in result.output


def test_new_then_ls(home: Home) -> None:
    result = runner.invoke(
        app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"]
    )
    assert result.exit_code == 0, result.output
    assert "team-dashboard" in result.output
    assert "src/main.txt" in result.output  # the entry point it told us to start at

    listed = runner.invoke(app, ["ls"])
    assert "team-dashboard" in listed.output
    assert "clean" in listed.output


def test_ls_says_when_an_app_has_uncommitted_work(home: Home) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    (paths.app("team-dashboard") / "src" / "main.txt").write_text("edit")
    assert "dirty" in runner.invoke(app, ["ls"]).output


def test_ls_on_an_empty_home_is_not_an_error(home: Home) -> None:
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "No apps yet" in result.output


def test_new_refuses_a_bad_name_with_a_message_not_a_traceback(home: Home) -> None:
    result = runner.invoke(app, ["new", "!!!", "--stack", "fake", "--no-install"])
    assert result.exit_code == 1
    assert "latin letters" in result.output


def test_new_refuses_an_unknown_stack_and_lists_the_real_ones(home: Home) -> None:
    result = runner.invoke(app, ["new", "App", "--stack", "nope", "--no-install"])
    assert result.exit_code == 1
    assert "vite-react-ts" in result.output


def test_stacks_lists_the_builtin_and_the_installed(home: Home) -> None:
    result = runner.invoke(app, ["stacks"])
    assert result.exit_code == 0
    assert "vite-react-ts" in result.output
    assert "fake" in result.output


def test_rm_keeps_the_repository_unless_asked(home: Home) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    result = runner.invoke(app, ["rm", "team-dashboard"])
    assert result.exit_code == 0
    assert paths.app("team-dashboard").is_dir()
    assert "team-dashboard" not in runner.invoke(app, ["ls"]).output


def test_rm_delete_asks_first(home: Home) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    refused = runner.invoke(app, ["rm", "team-dashboard", "--delete"], input="n\n")
    assert refused.exit_code == 1
    assert paths.app("team-dashboard").is_dir()

    accepted = runner.invoke(app, ["rm", "team-dashboard", "--delete", "--yes"])
    assert accepted.exit_code == 0
    assert not paths.app("team-dashboard").exists()


def test_rm_on_an_unknown_app_is_an_error_with_a_list(home: Home) -> None:
    result = runner.invoke(app, ["rm", "nope"])
    assert result.exit_code == 1
    assert "no app called" in result.output


def test_check_runs_the_gate_and_commits_a_hand_edit(home: Home) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Gate App", "--stack", "fake", "--no-install"])
    (paths.app("gate-app") / "src" / "byhand.txt").write_text("hi\n", encoding="utf-8")

    result = runner.invoke(app, ["check", "gate-app", "-m", "Keep the hand edit"])
    assert result.exit_code == 0, result.output
    assert "green" in result.output
    assert "committed" in result.output


def test_shot_writes_the_png_and_prints_what_the_browser_said(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser is faked; what is under test is the report a human reads."""
    import applace.cli as cli_module
    from applace.eyes import ConsoleMessage, FailedRequest, Shot

    paths, _ = home
    runner.invoke(app, ["new", "Shot App", "--stack", "fake", "--no-install"])
    picture = Shot(
        url="http://127.0.0.1:5180/",
        title="Shot App",
        png=b"\x89PNG-not-really",
        console=[ConsoleMessage(level="error", text="TypeError: boom")],
        failed_requests=[FailedRequest(url="/api/teams", method="GET", status=404, error="HTTP 404")],
        text_length=0,
    )
    monkeypatch.setattr(cli_module, "screenshot", lambda *a, **k: (picture, True))

    result = runner.invoke(app, ["shot", "shot-app"])
    # Non-zero: the page is broken, and a script running this should notice.
    assert result.exit_code == 1, result.output
    assert "BLANK" in result.output
    assert "TypeError: boom" in result.output
    assert "/api/teams" in result.output
    assert "started the preview" in result.output
    assert (paths.app_logs("shot-app") / "shot.png").read_bytes() == picture.png


def test_shot_on_a_healthy_page_is_quiet_and_succeeds(
    home: Home, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import applace.cli as cli_module
    from applace.eyes import Shot

    runner.invoke(app, ["new", "Shot App", "--stack", "fake", "--no-install"])
    picture = Shot(
        url="http://127.0.0.1:5180/",
        title="Shot App",
        png=b"\x89PNG-not-really",
        text_length=42,
    )
    monkeypatch.setattr(cli_module, "screenshot", lambda *a, **k: (picture, False))

    destination = tmp_path / "elsewhere" / "page.png"
    result = runner.invoke(app, ["shot", "shot-app", "--out", str(destination)])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output
    assert destination.exists()


def test_shot_says_how_to_get_a_browser_when_there_is_none(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    import applace.cli as cli_module
    from applace.eyes import INSTALL_HINT, EyesUnavailable

    runner.invoke(app, ["new", "Shot App", "--stack", "fake", "--no-install"])

    def refuse(*args: object, **kwargs: object) -> None:
        raise EyesUnavailable(INSTALL_HINT)

    monkeypatch.setattr(cli_module, "screenshot", refuse)
    result = runner.invoke(app, ["shot", "shot-app"])
    assert result.exit_code == 1
    assert "playwright install" in result.output


def test_github_connect_then_status(home: Home, fake_github: FakeGitHub) -> None:
    paths, _ = home
    result = runner.invoke(
        app,
        ["github", "connect", "--org", "acme", "--prefix", "lovable-", "--team", "web"],
    )
    assert result.exit_code == 0, result.output
    assert "Connected acme" in result.output
    assert "lovable-<slug>" in result.output

    # The fake lives on localhost, so point the saved connection at it and ask
    # again: status must report reachability, not just repeat the config.
    link = github.require(paths)
    github.save(paths, GitHubLink(**{**link.as_dict(), "api_base": fake_github.api_base}))
    status = runner.invoke(app, ["github", "status"])
    assert status.exit_code == 0, status.output
    assert "acme is reachable" in status.output
    assert "t0ken-for-tests" not in status.output


def test_github_status_with_nothing_connected_is_an_instruction(home: Home) -> None:
    result = runner.invoke(app, ["github", "status"])
    assert result.exit_code == 1
    assert "applace github connect" in result.output


def test_github_connect_refuses_a_visibility_that_is_not_one(home: Home) -> None:
    result = runner.invoke(app, ["github", "connect", "--org", "acme", "--visibility", "open"])
    assert result.exit_code == 1
    assert "must be one of" in result.output


def test_going_public_asks_a_human_first(home: Home) -> None:
    """D6: the gate is on exposure, and this is the moment of exposure."""
    refused = runner.invoke(
        app, ["github", "connect", "--org", "acme", "--visibility", "public"], input="n\n"
    )
    assert refused.exit_code != 0
    assert "PUBLIC" in refused.output
    assert github.load(applace_paths()) is None

    accepted = runner.invoke(
        app, ["github", "connect", "--org", "acme", "--visibility", "public", "--yes"]
    )
    assert accepted.exit_code == 0
    link = github.load(applace_paths())
    assert link is not None and link.visibility == "public"


def test_push_and_adopt_from_the_terminal(home: Home, fake_github: FakeGitHub) -> None:
    paths, _ = home
    connected(paths, fake_github)
    fake_github.create("acme/already-there")
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])

    adopted = runner.invoke(
        app, ["github", "adopt", "team-dashboard", "--repo", "acme/already-there"]
    )
    assert adopted.exit_code == 0, adopted.output
    assert "already-there" in adopted.output

    pushed = runner.invoke(app, ["push", "team-dashboard"])
    assert pushed.exit_code == 0, pushed.output
    assert "Pushed" in pushed.output


def test_push_with_no_repository_exits_nonzero_with_the_reason(home: Home) -> None:
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    result = runner.invoke(app, ["push", "team-dashboard"])
    assert result.exit_code == 1
    assert "no GitHub repository" in result.output


def test_new_says_where_the_repository_is(home: Home, fake_github: FakeGitHub) -> None:
    paths, _ = home
    connected(paths, fake_github)
    result = runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    assert result.exit_code == 0, result.output
    assert "github  https://github.test/acme/team-dashboard  (pushed)" in result.output


def test_init_reports_the_browser_as_optional(paths: ApplacePaths) -> None:
    """Missing eyes must never make a machine "not ready": they are optional."""
    import applace.init_cmd as init_module

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "Optional:" in result.output
    assert "browser" in result.output
    report = init_module.run_init(paths)
    assert [r.name for r in report.optional] == ["browser"]
    assert report.ready is True


def test_skill_prints_the_document_and_what_this_machine_does(home: Home) -> None:
    result = runner.invoke(app, ["skill"])
    assert result.exit_code == 0, result.output
    assert "write_files is a compiler" in result.output or "compiler" in result.output
    assert "--- this machine ---" in result.output
    assert "github   not connected" in result.output

    brief = runner.invoke(app, ["skill", "--brief"])
    assert "name: applace" not in brief.output
    assert "--- this machine ---" in brief.output


def test_policy_shows_the_defaults_and_then_the_file(home: Home) -> None:
    paths, _ = home
    default = runner.invoke(app, ["policy"])
    assert default.exit_code == 0, default.output
    assert "absent" in default.output
    assert "public repos  confirm" in default.output

    paths.policy.write_text("dependencies:\n  deny: ['left-pad']\n", encoding="utf-8")
    tightened = runner.invoke(app, ["policy"])
    assert "left-pad may not be added" in tightened.output


def test_a_broken_policy_file_is_reported_not_ignored(home: Home) -> None:
    paths, _ = home
    paths.policy.write_text("dependencies: 42\n", encoding="utf-8")
    result = runner.invoke(app, ["policy"])
    assert result.exit_code == 1
    assert "must be a mapping" in result.output


def test_a_policy_can_take_public_repositories_off_the_table(home: Home) -> None:
    """D6 says ask a human; D9 lets a company say the answer is always no."""
    paths, _ = home
    paths.policy.write_text("exposure:\n  public_repositories: deny\n", encoding="utf-8")
    result = runner.invoke(
        app, ["github", "connect", "--org", "acme", "--visibility", "public", "--yes"]
    )
    assert result.exit_code == 1
    assert "forbids public repositories" in result.output
    assert github.load(applace_paths()) is None


def test_env_set_stores_a_value_without_printing_it(home: Home) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Env App", "--stack", "fake", "--no-install"])

    typed = runner.invoke(
        app, ["env", "set", "env-app", "TEST_TOKEN"], input="sk-secret-value\n"
    )
    assert typed.exit_code == 0, typed.output
    assert "sk-secret-value" not in typed.output
    assert "TEST_TOKEN set for env-app" in typed.output
    assert "sk-secret-value" in paths.env_file("env-app").read_text(encoding="utf-8")

    listed = runner.invoke(app, ["env", "ls", "env-app"])
    assert "set      TEST_TOKEN" in listed.output
    assert "sk-secret-value" not in listed.output


def test_env_ls_says_which_variables_are_still_missing(home: Home) -> None:
    paths, conn = home
    runner.invoke(app, ["new", "Env App", "--stack", "fake", "--no-install"])
    from applace.apps import declare_env

    declare_env(paths, conn, "env-app", name="TEST_API_URL", description="Where the API is")
    declare_env(paths, conn, "env-app", name="SERVER_ONLY")

    listed = runner.invoke(app, ["env", "ls", "env-app"])
    assert "MISSING  TEST_API_URL" in listed.output
    assert "Where the API is" in listed.output
    assert "build only" in listed.output  # SERVER_ONLY has no TEST_ prefix


def test_route_add_ls_and_rm_are_the_same_words_the_agent_uses(home: Home) -> None:
    runner.invoke(app, ["new", "Route App", "--stack", "fake", "--no-install"])

    default = runner.invoke(app, ["route", "ls", "route-app"])
    assert default.exit_code == 0, default.output
    assert "the default, nothing declared" in default.output

    added = runner.invoke(
        app,
        [
            "route", "add", "route-app", "customers",
            "--shows", "table tbody tr",
            "--says", "Nine",
            "-d", "The list everyone opens first",
        ],
    )
    assert added.exit_code == 0, added.output
    assert "/customers" in added.output

    listed = runner.invoke(app, ["route", "ls", "route-app"])
    assert "/customers" in listed.output
    assert "shows    table tbody tr" in listed.output
    assert "says     'Nine'" in listed.output
    assert "The list everyone opens first" in listed.output

    removed = runner.invoke(app, ["route", "rm", "route-app", "/customers"])
    assert "Removed /customers" in removed.output
    assert "did not declare" in runner.invoke(
        app, ["route", "rm", "route-app", "/customers"]
    ).output


def test_route_ls_says_when_nothing_is_opened_at_all(home: Home) -> None:
    paths, _ = home
    paths.policy.write_text("gate:\n  visit: false\n", encoding="utf-8")
    runner.invoke(app, ["new", "Route App", "--stack", "fake", "--no-install"])
    listed = runner.invoke(app, ["route", "ls", "route-app"])
    assert "Not visited" in listed.output
    assert "gate: visit: false" in listed.output


def test_route_add_refuses_something_that_is_not_a_route(home: Home) -> None:
    runner.invoke(app, ["new", "Route App", "--stack", "fake", "--no-install"])
    result = runner.invoke(app, ["route", "add", "route-app", "https://example.com/x"])
    assert result.exit_code == 1
    assert "not a usable route" in result.output


def test_policy_shows_whether_the_gate_opens_a_browser(home: Home) -> None:
    paths, _ = home
    assert "every declared route" in runner.invoke(app, ["policy"]).output
    paths.policy.write_text("gate:\n  visit: false\n", encoding="utf-8")
    assert "gate: visit: false" in runner.invoke(app, ["policy"]).output


def test_env_rm_forgets_the_value_and_the_declaration(home: Home) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Env App", "--stack", "fake", "--no-install"])
    runner.invoke(app, ["env", "set", "env-app", "TEST_TOKEN", "--value", "x"])

    removed = runner.invoke(app, ["env", "rm", "env-app", "TEST_TOKEN"])
    assert removed.exit_code == 0, removed.output
    assert "TEST_TOKEN" not in paths.env_file("env-app").read_text(encoding="utf-8")
    assert runner.invoke(app, ["env", "ls", "env-app"]).output.strip().endswith(
        "declares no variables."
    )


def test_env_set_refuses_a_name_that_is_not_a_variable_name(home: Home) -> None:
    runner.invoke(app, ["new", "Env App", "--stack", "fake", "--no-install"])
    result = runner.invoke(app, ["env", "set", "env-app", "my key", "--value", "x"])
    assert result.exit_code == 1
    assert "usable variable name" in result.output


def test_check_exits_nonzero_and_prints_the_errors_when_red(home: Home) -> None:
    import yaml

    paths, _ = home
    runner.invoke(app, ["new", "Gate App", "--stack", "fake", "--no-install"])
    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["typecheck"] = (
        "sh -c 'echo \"src/a.ts(2,3): error TS1005: nope.\"; exit 1'"
    )
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    (paths.app("gate-app") / "src" / "byhand.txt").write_text("hi\n", encoding="utf-8")

    result = runner.invoke(app, ["check", "gate-app"])
    assert result.exit_code == 1
    assert "red at typecheck" in result.output
    assert "src/a.ts:2:3" in result.output
    assert "TS1005" in result.output


# -- deployment ------------------------------------------------------------


def _shippable(paths: ApplacePaths) -> None:
    """Make the fake stack build something real and say where it can go."""
    import yaml

    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["build"] = (
        "sh -c 'mkdir -p out && printf \"<html>shipped</html>\" > out/index.html'"
    )
    data["deploy"] = ["local", "vercel"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_deploy_local_prints_a_url_and_stop_takes_it_down(home: Home) -> None:
    paths, _ = home
    _shippable(paths)
    runner.invoke(app, ["new", "Ship It", "--stack", "fake", "--no-install"])

    result = runner.invoke(app, ["deploy", "ship-it"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:52" in result.output

    listed = runner.invoke(app, ["deployments", "ship-it"])
    assert "live" in listed.output and "local" in listed.output

    stopped = runner.invoke(app, ["stop", "ship-it"])
    assert "local deployment" in stopped.output


def test_deploying_to_production_asks_a_human_first(home: Home) -> None:
    """D6 again, at the other moment of exposure."""
    paths, _ = home
    _shippable(paths)
    runner.invoke(app, ["new", "Ship It", "--stack", "fake", "--no-install"])

    refused = runner.invoke(app, ["deploy", "ship-it", "--production"], input="n\n")
    assert refused.exit_code != 0
    assert "PRODUCTION" in refused.output
    assert runner.invoke(app, ["deployments", "ship-it"]).output.count("live") == 0

    accepted = runner.invoke(app, ["deploy", "ship-it", "--production", "--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert "(production)" in accepted.output
    runner.invoke(app, ["stop", "ship-it"])


def test_a_failed_deploy_exits_nonzero_with_the_build_log(home: Home) -> None:
    import yaml

    paths, _ = home
    _shippable(paths)
    runner.invoke(app, ["new", "Ship It", "--stack", "fake", "--no-install"])
    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["build"] = "sh -c 'echo the bundler gave up >&2; exit 1'"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    result = runner.invoke(app, ["deploy", "ship-it"])
    assert result.exit_code == 1
    assert "FAILED" in result.output
    assert "the bundler gave up" in result.output

    logged = runner.invoke(app, ["logs", "ship-it", "--of", "deploy"])
    assert "the bundler gave up" in logged.output


def test_rollback_from_the_terminal_puts_the_previous_commit_back(home: Home) -> None:
    paths, _ = home
    _shippable(paths)
    runner.invoke(app, ["new", "Ship It", "--stack", "fake", "--no-install"])
    first = runner.invoke(app, ["deploy", "ship-it"])
    (paths.app("ship-it") / "src" / "main.txt").write_text("second\n", encoding="utf-8")
    runner.invoke(app, ["check", "ship-it", "-m", "Second"])
    runner.invoke(app, ["deploy", "ship-it"])

    result = runner.invoke(app, ["rollback", "ship-it"])
    assert result.exit_code == 0, result.output
    assert "FAILED" not in result.output
    # "ship-it → local (preview) from <sha>": back to the sha that was live
    # before the second deploy.
    assert result.output.split()[5] == first.output.split()[5]
    runner.invoke(app, ["stop", "ship-it"])


def test_deployments_on_an_app_that_never_shipped_says_what_to_run(home: Home) -> None:
    paths, _ = home
    _shippable(paths)
    runner.invoke(app, ["new", "Ship It", "--stack", "fake", "--no-install"])

    result = runner.invoke(app, ["deployments", "ship-it"])
    assert "applace deploy ship-it" in result.output


def test_vercel_status_with_nothing_connected_is_an_instruction(home: Home) -> None:
    result = runner.invoke(app, ["vercel", "status"])
    assert result.exit_code == 1
    assert "applace vercel connect" in result.output


def test_vercel_connect_then_status(home: Home, fake_vercel: object) -> None:
    from conftest import FakeVercel

    assert isinstance(fake_vercel, FakeVercel)
    connect = runner.invoke(
        app, ["vercel", "connect", "--api-base", fake_vercel.api_base, "--team", "t_1"]
    )
    assert connect.exit_code == 0, connect.output
    assert "authenticated as acme-ci" in connect.output

    status = runner.invoke(app, ["vercel", "status"])
    assert status.exit_code == 0
    assert "team t_1" in status.output


# -- the handover: `applace open`, and reviewing what an agent wrote (M9) ----


def test_open_falls_back_to_the_directory_when_an_app_has_nothing_else(
    home: Home,
) -> None:
    paths, _ = home
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])

    result = runner.invoke(app, ["open", "team-dashboard", "--print"])
    assert result.exit_code == 0, result.output
    assert "dir" in result.output
    assert str(paths.app("team-dashboard")) in result.output


def test_open_prefers_what_is_live_over_the_repository(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, _ = home
    _shippable(paths)
    connected(paths, fake_github)
    runner.invoke(app, ["new", "Ship It", "--stack", "fake", "--no-install"])
    runner.invoke(app, ["deploy", "ship-it"])

    result = runner.invoke(app, ["open", "ship-it", "--print"])
    assert result.exit_code == 0, result.output
    assert "live" in result.output and "http://127.0.0.1:52" in result.output

    repo = runner.invoke(app, ["open", "ship-it", "--what", "repo", "--print"])
    assert "https://github.test/acme/ship-it" in repo.output

    runner.invoke(app, ["stop", "ship-it"])


def test_open_says_what_an_app_has_when_it_does_not_have_what_was_asked_for(
    home: Home,
) -> None:
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    result = runner.invoke(app, ["open", "team-dashboard", "--what", "live", "--print"])
    assert result.exit_code == 1
    assert "has no live" in result.output
    assert "dir" in result.output


def test_connecting_with_review_says_what_will_happen_to_every_app(
    paths: ApplacePaths, fake_github: FakeGitHub
) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        ["github", "connect", "--org", "acme", "--review", "pr",
         "--api-base", fake_github.api_base],
    )
    assert result.exit_code == 0, result.output
    assert "pull request" in result.output
    assert github.load(paths) is not None and github.load(paths).reviewed  # type: ignore[union-attr]

    status = runner.invoke(app, ["github", "status"])
    assert "a pull request per app" in status.output


def test_a_review_mode_that_does_not_exist_is_refused(paths: ApplacePaths) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["github", "connect", "--org", "acme", "--review", "maybe"])
    assert result.exit_code == 1
    assert "--review must be one of" in result.output


def test_push_prints_the_pull_request_it_opened(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, _ = home
    connected(paths, fake_github, review=github.REVIEW)
    runner.invoke(app, ["new", "Team Dashboard", "--stack", "fake", "--no-install"])
    root = paths.app("team-dashboard")
    (root / "src" / "page.txt").write_text("a page\n", encoding="utf-8")
    runner.invoke(app, ["check", "team-dashboard"])

    result = runner.invoke(app, ["push", "team-dashboard"])
    assert result.exit_code == 0, result.output
    assert "applace/team-dashboard" in result.output
    assert "/pull/1" in result.output
