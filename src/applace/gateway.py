"""The process that holds the credential so the browser never does (D24, M13).

One gateway per home, not per app: it is a proxy, and a home with forty apps
does not need forty of them. It answers on loopback at

    /<slug>/<api>/<sub-path>

and the app's own code never sees that URL. The stack's dev server proxies
``/api/gateway`` here while previewing; after a deploy, the generated serverless
function in the app's repository does the same job from the same declaration.
Either way the app fetches a relative path and the token lives one hop away.

Three things about this file are deliberate.

**It re-reads everything on every request.** Declarations from the home's
database, opened read-only; values from ``env/<slug>.env``. A long-lived proxy
holding a cached copy of an allowlist is a proxy that keeps forwarding to an
upstream somebody just revoked.

**It requires a key.** Loopback is not a boundary on a machine holding many
homes (D20): without this, one person's process could call another person's
gateway and read what their token can read. The key is a file in the home, mode
0600, and the dev server is handed it the same way it is handed the URL.

**It refuses by default.** A call whose app, API, method or path was not
declared is a 403 with the reason, not a pass-through. The declaration is the
allowlist (D24), and this is the place where that sentence becomes true.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import apis, machine, policy
from .apis import HOP_BY_HOP, Api
from .db import (
    delete_gateway,
    find_app,
    find_gateway,
    list_apis,
    upsert_gateway,
)
from .env import values_for
from .paths import ApplacePaths
from .preview import (
    claimed_ports,
    port_is_free,
    process_command,
    responds,
    spawn,
    terminate,
)

HOST = "127.0.0.1"

# Away from the preview range so a human reading `lsof` can tell at a glance
# which of the two they are looking at.
PORT_RANGE = range(5300, 5340)

KEY_HEADER = "X-Applace-Key"
KEY_NAME = "gateway.key"

# How long to wait on the upstream. Longer than a page load, shorter than a
# chat turn: an internal API that has not answered in thirty seconds is down,
# and saying so beats holding the browser open.
UPSTREAM_TIMEOUT = 30.0

# A body bigger than this is not what this gateway is for. It reads the whole
# request into memory to sign and forward it, and an unbounded read on a
# loopback port is a way to take the machine down by accident.
MAX_BODY = 8 * 1024 * 1024


def key_for(paths: ApplacePaths) -> str:
    """The home's gateway key, made once and kept 0600 beside its secrets."""
    path = paths.env / KEY_NAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    made = secrets.token_urlsafe(32)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(made + "\n", encoding="utf-8")
    return made


# -- what one request resolves to ------------------------------------------


@dataclass(frozen=True)
class Refused:
    status: int
    reason: str


def resolve(
    paths: ApplacePaths, *, slug: str, api_name: str, method: str, sub_path: str
) -> Api | Refused:
    """Find the declaration this call is allowed under, or say why there is none.

    Opened read-only on purpose: a proxy that can write to the journal is a
    proxy that can corrupt it, and it has nothing to say that belongs there.
    """
    if not paths.db.exists():
        return Refused(503, "this home has no database yet")
    conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        row = find_app(conn, slug)
        if row is None:
            return Refused(404, f"no app called {slug!r} in this home")
        declared = [apis.from_row(r) for r in list_apis(conn, str(row["id"]))]
    finally:
        conn.close()

    api = apis.find(declared, api_name)
    if api is None:
        known = ", ".join(a.name for a in declared) or "none"
        return Refused(
            403, f"{slug} declared no API called {api_name!r} (declared: {known})"
        )
    why = api.permits(method, sub_path)
    if why:
        return Refused(403, why)

    # Read per request, not per declaration: a machine tightened this morning
    # has to refuse an app that was declared last week, and the declaration is
    # not rewritten just because the policy changed under it.
    try:
        rules = policy.load(paths)
    except policy.PolicyError as exc:
        return Refused(503, f"this machine's policy cannot be read: {exc}")
    refused = rules.refuse_api(api.name, api.host)
    if refused:
        return Refused(403, refused)
    return api


