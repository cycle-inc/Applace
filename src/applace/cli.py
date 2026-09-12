"""The ``applace`` command line.

``init`` sets a home up, ``serve`` exposes it to an MCP host, and everything
else is what an agent can do, available to a human at a terminal: ``new``,
``ls``, ``stacks``, ``rm``. The two surfaces stay deliberately in step -- a
human debugging what an agent did should not have to use different words.
"""

from __future__ import annotations

from importlib import metadata
from typing import Annotated

import typer

from . import gate
from .apps import AppError, app_detail, create_app, remove_app, require_app
from .db import connect, list_apps
from .gate import GateReport
from .init_cmd import InitReport, run_init
from .naming import InvalidName
from .paths import ApplacePaths, paths as applace_paths
from .server import serve as serve_server
from .stacks import StackError, registry

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
            typer.echo(
                f"{str(row['slug']):<24} {str(row['stack']):<16} {state:<8} {commit}"
            )
    finally:
        conn.close()


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
