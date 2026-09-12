"""Applace called from Python (M11, D17, D18, D19).

These tests ask the question the developer embedding Applace asks: can I do from
my own process everything the agent can do, plus the things the agent must never
do, and get answers I can put in an HTTP response without catching anything?
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from applace import Applace
from applace import panel
from applace.db import Connection
from applace.paths import ApplacePaths
from applace.server import build_server

from conftest import call_tool

Home = tuple[ApplacePaths, Connection]

# Everything in the developer's half (D19). None of these is a tool, and the
# test that says so names them rather than counting them, so adding one to the
# class is a deliberate act rather than an accident.
#
# `set_env` is the exception that has to be spelled out: the class method and
# the MCP tool share a name and are different verbs -- the method takes a value,
# the tool declares a name. The test below checks that separately.
DEVELOPER_ONLY = {
    "unset_env", "connect_github", "adopt", "push", "connect_vercel",
    "install_stack", "update_stack", "remove",
}


@pytest.fixture
def ap(home: Home) -> Applace:
    paths, _ = home
    return Applace(paths)


def made(ap: Applace, name: str = "Team Dashboard") -> str:
    created = ap.create(name, stack="fake")
    assert created["ok"] is True, created
    return str(created["app"])


def break_typecheck(paths: ApplacePaths) -> None:
    """Make the fake stack's typecheck fail, the way a bad write would."""
    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["typecheck"] = "sh -c 'echo \"src/a.ts(1,1): error TS1: no.\"; exit 1'"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_the_package_builds_an_app_without_an_agent_or_a_terminal(ap: Applace) -> None:
    slug = made(ap)
    assert slug == "team-dashboard"
    assert [app["app"] for app in ap.apps()["apps"]] == [slug]

    written = ap.write(slug, {"src/hello.txt": "hi\n"}, message="Say hello")
    assert written["ok"] is True
    assert written["commit"]
    read = ap.read(slug, ["src/hello.txt"])["files"]
    assert read[0]["path"] == "src/hello.txt"
    assert read[0]["content"] == "hi\n"


def test_a_red_write_is_an_answer_not_an_exception(ap: Applace, home: Home) -> None:
    """A web handler should never have to catch a TypeScript error (D17)."""
    paths, _ = home
    slug = made(ap)
    break_typecheck(paths)

    written = ap.write(slug, {"src/a.ts": "x\n"})
    assert written["ok"] is False
    assert written["stage"] == "typecheck"
    assert written["errors"][0]["file"] == "src/a.ts"
    # D4: the file is on disk, and nothing was committed.
    assert (paths.app(slug) / "src" / "a.ts").exists()
    assert ap.app(slug)["dirty"] is True


def test_an_unknown_app_is_a_code_on_every_method(ap: Applace) -> None:
    for answer in (
        ap.app("nope"),
        ap.read("nope", ["src/main.txt"]),
        ap.write("nope", {"a.txt": "x"}),
        ap.declare_env("nope", "TEST_KEY"),
        ap.env("nope"),
        ap.card("nope"),
        ap.deployments("nope"),
        ap.stop_preview("nope"),
    ):
        assert answer["ok"] is False
        assert answer["code"] == "unknown-app"
        assert "nope" in answer["error"]


def test_the_two_doors_give_the_same_answer(ap: Applace, home: Home) -> None:
    """The MCP server is a skin over this class, so it cannot drift (D17)."""
    paths, _ = home
    server = build_server(paths)
    slug = made(ap)

    assert call_tool(server, "get_app", app=slug) == ap.app(slug)
    assert call_tool(server, "list_apps") == ap.apps()
    assert call_tool(server, "list_stacks") == ap.stacks()
    assert call_tool(server, "get_skill") == ap.skill()
    assert call_tool(server, "read_files", app=slug, paths=["src/main.txt"]) == ap.read(
        slug, ["src/main.txt"]
    )


def test_the_card_is_the_one_the_panel_serves(ap: Applace, home: Home) -> None:
    """A product that draws its own UI should not call its own harness over a socket."""
    paths, _ = home
    slug = made(ap)
    client = TestClient(Starlette(routes=panel.routes(paths)))

    assert ap.card(slug) == client.get(f"/panel/apps/{slug}").json()
    assert ap.cards()["apps"] == client.get("/panel/apps").json()["apps"]


def test_a_value_goes_in_and_never_comes_back(ap: Applace, home: Home) -> None:
    """The developer's half of a secret (D8, D19)."""
    paths, _ = home
    slug = made(ap)
    ap.declare_env(slug, "TEST_API_TOKEN", "The token the page calls the API with")

    stored = ap.set_env(slug, "TEST_API_TOKEN", "s3cret-value")
    assert stored["ok"] is True
    env_file = paths.env_file(slug)
    assert env_file.read_text(encoding="utf-8").strip().endswith("s3cret-value")
    assert oct(env_file.stat().st_mode)[-3:] == "600"

    listed = ap.env(slug)["env"]
    assert [variable["name"] for variable in listed] == ["TEST_API_TOKEN"]
    assert listed[0]["set"] is True
    # Nothing any method returns carries the value itself.
    everywhere = json.dumps(
        [stored, ap.env(slug), ap.app(slug), ap.card(slug), ap.skill()], default=str
    )
    assert "s3cret-value" not in everywhere

    assert ap.unset_env(slug, "TEST_API_TOKEN")["removed"] is True
    assert ap.env(slug)["env"][0]["set"] is False


