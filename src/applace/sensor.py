"""The gate ends in the browser (D25, M14).

Typecheck, lint and build answer one question: is this a program? They cannot
answer the other one: does it *work*. A white screen with a clean build is the
single most common way a generated front end fails, and every stage before this
one reports it as a success.

So after the production build goes green, the bundle is served and every
declared route is opened in the browser M4 already drives. An uncaught
exception, a page with nothing on it, or a console error is a red gate, carrying
the browser's own words and -- when the build shipped a sourcemap -- the line of
the app's own source that produced them. Nothing is committed (D4).

Three things about this file are deliberate.

**The routes are declared, never crawled.** This runs on every write, and an
agent that says which routes matter is cheaper and clearer than a crawler
guessing from the router's source. With nothing declared, the route is ``/``.

**The assertions are data.** A selector that must be present, a string that must
appear. No test framework enters the harness (D10) and none is written into the
app (D1): an assertion is a row beside the API declarations, and an app that
leaves Applace leaves with no trace of it.

**The server is the deployment, minus the deploying.** It is the same
single-page rule the ``local`` target serves with, plus the one non-file path
the app is allowed to call -- ``/api/gateway`` -- proxied by the same code and
the same allowlist the real gateway uses (D24). Visiting a build whose every API
call 404s would be a sensor that only ever sees an error state.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass, field
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import apis, eyes, gateway
from .db import Connection, declare_route, delete_route, list_routes
from .diagnostics import Diagnostic
from .paths import ApplacePaths
from .static import SinglePageHandler

HOST = "127.0.0.1"

# What an app with nothing declared gets visited at. Every single-page app has
# this route, and an agent that never thought about routes still gets the check.
DEFAULT_ROUTE = "/"

# Guard rails on the declaration, not on the router: Applace does not know what
# a route means to the app, only that it must be a path this server can ask for.
_UNUSABLE = ("://", "..", " ")


class RouteError(Exception):
    """The route cannot be declared, and is not stored half-checked."""


@dataclass(frozen=True)
class Route:
    """One page the gate opens, and what it has to find there."""

    path: str
    selector: str = ""
    text: str = ""
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "selector": self.selector,
            "text": self.text,
            "description": self.description,
        }

    def config(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "text": self.text,
            "description": self.description,
        }

    def url(self, origin: str) -> str:
        return origin.rstrip("/") + self.path

    def expectations(self) -> tuple[str, ...]:
        return tuple(s for s in (self.selector,) if s)


def build(
    *,
    path: str,
    selector: str | None = None,
    text: str | None = None,
    description: str | None = None,
) -> Route:
    """Validate a route declaration, or say exactly what is wrong with it."""
    cleaned = " ".join(path.split())
    if not cleaned:
        raise RouteError("a route needs a path: / or /customers.")
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    for bad in _UNUSABLE:
        if bad in cleaned:
            raise RouteError(
                f"{path!r} is not a usable route. A route is a path on this "
                f"app's own origin: /, /customers, /orders/recent."
            )
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/") or "/"
    return Route(
        path=cleaned,
        selector=" ".join((selector or "").split()),
        text=(text or "").strip(),
        description=" ".join((description or "").split()),
    )


def declare(conn: Connection, *, app_id: str, route: Route) -> Route:
    """Store a route. Re-declaring the same path replaces it outright."""
    declare_route(conn, app_id=app_id, path=route.path, config=route.config())
    conn.commit()
    return route


def forget(conn: Connection, *, app_id: str, path: str) -> bool:
    removed = delete_route(conn, app_id, build(path=path).path)
    conn.commit()
    return removed


def declarations(conn: Connection, app_id: str) -> list[Route]:
    """Every route this app declared, in the order a human would list them."""
    out: list[Route] = []
    for row in list_routes(conn, app_id):
        raw = json.loads(str(row["config_json"]))
        out.append(
            Route(
                path=str(row["path"]),
                selector=str(raw.get("selector", "")),
                text=str(raw.get("text", "")),
                description=str(raw.get("description", "")),
            )
        )
    return out


def to_visit(conn: Connection, app_id: str) -> list[Route]:
    """What the gate opens: the declared routes, or ``/`` when there are none."""
    return declarations(conn, app_id) or [Route(path=DEFAULT_ROUTE)]


# -- one visit --------------------------------------------------------------


@dataclass
class Visit:
    """What the browser found at one route."""

    route: str
    url: str
    ok: bool = True
    title: str = ""
    blank: bool = False
    errors: list[Diagnostic] = field(default_factory=list)
    console: list[dict[str, Any]] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "route": self.route,
            "ok": self.ok,
            "title": self.title,
            "blank": self.blank,
        }
        if self.errors:
            out["errors"] = [error.as_dict() for error in self.errors]
        if self.console:
            out["console"] = self.console
        if self.failed_requests:
            out["failed_requests"] = self.failed_requests
        return out


@dataclass
class Outcome:
    """What the visit stage did, including deciding not to happen."""

    ok: bool = True
    skipped: bool = False
    reason: str = ""
    visits: list[Visit] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [error for visit in self.visits for error in visit.errors]


def off_because(stack: Any, visit_allowed: bool) -> str:
    """Why the browser check will not run, or "" if it will (D25, deliverable 4).

    Stated rather than assumed: a check that silently does not happen is worse
    than one that is switched off on purpose, because only one of the two can be
    read back out of a report.
    """
    if not visit_allowed:
        return (
            "this machine's policy turns the browser check off "
            "(gate: visit: false)"
        )
    if not getattr(stack, "visit", True):
        return f"the {stack.name} stack turns the browser check off (visit: false)"
    if not eyes.available():
        return (
            "there is no browser on this machine. "
            "`pip install 'applace[eyes]'` then `playwright install chromium`"
        )
    return ""


def run(
    paths: ApplacePaths,
    conn: Connection,
    *,
    app_id: str,
    slug: str,
    root: Path,
    dist: Path,
    routes: list[Route],
) -> Outcome:
    """Serve the built app and open every route. Never raises on a bad page."""
    if not (dist / "index.html").is_file():
        # A stack whose build writes somewhere else, or a build that produced
        # nothing to look at. Not this stage's business to decide which.
        return Outcome(
            skipped=True,
            reason=f"the build left no {dist.name}/index.html to open",
        )

    needs_gateway = bool(apis.declarations(conn, app_id))
    server, origin = _serve(dist, paths, slug, proxy=needs_gateway)
    try:
        shots = eyes.tour(
            [
                eyes.Stop(url=route.url(origin), selectors=route.expectations())
                for route in routes
            ],
            screenshot=False,
        )
    except eyes.EyesUnavailable as exc:
        # The browser was there when the switch was read and is not there now,
        # or it could not open our own server. Either way it is a finding about
        # this machine, not about the app: red, and say so in those words.
        return Outcome(
            ok=False,
            visits=[
                Visit(
                    route=routes[0].path,
                    url=routes[0].url(origin),
                    ok=False,
                    errors=[Diagnostic(message=str(exc))],
                )
            ],
        )
    finally:
        server.shutdown()
        server.server_close()

    visits = [
        _judge(route, shot, root=root, dist=dist, origin=origin)
        for route, shot in zip(routes, shots)
    ]
    return Outcome(ok=all(visit.ok for visit in visits), visits=visits)


def _judge(
    route: Route, shot: eyes.Shot, *, root: Path, dist: Path, origin: str
) -> Visit:
    """Turn what the browser saw into the gate's own verdict on one route."""
    errors: list[Diagnostic] = []
    for message in shot.errors:
        file, line = locate(message.source, dist=dist, root=root, origin=origin)
        errors.append(
            Diagnostic(
                message=f"{route.path} in the browser: {message.text}",
                file=file,
                line=line,
            )
        )
    if shot.blank:
        errors.append(
            Diagnostic(
                message=(
                    f"{route.path} built and served, and the page is empty. "
                    f"Nothing a person could read was rendered."
                )
            )
        )
    if route.selector and not shot.present.get(route.selector, False):
        errors.append(
            Diagnostic(
                message=(
                    f"{route.path} was declared to show {route.selector!r}, and "
                    f"nothing on the page matches it."
                )
            )
        )
    if route.text and route.text not in shot.text:
        errors.append(
            Diagnostic(
                message=(
                    f"{route.path} was declared to say {route.text!r}, and the "
                    f"page does not."
                )
            )
        )
    return Visit(
        route=route.path,
        url=shot.url,
        ok=not errors,
        title=shot.title,
        blank=shot.blank,
        errors=errors,
        console=[message.as_dict() for message in shot.errors],
        failed_requests=[request.as_dict() for request in shot.failed_requests],
    )


