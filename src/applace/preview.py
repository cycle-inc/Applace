"""The dev-server supervisor (M3).

A preview is a long-lived `npm run dev` that outlives the tool call that started
it, and everything awkward about this module follows from that one fact.

* **Each preview gets its own process group.** `npm run dev` is a shell that
  spawns node; killing the pid we hold leaves vite holding the port. M1 learned
  this the hard way, so the child is started with ``start_new_session=True`` and
  stopped with ``killpg``.
* **The database row is a claim, not a fact.** The process it names may have
  died, and on a machine that has been up a while its pid may have been reused
  by something else entirely. Nothing here trusts a row: it is checked against
  the live process's own command line before it is believed, and reaped when it
  does not hold up. That is what makes a restarted supervisor safe.
* **Readiness is an HTTP request, not a sleep.** The server is up when it
  answers on the port we asked for, and if it never does, its log is the answer.
"""

from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import (
    Connection,
    claimed_ports,
    delete_preview,
    find_preview,
    upsert_preview,
)
from .paths import ApplacePaths
from .stacks import Stack

HOST = "127.0.0.1"

# M1's other lesson: Vite left alone binds `localhost`, which on a machine with
# IPv6 is ::1, and a health check on 127.0.0.1 would call a healthy server dead.
# Every stack's `dev` command pins the interface, and so does the URL we hand out.
PORT_RANGE = range(5180, 5280)

READY_TIMEOUT = 60.0
READY_INTERVAL = 0.25

# Between SIGTERM and SIGKILL. Vite exits in well under a second; a build that
# is mid-write gets the chance to finish it.
STOP_GRACE = 5.0

LOG_NAME = "dev.log"

# How much of the log to hand back when a preview fails to come up.
LOG_TAIL = 60


class PreviewError(Exception):
    """The preview could not be started, or could not be trusted."""


@dataclass
class Preview:
    app: str
    url: str
    port: int
    pid: int
    log: Path
    started_at: str
    ready: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "url": self.url,
            "port": self.port,
            "pid": self.pid,
            "log": str(self.log),
            "started_at": self.started_at,
            "ready": self.ready,
        }


# -- is it alive -----------------------------------------------------------


def process_command(pid: int) -> str | None:
    """The command line of a running pid, or None if there is no such process.

    Read from ``ps`` rather than from a pid file: this is the question "is the
    thing I started still the thing at this pid", and only the OS can answer it.

    A zombie counts as gone. It still has a pid and ``ps`` still prints it, but
    it is an exit status waiting to be collected, not a server holding a port.
    """
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - ps is on every unix
        return None
    if completed.returncode != 0:
        return None
    line = completed.stdout.strip()
    if not line:
        return None
    state, _, command = line.partition(" ")
    if state.startswith("Z"):
        return None
    return command.strip() or None


def responds(url: str, timeout: float = 1.0) -> bool:
    """True when something answers HTTP on the URL, whatever it answers.

    A 404 is a running dev server with no route at `/`, which is still a running
    dev server; only a connection that cannot be made means down.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _matches(row: Any, root: Path) -> bool:
    """Is the process at this pid still the preview we started?

    The recorded command is the stack's `dev` line, and the process we started
    runs it in the app's directory -- so either the app's path or the port shows
    up in the live command line. A pid that has been recycled will not match.
    """
    command = process_command(int(row["pid"]))
    if command is None:
        return False
    return str(root) in command or f"{row['port']}" in command


# -- the supervisor --------------------------------------------------------


def status(conn: Connection, app_id: str, slug: str, root: Path) -> Preview | None:
    """The app's preview if it is genuinely running, reaping the claim if not."""
    row = find_preview(conn, app_id)
    if row is None:
        return None
    if not _matches(row, root):
        delete_preview(conn, app_id)
        conn.commit()
        return None
    return Preview(
        app=slug,
        url=str(row["url"]),
        port=int(row["port"]),
        pid=int(row["pid"]),
        log=Path(str(row["log_path"])),
        started_at=str(row["started_at"]),
        ready=responds(str(row["url"])),
    )


