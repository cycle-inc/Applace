"""The MCP server: the surface an agent actually sees.

Exercised through `MCPServer.list_tools` / `call_tool` rather than by calling
the underlying functions, so the tool schemas, the descriptions and the
structured results are all part of what these tests check.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from conftest import FakeGitHub, call_tool, connected
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent

from applace.db import Connection
from applace.paths import ApplacePaths
from applace.server import build_server

Home = tuple[ApplacePaths, Connection]

TOOLS = {
    "get_skill",
    "set_env",
    "list_stacks",
    "create_app",
    "list_apps",
    "get_app",
    "read_files",
    "write_files",
    "start_preview",
    "stop_preview",
    "screenshot_app",
}


call = call_tool


def tools_of(server: MCPServer) -> dict[str, Any]:
    return {t.name: t for t in asyncio.run(server.list_tools())}


def test_the_server_exposes_exactly_the_tools_an_agent_needs(home: Home) -> None:
    paths, _ = home
    tools = tools_of(build_server(paths))
    assert set(tools) == TOOLS
    # The descriptions are the interface; an empty one is a broken tool.
    assert all(tool.description for tool in tools.values())


def test_get_skill_is_the_document_and_the_machine(home: Home) -> None:
    """The first call an agent makes has to answer both halves of "how".

    The skill says how to build; the rest of the payload says what this machine
    does with what you build.
    """
    paths, _ = home
    result = call(build_server(paths), "get_skill")
    assert result["ok"] is True
    assert "write_files" in result["skill"]
    assert "fake" in [stack["name"] for stack in result["stacks"]]
    assert result["github"] is None
    assert result["policy"]["summary"]


def test_set_env_declares_a_name_and_asks_a_human_for_the_value(home: Home) -> None:
    """D8, from the agent's side: it gets an instruction, never a value."""
    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Env App", stack="fake")

    declared = call(
        server, "set_env", app="env-app", name="TEST_API_URL", description="Where the API is"
    )
    assert declared["ok"] is True
    assert declared["set"] is False
    assert declared["instruction"] == "applace env set env-app TEST_API_URL"
    assert "value" not in declared

    detail = call(server, "get_app", app="env-app")
    assert detail["env_missing"] == ["TEST_API_URL"]

    # The human answers. The agent sees that, and still not the value.
    from applace import env

    env.set_value(paths, "env-app", "TEST_API_URL", "https://api.internal")
    after = call(server, "get_app", app="env-app")
    assert after["env"][0]["set"] is True
    assert "https://api.internal" not in json.dumps(after)


