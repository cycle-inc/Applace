"""Creating and describing apps.

An app is a directory under ``~/.applace/apps/`` that is an ordinary git
repository (D1) and a row in the database saying what it was made from. The row
is bookkeeping; the repository is the truth, and every question about the app's
*content* is answered by asking git rather than by reading the row.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import apis, deploy, env, eyes, gateway, github, gitrepo, handover, policy
from . import preview, register, sensor, shell
from .db import Connection, find_app, insert_app, insert_snapshot, latest_gate
from .db import list_apps as db_list_apps
from .db import list_snapshots, unpushed
from .github import GitHubError
from .sync import connect_app, push_app
from .naming import display_name, slugify
from .paths import ApplacePaths
from .stacks import Stack, resolve

FIRST_COMMIT = "Create {name} from the {stack} stack"

# `npm install` on a cold cache is minutes, not seconds.
INSTALL_TIMEOUT = 900.0


class AppError(Exception):
    """Something about the request makes the app impossible to create."""


class AppExists(AppError):
    pass


class UnknownApp(AppError):
    pass


@dataclass
class CreateReport:
    slug: str
    name: str
    path: Path
    stack: str
    entry: str
    commit: str
    files: list[str]
    installed: bool
    warnings: list[str] = field(default_factory=list)
    repo: str | None = None
    github_url: str | None = None
    pushed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "app": self.slug,
            "name": self.name,
            "stack": self.stack,
            "path": str(self.path),
            "entry": self.entry,
            "commit": self.commit,
            "files": self.files,
            "installed": self.installed,
            "warnings": self.warnings,
            "repo": self.repo,
            "github_url": self.github_url,
            "pushed": self.pushed,
        }


def create_app(
    paths: ApplacePaths,
    conn: Connection,
    *,
    name: str,
    stack_name: str | None = None,
    description: str | None = None,
    install: bool = True,
    publish: bool = True,
) -> CreateReport:
    """Render a stack into a new repository and record it.

    Failures before the first commit remove the directory again: a half-rendered
    app that cannot be created and cannot be used is worse than no app, and the
    caller's obvious next move is to try again with a different name.

    A failed ``install`` is *not* one of those failures. The source is valid, it
    is committed, and the fix is one command -- deleting the work because npm
    could not reach the registry would be the wrong call.
    """
    slug = slugify(name)
    title = display_name(name)
    stack = resolve(paths.stacks, stack_name)
    target = paths.app(slug)

    if find_app(conn, slug) is not None:
        raise AppExists(f"an app called {slug!r} already exists")
    if target.exists() and any(target.iterdir()):
        raise AppExists(
            f"{target} already exists and is not empty, but no app called "
            f"{slug!r} is registered. Move it aside or pick another name."
        )

    warnings: list[str] = []
    try:
        files = stack.render(
            target,
            context={
                "app_slug": slug,
                "app_name": title,
                "app_description": description or f"{title}, built with Applace.",
            },
        )
        gitrepo.init(target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    installed = False
    if install:
        result = shell.run(
            stack.commands["install"], cwd=target, timeout=INSTALL_TIMEOUT
        )
        installed = result.ok
        if not result.ok:
            warnings.append(
                f"`{result.command}` failed, so the app has no dependencies yet. "
                f"Run it again in {target} once the problem is fixed.\n{result.output}"
            )

    commit = gitrepo.commit_all(target, FIRST_COMMIT.format(name=title, stack=stack.name))

    app_id = uuid.uuid4().hex
    insert_app(
        conn,
        app_id=app_id,
        slug=slug,
        name=title,
        description=description,
        stack=stack.name,
        stack_source=stack.source,
        stack_commit=stack.commit,
        path=str(target),
    )
    insert_snapshot(
        conn,
        snapshot_id=uuid.uuid4().hex,
        app_id=app_id,
        commit_sha=commit.sha,
        message=commit.message,
    )
    conn.commit()

    report = CreateReport(
        slug=slug,
        name=title,
        path=target,
        stack=stack.name,
        entry=stack.entry,
        commit=commit.sha,
        files=files,
        installed=installed,
        warnings=warnings,
    )
    if publish:
        _publish(paths, conn, slug, report, description=description)
    return report


def _publish(
    paths: ApplacePaths,
    conn: Connection,
    slug: str,
    report: CreateReport,
    *,
    description: str | None,
) -> None:
    """Give the new app its repository, if an organisation is connected (D2).

    Every failure here is a warning, never an exception: the app exists, it is
    committed, and "GitHub was down" must not cost a developer their work. The
    fix is `applace push`, and the warning says so.
    """
    if github.load(paths) is None:
        return
    row = find_app(conn, slug)
    if row is None:  # pragma: no cover - it was inserted a line ago
        return
    try:
        repository = connect_app(paths, conn, row, description=description)
    except GitHubError as exc:
        report.warnings.append(f"{exc}\nThe app is committed locally. `applace push {slug}` retries.")
        return
    report.repo = repository.full_name
    report.github_url = repository.html_url

    pushed = push_app(paths, conn, find_app(conn, slug))
    report.pushed = pushed.pushed
    if not pushed.pushed:
        report.warnings.append(
            f"{repository.full_name} exists but the first push did not land: "
            f"{pushed.reason}"
        )


def require_app(conn: Connection, key: str) -> Any:
    row = find_app(conn, key)
    if row is None:
        known = ", ".join(str(r["slug"]) for r in db_list_apps(conn)) or "none yet"
        raise UnknownApp(f"no app called {key!r}. Known apps: {known}")
    return row


def app_summary(conn: Connection, row: Any) -> dict[str, Any]:
    """What `list_apps` says about one app: state, not content.

    The journal half of this answer is the register's (D27), so that one home's
    listing and the machine-wide one cannot disagree about the state of the same
    app. What is added here is what only a working tree can answer.
    """
    path = Path(str(row["path"]))
    present = path.is_dir()
    summary: dict[str, Any] = register.summarise(conn, row)
    if not present:
        # The row outlived the directory. Say so plainly rather than raising:
        # `list_apps` failing wholesale because one app was deleted by hand
        # would be a poor trade.
        summary["missing"] = True
        summary["state"] = "missing"
        return summary
    summary["dirty"] = gitrepo.is_dirty(path)
    summary["branch"] = gitrepo.current_branch(path)
    return summary


def app_detail(
    paths: ApplacePaths, conn: Connection, row: Any, *, stack: Stack | None = None
) -> dict[str, Any]:
    """What `get_app` says: everything an agent needs before it writes code."""
    detail = app_summary(conn, row)
    path = Path(str(row["path"]))
    if detail.get("missing"):
        return detail
    if stack is None:
        try:
            stack = resolve(paths.stacks, str(row["stack"]))
        except Exception:  # the stack was uninstalled; the app is still readable
            stack = None
    detail["files"] = gitrepo.tracked_files(path)
    detail["uncommitted"] = gitrepo.status(path)
    detail["snapshots"] = len(list_snapshots(conn, str(row["id"])))
    detail["installed"] = (path / "node_modules").is_dir()
    detail["repo"] = row["github_repo"]
    # Where the work is waiting, on a machine that reviews (D15). Remembered at
    # push time so it can be said again later without asking GitHub.
    detail["pull_request_url"] = row["pull_request_url"]
    # Commits that are here and not known to be on GitHub. Non-zero means a push
    # was refused or the machine was offline, and the next one carries them all.
    detail["unpushed"] = len(unpushed(conn, str(row["id"])))
    # What a human has done here that Applace did not (M9). An app is a
    # repository a person is invited to open, so "somebody else has been in
    # here" is a state the agent has to be told about before it writes.
    detail["human"] = handover.survey(conn, row).as_dict()
    detail.update(outstanding(conn, str(row["id"])))
    live = preview.status(conn, str(row["id"]), str(row["slug"]), path)
    detail["preview"] = live.as_dict() if live is not None else None
    # What is shipped, and from which commit -- the answer to "is what I am
    # looking at what is live", which a preview URL cannot give.
    detail["deployments"] = deploy.describe(conn, row)
    if stack is not None:
        detail["entry"] = stack.entry
        detail["env_prefix"] = stack.env_prefix
        detail["deploy"] = list(stack.deploy)
        # The app keeps the stack it was born from; the stack may have moved
        # since (D7). Saying so is the whole of what Applace does about it --
        # re-generating an app's files from a newer template would overwrite
        # work that is already committed in the app's own history.
        born = str(row["stack_commit"] or "")
        detail["stack_commit"] = born or None
        detail["stack_drifted"] = bool(
            born and stack.commit is not None and born != stack.commit
        )
        if detail["stack_drifted"]:
            detail["stack_note"] = (
                f"this app was created from {stack.name} at {born[:12]}; the "
                f"installed {stack.name} is now at {str(stack.commit)[:12]}. Its "
                f"files are the ones in this repository, not the newer template."
            )
    # Names and whether a human has answered, never a value (D8).
    declared = env.declarations(
        conn,
        paths,
        app_id=str(row["id"]),
        slug=str(row["slug"]),
        prefix=stack.env_prefix if stack is not None else "",
    )
    detail["env"] = [variable.as_dict() for variable in declared]
    detail["env_missing"] = [variable.name for variable in env.missing(declared)]
    # What the gate will open in a browser, and -- when it will not -- why (D25).
    detail["visit"] = visiting(paths, conn, row)
    return detail


def declare_env(
    paths: ApplacePaths,
    conn: Connection,
    key: str,
    *,
    name: str,
    description: str | None = None,
) -> dict[str, Any]:
    """`set_env`: record that an app needs a variable, and say who supplies it.

    Returns the state of that variable and the command a human runs to give it
    a value. The value is not an argument here and never will be (D8).
    """
    row = require_app(conn, key)
    slug = str(row["slug"])
    env.declare(conn, app_id=str(row["id"]), name=name, description=description)
    try:
        stack = resolve(paths.stacks, str(row["stack"]))
        prefix = stack.env_prefix
    except Exception:  # the stack was uninstalled; the declaration still stands
        prefix = ""
    variables = {
        variable.name: variable
        for variable in env.declarations(
            conn, paths, app_id=str(row["id"]), slug=slug, prefix=prefix
        )
    }
    variable = variables[name]
    out: dict[str, Any] = {
        "app": slug,
        **variable.as_dict(),
        "instruction": env.instruction(slug, name),
    }
    if not variable.has_value:
        out["note"] = (
            f"{name} has no value yet. Ask the human to run "
            f"`{env.instruction(slug, name)}`; you will never see the value, and "
            f"the app will have it in the preview and in the build."
        )
    if not variable.exposed:
        out["warning"] = (
            f"{name} does not start with {prefix!r}, so this stack's bundler will "
            f"not expose it to the browser. Name it {prefix}{name} if the page "
            f"itself has to read it."
        )
    return out


def outstanding(conn: Connection, app_id: str) -> dict[str, Any]:
    """The errors the last gate left behind, if it left any (D4).

    An app is dirty either because the agent wrote something that did not pass
    or because a human edited the repository. Only the first of those has
    errors attached, and saying which one it is saves the agent a guess.
    """
    gate = latest_gate(conn, app_id)
    if gate is None or bool(gate["ok"]):
        return {"errors": []}
    return {
        "errors": json.loads(str(gate["errors_json"] or "[]")),
        "failed_stage": str(gate["stage"]),
    }


def start_preview(
    paths: ApplacePaths, conn: Connection, key: str, *, port: int | None = None
) -> preview.Preview:
    """Run the app's dev server, or hand back the one already running."""
    row = require_app(conn, key)
    slug = str(row["slug"])
    stack = resolve(paths.stacks, str(row["stack"]))
    # The one place that knows an app is about to need both (D24). The preview
    # supervisor is not told the gateway exists and the gateway is not told an
    # app is previewing; tying their lifecycles together would mean stopping one
    # whenever the other stopped, and they do not have the same lifetime.
    environment = env.values_for(paths, slug)
    if stack.gateway and apis.declarations(conn, str(row["id"])):
        running = gateway.ensure(paths, conn)
        environment.update(
            gateway.environment(running, slug, gateway.key_for(paths))
        )
    return preview.start(
        paths,
        conn,
        app_id=str(row["id"]),
        slug=slug,
        root=Path(str(row["path"])),
        stack=stack,
        port=port,
        # The dev server is where a value is actually needed (D8): it goes into
        # the process, not into a file in the repository and not into a report.
        environment=environment,
    )


