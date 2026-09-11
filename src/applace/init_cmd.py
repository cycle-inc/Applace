"""``applace init``: make a home, and say whether this machine can build anything.

Applace drives other people's tools, so setup is mostly a question of what is
present. It is better to hear "node is missing" now than from a stack trace in
the middle of an agent's first app.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .db import connect
from .paths import ApplacePaths
from .stacks import StackError, registry

# What every stack Applace ships needs. A company stack may need more; that is
# its business, and it fails loudly at install time.
REQUIREMENTS = {
    "git": "stores every app, and is not optional",
    "node": "runs the dev server and the build",
    "npm": "installs dependencies",
}


@dataclass
class Requirement:
    name: str
    detail: str
    path: str | None

    @property
    def present(self) -> bool:
        return self.path is not None


@dataclass
class InitReport:
    home: Path
    created: bool
    stacks: list[tuple[str, str]] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.error is None and all(r.present for r in self.requirements)

    @property
    def missing(self) -> list[Requirement]:
        return [r for r in self.requirements if not r.present]


def run_init(paths: ApplacePaths) -> InitReport:
    """Create the layout, open the database, and inventory the machine."""
    created = not paths.home.exists()
    paths.create()
    connect(paths.db).close()

    report = InitReport(home=paths.home, created=created)
    try:
        report.stacks = [
            (stack.name, stack.title) for stack in registry(paths.stacks).values()
        ]
    except StackError as exc:
        report.error = str(exc)
    report.requirements = [
        Requirement(name=name, detail=detail, path=shutil.which(name))
        for name, detail in REQUIREMENTS.items()
    ]
    return report
