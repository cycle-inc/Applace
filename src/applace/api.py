"""Applace, called from Python (D17).

The MCP server is what an agent talks to. The CLI is what a human at a terminal
talks to. This is what the developer who deployed it talks to -- the same store,
the same gate and the same journal, from inside their own process. It exists
because of where Applace actually sits: not beside a person in a shell, but
under a chatbot their users are asking for an internal tool.

That developer has to be able to do two kinds of thing, and the line between
them is the whole design of this module. One half is what an agent does, method
for tool and answer for answer -- the MCP server is a skin over it, so the two
doors cannot drift. The other half is what an agent must never do (D19):
supplying the *value* of a secret, connecting this machine to a GitHub
organisation, a Vercel account or a company stack. Those are here and are not
tools, for the same reason Runlace's `approve` is not one.

Every method returns a plain dict and none of them raise because a build went
red: `ok`, `code`, `error` and `hint` are the same fields whether a model reads
them as text or a web handler turns them into a response. Exceptions are for the
caller getting the API wrong, not for an app getting its TypeScript wrong.

Everything is synchronous (D18). The slow calls here are subprocesses -- npm,
tsc, vite, a browser -- so an async backend offloads them where it knows its own
event loop::

    from applace import Applace

    ap = Applace()
    made = await asyncio.to_thread(ap.create, "Team Dashboard")
    built = await asyncio.to_thread(ap.write, made["app"], {"src/App.tsx": code})
    if not built["ok"]:
        ...                     # built["stage"], built["errors"]
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, ParamSpec

from . import apps, deploy, env, gate, github, machine, panel, policy, skill
from . import stackstore, sync, vercel
from .apps import AppError, AppExists
from .db import Connection, connect
from .db import list_apps as db_list_apps
from .deploy import LOCAL, PREVIEW, DeployError, DeployRefused
from .env import EnvError
from .eyes import DEFAULT_HEIGHT, DEFAULT_WIDTH, EyesUnavailable
from .github import DEFAULT_HOST, DEFAULT_VISIBILITY, DIRECT, REVIEWS, VISIBILITIES
from .github import GitHubError, GitHubLink
from .gitrepo import GitError
from .machine import Limits
from .naming import InvalidName
from .paths import ApplacePaths
from .paths import paths as default_paths
from .policy import PolicyError
from .preview import PreviewError
from .stacks import StackError, registry
from .stackstore import StackInstallError
from .vercel import VercelError, VercelLink

# Which failure is which, once, for every method. Order matters: the first
# class that matches wins, so a subclass is listed above the exception it
# refines. These are the codes the MCP tools already answer with, because a
# caller who moves from one door to the other should not have to relearn them.
CODES: tuple[tuple[type[Exception], str], ...] = (
    (AppExists, "app-exists"),
    (AppError, "unknown-app"),
    (InvalidName, "invalid-name"),
    (EnvError, "invalid-name"),
    (StackInstallError, "stack-install-failed"),
    (StackError, "unknown-stack"),
    (PreviewError, "preview-failed"),
    (EyesUnavailable, "eyes-unavailable"),
    (GitError, "git-failed"),
    (GitHubError, "github-failed"),
    (VercelError, "vercel-failed"),
    (PolicyError, "policy"),
    (DeployError, "deploy-failed"),
)

KNOWN = tuple(kind for kind, _ in CODES)

P = ParamSpec("P")


def failure(exc: Exception) -> dict[str, Any]:
    """One of Applace's own exceptions, as the answer a caller reads."""
    if isinstance(exc, DeployRefused):
        # It already knows why it refused, and why is the useful part.
        return exc.as_dict()
    for kind, code in CODES:
        if isinstance(exc, kind):
            return {"ok": False, "code": code, "error": str(exc)}
    raise exc  # pragma: no cover - KNOWN is built from the same table


