"""The process that holds the credential (D24).

Everything here is about a *negative*: the browser never sees the token, another
process on the machine cannot borrow the gateway (D20), and a call nobody
declared does not reach the upstream. The upstream in these tests is a real HTTP
server that records what it was sent, because the only convincing way to test
"the token arrived, and only there" is to look at what arrived.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from applace import env, gateway, preview
from applace.apps import create_app, declare_api, start_preview
from applace.db import Connection, find_gateway
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]

KEY = "test-gateway-key"
TOKEN = "sk-live-do-not-print-me"


class Upstream:
    """The company API. It records every request, headers and all."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                upstream.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": self.rfile.read(length).decode() if length else "",
                    }
                )
                body = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _record
            do_POST = _record
            do_DELETE = _record

        return Handler


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def served(home: Home, upstream: Upstream) -> Iterator[tuple[ApplacePaths, Connection, str]]:
    """An app with one declared API, and a gateway in this process serving it."""
    paths, conn = home
    create_app(paths, conn, name="Gate App", stack_name="fake", install=False)
    declare_api(
        paths,
        conn,
        "gate-app",
        name="crm",
        base_url=f"{upstream.base}/api",
        token_env="CRM_TOKEN",
        paths=["customers/**"],
        methods=["GET", "POST"],
    )
    env.set_value(paths, "gate-app", "CRM_TOKEN", TOKEN)

    handler = type("Bound", (gateway.Handler,), {"paths": paths, "key": KEY})
    server = ThreadingHTTPServer((gateway.HOST, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield paths, conn, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def call(
    url: str, *, key: str | None = KEY, method: str = "GET", data: bytes | None = None
) -> tuple[int, str]:
    request = urllib.request.Request(url, method=method, data=data)
    if key is not None:
        request.add_header(gateway.KEY_HEADER, key)
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# -- the credential ---------------------------------------------------------


def test_the_token_is_added_on_the_way_out_and_the_caller_never_sent_it(
    served: tuple[ApplacePaths, Connection, str], upstream: Upstream
) -> None:
    _, _, base = served
    status, body = call(f"{base}/gate-app/crm/customers/42?limit=20")
    assert status == 200
    assert TOKEN not in body

    seen = upstream.seen[-1]
    assert seen["path"] == "/api/customers/42?limit=20"
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_the_key_never_reaches_the_upstream(
    served: tuple[ApplacePaths, Connection, str], upstream: Upstream
) -> None:
    _, _, base = served
    call(f"{base}/gate-app/crm/customers")
    assert gateway.KEY_HEADER.lower() not in {
        name.lower() for name in upstream.seen[-1]["headers"]
    }


def test_a_declared_api_with_no_value_yet_says_who_supplies_it(
    served: tuple[ApplacePaths, Connection, str], upstream: Upstream
) -> None:
    paths, _, base = served
    env.unset_value(paths, "gate-app", "CRM_TOKEN")
    status, body = call(f"{base}/gate-app/crm/customers")
    assert status == 503
    assert "applace env set gate-app CRM_TOKEN" in body
    assert upstream.seen == []


# -- who may call it (D20) --------------------------------------------------


def test_another_process_on_the_machine_is_not_this_home(
    served: tuple[ApplacePaths, Connection, str], upstream: Upstream
) -> None:
    """Loopback is not a boundary on a machine holding many homes."""
    _, _, base = served
    for wrong in (None, "", "guessed"):
        status, body = call(f"{base}/gate-app/crm/customers", key=wrong)
        assert status == 403
        assert "not for you" in body
    assert upstream.seen == []


def test_health_needs_no_key_and_says_nothing(
    served: tuple[ApplacePaths, Connection, str],
) -> None:
    _, _, base = served
    status, body = call(f"{base}/__health", key=None)
    assert status == 200
    assert json.loads(body) == {"ok": True}


# -- what it refuses --------------------------------------------------------


@pytest.mark.parametrize(
    "path, method, status",
    [
        ("/gate-app/crm/admin", "GET", 403),
        ("/gate-app/crm/customers/1", "DELETE", 403),
        ("/gate-app/crm/customers/../../admin", "GET", 403),
        ("/gate-app/billing/invoices", "GET", 403),
        ("/other-app/crm/customers", "GET", 404),
        ("/gate-app", "GET", 404),
    ],
)
def test_an_undeclared_call_does_not_reach_the_upstream(
    served: tuple[ApplacePaths, Connection, str],
    upstream: Upstream,
    path: str,
    method: str,
    status: int,
) -> None:
    _, _, base = served
    answered, _ = call(f"{base}{path}", method=method)
    assert answered == status
    assert upstream.seen == []


def test_a_declared_post_goes_through_with_its_body(
    served: tuple[ApplacePaths, Connection, str], upstream: Upstream
) -> None:
    _, _, base = served
    status, _ = call(
        f"{base}/gate-app/crm/customers", method="POST", data=b'{"name":"x"}'
    )
    assert status == 200
    assert upstream.seen[-1]["body"] == '{"name":"x"}'


def test_the_policy_is_read_per_request_not_per_declaration(
    served: tuple[ApplacePaths, Connection, str], upstream: Upstream
) -> None:
    """A machine tightened today must refuse an app declared yesterday."""
    paths, _, base = served
    status, _ = call(f"{base}/gate-app/crm/customers")
    assert status == 200
    paths.policy.write_text("apis:\n  hosts:\n    - crm.internal\n", encoding="utf-8")
    status, body = call(f"{base}/gate-app/crm/customers")
    assert status == 403
    assert "crm.internal" in body
    assert len(upstream.seen) == 1


# -- the supervisor ---------------------------------------------------------


def test_the_gateway_starts_once_and_stops_when_asked(home: Home) -> None:
    paths, conn = home
    running = gateway.ensure(paths, conn)
    try:
        assert gateway.ensure(paths, conn).pid == running.pid
        assert find_gateway(conn) is not None
        with urllib.request.urlopen(f"{running.url}/__health", timeout=10) as answer:
            assert answer.status == 200
    finally:
        assert gateway.stop(conn) is True
    assert gateway.stop(conn) is False
    assert gateway.status(conn) is None


def test_the_key_is_written_once_and_kept_to_its_owner(paths: ApplacePaths) -> None:
    first = gateway.key_for(paths)
    assert gateway.key_for(paths) == first
    path = paths.env / gateway.KEY_NAME
    assert path.stat().st_mode & 0o777 == 0o600


def test_what_the_dev_server_is_told_cannot_be_inlined_by_a_bundler() -> None:
    running = gateway.Running(
        url="http://127.0.0.1:5300", port=5300, pid=1, log=Path("x"), started_at=""
    )
    values = gateway.environment(running, "gate-app", KEY)
    assert values == {
        "APPLACE_GATEWAY": "http://127.0.0.1:5300/gate-app",
        "APPLACE_GATEWAY_KEY": KEY,
    }
    # A bundler only inlines what carries the stack's prefix.
    assert not any(name.startswith("TEST_") or name.startswith("VITE_") for name in values)


def test_only_an_app_that_declared_something_gets_a_gateway(
    home: Home, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dev server is not a party to this: it is handed a URL or it is not."""
    paths, conn = home
    given: list[dict[str, str]] = []

    def fake_start(*args: Any, **kwargs: Any) -> None:
        given.append(dict(kwargs["environment"]))

    monkeypatch.setattr(preview, "start", fake_start)

    create_app(paths, conn, name="Plain App", stack_name="fake", install=False)
    start_preview(paths, conn, "plain-app")
    assert "APPLACE_GATEWAY" not in given[-1]
    assert find_gateway(conn) is None

    create_app(paths, conn, name="Wired App", stack_name="fake", install=False)
    declare_api(
        paths, conn, "wired-app", name="crm", base_url=f"{upstream.base}/api"
    )
    start_preview(paths, conn, "wired-app")
    try:
        assert given[-1]["APPLACE_GATEWAY"].endswith("/wired-app")
        assert given[-1]["APPLACE_GATEWAY_KEY"] == gateway.key_for(paths)
        assert find_gateway(conn) is not None
    finally:
        gateway.stop(conn)
