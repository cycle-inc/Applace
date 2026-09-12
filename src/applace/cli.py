"""The ``applace`` command line.

``init`` sets a home up, ``serve`` exposes it to an MCP host, and everything
else is what an agent can do, available to a human at a terminal: ``new``,
``ls``, ``stacks``, ``rm``. The two surfaces stay deliberately in step -- a
human debugging what an agent did should not have to use different words.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Annotated

import typer

from . import gate
from .apps import (
    AppError,
    app_detail,
    create_app,
    remove_app,
    require_app,
    screenshot,
    start_preview,
    stop_preview,
)
from .db import connect, list_apps
from .eyes import DEFAULT_HEIGHT, DEFAULT_WIDTH, EyesUnavailable, Shot
from .gate import GateReport
from .github import DEFAULT_HOST, DEFAULT_VISIBILITY, VISIBILITIES, GitHubError, GitHubLink
from .github import api as github_api
from .github import load as github_load
from .github import save as github_save
from .github import token as github_token
from .init_cmd import InitReport, run_init
from .naming import InvalidName
from .paths import ApplacePaths, paths as applace_paths
from .preview import PreviewError
from .server import serve as serve_server
from .stacks import StackError, registry
from .sync import adopt_app, push_app

app = typer.Typer(
    add_completion=False,
    help="Build, preview and ship web apps from any LLM agent.",
    no_args_is_help=True,
)


def _print_version(show: bool) -> None:
    """The first thing any bug report needs, and the first thing people try."""
    if show:
        typer.echo(f"applace {metadata.version('applace')}")
        raise typer.Exit()


@app.callback()
def _root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Show the version."
        ),
    ] = False,
) -> None:
    pass


def _home() -> ApplacePaths:
    return applace_paths()


def _require_home(paths: ApplacePaths) -> None:
    if not paths.db.exists():
        typer.secho(
            f"No Applace home at {paths.home}. Run `applace init` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def init() -> None:
    """Create ~/.applace and check this machine can build apps."""
    paths = _home()
    report = run_init(paths)
    typer.echo(format_init(report))
    if not report.ready:
        raise typer.Exit(code=1)


def format_init(report: InitReport) -> str:
    lines = [
        f"{'Created' if report.created else 'Updated'} {report.home}",
        "",
        "Stacks:",
    ]
    lines += [f"  {name:<20} {title}" for name, title in report.stacks] or [
        "  (none — this is a broken install)"
    ]
    lines += ["", "Requirements:"]
    for requirement in report.requirements:
        mark = "ok " if requirement.present else "MISSING"
        where = requirement.path or requirement.detail
        lines.append(f"  {mark:<8} {requirement.name:<6} {where}")
    if report.optional:
        lines += ["", "Optional:"]
        for requirement in report.optional:
            mark = "ok " if requirement.present else "absent"
            where = requirement.path or requirement.detail
            lines.append(f"  {mark:<8} {requirement.name:<6} {where}")
    if report.error:
        lines += ["", report.error]
    if report.missing:
        names = ", ".join(r.name for r in report.missing)
        lines += ["", f"Install {names} before creating an app."]
    return "\n".join(lines)


@app.command()
def new(
    name: Annotated[str, typer.Argument(help="What the app is called, as a human says it.")],
    stack: Annotated[
        str | None, typer.Option("--stack", "-s", help="Which stack to build from.")
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="One line about the app.")
    ] = None,
    install: Annotated[
        bool,
        typer.Option(
            "--install/--no-install",
            help="Install dependencies. Skipping is for tests and offline work.",
        ),
    ] = True,
) -> None:
    """Create an app from a stack, installed and committed."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        report = create_app(
            paths,
            conn,
            name=name,
            stack_name=stack,
            description=description,
            install=install,
        )
    except (AppError, InvalidName, StackError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"{report.slug}  {report.path}")
    typer.echo(f"  stack   {report.stack}")
    typer.echo(f"  commit  {report.commit[:12]}  ({len(report.files)} files)")
    if report.entry:
        typer.echo(f"  start   {report.entry}")
    if report.github_url:
        state = "pushed" if report.pushed else "not pushed yet"
        typer.echo(f"  github  {report.github_url}  ({state})")
    for warning in report.warnings:
        typer.secho(f"\n{warning}", fg=typer.colors.YELLOW, err=True)


