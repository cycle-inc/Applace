"""The Applace MCP server.

What an agent talks to. Every tool returns structured JSON and every docstring
is written for a model to read, because the docstrings *are* the interface.

Each call opens its own SQLite connection: MCP hosts call tools concurrently and
sqlite3 connections are not shared across threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from mcp.server.mcpserver import Image, MCPServer

from . import gate, panel, skill
from .apps import AppError, app_detail, app_summary, create_app, require_app
from .apps import declare_env as declare_env_for
from .apps import deploy_app as deploy_app_for
from .apps import rollback_app as rollback_app_for
from .apps import screenshot as screenshot_for
from .apps import start_preview as start_preview_for
from .apps import stop_preview as stop_preview_for
from .db import Connection, connect
from .db import list_apps as db_list_apps
from .deploy import LOCAL, PREVIEW, DeployError, DeployRefused
from .env import EnvError
from .eyes import DEFAULT_HEIGHT, DEFAULT_WIDTH, EyesUnavailable
from .gitrepo import GitError
from .preview import PreviewError
from .naming import InvalidName
from .paths import ApplacePaths, paths as default_paths
from .stacks import StackError, registry

SERVER_NAME = "applace"

INSTRUCTIONS = """\
Applace builds and hosts web apps. An app here is an ordinary git repository
that you never touch directly: ask for it and Applace renders it, installs it,
type-checks it, builds it and commits it. Call list_stacks to see what an app
can be made of, create_app to make one, get_app to read its state and file
tree, read_files and write_files to change it.

write_files is a compiler: every write is type-checked, linted and built before
it is kept, and you get the real tool's errors back with file and line when it
is not. start_preview gives a human a URL to watch while you work, and
screenshot_app tells you what the page actually did. Nothing you create is
thrown away -- every green change is a commit, and when the machine has a GitHub
organisation connected, every commit is pushed to the app's own repository.

deploy_app ships a commit -- to this machine, or to Vercel through the app's own
repository. A production deploy needs a human to confirm it; that is a rule, not
a setting, and rollback_app puts the previous commit back when one goes wrong.