def test_setting_a_value_declares_the_name_too(ap: Applace) -> None:
    """A person may be ahead of the agent, and the agent has to be able to see it."""
    slug = made(ap)
    ap.set_env(slug, "TEST_API_BASE_URL", "https://api.acme.test")
    assert [v["name"] for v in ap.env(slug)["env"]] == ["TEST_API_BASE_URL"]


def test_the_developers_half_is_not_reachable_from_the_agents_door(home: Home) -> None:
    """D19: an agent that could do these would be authorising itself."""
    paths, _ = home
    tools = {tool.name for tool in asyncio.run(build_server(paths).list_tools())}
    assert tools & DEVELOPER_ONLY == set()
    # `set_env` exists as a tool, but it is the other verb: a name, never a value.
    declaring = {tool.name: tool for tool in asyncio.run(build_server(paths).list_tools())}
    assert "value" not in declaring["set_env"].input_schema["properties"]
    assert DEVELOPER_ONLY <= {name for name in dir(Applace) if not name.startswith("_")}
    # The class's `set_env` is the value verb, and it is only on the class.
    assert "value" in inspect.signature(Applace.set_env).parameters


def test_making_repositories_public_is_a_policy_decision(ap: Applace, home: Home) -> None:
    """D6: the gate is on exposure, and the policy can take even the choice away."""
    paths, _ = home
    assert ap.connect_github("acme")["ok"] is True
    assert ap.github()["org"] == "acme"

    paths.policy.write_text(
        "exposure:\n  public_repositories: confirm\n", encoding="utf-8"
    )
    asked = ap.connect_github("acme", visibility="public")
    assert asked["ok"] is False
    assert asked["code"] == "confirm-required"
    assert ap.connect_github("acme", visibility="public", confirm=True)["ok"] is True

    paths.policy.write_text("exposure:\n  public_repositories: deny\n", encoding="utf-8")
    refused = ap.connect_github("acme", visibility="public", confirm=True)
    assert refused["ok"] is False
    assert refused["code"] == "policy"


def test_a_bad_argument_is_refused_where_it_was_passed(ap: Applace) -> None:
    wrong = ap.connect_github("acme", visibility="semi-public")
    assert wrong["code"] == "invalid-argument"
    assert "private" in wrong["error"]
    assert ap.connect_github("acme", review="maybe")["code"] == "invalid-argument"


def test_the_reads_answer_before_anything_exists(ap: Applace) -> None:
    """A backend renders its settings page before anybody has built an app."""
    assert ap.apps() == {"ok": True, "apps": []}
    assert ap.cards() == {"ok": True, "apps": []}
    assert ap.github() == {"ok": True, "connected": False}
    assert ap.vercel() == {"ok": True, "connected": False}
    assert ap.drift() == {"ok": True, "apps": []}
    assert ap.policy()["ok"] is True
    assert "fake" in [stack["name"] for stack in ap.installed_stacks()["stacks"]]


def test_a_home_is_a_path_a_caller_already_has(tmp_path: Path, home: Home) -> None:
    paths, _ = home
    for given in (paths, str(paths.home), paths.home):
        assert Applace(given).paths.home == paths.home
    # And a home that does not exist yet is made rather than refused.
    fresh = Applace(tmp_path / "elsewhere")
    assert fresh.paths.apps.is_dir()
    assert fresh.apps() == {"ok": True, "apps": []}


def test_removing_an_app_forgets_it_here_and_nowhere_else(ap: Applace, home: Home) -> None:
    paths, _ = home
    slug = made(ap)
    gone = ap.remove(slug)
    assert gone["ok"] is True
    assert ap.apps()["apps"] == []
    # The repository is still on disk: deleting a person's code is not implied.
    assert (paths.app(slug) / ".git").is_dir()


def test_the_repr_says_which_home(ap: Applace, home: Home) -> None:
    paths, _ = home
    assert str(paths.home) in repr(ap)


def test_a_shot_hands_back_the_image_itself(ap: Applace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes, not a path: the caller decides whether a model or a page sees them."""
    from applace import apps as apps_module
    from applace.eyes import ConsoleMessage, Shot

    slug = made(ap)
    picture = Shot(
        url="http://127.0.0.1:5180/",
        title="Team Dashboard",
        png=b"\x89PNG-not-really",
        console=[ConsoleMessage(level="error", text="TypeError: boom")],
        text_length=12,
    )
    monkeypatch.setattr(apps_module, "screenshot", lambda *a, **k: (picture, True))

    looked: dict[str, Any] = ap.shot(slug)
    assert looked["png"] == b"\x89PNG-not-really"
    assert looked["started_preview"] is True
    assert looked["errors"][0]["text"] == "TypeError: boom"