@app.command("ls")
def list_command() -> None:
    """The apps on this machine, and whether they are committed."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        rows = list_apps(conn)
        if not rows:
            typer.echo("No apps yet. `applace new \"My App\"` makes one.")
            return
        for row in rows:
            detail = app_detail(paths, conn, row)
            state = (
                "missing"
                if detail.get("missing")
                else ("dirty" if detail.get("dirty") else "clean")
            )
            commit = (detail.get("commit") or "")[:12] or "-"
            live = detail.get("preview")
            where = f"  {live['url']}" if live else ""
            typer.echo(
                f"{str(row['slug']):<24} {str(row['stack']):<16} {state:<8} {commit}{where}"
            )
    finally:
        conn.close()


@app.command()
def dev(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    port: Annotated[
        int | None, typer.Option("--port", "-p", help="Use this port instead of a free one.")
    ] = None,
) -> None:
    """Run the app's dev server in the background and print its URL.

    It outlives this command: stop it with `applace stop`.
    """
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        running = start_preview(paths, conn, name, port=port)
    except (AppError, StackError, PreviewError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(running.url)
    typer.echo(f"  pid  {running.pid}")
    typer.echo(f"  log  {running.log}")


@app.command()
def stop(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
) -> None:
    """Stop the app's dev server."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        stopped = stop_preview(conn, name)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"Stopped {name}" if stopped else f"{name} was not running")


@app.command()
def shot(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    route: Annotated[
        str, typer.Option("--route", "-r", help="Which route to look at.")
    ] = "/",
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Where to write the PNG."),
    ] = None,
    width: Annotated[int, typer.Option("--width", help="Viewport width.")] = DEFAULT_WIDTH,
    height: Annotated[int, typer.Option("--height", help="Viewport height.")] = DEFAULT_HEIGHT,
    full_page: Annotated[
        bool, typer.Option("--full-page", help="Capture below the fold too.")
    ] = False,
) -> None:
    """Open a route in a browser and report the console, the errors and the page.

    Starts the preview if it is not already running, and leaves it running.
    """
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        destination = out or paths.app_logs(str(row["slug"])) / "shot.png"
        picture, started = screenshot(
            paths, conn, name, route=route, width=width, height=height, full_page=full_page
        )
    except (AppError, StackError, PreviewError, EyesUnavailable) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(picture.png)
    typer.echo(format_shot(picture, destination, started=started))
    if picture.errors or picture.blank:
        raise typer.Exit(code=1)


def format_shot(picture: Shot, destination: Path, *, started: bool) -> str:
    lines = [picture.url, f"  title  {picture.title or '(none)'}", f"  png    {destination}"]
    if started:
        lines.append("  note   started the preview; `applace stop` when you are done")
    if picture.blank:
        lines.append("  BLANK  the page rendered no text at all")
    for message in picture.console:
        mark = {"error": "ERROR", "warning": "warn "}.get(message.level, "info ")
        lines.append(f"  {mark}  {message.text}")
    for request in picture.failed_requests:
        lines.append(f"  FAILED {request.method} {request.url} — {request.error}")
    if not picture.console and not picture.failed_requests and not picture.blank:
        lines.append("  clean  nothing in the console, nothing failed")
    return "\n".join(lines)


@app.command()
def check(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    message: Annotated[
        str | None,
        typer.Option("--message", "-m", help="Commit subject, if this passes."),
    ] = None,
) -> None:
    """Take an app through the gate: typecheck, lint, build, and commit if green.

    The same pipeline an agent's write goes through, so a human can reproduce
    exactly what the agent was told.
    """
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        report = gate.write_files(paths, conn, app=name, message=message)
    except (AppError, StackError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_gate(report))
    if not report.ok:
        raise typer.Exit(code=1)


def format_gate(report: GateReport) -> str:
    lines: list[str] = []
    for stage in report.stages:
        if stage.skipped:
            lines.append(f"  skip  {stage.name:<10} ({stage.reason})")
        else:
            mark = "ok  " if stage.ok else "FAIL"
            lines.append(f"  {mark}  {stage.name:<10} {stage.duration_ms} ms")
    if report.new_deps:
        names = ", ".join(f"{d['name']}@{d['version']}" for d in report.new_deps)
        lines += ["", f"New dependencies: {names}"]
    if report.ok:
        tail = (
            f"green — committed {report.commit[:12]}"
            if report.committed and report.commit
            else "green — nothing had changed, so there is nothing to commit"
        )
        lines += ["", tail]
        return "\n".join(lines)
    lines += ["", f"red at {report.stage} — nothing was committed"]
    for error in report.errors:
        where = error.file or ""
        if error.line is not None:
            where = f"{where}:{error.line}:{error.column}"
        code = f" [{error.code}]" if error.code else ""
        lines.append(f"  {where}{code} {error.message}" if where else f"  {error.message}{code}")
    return "\n".join(lines)


github_app = typer.Typer(
    add_completion=False,
    help="The GitHub connection every app follows (D2b).",
    no_args_is_help=True,
)
app.add_typer(github_app, name="github")