# -- declared APIs (D24) ----------------------------------------------------


def declare_api(
    home: ApplacePaths, conn: Connection, key: str, **fields: Any
) -> dict[str, Any]:
    """`declare_api`: record an upstream this app may call, by name (D24).

    The agent's half of a credential, exactly as :func:`declare_env` is. What is
    stored is a name, a URL and the *name* of the variable holding the token;
    the value is supplied by a human and read only by the gateway.

    ``home`` rather than ``paths`` because one of the declaration's own fields is
    called ``paths`` -- the patterns the app may call -- and the two would
    collide in the keyword arguments.
    """
    paths = home
    row = require_app(conn, key)
    slug = str(row["slug"])
    stack = resolve(paths.stacks, str(row["stack"]))
    if not stack.gateway:
        raise apis.ApiError(
            f"the {stack.name} stack has no gateway, so {slug} cannot reach an "
            f"API without putting the credential in the browser. Build this "
            f"against an API that needs no token, or use a stack that has one."
        )
    rules = policy.load(paths)
    api = apis.build(**fields)
    # Both hosts, on a delegated API: where the data comes from and where the
    # person's identity is proved. A company that allowlists egress means both.
    for what, host in apis.egress(api):
        refused = rules.refuse_api(what, host)
        if refused:
            raise policy.PolicyError(
                f"{refused}. A human changes that in {paths.policy}."
            )

    # The one variable this declaration needs a human to answer: the app's own
    # credential, or the secret that authenticates Applace to the exchange
    # (D28). Never both -- `apis.build` refused that already.
    secret_name = api.token_env
    purpose = f"credential the gateway sends to {api.host} for {api.name}"
    if api.on_behalf_of is not None:
        secret_name = api.on_behalf_of.secret_env
        purpose = (
            f"authenticates Applace to {api.on_behalf_of.host}, which mints "
            f"{api.name} tokens for whoever is using {slug}"
        )
    if secret_name and stack.env_prefix and secret_name.startswith(stack.env_prefix):
        # The whole point of the gateway is that this value stays behind it, and
        # a name starting with the stack's prefix is a name the bundler inlines.
        raise apis.ApiError(
            f"{secret_name} starts with {stack.env_prefix}, which on the "
            f"{stack.name} stack means the value is compiled into the bundle the "
            f"browser downloads. Name it without the prefix: "
            f"{secret_name[len(stack.env_prefix) :] or 'CRM_TOKEN'}."
        )

    apis.declare(conn, app_id=str(row["id"]), api=api)
    if secret_name:
        # Declared here too, so it shows up in `env` beside everything else a
        # human still has to answer. The gateway is the only reader of the value.
        env.declare(
            conn, app_id=str(row["id"]), name=secret_name, description=purpose
        )
    declared = env.declarations(
        conn, paths, app_id=str(row["id"]), slug=slug, prefix=stack.env_prefix
    )
    has_value = any(v.name == secret_name and v.has_value for v in declared)
    out: dict[str, Any] = {"app": slug, **api.as_dict(), "token_set": has_value}
    if api.on_behalf_of is not None:
        out["calls_as"] = (
            f"whoever is using {slug}; {api.name} carries no credential of this "
            f"app's own, and a call with nobody behind it is refused."
        )
    if secret_name and not has_value:
        # Declared but unanswered is the normal state right after this call, and
        # the agent has to be able to say what a human should do about it (D8).
        out["needs"] = env.instruction(slug, secret_name)
        out["hint"] = (
            f"the gateway refuses the call until {secret_name} has a value; "
            f"ask the person you are talking to to run the command in `needs`."
        )
    return out


