"""Shipping an app: adapters, the confirm gate, and the journal.

Three things hold everywhere in here, whatever the target is.

**A deployment is a commit.** Not "the working tree as it stood at 14:32" -- a
sha, resolved before anything is built, recorded in the row, and reported back.
That is what makes a rollback a normal operation rather than a rescue: the
previous deployment names a commit, so redeploying it is the same code path with
a different argument.

**Exposure is gated on the human, not on the agent's good sense (D6).** A
production deploy needs ``confirm=True`` no matter what the policy says, and a
company policy can take the act off the table entirely. An agent cannot confirm;
that is the point.

**The journal opens before the adapter runs.** A build that hangs, a token that
expires mid-upload, a machine that loses power -- each of those leaves a row
saying `running`, which is true. A row written only on success would hide
exactly the deploys a human needs to go and look at.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import apis, env, gitrepo, machine, policy as policy_module, preview, shell
from . import vercel
from .db import (
    Connection,
    claimed_ports,
    find_snapshot,
    finish_deployment,
    insert_deployment,
    latest_deployment,
    list_deployments,
    live_deployments,
)
from .paths import ApplacePaths
from .stacks import Stack
from .sync import push_app

LOCAL = "local"
VERCEL = "vercel"
TARGETS = (LOCAL, VERCEL)

PREVIEW = "preview"
PRODUCTION = "production"
ENVIRONMENTS = (PREVIEW, PRODUCTION)

# Deliberately clear of the dev servers' 5180-5279: a preview and a local
# deployment of the same app run side by side, and a port collision between the
# two would be a confusing way to learn that.
PORT_RANGE = range(5280, 5380)

BUILD_TIMEOUT = 900.0
INSTALL_TIMEOUT = 900.0
READY_TIMEOUT = 20.0
READY_INTERVAL = 0.2
LOG_NAME = "deploy.log"
LOG_TAIL = 40


class DeployError(Exception):
    """The deploy could not be carried out."""


class DeployRefused(DeployError):
    """Applace will not do this, and the reason is a rule rather than a fault."""

    def __init__(self, message: str, *, code: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), **self.detail}


@dataclass(frozen=True)
class Request:
    """Everything an adapter is allowed to know, and nothing else."""

    paths: ApplacePaths
    conn: Connection
    app_id: str
    app: str
    root: Path
    # Where the commit's files actually are. The app itself when the commit is
    # HEAD, a scratch worktree when it is not.
    workdir: Path
    stack: Stack
    commit: str
    environment: str
    # The human's values (D8). They reach a build and a provider; they are never
    # part of an Outcome, a log line or a report.
    variables: dict[str, str]
    repo: str | None
    branch: str
    log: Path

    @property
    def production(self) -> bool:
        return self.environment == PRODUCTION


@dataclass
class Outcome:
    """How it went. `status` is the word that lands in the deployments table."""

    status: str  # live | failed
    url: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "live"


@dataclass
class Report:
    id: str
    app: str
    target: str
    environment: str
    commit: str
    status: str
    url: str | None = None
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Names only, so that a report is something an agent may read (D8).
    env_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "live"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "deployment": self.id,
            "app": self.app,
            "target": self.target,
            "environment": self.environment,
            "commit": self.commit,
            "status": self.status,
            "url": self.url,
            "message": self.message,
            "detail": self.detail,
            "warnings": self.warnings,
            "env": self.env_names,
        }


class Adapter(Protocol):
    """A place an app can go. Stacks say which of these they are built for (D7)."""

    name: str

    def deploy(self, request: Request) -> Outcome: ...

    def stop(self, paths: ApplacePaths, row: Any) -> bool: ...


# -- the local target ------------------------------------------------------


class LocalTarget:
    """Build the app and serve the result on this machine.

    The point of it is not convenience, it is that *deploying is a thing you can
    do before you have an account anywhere*. The build is the real one -- the
    same command, the same env values, the production bundle -- so a page that
    works here has cleared everything except the provider.
    """

    name = LOCAL

    def deploy(self, request: Request) -> Outcome:
        build = _build(request)
        if build is not None:
            return build

        dist = request.workdir / request.stack.dist
        served = request.paths.served(request.app, request.environment)
        _replace(dist, served)

        # One server per app per environment: stop the old one before the new
        # one takes the name, so `applace ls` never shows two truths.
        previous = latest_deployment(
            request.conn,
            request.app_id,
            target=LOCAL,
            environment=request.environment,
            status="live",
        )
        if previous is not None:
            self.stop(request.paths, previous)
            finish_deployment(
                request.conn,
                str(previous["id"]),
                status="stopped",
                url=previous["url"],
                detail=_detail(previous),
            )
            request.conn.commit()

        port = _free_port(request.paths, request.conn, request.app)
        command = (
            f"{sys.executable} -m applace.static "
            f"--root {served} --host {preview.HOST} --port {port}"
        )
        pid = preview.spawn(command, cwd=served, log_path=request.log)
        machine.started(request.paths, port, pid)
        url = f"http://{preview.HOST}:{port}/"
        if not _wait_until_ready(url, pid):
            preview.terminate(pid)
            return Outcome(
                status="failed",
                detail={"stage": "serve", "port": port,
                        "log": preview.tail(request.log, LOG_TAIL)},
                message=f"the static server did not answer on {url}.",
            )
        return Outcome(
            status="live",
            url=url,
            detail={"pid": pid, "port": port, "root": str(served),
                    "command": command, "log": str(request.log)},
            message=f"{request.app} is live on {url} from {request.commit[:12]}.",
        )

    def stop(self, paths: ApplacePaths, row: Any) -> bool:
        detail = _detail(row)
        pid = detail.get("pid")
        if not isinstance(pid, int):
            return False
        # The same question the preview supervisor asks: is the process at this
        # pid still *our* process, or a recycled number?
        command = preview.process_command(pid)
        if command is None or str(detail.get("root", "")) not in command:
            return False
        preview.terminate(pid)
        return True


# -- the Vercel target -----------------------------------------------------


class VercelTarget:
    """Vercel, deploying a commit of the app's GitHub repository (D12).

    Uploading the built files is possible and this does it for an app that has
    no repository -- but it is the fallback, because what it puts live cannot be
    traced back to anything. With a repository, the deployment names a sha, and
    the sha is in the org's GitHub whether or not Applace is ever run again.
    """

    name = VERCEL

    def deploy(self, request: Request) -> Outcome:
        link = vercel.require(request.paths)
        token = vercel.token()
        project = vercel.ensure_project(
            link,
            name=request.app,
            repo=request.repo,
            settings=_project_settings(request.stack),
            auth=token,
        )
        names = vercel.sync_env(
            link,
            project,
            request.variables,
            targets=[request.environment],
            auth=token,
        )

        if project.repo_id:
            build = vercel.deploy_commit(
                link,
                project,
                repo_id=project.repo_id,
                ref=request.branch,
                sha=request.commit,
                production=request.production,
                auth=token,
            )
            how = "github"
        else:
            failed = _build(request)
            if failed is not None:
                return failed
            build = vercel.deploy_files(
                link,
                project,
                request.workdir / request.stack.dist,
                production=request.production,
                auth=token,
            )
            how = "upload"

        build = vercel.wait(link, build, auth=token)
        detail = {
            "project": project.as_dict(),
            "build": build.id,
            "state": build.state,
            "how": how,
            "inspector": build.inspector,
            "env": names,
        }
        if build.ok:
            return Outcome(
                status="live",
                url=build.url,
                detail=detail,
                message=f"{request.app} is live on {build.url}.",
            )
        detail["log"] = vercel.logs(link, build.id, auth=token)
        return Outcome(
            status="failed",
            url=build.url,
            detail=detail,
            message=build.error or f"Vercel ended the build as {build.state}.",
        )

    def stop(self, paths: ApplacePaths, row: Any) -> bool:
        # Nothing local is holding anything. Taking a Vercel deployment down is
        # deleting it, which is a human's call in Vercel's own dashboard --
        # Applace does not own that button.
        return False


def _project_settings(stack: Stack) -> dict[str, Any]:
    """Tell Vercel to build the app the way this stack builds it (D7).

    Naming a framework preset would let Vercel guess, and its guess and the
    stack's `build` command are two different definitions of the app. The stack
    wins, so the commands go over explicitly.
    """
    return {
        "framework": None,
        "installCommand": stack.commands.get("install"),
        "buildCommand": stack.commands.get("build"),
        "outputDirectory": stack.dist,
    }


ADAPTERS: dict[str, Adapter] = {LOCAL: LocalTarget(), VERCEL: VercelTarget()}


# -- the gate (D6) ---------------------------------------------------------


def gate(paths: ApplacePaths, environment: str, *, confirm: bool) -> str:
    """May this deploy happen, and did a human say so? Returns the rule applied.

    Note what is *not* here: no rule can make a production deploy happen without
    `confirm`. The policy can only tighten -- `deny` removes the act, `allow`
    still leaves the human in the loop -- because D6 is a property of the
    harness, not a default a company can configure away.
    """
    if environment != PRODUCTION:
        return "allow"
    rule = policy_module.load(paths).rule_for("production_deploys")
    if rule == "deny":
        raise DeployRefused(
            f"{paths.policy} forbids production deploys from this machine. "
            f"Deploy to the preview environment, or change the policy.",
            code="policy",
            rule=rule,
        )
    if not confirm:
        raise DeployRefused(
            "A production deploy puts this code in front of real users, so a "
            "human has to confirm it. Ask the person you are working with to "
            "run `applace deploy <app> --production`, or call this again with "
            "confirm=true once they have said yes.",
            code="confirm-required",
            rule=rule,
        )
    return rule


# -- the orchestration -----------------------------------------------------


def run(
    paths: ApplacePaths,
    conn: Connection,
    row: Any,
    stack: Stack,
    *,
    target: str,
    environment: str = PREVIEW,
    commit: str | None = None,
    confirm: bool = False,
) -> Report:
    """Deploy one commit of one app to one target, and journal all of it."""
    if target not in TARGETS:
        raise DeployRefused(
            f"{target!r} is not a target. Applace deploys to "
            f"{' or '.join(TARGETS)}.",
            code="unknown-target",
            targets=list(TARGETS),
        )
    if environment not in ENVIRONMENTS:
        raise DeployRefused(
            f"{environment!r} is not an environment. Use "
            f"{' or '.join(ENVIRONMENTS)}.",
            code="unknown-environment",
            environments=list(ENVIRONMENTS),
        )
    if target not in stack.deploy:
        raise DeployRefused(
            f"the {stack.name} stack does not declare the {target} target. It "
            f"builds for {', '.join(stack.deploy) or 'nothing yet'}.",
            code="unsupported-target",
            stack=stack.name,
            deploy=list(stack.deploy),
        )

    gate(paths, environment, confirm=confirm)

    app = str(row["slug"])
    root = Path(str(row["path"]))
    if not root.is_dir():
        raise DeployError(f"{root} is gone, so there is nothing to deploy.")
    if not gitrepo.has_commits(root):
        raise DeployRefused(
            f"{app} has no commits yet, and a deployment is a commit.",
            code="nothing-committed",
        )

    chosen, warnings = _resolve_commit(conn, row, root, commit)
    variables, missing = _variables(paths, conn, row, stack)
    if missing:
        warnings.append(
            f"{', '.join(missing)} has no value on this machine, so the build "
            f"will see it empty. `applace env set {app} {missing[0]}` fixes that."
        )
    if target == LOCAL and apis.declarations(conn, str(row["id"])):
        # The local target serves the built bundle and nothing else: there is no
        # runtime to run the generated function, so every gateway call 404s. Say
        # so rather than let a page look broken for a reason nobody can see.
        warnings.append(
            f"{app} calls declared APIs through {apis.GATEWAY_PATH}, and a local "
            f"deployment serves static files only, so those calls will 404. Use "
            f"the preview to exercise them, or deploy to vercel."
        )
    if target == VERCEL:
        warnings.extend(_ensure_pushed(paths, conn, row, chosen))

    deployment_id = uuid.uuid4().hex
    insert_deployment(
        conn,
        deployment_id=deployment_id,
        app_id=str(row["id"]),
        commit_sha=chosen.sha,
        target=target,
        environment=environment,
        confirmed=confirm,
    )
    conn.commit()

    log = paths.app_logs(app) / LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    workdir, temporary = _workdir(paths, root, chosen.sha, app, stack)
    request = Request(
        paths=paths,
        conn=conn,
        app_id=str(row["id"]),
        app=app,
        root=root,
        workdir=workdir,
        stack=stack,
        commit=chosen.sha,
        environment=environment,
        variables=variables,
        repo=row["github_repo"],
        branch=str(row["default_branch"] or gitrepo.DEFAULT_BRANCH),
        log=log,
    )

    try:
        outcome = ADAPTERS[target].deploy(request)
    except (DeployError, vercel.VercelError, gitrepo.GitError,
            shell.CommandNotFound, OSError) as exc:
        finish_deployment(
            conn, deployment_id, status="failed", detail={"error": str(exc)}
        )
        conn.commit()
        outcome = Outcome(status="failed", message=str(exc), detail={"error": str(exc)})
    except BaseException:
        # A bug, a Ctrl-C, a killed process: the row must not stay `running`,
        # and the exception must not be swallowed.
        finish_deployment(conn, deployment_id, status="failed",
                          detail={"error": "interrupted"})
        conn.commit()
        raise
    else:
        finish_deployment(
            conn,
            deployment_id,
            status=outcome.status,
            url=outcome.url,
            detail=outcome.detail,
        )
        conn.commit()
    finally:
        if temporary:
            gitrepo.remove_worktree(root, workdir)

    return Report(
        id=deployment_id,
        app=app,
        target=target,
        environment=environment,
        commit=chosen.sha,
        status=outcome.status,
        url=outcome.url,
        message=outcome.message,
        detail=outcome.detail,
        warnings=warnings,
        env_names=sorted(variables),
    )


def rollback(
    paths: ApplacePaths,
    conn: Connection,
    row: Any,
    stack: Stack,
    *,
    commit: str | None = None,
    target: str | None = None,
    environment: str | None = None,
    confirm: bool = False,
) -> Report:
    """Put a previous commit back, as an ordinary deploy of an older sha.

    With no commit named, it is the one before whatever is live: the common case
    is "the thing I just shipped is broken", and having to go and find the sha
    first is a poor thing to make someone do at that moment.
    """
    app = str(row["slug"])
    live = latest_deployment(conn, str(row["id"]), status="live")
    if target is None:
        if live is None:
            raise DeployRefused(
                f"{app} has never been deployed, so there is nothing to roll "
                f"back to. Name a target to deploy it.",
                code="never-deployed",
            )
        target = str(live["target"])
    if environment is None:
        environment = str(live["environment"]) if live is not None else PREVIEW

    if commit is None:
        commit = _previous_commit(conn, row, live)
        if commit is None:
            raise DeployRefused(
                f"there is no earlier deployment of {app} to go back to. Name a "
                f"commit from `applace log {app}`.",
                code="no-earlier-deployment",
            )
    return run(
        paths,
        conn,
        row,
        stack,
        target=target,
        environment=environment,
        commit=commit,
        confirm=confirm,
    )


def stop(paths: ApplacePaths, conn: Connection, row: Any) -> bool:
    """Take down whatever this app has running locally. False if nothing was."""
    stopped = False
    for environment in ENVIRONMENTS:
        live = latest_deployment(
            conn, str(row["id"]), target=LOCAL, environment=environment, status="live"
        )
        if live is None:
            continue
        ADAPTERS[LOCAL].stop(paths, live)
        finish_deployment(
            conn, str(live["id"]), status="stopped", url=live["url"],
            detail=_detail(live),
        )
        stopped = True
    if stopped:
        conn.commit()
    return stopped


def describe(conn: Connection, row: Any, limit: int = 5) -> list[dict[str, Any]]:
    """An app's recent deployments, for `get_app` and `applace ls`."""
    out: list[dict[str, Any]] = []
    for deployment in list_deployments(conn, str(row["id"]), limit=limit):
        out.append(
            {
                "id": str(deployment["id"]),
                "target": str(deployment["target"]),
                "environment": str(deployment["environment"]),
                "commit": str(deployment["commit_sha"]),
                "status": str(deployment["status"]),
                "url": deployment["url"],
                "at": str(deployment["finished_at"] or deployment["started_at"]),
            }
        )
    return out


