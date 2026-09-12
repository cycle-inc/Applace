"""The eyes (D5).

A build that goes green and a page that renders are two different facts, and
until now Applace could only report the first. This module reports the second:
it opens a route in a headless Chromium and comes back with the picture, the
browser console, and every request that failed.

The console is the important half. A white page whose console says
``TypeError: Cannot read properties of undefined (reading 'map')`` is a diagnosis;
a screenshot of a white page is a riddle. An agent that cannot see the screen can
still fix the first one.

Playwright is an optional dependency and the browser binary is a separate
download, so everything here fails politely into an instruction rather than an
ImportError: a harness that only builds and pushes does not need 150 MB of
Chromium.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The default is a laptop, because that is what the person who asked for the app
# is looking at.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800

# Long enough for a cold dev server to compile the route on first request, short
# enough that a hung page is reported rather than waited on.
NAVIGATION_TIMEOUT_MS = 20_000

# After the network goes quiet, how long to let React finish painting.
SETTLE_MS = 400

INSTALL_HINT = (
    "The browser is not installed. Run `pip install 'applace[eyes]'` (or "
    "`uv sync --extra eyes`) and then `playwright install chromium`."
)

# Noise every dev server produces, which is not the app saying anything.
_IGNORED_CONSOLE = (
    "[vite] connecting...",
    "[vite] connected.",
    "Download the React DevTools",
)


class EyesUnavailable(Exception):
    """Playwright or its browser is not installed on this machine."""


class _PlaywrightTeardownNoise(logging.Filter):
    """Drops the two lines playwright's sync API leaves behind when it stops.

    Playwright runs its driver on a private event loop in another thread and
    stopping it leaves a pending task, which asyncio reports at garbage
    collection -- after our own output, on stderr, looking like a crash. It is
    not one, and an agent reading a tool's stderr should not have to know that.
    """

    _NOISE = ("Task was destroyed", "Future exception was never retrieved")

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self._NOISE)


def _hush() -> None:
    """Install the filter once, as late as possible -- only if eyes are used."""
    logger = logging.getLogger("asyncio")
    if not any(isinstance(f, _PlaywrightTeardownNoise) for f in logger.filters):
        logger.addFilter(_PlaywrightTeardownNoise())


@dataclass
class ConsoleMessage:
    level: str
    text: str
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "text": self.text, "source": self.source}


@dataclass
class FailedRequest:
    url: str
    method: str
    status: int | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Shot:
    url: str
    title: str
    png: bytes
    console: list[ConsoleMessage] = field(default_factory=list)
    failed_requests: list[FailedRequest] = field(default_factory=list)
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    text_length: int = 0

    @property
    def errors(self) -> list[ConsoleMessage]:
        return [message for message in self.console if message.level == "error"]

    @property
    def blank(self) -> bool:
        """True when the page rendered nothing a human would see.

        Cheap and useful: "it built, it served, and the screen is empty" is the
        single most common failure of a generated front end, and naming it saves
        the agent from having to interpret its own screenshot.
        """
        return self.text_length == 0

    def report(self) -> dict[str, Any]:
        """Everything but the image -- what goes in the JSON half of the result."""
        return {
            "url": self.url,
            "title": self.title,
            "viewport": {"width": self.width, "height": self.height},
            "blank": self.blank,
            "console": [message.as_dict() for message in self.console],
            "errors": [message.as_dict() for message in self.errors],
            "failed_requests": [request.as_dict() for request in self.failed_requests],
        }


def browser_path() -> str | None:
    """Where the Chromium Applace would drive lives, or None if there is none.

    Playwright reports an executable path even when the download never happened,
    so the file itself has to be looked for -- ``applace init`` saying "ok" for a
    browser that is not on disk would be worse than saying nothing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    _hush()
    try:
        with sync_playwright() as playwright:
            found = playwright.chromium.executable_path
    except Exception:
        return None
    return found if found and Path(found).exists() else None


def available() -> bool:
    """Is there a browser to drive? Asked by `applace init` and by the tool."""
    return browser_path() is not None