def forget_api(conn: Connection, key: str, name: str) -> dict[str, Any]:
    """Undeclare an upstream. The generated function goes on the next gate."""
    row = require_app(conn, key)
    removed = apis.forget(conn, app_id=str(row["id"]), name=name)
    return {"app": str(row["slug"]), "api": name, "forgotten": removed}


def declared_apis(conn: Connection, key: str) -> dict[str, Any]:
    """Every upstream this app declared, and where its own code fetches them."""
    row = require_app(conn, key)
    declared = apis.declarations(conn, str(row["id"]))
    return {
        "app": str(row["slug"]),
        "apis": [api.as_dict() for api in declared],
        "mount": apis.GATEWAY_PATH,
    }


# -- declared routes (D25) --------------------------------------------------


def declare_route(
    paths: ApplacePaths, conn: Connection, key: str, **fields: Any
) -> dict[str, Any]:
    """`add_route`: record a page the gate opens, and what it must show (D25).

    The agent's half of "it works": the gate can build anything, and only the
    agent knows which routes are the app. What is stored is a path and, at most,
    a selector and a string -- no test file enters the repository (D1) and no
    test framework enters the harness (D10).
    """
    row = require_app(conn, key)
    slug = str(row["slug"])
    route = sensor.build(**fields)
    sensor.declare(conn, app_id=str(row["id"]), route=route)
    out: dict[str, Any] = {"app": slug, **route.as_dict()}
    try:
        stack = resolve(paths.stacks, str(row["stack"]))
        rules = policy.load(paths)
    except Exception:  # the stack or the policy is this machine's problem, not the route's
        return out
    off = sensor.off_because(stack, rules.visit)
    if off:
        # Stored anyway: the switch is about this machine, and a machine that
        # has a browser tomorrow should not need the declaration written again.
        out["visited"] = False
        out["note"] = f"the route is recorded, but {off}."
    else:
        out["visited"] = True
    return out


