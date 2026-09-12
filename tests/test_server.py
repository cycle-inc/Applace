"""The MCP server: the surface an agent actually sees.

Exercised through `MCPServer.list_tools` / `call_tool` rather than by calling
the underlying functions, so the tool schemas, the descriptions and the
structured results are all part of what these tests check.
"""

from __future__ import annotations

import asyncio
from typing import Any

from conftest import call_tool
from mcp.server.mcpserver import MCPServer

from applace.db import Connection
from applace.paths import ApplacePaths
from applace.server import build_server

Home = tuple[ApplacePaths, Connection]

TOOLS = {
    "list_stacks",
    "create_app",
    "list_apps",
    "get_app",
    "read_files",
    "write_files",
    "start_preview",
    "stop_preview",
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
