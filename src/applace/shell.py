"""Running the commands a stack declares.

Applace drives Node rather than being written in it (D10), so almost everything
interesting here happens in a subprocess. Two rules hold everywhere:

* no shell -- commands are split with ``shlex`` and executed directly, so a
  stack file can never smuggle a pipeline or a ``rm -rf`` past us;
* output is captured, never inherited, because it becomes an error message an
  agent reads or a log a human opens.

This is not a sandbox and does not pretend to be one: ``npm install`` runs
package scripts, and a stack you installed is code you trusted.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 600.0

# Enough of it that the agent sees the real failure, little enough that a
# runaway build log does not become the answer to a tool call.
MAX_OUTPUT = 20_000


class CommandNotFound(Exception):
    """The stack asked for a program this machine does not have."""


@dataclass(frozen=True)
class Result:
    command: str
    code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Both streams, in the order a human would have seen them.

        Tools split their diagnostics across the two inconsistently -- npm warns
        on stderr and errors on stdout, tsc does the opposite -- so anything that
        parses a failure has to look at both anyway.
        """
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


def run(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Result:
    """Run one stack command in ``cwd`` and capture what it said."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")
    environment = {**os.environ, **(env or {})}
    # npm writes progress bars and colour escapes that make a captured log
    # unreadable and an error message unparseable.
    environment.setdefault("CI", "1")
    environment.setdefault("NO_COLOR", "1")
    environment.setdefault("FORCE_COLOR", "0")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandNotFound(
            f"{argv[0]!r} is not on PATH, and the stack needs it to run "
            f"{command!r}."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        return Result(
            command=command,
            code=-1,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=True,
        )
    return Result(
        command=command,
        code=completed.returncode,
        stdout=_clip(completed.stdout),
        stderr=_clip(completed.stderr),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return _clip(raw.decode("utf-8", "replace"))
    return _clip(raw)


def _clip(text: str) -> str:
    """Keep the end of a long output: that is where the error is."""
    if len(text) <= MAX_OUTPUT:
        return text
    kept = text[-MAX_OUTPUT:]
    return f"[... {len(text) - MAX_OUTPUT} characters omitted ...]\n{kept}"