# -- the parts that are only interesting when they go wrong ----------------


def _resolve_commit(
    conn: Connection, row: Any, root: Path, commit: str | None
) -> tuple[gitrepo.Commit, list[str]]:
    """Which sha goes live, and what a human should know about that choice."""
    warnings: list[str] = []
    if commit is None:
        chosen = gitrepo.head(root)
        if gitrepo.is_dirty(root):
            warnings.append(
                "the working tree has uncommitted changes, and they are not in "
                "this deploy: a deployment is a commit."
            )
    else:
        found = gitrepo.resolve(root, commit)
        if found is None:
            raise DeployRefused(
                f"{commit!r} is not a commit in {root}.",
                code="unknown-commit",
            )
        chosen = found
    if find_snapshot(conn, str(row["id"]), chosen.sha) is None:
        warnings.append(
            f"{chosen.sha[:12]} was not committed by Applace's gate, so nothing "
            f"has checked that it type-checks, lints and builds."
        )
    return chosen, warnings


def _variables(
    paths: ApplacePaths, conn: Connection, row: Any, stack: Stack
) -> tuple[dict[str, str], list[str]]:
    values = env.values_for(paths, str(row["slug"]))
    declared = env.declarations(
        conn,
        paths,
        app_id=str(row["id"]),
        slug=str(row["slug"]),
        prefix=stack.env_prefix,
    )
    return values, [variable.name for variable in env.missing(declared)]