def forget_route(conn: Connection, key: str, path: str) -> dict[str, Any]:
    """Stop visiting a route. Forgetting the last one puts `/` back."""
    row = require_app(conn, key)
    removed = sensor.forget(conn, app_id=str(row["id"]), path=path)
    return {"app": str(row["slug"]), "route": path, "forgotten": removed}


def declared_routes(paths: ApplacePaths, conn: Connection, key: str) -> dict[str, Any]:
    """Every route the gate opens on this app, and whether it opens any."""
    row = require_app(conn, key)
    return {"app": str(row["slug"]), **visiting(paths, conn, row)}


def visiting(paths: ApplacePaths, conn: Connection, row: Any) -> dict[str, Any]:
    """The state of the browser check for one app, as `get_app` reports it.

    Says "off, because..." rather than saying nothing (D25): a check that
    quietly did not run must never read like a check that passed.
    """
    routes = sensor.to_visit(conn, str(row["id"]))
    state: dict[str, Any] = {
        "routes": [route.as_dict() for route in routes],
        "declared": len(sensor.declarations(conn, str(row["id"]))),
    }
    try:
        stack = resolve(paths.stacks, str(row["stack"]))
        rules = policy.load(paths)
    except Exception as exc:
        state["on"] = False
        state["reason"] = str(exc)
        return state
    off = sensor.off_because(stack, rules.visit)
    state["on"] = not off
    if off:
        state["reason"] = off
    return state


