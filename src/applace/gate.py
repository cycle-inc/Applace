"""The compiler (D3, D4).

`write_files` is not a file writer. It is a compiler whose source happens to be
whatever the agent just wrote, and it runs, in order:

    paths -> install (only if the manifest moved) -> typecheck -> lint -> build
          -> visit

It stops at the first red stage, because the ones after it would only report
consequences of the same mistake. What comes back is the failing stage's own
diagnostics, parsed to `{file, line, column, message}`.

The last stage is the one the others cannot do: the build is served and every
declared route is opened in a browser (D25). Four green stages and a white
screen is a program, not an app, and until M14 that shipped.

Green commits (D4). Red does not, and the files stay on disk: the agent needs to
see its own broken code to fix it, and `get_app` says `dirty: true` with the
outstanding errors until it does. That asymmetry is the whole design -- the
repository's history is a list of states that built, with nothing else in it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import apis, diagnostics, env, files, gitrepo, handover, policy, sensor, shell
from .apis import Reach
from .apps import INSTALL_TIMEOUT, require_app
from .db import Connection, insert_gate, insert_snapshot, unpushed
from .diagnostics import Diagnostic
from .files import PathRefused
from .handover import Handover, HumanEdits
from .paths import ApplacePaths
from .policy import PolicyError, Refusal
from .shell import CommandNotFound
from .stacks import Stack, resolve
from .sync import PushReport, push_app

# In order. `install` is conditional and the two middle stages are optional --
# a stack that declares neither still gets a build, which is the real gate.
PIPELINE = ("install", "typecheck", "lint", "build")

# What runs after it, in this module rather than in a stack's commands: a stack
# cannot be asked to own the browser, and a company that writes its own must not
# have to remember to add the check that makes the gate worth having.
VISIT = "visit"

# Set for every stage the gate runs, and for nothing else. A stack that wants to
# build differently when Applace is the one building reads it -- the built-in one
# turns sourcemaps on, so a console error names a `.tsx` file and a line instead
# of a minified chunk (D25). Vercel never sets it, so nothing shipped carries a
# map: a debugging aid for the machine that built it is not something to publish.
GATE_MARK = "APPLACE_GATE"

STAGE_TIMEOUT = 600.0

DEFAULT_MESSAGE = "Update {summary}"


@dataclass
class StageRun:
    name: str
    ok: bool
    duration_ms: int
    skipped: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"stage": self.name, "ok": self.ok}
        if self.skipped:
            out["skipped"] = True
            out["reason"] = self.reason
        else:
            out["duration_ms"] = self.duration_ms
        return out


@dataclass
class GateReport:
    app: str
    ok: bool
    stage: str
    errors: list[Diagnostic] = field(default_factory=list)
    stages: list[StageRun] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    new_deps: list[dict[str, str]] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)
    # Absolute URLs the write tried to call directly instead of declaring (D24).
    reached: list[Reach] = field(default_factory=list)
    # The gateway function this gate wrote or removed, when the declarations
    # moved. Reported because it is a file in the app's history that no agent
    # asked for, and an unexplained commit is the thing D1 exists to avoid.
    generated: str = ""
    # What the browser found at each declared route (D25). Empty when the visit
    # was skipped, which the `stages` entry says in words.
    visits: list[sensor.Visit] = field(default_factory=list)
    commit: str | None = None
    committed: bool = False
    dirty: bool = False
    duration_ms: int = 0
    # Filled by the push that follows a green commit (D2). None when the app has
    # no repository, which is the normal state until someone connects an org.
    push: PushReport | None = None
    # What a human has done to this repository that Applace did not do (M9).
    human: Handover | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {
            "app": self.app,
            "ok": self.ok,
            "stage": self.stage,
            "errors": [error.as_dict() for error in self.errors],
            "stages": [stage.as_dict() for stage in self.stages],
            "written": self.written,
            "deleted": self.deleted,
            "new_deps": self.new_deps,
            "commit": self.commit,
            "committed": self.committed,
            "dirty": self.dirty,
            "duration_ms": self.duration_ms,
        }
        if self.refused:
            out["refused_deps"] = [refusal.as_dict() for refusal in self.refused]
        if self.reached:
            out["undeclared_calls"] = [reach.as_dict() for reach in self.reached]
        if self.generated:
            out["generated"] = self.generated
        if self.visits:
            out["visited"] = [visit.as_dict() for visit in self.visits]
        if self.push is not None:
            out["github"] = self.push.as_dict()
        if self.human is not None and self.human.touched:
            out["human"] = self.human.as_dict()
        return out


def write_files(
    paths: ApplacePaths,
    conn: Connection,
    *,
    app: str,
    files_to_write: dict[str, str] | None = None,
    delete: list[str] | None = None,
    message: str | None = None,
    commit: bool = True,
) -> GateReport:
    """Write source into an app and take it through the gate.

    With no files this is a plain re-check of whatever is on disk, which is what
    ``applace check`` is and what an agent should call after a human edited the
    repository by hand.

    ``commit=False`` runs the same stages and stops before the commit and the
    push. It exists for the one caller that must not commit a green tree (D26):
    the first gate of an app Applace has just taken over, whose working tree may
    hold edits a person left there and whose history is not ours to add to
    before anyone has asked for a change.
    """
    row = require_app(conn, app)
    slug = str(row["slug"])
    root = Path(str(row["path"]))
    if not root.is_dir():
        raise gitrepo.GitError(
            f"{slug} is registered but {root} is gone. Remove it with "
            f"`applace rm {slug}` or restore the directory."
        )
    stack = resolve(paths.stacks, str(row["stack"]))

    to_write = files_to_write or {}
    to_delete = delete or []
    report = GateReport(app=slug, ok=False, stage="paths")

    # Stage 0. Everything is checked before anything is written, so a batch with
    # one bad path leaves the app exactly as it was.
    try:
        files.lint_paths(root, [*to_write, *to_delete])
    except PathRefused as exc:
        report.errors = [Diagnostic(message=str(exc))]
        report.dirty = gitrepo.is_dirty(root)
        _journal(conn, row, report, snapshot_id=None)
        return report

    # Stage 0b (M9). A whole-file write over somebody's uncommitted edit is the
    # one mistake this harness cannot undo: the previous content is not in git,
    # not in a snapshot and not in the model's context. It is refused here,
    # before anything is written, and the way out is a human's decision.
    try:
        handover.guard(conn, row, list(to_write))
    except HumanEdits as exc:
        report.stage = "handover"
        report.errors = [Diagnostic(message=str(exc), file=exc.paths[0])]
        report.dirty = gitrepo.is_dirty(root)
        report.human = handover.survey(conn, row)
        _journal(conn, row, report, snapshot_id=None)
        return report

    before = files.read_dependencies(root, stack.manifest)
    try:
        applied = files.apply_writes(root, to_write, to_delete)
    except PathRefused as exc:  # a directory named for deletion, caught late
        report.errors = [Diagnostic(message=str(exc))]
        report.dirty = gitrepo.is_dirty(root)
        _journal(conn, row, report, snapshot_id=None)
        return report
    report.written = applied.written
    report.deleted = applied.deleted

    after = files.read_dependencies(root, stack.manifest)
    report.new_deps = files.diff_dependencies(before, after)

    # D9, before the pipeline: a dependency the policy will not have is a
    # dependency this machine must not install, and `npm install` is the moment
    # it would run somebody's postinstall script.
    if not _check_policy(paths, report, root, stack):
        report.dirty = gitrepo.is_dirty(root)
        _journal(conn, row, report, snapshot_id=None)
        return report

    # D24, before the pipeline for the same reason D9 is: this is cheap, it is
    # about what the agent just wrote, and the answer changes what gets built --
    # the generated function has to exist before the build that bundles it.
    if not _check_apis(paths, conn, report, row, root, stack, to_write):
        report.dirty = gitrepo.is_dirty(root)
        _journal(conn, row, report, snapshot_id=None)
        return report

    _run_pipeline(
        report,
        root,
        stack,
        manifest_moved=before != after,
        environment=env.values_for(paths, slug),
    )
    # Only after a green build: there is nothing to open otherwise, and a
    # typecheck error reported twice -- once as itself, once as a blank page --
    # is a worse report than the same error reported once (D25).
    if report.ok:
        _check_visit(paths, conn, report, row, root, stack)
    report.duration_ms = sum(stage.duration_ms for stage in report.stages)

    snapshot_id: str | None = None
    if report.ok and commit:
        snapshot_id = _snapshot(conn, row, root, report, message)
        report.committed = snapshot_id is not None
    elif report.ok:
        report.commit = gitrepo.head(root).sha if gitrepo.has_commits(root) else None
    report.dirty = gitrepo.is_dirty(root)
    _journal(conn, row, report, snapshot_id=snapshot_id)
    conn.commit()
    # After the commit *and* after the journal. After the commit because a green
    # gate stages the whole tree, so a human's edits that built are history now
    # rather than something to warn about; after the journal because "is this
    # tree dirty because of the agent or because of a person" is answered by the
    # last gate, and the last gate is this one.
    report.human = handover.survey(conn, row)

    # After the journal, deliberately: a push is a fact about GitHub, not about
    # the gate, and a network that is down must not turn a green write red (D2).
    # A green re-check with nothing to commit still pushes, which is how an app
    # whose last push was refused catches up once the divergence is resolved.
    if (
        report.ok
        and commit
        and row["github_repo"]
        and (report.committed or unpushed(conn, str(row["id"])))
    ):
        report.push = push_app(paths, conn, require_app(conn, slug))
    return report


def _check_policy(
    paths: ApplacePaths, report: GateReport, root: Path, stack: Stack
) -> bool:
    """The D9 check. False means the gate is red before anything was run.

    A policy file that does not parse stops the write too. A company that wrote
    a policy and got a typo in it is better served by a loud refusal than by a
    machine that quietly allows everything.
    """
    try:
        rules = policy.load(paths)
    except PolicyError as exc:
        report.stage = "policy"
        report.errors = [Diagnostic(message=str(exc), file=str(paths.policy))]
        report.stages.append(StageRun("policy", ok=False, duration_ms=0))
        return False

    refused = rules.check(report.new_deps)
    misconfigured = rules.check_registry_config(root)
    if misconfigured is not None:
        refused.append(misconfigured)
    if refused:
        report.stage = "policy"
        report.refused = refused
        report.errors = [
            Diagnostic(
                message=(
                    f"{refusal.reason}. Build this with what the stack already "
                    f"has, or ask the human to allow it in {paths.policy}."
                ),
                file=refusal.where or stack.manifest,
            )
            for refusal in refused
        ]
        report.stages.append(StageRun("policy", ok=False, duration_ms=0))
        return False

    if not report.new_deps:
        report.stages.append(
            StageRun("policy", ok=True, duration_ms=0, skipped=True,
                     reason="the write added no dependencies")
        )
    else:
        report.stages.append(StageRun("policy", ok=True, duration_ms=0))
    return True


def _check_apis(
    paths: ApplacePaths,
    conn: Connection,
    report: GateReport,
    row: Any,
    root: Path,
    stack: Stack,
    written: dict[str, str],
) -> bool:
    """The D24 stage: no undeclared egress, and the function matches the truth.

    False means the gate is red. Two different things happen here and they are
    one stage on purpose -- both answer "does this app's outside world match
    what was declared", and an agent fixing one usually has to see the other.
    """
    declared = apis.declarations(conn, str(row["id"]))

    # A declaration the policy no longer allows. Checked here and not only when
    # it was made, because a company tightens `policy.yaml` long after the app
    # was built, and the app must stop building rather than keep shipping.
    rules = policy.load(paths)
    forbidden = [
        reason
        for api in declared
        if (reason := rules.refuse_api(api.name, api.host))
    ]
    if forbidden:
        report.stage = "apis"
        report.errors = [
            Diagnostic(message=f"{reason}. Undeclare it with forget_api, or ask "
                                f"the human to allow the host in {paths.policy}.")
            for reason in forbidden
        ]
        report.stages.append(StageRun("apis", ok=False, duration_ms=0))
        return False

    # The guide (D24): an absolute URL in a `fetch` is an agent that has not
    # been told about `declare_api` yet. Red, because a call with no credential
    # is a page that will not work, and finding that out at runtime is worse.
    report.reached = apis.host_lint(written, declared)
    if report.reached:
        report.stage = "apis"
        report.errors = [
            Diagnostic(
                message=(
                    f"this app may not call {reach.host} directly. "
                    f"{reach.hint}, so the credential stays out of the browser."
                ),
                file=reach.file,
                line=reach.line,
            )
            for reach in report.reached
        ]
        report.stages.append(StageRun("apis", ok=False, duration_ms=0))
        return False

    changed = _write_function(root, stack, declared)
    if changed:
        report.generated = changed
    report.stages.append(
        StageRun(
            "apis", ok=True, duration_ms=0,
            skipped=not declared and not changed,
            reason="the app declares no APIs",
        )
    )
    return True


def _write_function(root: Path, stack: Stack, declared: list[Any]) -> str:
    """Keep the generated function in step with the declarations.

    Returns the path it wrote or removed, or "" when nothing had to move. It is
    compared before writing so an unchanged declaration does not produce a
    commit on every gate -- a repository full of "Update the app" commits that
    changed one generated file is a history nobody can read.
    """
    target = root / apis.FUNCTION_PATH
    if not declared:
        if target.exists():
            target.unlink()
            _prune(target.parent, root)
            return apis.FUNCTION_PATH
        return ""
    if not stack.gateway:
        # Refused at declaration time, so reaching here means the stack changed
        # under an app that already had declarations. Say nothing and write
        # nothing: the drift belongs to M8's report, not to this gate.
        return ""
    wanted = apis.function_source(declared)
    if target.exists() and target.read_text(encoding="utf-8") == wanted:
        return ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(wanted, encoding="utf-8")
    return apis.FUNCTION_PATH


def _check_visit(
    paths: ApplacePaths,
    conn: Connection,
    report: GateReport,
    row: Any,
    root: Path,
    stack: Stack,
) -> None:
    """The D25 stage: the build is served and every declared route is opened.

    Runs only on a green build, and turns it red when a route throws, renders
    nothing, or does not show what it was declared to show. A skip is recorded
    as a skip with its reason -- nobody may mistake "not looked at" for "looked
    at and fine".
    """
    started = time.monotonic()
    try:
        rules = policy.load(paths)
    except PolicyError as exc:  # pragma: no cover - the policy stage refused first
        report.stages.append(
            StageRun(VISIT, ok=True, duration_ms=0, skipped=True, reason=str(exc))
        )
        return

    off = sensor.off_because(stack, rules.visit)
    if off:
        report.stages.append(
            StageRun(VISIT, ok=True, duration_ms=0, skipped=True, reason=off)
        )
        return

    routes = sensor.to_visit(conn, str(row["id"]))
    outcome = sensor.run(
        paths,
        conn,
        app_id=str(row["id"]),
        slug=str(row["slug"]),
        root=root,
        dist=root / stack.dist,
        routes=routes,
    )
    elapsed = int((time.monotonic() - started) * 1000)
    report.visits = outcome.visits
    if outcome.skipped:
        report.stages.append(
            StageRun(VISIT, ok=True, duration_ms=elapsed, skipped=True,
                     reason=outcome.reason)
        )
        return
    report.stages.append(StageRun(VISIT, ok=outcome.ok, duration_ms=elapsed))
    if outcome.ok:
        # `stage` is how far the gate got, and it got further than the build.
        report.stage = VISIT
        return
    report.ok = False
    report.stage = VISIT
    report.errors = outcome.errors


def _prune(directory: Path, root: Path) -> None:
    """Remove the directories a deleted generated file leaves behind."""
    while directory != root and directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        directory = directory.parent


def _run_pipeline(
    report: GateReport,
    root: Path,
    stack: Stack,
    *,
    manifest_moved: bool,
    environment: dict[str, str] | None = None,
) -> None:
    """Run the stages until one fails. Fills ``report`` in place."""
    for name in PIPELINE:
        command = stack.commands.get(name)
        if command is None:
            # Only `install`, `dev` and `build` are required of a stack; a stack
            # with no linter is a stack whose author decided that.
            report.stages.append(
                StageRun(name, ok=True, duration_ms=0, skipped=True,
                         reason="the stack declares no such command")
            )
            continue
        if name == "install" and not _needs_install(root, stack, manifest_moved):
            report.stages.append(
                StageRun(name, ok=True, duration_ms=0, skipped=True,
                         reason="dependencies are installed and the manifest did not change")
            )
            continue

        timeout = INSTALL_TIMEOUT if name == "install" else STAGE_TIMEOUT
        try:
            # The build needs the app's declared variables: a bundler inlines
            # them, so a build without them is a build of a different app (D8).
            result = shell.run(
                command,
                cwd=root,
                env={**(environment or {}), GATE_MARK: "1"},
                timeout=timeout,
            )
        except CommandNotFound as exc:
            report.stage = name
            report.errors = [Diagnostic(message=str(exc))]
            report.stages.append(StageRun(name, ok=False, duration_ms=0))
            return

        report.stages.append(StageRun(name, ok=result.ok, duration_ms=result.duration_ms))
        if result.ok:
            continue

        report.stage = name
        if result.timed_out:
            report.errors = [
                Diagnostic(
                    message=f"`{command}` was still running after {int(timeout)}s "
                            f"and was stopped."
                )
            ]
        else:
            report.errors = diagnostics.parse(name, result.output, root=root)
        return

    report.ok = True
    report.stage = "build"


def _needs_install(root: Path, stack: Stack, manifest_moved: bool) -> bool:
    """Install when the dependencies changed, or when there are none yet.

    Not on every write: `npm install` on an unchanged manifest is tens of
    seconds of nothing, and it would sit between the agent and every edit.
    """
    return manifest_moved or not (root / stack.modules).is_dir()


def _snapshot(
    conn: Connection, row: Any, root: Path, report: GateReport, message: str | None
) -> str | None:
    """Commit a green tree. Returns the snapshot id, or None if nothing moved."""
    if not gitrepo.is_dirty(root):
        # Green, but the agent wrote what was already there. There is nothing to
        # commit and that is not an error; report the commit the app is on.
        report.commit = gitrepo.head(root).sha if gitrepo.has_commits(root) else None
        return None
    commit = gitrepo.commit_all(root, message or _default_message(report))
    snapshot_id = uuid.uuid4().hex
    insert_snapshot(
        conn,
        snapshot_id=snapshot_id,
        app_id=str(row["id"]),
        commit_sha=commit.sha,
        message=commit.message,
    )
    report.commit = commit.sha
    return snapshot_id


def _default_message(report: GateReport) -> str:
    """A commit subject for an agent that did not write one."""
    touched = report.written + report.deleted
    if not touched:
        return "Update the app"
    if len(touched) == 1:
        return DEFAULT_MESSAGE.format(summary=touched[0])
    return DEFAULT_MESSAGE.format(summary=f"{len(touched)} files")


def _journal(
    conn: Connection, row: Any, report: GateReport, *, snapshot_id: str | None
) -> None:
    insert_gate(
        conn,
        gate_id=uuid.uuid4().hex,
        app_id=str(row["id"]),
        snapshot_id=snapshot_id,
        stage=report.stage,
        ok=report.ok,
        duration_ms=report.duration_ms,
        errors=[error.as_dict() for error in report.errors],
        new_deps=report.new_deps,
        written=report.written + report.deleted,
    )
    conn.commit()


def read(paths: ApplacePaths, conn: Connection, *, app: str, paths_to_read: list[str]) -> dict[str, Any]:
    """`read_files`: the app's own source, by relative path."""
    row = require_app(conn, app)
    root = Path(str(row["path"]))
    return {"app": str(row["slug"]), "files": files.read_files(root, paths_to_read)}


__all__ = ["GateReport", "StageRun", "read", "write_files"]