def _ensure_pushed(
    paths: ApplacePaths, conn: Connection, row: Any, commit: gitrepo.Commit
) -> list[str]:
    """Vercel builds from GitHub, so GitHub has to have the commit first (D12).

    A push that does not land is not fatal here -- the sha may already be up
    there from an earlier one -- but it is said out loud, because "Vercel cannot
    find that commit" is a baffling error to receive second-hand.
    """
    if not row["github_repo"]:
        return [
            "this app has no GitHub repository, so Vercel gets an upload of the "
            "built files instead of a commit. Connect GitHub for deployments "
            "that can be traced back to a sha."
        ]
    report = push_app(paths, conn, row)
    if report.pushed:
        return []
    snapshot = find_snapshot(conn, str(row["id"]), commit.sha)
    if snapshot is not None and snapshot["pushed_at"]:
        return []
    raise DeployRefused(
        f"{commit.sha[:12]} is not on {row['github_repo']} and the push did not "
        f"land, so Vercel has nothing to build: {report.reason}",
        code="not-pushed",
        repo=row["github_repo"],
    )


def _workdir(
    paths: ApplacePaths, root: Path, sha: str, app: str, stack: Stack
) -> tuple[Path, bool]:
    """Where to build. The app itself for a clean HEAD, a worktree otherwise.

    The dirty case is the one that matters. Building in place would quietly
    deploy whatever the agent happens to have half-written -- the working tree,
    not the commit -- and the difference only shows up later, in production,
    as a page nobody can explain from the sha.
    """
    head = gitrepo.head(root)
    if head.sha == sha and not gitrepo.is_dirty(root):
        return root, False

    destination = paths.work / f"{app}-{sha[:12]}"
    if destination.exists():
        gitrepo.remove_worktree(root, destination)
    gitrepo.add_worktree(root, sha, destination)
    _lend_modules(root, destination, stack)
    return destination, True