def screenshot(
    paths: ApplacePaths,
    conn: Connection,
    key: str,
    *,
    route: str = "/",
    width: int = eyes.DEFAULT_WIDTH,
    height: int = eyes.DEFAULT_HEIGHT,
    full_page: bool = False,
) -> tuple[eyes.Shot, bool]:
    """Look at a route of the app. Starts the preview if it is not running.

    Returns the shot and whether a preview had to be started, which the caller
    reports -- an agent should know it now owns a dev server.
    """
    row = require_app(conn, key)
    running = preview.status(
        conn, str(row["id"]), str(row["slug"]), Path(str(row["path"]))
    )
    started = running is None
    if running is None:
        running = start_preview(paths, conn, key)
    url = running.url.rstrip("/") + "/" + route.lstrip("/")
    shot = eyes.capture(url, width=width, height=height, full_page=full_page)
    # Kept on disk as well as returned: the model gets the image once, but the
    # panel a human is looking at has to be able to show it again (D16).
    paths.shots.mkdir(parents=True, exist_ok=True)
    paths.shot(str(row["slug"])).write_bytes(shot.png)
    return shot, started


def stop_preview(conn: Connection, key: str) -> bool:
    """Stop the app's dev server. False when there was nothing running."""
    row = require_app(conn, key)
    stopped = preview.stop(
        conn, str(row["id"]), str(row["slug"]), Path(str(row["path"]))
    )
    # The gateway holds a credential in memory; the last preview going down is
    # the moment nothing in this home has a reason for it to still be up.
    if not preview.running(conn):
        gateway.stop(conn)
    return stopped


