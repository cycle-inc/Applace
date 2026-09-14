"""Calling an API as the person using the app (D28, M17).

The negative here is sharper than D24's. There, the question was whether a
credential could reach the browser. Here it is whether one person's call can
come back with another person's data -- so every test that matters looks at
what the *upstream* was sent, and at what the exchange was asked, rather than
at what the gateway returned.

The exchange and the upstream are real HTTP servers on loopback: a fake that
agrees with the code proves nothing about a mechanism whose whole point is that
two independent processes have to agree on one shape.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from applace import apis, env, gateway, machine
from applace.apps import create_app, declare_api
from applace.db import Connection, connect
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]

KEY = "test-delegated-key"
SECRET = "sk-exchange-must-never-be-printed"
OWNER = "alice@example.com"


class Exchange:
    """The company's endpoint: one token per subject, and it remembers every ask."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.expires_in: int | None = 300
        self.status = 200
        self.body: dict[str, Any] | None = None
        # What to mint for a given assertion. A token that contained the
        # assertion would make "the assertion did not go upstream" unprovable,
        # which is the one thing the upstream is looked at for.
        self.mints: dict[str, str] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/token"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        exchange = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                asked = json.loads(self.rfile.read(length) or b"{}")
                asked["_authorization"] = self.headers.get("Authorization", "")
                exchange.asked.append(asked)

                if exchange.body is not None:
                    payload: dict[str, Any] = dict(exchange.body)
                else:
                    who = asked.get("subject") or asked.get("assertion") or "?"
                    payload = {"token": exchange.mints.get(who, f"token-for-{who}")}
                    if exchange.expires_in is not None:
                        payload["expires_in"] = exchange.expires_in
                raw = json.dumps(payload).encode()
                self.send_response(exchange.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


class Upstream:
    """The company API. It echoes back the credential it was handed."""

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

            def do_GET(self) -> None:
                upstream.seen.append(
                    {"path": self.path, "headers": dict(self.headers.items())}
                )
                body = json.dumps(
                    {"as": self.headers.get("Authorization", "")}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """No token survives a test. The cache is process-wide, on purpose (D28)."""
    gateway._TOKENS.clear()
    yield
    gateway._TOKENS.clear()


@pytest.fixture
def exchange() -> Iterator[Exchange]:
    server = Exchange()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    try:
        yield server
    finally:
        server.close()


Served = tuple[ApplacePaths, Connection, str]


@pytest.fixture
def served(home: Home, exchange: Exchange, upstream: Upstream) -> Iterator[Served]:
    """A home that knows whose it is, with one delegated API and a live gateway."""
    paths, conn = home
    machine.remember_whose(paths, OWNER)
    create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
    declare_api(
        paths,
        conn,
        "billing-app",
        name="billing",
        base_url=f"{upstream.base}/v1",
        on_behalf_of={"url": exchange.url, "secret_env": "EXCHANGE_SECRET"},
        paths=["invoices/**"],
        methods=["GET"],
    )
    env.set_value(paths, "billing-app", "EXCHANGE_SECRET", SECRET)

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
    url: str, *, key: str | None = KEY, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    if key is not None:
        request.add_header(gateway.KEY_HEADER, key)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# -- the declaration --------------------------------------------------------


def test_an_api_is_the_apps_or_the_persons_and_never_both() -> None:
    with pytest.raises(apis.ApiError) as caught:
        apis.build(
            name="billing",
            base_url="https://billing.internal",
            token_env="BILLING_TOKEN",
            on_behalf_of={"url": "https://auth.internal/t", "secret_env": "S"},
        )
    assert "both" in str(caught.value)


@pytest.mark.parametrize(
    "block, says",
    [
        ({"secret_env": "S"}, "exchange URL"),
        ({"url": "auth.internal/t", "secret_env": "S"}, "exchange URL"),
        ({"url": "https://auth.internal/t?x=1", "secret_env": "S"}, "query string"),
        ({"url": "https://auth.internal/t"}, "secret_env"),
        ({"url": "https://auth.internal/t", "secret_env": "s-s"}, "variable name"),
        (
            {"url": "https://auth.internal/t", "secret_env": "S", "token": "oops"},
            "does not take token",
        ),
        (
            {"url": "https://auth.internal/t", "secret_env": "S",
             "assertion_header": "not a header"},
            "usable header name",
        ),
    ],
)
def test_a_broken_exchange_block_is_refused_by_name(
    block: dict[str, Any], says: str
) -> None:
    with pytest.raises(Exception) as caught:
        apis.build(name="billing", base_url="https://billing.internal", on_behalf_of=block)
    assert says in str(caught.value)


def test_the_declaration_survives_a_round_trip_through_the_journal(
    home: Home, exchange: Exchange
) -> None:
    paths, conn = home
    create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
    out = declare_api(
        paths,
        conn,
        "billing-app",
        name="billing",
        base_url="https://billing.internal",
        on_behalf_of={
            "url": exchange.url,
            "secret_env": "EXCHANGE_SECRET",
            "assertion_header": "X-User-Assertion",
        },
    )
    assert out["delegated"] is True
    assert out["token_env"] == ""
    assert "whoever is using billing-app" in out["calls_as"]
    # The human still has one thing to supply, and it is named, not valued.
    assert "EXCHANGE_SECRET" in out["needs"]
    assert SECRET not in json.dumps(out)

    [read_back] = apis.declarations(conn, str(conn.execute(
        "SELECT id FROM apps WHERE slug = 'billing-app'"
    ).fetchone()["id"]))
    assert read_back.delegated
    assert read_back.on_behalf_of is not None
    assert read_back.on_behalf_of.secret_env == "EXCHANGE_SECRET"
    assert read_back.on_behalf_of.assertion_header == "X-User-Assertion"


def test_the_exchange_host_goes_through_the_same_policy_as_the_upstream(
    home: Home,
) -> None:
    paths, conn = home
    paths.policy.write_text(
        "apis:\n  hosts: ['billing.internal']\n", encoding="utf-8"
    )
    create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
    with pytest.raises(Exception) as caught:
        declare_api(
            paths,
            conn,
            "billing-app",
            name="billing",
            base_url="https://billing.internal",
            on_behalf_of={"url": "https://elsewhere.example/t", "secret_env": "S"},
        )
    assert "elsewhere.example" in str(caught.value)


# -- whose home this is -----------------------------------------------------


def test_a_home_learns_whose_it_is_once_and_does_not_change_hands(
    paths: ApplacePaths,
) -> None:
    assert machine.whose(paths) == ""
    assert machine.remember_whose(paths, OWNER) == OWNER
    assert machine.whose(paths) == OWNER
    # A second caller does not win, and does not raise: the first answer stands.
    assert machine.remember_whose(paths, "mallory@example.com") == OWNER
    assert machine.whose(paths) == OWNER


def test_naming_a_home_leaves_the_rest_of_its_config_alone(
    paths: ApplacePaths,
) -> None:
    paths.config.write_text(json.dumps({"github": {"owner": "cycle-inc"}}), "utf-8")
    machine.remember_whose(paths, OWNER)
    data = json.loads(paths.config.read_text(encoding="utf-8"))
    assert data["github"] == {"owner": "cycle-inc"}
    assert data["whose"] == OWNER


def test_the_machine_names_every_home_it_makes(tmp_path: Path) -> None:
    root = machine.Machine(tmp_path / "srv")
    root.user(OWNER)
    assert machine.whose(root.home_of(OWNER)) == OWNER
    # And a home made before M17 is named the next time its owner appears.
    older = root.home_of("bob@example.com")
    older.create()
    assert machine.whose(older) == ""
    root.user("bob@example.com")
    assert machine.whose(older) == "bob@example.com"


# -- the call ---------------------------------------------------------------


def test_the_upstream_is_called_with_a_token_minted_for_the_home_s_owner(
    served: Served, exchange: Exchange, upstream: Upstream
) -> None:
    _, _, base = served
    status, body = call(f"{base}/billing-app/billing/invoices/42")
    assert status == 200
    assert json.loads(body)["as"] == f"Bearer token-for-{OWNER}"

    # What the exchange was asked, exactly: no assertion, the home's own id, and
    # Applace saying so rather than pretending a door vouched for it (D28).
    [asked] = exchange.asked
    assert asked["app"] == "billing-app"
    assert asked["api"] == "billing"
    assert asked["subject"] == OWNER
    assert asked["asserted_by"] == "applace"
    assert "assertion" not in asked
    assert asked["_authorization"] == f"Bearer {SECRET}"

    # And the secret itself went nowhere near the upstream.
    assert SECRET not in json.dumps(upstream.seen)


def test_an_assertion_on_the_request_is_what_is_exchanged_and_never_forwarded(
    served: Served, exchange: Exchange, upstream: Upstream
) -> None:
    _, _, base = served
    exchange.mints["Bearer session-of-carol"] = "minted-elsewhere"
    status, body = call(
        f"{base}/billing-app/billing/invoices/42",
        headers={"Authorization": "Bearer session-of-carol"},
    )
    assert status == 200
    assert json.loads(body)["as"] == "Bearer minted-elsewhere"

    [asked] = exchange.asked
    assert asked["assertion"] == "Bearer session-of-carol"
    assert asked["asserted_by"] == "runtime"
    assert "subject" not in asked

    # The assertion was minted for the exchange. The upstream is not the
    # exchange, and what reached it is the exchanged token and nothing else.
    [seen] = upstream.seen
    assert "session-of-carol" not in json.dumps(seen)


def test_a_declared_assertion_header_is_read_and_stripped_wherever_it_is(
    home: Home, exchange: Exchange, upstream: Upstream
) -> None:
    paths, conn = home
    machine.remember_whose(paths, OWNER)
    create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
    declare_api(
        paths,
        conn,
        "billing-app",
        name="billing",
        base_url=f"{upstream.base}/v1",
        on_behalf_of={
            "url": exchange.url,
            "secret_env": "EXCHANGE_SECRET",
            "assertion_header": "X-User-Assertion",
        },
        paths=["invoices/**"],
    )
    env.set_value(paths, "billing-app", "EXCHANGE_SECRET", SECRET)
    handler = type("Bound", (gateway.Handler,), {"paths": paths, "key": KEY})
    server = ThreadingHTTPServer((gateway.HOST, 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        status, _ = call(
            f"http://{host}:{port}/billing-app/billing/invoices/1",
            headers={"X-User-Assertion": "proof-of-dave"},
        )
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert exchange.asked[0]["assertion"] == "proof-of-dave"
    assert "x-user-assertion" not in {k.lower() for k in upstream.seen[0]["headers"]}


def test_two_homes_previewing_the_same_app_get_two_different_tokens(
    tmp_path: Path, exchange: Exchange, upstream: Upstream, fake_stack: Path
) -> None:
    """The whole milestone in one test: same app, same declaration, two people."""
    root = machine.Machine(tmp_path / "srv")
    seen: list[str] = []
    for user in ("alice@example.com", "bob@example.com"):
        root.user(user)  # makes the home, and names it (D28)
        paths = root.home_of(user)
        shutil.copytree(fake_stack, paths.stacks / "fake", dirs_exist_ok=True)
        conn = connect(paths.db)
        try:
            create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
            declare_api(
                paths,
                conn,
                "billing-app",
                name="billing",
                base_url=f"{upstream.base}/v1",
                on_behalf_of={"url": exchange.url, "secret_env": "EXCHANGE_SECRET"},
                paths=["invoices/**"],
            )
        finally:
            conn.close()
        env.set_value(paths, "billing-app", "EXCHANGE_SECRET", SECRET)

        handler = type("Bound", (gateway.Handler,), {"paths": paths, "key": KEY})
        server = ThreadingHTTPServer((gateway.HOST, 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address[:2]
        try:
            status, body = call(
                f"http://{host}:{port}/billing-app/billing/invoices/42"
            )
        finally:
            server.shutdown()
            server.server_close()
        assert status == 200
        seen.append(json.loads(body)["as"])

    assert seen == [
        "Bearer token-for-alice@example.com",
        "Bearer token-for-bob@example.com",
    ]
    # Neither token is anywhere in either journal: nothing is kept (D28).
    for user in ("alice@example.com", "bob@example.com"):
        raw = root.home_of(user).db.read_bytes()
        assert b"token-for-" not in raw
        assert SECRET.encode() not in raw


# -- the refusals -----------------------------------------------------------


def test_a_home_that_is_nobody_s_is_a_401_and_not_somebody_else_s_token(
    home: Home, exchange: Exchange, upstream: Upstream
) -> None:
    paths, conn = home  # deliberately not named
    create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
    declare_api(
        paths,
        conn,
        "billing-app",
        name="billing",
        base_url=f"{upstream.base}/v1",
        on_behalf_of={"url": exchange.url, "secret_env": "EXCHANGE_SECRET"},
        paths=["invoices/**"],
    )
    env.set_value(paths, "billing-app", "EXCHANGE_SECRET", SECRET)
    handler = type("Bound", (gateway.Handler,), {"paths": paths, "key": KEY})
    server = ThreadingHTTPServer((gateway.HOST, 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        status, body = call(f"http://{host}:{port}/billing-app/billing/invoices/1")
    finally:
        server.shutdown()
        server.server_close()

    assert status == 401
    assert "does not know whose it is" in json.loads(body)["error"]
    assert "whoami" in json.loads(body)["error"]
    # Nothing was asked of anybody, and nothing reached the upstream.
    assert exchange.asked == []
    assert upstream.seen == []


def test_an_unanswered_secret_is_a_503_naming_the_command(
    home: Home, exchange: Exchange, upstream: Upstream
) -> None:
    paths, conn = home
    machine.remember_whose(paths, OWNER)
    create_app(paths, conn, name="Billing App", stack_name="fake", install=False)
    declare_api(
        paths,
        conn,
        "billing-app",
        name="billing",
        base_url=f"{upstream.base}/v1",
        on_behalf_of={"url": exchange.url, "secret_env": "EXCHANGE_SECRET"},
    )
    api = apis.declarations(conn, str(conn.execute(
        "SELECT id FROM apps WHERE slug = 'billing-app'"
    ).fetchone()["id"]))[0]
    refused = gateway.credential(paths, "billing-app", api)
    assert isinstance(refused, gateway.Refused)
    assert refused.status == 503
    assert "applace env set billing-app EXCHANGE_SECRET" in refused.reason
    assert exchange.asked == []


def test_a_refusal_from_the_exchange_passes_through_as_a_refusal(
    served: Served, exchange: Exchange, upstream: Upstream
) -> None:
    _, _, base = served
    exchange.status = 403
    exchange.body = {"error": "this person has no cabinet", "token": "leaked"}
    status, body = call(f"{base}/billing-app/billing/invoices/1")
    assert status == 403
    said = json.loads(body)["error"]
    assert "this person has no cabinet" in said
    # The status is the company's; the body is not. A token in an error body is
    # still a token, and repeating a whole body is how one gets copied out.
    assert "leaked" not in said
    assert upstream.seen == []


def test_an_exchange_that_is_not_there_is_a_502_and_not_a_pass_through(
    served: Served, exchange: Exchange, upstream: Upstream
) -> None:
    _, _, base = served
    exchange.close()
    status, body = call(f"{base}/billing-app/billing/invoices/1")
    assert status == 502
    assert "exchange did not answer" in json.loads(body)["error"]
    assert upstream.seen == []


def test_an_exchange_that_answers_without_a_token_is_a_502(
    served: Served, exchange: Exchange, upstream: Upstream
) -> None:
    _, _, base = served
    exchange.body = {"expires_in": 60}
    status, body = call(f"{base}/billing-app/billing/invoices/1")
    assert status == 502
    assert "without a token" in json.loads(body)["error"]
    assert upstream.seen == []


def test_the_declared_allowlist_still_governs_a_delegated_call(
    served: Served, exchange: Exchange
) -> None:
    _, _, base = served
    status, body = call(f"{base}/billing-app/billing/admin/wipe")
    assert status == 403
    assert "invoices/**" in json.loads(body)["error"]
    # Refused before anybody was asked for a token: the allowlist is first.
    assert exchange.asked == []


# -- the cache --------------------------------------------------------------


def test_a_second_call_inside_expires_in_does_not_reach_the_exchange(
    served: Served, exchange: Exchange
) -> None:
    _, _, base = served
    for _ in range(3):
        assert call(f"{base}/billing-app/billing/invoices/1")[0] == 200
    assert len(exchange.asked) == 1


def test_a_call_after_it_expires_asks_again(
    served: Served, exchange: Exchange
) -> None:
    _, _, base = served
    # Shorter than the skew, so nothing is ever cached: the same effect as an
    # expiry, without a test that sleeps.
    exchange.expires_in = 1
    for _ in range(3):
        assert call(f"{base}/billing-app/billing/invoices/1")[0] == 200
    assert len(exchange.asked) == 3


def test_an_exchange_that_says_nothing_about_expiry_is_not_cached(
    served: Served, exchange: Exchange
) -> None:
    _, _, base = served
    exchange.expires_in = None
    for _ in range(2):
        assert call(f"{base}/billing-app/billing/invoices/1")[0] == 200
    assert len(exchange.asked) == 2


def test_the_cache_does_not_serve_one_person_another_person_s_token(
    served: Served, exchange: Exchange
) -> None:
    _, _, base = served
    first = call(
        f"{base}/billing-app/billing/invoices/1",
        headers={"Authorization": "Bearer carol"},
    )[1]
    second = call(
        f"{base}/billing-app/billing/invoices/1",
        headers={"Authorization": "Bearer dave"},
    )[1]
    assert json.loads(first)["as"] == "Bearer token-for-Bearer carol"
    assert json.loads(second)["as"] == "Bearer token-for-Bearer dave"
    assert len(exchange.asked) == 2


def test_an_absurd_expires_in_is_not_believed_for_ever(
    served: Served, exchange: Exchange
) -> None:
    _, _, base = served
    exchange.expires_in = 10**9
    assert call(f"{base}/billing-app/billing/invoices/1")[0] == 200
    [(_, until)] = list(gateway._TOKENS.values())
    assert until - time.monotonic() <= apis.MAX_TOKEN_TTL


# -- the generated function -------------------------------------------------


def test_the_generated_function_carries_the_exchange_and_no_value(
    exchange: Exchange,
) -> None:
    source = apis.function_source(
        [
            apis.build(
                name="billing",
                base_url="https://billing.internal/v1",
                on_behalf_of={"url": exchange.url, "secret_env": "EXCHANGE_SECRET"},
            )
        ],
        app="billing-app",
    )
    assert '"secretEnv": "EXCHANGE_SECRET"' in source
    assert "process.env[exchange.secretEnv]" in source
    assert "asserted_by: 'runtime'" in source
    assert "import " not in source
    assert "require(" not in source
    assert SECRET not in source
    # A production call with nobody behind it is refused there too.
    assert "status: 401" in source


def test_the_generated_function_knows_which_app_it_belongs_to() -> None:
    source = apis.function_source(
        [apis.build(name="crm", base_url="https://crm.internal", token_env="T")],
        app="sales-board",
    )
    assert 'const APP = "sales-board";' in source