# -- the server the browser talks to ----------------------------------------


def _serve(
    dist: Path, paths: ApplacePaths, slug: str, *, proxy: bool
) -> tuple[ThreadingHTTPServer, str]:
    """Put the built app on an ephemeral loopback port. Returns it and its origin.

    Ephemeral, and not a port from the machine's ledger (M12): the ledger exists
    so two long-lived servers do not pick the same number, and a port the kernel
    hands out cannot collide with anything by definition. This one lives for the
    length of one gate.
    """
    bound = type(
        "BoundHandler",
        (_Handler,),
        {"paths": paths, "slug": slug, "key": "", "proxying": proxy},
    )
    server = ThreadingHTTPServer(
        (HOST, 0), partial(bound, directory=str(dist))
    )
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{HOST}:{server.server_address[1]}"


class _Handler(gateway.Handler, SinglePageHandler):
    """The built app, plus the one non-file path it is allowed to call.

    It inherits the gateway's request handling rather than reimplementing it:
    the declaration is the allowlist (D24), and a second copy of that rule --
    one for the preview, one for the gate -- is a rule that will disagree with
    itself the first time either is touched.

    What it does not inherit is the key. The page's own `fetch` cannot add a
    header it was never told about, so this port accepts a call that a browser
    made to the page it is serving, and nothing else. That is a same-origin
    check and not a boundary; what makes it acceptable is the rest of the
    sentence -- a port the kernel chose at random, bound to loopback, alive for
    the few seconds of one gate.
    """

    paths: ApplacePaths
    slug: str
    proxying: bool = False

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silent: the gate's output is the report, and a green visit that prints
        # forty asset requests to stderr buries it.
        pass

    def do_GET(self) -> None:
        self._sort("GET")

    def do_HEAD(self) -> None:
        self._sort("HEAD")

    def do_POST(self) -> None:
        self._sort("POST")

    def do_PUT(self) -> None:
        self._sort("PUT")

    def do_PATCH(self) -> None:
        self._sort("PATCH")

    def do_DELETE(self) -> None:
        self._sort("DELETE")

    def _sort(self, method: str) -> None:
        """A file, or a call to the gateway. There is no third kind of request."""
        under = _under_gateway(self.path)
        if under is None:
            if method == "GET":
                SinglePageHandler.do_GET(self)
            elif method == "HEAD":
                SinglePageHandler.do_HEAD(self)
            else:
                self.send_error(405, "this is a static server")
            return
        if not self.proxying:
            # The app declared no API, so nothing here is meant to answer. Say
            # 404 exactly as the real deployment would if the function were not
            # generated -- a silent 200 would hide a missing declaration.
            self._say(404, {"ok": False, "error": "this app declared no API"})
            return
        self.path = f"/{self.slug}/{under}"
        gateway.Handler._handle(self, method)

    def _authorised(self) -> bool:
        """Same origin, meaning: the browser looking at the page we are serving."""
        if self.headers.get("Sec-Fetch-Site") == "same-origin":
            return True
        referer = self.headers.get("Referer", "")
        mine = f"http://{self.headers.get('Host', '')}/"
        return bool(referer) and referer.startswith(mine)