def credential(paths: ApplacePaths, slug: str, api: Api) -> str | Refused:
    """The value to send upstream. The only place in the gateway that reads one."""
    if not api.token_env:
        return ""
    value = values_for(paths, slug).get(api.token_env, "")
    if not value:
        return Refused(
            503,
            f"{api.token_env} has no value yet. A human supplies it with "
            f"`applace env set {slug} {api.token_env}` (D8).",
        )
    return f"{api.scheme} {value}".strip() if api.scheme else value


# -- the server ------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """One request: authorise, resolve, forward, copy back."""

    paths: ApplacePaths
    key: str

    server_version = "Applace"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # The default logs to stderr, which is this process's log file. Keep the
        # line but never the query string: it is the one part of a proxied URL
        # that people put tokens in despite being told not to.
        sys.stderr.write(
            "%s - %s\n" % (self.log_date_time_string(), format % args)
        )

    # Every verb lands here. The allowlist is the declaration's, not the
    # server's: refusing DELETE globally would be a second rule to keep in step
    # with the first one.
    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    # -- the work ----------------------------------------------------------

    def _authorised(self) -> bool:
        """Is the caller this home? (D20)

        A method rather than a line in `_handle` because the gate's own server
        answers the same question differently -- it is serving a browser, and a
        page's `fetch` cannot add a header nobody told it about -- and the
        forwarding below must not be written twice to say so.
        """
        return secrets.compare_digest(self.headers.get(KEY_HEADER, ""), self.key)

    def _handle(self, method: str) -> None:
        if self.path == "/__health":
            self._say(200, {"ok": True})
            return
        if not self._authorised():
            # Deliberately not "wrong key": this port is reachable by anything
            # on the machine, and telling it what it got wrong is a favour.
            self._say(403, {"ok": False, "error": "not for you"})
            return

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            self._say(
                404,
                {"ok": False, "error": "the shape is /<app>/<api>/<path>"},
            )
            return
        slug, api_name, rest = parts[0], parts[1], "/".join(parts[2:])

        found = resolve(
            self.paths, slug=slug, api_name=api_name, method=method, sub_path=rest
        )
        if isinstance(found, Refused):
            self._say(found.status, {"ok": False, "error": found.reason})
            return

        token = credential(self.paths, slug, found)
        if isinstance(token, Refused):
            self._say(token.status, {"ok": False, "error": token.reason})
            return

        self._forward(method, found, rest, parsed.query, token)

    def _forward(
        self, method: str, api: Api, rest: str, query: str, token: str
    ) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._say(413, {"ok": False, "error": "body too large for the gateway"})
            return
        body = self.rfile.read(length) if length else None

        # Case-insensitively, because the header arrives however the dev server's
        # proxy chose to spell it and the one thing that must never go upstream
        # is the key that proves the caller is this home.
        skip = HOP_BY_HOP | {KEY_HEADER.lower()}
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in skip
        }
        if token:
            headers[api.header] = token

        request = urllib.request.Request(
            api.upstream(rest, query), data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT) as answer:
                self._copy(answer.status, answer.headers.items(), answer.read())
        except urllib.error.HTTPError as exc:
            # An upstream 404 is an answer, not a gateway failure: pass it
            # through whole so the app sees what it would have seen directly.
            self._copy(exc.code, exc.headers.items(), exc.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._say(
                502,
                {
                    "ok": False,
                    "error": f"{api.name} did not answer: {exc}",
                    "upstream": api.host,
                },
            )

    def _copy(self, status: int, headers: Any, body: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            if name.lower() in HOP_BY_HOP or name.lower() == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _say(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


# -- the supervisor --------------------------------------------------------
#
# Deliberately here and not in `preview`: the preview supervisor must not know
# that a gateway exists, or the two would have to be started, stopped and reaped
# together for ever. What ties them is `Applace.preview`, which is the only place
# that knows an app is about to need both.


@dataclass(frozen=True)
class Running:
    """The home's gateway, as a URL somebody can be handed."""

    url: str
    port: int
    pid: int
    log: Path
    started_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "port": self.port,
            "pid": self.pid,
            "log": str(self.log),
            "started_at": self.started_at,
        }


class GatewayError(Exception):
    """The gateway could not be started, or could not be trusted."""


LOG_NAME = "gateway.log"

# What the live command line must contain for a claimed pid to be ours. The
# port is not in it -- it arrives by environment so the key can too (see
# `main`) -- so the module name is the only marker there is.
MARKER = "applace.gateway"


def status(conn: Any) -> Running | None:
    """The home's gateway if it is genuinely running, reaping the claim if not."""
    row = find_gateway(conn)
    if row is None:
        return None
    command = process_command(int(row["pid"]))
    if command is None or MARKER not in command:
        delete_gateway(conn)
        conn.commit()
        return None
    return Running(
        url=str(row["url"]),
        port=int(row["port"]),
        pid=int(row["pid"]),
        log=Path(str(row["log_path"])),
        started_at=str(row["started_at"]),
    )


def ensure(paths: ApplacePaths, conn: Any) -> Running:
    """Start the home's gateway, or hand back the one already running."""
    live = status(conn)
    if live is not None:
        return live

    port = _free_port(paths, conn)
    log_path = paths.logs / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    key = key_for(paths)

    pid = spawn(
        f"{sys.executable} -m applace.gateway",
        cwd=paths.home,
        log_path=log_path,
        # By environment, never by argument: `ps` is world-readable and the key
        # is what keeps another home out of this one (D20).
        environment={
            "APPLACE_HOME": str(paths.home),
            "APPLACE_GATEWAY_PORT": str(port),
            "APPLACE_GATEWAY_KEY": key,
        },
    )
    machine.started(paths, port, pid)
    url = f"http://{HOST}:{port}"
    upsert_gateway(conn, port=port, pid=pid, url=url, log_path=str(log_path))
    conn.commit()

    if not _wait_until_ready(f"{url}/__health", pid):
        raise GatewayError(
            f"the gateway did not answer on {url} within 15s. See {log_path}."
        )
    return Running(url=url, port=port, pid=pid, log=log_path, started_at="")


def stop(conn: Any) -> bool:
    """Stop the home's gateway. False when there was nothing to stop."""
    row = find_gateway(conn)
    if row is None:
        return False
    live = status(conn) is not None
    delete_gateway(conn)
    conn.commit()
    if live:
        terminate(int(row["pid"]))
    return live


def environment(running: Running, slug: str, key: str) -> dict[str, str]:
    """What a dev server needs to proxy to the gateway.

    Not prefixed with the stack's `env_prefix`, and that is the point: a bundler
    only inlines what carries its prefix, so neither the URL nor the key can end
    up in a file a browser downloads. The stack's config reads them in node,
    where they belong.
    """
    return {
        "APPLACE_GATEWAY": f"{running.url}/{slug}",
        "APPLACE_GATEWAY_KEY": key,
    }


def _free_port(paths: ApplacePaths, conn: Any) -> int:
    taken = claimed_ports(conn)
    candidates = [port for port in PORT_RANGE if port not in taken]
    brokered = machine.claim(
        paths, candidates, slug="__gateway", kind="gateway", free=port_is_free
    )
    if brokered is not None:
        return brokered
    if paths.ledger is None:
        for candidate in candidates:
            if port_is_free(candidate):
                return candidate
    raise GatewayError(
        f"no free port between {PORT_RANGE.start} and {PORT_RANGE.stop - 1} "
        f"for the gateway."
    )


def _wait_until_ready(url: str, pid: int) -> bool:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if responds(url):
            return True
        if process_command(pid) is None:
            return False
        time.sleep(0.1)
    return False


def serve(paths: ApplacePaths, port: int, key: str) -> None:
    """Run the gateway until it is killed. The body of `python -m applace.gateway`."""
    handler = type("BoundHandler", (Handler,), {"paths": paths, "key": key})
    with ThreadingHTTPServer((HOST, port), handler) as server:
        server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    """Entry point for the supervised child. Everything comes in by environment.

    By environment and not by argument: the key would otherwise be in the
    machine's process list, where every other user can read it.
    """
    _ = argv
    home = os.environ.get("APPLACE_HOME")
    port = os.environ.get("APPLACE_GATEWAY_PORT")
    key = os.environ.get("APPLACE_GATEWAY_KEY")
    if not (home and port and key):
        sys.stderr.write(
            "the gateway is started by Applace, with APPLACE_HOME, "
            "APPLACE_GATEWAY_PORT and APPLACE_GATEWAY_KEY set.\n"
        )
        return 2
    serve(ApplacePaths(Path(home)), int(port), key)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
