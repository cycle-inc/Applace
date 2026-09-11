"""The Applace MCP server.

What an agent talks to. Every tool returns structured JSON and every docstring
is written for a model to read, because the docstrings *are* the interface.

Each call opens its own SQLite connection: MCP hosts call tools concurrently and
sqlite3 connections are not shared across threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from mcp.server.mcpserver import MCPServer

from .apps import AppError, app_detail, app_summary, create_app, require_app
from .db import Connection, connect
from .db import list_apps as db_list_apps
from .naming import InvalidName
from .paths import ApplacePaths, paths as default_paths
from .stacks import StackError, registry

SERVER_NAME = "applace"

INSTRUCTIONS = """\
Applace builds and hosts web apps. An app here is an ordinary git repository
that you never touch directly: ask for it and Applace renders it, installs it,
type-checks it, builds it and commits it. Call list_stacks to see what an app
can be made of, create_app to make one, get_app to read its state and file
tree. Nothing you create is thrown away -- every green change is a commit.
"""


def build_server(paths: ApplacePaths | None = None) -> MCPServer:
    """Create the MCP server. ``paths`` is injectable so tests can isolate it."""
    home = paths or default_paths()
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    @contextmanager
    def session() -> Iterator[Connection]:
        conn = connect(home.db)
        try:
            yield conn
        finally:
            conn.close()

    @server.tool()
    def list_stacks() -> dict[str, Any]:
        """See what an app can be made of before making one.

        A stack decides the framework, the build, and what the app is allowed to
        talk to. Company stacks come with a design system and an API client
        already wired in, so prefer one of those over the generic stack when the
        human is building something internal.

        `entry` is the file to start reading and editing. `env_prefix` is what a
        variable name must start with to reach the browser.
        """
        try:
            available = registry(home.stacks)
        except StackError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "stacks": [stack.summary() for stack in available.values()],
        }

    # Deliberately sync: this shells out to npm for minutes. The SDK runs sync
    # tools on a worker thread, which is what keeps the server answering other
    # calls while an install is going.
    @server.tool()
    def create_app(
        name: str, stack: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Create a new app from a stack, installed and committed.

        `name` is what a human would call it ("Team Dashboard"); the slug used
        everywhere afterwards is derived from it and comes back as `app`. Pass
        that slug to every other tool.

        This takes a minute or two -- it installs dependencies -- and when it
        returns the app already builds. Read `entry` and start there.

        On failure `code` is invalid-name, app-exists or unknown-stack.
        """
        with session() as conn:
            try:
                report = _create(home, conn, name, stack, description)
            except InvalidName as exc:
                return {"ok": False, "code": "invalid-name", "error": str(exc)}
            except StackError as exc:
                return {"ok": False, "code": "unknown-stack", "error": str(exc)}
            except AppError as exc:
                return {"ok": False, "code": "app-exists", "error": str(exc)}
            return {"ok": True, **report.as_dict()}

    @server.tool()
    def list_apps() -> dict[str, Any]:
        """Every app on this machine, with its state.

        `dirty` means the working tree holds changes that are not in a commit --
        that is, work that was written but did not pass the build. `commit` is
        the last change Applace committed.
        """
        with session() as conn:
            return {
                "ok": True,
                "apps": [app_summary(conn, row) for row in db_list_apps(conn)],
            }

    @server.tool()
    def get_app(app: str) -> dict[str, Any]:
        """Everything about one app: its state, its file tree, where to start.

        Call this before writing any code for an app you did not just create.
        `files` is the repository's tracked files -- dependencies and build
        output are excluded, so this is the app itself and nothing else.

        On failure `code` is unknown-app.
        """
        with session() as conn:
            try:
                row = require_app(conn, app)
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            return {"ok": True, **app_detail(home, conn, row)}

    return server


def _create(
    home: ApplacePaths,
    conn: Connection,
    name: str,
    stack: str | None,
    description: str | None,
) -> Any:
    """Named so the tool above can shadow ``create_app`` without recursing."""
    return create_app(
        home, conn, name=name, stack_name=stack, description=description
    )


def serve(
    paths: ApplacePaths | None = None,
    *,
    port: int | None = None,
    host: str = "127.0.0.1",
) -> None:
    """Run the server on stdio, or over streamable HTTP when a port is given.

    ``host`` stays on loopback unless you say otherwise. A container cannot
    reach loopback on its host, so a Dockerised MCP client needs ``0.0.0.0``.
    """
    server = build_server(paths)
    if port is None:
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", host=host, port=port)