def _under_gateway(path: str) -> str | None:
    """What follows ``/api/gateway/`` in this request, or None if it is a file."""
    prefix = apis.GATEWAY_PATH.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix) :]


# -- from a line of the bundle back to a line of the source -----------------
#
# A built app's stack trace points at `assets/index-C7f3.js:1:48213`, which is
# true and useless. When the build shipped a sourcemap next to it, that position
# names a file and a line a person can open, and this is the arithmetic that
# turns one into the other. When it did not, the bundle's own position is still
# better than nothing and is reported as it is.

_POSITION = re.compile(r"^(?P<url>.*?):(?P<line>\d+)(?::(?P<column>\d+))?$")

# A stack frame, which is a position with a function name in front of it:
# `handleClick (http://127.0.0.1:5173/assets/index-C7f3.js:9:51169)`. An
# uncaught exception arrives in this shape and a console error does not, and
# both have to reach the same arithmetic.
_FRAME = re.compile(r"^.*?\((?P<inside>[^()]+)\)$")
_MAP_COMMENT = re.compile(r"^//[#@]\s*sourceMappingURL=(\S+)\s*$", re.MULTILINE)
_DATA_URL = re.compile(r"^data:application/json;(?:charset=[^;]+;)?base64,(.*)$")

# The base64 alphabet a sourcemap's variable-length quantities are written in.
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def locate(
    source: str, *, dist: Path, root: Path, origin: str
) -> tuple[str, int | None]:
    """``file, line`` for a browser position, mapped through the sourcemap.

    ``source`` is what the eyes recorded: a URL with a line and usually a
    column. Everything about this is best-effort -- it is a better error
    message, never a decision -- so anything unreadable comes back as the
    position the browser gave, and no exception ever leaves here.
    """
    if not source:
        return "", None
    cleaned = source.strip()
    framed = _FRAME.match(cleaned)
    if framed is not None:
        cleaned = framed.group("inside").strip()
    found = _POSITION.match(cleaned)
    if found is None:
        return source, None
    url = found.group("url")
    line = int(found.group("line"))
    column = int(found.group("column") or 0)
    if not url.startswith(origin):
        # Something the page pulled from elsewhere. Not this repository's file.
        return url, line

    relative = unquote(urlparse(url[len(origin):]).path).lstrip("/")
    bundle = (dist / relative).resolve()
    try:
        bundle.relative_to(dist.resolve())
    except ValueError:  # pragma: no cover - the server would not have served it
        return relative, line
    original = _through_sourcemap(bundle, line - 1, max(column - 1, 0), root=root)
    if original is not None:
        return original
    return relative, line


