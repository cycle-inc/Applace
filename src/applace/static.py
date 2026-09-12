"""The server behind the `local` deploy target.

A built single-page app is a directory of files plus one rule: a URL that names
no file is not a 404, it is the router's problem, so it gets `index.html`. No
static file server we could shell out to is on every machine, and Applace drives
Node rather than being written in it (D10) -- so this is a hundred lines of
stdlib, started as ``python -m applace.static``.

The rule has an edge that matters for the eyes (D5): a *missing asset* must
still 404. Answering `/assets/index-abc.js` with an HTML document would turn a
broken build into a page that renders nothing with a console full of syntax
errors, which is exactly the diagnosis Applace exists to make easy. So the
fallback applies only to paths that look like routes -- no file extension.
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INDEX = "index.html"


class SinglePageHandler(SimpleHTTPRequestHandler):
    """Static files, with `index.html` for anything that looks like a route."""

    protocol_version = "HTTP/1.1"

    def send_head(self):  # type: ignore[no-untyped-def] - stdlib signature
        route = self.path.split("?", 1)[0].split("#", 1)[0]
        candidate = Path(self.translate_path(route))
        looks_like_a_file = Path(route).suffix != ""
        if not candidate.exists() and not looks_like_a_file:
            self.path = f"/{INDEX}"
        return super().send_head()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # One line per request, to the log the deployment records. A human
        # reading it wants to know which asset 404'd, not the date twice.
        sys.stdout.write(f"{self.address_string()} {format % args}\n")
        sys.stdout.flush()


def serve(root: Path, host: str, port: int) -> None:
    if not (root / INDEX).is_file():
        raise SystemExit(f"{root} holds no {INDEX}: there is nothing to serve.")
    handler = partial(SinglePageHandler, directory=str(root))
    server = ThreadingHTTPServer((host, port), handler)
    sys.stdout.write(f"serving {root} on http://{host}:{port}/\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - a human pressing ^C
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m applace.static", description=__doc__
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    serve(args.root.resolve(), args.host, args.port)


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    main()
