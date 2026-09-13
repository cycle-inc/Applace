"""The front ends that already exist (D26).

Every other way into Applace starts with an empty directory. This one does not:
a company has front ends already, written before Applace was installed, and the
way in cannot be "rewrite it here". ``take`` points Applace at a repository that
exists -- a checkout on the developer's disk or a URL to clone -- recognises
which stack can build it, and records a row. From that moment the app has a
journal, a gate, previews and deploys like any other.

Three rules give the module its shape.

**Nothing is written into the tree.** No config key, no marker file, no import.
D1 says an app must not be able to tell that Applace exists, and an adopted app
is the case that proves it: the only evidence of the take-over is a row in a
database somewhere else, and deleting that row leaves the repository exactly as
it was found.

**Recognition, never a guess.** A stack says what it looks like in its own
``stack.yaml``; a tree that matches none of them is refused, naming what was
looked for. Guessing would mean building somebody's Next.js app with Vite and
handing them the error as if it were theirs.

**The first gate is a report, not a verdict.** A codebase that is already red is
taken over anyway and told so. Refusing red would be refusing every real
codebase in the building -- and because that gate must not commit a tree it has
just met, it runs with ``commit=False``.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import files, gate, gitrepo, policy
from .db import Connection, find_app, insert_app, insert_snapshot
from .db import list_apps as db_list_apps
from .gate import GateReport
from .naming import display_name, slugify
from .paths import ApplacePaths
from .stacks import Stack, registry, resolve

# What a lockfile says about the tool that wrote it. The stack's own `install`
# command is the one that will run; when the two disagree the take-over says so
# and carries on, because a warning about resolution is not a reason to refuse
# somebody's repository.
MANAGERS = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "bun.lock": "bun",
}

# How a source is read as a URL rather than as a path on this disk.
_REMOTE_MARKS = ("://", "git@")


class TakeError(Exception):
    """This cannot be taken over, and the message says what was looked for."""


@dataclass(frozen=True)
class Recognised:
    """Which stack can build this tree, and what made us say so."""

    stack: Stack
    matched: tuple[str, ...]


@dataclass
class TakeReport:
    slug: str
    name: str
    path: Path
    stack: str
    # Where that stack came from, for the row: `builtin`, or the URL a company
    # stack was installed from. Not reported -- nobody adopting an app is asking
    # about the stack's own provenance -- but the column means something.
    stack_source: str
    source: str
    cloned: bool
    commit: str | None
    branch: str
    commits: int
    files: int
    recognised_by: list[str]
    dry_run: bool = False
    # False on a dry run, and on nothing else: it is the answer to "is this an
    # app now".
    taken: bool = False
    remote: str | None = None
    warnings: list[str] = field(default_factory=list)
    # The first gate (D26.3). None when the caller asked for no check, or on a
    # dry run, where running npm install would be a change to somebody's disk.
    gate: GateReport | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "app": self.slug,
            "name": self.name,
            "path": str(self.path),
            "stack": self.stack,
            "source": self.source,
            "cloned": self.cloned,
            "commit": self.commit,
            "branch": self.branch,
            "commits": self.commits,
            "files": self.files,
            "recognised_by": self.recognised_by,
            "dry_run": self.dry_run,
            "taken": self.taken,
            "remote": self.remote,
            "warnings": self.warnings,
        }
        if self.gate is not None:
            out["gate"] = self.gate.as_dict()
        return out


def recognise(root: Path, available: dict[str, Stack]) -> Recognised:
    """Which installed stack claims this tree, from its manifest (D26).

    A stack is recognised when every dependency it named in ``recognise:`` is in
    the manifest. The most specific claim wins -- a company stack that looks for
    its own design system beats the generic one it is built on -- and two stacks
    with an equally good claim are a refusal, not a coin toss.
    """
    manifests = {stack.manifest for stack in available.values()} or {"package.json"}
    declared: set[str] = set()
    present = [name for name in sorted(manifests) if (root / name).is_file()]
    for name in present:
        declared |= {dependency for _section, dependency in files.read_dependencies(root, name)}
    if not present:
        raise TakeError(
            f"{root} has no {' and no '.join(sorted(manifests))}, so nothing here "
            f"says what this front end is built from. Applace takes over "
            f"repositories, not directories."
        )

    claims = [
        Recognised(stack=stack, matched=stack.recognise)
        for stack in available.values()
        if stack.recognise and set(stack.recognise) <= declared
    ]
    if not claims:
        raise TakeError(_nothing_matched(present, declared, available))
    best = max(len(claim.matched) for claim in claims)
    winners = sorted(
        (claim for claim in claims if len(claim.matched) == best),
        key=lambda claim: claim.stack.name,
    )
    if len(winners) > 1:
        names = ", ".join(claim.stack.name for claim in winners)
        raise TakeError(
            f"{names} all recognise this app and none of them is more specific. "
            f"Name the one to use with --stack."
        )
    return winners[0]


def _nothing_matched(
    manifests: list[str], declared: set[str], available: dict[str, Stack]
) -> str:
    """The refusal, naming what was looked for. D26: refuse, do not repair."""
    looked = [
        f"{stack.name} looks for {' + '.join(stack.recognise)}"
        for stack in sorted(available.values(), key=lambda s: s.name)
        if stack.recognise
    ]
    what = "; ".join(looked) or "no installed stack says what it looks like"
    shown = ", ".join(sorted(declared)[:12]) or "nothing"
    return (
        f"no installed stack recognises this app. {' and '.join(manifests)} "
        f"declares {shown}. {what}. Install the stack this app is built on "
        f"(`applace stacks add`), or name one with --stack -- Applace will not "
        f"guess which toolchain built somebody's repository."
    )


def take(
    paths: ApplacePaths,
    conn: Connection,
    *,
    source: str,
    name: str | None = None,
    stack_name: str | None = None,
    check: bool = True,
    dry_run: bool = False,
    token: str | None = None,
) -> TakeReport:
    """Adopt a front end that already exists: a row, never a file (D26).

    ``source`` is a directory on this machine or a URL to clone. A local
    checkout is recorded where it is -- moving somebody's working directory
    under ``~/.applace/apps`` would break every path, editor and shell they have
    open on it -- and only a clone lands in the home.
    """
    slug, title = _naming(source, name)
    if not dry_run and find_app(conn, slug) is not None:
        raise TakeError(
            f"an app called {slug!r} already exists here. Take it over under "
            f"another name with --name."
        )

    scratch: Path | None = None
    try:
        if _is_url(source):
            destination = paths.app(slug)
            if dry_run:
                scratch = Path(tempfile.mkdtemp(prefix="applace-take-"))
                destination = scratch / slug
            elif destination.exists() and any(destination.iterdir()):
                raise TakeError(
                    f"{destination} already exists and is not empty, but no app "
                    f"called {slug!r} is registered. Move it aside or use --name."
                )
            gitrepo.clone(source, destination, token=token)
            root, cloned = destination, True
        else:
            root, cloned = _local(conn, source), False

        report = _describe(paths, conn, root, source, slug, title, cloned, stack_name)
        if dry_run:
            report.dry_run = True
            if scratch is not None:
                # The clone it inspected is about to be deleted; where it *would*
                # live is the useful answer.
                report.path = paths.app(slug)
            return report

        _record(conn, root, report)
        if check:
            # A report, not a verdict (D26.3): whatever it says, the app is
            # taken. `commit=False` because this tree's history is not ours to
            # add to before anybody has asked for a change -- and because a
            # green gate would otherwise commit edits a person left behind.
            report.gate = gate.write_files(paths, conn, app=report.slug, commit=False)
        return report
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _naming(source: str, name: str | None) -> tuple[str, str]:
    """The slug and the title, from ``--name`` or from the source itself."""
    raw = name or _name_from(source)
    return slugify(raw), display_name(raw)


def _name_from(source: str) -> str:
    tail = source.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    # `git@github.com:acme/board` has no slash before the org on some remotes.
    return tail.rsplit(":", 1)[-1] or source


def _is_url(source: str) -> bool:
    return any(mark in source for mark in _REMOTE_MARKS)


def _local(conn: Connection, source: str) -> Path:
    """The checkout on this disk, refused unless it is a repository with history."""
    root = Path(source).expanduser()
    if not root.is_dir():
        raise TakeError(
            f"{root} is not a directory on this machine. Give a path to a "
            f"checkout, or a URL to clone."
        )
    root = root.resolve()
    if not (root / ".git").exists():
        raise TakeError(
            f"{root} is not a git repository, and Applace keeps every version of "
            f"an app as a commit (D2). Run `git init` and commit what is there, "
            f"then take it over."
        )
    if not gitrepo.has_commits(root):
        raise TakeError(
            f"{root} is a repository with no commits yet. Commit what is there "
            f"first: the take-over adopts a history, and there is none."
        )
    for row in db_list_apps(conn):
        if Path(str(row["path"])) == root:
            raise TakeError(f"{root} is already the app {str(row['slug'])!r}.")
    return root


def _describe(
    paths: ApplacePaths,
    conn: Connection,
    root: Path,
    source: str,
    slug: str,
    title: str,
    cloned: bool,
    stack_name: str | None,
) -> TakeReport:
    """Everything the take-over knows before it writes a row -- and a dry run."""
    if stack_name is not None:
        # A human overriding recognition is a human who knows something the
        # manifest does not say. Still resolved through the registry, so an
        # unknown name fails here rather than at the first build.
        found = Recognised(stack=resolve(paths.stacks, stack_name), matched=())
    else:
        found = recognise(root, registry(paths.stacks))
    stack = found.stack

    tracked = gitrepo.tracked_files(root)
    _boundary(root, tracked)
    report = TakeReport(
        slug=slug,
        name=title,
        path=root,
        stack=stack.name,
        stack_source=stack.source,
        source=source,
        cloned=cloned,
        commit=gitrepo.head(root).sha,
        branch=gitrepo.current_branch(root),
        commits=gitrepo.count_commits(root),
        files=len(tracked),
        recognised_by=list(found.matched) if found.matched else ["--stack"],
    )
    report.remote = gitrepo.remote_url(root)
    report.warnings = _warnings(paths, root, stack)
    return report


def _boundary(root: Path, tracked: list[str]) -> None:
    """The path lint, over a tree nobody wrote for Applace (D26).

    Only the boundary half of it: a repository that tracks its own lockfile is
    entirely normal, while the agent that may not *write* one is a different
    question, answered by the same lint at write time. What is refused here is a
    file Applace could never address -- a link out of the app -- because an app
    with one is an app whose gate reads somebody else's disk.
    """
    escaped = [
        relative
        for relative in tracked
        if (link := root / relative).is_symlink()
        and not _within(link.resolve(), root.resolve())
    ]
    if escaped:
        raise TakeError(
            f"{escaped[0]} is a link out of the repository, so this app's files "
            f"are not all inside it. Applace cannot take over a tree it cannot "
            f"draw a boundary around; replace the link with a file."
        )


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _warnings(paths: ApplacePaths, root: Path, stack: Stack) -> list[str]:
    """What this machine will do to the app that its authors did not choose.

    None of these refuse the take-over. They are the difference between the
    repository as it is and the repository as Applace will treat it, said once,
    at the moment somebody can still decide not to.
    """
    out: list[str] = []
    lock = next((name for name in MANAGERS if (root / name).is_file()), None)
    manager = stack.commands.get("install", "").split(" ", 1)[0]
    # Only when the stack installs with a manager we recognise: a stack whose
    # install command is `make bootstrap` is not contradicting anything, and
    # inventing a disagreement out of not understanding the command is worse
    # than saying nothing.
    if lock is not None and manager in set(MANAGERS.values()) and MANAGERS[lock] != manager:
        out.append(
            f"this repository has a {lock}, and the {stack.name} stack installs "
            f"with `{manager}`. The gate will use {manager}, which may resolve "
            f"different versions than the lockfile pins."
        )
    untidy = [
        name
        for name in (stack.dist, stack.modules)
        if name and not gitrepo.ignores_directory(root, name)
    ]
    if untidy:
        out.append(
            f"{' and '.join(untidy)} are not ignored by this repository, so once "
            f"the gate has built it `git status` will list them. Applace writes "
            f"no .gitignore into a tree it took over; a human may want to."
        )
    if not (root / stack.modules).is_dir():
        out.append(
            f"{stack.modules}/ is not there, so the first gate installs "
            f"dependencies and will take minutes rather than seconds."
        )
    try:
        rules = policy.load(paths)
    except policy.PolicyError as exc:
        out.append(f"this machine's policy does not parse, and every gate will refuse: {exc}")
        return out
    existing = files.diff_dependencies({}, files.read_dependencies(root, stack.manifest))
    refused = rules.check(existing)
    if refused:
        names = ", ".join(refusal.name for refusal in refused)
        out.append(
            f"{names} would be refused by this machine's policy if an agent "
            f"added them. They are already in the manifest and the take-over "
            f"does not remove them, but a human may want to know."
        )
    return out


def _record(conn: Connection, root: Path, report: TakeReport) -> None:
    """The whole of what a take-over writes: one row, and the history it found.

    The commit the repository was already on becomes the app's first snapshot,
    so `applace log`, `deploy` and `rollback` have something to point at from
    the first second -- and so that nothing has to pretend Applace made it.
    """
    app_id = uuid.uuid4().hex
    insert_app(
        conn,
        app_id=app_id,
        slug=report.slug,
        name=report.name,
        description=f"Taken over from {report.source}",
        stack=report.stack,
        stack_source=report.stack_source,
        stack_commit=None,
        path=str(root),
        taken_from=report.source,
    )
    head = gitrepo.head(root)
    insert_snapshot(
        conn,
        snapshot_id=uuid.uuid4().hex,
        app_id=app_id,
        commit_sha=head.sha,
        message=head.message,
    )
    conn.commit()
    report.taken = True


__all__ = ["Recognised", "TakeError", "TakeReport", "recognise", "take"]