Call get_skill first. It is short, it says how to use the rest of these tools,
and it says what this particular machine is set up to do.
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
    def get_skill() -> dict[str, Any]:
        """How to build an app here. Call this before anything else.

        Returns `skill`, a short document written for you: the loop to follow,
        the rules that save a round trip, what the stack already contains, and
        how secrets work. Then it says what *this* machine does -- which stacks
        are installed, which GitHub organisation every app lands in, and which
        dependencies the policy allows.

        Read it once per session. It is cheaper than a red build.
        """
        return {"ok": True, **skill.describe(home)}

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

        If this machine has a GitHub organisation connected, the app is also a
        repository there from birth: `github_url` is where a human can see it.
        Anything that went wrong on the way is in `warnings`, and none of it
        means the app failed.

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

        `human` is what somebody did here that Applace did not: commits they
        made by hand, and files they have open right now. Read those files
        before you touch them; `write_files` will refuse to overwrite them.

        On failure `code` is unknown-app.
        """
        with session() as conn:
            try:
                row = require_app(conn, app)
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            return {"ok": True, **app_detail(home, conn, row)}

    # Sync: starting a dev server waits for it to answer, which is seconds.
    @server.tool()
    def start_preview(app: str) -> dict[str, Any]:
        """Run the app so a human can look at it, and get back a URL.

        Idempotent: calling it again while the app is already previewing hands
        back the same URL rather than starting a second server. The server keeps
        running between your tool calls and reloads by itself when you write, so
        start it once and leave it.

        On failure `code` is unknown-app or preview-failed; `error` then carries
        the end of the dev server's own log.
        """
        with session() as conn:
            try:
                running = start_preview_for(home, conn, app)
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            except StackError as exc:
                return {"ok": False, "code": "unknown-stack", "error": str(exc)}
            except PreviewError as exc:
                return {"ok": False, "code": "preview-failed", "error": str(exc)}
            return {"ok": True, **running.as_dict()}

    @server.tool()
    def stop_preview(app: str) -> dict[str, Any]:
        """Stop the app's dev server and give the port back.

        `stopped` is false when nothing was running, which is not an error.
        """
        with session() as conn:
            try:
                stopped = stop_preview_for(conn, app)
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            return {"ok": True, "app": app, "stopped": stopped}

    # Sync: this drives a browser, which is seconds.
    @server.tool()
    def screenshot_app(
        app: str,
        route: str = "/",
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        full_page: bool = False,
    ) -> Any:
        """Look at a route of the app: the picture, the console, and what failed.

        Use this after a green write, before telling anyone the app is done. A
        build that passes and a page that renders are two different facts.

        You get back the screenshot *and* a JSON report. Read the report first:
        `blank` true means the page painted nothing, `errors` is what the
        browser's console said, and `failed_requests` is every request that
        404'd or never completed -- that is usually an API path or an asset that
        does not exist. A white page with a `TypeError` in `errors` is a fixable
        bug; the picture alone would only tell you it is white.

        Starts the app's preview if it is not already running, and leaves it
        running. On failure `code` is unknown-app, preview-failed or
        eyes-unavailable.
        """
        with session() as conn:
            try:
                shot, started = screenshot_for(
                    home, conn, app,
                    route=route, width=width, height=height, full_page=full_page,
                )
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            except PreviewError as exc:
                return {"ok": False, "code": "preview-failed", "error": str(exc)}
            except EyesUnavailable as exc:
                return {"ok": False, "code": "eyes-unavailable", "error": str(exc)}
        # A list, not a dict: the image has to reach the model as an image, and
        # that costs the structured-content half of the result. The JSON goes
        # alongside it as text, which is what every host renders anyway.
        return [
            Image(data=shot.png, format="png"),
            {"ok": True, "app": app, "started_preview": started, **shot.report()},
        ]

    @server.tool()
    def read_files(app: str, paths: list[str]) -> dict[str, Any]:
        """Read the app's source, by repository-relative path.

        Ask for the files you are about to change, all in one call. Each entry
        comes back with `content`, or with `error` when that one path could not
        be read -- the rest of the batch still arrives.

        On failure `code` is unknown-app.
        """
        with session() as conn:
            try:
                return {"ok": True, **gate.read(home, conn, app=app, paths_to_read=paths)}
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}

    # Sync for the same reason create_app is: this runs a build.
    @server.tool()
    def write_files(
        app: str,
        files: dict[str, str] | None = None,
        delete: list[str] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Write source into an app, then type-check, lint and build it.

        This is the only way to change an app, and it is a compiler, not a file
        writer. Pass whole files in `files` as {path: content} -- a path that
        does not exist yet is created. `message` is the commit subject.

        When `ok` is true the app built and the change is committed; `commit` is
        the new sha. When `ok` is false nothing was committed, your files are
        still on disk, and `stage` says which step failed -- read `errors`, each
        of which carries the file, line and column the real tool reported, fix
        them, and call this again.

        `new_deps` lists dependencies your change added: adding one costs an
        install, so prefer what the stack already has.

        `stage: "handover"` means a human has uncommitted changes in a file you
        tried to write. Nothing was written. Tell them which file you need and
        let them commit or discard -- that decision is not yours to make.

        When the app has a GitHub repository, `github` says whether the commit
        reached it. `github.diverged` true means someone else pushed and Applace
        refused rather than overwrite them -- say so to the human, do not try to
        work around it. On a machine that reviews, `github.pull_request_url` is
        the pull request holding your work; give it to the human.

        Call this with no `files` to re-check an app that is already dirty, or to
        retry a push that was refused.

        On failure `code` is unknown-app or unknown-stack.
        """
        with session() as conn:
            try:
                report = gate.write_files(
                    home,
                    conn,
                    app=app,
                    files_to_write=files,
                    delete=delete,
                    message=message,
                )
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            except StackError as exc:
                return {"ok": False, "code": "unknown-stack", "error": str(exc)}
            except GitError as exc:
                return {"ok": False, "code": "git-failed", "error": str(exc)}
            return report.as_dict()

    @server.tool()
    def set_env(app: str, name: str, description: str | None = None) -> dict[str, Any]:
        """Declare a configuration value or secret the app needs, by name.

        You declare names; a human supplies the values. There is no argument
        for the value here and no tool that returns one -- a secret must never
        enter your context, and anything a browser bundle holds is public
        anyway.

        The response carries `instruction`: the exact command for the human to
        run. Relay it to them and carry on. The value reaches the dev server
        and the build without passing through you.

        `exposed` false means the name does not carry the stack's prefix, so
        the bundler will not give it to the browser -- read `warning` and
        rename it if the page itself needs to read it.

        On failure `code` is unknown-app or invalid-name.
        """
        with session() as conn:
            try:
                return {
                    "ok": True,
                    **declare_env_for(home, conn, app, name=name, description=description),
                }
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            except EnvError as exc:
                return {"ok": False, "code": "invalid-name", "error": str(exc)}

    # Sync: a deploy builds, and a provider build is minutes.
    @server.tool()
    def deploy_app(
        app: str,
        target: str = LOCAL,
        environment: str = PREVIEW,
        commit: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Build the app's current commit and put it somewhere a human can open.

        `target` is "local" (served from this machine, needs no account) or
        "vercel". `environment` is "preview" or "production". Start with a local
        preview deploy: it runs the real production build, so it catches what
        the dev server hides.

        A **production** deploy is not yours to make. It needs `confirm` true,
        and `confirm` means a human said yes out loud -- you cannot decide that
        for them. Ask, and relay what they answer. If the machine's policy
        forbids production deploys, `code` is policy and that is final.

        What goes live is a commit, never your uncommitted work. `commit` names
        an older one if you are putting something back. When it returns, `url`
        is the address and `status` is live or failed; a failed deploy leaves
        `detail.log`, which is the build's own output.

        On failure `code` is unknown-app, unknown-stack, confirm-required,
        policy, unsupported-target, not-pushed, or deploy-failed.
        """
        with session() as conn:
            try:
                report = deploy_app_for(
                    home, conn, app,
                    target=target, environment=environment,
                    commit=commit, confirm=confirm,
                )
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            except StackError as exc:
                return {"ok": False, "code": "unknown-stack", "error": str(exc)}
            except DeployRefused as exc:
                return exc.as_dict()
            except DeployError as exc:
                return {"ok": False, "code": "deploy-failed", "error": str(exc)}
            return report.as_dict()

    @server.tool()
    def rollback_app(
        app: str, commit: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Put back a commit that worked, to wherever the app is deployed.

        With no `commit`, this is the one that was live before the current one.
        Use it the moment a human says the deployed app is broken: it is faster
        and safer than fixing forward, and the broken commit is still in git for
        you to work on afterwards.

        Rolling back production is still a production deploy, so it still needs
        `confirm` from a human.

        On failure `code` is unknown-app, never-deployed, no-earlier-deployment,
        unknown-commit, confirm-required, policy or deploy-failed.
        """
        with session() as conn:
            try:
                report = rollback_app_for(home, conn, app, commit=commit, confirm=confirm)
            except AppError as exc:
                return {"ok": False, "code": "unknown-app", "error": str(exc)}
            except StackError as exc:
                return {"ok": False, "code": "unknown-stack", "error": str(exc)}
            except DeployRefused as exc:
                return exc.as_dict()
            except DeployError as exc:
                return {"ok": False, "code": "deploy-failed", "error": str(exc)}
            return report.as_dict()

    # The chat window's half of the same server (D16). Read-only, and inert on
    # stdio -- an HTTP route nobody can reach costs nothing.
    panel.attach(server, home)
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
