"""The ``applace`` command line.

``init`` sets a home up, ``serve`` exposes it to an MCP host, and everything
else is what an agent can do, available to a human at a terminal: ``new``,
``ls``, ``stacks``, ``rm``. The two surfaces stay deliberately in step -- a
human debugging what an agent did should not have to use different words.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Annotated, Any

import typer

from . import deploy as deployment
from . import env as env_store
from . import apis, apps, gate, gateway, policy, skill
from .api import Applace
from .apps import (
    AppError,
    app_detail,
    create_app,
    declare_env,
    deploy_app,
    remove_app,
    require_app,
    rollback_app,
    screenshot,
    start_preview,
    stop_deployments,
    stop_preview,
)
from .db import connect, latest_deployment, list_apps
from .deploy import DeployError
from .deploy import Report as DeployReport
from .deploy import describe as describe_deployments
from .eyes import DEFAULT_HEIGHT, DEFAULT_WIDTH, EyesUnavailable, Shot
from .gate import GateReport
from .github import (
    DEFAULT_HOST,
    DEFAULT_VISIBILITY,
    DIRECT,
    REVIEWS,
    VISIBILITIES,
    GitHubError,
    GitHubLink,
)
from .github import api as github_api
from .github import load as github_load
from .github import token as github_token
from .init_cmd import InitReport, run_init
from .machine import Machine, MachineError
from .naming import InvalidName
from .paths import ApplacePaths, paths as applace_paths
from .preview import PreviewError
from .preview import tail as preview_tail
from .sensor import RouteError
from .server import serve as serve_server
from .stacks import StackError
from .stackstore import add as add_stack
from .stackstore import describe as describe_stacks
from .stackstore import drift as stack_drift
from .stackstore import remove as remove_stack
from .stackstore import update as update_stack
from .sync import adopt_app, push_app
from .vercel import VercelError, VercelLink
from .vercel import api as vercel_api
from .vercel import load as vercel_load
from .vercel import save as vercel_save
from .vercel import token as vercel_token

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
            shipped = next(
                (
                    entry
                    for entry in detail.get("deployments", [])
                    if entry["status"] == "live"
                ),
                None,
            )
            if shipped:
                where += f"  [{shipped['environment']}] {shipped['url']}"
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
    """Stop the app's dev server, and anything it is serving locally."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        stopped = stop_preview(conn, name)
        # `applace stop` means "stop this app on my machine". Leaving a local
        # deployment up because it came from a different command would be a
        # surprise, and a held port.
        served = stop_deployments(paths, conn, name)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if stopped:
        typer.echo(f"Stopped the preview of {name}")
    if served:
        typer.echo(f"Stopped the local deployment of {name}")
    if not stopped and not served:
        typer.echo(f"{name} was not running")


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


