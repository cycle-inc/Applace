"""The Applace MCP server.

What an agent talks to. Every tool returns structured JSON and every docstring
is written for a model to read, because the docstrings *are* the interface.

Behind them there is no second implementation: each tool calls the method of the
same name on :class:`applace.api.Applace` (D17), so the agent's door and the
developer's door cannot answer the same question differently. What lives here is
what is genuinely the server's own -- the wording an agent reads, the argument
names it sees, and the one tool whose result is a picture as well as a report.

Each call opens its own SQLite connection: MCP hosts call tools concurrently and
sqlite3 connections are not shared across threads.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import panel
from .api import Applace
from .deploy import LOCAL, PREVIEW
from .eyes import DEFAULT_HEIGHT, DEFAULT_WIDTH
from .paths import ApplacePaths

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
    applace = Applace(paths)
    home = applace.paths
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

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
        return applace.skill()

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
        return applace.stacks()

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
        return applace.create(name, stack=stack, description=description)

    @server.tool()
    def list_apps() -> dict[str, Any]:
        """Every app on this machine, with its state.

        `dirty` means the working tree holds changes that are not in a commit --
        that is, work that was written but did not pass the build. `commit` is
        the last change Applace committed.
        """
        return applace.apps()

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
        return applace.app(app)

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
        return applace.preview(app)

    @server.tool()
    def stop_preview(app: str) -> dict[str, Any]:
        """Stop the app's dev server and give the port back.

        `stopped` is false when nothing was running, which is not an error.
        """
        return applace.stop_preview(app)

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
        answer = applace.shot(
            app, route=route, width=width, height=height, full_page=full_page
        )
        png = answer.pop("png", None)
        if not answer.get("ok") or not isinstance(png, bytes):
            return answer
        # A list, not a dict: the image has to reach the model as an image, and
        # that costs the structured-content half of the result. The JSON goes
        # alongside it as text, which is what every host renders anyway.
        return [Image(data=png, format="png"), answer]

    @server.tool()
    def read_files(app: str, paths: list[str]) -> dict[str, Any]:
        """Read the app's source, by repository-relative path.

        Ask for the files you are about to change, all in one call. Each entry
        comes back with `content`, or with `error` when that one path could not
        be read -- the rest of the batch still arrives.

        On failure `code` is unknown-app.
        """
        return applace.read(app, paths)

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
        return applace.write(app, files=files, delete=delete, message=message)

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
        return applace.declare_env(app, name, description=description)

    @server.tool()
    def use_api(
        app: str,
        name: str,
        base_url: str,
        token_env: str | None = None,
        paths: list[str] | None = None,
        methods: list[str] | None = None,
        header: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Let the app call a company API, without the token reaching the browser.

        Anything a page downloads is readable by whoever opens it, so front-end
        code cannot hold a credential. Declare the API here instead, then write
        code that fetches the relative path in `fetch` -- Applace proxies it and
        adds the credential on the way out, in preview and in production alike.

        `name` is a short slug you choose (`crm`, `billing`). `base_url` is the
        API's root. `token_env` is the **name** of the variable holding the
        credential -- never the credential. `paths` and `methods` are the
        allowlist: `["customers/**"]` and `["GET"]` mean exactly that, and the
        proxy refuses the rest. Declare the narrowest thing that works.

        `needs` in the response is the command for the human to run so the
        credential exists on their machine. Relay it; you will never see the
        value. Calling a host you did not declare is a red gate on the next
        write.

        On failure `code` is unknown-app, invalid-api or policy.
        """
        return applace.declare_api(
            app,
            name,
            base_url,
            token_env=token_env,
            paths=paths,
            methods=methods,
            header=header,
            description=description,
        )

    @server.tool()
    def apis(app: str) -> dict[str, Any]:
        """The APIs this app may call, and the path to fetch each one under."""
        return applace.apis(app)

    @server.tool()
    def drop_api(app: str, name: str) -> dict[str, Any]:
        """Stop allowing the app to call an API it no longer uses."""
        return applace.forget_api(app, name)

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
        return applace.deploy(
            app,
            target=target,
            environment=environment,
            commit=commit,
            confirm=confirm,
        )

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
        return applace.rollback(app, commit=commit, confirm=confirm)

    # The chat window's half of the same server (D16). Read-only, and inert on
    # stdio -- an HTTP route nobody can reach costs nothing.
    panel.attach(server, home)
    return server


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