@github_app.command("connect")
def github_connect(
    org: Annotated[str, typer.Option("--org", help="The organisation every app lands in.")],
    visibility: Annotated[
        str, typer.Option("--visibility", help="private, internal or public.")
    ] = DEFAULT_VISIBILITY,
    prefix: Annotated[
        str, typer.Option("--prefix", help="Prepended to every repository name.")
    ] = "",
    team: Annotated[
        str | None, typer.Option("--team", help="Team slug to grant write access.")
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="GitHub Enterprise host, if not github.com.")
    ] = DEFAULT_HOST,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Which `gh` account to take the token from."),
    ] = None,
    api_base: Annotated[
        str | None,
        typer.Option(
            "--api-base",
            help="Override the REST API root. For an enterprise install that is "
            "not at /api/v3, and for testing against a stand-in.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
) -> None:
    """Answer once where code goes. Every app created afterwards follows it.

    Apps that already exist keep the remote they have: changing this never
    rewrites a repository behind a developer's back (D2b).
    """
    paths = _home()
    _require_home(paths)
    if visibility not in VISIBILITIES:
        typer.secho(
            f"--visibility must be one of {', '.join(VISIBILITIES)}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    # D6: making code visible is the gated act. Here it is gated once, at the
    # only moment a human is definitely present.
    if visibility == "public" and not yes:
        typer.confirm(
            f"Every app Applace creates in {org} will be a PUBLIC repository, "
            f"readable by anyone on the internet. Continue?",
            abort=True,
            default=False,
        )

    link = GitHubLink(
        org=org,
        visibility=visibility,
        prefix=prefix,
        team=team,
        host=host,
        user=user,
        api_base=api_base,
    )
    github_save(paths, link)
    typer.echo(f"Connected {org} on {host} ({visibility})")
    if prefix:
        typer.echo(f"  repositories will be named {prefix}<slug>")
    typer.echo(_reachability(link))


@github_app.command("status")
def github_status() -> None:
    """What Applace would do with the next app, and whether it can."""
    paths = _home()
    _require_home(paths)
    link = github_load(paths)
    if link is None:
        typer.echo(
            "No organisation connected. `applace github connect --org <org>` "
            "makes every new app a repository."
        )
        raise typer.Exit(code=1)
    typer.echo(f"{link.org} on {link.host} ({link.visibility})")
    typer.echo(f"  api     {link.api}")
    if link.prefix:
        typer.echo(f"  prefix  {link.prefix}")
    if link.team:
        typer.echo(f"  team    {link.team}")
    typer.echo(f"  {_reachability(link)}")


def _reachability(link: GitHubLink) -> str:
    """Say whether the token exists and the organisation answers, without
    printing the token or failing the command over it."""
    try:
        auth = github_token(link)
    except GitHubError as exc:
        return str(exc)
    status, body = github_api(link, "GET", f"/orgs/{link.org}", auth=auth)
    if status == 200:
        return f"token ok, {link.org} is reachable"
    if status == 404:
        return f"the token works but cannot see an organisation called {link.org}"
    return f"GitHub answered {status}: {body.get('message', 'no detail')}"


@github_app.command("adopt")
def github_adopt(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    repo: Annotated[str, typer.Option("--repo", help="owner/name of the repository.")],
) -> None:
    """Point an existing app at a repository that already exists."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        repository = adopt_app(paths, conn, row, repo)
    except (AppError, GitHubError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"{name} -> {repository.html_url}")
    typer.echo(f"  `applace push {name}` sends its history there")


@app.command()
def push(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
) -> None:
    """Push the app's commits to GitHub. Never forced (D13)."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        report = push_app(paths, conn, row)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if report.pushed:
        typer.echo(f"Pushed {report.commit[:12] if report.commit else ''} to {report.github_url}")
        return
    typer.secho(report.reason, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def stacks() -> None:
    """The stacks an app can be built from."""
    paths = _home()
    try:
        available = registry(paths.stacks)
    except StackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for stack in available.values():
        source = "" if stack.source == "builtin" else f"  ({stack.source})"
        typer.echo(f"{stack.name:<20} {stack.title}{source}")
        typer.echo(f"{'':<20} {stack.description}")


@app.command("rm")
def remove(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    delete: Annotated[
        bool,
        typer.Option(
            "--delete/--keep",
            help="Also delete the repository. --keep only makes Applace forget it.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
) -> None:
    """Forget an app, and optionally delete its repository."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        path = str(row["path"])
        if delete and not yes:
            typer.confirm(
                f"Delete {path} and everything in it?", abort=True, default=False
            )
        remove_app(paths, conn, name, delete_files=delete)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"Removed {name}" + ("" if delete else f" (kept {path})"))


@app.command()
def serve(
    http: Annotated[
        int | None,
        typer.Option("--http", help="Serve over streamable HTTP on this port."),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Interface to bind when serving over HTTP.")
    ] = "127.0.0.1",
) -> None:
    """Expose this home to an MCP host. Stdio unless --http is given."""
    paths = _home()
    _require_home(paths)
    serve_server(paths, port=http, host=host)


def main() -> None:
    app()