def _lend_modules(root: Path, destination: Path, stack: Stack) -> None:
    """Give the worktree the app's dependencies, or install its own.

    Symlinking the installed tree makes a rollback take seconds instead of
    minutes, and it is only honest while the two manifests are byte-identical.
    When they are not, the old commit wanted different dependencies and gets an
    install of its own -- which is the whole reason the manifest is compared.
    """
    modules = root / stack.modules
    if not modules.is_dir():
        return
    manifest = root / stack.manifest
    older = destination / stack.manifest
    if manifest.is_file() and older.is_file() and manifest.read_bytes() == older.read_bytes():
        (destination / stack.modules).symlink_to(modules, target_is_directory=True)
        return
    shell.run(stack.commands["install"], cwd=destination, timeout=INSTALL_TIMEOUT)


def _build(request: Request) -> Outcome | None:
    """Run the stack's build in the workdir. None means it went green.

    The env values go into the build's process and nowhere else (D8): the log
    this writes is a file a human opens, and the Outcome is something an agent
    reads, so neither of them may carry a value.
    """
    result = shell.run(
        request.stack.commands["build"],
        cwd=request.workdir,
        env=request.variables,
        timeout=BUILD_TIMEOUT,
    )
    request.log.write_text(
        f"$ {result.command}\n{result.output}\n", encoding="utf-8"
    )
    if result.ok:
        dist = request.workdir / request.stack.dist
        if (dist / "index.html").is_file():
            return None
        return Outcome(
            status="failed",
            detail={"stage": "build", "expected": str(dist)},
            message=(
                f"the build went green but left no {request.stack.dist}/index.html "
                f"to serve. Check what the {request.stack.name} stack builds."
            ),
        )
    return Outcome(
        status="failed",
        detail={"stage": "build", "log": _tail(result.output)},
        message=(
            f"`{result.command}` failed, so there is nothing to deploy. This is "
            f"the production build, which is stricter than the dev server."
        ),
    )


