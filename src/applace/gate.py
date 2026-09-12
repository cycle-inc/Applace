"""The compiler (D3, D4).

`write_files` is not a file writer. It is a compiler whose source happens to be
whatever the agent just wrote, and it runs, in order:

    paths -> install (only if the manifest moved) -> typecheck -> lint -> build

It stops at the first red stage, because the ones after it would only report
consequences of the same mistake. What comes back is the failing stage's own
diagnostics, parsed to `{file, line, column, message}`.

Green commits (D4). Red does not, and the files stay on disk: the agent needs to
see its own broken code to fix it, and `get_app` says `dirty: true` with the
outstanding errors until it does. That asymmetry is the whole design -- the
repository's history is a list of states that built, with nothing else in it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import diagnostics, files, gitrepo, shell
from .apps import INSTALL_TIMEOUT, require_app
from .db import Connection, insert_gate, insert_snapshot
from .diagnostics import Diagnostic
from .files import PathRefused
from .paths import ApplacePaths
from .shell import CommandNotFound
from .stacks import Stack, resolve

# In order. `install` is conditional and the two middle stages are optional --
# a stack that declares neither still gets a build, which is the real gate.
PIPELINE = ("install", "typecheck", "lint", "build")

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
    commit: str | None = None
    committed: bool = False
    dirty: bool = False
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
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


def write_files(
    paths: ApplacePaths,
    conn: Connection,
    *,
    app: str,
    files_to_write: dict[str, str] | None = None,
    delete: list[str] | None = None,
    message: str | None = None,
) -> GateReport:
    """Write source into an app and take it through the gate.

    With no files this is a plain re-check of whatever is on disk, which is what
    ``applace check`` is and what an agent should call after a human edited the
    repository by hand.
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

    _run_pipeline(report, root, stack, manifest_moved=before != after)
    report.duration_ms = sum(stage.duration_ms for stage in report.stages)

    snapshot_id: str | None = None
    if report.ok:
        snapshot_id = _snapshot(conn, row, root, report, message)
        report.committed = snapshot_id is not None
    report.dirty = gitrepo.is_dirty(root)
    _journal(conn, row, report, snapshot_id=snapshot_id)
    conn.commit()
    return report


def _run_pipeline(
    report: GateReport, root: Path, stack: Stack, *, manifest_moved: bool
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
            result = shell.run(command, cwd=root, timeout=timeout)
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