def _through_sourcemap(
    bundle: Path, line: int, column: int, *, root: Path
) -> tuple[str, int] | None:
    """The source position for a zero-based position in ``bundle``."""
    raw = _sourcemap_for(bundle)
    if raw is None:
        return None
    sources = raw.get("sources")
    mappings = raw.get("mappings")
    if not isinstance(sources, list) or not isinstance(mappings, str):
        return None
    found = _segment(mappings, line, column)
    if found is None:
        return None
    index, original_line = found
    if not 0 <= index < len(sources):
        return None
    return _readable(str(sources[index]), bundle.parent, raw, root), original_line + 1


def _sourcemap_for(bundle: Path) -> dict[str, Any] | None:
    """The map beside a built file, or the one its last comment points at."""
    for candidate in _candidates(bundle):
        try:
            if isinstance(candidate, dict):
                return candidate
            found = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(found, dict):
            return found
    return None


def _candidates(bundle: Path) -> list[Path | dict[str, Any]]:
    """Where a map might be: beside the file, or named by its own comment."""
    out: list[Path | dict[str, Any]] = []
    sibling = bundle.with_name(bundle.name + ".map")
    if sibling.is_file():
        out.append(sibling)
    try:
        content = bundle.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    named = _MAP_COMMENT.findall(content)
    if not named:
        return out
    reference = named[-1]
    inline = _DATA_URL.match(reference)
    if inline is None:
        out.append(bundle.parent / reference)
        return out
    try:
        decoded = json.loads(base64.b64decode(inline.group(1)))
    except (ValueError, json.JSONDecodeError):
        return out
    if isinstance(decoded, dict):
        out.append(decoded)
    return out


def _segment(mappings: str, line: int, column: int) -> tuple[int, int] | None:
    """The ``(source index, source line)`` covering a generated position.

    The decoding has to start at the beginning: a segment's source index and
    source line are deltas against the running total for the whole file, and
    only the generated column resets at each line.
    """
    source_index = 0
    source_line = 0
    source_column = 0
    best: tuple[int, int] | None = None
    for number, group in enumerate(mappings.split(";")):
        generated_column = 0
        if not group:
            continue
        for piece in group.split(","):
            values = _vlq(piece)
            if not values:
                continue
            generated_column += values[0]
            if len(values) < 4:
                # A segment that names no source: a stretch of generated code
                # that came from nowhere in particular.
                continue
            source_index += values[1]
            source_line += values[2]
            source_column += values[3]
            if number == line and generated_column <= column:
                best = (source_index, source_line)
        if number == line:
            return best
    return None


def _vlq(piece: str) -> list[int]:
    """Base64 variable-length quantities, as the sourcemap spec writes them."""
    values: list[int] = []
    shift = 0
    accumulated = 0
    for character in piece:
        digit = _B64.find(character)
        if digit < 0:
            return []
        accumulated += (digit & 31) << shift
        if digit & 32:
            shift += 5
            continue
        # The low bit is the sign, which is why this is not just a shift.
        value = accumulated >> 1
        values.append(-value if accumulated & 1 else value)
        shift = 0
        accumulated = 0
    return values


def _readable(source: str, map_dir: Path, raw: dict[str, Any], root: Path) -> str:
    """A source name from a map, as a path in the app's repository if it is one."""
    prefix = str(raw.get("sourceRoot") or "")
    joined = f"{prefix.rstrip('/')}/{source}" if prefix else source
    if "://" in joined:
        return joined
    try:
        resolved = (map_dir / joined).resolve()
        return str(resolved.relative_to(root.resolve()))
    except (OSError, ValueError):
        return joined.lstrip("./")


__all__ = [
    "DEFAULT_ROUTE",
    "Outcome",
    "Route",
    "RouteError",
    "Visit",
    "build",
    "declarations",
    "declare",
    "forget",
    "locate",
    "off_because",
    "run",
    "to_visit",
]