def capture(
    url: str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    full_page: bool = False,
) -> Shot:
    """Open ``url`` in a headless browser and report what happened.

    Never raises on a page that misbehaves: a route that 404s, throws, or paints
    nothing is exactly what this is for, and the report says so. It raises only
    when there is no browser to look with, or when the page never loads at all.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EyesUnavailable(INSTALL_HINT) from exc

    _hush()
    console: list[ConsoleMessage] = []
    failed: list[FailedRequest] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.set_default_timeout(NAVIGATION_TIMEOUT_MS)

                page.on("console", lambda message: _record(console, message))
                # An exception that escapes to the top is not a console message
                # in Playwright's model, and it is the one an agent most needs.
                page.on("pageerror", lambda error: _record_page_error(console, error))
                page.on("requestfailed", lambda request: failed.append(
                    FailedRequest(
                        url=request.url,
                        method=request.method,
                        error=(request.failure or "the request failed"),
                    )
                ))
                page.on("response", lambda response: _record_bad_status(failed, response))

                try:
                    page.goto(url, wait_until="networkidle")
                except PlaywrightTimeout:
                    # A page that never goes quiet is a finding, not a crash:
                    # take the picture of whatever it managed to paint and say
                    # what happened.
                    console.append(
                        ConsoleMessage(
                            level="warning",
                            text=f"the page was still busy after "
                                 f"{NAVIGATION_TIMEOUT_MS // 1000}s; this is what it "
                                 f"looked like at that point",
                            source="applace",
                        )
                    )
                page.wait_for_timeout(SETTLE_MS)

                # innerText, not textContent: textContent counts the source of
                # every inline <script> as text on the page, which would call a
                # blank page with a script tag in it "rendered".
                body = page.evaluate("document.body ? document.body.innerText : ''") or ""
                return Shot(
                    url=url,
                    title=page.title(),
                    png=page.screenshot(full_page=full_page),
                    console=console,
                    failed_requests=failed,
                    width=width,
                    height=height,
                    text_length=len(body.strip()),
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise EyesUnavailable(INSTALL_HINT) from exc
        raise EyesUnavailable(f"the browser could not open {url}: {message}") from exc


def _record(console: list[ConsoleMessage], message: Any) -> None:
    text = message.text
    if any(text.startswith(prefix) for prefix in _IGNORED_CONSOLE):
        return
    location = message.location or {}
    source = ""
    if location.get("url"):
        source = f"{location['url']}:{location.get('lineNumber', 0)}"
    console.append(
        ConsoleMessage(level=_level(message.type), text=text, source=source)
    )


def _record_page_error(console: list[ConsoleMessage], error: Any) -> None:
    """An uncaught exception, kept whole: the name, the message, and where.

    ``str(error)`` is the message alone -- "Cannot read properties of undefined
    (reading 'map')" without the "TypeError" in front of it, and without the
    file. Both halves are what makes this fixable from the report instead of
    from the screen, so both are kept.
    """
    name = getattr(error, "name", "") or "Error"
    message = getattr(error, "message", "") or str(error)
    console.append(
        ConsoleMessage(
            level="error",
            text=f"{name}: {message}",
            source=_first_frame(getattr(error, "stack", "") or "") or "pageerror",
        )
    )


def _first_frame(stack: str) -> str:
    """The app's own frame from a JS stack, which is the line to go and read."""
    for line in stack.splitlines():
        line = line.strip()
        if line.startswith("at ") and "node_modules" not in line:
            return line[len("at "):]
    return ""


def _level(kind: str) -> str:
    """Playwright's console types, reduced to the three that matter."""
    if kind in {"error", "assert"}:
        return "error"
    if kind in {"warning", "warn"}:
        return "warning"
    return "info"


def _record_bad_status(failed: list[FailedRequest], response: Any) -> None:
    """A 404 for a font or an API call is a completed request, not a failed one.

    Playwright only fires `requestfailed` when the request did not complete at
    all, so the status codes have to be watched separately -- and a generated
    front end calling an endpoint that does not exist is the failure mode this
    whole module is for.
    """
    if response.status < 400:
        return
    failed.append(
        FailedRequest(
            url=response.url,
            method=response.request.method,
            status=response.status,
            error=f"HTTP {response.status}",
        )
    )
