"""Stacks: what an app is made of before an agent writes a line of it (D7).

A stack is a directory holding ``stack.yaml`` and a ``template/`` tree. The
built-in ones ship inside this package; a company's own are cloned into
``~/.applace/stacks/`` and pinned to the commit they came from, so an app can
always say what it was born from.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BUILTIN_ROOT = Path(__file__).parent / "builtin_stacks"
BUILTIN = "builtin"

# Every command a stack may name. `dev` is the only one M1 does not run itself.
COMMANDS = ("install", "dev", "typecheck", "lint", "build")
REQUIRED_COMMANDS = ("install", "dev", "build")

# A `.gitignore` shipped inside a Python package is filtered out by most build
# backends and by git itself, so the template carries it under a name nothing
# objects to and we rename it on the way out.
GITIGNORE_TEMPLATE = "_gitignore"

_TEXT_SUFFIXES = frozenset(
    {
        ".css", ".html", ".js", ".json", ".jsx", ".md", ".mjs", ".svg", ".ts",
        ".tsx", ".txt", ".yaml", ".yml",
    }
)


class StackError(Exception):
    """A stack directory is missing, malformed, or names a command it must not."""


@dataclass(frozen=True)
class Stack:
    name: str
    title: str
    description: str
    runtime: str
    commands: dict[str, str]
    root: Path
    manifest: str = "package.json"
    dist: str = "dist"
    # Where `install` puts what it downloads. Its absence is how the compiler
    # knows an app has never been installed; a non-node stack names its own.
    modules: str = "node_modules"
    env_prefix: str = ""
    entry: str = ""
    # The path this stack's dev server proxies to Applace's gateway (D24). Empty
    # means the stack has no gateway, and an app on it cannot declare an API --
    # which is a clear refusal rather than a call that silently has no token.
    gateway: str = ""
    # Does the gate open this stack's build in a browser (D25)? A stack whose
    # every route is behind a login cannot be visited anonymously, and saying so
    # once here beats every app on it discovering it one red gate at a time.
    visit: bool = True
    # What an existing front end has to depend on to be recognised as this stack
    # (D26). Every name must be in the manifest, and a stack that declares none
    # is never recognised -- `take` refuses rather than guessing, and a stack
    # that has not said what it looks like has not been asked for a guess.
    recognise: tuple[str, ...] = ()
    deploy: list[str] = field(default_factory=list)
    source: str = BUILTIN
    commit: str | None = None

    @property
    def template(self) -> Path:
        return self.root / "template"

    def summary(self) -> dict[str, Any]:
        """What `list_stacks` tells an agent. Commands are ours, not its business."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "entry": self.entry,
            "env_prefix": self.env_prefix,
            "gateway": self.gateway,
            "visit": self.visit,
            "recognise": list(self.recognise),
            "deploy": list(self.deploy),
            "source": self.source,
            # The commit a company stack is pinned at. An agent does not act on
            # it, but it is what makes "which version of our design system is
            # this app built on" answerable months later.
            "commit": self.commit,
        }

    def render(self, target: Path, *, context: dict[str, str]) -> list[str]:
        """Copy the template into ``target``, substituting ``{{placeholders}}``.

        Returns the relative paths written, so a caller can report what it made.
        The substitution is deliberately dumb -- a literal replace of known keys,
        no template engine -- because template files must stay valid source that
        a human can open, lint and run outside Applace.
        """
        if not self.template.is_dir():
            raise StackError(f"stack {self.name!r} has no template/ directory")
        target.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for source in sorted(self.template.rglob("*")):
            relative = source.relative_to(self.template)
            if source.is_dir():
                (target / relative).mkdir(parents=True, exist_ok=True)
                continue
            destination = target / relative
            if source.name == GITIGNORE_TEMPLATE:
                destination = destination.with_name(".gitignore")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if _is_text(source):
                text = source.read_text(encoding="utf-8")
                for key, value in context.items():
                    text = text.replace("{{" + key + "}}", value)
                destination.write_text(text, encoding="utf-8")
            else:
                shutil.copyfile(source, destination)
            shutil.copymode(source, destination)
            written.append(str(destination.relative_to(target)))
        return written