def deploy_app(
    paths: ApplacePaths,
    conn: Connection,
    key: str,
    *,
    target: str = deploy.LOCAL,
    environment: str = deploy.PREVIEW,
    commit: str | None = None,
    confirm: bool = False,
) -> deploy.Report:
    """Ship a commit of the app. Production needs a human's `confirm` (D6)."""
    row = require_app(conn, key)
    return deploy.run(
        paths,
        conn,
        row,
        resolve(paths.stacks, str(row["stack"])),
        target=target,
        environment=environment,
        commit=commit,
        confirm=confirm,
    )


def rollback_app(
    paths: ApplacePaths,
    conn: Connection,
    key: str,
    *,
    commit: str | None = None,
    target: str | None = None,
    environment: str | None = None,
    confirm: bool = False,
) -> deploy.Report:
    """Put back an earlier commit, wherever this app was last deployed."""
    row = require_app(conn, key)
    return deploy.rollback(
        paths,
        conn,
        row,
        resolve(paths.stacks, str(row["stack"])),
        commit=commit,
        target=target,
        environment=environment,
        confirm=confirm,
    )


def stop_deployments(paths: ApplacePaths, conn: Connection, key: str) -> bool:
    """Take down whatever this app has serving locally."""
    row = require_app(conn, key)
    return deploy.stop(paths, conn, row)


def remove_app(
    paths: ApplacePaths, conn: Connection, key: str, *, delete_files: bool
) -> Path:
    """Forget an app, and optionally delete its repository."""
    row = require_app(conn, key)
    path = Path(str(row["path"]))
    # Before the row goes: after it, nothing knows which process to signal and
    # the port stays held until someone finds it by hand.
    preview.stop(conn, str(row["id"]), str(row["slug"]), path)
    deploy.stop(paths, conn, row)
    conn.execute("DELETE FROM apps WHERE id = ?", (str(row["id"]),))
    conn.commit()
    if delete_files and path.is_dir() and path.parent == paths.apps:
        shutil.rmtree(path, ignore_errors=True)
    return path