def test_set_env_warns_when_the_name_will_not_reach_the_browser(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Env App", stack="fake")
    declared = call(server, "set_env", app="env-app", name="API_URL")
    assert declared["exposed"] is False
    assert "TEST_" in declared["warning"]


def test_set_env_on_an_unknown_app_or_a_bad_name_is_a_readable_result(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    assert call(server, "set_env", app="nope", name="TEST_X")["code"] == "unknown-app"
    call(server, "create_app", name="Env App", stack="fake")
    assert call(server, "set_env", app="env-app", name="nope!")["code"] == "invalid-name"


def test_a_refused_dependency_comes_back_as_a_red_write_an_agent_can_act_on(
    home: Home,
) -> None:
    paths, _ = home
    paths.policy.write_text("dependencies:\n  deny: ['left-pad']\n", encoding="utf-8")
    server = build_server(paths)
    call(server, "create_app", name="Policy App", stack="fake")

    result = call(
        server,
        "write_files",
        app="policy-app",
        files={"manifest.json": '{"dependencies": {"left-pad": "^1.0.0"}}'},
    )
    assert result["ok"] is False
    assert result["stage"] == "policy"
    assert result["refused_deps"][0]["name"] == "left-pad"
    assert "denylist" in result["errors"][0]["message"]


def test_list_stacks_describes_where_to_start(home: Home) -> None:
    paths, _ = home
    result = call(build_server(paths), "list_stacks")
    by_name = {s["name"]: s for s in result["stacks"]}
    assert by_name["vite-react-ts"]["entry"] == "src/App.tsx"
    assert by_name["vite-react-ts"]["env_prefix"] == "VITE_"
    assert "fake" in by_name


def test_create_app_returns_the_slug_to_use_everywhere_else(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    created = call(server, "create_app", name="Team Dashboard", stack="fake")

    assert created["ok"] is True
    assert created["app"] == "team-dashboard"
    assert created["entry"] == "src/main.txt"
    assert len(created["commit"]) == 40
    assert "src/main.txt" in created["files"]


def test_create_app_reports_a_bad_name_as_a_result_not_an_exception(home: Home) -> None:
    """An agent has to be able to read the failure and try again."""
    paths, _ = home
    result = call(build_server(paths), "create_app", name="!!!", stack="fake")
    assert result["ok"] is False
    assert result["code"] == "invalid-name"


def test_create_app_reports_an_unknown_stack(home: Home) -> None:
    paths, _ = home
    result = call(build_server(paths), "create_app", name="App", stack="nope")
    assert result["ok"] is False
    assert result["code"] == "unknown-stack"
    assert "vite-react-ts" in result["error"]


def test_create_app_reports_a_name_already_taken(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Team Dashboard", stack="fake")
    again = call(server, "create_app", name="Team Dashboard", stack="fake")
    assert again["ok"] is False
    assert again["code"] == "app-exists"


def test_list_apps_and_get_app_agree_about_state(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Team Dashboard", stack="fake")

    listed = call(server, "list_apps")["apps"]
    assert [a["app"] for a in listed] == ["team-dashboard"]
    assert listed[0]["dirty"] is False

    detail = call(server, "get_app", app="team-dashboard")
    assert detail["ok"] is True
    assert detail["commit"] == listed[0]["commit"]
    assert set(detail["files"]) == {".gitignore", "manifest.json", "src/main.txt"}

    (paths.app("team-dashboard") / "manifest.json").write_text("{}", encoding="utf-8")
    after = call(server, "get_app", app="team-dashboard")
    assert after["dirty"] is True
    assert after["uncommitted"] == [" M manifest.json"]


def test_get_app_on_an_unknown_app_is_a_readable_result(home: Home) -> None:
    paths, _ = home
    result = call(build_server(paths), "get_app", app="nope")
    assert result["ok"] is False
    assert result["code"] == "unknown-app"


def test_list_apps_on_an_empty_home(home: Home) -> None:
    paths, _ = home
    assert call(build_server(paths), "list_apps") == {"ok": True, "apps": []}


def test_read_files_returns_source_and_names_what_it_could_not_read(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Team Dashboard", stack="fake")

    result = call(
        server, "read_files", app="team-dashboard", paths=["src/main.txt", ".git/config"]
    )
    assert "Team Dashboard" in result["files"][0]["content"]
    assert "Applace manages" in result["files"][1]["error"]


def test_write_files_builds_commits_and_says_what_it_committed(home: Home) -> None:
    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Team Dashboard", stack="fake")

    result = call(
        server,
        "write_files",
        app="team-dashboard",
        files={"src/page.txt": "a page\n"},
        message="Add a page",
    )
    assert result["ok"] is True
    assert result["written"] == ["src/page.txt"]
    assert result["committed"] is True
    assert call(server, "get_app", app="team-dashboard")["commit"] == result["commit"]


def test_write_files_on_an_unknown_app_is_a_readable_result(home: Home) -> None:
    paths, _ = home
    result = call(build_server(paths), "write_files", app="nope", files={"a.txt": "x"})
    assert result["ok"] is False
    assert result["code"] == "unknown-app"


def test_an_agent_sees_the_repository_it_never_asked_for(
    home: Home, fake_github: FakeGitHub
) -> None:
    """The agent chooses nothing about GitHub, and is told everything (D2b)."""
    paths, _ = home
    connected(paths, fake_github)
    server = build_server(paths)

    created = call(server, "create_app", name="Team Dashboard", stack="fake")
    assert created["github_url"] == "https://github.test/acme/team-dashboard"
    assert created["pushed"] is True

    written = call(
        server, "write_files", app="team-dashboard", files={"src/page.txt": "a page\n"}
    )
    assert written["github"]["pushed"] is True
    assert written["github"]["repo"] == "acme/team-dashboard"

    detail = call(server, "get_app", app="team-dashboard")
    assert detail["repo"] == "acme/team-dashboard"
    assert detail["unpushed"] == 0


def test_screenshot_app_sends_back_the_picture_and_the_report(
    home: Home, monkeypatch: Any
) -> None:
    """The image has to arrive as an image, with the JSON alongside it.

    The browser is faked here on purpose: what is under test is the shape of the
    MCP result, not Chromium. test_eyes.py drives the real thing.
    """
    import applace.server as server_module
    from applace.eyes import ConsoleMessage, Shot

    paths, _ = home
    server = build_server(paths)
    call(server, "create_app", name="Team Dashboard", stack="fake")

    picture = Shot(
        url="http://127.0.0.1:5180/",
        title="Team Dashboard",
        png=b"\x89PNG-not-really",
        console=[ConsoleMessage(level="error", text="TypeError: boom")],
        text_length=0,
    )
    monkeypatch.setattr(
        server_module, "screenshot_for", lambda *a, **k: (picture, True)
    )

    result = asyncio.run(server.call_tool("screenshot_app", {"app": "team-dashboard"}))
    assert isinstance(result, CallToolResult), result
    assert not result.is_error, result.content
    image, text = result.content
    assert isinstance(image, ImageContent), image
    assert isinstance(text, TextContent), text
    assert image.mime_type == "image/png"
    report = json.loads(text.text)
    assert report["ok"] is True
    assert report["started_preview"] is True
    assert report["blank"] is True
    assert report["errors"][0]["text"] == "TypeError: boom"


def test_screenshot_app_on_an_unknown_app_is_a_readable_result(home: Home) -> None:
    paths, _ = home
    result = call(build_server(paths), "screenshot_app", app="nope")
    assert result["ok"] is False
    assert result["code"] == "unknown-app"