def answered(method: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
    """Turn Applace's exceptions into Applace's answers (D17)."""

    @wraps(method)
    def call(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            return method(*args, **kwargs)
        except KNOWN as exc:
            return failure(exc)

    return call


class Applace:
    """One Applace home, called from Python.

    Safe to build once at process start and share: each call opens its own
    SQLite connection and closes it, so no connection travels between threads
    and no cursor's state is interleaved by two requests. That is the same
    arrangement the MCP server uses, and for the same reason -- a web framework
    will call this from wherever it likes.

        ap = Applace()                      # ~/.applace, or $APPLACE_HOME
        ap = Applace("/srv/applace")        # somewhere else

    `limits` is what one home may have and may do (D22). It is None here and on
    every single-user machine -- nothing is counted and nothing is refused --
    and it is set for you by :class:`applace.machine.Machine`, which is how a
    backend serving many people gets quotas without each caller remembering to
    ask for them.
    """

    def __init__(
        self,
        home: ApplacePaths | Path | str | None = None,
        *,
        limits: Limits | None = None,
    ) -> None:
        self.paths = _resolve(home)
        self.paths.create()
        self.limits = limits

    # -- what an agent does -------------------------------------------------

    @answered
    def skill(self) -> dict[str, Any]:
        """The document an agent is given, plus what this machine can do.

        The same answer as the `get_skill` tool, and the right system prompt for
        a chatbot with these tools bolted under it: it carries which stacks are
        installed, whether apps are pushed to GitHub, what the policy allows and
        where this machine can deploy. Hard-coding any of that into a prompt of
        your own means maintaining it twice.
        """
        return {"ok": True, **skill.describe(self.paths)}

    @answered
    def stacks(self) -> dict[str, Any]:
        """What an app can be made of on this machine."""
        return {
            "ok": True,
            "stacks": [
                stack.summary() for stack in registry(self.paths.stacks).values()
            ],
        }

    @answered
    def create(
        self, name: str, stack: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Create an app from a stack: rendered, installed and committed.

        Minutes, not seconds -- it installs dependencies. When it returns, the
        app already builds and `app` is the slug every other method takes.

        Under a root this is where a quota bites, before the install rather
        than after it (D22): `code` is then `quota`.
        """
        refused = self._over_quota("apps") or self._over_quota("disk_mb")
        if refused is not None:
            return refused
        with self._session() as conn:
            report = apps.create_app(
                self.paths, conn, name=name, stack_name=stack, description=description
            )
        return {"ok": True, **report.as_dict()}

    @answered
    def apps(self) -> dict[str, Any]:
        """Every app on this machine, with its state."""
        with self._session() as conn:
            return {
                "ok": True,
                "apps": [apps.app_summary(conn, row) for row in db_list_apps(conn)],
            }

    @answered
    def app(self, app: str) -> dict[str, Any]:
        """One app in full: state, file tree, env, deployments, what a human did."""
        with self._session() as conn:
            row = apps.require_app(conn, app)
            return {"ok": True, **apps.app_detail(self.paths, conn, row)}

    @answered
    def read(self, app: str, paths: list[str]) -> dict[str, Any]:
        """The app's own source, by repository-relative path."""
        with self._session() as conn:
            return {
                "ok": True,
                **gate.read(self.paths, conn, app=app, paths_to_read=paths),
            }

    @answered
    def write(
        self,
        app: str,
        files: dict[str, str] | None = None,
        delete: list[str] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Write whole files into an app, then type-check, lint and build it (D3).

        The compiler, not a file writer. `ok` true means it built and the change
        is a commit; `ok` false means nothing was committed, the files are still
        on disk, `stage` says which step failed and `errors` carries the real
        tool's diagnostics with file, line and column. Call it with no `files`
        to re-check an app that is dirty.
        """
        refused = self._over_rate("write")
        if refused is not None:
            return refused
        self._charge("write")
        with self._session() as conn:
            report = gate.write_files(
                self.paths,
                conn,
                app=app,
                files_to_write=files,
                delete=delete,
                message=message,
            )
        return report.as_dict()

    @answered
    def declare_env(
        self, app: str, name: str, description: str | None = None
    ) -> dict[str, Any]:
        """Record that the app needs a variable, by **name** (D8).

        The agent's half of a secret. The value is not an argument here and
        never will be: see :meth:`set_env`, which is the developer's half and is
        deliberately not a tool.
        """
        with self._session() as conn:
            return {
                "ok": True,
                **apps.declare_env(
                    self.paths, conn, app, name=name, description=description
                ),
            }

    @answered
    def preview(self, app: str) -> dict[str, Any]:
        """Run the app's dev server and get a URL. Idempotent.

        A home may only have so many running at once (D22); asking again for
        one that is already up costs nothing and is never refused.
        """
        if not self._previewing(app):
            refused = self._over_quota("previews")
            if refused is not None:
                return refused
        with self._session() as conn:
            return {"ok": True, **apps.start_preview(self.paths, conn, app).as_dict()}

    @answered
    def stop_preview(self, app: str) -> dict[str, Any]:
        """Stop the dev server. `stopped` false means nothing was running."""
        with self._session() as conn:
            return {"ok": True, "app": app, "stopped": apps.stop_preview(conn, app)}

    @answered
    def shot(
        self,
        app: str,
        route: str = "/",
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        full_page: bool = False,
    ) -> dict[str, Any]:
        """Look at a route: the picture, the console and what failed (D5).

        `png` is the image itself, as bytes -- put it in front of the model as
        an image if a model is asking, and on the page if a person is. A build
        that passes and a page that renders are two different facts, and the
        report is the half that says which one you have.

        Starts the preview if it is not running, and leaves it running.
        """
        refused = self._over_rate("shot")
        if refused is not None:
            return refused
        self._charge("shot")
        with self._session() as conn:
            shot, started = apps.screenshot(
                self.paths,
                conn,
                app,
                route=route,
                width=width,
                height=height,
                full_page=full_page,
            )
        return {
            "ok": True,
            "app": app,
            "started_preview": started,
            "png": shot.png,
            **shot.report(),
        }

    @answered
    def deploy(
        self,
        app: str,
        target: str = LOCAL,
        environment: str = PREVIEW,
        commit: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Build a commit and put it somewhere a person can open it.

        Production needs `confirm=True` and `confirm` means a human said yes
        (D6); without it the answer is `code: "confirm-required"`, and a policy
        that forbids production refuses it outright. What goes live is a commit,
        never the working tree.
        """
        refused = self._over_rate("deploy")
        if refused is not None:
            return refused
        self._charge("deploy")
        with self._session() as conn:
            report = apps.deploy_app(
                self.paths,
                conn,
                app,
                target=target,
                environment=environment,
                commit=commit,
                confirm=confirm,
            )
        return report.as_dict()

    @answered
    def rollback(
        self, app: str, commit: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Put an earlier commit back where this app is deployed."""
        with self._session() as conn:
            report = apps.rollback_app(
                self.paths, conn, app, commit=commit, confirm=confirm
            )
        return report.as_dict()

    # -- what only the deployment does (D19) --------------------------------

    @answered
    def set_env(self, app: str, name: str, value: str) -> dict[str, Any]:
        """Give a variable its **value**. Not a tool, and never will be (D8).

        This is the human's half of a secret, and in a product the human is your
        settings form. The value is written to `~/.applace/env/<app>.env` at
        `0600`, outside the repository, and is injected into the dev server and
        the build; it is never committed, never logged and never returned by
        anything here.

        The name is declared as well as set, because a person may be ahead of
        the agent and the agent has to be able to see that the value is there.
        """
        with self._session() as conn:
            row = apps.require_app(conn, app)
            slug = str(row["slug"])
            env.check_name(name)
            env.set_value(self.paths, slug, name, value)
            apps.declare_env(self.paths, conn, slug, name=name)
        return {
            "ok": True,
            "app": slug,
            "name": name,
            "stored": str(self.paths.env_file(slug)),
            "restart_preview": True,
        }

    @answered
    def env(self, app: str) -> dict[str, Any]:
        """The variables this app declares, and which ones have a value.

        Names, descriptions, `set` and `exposed` -- never a value. There is no
        method that returns one (D19).
        """
        with self._session() as conn:
            detail = apps.app_detail(self.paths, conn, apps.require_app(conn, app))
        return {"ok": True, "app": app, "env": detail.get("env") or []}

    @answered
    def unset_env(self, app: str, name: str) -> dict[str, Any]:
        """Remove a value. `removed` false means there was none."""
        with self._session() as conn:
            slug = str(apps.require_app(conn, app)["slug"])
        return {"ok": True, "app": slug, "name": name,
                "removed": env.unset_value(self.paths, slug, name)}

    @answered
    def connect_github(
        self,
        org: str,
        *,
        visibility: str = DEFAULT_VISIBILITY,
        prefix: str = "",
        team: str | None = None,
        host: str = DEFAULT_HOST,
        user: str | None = None,
        review: str = DIRECT,
        api_base: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Answer once where code goes; every app created afterwards follows it.

        Apps that already exist keep the remote they have (D2b). `review="pr"`
        puts every app's commits on `applace/<slug>` behind one open pull
        request, so nothing reaches the default branch without a person (D15).

        Making repositories **public** is the gated act, not creating them (D6):
        a policy of `deny` refuses it, and a policy of `confirm` needs
        `confirm=True` from you, because you are the one who can ask a human.
        """
        if visibility not in VISIBILITIES:
            return _wrong("visibility", visibility, VISIBILITIES)
        if review not in REVIEWS:
            return _wrong("review", review, REVIEWS)
        if visibility == "public":
            rule = policy.load(self.paths).public_repositories
            if rule == "deny":
                return {
                    "ok": False,
                    "code": "policy",
                    "error": f"{self.paths.policy} forbids public repositories "
                    f"on this machine.",
                    "hint": "Connect with visibility 'private' or 'internal'.",
                }
            if rule == "confirm" and not confirm:
                return {
                    "ok": False,
                    "code": "confirm-required",
                    "error": f"every app Applace creates in {org} would be a "
                    f"public repository, readable by anyone.",
                    "hint": "Ask a human, then pass confirm=True.",
                }
        link = GitHubLink(
            org=org,
            visibility=visibility,
            prefix=prefix,
            team=team,
            host=host,
            user=user,
            review=review,
            api_base=api_base,
        )
        github.save(self.paths, link)
        return {"ok": True, "connected": True, **link.as_dict()}

    @answered
    def github(self) -> dict[str, Any]:
        """What Applace would do with the next app, and where."""
        link = github.load(self.paths)
        if link is None:
            return {"ok": True, "connected": False}
        return {
            "ok": True, "connected": True, "reviewed": link.reviewed, **link.as_dict()
        }

    @answered
    def adopt(self, app: str, repo: str) -> dict[str, Any]:
        """Point an app at a repository that already exists, by `owner/name`."""
        with self._session() as conn:
            row = apps.require_app(conn, app)
            repository = sync.adopt_app(self.paths, conn, row, repo)
        return {"ok": True, "app": str(row["slug"]), **repository.as_dict()}

    @answered
    def push(self, app: str) -> dict[str, Any]:
        """Push what is committed. Refused, not forced, if the remote moved (D13)."""
        with self._session() as conn:
            row = apps.require_app(conn, app)
            report = sync.push_app(self.paths, conn, row)
        return {"ok": True, "app": str(row["slug"]), **report.as_dict()}

    @answered
    def connect_vercel(
        self, team: str | None = None, api_base: str | None = None
    ) -> dict[str, Any]:
        """Say which Vercel account ships this machine's apps.

        The token is not stored: it is read from `VERCEL_TOKEN` (or
        `APPLACE_VERCEL_TOKEN`) at every call, so a leaked home leaks nothing.
        """
        link = VercelLink(team=team, api_base=api_base)
        vercel.save(self.paths, link)
        return {"ok": True, "connected": True, **link.as_dict()}

    @answered
    def vercel(self) -> dict[str, Any]:
        """Whether this machine can deploy to Vercel, and as whom."""
        link = vercel.load(self.paths)
        if link is None:
            return {"ok": True, "connected": False}
        return {"ok": True, "connected": True, "api": link.api, **link.as_dict()}

    @answered
    def install_stack(
        self,
        url: str,
        *,
        name: str | None = None,
        ref: str | None = None,
        subdir: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Install a company stack from a git URL, pinned to the commit it came from."""
        return {"ok": True, **stackstore.add(
            self.paths, url, name=name, ref=ref, subdir=subdir, force=force
        ).as_dict()}

    @answered
    def update_stack(self, name: str) -> dict[str, Any]:
        """Move an installed stack to the newest commit of its ref.

        This changes what **new** apps start from. Existing apps are never
        rewritten: their files are commits in their own repositories, which is
        where an agent's work and a person's both live.
        """
        return {"ok": True, **stackstore.update(self.paths, name).as_dict()}

    @answered
    def installed_stacks(self) -> dict[str, Any]:
        """Every stack available here, and where each one came from."""
        return {"ok": True, "stacks": stackstore.describe(self.paths)}

    @answered
    def drift(self) -> dict[str, Any]:
        """Apps whose stack has moved since they were born. A report, not an action."""
        with self._session() as conn:
            return {"ok": True, "apps": stackstore.drift(conn, self.paths)}

    @answered
    def policy(self) -> dict[str, Any]:
        """What this machine allows: dependencies, registry, exposure (D9)."""
        return {"ok": True, **policy.load(self.paths).as_dict()}

    @answered
    def deployments(self, app: str, limit: int = 5) -> dict[str, Any]:
        """What went out for this app, where, and when."""
        with self._session() as conn:
            row = apps.require_app(conn, app)
            return {
                "ok": True,
                "app": str(row["slug"]),
                "deployments": deploy.describe(conn, row, limit),
            }

    @answered
    def card(self, app: str) -> dict[str, Any]:
        """One app folded down to what a chat window draws (D16).

        The same card the panel serves at `/panel/apps/<slug>`: a state, a
        sentence, the errors, the links and the last screenshot. A product that
        renders its own UI should not have to call its own harness over a
        socket to get it.
        """
        with self._session() as conn:
            found = panel.card(self.paths, conn, apps.require_app(conn, app))
        return {"ok": True, **found}

    @answered
    def cards(self) -> dict[str, Any]:
        """Every app, folded down the same way."""
        with self._session() as conn:
            return {"ok": True, "apps": panel.cards(self.paths, conn)}

    @answered
    def remove(self, app: str, delete_files: bool = False) -> dict[str, Any]:
        """Forget an app here, and optionally delete its directory.

        Its GitHub repository is untouched: Applace does not delete a person's
        repositories, and a deleted app whose code is still on GitHub can be
        adopted back.
        """
        with self._session() as conn:
            path = apps.remove_app(self.paths, conn, app, delete_files=delete_files)
        return {"ok": True, "app": app, "path": str(path), "deleted": delete_files}

    # -- limits (D22) -------------------------------------------------------

    def _over_quota(self, what: str) -> dict[str, Any] | None:
        """Is this home already at its limit for `what`? None means go ahead.

        Counted at the moment of asking rather than tracked as a balance: the
        truth about how many apps a person has is the apps table, and a balance
        would be one more thing that can be wrong.
        """
        if self.limits is None:
            return None
        limit = getattr(self.limits, what)
        if limit is None:
            return None
        used = self._used(what)
        if used < limit:
            return None
        return {
            "ok": False,
            "code": "quota",
            "error": f"this home is at its limit of {limit} "
            f"{'MB of disk' if what == 'disk_mb' else what}.",
            "limit": limit,
            "used": used,
            "hint": {
                "apps": "Remove an app you are done with.",
                "previews": "Stop a preview before starting another.",
                "disk_mb": "Remove an app you are done with, with its files.",
            }[what],
        }

    def _previewing(self, app: str) -> bool:
        """Is this app already the one holding one of the home's preview slots?"""
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM previews JOIN apps ON apps.id = previews.app_id "
                "WHERE apps.slug = ?",
                (app,),
            ).fetchone()
        return row is not None

    def _used(self, what: str) -> int:
        if what == "disk_mb":
            return machine.disk_mb(self.paths.home)
        table = "apps" if what == "apps" else "previews"
        with self._session() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )

    def _over_rate(self, act: str) -> dict[str, Any] | None:
        """Has this home done too many of those this hour? None means go ahead."""
        if self.limits is None or self.paths.ledger is None:
            return None
        limit = self.limits.rate_for(act)
        if limit is None:
            return None
        used = machine.spent(self.paths, act)
        if used < limit:
            return None
        return {
            "ok": False,
            "code": "rate-limit",
            "error": f"this home has done {used} {act}s in the last hour, "
            f"which is its limit.",
            "limit": limit,
            "used": used,
            "window": "hour",
            "retry_after": machine.frees_in(self.paths, act),
            "hint": "Wait, or ask whoever runs this machine for more.",
        }

    def _charge(self, act: str) -> None:
        if self.limits is not None:
            machine.spend(self.paths, act)

    # ----------------------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[Connection]:
        conn = connect(self.paths.db)
        try:
            yield conn
        finally:
            conn.close()

    def __repr__(self) -> str:
        return f"Applace(home={str(self.paths.home)!r})"


def _wrong(what: str, given: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    """A bad argument, refused where it was passed rather than at the next run."""
    return {
        "ok": False,
        "code": "invalid-argument",
        "error": f"{what} must be one of {', '.join(allowed)}, not {given!r}.",
    }


def _resolve(home: ApplacePaths | Path | str | None) -> ApplacePaths:
    """Accept the three things a caller reasonably has in hand."""
    if home is None:
        return default_paths()
    if isinstance(home, ApplacePaths):
        return home
    return ApplacePaths(Path(home).expanduser())


__all__ = ["Applace"]
