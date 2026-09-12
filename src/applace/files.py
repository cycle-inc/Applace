"""Reading and writing an app's source, and the lint that says what is source.

The first stage of the compiler (D3) is a path lint, and it runs before a single
byte is written. An agent addresses files by repository-relative path and
nothing else: it cannot reach outside the app, it cannot edit the repository's
own plumbing, and it cannot hand-write the dependency graph.

What is refused, and why each one:

* absolute paths and ``..`` -- the app is the boundary;
* a path that leaves the app through a symlink -- the same boundary, checked
  after resolution rather than before, because ``a/b`` where ``a`` is a link is
  not spelled with ``..``;
* ``.git/`` -- rewriting HEAD or a hook is not editing an app;
* ``node_modules/`` -- installed code is the manifest's output, not source;
* the lockfile -- it is npm's to write, and a hand-edited one is a build that
  works here and nowhere else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Any manifest-adjacent lockfile, not just the one the built-in stack uses: a
# company stack may be on pnpm or yarn and the rule is the same.
LOCKFILES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"}
)

FORBIDDEN_ROOTS = frozenset({".git", "node_modules"})

# Reading a file into a model's context has a cost, and a 2 MB generated asset
# is never what the agent meant to open.
MAX_READ_BYTES = 400_000

# npm's own sections, in the order a report should list them.
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


class PathRefused(Exception):
    """A path the agent asked for is not part of the app's source."""


def resolve_in_app(root: Path, relative: str) -> Path:
    """The absolute path of ``relative`` inside ``root``, or raise.

    Returns a path that may not exist yet -- this is the check `write_files`
    runs before creating anything.
    """
    raw = relative.strip()
    if not raw:
        raise PathRefused("an empty path is not a file")
    candidate = Path(raw)
    if candidate.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
        raise PathRefused(
            f"{raw!r} is an absolute path. Use a path relative to the app, "
            f"like 'src/App.tsx'."
        )
    parts = candidate.parts
    if ".." in parts:
        raise PathRefused(f"{raw!r} leaves the app. Paths may not contain '..'.")
    if parts and parts[0] in FORBIDDEN_ROOTS:
        raise PathRefused(
            f"{raw!r} is inside {parts[0]}/, which Applace manages. "
            f"Edit the app's source instead."
        )
    if candidate.name in LOCKFILES:
        raise PathRefused(
            f"{raw!r} is a lockfile; npm writes it. Change the dependency in "
            f"package.json and Applace will reinstall."
        )

    target = root / candidate
    # Resolution happens against the nearest existing ancestor: `.resolve()` on
    # a path that does not exist yet still follows the links it does have.
    anchor = target
    while not anchor.exists() and anchor != root:
        anchor = anchor.parent
    resolved_root = root.resolve()
    if not _is_within(anchor.resolve(), resolved_root):
        raise PathRefused(f"{raw!r} points outside the app through a symlink.")
    return target


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def lint_paths(root: Path, relatives: Iterable[str]) -> dict[str, Path]:
    """Check every path first, so a refusal writes nothing at all."""
    checked: dict[str, Path] = {}
    for relative in relatives:
        checked[relative] = resolve_in_app(root, relative)
    return checked


def read_files(root: Path, relatives: Iterable[str]) -> list[dict[str, Any]]:
    """Read source files, reporting per file rather than failing the batch.

    An agent asking for five files and mistyping one should get four files and
    a note, not an error.
    """
    out: list[dict[str, Any]] = []
    for relative in relatives:
        entry: dict[str, Any] = {"path": relative}
        try:
            target = resolve_in_app(root, relative)
        except PathRefused as exc:
            out.append({**entry, "error": str(exc)})
            continue
        if not target.exists():
            out.append({**entry, "error": "no such file in this app"})
            continue
        if target.is_dir():
            out.append({**entry, "error": "that is a directory, not a file"})
            continue
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            out.append({**entry, "error": f"too large to read ({size} bytes)"})
            continue
        try:
            entry["content"] = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            out.append({**entry, "error": "not a text file"})
            continue
        out.append(entry)
    return out


@dataclass
class WriteResult:
    written: list[str]
    deleted: list[str]


def apply_writes(
    root: Path, files: dict[str, str], deletions: Iterable[str] = ()
) -> WriteResult:
    """Write the files and remove the deletions. Paths are already linted."""
    written: list[str] = []
    for relative, content in files.items():
        target = resolve_in_app(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Newline-normalised on purpose: models emit "\r\n" from time to time
        # and a stray CR is a lint error in a repository nobody edited.
        target.write_text(content.replace("\r\n", "\n"), encoding="utf-8")
        written.append(relative)

    deleted: list[str] = []
    for relative in deletions:
        target = resolve_in_app(root, relative)
        if target.is_dir():
            raise PathRefused(
                f"{relative!r} is a directory. Delete the files in it by name."
            )
        if target.exists():
            target.unlink()
            deleted.append(relative)
    return WriteResult(written=sorted(written), deleted=sorted(deleted))


# -- dependencies ----------------------------------------------------------


def read_dependencies(root: Path, manifest: str) -> dict[tuple[str, str], str]:
    """Every declared dependency as ``(section, name) -> range``.

    A manifest that is missing or unparseable reads as empty rather than
    raising: the agent may have just broken it, and the build is where it
    should hear about that, in the build's own words.
    """
    path = root / manifest
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[tuple[str, str], str] = {}
    for section in DEPENDENCY_SECTIONS:
        block = data.get(section)
        if isinstance(block, dict):
            for name, spec in block.items():
                out[(section, str(name))] = str(spec)
    return out


def diff_dependencies(
    before: dict[tuple[str, str], str], after: dict[tuple[str, str], str]
) -> list[dict[str, str]]:
    """What the write added or moved (D9).

    Only additions and changes are reported. A removal shrinks the app's
    surface and is nobody's business to approve.
    """
    changed: list[dict[str, str]] = []
    for (section, name), spec in sorted(after.items()):
        previous = before.get((section, name))
        if previous == spec:
            continue
        entry = {"name": name, "version": spec, "section": section}
        if previous is not None:
            entry["was"] = previous
        changed.append(entry)
    return changed