@app.command()
def deploy(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    target: Annotated[
        str, typer.Option("--target", "-t", help="local or vercel.")
    ] = deployment.LOCAL,
    production: Annotated[
        bool,
        typer.Option("--production", help="Ship it for real, not to a preview URL."),
    ] = False,
    commit: Annotated[
        str | None, typer.Option("--commit", help="Deploy this commit instead of HEAD.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
) -> None:
    """Build a commit of the app and put it somewhere it can be opened.

    `--target local` needs no account: it runs the same production build and
    serves the result from this machine. Production is the gated act (D6) and
    this command asks before doing it.
    """
    paths = _home()
    _require_home(paths)
    environment = deployment.PRODUCTION if production else deployment.PREVIEW
    if production and not yes:
        # The confirmation the agent cannot give. Asked here, before anything is
        # built, so a "no" costs nobody a minute of npm.
        typer.confirm(
            f"Deploy {name} to PRODUCTION on {target}? This is what real users "
            f"will see.",
            abort=True,
            default=False,
        )
    conn = connect(paths.db)
    try:
        report = deploy_app(
            paths, conn, name,
            target=target, environment=environment, commit=commit,
            confirm=production,
        )
    except (AppError, StackError, DeployError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_deployment(report))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def rollback(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    commit: Annotated[
        str | None,
        typer.Argument(help="The commit to put back. The previous one by default."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
) -> None:
    """Put an earlier commit back where this app is deployed."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        live = latest_deployment(conn, str(row["id"]), status="live")
        production = live is not None and str(live["environment"]) == "production"
        if production and not yes:
            typer.confirm(
                f"Roll PRODUCTION back to {commit or 'the previous commit'}?",
                abort=True,
                default=False,
            )
        report = rollback_app(paths, conn, name, commit=commit, confirm=production)
    except (AppError, StackError, DeployError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(format_deployment(report))
    if not report.ok:
        raise typer.Exit(code=1)


def format_deployment(report: DeployReport) -> str:
    lines = [
        f"{report.app} → {report.target} ({report.environment}) "
        f"from {report.commit[:12]}"
    ]
    if report.ok:
        lines.append(f"  {report.url}")
    else:
        lines.append(f"  FAILED  {report.message}")
        log = report.detail.get("log")
        if isinstance(log, list):
            log = "\n".join(str(line) for line in log)
        if log:
            lines += [f"  | {line}" for line in str(log).splitlines()[-20:]]
    if report.env_names:
        lines.append(f"  env     {', '.join(report.env_names)}")
    lines += [f"  note    {warning}" for warning in report.warnings]
    return "\n".join(lines)


@app.command("deployments")
def deployments_command(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """What has been deployed for this app, and what is live now."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        rows = describe_deployments(conn, row, limit=limit)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if not rows:
        typer.echo(f"{name} has never been deployed. `applace deploy {name}` ships it.")
        return
    for entry in rows:
        where = f"  {entry['url']}" if entry["url"] else ""
        typer.echo(
            f"{entry['at']:<22} {entry['target']:<8} {entry['environment']:<11} "
            f"{entry['status']:<8} {entry['commit'][:12]}{where}"
        )


@app.command("skill")
def skill_command(
    full: Annotated[
        bool, typer.Option("--full/--brief", help="Print the whole document.")
    ] = True,
) -> None:
    """Print what an agent is told: SKILL.md, plus what this machine does.

    The same text `get_skill` returns. Read it when an agent does something you
    did not expect -- the answer is usually that this document told it to.
    """
    paths = _home()
    _require_home(paths)
    described = skill.describe(paths)
    if full:
        typer.echo(described["skill"])
    typer.echo("--- this machine ---")
    typer.echo(f"home     {described['home']}")
    typer.echo(
        "stacks   " + ", ".join(stack["name"] for stack in described["stacks"])
    )
    connection = described["github"]
    typer.echo(
        f"github   {connection['org']} ({connection['visibility']})"
        if connection
        else "github   not connected"
    )
    rules = described["policy"]
    typer.echo(f"policy   {rules.get('error') or rules['summary']}")
    shipping = described["deploy"]
    typer.echo(
        f"deploy   {', '.join(shipping['targets'])} "
        f"(production: {shipping['production']}, and always a human)"
    )


@app.command("policy")
def policy_command() -> None:
    """What this machine allows an agent to add, and to expose (D9, D6)."""
    paths = _home()
    _require_home(paths)
    try:
        rules = policy.load(paths)
    except policy.PolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(str(rules.source) if rules.source else f"{paths.policy} (absent — defaults)")
    typer.echo(f"  dependencies  {rules.summary()}")
    typer.echo(f"  public repos  {rules.public_repositories}")
    typer.echo(f"  production    {rules.production_deploys}")
    hosts = ", ".join(rules.hosts) if rules.hosts else "any host an app declares"
    typer.echo(f"  apis          {hosts}")
    looks = "every declared route, in a browser" if rules.visit else "off (gate: visit: false)"
    typer.echo(f"  visit         {looks}")


env_app = typer.Typer(
    add_completion=False,
    help="Values an app needs. Agents declare names; you supply values (D8).",
    no_args_is_help=True,
)
app.add_typer(env_app, name="env")


@env_app.command("set")
def env_set(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    variable: Annotated[str, typer.Argument(help="The variable, e.g. VITE_API_BASE_URL.")],
    value: Annotated[
        str | None,
        typer.Option(
            "--value",
            help="The value. Omit it and you are prompted without an echo, "
            "which keeps it out of your shell history.",
        ),
    ] = None,
) -> None:
    """Give a variable its value. Nothing here is printed, logged or committed."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        slug = str(row["slug"])
        env_store.check_name(variable)
        secret = value if value is not None else typer.prompt(variable, hide_input=True)
        env_store.set_value(paths, slug, variable, secret)
        # Declared as well as set: a human may be ahead of the agent, and the
        # agent has to be able to see that the value is already there.
        declare_env(paths, conn, slug, name=variable)
    except (AppError, env_store.EnvError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"{variable} set for {name}")
    typer.echo(f"  stored in {paths.env_file(name)} (0600), never in the repository")
    typer.echo("  restart the preview to pick it up: `applace stop` then `applace dev`")


@env_app.command("ls")
def env_list(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
) -> None:
    """The variables this app declares, and which of them have a value."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        detail = app_detail(paths, conn, row)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    variables = detail.get("env") or []
    if not variables:
        typer.echo(f"{name} declares no variables.")
        return
    for variable in variables:
        state = "set" if variable["set"] else "MISSING"
        browser = "" if variable["exposed"] else "  (build only — no bundler prefix)"
        typer.echo(f"  {state:<8} {variable['name']}{browser}")
        if variable["description"]:
            typer.echo(f"           {variable['description']}")


@env_app.command("rm")
def env_remove(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    variable: Annotated[str, typer.Argument(help="The variable to forget.")],
) -> None:
    """Remove a variable's value and its declaration."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        had_value = env_store.unset_value(paths, str(row["slug"]), variable)
        env_store.forget(conn, app_id=str(row["id"]), name=variable)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"Removed {variable}" + ("" if had_value else " (it had no value)"))


api_app = typer.Typer(
    add_completion=False,
    help="The upstreams an app may call, and the gateway that holds the token (D24).",
    no_args_is_help=True,
)
app.add_typer(api_app, name="api")


@api_app.command("ls")
def api_list(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
) -> None:
    """What this app is allowed to call, and under which path its code calls it."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        declared = apis.declarations(conn, str(row["id"]))
        values = env_store.values_for(paths, str(row["slug"]))
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if not declared:
        typer.echo(f"{name} declares no APIs.")
        return
    for api in declared:
        typer.echo(f"  {api.name}  →  {api.base_url}")
        typer.echo(f"    fetch    {api.mount}/…")
        typer.echo(f"    allowed  {', '.join(api.methods)} on {', '.join(api.paths)}")
        if api.token_env:
            state = "set" if api.token_env in values else "MISSING"
            typer.echo(f"    token    {api.token_env} ({state}, sent as {api.header})")


@api_app.command("rm")
def api_remove(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    api_name: Annotated[str, typer.Argument(help="The declared API to forget.")],
) -> None:
    """Stop allowing an app to call an API. Takes effect on the next request."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        removed = apis.forget(conn, app_id=str(row["id"]), name=api_name)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if not removed:
        typer.echo(f"{name} did not declare {api_name}.")
        return
    typer.echo(f"Removed {api_name} from {name}")
    typer.echo("  the generated function goes on the next write")


@api_app.command("gateway")
def api_gateway(
    stop: Annotated[
        bool, typer.Option("--stop", help="Stop the gateway instead of describing it.")
    ] = False,
) -> None:
    """The process that holds this home's credentials, and what it has refused."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        if stop:
            typer.echo("Gateway stopped." if gateway.stop(conn) else "Nothing running.")
            return
        running = gateway.status(conn)
    finally:
        conn.close()
    if running is None:
        typer.echo("No gateway running. It starts with the first preview that needs one.")
        return
    typer.echo(f"{running.url}  (pid {running.pid})")
    typer.echo(f"  log  {running.log}")
    typer.echo(f"  key  {paths.env / gateway.KEY_NAME} (0600)")


route_app = typer.Typer(
    add_completion=False,
    help="The pages the gate opens in a browser on every write (D25).",
    no_args_is_help=True,
)
app.add_typer(route_app, name="route")


@route_app.command("add")
def route_add(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    path: Annotated[str, typer.Argument(help="The route, as the app's router spells it.")],
    selector: Annotated[
        str | None, typer.Option("--shows", help="A CSS selector the page must match.")
    ] = None,
    text: Annotated[
        str | None, typer.Option("--says", help="Text the page must contain.")
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="What this page is for.")
    ] = None,
) -> None:
    """Declare a page the gate opens, and what it must find there."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        result = apps.declare_route(
            paths,
            conn,
            name,
            path=path,
            selector=selector,
            text=text,
            description=description,
        )
    except (AppError, RouteError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"{result['app']} {result['path']}")
    if result.get("note"):
        typer.secho(f"  {result['note']}", fg=typer.colors.YELLOW)


@route_app.command("ls")
def route_list(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
) -> None:
    """What the gate opens, what it looks for there, and whether it looks at all."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        state = apps.declared_routes(paths, conn, name)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if not state["on"]:
        typer.secho(f"Not visited: {state['reason']}", fg=typer.colors.YELLOW)
    for route in state["routes"]:
        suffix = "" if state["declared"] else "   (the default, nothing declared)"
        typer.echo(f"  {route['path']}{suffix}")
        if route["selector"]:
            typer.echo(f"    shows    {route['selector']}")
        if route["text"]:
            typer.echo(f"    says     {route['text']!r}")
        if route["description"]:
            typer.echo(f"    {route['description']}")


@route_app.command("rm")
def route_remove(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    path: Annotated[str, typer.Argument(help="The declared route to forget.")],
) -> None:
    """Stop opening a route. Forgetting the last one goes back to `/` alone."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        removed = apps.forget_route(conn, name, path)["forgotten"]
    except (AppError, RouteError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    typer.echo(f"Removed {path} from {name}" if removed else f"{name} did not declare {path}.")


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
    review: Annotated[
        str,
        typer.Option(
            "--review",
            help="direct: push to the default branch. pr: push to a branch per "
            "app and keep a pull request open against it (D15).",
        ),
    ] = DIRECT,
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
    if review not in REVIEWS:
        typer.secho(
            f"--review must be one of {', '.join(REVIEWS)}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    # D6 lives in the class, not here: making code visible is gated once, and a
    # backend embedding Applace gets the same gate without re-deciding it (D17).
    # What is the terminal's own is asking the question out loud.
    connect = Applace(paths).connect_github
    settings: dict[str, Any] = {
        "visibility": visibility,
        "prefix": prefix,
        "team": team,
        "host": host,
        "user": user,
        "review": review,
        "api_base": api_base,
    }
    answer = connect(org, **settings, confirm=yes)
    if answer.get("code") == "confirm-required":
        typer.confirm(
            f"Every app Applace creates in {org} will be a PUBLIC repository, "
            f"readable by anyone on the internet. Continue?",
            abort=True,
            default=False,
        )
        answer = connect(org, **settings, confirm=True)
    if not answer.get("ok"):
        message = str(answer.get("error", "could not connect"))
        hint = answer.get("hint")
        typer.secho(
            f"{message} {hint}" if hint else message, fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

    link = github_load(paths)
    if link is None:  # pragma: no cover -- it was just saved
        raise typer.Exit(code=1)
    typer.echo(f"Connected {org} on {host} ({visibility})")
    if prefix:
        typer.echo(f"  repositories will be named {prefix}<slug>")
    if link.reviewed:
        typer.echo(
            "  every app pushes to applace/<slug> and keeps a pull request open"
        )
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
    typer.echo(
        "  review  a pull request per app, against the default branch"
        if link.reviewed
        else "  review  none: green commits go straight to the default branch"
    )
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


vercel_app = typer.Typer(
    add_completion=False,
    help="The Vercel account deployments go to (D12).",
    no_args_is_help=True,
)
app.add_typer(vercel_app, name="vercel")


@vercel_app.command("connect")
def vercel_connect(
    team: Annotated[
        str | None,
        typer.Option("--team", help="Team id, if the projects belong to a team."),
    ] = None,
    api_base: Annotated[
        str | None,
        typer.Option("--api-base", help="Override the API root. For testing."),
    ] = None,
) -> None:
    """Say which Vercel account ships this machine's apps.

    The token is not stored: it is read from VERCEL_TOKEN (or
    APPLACE_VERCEL_TOKEN) each time, so `config.json` stays a file anyone can
    read and a leaked home directory leaks nothing.
    """
    paths = _home()
    _require_home(paths)
    link = VercelLink(team=team, api_base=api_base)
    vercel_save(paths, link)
    typer.echo(f"Vercel connected{f' (team {team})' if team else ''}")
    typer.echo(f"  api     {link.api}")
    typer.echo(f"  {_vercel_reachability(link)}")


@vercel_app.command("status")
def vercel_status() -> None:
    """Whether this machine could deploy to Vercel right now."""
    paths = _home()
    _require_home(paths)
    link = vercel_load(paths)
    if link is None:
        typer.echo(
            "Vercel is not connected. `applace vercel connect` is one command, "
            "and `applace deploy <app>` works without it."
        )
        raise typer.Exit(code=1)
    typer.echo(f"Vercel{f' (team {link.team})' if link.team else ''}")
    typer.echo(f"  api     {link.api}")
    typer.echo(f"  {_vercel_reachability(link)}")


def _vercel_reachability(link: VercelLink) -> str:
    try:
        auth = vercel_token()
    except VercelError as exc:
        return str(exc)
    status, body = vercel_api(link, "GET", "/v2/user", auth=auth)
    if status == 200:
        user = body.get("user") or body
        return f"token ok, authenticated as {user.get('username') or user.get('email')}"
    return f"Vercel answered {status}: {body.get('error', {}).get('message', 'no detail')}"


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
        typer.echo(
            f"Pushed {report.commit[:12] if report.commit else ''} to "
            f"{report.github_url} ({report.branch})"
        )
        if report.pull_request is not None:
            verb = "Opened" if report.pull_request.created else "Updated"
            typer.echo(f"  {verb} #{report.pull_request.number} {report.pull_request.html_url}")
        elif report.reason:
            typer.echo(f"  {report.reason}")
        return
    typer.secho(report.reason, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


# In the order `applace open` tries them, which is "the most finished thing
# this app has": a production URL beats a preview deployment, which beats a dev
# server on this laptop, which beats the repository it all came from.
OPENABLE = ("live", "preview", "repo", "dir")


@app.command("open")
def open_command(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    what: Annotated[
        str | None,
        typer.Option(
            "--what",
            "-w",
            help="live, preview, repo or dir. The default is the first of those "
            "this app has.",
        ),
    ] = None,
    show: Annotated[
        bool,
        typer.Option("--print", help="Print the target instead of opening it."),
    ] = False,
) -> None:
    """Open the app -- what is live, the preview, the repository, the directory.

    This is the handover command. An agent hands back URLs in a chat window; a
    human wants the thing itself, and typing `applace open billing-portal` is
    shorter than finding which of four addresses the app currently has.
    """
    paths = _home()
    _require_home(paths)
    if what is not None and what not in OPENABLE:
        typer.secho(
            f"--what must be one of {', '.join(OPENABLE)}.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
        detail = app_detail(paths, conn, row)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    targets = _openable(detail)
    if what is not None:
        target = targets.get(what)
        if target is None:
            typer.secho(
                f"{name} has no {what} to open. It has: "
                f"{', '.join(targets) or 'nothing yet'}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        kind = what
    else:
        kind, target = next(
            ((key, targets[key]) for key in OPENABLE if key in targets),
            ("", ""),
        )
        if not target:  # pragma: no cover - every app has a directory
            typer.secho(f"{name} has nothing to open.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

    typer.echo(f"{kind:<8} {target}")
    if not show:
        typer.launch(target)


def _openable(detail: dict[str, object]) -> dict[str, str]:
    """The addresses an app has right now, keyed by what they are."""
    out: dict[str, str] = {}
    deployments = detail.get("deployments")
    live = [
        entry
        for entry in (deployments if isinstance(deployments, list) else [])
        if entry.get("status") == "live" and entry.get("url")
    ]
    production = next(
        (entry for entry in live if entry.get("environment") == "production"), None
    )
    if production is not None:
        out["live"] = str(production["url"])
    elif live:
        out["live"] = str(live[0]["url"])
    running = detail.get("preview")
    if isinstance(running, dict) and running.get("url"):
        out["preview"] = str(running["url"])
    if detail.get("github_url"):
        out["repo"] = str(detail["github_url"])
    if detail.get("path"):
        out["dir"] = str(detail["path"])
    return out


@app.command()
def logs(
    name: Annotated[str, typer.Argument(help="The app's slug.")],
    which: Annotated[
        str, typer.Option("--of", help="dev or deploy.")
    ] = "dev",
    lines: Annotated[int, typer.Option("--lines", "-n")] = 40,
) -> None:
    """The end of the dev server's log, or of the last deploy's."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        row = require_app(conn, name)
    except AppError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    log = paths.app_logs(str(row["slug"])) / ("deploy.log" if which == "deploy" else "dev.log")
    if not log.is_file():
        typer.echo(f"No {which} log for {name} yet ({log}).")
        raise typer.Exit(code=1)
    typer.echo(preview_tail(log, lines))


stacks_app = typer.Typer(
    help="The stacks apps are built from, and the company's own.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(stacks_app, name="stacks")


@stacks_app.callback()
def stacks_root(context: typer.Context) -> None:
    """List the stacks an app can be built from. Subcommands manage them."""
    if context.invoked_subcommand is not None:
        return
    paths = _home()
    try:
        entries = describe_stacks(paths)
    except StackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    for entry in entries:
        origin = "built in" if entry["builtin"] else str(entry["source"])
        typer.echo(f"{entry['name']:<20} {entry['title']}  ({origin})")
        typer.echo(f"{'':<20} {entry['description']}")
        if entry["commit"]:
            pinned = f"{'':<20} pinned at {str(entry['commit'])[:12]}"
            if entry.get("ref") and entry["ref"] != "HEAD":
                pinned += f" on {entry['ref']}"
            typer.echo(pinned)


@stacks_app.command("add")
def stacks_add(
    url: Annotated[str, typer.Argument(help="The stack repository, as git would clone it.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Install it under this name instead.")
    ] = None,
    ref: Annotated[
        str | None, typer.Option("--ref", help="A branch or tag, rather than the default one.")
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", help="The stack's directory inside the repository."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a stack of the same name.")
    ] = False,
) -> None:
    """Install a company stack from git, pinned to the commit it was cloned at."""
    paths = _home()
    _require_home(paths)
    try:
        installed = add_stack(paths, url, name=name, ref=ref, subdir=path, force=force)
    except StackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Installed {installed.name} ({installed.title})")
    typer.echo(f"  from    {installed.source} at {installed.commit[:12]}")
    typer.echo(f"  in      {installed.path}")
    typer.echo(f"  new     applace new \"My App\" --stack {installed.name}")


@stacks_app.command("update")
def stacks_update(
    name: Annotated[str, typer.Argument(help="The installed stack's name.")],
) -> None:
    """Move an installed stack to the newest commit of the ref it came from."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        installed = update_stack(paths, name)
        drifted = [entry for entry in stack_drift(conn, paths) if entry["stack"] == name]
    except StackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()
    if not installed.changed:
        typer.echo(f"{name} is already at {installed.commit[:12]}")
        return
    previous = (installed.previous or "")[:12] or "an unrecorded commit"
    typer.echo(f"Updated {name}: {previous} → {installed.commit[:12]}")
    for entry in drifted:
        typer.echo(
            f"  {entry['app']} was built from {str(entry['born_at'])[:12]} and "
            f"stays there; new apps get {str(entry['stack_at'])[:12]}."
        )


@stacks_app.command("rm")
def stacks_remove(
    name: Annotated[str, typer.Argument(help="The installed stack's name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask.")] = False,
) -> None:
    """Uninstall a company stack. The apps built from it are repositories and stay."""
    paths = _home()
    _require_home(paths)
    if not yes:
        typer.confirm(f"Uninstall the stack {name}?", abort=True, default=False)
    try:
        root = remove_stack(paths, name)
    except StackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Removed {root}")


@stacks_app.command("drift")
def stacks_drift() -> None:
    """Which apps were born from a stack commit that is no longer installed."""
    paths = _home()
    _require_home(paths)
    conn = connect(paths.db)
    try:
        drifted = stack_drift(conn, paths)
    finally:
        conn.close()
    if not drifted:
        typer.echo("Every app is on the stack commit that is installed now.")
        return
    for entry in drifted:
        typer.echo(
            f"{entry['app']:<24} {entry['stack']:<16} "
            f"born {str(entry['born_at'])[:12]}  now {str(entry['stack_at'])[:12]}"
        )


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
    """Expose this home to an MCP host. Stdio unless --http is given.

    Over HTTP the same port also serves the panel (D16): `/panel` for a page a
    human can watch, `/panel/embed.js` for the `<applace-app>` element a chat UI
    embeds, and `/panel/apps/<slug>` for the card behind both.
    """
    paths = _home()
    _require_home(paths)
    if http is not None:
        typer.echo(f"MCP    http://{host}:{http}/mcp")
        typer.echo(f"Panel  http://{host}:{http}/panel")
    serve_server(paths, port=http, host=host)


machine_app = typer.Typer(
    add_completion=False,
    help="A root holding one home per person, for a backend serving many (D20).",
    no_args_is_help=True,
)
app.add_typer(machine_app, name="machine")

ROOT_OPTION = typer.Option(
    "--root",
    envvar="APPLACE_ROOT",
    help="The directory holding every user's home. Defaults to $APPLACE_ROOT.",
)


def _machine(root: Path | None) -> Machine:
    """The root an operator means. Nothing is created by asking about one.

    A root that does not exist yet is a typo far more often than it is a new
    machine, and `users` on a fresh empty directory says nothing useful.
    """
    if root is None:
        typer.secho(
            "Say which root with --root, or set APPLACE_ROOT.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if not root.exists():
        typer.secho(f"No machine root at {root}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        found = Machine(root)
        _ = found.limits  # read machine.yaml here: a bad file is one error, early
    except MachineError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    return found


@machine_app.command("users")
def machine_users(
    root: Annotated[Path | None, ROOT_OPTION] = None,
) -> None:
    """Everyone with a home here, and how much of it they are using."""
    found = _machine(root)
    people = found.users()
    if not people:
        typer.echo(f"No homes yet under {found.root}.")
        return
    limits = found.limits
    for record in people:
        typer.echo(
            f"{record['user']}  {record['apps']} apps, "
            f"{record['previews']} previewing, {record['disk_mb']} MB"
            + (f" of {limits.disk_mb}" if limits.disk_mb is not None else "")
        )
        typer.echo(f"  {record['home']}")


@machine_app.command("ports")
def machine_ports(
    root: Annotated[Path | None, ROOT_OPTION] = None,
) -> None:
    """Who holds which port, across every home (D21)."""
    found = _machine(root)
    holds = found.ports()
    if not holds:
        typer.echo("No port is held.")
        return
    for hold in holds:
        pid = hold["pid"]
        typer.echo(
            f"{hold['port']}  {hold['kind']:<7} {hold['app']}  "
            + (f"pid {pid}" if pid else "starting")
        )
        typer.echo(f"  {hold['home']}")


@machine_app.command("gc")
def machine_gc(
    root: Annotated[Path | None, ROOT_OPTION] = None,
    preview_hours: Annotated[
        float,
        typer.Option(
            "--preview-hours", help="Stop previews nobody has touched for this long."
        ),
    ] = 2.0,
    scratch_hours: Annotated[
        float,
        typer.Option(
            "--scratch-hours", help="Remove scratch checkouts older than this."
        ),
    ] = 24.0,
) -> None:
    """Stop what nobody is watching and free what nobody is holding (D23).

    Never deletes an app, a repository, a secret or a home. Safe on a cron.
    """
    found = _machine(root)
    report = found.gc(preview_hours=preview_hours, scratch_hours=scratch_hours)
    for stopped in report["previews_stopped"]:
        typer.echo(f"Stopped {stopped['app']} ({stopped['user']})")
    typer.echo(
        f"{len(report['previews_stopped'])} previews stopped, "
        f"{report['ports_released']} ports released, "
        f"{report['scratch_removed']} scratch checkouts removed"
    )


def main() -> None:
    app()
