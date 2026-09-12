"""Noticing that a human has been in the repository (D1).

An app is an ordinary git repository a human is invited to open (D1), which
means the interesting failure of this harness is not a bad build -- the gate
catches those -- but an agent quietly writing over work that a person did by
hand. Applace can tell the two apart because it records every commit it makes
itself: a commit in ``git log`` that is not in ``snapshots`` was made by
somebody else, and a dirty working tree that the last gate does not explain is
somebody else's edit.

What Applace does about each is different, and deliberately so.

**A human's commits are news, not a problem.** They are in the history, they
will be pushed with everything else, and an agent that knows about them can read
the files before it changes them. They are reported and nothing more.

**A human's uncommitted edits are a refusal.** ``write_files`` replaces whole
files; writing one that somebody is in the middle of editing destroys work that
exists nowhere else -- not in git, not in a snapshot, not in the model's
context. So the write is refused for those paths, and the way out is a human's
decision (commit it, or throw it away), not the agent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json

from . import gitrepo
from .db import Connection, list_snapshots, recent_gates

# The two porcelain codes that mean "this file is not in git at all yet": an
# untracked file and one that was added but never committed. Everything else in
# a porcelain line is a modification of something git already knows.
UNTRACKED = ("??", "A ", "AM")

REFUSAL = (
    "{count} of these files have changes that are not committed and that Applace "
    "did not make: {paths}. Somebody is editing this app by hand, and "
    "`write_files` replaces whole files -- writing them now would destroy work "
    "that exists nowhere else. Tell the human what you were about to change and "
    "ask them to commit or discard theirs; `applace check {app}` commits them "
    "once they build."
)


class HumanEdits(Exception):
    """A write would have landed on top of somebody's uncommitted work."""

    def __init__(self, message: str, paths: list[str]) -> None:
        super().__init__(message)
        self.paths = paths


@dataclass(frozen=True)
class Handover:
    """What a human has done to this app that Applace did not do."""

    commits: list[dict[str, str]] = field(default_factory=list)
    edits: list[str] = field(default_factory=list)
    # True when part of why this tree is dirty is the agent's own red write
    # (D4). Those files are left on disk on purpose and are excluded from
    # `edits`: the agent's mess is not a human's work in progress.
    agent_is_dirty: bool = False

    @property
    def touched(self) -> bool:
        return bool(self.commits or self.edits)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "commits": self.commits,
            "edits": self.edits,
            "touched": self.touched,
        }
        if self.touched:
            out["note"] = self.note
        return out

    @property
    def note(self) -> str:
        parts: list[str] = []
        if self.commits:
            listed = ", ".join(
                f"{commit['sha'][:12]} {commit['message']}" for commit in self.commits
            )
            parts.append(
                f"A human made {_plural(len(self.commits), 'commit')} in this "
                f"repository that Applace did not: {listed}. They are part of the "
                f"app now -- read the files before you change them."
            )
        if self.edits:
            parts.append(
                f"A human has uncommitted changes in "
                f"{_plural(len(self.edits), 'file')}: {', '.join(self.edits)}. Do "
                f"not write those files. Say so to the human and let them commit "
                f"or discard first."
            )
        return " ".join(parts)


def survey(conn: Connection, row: Any) -> Handover:
    """What a human has done here since Applace last looked.

    Cheap enough to run inside ``get_app``: two git commands and one query.
    """
    root = Path(str(row["path"]))
    if not root.is_dir() or not gitrepo.has_commits(root):
        return Handover()
    ours = {str(snapshot["commit_sha"]) for snapshot in list_snapshots(conn, str(row["id"]))}
    commits = [
        {"sha": commit.sha, "message": commit.message}
        for commit in gitrepo.log(root, limit=50)
        if commit.sha not in ours
    ]
    theirs = _files_the_agent_left_red(conn, str(row["id"]))
    edits = [path for path in _edited(root) if path not in theirs]
    return Handover(commits=commits, edits=edits, agent_is_dirty=bool(theirs))


def guard(conn: Connection, row: Any, targets: list[str]) -> None:
    """Refuse a write that would land on somebody's uncommitted work.

    Only the paths actually being written are checked. An agent editing
    ``src/Chart.tsx`` while a human edits ``README.md`` is not a conflict, and a
    harness that stopped for it would be one nobody leaves running.
    """
    if not targets:
        return
    found = survey(conn, row)
    if not found.edits:
        return
    collisions = sorted(set(found.edits) & {target.strip("/") for target in targets})
    if not collisions:
        return
    raise HumanEdits(
        REFUSAL.format(
            count=len(collisions),
            paths=", ".join(collisions),
            app=str(row["slug"]),
        ),
        collisions,
    )


def _edited(root: Path) -> list[str]:
    """Paths with uncommitted changes, ignoring what is not in git yet.

    A new file cannot be somebody's overwritten work -- there is nothing there to
    destroy -- and treating every stray untracked file as a handover would refuse
    writes in an app where a human once ran a script.
    """
    out: list[str] = []
    for line in gitrepo.status(root):
        code, _, path = line[:2], line[2:3], line[3:]
        if code in UNTRACKED or not path:
            continue
        # A rename reads "R  old -> new"; the file at risk is the new one.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append(path.strip('"'))
    return sorted(out)


def _files_the_agent_left_red(conn: Connection, app_id: str) -> set[str]:
    """Paths the agent wrote that no green gate has committed yet (D4).

    A red write leaves its files on disk deliberately -- the agent has to see
    its own broken code to fix it -- so those paths are dirty for a reason that
    has nothing to do with a human, and reporting them as somebody's work in
    progress would refuse the very write that fixes them. Every red gate since
    the last green one counts, not just the last, because an agent that broke
    two files in two calls still owns both.
    """
    out: set[str] = set()
    for gate in recent_gates(conn, app_id):
        if bool(gate["ok"]):
            break
        out.update(json.loads(str(gate["written_json"] or "[]")))
    return out


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


__all__ = ["Handover", "HumanEdits", "guard", "survey"]