def start(
    paths: ApplacePaths,
    conn: Connection,
    *,
    app_id: str,
    slug: str,
    root: Path,
    stack: Stack,
    port: int | None = None,
) -> Preview:
    """Start the app's dev server, or hand back the one already running.

    Idempotent on purpose: an agent that calls this twice wants a URL, not an
    error, and a second dev server on a second port would be a worse answer than
    either.
    """
    running = status(conn, app_id, slug, root)
    if running is not None:
        if port is not None and port != running.port:
            raise PreviewError(
                f"{slug} is already previewing on port {running.port}. Stop it "
                f"first if you want port {port}."
            )
        return running

    command_template = stack.commands.get("dev")
    if command_template is None:  # pragma: no cover - `dev` is required of a stack
        raise PreviewError(f"the {stack.name} stack declares no dev command")

    chosen = port if port is not None else _free_port(conn)
    if port is not None and not _is_free(port):
        raise PreviewError(f"port {port} is already in use by something else.")
    command = command_template.format(port=chosen, host=HOST)
    url = f"http://{HOST}:{chosen}/"

    log_path = paths.app_logs(slug) / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid = _spawn(command, cwd=root, log_path=log_path)

    upsert_preview(
        conn,
        app_id=app_id,
        port=chosen,
        pid=pid,
        url=url,
        command=command,
        log_path=str(log_path),
    )
    conn.commit()

    ready = _wait_until_ready(url, pid)
    if not ready:
        # Leave the row: the process may still be alive and holding the port,
        # and `stop_preview` has to be able to find it. Say what the log said.
        raise PreviewError(
            f"{slug} did not answer on {url} within {int(READY_TIMEOUT)}s.\n"
            f"{tail(log_path)}"
        )
    return status(conn, app_id, slug, root) or Preview(
        app=slug, url=url, port=chosen, pid=pid, log=log_path, started_at="", ready=True
    )


def stop(conn: Connection, app_id: str, slug: str, root: Path) -> bool:
    """Stop the app's preview. False when there was nothing to stop."""
    row = find_preview(conn, app_id)
    if row is None:
        return False
    pid = int(row["pid"])
    running = _matches(row, root)
    delete_preview(conn, app_id)
    conn.commit()
    if not running:
        return False
    _terminate(pid)
    return True


def reap(conn: Connection, rows: list[Any]) -> list[str]:
    """Drop claims whose process is gone. Returns the slugs that were reaped."""
    reaped: list[str] = []
    for row in rows:
        if not _matches(row, Path(str(row["path"]))):
            delete_preview(conn, str(row["app_id"]))
            reaped.append(str(row["slug"]))
    if reaped:
        conn.commit()
    return reaped


def tail(log_path: Path, lines: int = LOG_TAIL) -> str:
    """The end of a preview's log, which is where it says why it would not start."""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    kept = [line for line in content.splitlines() if line.strip()][-lines:]
    return "\n".join(kept) or "(the dev server printed nothing)"


# -- the awkward parts -----------------------------------------------------


def _spawn(command: str, *, cwd: Path, log_path: Path) -> int:
    """Start the dev server detached, with both streams going to its log."""
    argv = shlex.split(command)
    handle = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"},
            # Its own session, so the whole tree can be signalled at once and so
            # a Ctrl-C in the terminal that started Applace does not reach it.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        handle.close()
        raise PreviewError(
            f"{argv[0]!r} is not on PATH, and the stack needs it to preview."
        ) from exc
    finally:
        # The child holds its own descriptor; ours would keep the file open for
        # as long as the supervisor lives.
        handle.close()
    return process.pid


def _terminate(pid: int) -> None:
    """SIGTERM the group, then SIGKILL what is left of it."""
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return
    for sig, wait in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, 0.5)):
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError):
            _reap_zombie(pid)
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            _reap_zombie(pid)
            if not _group_alive(group):
                # Again: the child may have exited in the instant between the
                # reap above and this check, and an unreaped zombie would keep
                # showing up in `ps` as if the preview were still running.
                _reap_zombie(pid)
                return
            time.sleep(0.05)
    _reap_zombie(pid)


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin answers EPERM, not ESRCH, for a group whose leader is a zombie
        # nobody has waited on yet. Either way, nothing in it is running.
        return False
    return True


def _reap_zombie(pid: int) -> None:
    """Collect the child if it is ours, so it does not linger as a zombie.

    A preview recovered after the supervisor restarted is not our child at all;
    ``waitpid`` says so and there is nothing to do -- init has it.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _wait_until_ready(url: str, pid: int) -> bool:
    """Poll until the server answers, or until it is clear it never will."""
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if responds(url):
            return True
        if process_command(pid) is None:
            # It exited. Waiting out the timeout would only delay the log.
            return False
        time.sleep(READY_INTERVAL)
    return False


def _free_port(conn: Connection) -> int:
    """A port nothing else on this machine, and no other app, is using."""
    claimed = claimed_ports(conn)
    for candidate in PORT_RANGE:
        if candidate not in claimed and _is_free(candidate):
            return candidate
    raise PreviewError(
        f"no free port between {PORT_RANGE.start} and {PORT_RANGE.stop - 1}. "
        f"Stop a preview before starting another."
    )


def _is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # SO_REUSEADDR because node sets it: the question this answers is "could
        # the dev server bind here", and without it a port whose last connection
        # is still in TIME_WAIT -- which is every port we just stopped a preview
        # on -- would read as busy for two minutes when vite would take it
        # happily.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True
