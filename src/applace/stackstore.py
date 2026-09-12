"""Installing a company's own stacks, and keeping track of which one an app came from.

A stack is what makes this harness *a company's* Lovable rather than a generic
one: the design system, the API client, the lint rules and the conventions an
agent must not have to invent. So a stack is not a template Applace ships, it is
a git repository the company owns, cloned into ``~/.applace/stacks/`` and pinned
to the commit it was cloned at.

Three rules hold the rest of this module together.

**What is installed is a snapshot, and ``.applace-stack.yaml`` says which one.**
The clone's own ``.git`` is dropped: a stack directory is a pinned copy of a
repository, not a second working tree of it. Fixing a component means fixing it
where the company keeps it and running `applace stacks update` -- which is the
same discipline the apps get, where the git history that matters is each app's
own (D1).

**A stack is validated before it is installed.** The clone happens into a
temporary directory and only moves into place once :func:`stacks.load` accepts
it, because a broken ``stack.yaml`` in the registry is not one bad stack, it is
`applace stacks`, `list_stacks` and every `create_app` refusing to answer.

**Updating a stack never touches an app.** An app is its own repository with its
own committed copy of what it was born from (D1); rewriting those files from a
newer template would overwrite an agent's work and a human's, and there is no
merge in git for "the template moved". What an update changes is what the *next*
app is born from. Applace records the drift and says it out loud instead --
:func:`drift` is that sentence.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from . import gitrepo
from .db import Connection, list_apps
from .paths import ApplacePaths
from .stacks import BUILTIN, Stack, StackError, load, registry

PIN = ".applace-stack.yaml"
DEFAULT_REF = "HEAD"


class StackInstallError(StackError):
    """A stack could not be installed, updated or removed. The message says why."""


@dataclass(frozen=True)
class Pin:
    """Where a stack came from, and when."""

    source: str
    commit: str
    ref: str = DEFAULT_REF
    subdir: str = ""
    added_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = {
            "source": self.source,
            "commit": self.commit,
            "ref": self.ref,
            "added_at": self.added_at,
        }
        if self.subdir:
            data["subdir"] = self.subdir
        if self.updated_at:
            data["updated_at"] = self.updated_at
        return data


@dataclass(frozen=True)
class Installed:
    """The result of adding or updating a stack, as a human wants to read it."""

    name: str
    title: str
    source: str
    commit: str
    previous: str | None
    ref: str
    path: Path

    @property
    def changed(self) -> bool:
        return self.previous != self.commit

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "source": self.source,
            "commit": self.commit,
            "previous": self.previous,
            "ref": self.ref,
            "path": str(self.path),
            "changed": self.changed,
        }


def read_pin(root: Path) -> Pin | None:
    """The pin next to an installed stack, or None for a built-in one."""
    path = root / PIN
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise StackInstallError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not data.get("source"):
        raise StackInstallError(f"{path} does not say where the stack came from")
    return Pin(
        source=str(data["source"]),
        commit=str(data.get("commit") or ""),
        ref=str(data.get("ref") or DEFAULT_REF),
        subdir=str(data.get("subdir") or ""),
        added_at=str(data.get("added_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )


def write_pin(root: Path, pin: Pin) -> None:
    (root / PIN).write_text(
        yaml.safe_dump(pin.as_dict(), sort_keys=False), encoding="utf-8"
    )


def add(
    paths: ApplacePaths,
    url: str,
    *,
    name: str | None = None,
    ref: str | None = None,
    subdir: str | None = None,
    force: bool = False,
) -> Installed:
    """Clone a stack repository into this home and pin the commit it came from.

    The name comes from the stack's own ``stack.yaml`` unless one is given: a
    company that called its stack ``acme-web`` gets that name here, whatever the
    repository happens to be called.
    """
    paths.stacks.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="applace-stack-") as scratch:
        clone = Path(scratch) / "clone"
        _clone(url, clone, ref)
        commit = gitrepo.head(clone).sha
        root = clone / subdir if subdir else clone
        if not root.is_dir():
            raise StackInstallError(
                f"{url} has no directory {subdir!r}. Check --path against the "
                f"repository's own layout."
            )
        stack = _validate(root, url)

        chosen = name or stack.name
        destination = paths.stacks / chosen
        if destination.exists() and not force:
            raise StackInstallError(
                f"a stack called {chosen!r} is already installed. "
                f"`applace stacks update {chosen}` moves it to a newer commit, "
                f"or pass --force to replace it."
            )
        previous = _installed_commit(destination)
        pin = Pin(
            source=url,
            commit=commit,
            ref=ref or DEFAULT_REF,
            subdir=subdir or "",
            added_at=_now(),
        )
        _replace(root, destination)
        write_pin(destination, pin)

    return Installed(
        name=chosen,
        title=stack.title,
        source=url,
        commit=commit,
        previous=previous,
        ref=pin.ref,
        path=destination,
    )


def update(paths: ApplacePaths, name: str) -> Installed:
    """Move an installed stack to the newest commit of the ref it was cloned at.

    It re-clones rather than pulling, because what is installed is a snapshot
    with no history of its own: the repository is the only thing that knows what
    the stack is now, and anything edited in place here was going to be lost the
    moment somebody else ran the same command.
    """
    root = paths.stacks / name
    pin = _require_pin(root, name)
    installed = add(
        paths,
        pin.source,
        name=name,
        ref=None if pin.ref == DEFAULT_REF else pin.ref,
        subdir=pin.subdir or None,
        force=True,
    )
    write_pin(
        root,
        Pin(
            source=pin.source,
            commit=installed.commit,
            ref=pin.ref,
            subdir=pin.subdir,
            added_at=pin.added_at,
            updated_at=_now(),
        ),
    )
    return Installed(
        name=installed.name,
        title=installed.title,
        source=installed.source,
        commit=installed.commit,
        previous=pin.commit or None,
        ref=pin.ref,
        path=installed.path,
    )


def remove(paths: ApplacePaths, name: str) -> Path:
    """Uninstall a stack. The apps built from it are untouched -- they are repositories."""
    root = paths.stacks / name
    if not (root / "stack.yaml").is_file():
        raise StackInstallError(
            f"no stack called {name!r} is installed here. "
            f"`applace stacks` lists the ones that are."
        )
    shutil.rmtree(root)
    return root


def describe(paths: ApplacePaths) -> list[dict[str, Any]]:
    """Every available stack with where it came from, for `applace stacks`."""
    out: list[dict[str, Any]] = []
    for stack in registry(paths.stacks).values():
        entry: dict[str, Any] = {
            "name": stack.name,
            "title": stack.title,
            "description": stack.description,
            "source": stack.source,
            "commit": stack.commit,
            "deploy": list(stack.deploy),
            "builtin": stack.source == BUILTIN,
        }
        pin = read_pin(stack.root) if stack.source != BUILTIN else None
        if pin is not None:
            entry["ref"] = pin.ref
            entry["added_at"] = pin.added_at
            entry["updated_at"] = pin.updated_at
        out.append(entry)
    return out


def drift(conn: Connection, paths: ApplacePaths) -> list[dict[str, Any]]:
    """Apps whose stack has moved since they were created.

    This is a report, never an action. Knowing that `billing-portal` was born
    from `acme-web` at a commit four releases old is what lets a human decide to
    re-generate it, cherry-pick one component, or leave it alone -- all three are
    reasonable, and none of them is Applace's call.
    """
    available = registry(paths.stacks)
    out: list[dict[str, Any]] = []
    for row in list_apps(conn):
        stack = available.get(str(row["stack"]))
        if stack is None or stack.commit is None:
            continue
        born = str(row["stack_commit"] or "")
        if not born or born == stack.commit:
            continue
        out.append(
            {
                "app": str(row["slug"]),
                "stack": stack.name,
                "born_at": born,
                "stack_at": stack.commit,
            }
        )
    return out


# -- the bits that touch git and the filesystem ----------------------------


def _clone(url: str, destination: Path, ref: str | None) -> None:
    args = ["clone", "--quiet"]
    if ref:
        args += ["--branch", ref]
    args += [url, str(destination)]
    try:
        gitrepo.git(*args, cwd=destination.parent)
    except gitrepo.GitError as exc:
        # git's own sentence, without the command line we built in front of it:
        # "does not appear to be a git repository" is the useful half.
        _, _, reason = str(exc).partition("failed: ")
        raise StackInstallError(
            f"could not clone {url}"
            + (f" at {ref!r}" if ref else "")
            + f": {reason or exc}"
        ) from exc


def _validate(root: Path, url: str) -> Stack:
    try:
        return load(root, source=url)
    except StackError as exc:
        raise StackInstallError(
            f"{url} is not a stack Applace can use: {exc}. A stack repository "
            f"holds stack.yaml and a template/ directory."
        ) from exc


def _installed_commit(destination: Path) -> str | None:
    if not destination.exists():
        return None
    try:
        pin = read_pin(destination)
    except StackInstallError:
        return None
    return pin.commit if pin is not None else None


def _replace(source: Path, destination: Path) -> None:
    """Move the validated clone into place, replacing whatever was there.

    The old directory goes to one side first and is deleted afterwards, so a
    failure halfway leaves the previous stack installed rather than nothing.
    """
    shutil.rmtree(source / ".git", ignore_errors=True)
    previous = destination.with_name(destination.name + ".replacing")
    shutil.rmtree(previous, ignore_errors=True)
    if destination.exists():
        destination.rename(previous)
    try:
        shutil.move(str(source), str(destination))
    except OSError:
        if previous.exists():
            previous.rename(destination)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def _require_pin(root: Path, name: str) -> Pin:
    if not (root / "stack.yaml").is_file():
        raise StackInstallError(
            f"no stack called {name!r} is installed here. "
            f"`applace stacks add <git-url>` installs one."
        )
    pin = read_pin(root)
    if pin is None:
        raise StackInstallError(
            f"{name} is built in, so there is nothing to update: it ships with "
            f"Applace and moves when Applace does."
        )
    return pin


def _now() -> str:
    from .db import now_iso

    return now_iso()


__all__ = [
    "Installed",
    "Pin",
    "StackInstallError",
    "add",
    "describe",
    "drift",
    "read_pin",
    "remove",
    "update",
    "write_pin",
]