def _is_text(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_SUFFIXES or path.name == GITIGNORE_TEMPLATE:
        return True
    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def load(
    root: Path,
    *,
    source: str = BUILTIN,
    commit: str | None = None,
    name: str | None = None,
) -> Stack:
    """Read one stack directory, refusing anything a later stage would trip over.

    ``name`` overrides what ``stack.yaml`` calls itself, which is how a stack
    installed under a chosen directory name is known by that name: two forks of
    the same company stack both say ``name: acme-web`` inside, and the directory
    is the only place a human can tell them apart.
    """
    manifest_path = root / "stack.yaml"
    if not manifest_path.is_file():
        raise StackError(f"{root} has no stack.yaml")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise StackError(f"{manifest_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise StackError(f"{manifest_path} must be a mapping")

    stack_name = str(name or raw.get("name") or root.name)
    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, dict):
        raise StackError(f"stack {stack_name!r} declares no commands")
    commands = {str(k): str(v) for k, v in commands_raw.items() if v}
    unknown = sorted(set(commands) - set(COMMANDS))
    if unknown:
        raise StackError(
            f"stack {stack_name!r} declares unknown command(s) {', '.join(unknown)}; "
            f"known commands are {', '.join(COMMANDS)}"
        )
    missing = [c for c in REQUIRED_COMMANDS if c not in commands]
    if missing:
        raise StackError(
            f"stack {stack_name!r} is missing required command(s) {', '.join(missing)}"
        )

    deploy_raw = raw.get("deploy") or []
    recognise_raw = raw.get("recognise") or []
    if isinstance(recognise_raw, dict):
        # `recognise: {dependencies: [...]}` reads better in a company's own
        # stack.yaml and leaves room for a second key later without breaking the
        # short form, which is a bare list of dependency names.
        recognise_raw = recognise_raw.get("dependencies") or []
    if isinstance(recognise_raw, str) or not isinstance(recognise_raw, list):
        raise StackError(
            f"stack {stack_name!r} declares `recognise` as {type(recognise_raw).__name__}; "
            f"it is a list of dependency names an existing app must have"
        )
    return Stack(
        name=stack_name,
        title=str(raw.get("title") or stack_name),
        description=" ".join(str(raw.get("description") or "").split()),
        runtime=str(raw.get("runtime") or "node"),
        commands=commands,
        root=root,
        manifest=str(raw.get("manifest") or "package.json"),
        dist=str(raw.get("dist") or "dist"),
        modules=str(raw.get("modules") or "node_modules"),
        env_prefix=str(raw.get("env_prefix") or ""),
        entry=str(raw.get("entry") or ""),
        gateway=str(raw.get("gateway") or ""),
        visit=raw.get("visit", True) is not False,
        recognise=tuple(str(d) for d in recognise_raw),
        deploy=[str(d) for d in deploy_raw],
        source=source,
        commit=commit,
    )


def registry(installed_root: Path | None) -> dict[str, Stack]:
    """Every stack available, keyed by name.

    Built-in first, then the ones under ``~/.applace/stacks/``, which win on a
    name collision: a company that ships its own ``vite-react-ts`` has decided
    what that name means here.
    """
    stacks: dict[str, Stack] = {}
    for root in _stack_dirs(BUILTIN_ROOT):
        stack = load(root)
        stacks[stack.name] = stack
    if installed_root is not None:
        for root in _stack_dirs(installed_root):
            pin = root / ".applace-stack.yaml"
            source, commit = BUILTIN, None
            if pin.is_file():
                data = yaml.safe_load(pin.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    source = str(data.get("source") or root.name)
                    commit = data.get("commit")
                    commit = str(commit) if commit else None
            stack = load(root, source=source, commit=commit, name=root.name)
            stacks[stack.name] = stack
    return stacks


def _stack_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if (d / "stack.yaml").is_file())


DEFAULT_STACK = "vite-react-ts"


def resolve(installed_root: Path | None, name: str | None) -> Stack:
    """The stack an agent asked for, or the default, with a listing if it is wrong."""
    available = registry(installed_root)
    if not available:
        raise StackError("no stacks are installed; run `applace init`")
    wanted = name or DEFAULT_STACK
    stack = available.get(wanted)
    if stack is None:
        raise StackError(
            f"unknown stack {wanted!r}. Available: {', '.join(sorted(available))}"
        )
    return stack