def _replace(dist: Path, served: Path) -> None:
    """Move the built files to where they are served from, atomically enough."""
    served.parent.mkdir(parents=True, exist_ok=True)
    if served.exists():
        shutil.rmtree(served)
    shutil.copytree(dist, served)


def _detail(row: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row["detail_json"] or "{}"))
    except (ValueError, TypeError):  # pragma: no cover - we wrote it ourselves
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _previous_commit(conn: Connection, row: Any, live: Any) -> str | None:
    """The last commit that was live before the current one, if there was one."""
    current = str(live["commit_sha"]) if live is not None else None
    for deployment in list_deployments(conn, str(row["id"]), limit=50):
        if str(deployment["status"]) not in ("live", "stopped"):
            continue
        sha = str(deployment["commit_sha"])
        if sha != current:
            return sha
    return None


def _free_port(paths: ApplacePaths, conn: Connection, slug: str) -> int:
    """A port no preview and no other deployment on this machine is holding.

    Under a root, "this machine" means every home on it, and the ledger is what
    makes that true rather than hopeful (D21).
    """
    taken = claimed_ports(conn) | {
        int(detail["port"])
        for detail in (_detail(row) for row in live_deployments(conn))
        if isinstance(detail.get("port"), int)
    }
    candidates = [port for port in PORT_RANGE if port not in taken]
    brokered = machine.claim(
        paths, candidates, slug=slug, kind="deploy", free=preview.port_is_free
    )
    if brokered is not None:
        return brokered
    if paths.ledger is None:
        for candidate in candidates:
            if preview.port_is_free(candidate):
                return candidate
    raise DeployError(
        f"no free port between {PORT_RANGE.start} and {PORT_RANGE.stop - 1}. "
        f"Stop a deployment before starting another."
    )


def _wait_until_ready(url: str, pid: int) -> bool:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if preview.responds(url):
            return True
        if preview.process_command(pid) is None:
            return False
        time.sleep(READY_INTERVAL)
    return False


def _tail(output: str, lines: int = LOG_TAIL) -> str:
    kept = [line for line in output.splitlines() if line.strip()][-lines:]
    return "\n".join(kept) or "(the build printed nothing)"


__all__ = [
    "ADAPTERS",
    "ENVIRONMENTS",
    "LOCAL",
    "PREVIEW",
    "PRODUCTION",
    "TARGETS",
    "VERCEL",
    "Adapter",
    "DeployError",
    "DeployRefused",
    "Outcome",
    "Report",
    "Request",
    "describe",
    "gate",
    "rollback",
    "run",
    "stop",
]
