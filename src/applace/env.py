"""Environment variables: the names an agent knows, the values it never sees (D8).

The rule is one sentence long. **An agent declares and reads names; a human
supplies values.** Nothing in this module hands a value back to a caller that
could put it in a model's context, and the one function that reads values --
:func:`values_for` -- exists to build a subprocess environment with, which is
the only place a value is ever needed.

So there are two stores, deliberately apart:

* the *declarations* live in the database, next to the app, and are safe to
  print, log and return from a tool;
* the *values* live in ``~/.applace/env/<slug>.env``, outside every repository,
  in a directory ``init`` creates as 0700 and in a file written 0600.

The app's ``.gitignore`` names ``.env`` from the very first commit, so even a
human who copies a value into the repository by hand does not commit it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Connection, declare_env, delete_env, list_env
from .paths import ApplacePaths

# The shape every runtime agrees on. A lowercase name is not an error anywhere,
# but it is a mistake everywhere, and a bundler prefix is uppercase by custom.
NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

# What a value may not contain. A newline would make the file ambiguous, and an
# ambiguous secret store is worse than one that says no.
FORBIDDEN = "\n\r"


class EnvError(Exception):
    """The name or the value cannot be used."""


@dataclass(frozen=True)
class Variable:
    """A declared variable, and whether a human has answered yet."""

    name: str
    description: str = ""
    has_value: bool = False
    exposed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "set": self.has_value,
            "exposed": self.exposed,
        }


def check_name(name: str) -> str:
    """Validate a variable name, returning it unchanged."""
    if not NAME.match(name):
        raise EnvError(
            f"{name!r} is not a usable variable name. Use capitals, digits and "
            f"underscores, starting with a letter: API_BASE_URL."
        )
    return name


def declare(
    conn: Connection, *, app_id: str, name: str, description: str | None = None
) -> str:
    """Say that an app needs a variable. The value is not this function's business."""
    check_name(name)
    declare_env(conn, app_id=app_id, name=name, description=description)
    conn.commit()
    return name


def forget(conn: Connection, *, app_id: str, name: str) -> None:
    delete_env(conn, app_id, name)
    conn.commit()


def declarations(
    conn: Connection, paths: ApplacePaths, *, app_id: str, slug: str, prefix: str = ""
) -> list[Variable]:
    """Every declared variable, with whether it has a value and whether it ships.

    ``prefix`` is the stack's ``env_prefix``: a variable without it exists for
    the build but never reaches the browser, which is a bug an agent should be
    able to see rather than debug.
    """
    values = values_for(paths, slug)
    return [
        Variable(
            name=str(row["name"]),
            description=str(row["description"] or ""),
            has_value=str(row["name"]) in values,
            exposed=not prefix or str(row["name"]).startswith(prefix),
        )
        for row in list_env(conn, app_id)
    ]


# -- values ----------------------------------------------------------------


def values_for(paths: ApplacePaths, slug: str) -> dict[str, str]:
    """The app's variables as an environment to run something with.

    The only reader of secret values in Applace. Everything that calls it hands
    the result straight to a subprocess.
    """
    path = paths.env_file(slug)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value
    return values


def set_value(paths: ApplacePaths, slug: str, name: str, value: str) -> Path:
    """Write one value. Creates the file 0600, rewrites it with the same mode."""
    check_name(name)
    if any(character in value for character in FORBIDDEN):
        raise EnvError(
            f"the value for {name} spans more than one line. Applace stores "
            f"single-line values; put a file path in the variable instead."
        )
    values = values_for(paths, slug)
    values[name] = value
    return _write(paths, slug, values)


def unset_value(paths: ApplacePaths, slug: str, name: str) -> bool:
    """Remove one value. False when there was none."""
    values = values_for(paths, slug)
    if name not in values:
        return False
    del values[name]
    _write(paths, slug, values)
    return True


def _write(paths: ApplacePaths, slug: str, values: dict[str, str]) -> Path:
    path = paths.env_file(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    body = "".join(f"{name}={value}\n" for name, value in sorted(values.items()))
    # Write, then narrow: between `open` and `chmod` the file is the umask's
    # idea of private, so create it empty and fill it once it is ours alone.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(
        "# Applace keeps this file outside every repository (D8).\n"
        "# `applace env set <app> <NAME>` writes it; nothing prints it.\n" + body,
        encoding="utf-8",
    )
    return path


def missing(variables: list[Variable]) -> list[Variable]:
    """The declared variables nobody has answered yet."""
    return [variable for variable in variables if not variable.has_value]


def instruction(slug: str, name: str) -> str:
    """What to tell a human, and what an agent should relay verbatim."""
    return f"applace env set {slug} {name}"


__all__ = [
    "EnvError",
    "Variable",
    "check_name",
    "declarations",
    "declare",
    "forget",
    "instruction",
    "missing",
    "set_value",
    "unset_value",
    "values_for",
]
