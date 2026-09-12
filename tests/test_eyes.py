"""The eyes: what a browser sees, and what it says went wrong.

The pure tests run anywhere. The ones marked `needs_browser` drive a real
Chromium against a real HTTP server, because the whole point of this module is
that it reports what actually happens in a browser -- a mocked page would only
prove the mock.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from applace import eyes

PAGES = {
    "index.html": """\
<!doctype html>
<html><head><title>A working page</title></head>
<body><h1>It renders</h1></body></html>
""",
    "throws.html": """\
<!doctype html>
<html><head><title>Broken</title></head>
<body><div id="root"></div>
<script>
  const items = undefined
  document.getElementById('root').textContent = items.map(x => x).join('')
</script>
</body></html>
""",
    "missing-api.html": """\
<!doctype html>
<html><head><title>Calls an endpoint that is not there</title></head>
<body><p>Loading</p>
<script>fetch('/api/teams').then(r => r.json())</script>
</body></html>
""",
    "noisy.html": """\
<!doctype html>
<html><head><title>Noisy</title></head>
<body><p>Fine</p>
<script>
  console.log('[vite] connected.')
  console.warn('a warning worth keeping')
  console.error('an error worth keeping')
</script>
</body></html>
""",
}


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real HTTP server on a real port, serving the pages above."""
    root = tmp_path_factory.mktemp("site")
    for name, body in PAGES.items():
        (root / name).write_text(body, encoding="utf-8")
    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture(scope="module")
def browser() -> None:
    if not eyes.available():
        pytest.skip("no chromium; run `playwright install chromium`")


def shot(**kwargs: object) -> eyes.Shot:
    defaults: dict[str, object] = {"url": "http://localhost/", "title": "t", "png": b""}
    return eyes.Shot(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_a_page_with_no_text_is_reported_as_blank() -> None:
    assert shot(text_length=0).blank is True
    assert shot(text_length=12).blank is False


def test_errors_are_the_console_messages_that_are_errors() -> None:
    picture = shot(
        console=[
            eyes.ConsoleMessage(level="info", text="hello"),
            eyes.ConsoleMessage(level="error", text="boom"),
            eyes.ConsoleMessage(level="warning", text="careful"),
        ]
    )
    assert [message.text for message in picture.errors] == ["boom"]


def test_the_report_carries_everything_but_the_picture() -> None:
    report = shot(
        console=[eyes.ConsoleMessage(level="error", text="boom")],
        failed_requests=[eyes.FailedRequest(url="/api", method="GET", status=404)],
        text_length=3,
    ).report()
    assert report["blank"] is False
    assert report["viewport"] == {"width": eyes.DEFAULT_WIDTH, "height": eyes.DEFAULT_HEIGHT}
    assert report["errors"] == [{"level": "error", "text": "boom", "source": ""}]
    assert report["failed_requests"][0]["status"] == 404
    assert "png" not in report


def test_playwrights_console_types_reduce_to_three_levels() -> None:
    assert eyes._level("error") == "error"
    assert eyes._level("assert") == "error"
    assert eyes._level("warning") == "warning"
    assert eyes._level("log") == "info"
    assert eyes._level("debug") == "info"


def test_a_response_under_400_is_not_a_failed_request() -> None:
    failed: list[eyes.FailedRequest] = []
    eyes._record_bad_status(failed, _Response(200))
    assert failed == []
    eyes._record_bad_status(failed, _Response(404))
    assert [(f.status, f.method) for f in failed] == [(404, "GET")]


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.url = "http://127.0.0.1/api/teams"
        self.request = type("R", (), {"method": "GET"})()


def test_a_missing_browser_is_an_instruction_not_a_stack_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("playwright"):
            raise ImportError("no playwright here")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert eyes.browser_path() is None
    with pytest.raises(eyes.EyesUnavailable) as raised:
        eyes.capture("http://127.0.0.1:1/")
    assert "playwright install" in str(raised.value)


def test_a_browser_that_is_not_downloaded_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playwright names an executable whether or not it was ever downloaded."""
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert eyes.browser_path() is None


@pytest.mark.needs_browser
def test_a_page_that_renders_comes_back_clean(browser: None, site: str) -> None:
    picture = eyes.capture(f"{site}/index.html")
    assert picture.title == "A working page"
    assert picture.blank is False
    assert picture.errors == []
    assert picture.failed_requests == []
    assert picture.png.startswith(b"\x89PNG")


@pytest.mark.needs_browser
def test_a_page_that_throws_is_diagnosed_without_looking_at_it(
    browser: None, site: str
) -> None:
    """The M4 promise: the console names the bug even though the screen is white."""
    picture = eyes.capture(f"{site}/throws.html")
    assert picture.blank is True
    assert len(picture.errors) == 1
    failure = picture.errors[0]
    assert failure.text.startswith("TypeError: ")
    assert "reading 'map'" in failure.text
    # And where, so the fix does not need the screen either.
    assert "throws.html" in failure.source


@pytest.mark.needs_browser
def test_a_call_to_an_endpoint_that_does_not_exist_is_reported(
    browser: None, site: str
) -> None:
    picture = eyes.capture(f"{site}/missing-api.html")
    failed = [request for request in picture.failed_requests if "/api/teams" in request.url]
    assert failed, picture.report()
    assert failed[0].status == 404


@pytest.mark.needs_browser
def test_the_dev_servers_own_chatter_is_dropped(browser: None, site: str) -> None:
    picture = eyes.capture(f"{site}/noisy.html")
    texts = [message.text for message in picture.console]
    assert "[vite] connected." not in texts
    assert "a warning worth keeping" in texts
    assert "an error worth keeping" in texts


@pytest.mark.needs_browser
def test_the_viewport_is_what_was_asked_for(browser: None, site: str) -> None:
    picture = eyes.capture(f"{site}/index.html", width=480, height=900)
    assert (picture.width, picture.height) == (480, 900)
    assert picture.report()["viewport"] == {"width": 480, "height": 900}


@pytest.mark.needs_browser
def test_a_url_that_answers_nothing_at_all_says_so(browser: None) -> None:
    with pytest.raises(eyes.EyesUnavailable) as raised:
        # Port 1 is reserved and nothing is listening: connection refused, which
        # is not a page misbehaving but a page that never existed.
        eyes.capture("http://127.0.0.1:1/")
    assert "could not open" in str(raised.value)
