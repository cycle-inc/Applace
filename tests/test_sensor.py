"""The last stage of the gate: the build, in a browser (D25).

Two halves. The first needs nothing but a database -- what a route declaration
accepts, what the gate opens when nothing was declared, and how a page the
browser already looked at is judged. The second opens a real Chromium on a real
port and is skipped where there is none, because the bug this stage exists to
catch -- four green stages and a white screen -- has never once been visible to
a fake browser.
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
import yaml

from applace import env, eyes, gate, gitrepo, policy, sensor
from applace.apps import create_app, declare_api, require_app
from applace.db import Connection
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]

TOKEN = "sk-live-do-not-print-me"

# The fake stack builds nothing; this makes it build the one page a test wrote,
# so `out/index.html` is a real file produced by a real build command.
BUILD = "sh -c 'mkdir -p out && cp page.html out/index.html'"


@pytest.fixture(scope="module")
def browser() -> None:
    if not eyes.available():
        pytest.skip("no chromium; run `playwright install chromium`")


def _set_command(paths: ApplacePaths, name: str, command: str) -> None:
    manifest = paths.stacks / "fake" / "stack.yaml"
    data: dict[str, Any] = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"][name] = command
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def app(home: Home) -> tuple[ApplacePaths, Connection, Path]:
    paths, conn = home
    _set_command(paths, "build", BUILD)
    report = create_app(paths, conn, name="Seen App", stack_name="fake", install=False)
    return paths, conn, report.path


def write(paths: ApplacePaths, conn: Connection, body: str) -> gate.GateReport:
    """Write the page this app builds from, through the gate, as an agent would."""
    return gate.write_files(
        paths, conn, app="seen-app", files_to_write={"page.html": body}
    )


PAGE = """<!doctype html><html><head><title>Seen</title></head>
<body><h1 id="hello">Customers</h1><p>Nine of them.</p></body></html>
"""


# -- what a route declaration is --------------------------------------------


def test_a_route_is_a_path_on_this_apps_own_origin() -> None:
    assert sensor.build(path="customers").path == "/customers"
    assert sensor.build(path="/orders/recent/").path == "/orders/recent"
    assert sensor.build(path="/").path == "/"


def test_a_url_is_not_a_route_and_neither_is_a_way_out_of_the_app() -> None:
    for bad in ("https://example.com/customers", "/../etc/passwd", "/two words"):
        with pytest.raises(sensor.RouteError):
            sensor.build(path=bad)


def test_a_route_with_no_path_says_what_a_path_looks_like() -> None:
    with pytest.raises(sensor.RouteError) as raised:
        sensor.build(path="   ")
    assert "/customers" in str(raised.value)


def test_a_declared_route_comes_back_with_what_it_must_show(home: Home) -> None:
    paths, conn = home
    create_app(paths, conn, name="Seen App", stack_name="fake", install=False)
    app_id = str(require_app(conn, "seen-app")["id"])

    sensor.declare(
        conn,
        app_id=app_id,
        route=sensor.build(
            path="/customers", selector="table tbody tr", text="Nine", description="The list"
        ),
    )
    (route,) = sensor.declarations(conn, app_id)
    assert route.path == "/customers"
    assert route.selector == "table tbody tr"
    assert route.text == "Nine"
    assert route.description == "The list"


def test_declaring_the_same_path_twice_replaces_it(home: Home) -> None:
    paths, conn = home
    create_app(paths, conn, name="Seen App", stack_name="fake", install=False)
    app_id = str(require_app(conn, "seen-app")["id"])

    sensor.declare(conn, app_id=app_id, route=sensor.build(path="/", text="First"))
    sensor.declare(conn, app_id=app_id, route=sensor.build(path="/", text="Second"))
    routes = sensor.declarations(conn, app_id)
    assert [route.text for route in routes] == ["Second"]

    assert sensor.forget(conn, app_id=app_id, path="/") is True
    assert sensor.forget(conn, app_id=app_id, path="/") is False


def test_an_app_that_declared_nothing_still_gets_its_front_page_opened(
    home: Home,
) -> None:
    paths, conn = home
    create_app(paths, conn, name="Seen App", stack_name="fake", install=False)
    app_id = str(require_app(conn, "seen-app")["id"])
    assert [route.path for route in sensor.to_visit(conn, app_id)] == ["/"]


# -- when the check does not run, and says so -------------------------------


class _Stack:
    def __init__(self, visit: bool) -> None:
        self.name = "fake"
        self.visit = visit


def test_the_policy_can_turn_the_browser_check_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eyes, "available", lambda: True)
    assert "policy" in sensor.off_because(_Stack(visit=True), False)


def test_a_stack_whose_pages_need_a_login_can_turn_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eyes, "available", lambda: True)
    assert "fake stack" in sensor.off_because(_Stack(visit=False), True)


def test_a_machine_with_no_browser_gets_the_install_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eyes, "available", lambda: False)
    assert "playwright install chromium" in sensor.off_because(_Stack(True), True)


def test_with_a_browser_and_no_switch_thrown_the_check_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eyes, "available", lambda: True)
    assert sensor.off_because(_Stack(visit=True), True) == ""


def test_a_build_that_left_nothing_to_open_is_skipped_not_failed(home: Home) -> None:
    paths, conn = home
    create_app(paths, conn, name="Seen App", stack_name="fake", install=False)
    app_id = str(require_app(conn, "seen-app")["id"])
    outcome = sensor.run(
        paths,
        conn,
        app_id=app_id,
        slug="seen-app",
        root=paths.apps / "seen-app",
        dist=paths.apps / "seen-app" / "out",
        routes=[sensor.Route(path="/")],
    )
    assert outcome.skipped and outcome.ok
    assert "out/index.html" in outcome.reason


# -- the verdict on one page ------------------------------------------------


def shot(**kwargs: Any) -> eyes.Shot:
    defaults: dict[str, Any] = {"url": "http://127.0.0.1:1/", "title": "t", "png": b""}
    return eyes.Shot(**{**defaults, **kwargs})


def judge(route: sensor.Route, page: eyes.Shot, tmp_path: Path) -> sensor.Visit:
    return sensor._judge(
        route, page, root=tmp_path, dist=tmp_path / "out", origin="http://127.0.0.1:1"
    )


def test_a_page_that_renders_and_shows_what_was_promised_is_green(tmp_path: Path) -> None:
    route = sensor.Route(path="/customers", selector="table", text="Nine")
    visit = judge(
        route,
        shot(text="Nine of them", text_length=12, present={"table": True}),
        tmp_path,
    )
    assert visit.ok and visit.errors == []


def test_a_clean_build_and_an_empty_page_is_the_failure_this_stage_exists_for(
    tmp_path: Path,
) -> None:
    visit = judge(sensor.Route(path="/"), shot(text_length=0), tmp_path)
    assert not visit.ok and visit.blank
    assert "the page is empty" in visit.errors[0].message


def test_a_selector_that_is_not_there_names_the_route_and_the_selector(
    tmp_path: Path,
) -> None:
    route = sensor.Route(path="/customers", selector="table tbody tr")
    visit = judge(
        route, shot(text="anything", text_length=8, present={"table tbody tr": False}), tmp_path
    )
    assert not visit.ok
    assert "/customers" in visit.errors[0].message
    assert "table tbody tr" in visit.errors[0].message


def test_text_the_page_was_declared_to_say_and_does_not(tmp_path: Path) -> None:
    route = sensor.Route(path="/", text="Nine of them")
    visit = judge(route, shot(text="Loading...", text_length=10), tmp_path)
    assert not visit.ok
    assert "declared to say" in visit.errors[0].message


def test_a_console_error_is_reported_as_the_browsers_own_words(tmp_path: Path) -> None:
    page = shot(
        text="x",
        text_length=1,
        console=[
            eyes.ConsoleMessage(
                level="error", text="TypeError: rows.map is not a function", source=""
            )
        ],
    )
    visit = judge(sensor.Route(path="/customers"), page, tmp_path)
    assert not visit.ok
    assert visit.errors[0].message == (
        "/customers in the browser: TypeError: rows.map is not a function"
    )
    assert visit.console[0]["level"] == "error"


# -- from the bundle back to the source -------------------------------------


def _bundle(root: Path, *, mapped: bool) -> Path:
    """A built chunk in `out/assets`, with or without the map beside it."""
    assets = root / "out" / "assets"
    assets.mkdir(parents=True)
    chunk = assets / "index.js"
    chunk.write_text("console.log(1)\n//# sourceMappingURL=index.js.map\n", encoding="utf-8")
    if mapped:
        # One segment: generated line 1, column 1, from source 0, line 5.
        (assets / "index.js.map").write_text(
            json.dumps({"version": 3, "sources": ["../../src/App.tsx"], "mappings": "AAIA"}),
            encoding="utf-8",
        )
    (root / "src").mkdir(exist_ok=True)
    return chunk


def test_a_position_in_the_bundle_becomes_a_file_a_person_can_open(tmp_path: Path) -> None:
    _bundle(tmp_path, mapped=True)
    file, line = sensor.locate(
        "http://127.0.0.1:1/assets/index.js:1:5",
        dist=tmp_path / "out",
        root=tmp_path,
        origin="http://127.0.0.1:1",
    )
    assert file == "src/App.tsx"
    assert line == 5


def test_without_a_map_the_bundles_own_position_is_still_reported(tmp_path: Path) -> None:
    _bundle(tmp_path, mapped=False)
    file, line = sensor.locate(
        "http://127.0.0.1:1/assets/index.js:1:5",
        dist=tmp_path / "out",
        root=tmp_path,
        origin="http://127.0.0.1:1",
    )
    assert file == "assets/index.js"
    assert line == 1


def test_an_uncaught_exceptions_stack_frame_maps_like_any_other_position(
    tmp_path: Path,
) -> None:
    """A pageerror arrives as `name (url:line:col)`; the same arithmetic applies."""
    _bundle(tmp_path, mapped=True)
    file, line = sensor.locate(
        "App (http://127.0.0.1:1/assets/index.js:1:5)",
        dist=tmp_path / "out",
        root=tmp_path,
        origin="http://127.0.0.1:1",
    )
    assert (file, line) == ("src/App.tsx", 5)


def test_a_file_from_somewhere_else_is_left_alone(tmp_path: Path) -> None:
    file, line = sensor.locate(
        "https://cdn.example.com/thing.js:12:3",
        dist=tmp_path / "out",
        root=tmp_path,
        origin="http://127.0.0.1:1",
    )
    assert (file, line) == ("https://cdn.example.com/thing.js", 12)


def test_a_position_that_is_not_one_is_not_invented(tmp_path: Path) -> None:
    assert sensor.locate(
        "pageerror", dist=tmp_path / "out", root=tmp_path, origin="http://127.0.0.1:1"
    ) == ("pageerror", None)
    assert sensor.locate(
        "", dist=tmp_path / "out", root=tmp_path, origin="http://127.0.0.1:1"
    ) == ("", None)


# -- the server the browser talks to ----------------------------------------


class Upstream:
    """The company API, recording what it was sent."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

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
                upstream.seen.append({"path": self.path, "headers": dict(self.headers)})
                body = json.dumps({"count": 9}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    try:
        yield server
    finally:
        server.close()


def fetch(url: str, **headers: str) -> tuple[int, str]:
    request = urllib.request.Request(url)
    for name, value in headers.items():
        request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.fixture
def serving(app: tuple[ApplacePaths, Connection, Path]) -> Iterator[tuple[Any, str]]:
    """The sensor's own server, over a one-page build, with no API declared."""
    paths, _, root = app
    dist = root / "out"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(PAGE, encoding="utf-8")
    server, origin = sensor._serve(dist, paths, "seen-app", proxy=False)
    try:
        yield server, origin
    finally:
        server.shutdown()
        server.server_close()


def test_any_route_serves_the_single_page(serving: tuple[Any, str]) -> None:
    _, origin = serving
    for path in ("/", "/customers", "/orders/recent"):
        status, body = fetch(f"{origin}{path}")
        assert status == 200
        assert "Customers" in body


def test_an_app_that_declared_no_api_gets_the_404_the_deployment_would_give(
    serving: tuple[Any, str],
) -> None:
    _, origin = serving
    status, body = fetch(
        f"{origin}/api/gateway/crm/customers", Sec_Fetch_Site="same-origin"
    )
    assert status == 404
    assert "declared no API" in body


def test_a_call_that_did_not_come_from_the_page_is_not_answered(
    app: tuple[ApplacePaths, Connection, Path], upstream: Upstream
) -> None:
    """Loopback is not a boundary; the page we are serving is the only caller."""
    paths, conn, root = app
    declare_api(
        paths,
        conn,
        "seen-app",
        name="crm",
        base_url=f"{upstream.base}/api",
        token_env="CRM_TOKEN",
        paths=["customers/**"],
        methods=["GET"],
    )
    env.set_value(paths, "seen-app", "CRM_TOKEN", TOKEN)
    dist = root / "out"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(PAGE, encoding="utf-8")
    server, origin = sensor._serve(dist, paths, "seen-app", proxy=True)
    try:
        assert fetch(f"{origin}/api/gateway/crm/customers")[0] == 403
        status, body = fetch(
            f"{origin}/api/gateway/crm/customers", Sec_Fetch_Site="same-origin"
        )
        assert status == 200
        assert TOKEN not in body
        assert upstream.seen[-1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    finally:
        server.shutdown()
        server.server_close()


# -- the whole stage, through the gate --------------------------------------


def test_a_page_that_renders_is_committed(
    browser: None, app: tuple[ApplacePaths, Connection, Path]
) -> None:
    paths, conn, root = app
    report = write(paths, conn, PAGE)
    assert report.ok, [error.as_dict() for error in report.errors]
    assert report.stage == gate.VISIT
    assert report.committed
    assert [visit.route for visit in report.visits] == ["/"]
    assert report.visits[0].title == "Seen"
    assert not gitrepo.is_dirty(root)


def test_four_green_stages_and_a_white_screen_is_a_red_gate(
    browser: None, app: tuple[ApplacePaths, Connection, Path]
) -> None:
    paths, conn, root = app
    report = write(paths, conn, "<!doctype html><html><body></body></html>")
    assert not report.ok
    assert report.stage == gate.VISIT
    assert not report.committed
    assert any("the page is empty" in error.message for error in report.errors)
    # D4: the files are still there to fix, and nothing was committed over them.
    assert gitrepo.is_dirty(root)


def test_a_page_that_throws_comes_back_with_the_browsers_own_words(
    browser: None, app: tuple[ApplacePaths, Connection, Path]
) -> None:
    paths, conn, _ = app
    report = write(
        paths,
        conn,
        "<!doctype html><html><body><h1>Hi</h1>"
        "<script>null.rows.map(r => r)</script></body></html>",
    )
    assert not report.ok
    assert report.stage == gate.VISIT
    assert any("TypeError" in error.message for error in report.errors)


def test_a_declared_route_is_opened_and_its_promise_checked(
    browser: None, app: tuple[ApplacePaths, Connection, Path]
) -> None:
    paths, conn, _ = app
    app_id = str(require_app(conn, "seen-app")["id"])
    sensor.declare(
        conn,
        app_id=app_id,
        route=sensor.build(path="/customers", selector="table", text="Nine"),
    )
    report = write(paths, conn, PAGE)
    assert not report.ok
    assert [visit.route for visit in report.visits] == ["/customers"]
    messages = " ".join(error.message for error in report.errors)
    assert "'table'" in messages

    sensor.declare(
        conn,
        app_id=app_id,
        route=sensor.build(path="/customers", selector="#hello", text="Nine of them"),
    )
    again = write(paths, conn, PAGE)
    assert again.ok, [error.as_dict() for error in again.errors]


def test_the_switch_is_stated_rather_than_the_check_silently_skipped(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    paths.policy.write_text("gate:\n  visit: false\n", encoding="utf-8")
    assert policy.load(paths).visit is False

    report = write(paths, conn, PAGE)
    assert report.ok and report.committed
    visit = next(stage for stage in report.stages if stage.name == gate.VISIT)
    assert visit.skipped
    assert "gate: visit: false" in visit.reason


def test_the_built_app_reaches_its_api_through_the_gateway_it_does_not_own(
    browser: None, app: tuple[ApplacePaths, Connection, Path], upstream: Upstream
) -> None:
    """The M13/M14 seam: a declared API answers during the visit, with its token."""
    paths, conn, _ = app
    declare_api(
        paths,
        conn,
        "seen-app",
        name="crm",
        base_url=f"{upstream.base}/api",
        token_env="CRM_TOKEN",
        paths=["customers/**"],
        methods=["GET"],
    )
    env.set_value(paths, "seen-app", "CRM_TOKEN", TOKEN)
    app_id = str(require_app(conn, "seen-app")["id"])
    sensor.declare(
        conn, app_id=app_id, route=sensor.build(path="/", text="9 of them")
    )

    report = write(
        paths,
        conn,
        "<!doctype html><html><body><h1 id=hello>Customers</h1>"
        "<script>"
        "fetch('/api/gateway/crm/customers')"
        ".then(r => r.json())"
        ".then(d => { document.body.textContent = d.count + ' of them' })"
        ".catch(e => { document.body.textContent = 'failed: ' + e })"
        "</script></body></html>",
    )
    assert report.ok, [error.as_dict() for error in report.errors]
    seen = upstream.seen[-1]
    assert seen["path"] == "/api/customers"
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
