"""Turning a tool's own words into `{file, line, column, message}`.

The compiler (D3) is only worth having if a failure comes back as something an
agent can act on. Handing it 200 lines of rolldown stack trace and hoping is not
that, so each stage's real output is parsed here into diagnostics that point at
a file and a line.

Every pattern in this module was read off a real run of the built-in stack --
tsc 5.9, eslint 10 stylish, vite 8 on rolldown -- not off documentation. When a
pattern misses, :func:`parse` falls back to the tail of the output rather than
to silence: a wrong-looking error beats a green light on a red build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A red gate with 300 diagnostics is a red gate with one cause. Keep enough to
# see the shape of the failure and say how many were dropped.
MAX_DIAGNOSTICS = 40

# How much raw output to keep when nothing parsed.
TAIL_LINES = 40

# Tools promise to honour NO_COLOR and then colour anyway -- rolldown does,
# through its own renderer -- so stripping escapes is not optional here.
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


@dataclass(frozen=True)
class Diagnostic:
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    code: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "code": self.code,
            "severity": self.severity,
        }


def parse(stage: str, output: str, *, root: Path | None = None) -> list[Diagnostic]:
    """Diagnostics for one failed stage, newest tool first, never empty."""
    text = _ANSI.sub("", output)
    parser = _PARSERS.get(stage)
    found = parser(text, root) if parser is not None else []
    if not found:
        found = _fallback(text)
    if len(found) > MAX_DIAGNOSTICS:
        dropped = len(found) - MAX_DIAGNOSTICS
        found = found[:MAX_DIAGNOSTICS]
        found.append(Diagnostic(message=f"... and {dropped} more of the same kind."))
    return found


# -- typecheck -------------------------------------------------------------

# `src/App.tsx(1,7): error TS2322: Type 'number' is not ...` -- what tsc writes
# when its output is a pipe rather than a terminal, which is always, here.
_TSC_PLAIN = re.compile(
    r"^(?P<file>\S[^(]*)\((?P<line>\d+),(?P<column>\d+)\):\s+"
    r"(?P<severity>error|warning)\s+(?P<code>TS\d+):\s+(?P<message>.*)$"
)
# The same thing under `--pretty`, which a company stack may well turn on.
_TSC_PRETTY = re.compile(
    r"^(?P<file>\S.*?):(?P<line>\d+):(?P<column>\d+)\s+-\s+"
    r"(?P<severity>error|warning)\s+(?P<code>TS\d+):\s+(?P<message>.*)$"
)


def parse_tsc(output: str, root: Path | None = None) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for line in output.splitlines():
        match = _TSC_PLAIN.match(line) or _TSC_PRETTY.match(line)
        if match is None:
            continue
        found.append(
            Diagnostic(
                message=match["message"].strip(),
                file=_relative(match["file"], root),
                line=int(match["line"]),
                column=int(match["column"]),
                code=match["code"],
                severity=match["severity"],
            )
        )
    return found


# -- lint ------------------------------------------------------------------

# eslint's stylish formatter prints the *absolute* path of a file on its own
# line, then indented `line:col severity message rule` rows under it. The rule
# name is the last run of non-space text, and the message may contain doubled
# spaces of its own, so the split is on the trailing rule rather than greedy.
_ESLINT_PROBLEM = re.compile(
    r"^\s+(?P<line>\d+):(?P<column>\d+)\s+(?P<severity>error|warning)\s+"
    r"(?P<message>.+?)(?:\s\s+(?P<code>[\w@/-]+))?\s*$"
)
_ESLINT_SUMMARY = re.compile(r"^[✖x]?\s*\d+\s+problems?\b")
# An unindented line naming a file. The trailing extension is what tells a
# heading apart from npm's own `> app@0.0.0 lint` preamble, which is captured
# in the same stream.
_ESLINT_HEADING = re.compile(r"^[^\s>].*\.[A-Za-z0-9]+$")


def parse_eslint(output: str, root: Path | None = None) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    current: str | None = None
    for line in output.splitlines():
        if not line.strip() or _ESLINT_SUMMARY.match(line.strip()):
            continue
        if _ESLINT_HEADING.match(line):
            current = _relative(line.strip(), root)
            continue
        match = _ESLINT_PROBLEM.match(line)
        if match is None:
            continue
        found.append(
            Diagnostic(
                message=match["message"].strip(),
                file=current,
                line=int(match["line"]),
                column=int(match["column"]),
                code=match["code"],
                severity=match["severity"],
            )
        )
    return found


# -- build -----------------------------------------------------------------

# rolldown (vite 8) leads with `[CODE] message` and puts the location in the
# snippet frame that follows: `   ╭─[ src/App.tsx:1:25 ]`.
_ROLLDOWN_HEAD = re.compile(r"^\[(?P<code>[^\]]+)\]\s+(?P<message>.+)$")
_ROLLDOWN_FRAME = re.compile(r"╭─\[\s*(?P<file>[^\s\]]+?):(?P<line>\d+):(?P<column>\d+)\s*\]")
# esbuild, which vite still uses for dependency pre-bundling.
_ESBUILD_HEAD = re.compile(r"^[X✘]\s+\[(?P<severity>ERROR|WARNING)\]\s+(?P<message>.+)$")
_LOCATION = re.compile(r"^\s*(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<column>\d+):?\s*$")

# Past this line everything is rolldown's own call stack through node_modules,
# which is never the app's problem.
_STACK_FRAME = re.compile(r"^\s+at\s")


def parse_bundler(output: str, root: Path | None = None) -> list[Diagnostic]:
    lines = [line for line in output.splitlines() if not _STACK_FRAME.match(line)]
    found: list[Diagnostic] = []
    for index, line in enumerate(lines):
        head = _ROLLDOWN_HEAD.match(line.strip())
        if head is not None:
            file, row, column = _location_near(lines, index)
            found.append(
                Diagnostic(
                    message=head["message"].strip(),
                    file=_relative(file, root) if file else None,
                    line=row,
                    column=column,
                    code=head["code"],
                )
            )
            continue
        esbuild = _ESBUILD_HEAD.match(line.strip())
        if esbuild is not None:
            file, row, column = _location_near(lines, index)
            found.append(
                Diagnostic(
                    message=esbuild["message"].strip(),
                    file=_relative(file, root) if file else None,
                    line=row,
                    column=column,
                    severity=esbuild["severity"].lower(),
                )
            )
    return found


def _location_near(
    lines: list[str], index: int, *, window: int = 4
) -> tuple[str | None, int | None, int | None]:
    """The file:line:column a bundler prints just under its message."""
    for line in lines[index + 1 : index + 1 + window]:
        frame = _ROLLDOWN_FRAME.search(line)
        if frame is not None:
            return frame["file"], int(frame["line"]), int(frame["column"])
        plain = _LOCATION.match(line)
        if plain is not None:
            return plain["file"], int(plain["line"]), int(plain["column"])
    return None, None, None


# -- install ---------------------------------------------------------------

# npm puts one useful sentence in a wall of `npm error` lines: the first one
# that is not a code, a path, or a pointer to the debug log.
_NPM_NOISE = re.compile(r"^npm (error|ERR!)\s+(code\b|errno\b|A complete log|/|[A-Z]:\\)")
_NPM_ERROR = re.compile(r"^npm (?:error|ERR!)\s+(?P<message>.+)$")


def parse_npm(output: str, root: Path | None = None) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for line in output.splitlines():
        stripped = line.strip()
        if _NPM_NOISE.match(stripped):
            continue
        match = _NPM_ERROR.match(stripped)
        if match is not None and match["message"].strip():
            found.append(Diagnostic(message=match["message"].strip()))
        if len(found) >= 5:
            break
    return found


_PARSERS = {
    "typecheck": parse_tsc,
    "lint": parse_eslint,
    "build": parse_bundler,
    "install": parse_npm,
}


# -- shared ----------------------------------------------------------------


def _relative(raw: str, root: Path | None) -> str:
    """A path the agent can pass straight back to `write_files`.

    eslint reports absolute paths, and on macOS ``/tmp`` resolves through a
    symlink to ``/private/tmp``, so comparing the strings is not enough -- both
    sides get resolved before the subtraction.
    """
    path = Path(raw.strip())
    if root is None or not path.is_absolute():
        return raw.strip()
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _fallback(output: str) -> list[Diagnostic]:
    """No pattern matched. Say so, and hand over the end of the output.

    The alternative -- returning nothing -- would tell the agent the stage
    failed for no reason, which is the one answer it cannot work with.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return [Diagnostic(message="The command failed without printing anything.")]
    return [Diagnostic(message="\n".join(lines[-TAIL_LINES:]))]
