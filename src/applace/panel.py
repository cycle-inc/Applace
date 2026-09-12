"""What a chat window needs to show what the agent is doing (D16).

A company already has a chatbot. Applace is what goes underneath it, which means
the harness has to be readable by something other than an agent: the person in
the conversation wants to see the page appear, watch the build, and click the
link. That view is served here, on the same port as the MCP tools, from the same
journal the agent writes to -- so a chat UI never polls git, never parses a
gate's output, and never needs a second process or a second set of credentials.

Everything in this module is **read-only**. Nothing here creates an app, writes a
file or deploys anything: those are tool calls, made by the agent, and a panel
that could make them would be a second way into the harness with none of the
rules. The panel inherits the trust of the machine it runs on (D11); who may see
an app is a later question, deliberately not answered here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from anyio import sleep, to_thread
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from .apps import app_detail, require_app
from .db import connect, list_apps
from .paths import ApplacePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.mcpserver import MCPServer

# How often the stream looks at the journal, and how often it says something
# even when nothing has happened. A build stage lasts seconds, so half a second
# is fast enough to feel live; the heartbeat is what keeps a proxy from closing
# an idle connection.
POLL = 0.5
HEARTBEAT = 15.0
# A stream that nobody closed should not outlive the conversation that opened it.
MAX_STREAM = 3600.0

# The panel is read-only and a company's chat UI is served from another origin
# than the harness. Refusing it would mean asking every integrator to run a
# proxy for four GET requests.
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
}


def card(paths: ApplacePaths, conn: Any, row: Any) -> dict[str, Any]:
    """One app, as a chat message needs it: a state, a sentence, and the links.

    This is `get_app` folded down. The agent's version is exhaustive because it
    is about to write code; this one is about to be rendered next to a human's
    question, so it answers "what is it doing" before "what is in it".
    """
    detail = app_detail(paths, conn, row)
    slug = str(detail["app"])
    if detail.get("missing"):
        return {
            "app": slug,
            "name": detail.get("name"),
            "state": "missing",
            "headline": "Its directory is gone; the app is only a row now.",
            "urls": {},
            "errors": [],
        }

    errors = list(detail.get("errors") or [])
    live = [
        entry for entry in (detail.get("deployments") or [])
        if entry.get("status") == "live" and entry.get("url")
    ]
    production = next(
        (entry for entry in live if entry.get("environment") == "production"), None
    )
    running = detail.get("preview") or None
    human = detail.get("human") or {}

    urls: dict[str, str] = {}
    if production is not None:
        urls["live"] = str(production["url"])
    elif live:
        urls["live"] = str(live[0]["url"])
    if isinstance(running, dict) and running.get("url"):
        urls["preview"] = str(running["url"])
    if detail.get("github_url"):
        urls["repo"] = str(detail["github_url"])
    if detail.get("pull_request_url"):
        urls["pull_request"] = str(detail["pull_request_url"])
    urls["dir"] = str(detail["path"])

    shot = paths.shot(slug)
    state = (
        "red" if errors
        else "green" if detail.get("commit")
        else "new"
    )
    return {
        "app": slug,
        "name": detail.get("name"),
        "description": detail.get("description"),
        "stack": detail.get("stack"),
        "state": state,
        "headline": headline(state, detail, urls, human),
        "failed_stage": detail.get("failed_stage"),
        "errors": errors,
        "urls": urls,
        "commit": detail.get("commit"),
        "branch": detail.get("branch"),
        "dirty": bool(detail.get("dirty")),
        "unpushed": int(detail.get("unpushed") or 0),
        "human": human if human.get("touched") else None,
        "shot": f"/panel/apps/{slug}/shot.png" if shot.exists() else None,
        "shot_at": int(shot.stat().st_mtime) if shot.exists() else None,
        "created_at": detail.get("created_at"),
    }


def headline(
    state: str, detail: dict[str, Any], urls: dict[str, str], human: dict[str, Any]
) -> str:
    """One sentence, because a chat bubble has room for one sentence."""
    if state == "new":
        return "Created, with nothing built into it yet."
    if state == "red":
        stage = detail.get("failed_stage") or "the build"
        count = len(detail.get("errors") or [])
        said = f"{count} error{'s' if count != 1 else ''}"
        parts = [f"The last write did not pass {stage}: {said} to fix."]
    elif "live" in urls:
        parts = ["Green, and what is live is this commit."]
    elif "preview" in urls:
        parts = ["Green, and running on this machine."]
    else:
        parts = ["Green, committed, and not running anywhere yet."]
    if human.get("touched"):
        parts.append("A person has been editing it too.")
    waiting = int(detail.get("unpushed") or 0)
    # Only worth saying when there is somewhere for them to go: an app on a
    # machine with no GitHub connection is not "behind", it is just local.
    if waiting and "repo" in urls:
        commits = "commit is" if waiting == 1 else "commits are"
        parts.append(f"{waiting} {commits} not on GitHub yet.")
    return " ".join(parts)


def cards(paths: ApplacePaths, conn: Any) -> list[dict[str, Any]]:
    return [card(paths, conn, row) for row in list_apps(conn)]


def fingerprint(payload: Any) -> str:
    """What "nothing has changed" means for the stream."""
    return json.dumps(payload, sort_keys=True, default=str)


async def events(
    paths: ApplacePaths,
    slug: str,
    disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """The app's state, now and whenever it changes, as server-sent events.

    The journal is the source of truth and this watches it, rather than being
    told: a gate, a push and a deploy already write everything down, and a
    notification bus between them and a chat window would be a second account of
    the same facts, able to disagree with the first. Half a second of latency is
    the price, and a build stage lasts longer than that.

    An async generator rather than a route so that what it emits can be read in
    a test without a socket.
    """
    last = ""
    quiet = 0.0
    waited = 0.0
    while waited < MAX_STREAM:
        if disconnected is not None and await disconnected():
            return
        found = await to_thread.run_sync(_read, paths, slug)
        if found is None:
            yield _event("gone", {"app": slug})
            return
        current = fingerprint(found)
        if current != last:
            last, quiet = current, 0.0
            yield _event("card", found)
        elif quiet >= HEARTBEAT:
            # Not news, but a proxy that sees nothing for a minute closes the
            # connection, and a chat window that reconnects loses the build.
            quiet = 0.0
            yield ": still here\n\n"
        await sleep(POLL)
        quiet += POLL
        waited += POLL


def _read(paths: ApplacePaths, slug: str) -> dict[str, Any] | None:
    """One card, on its own connection. None when there is no such app."""
    conn = connect(paths.db)
    try:
        return card(paths, conn, require_app(conn, slug))
    except Exception:
        return None
    finally:
        conn.close()


# -- the routes ------------------------------------------------------------


def routes(paths: ApplacePaths) -> list[Route]:
    """The panel's endpoints, as plain Starlette routes.

    Returned rather than mounted so they can be tested without an MCP server and
    attached to one without a second port.
    """

    def one(slug: str) -> dict[str, Any] | None:
        return _read(paths, slug)

    def many() -> list[dict[str, Any]]:
        conn = connect(paths.db)
        try:
            return cards(paths, conn)
        finally:
            conn.close()

    async def list_route(request: Request) -> Response:
        found = await to_thread.run_sync(many)
        return JSONResponse({"ok": True, "apps": found}, headers=CORS)

    async def card_route(request: Request) -> Response:
        found = await to_thread.run_sync(one, request.path_params["slug"])
        if found is None:
            return JSONResponse(
                {"ok": False, "error": "no such app"}, status_code=404, headers=CORS
            )
        return JSONResponse({"ok": True, **found}, headers=CORS)

    async def events_route(request: Request) -> Response:
        return StreamingResponse(
            events(paths, str(request.path_params["slug"]), request.is_disconnected),
            media_type="text/event-stream",
            # `X-Accel-Buffering` is for the reverse proxy a company will put in
            # front of this: without it nginx buffers the stream into silence.
            headers={**CORS, "X-Accel-Buffering": "no"},
        )

    async def shot_route(request: Request) -> Response:
        shot = paths.shot(str(request.path_params["slug"]))
        if not shot.exists():
            return JSONResponse(
                {"ok": False, "error": "nobody has looked at this app yet"},
                status_code=404,
                headers=CORS,
            )
        return FileResponse(shot, media_type="image/png", headers=CORS)

    async def embed_route(request: Request) -> Response:
        return PlainTextResponse(
            EMBED, media_type="application/javascript", headers=CORS
        )

    async def index_route(request: Request) -> Response:
        return HTMLResponse(INDEX, headers=CORS)

    return [
        Route("/panel", index_route, methods=["GET"]),
        Route("/panel/embed.js", embed_route, methods=["GET"]),
        Route("/panel/apps", list_route, methods=["GET"]),
        Route("/panel/apps/{slug}", card_route, methods=["GET"]),
        Route("/panel/apps/{slug}/events", events_route, methods=["GET"]),
        Route("/panel/apps/{slug}/shot.png", shot_route, methods=["GET"]),
    ]


def attach(server: MCPServer, paths: ApplacePaths) -> None:
    """Put the panel on the MCP server's own HTTP app (D16).

    One port, one process. On stdio these routes simply never fire.
    """
    for route in routes(paths):
        handler: Callable[[Request], Any] = route.endpoint
        server.custom_route(route.path, methods=["GET"], include_in_schema=False)(
            handler
        )


def _event(name: str, payload: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


# -- the embed -------------------------------------------------------------
#
# A custom element rather than a React component: a chat UI is somebody else's
# codebase, with somebody else's bundler and somebody else's React version, and
# "add one script tag" is the only integration that survives all of them.

EMBED = """\
// <applace-app slug="team-dashboard"></applace-app>
// Optional: base="http://127.0.0.1:8848" height="480" view="preview|shot|none"
const CSS = `
:host { display:block; font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; color:#0f172a }
.card { border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; background:#fff }
.head { display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid #f1f5f9 }
.name { font-weight:600 }
.pill { font-size:12px; padding:1px 8px; border-radius:999px; background:#f1f5f9; color:#475569 }
.pill[data-state=green] { background:#dcfce7; color:#166534 }
.pill[data-state=red] { background:#fee2e2; color:#991b1b }
.pill[data-state=new] { background:#e0f2fe; color:#075985 }
.pill[data-state=missing] { background:#f1f5f9; color:#64748b }
.say { padding:0 12px 10px; color:#475569 }
.frame { width:100%; height:var(--h,420px); border:0; border-top:1px solid #f1f5f9; background:#f8fafc; display:block }
img.frame { object-fit:cover; object-position:top }
.links { display:flex; flex-wrap:wrap; gap:8px; padding:10px 12px; border-top:1px solid #f1f5f9 }
a { color:#2563eb; text-decoration:none } a:hover { text-decoration:underline }
ul.errs { margin:0; padding:0 12px 12px 28px; color:#991b1b } ul.errs li { font-family:ui-monospace,monospace; font-size:12px }
`;

class ApplaceApp extends HTMLElement {
  static get observedAttributes() { return ['slug', 'base', 'height', 'view']; }

  connectedCallback() {
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<style>${CSS}</style><div class="card"></div>`;
    this.load();
  }

  disconnectedCallback() { this.source && this.source.close(); }
  attributeChangedCallback() { this.isConnected && this.load(); }

  get base() { return (this.getAttribute('base') || '').replace(/\\/$/, ''); }

  async load() {
    const slug = this.getAttribute('slug');
    if (!slug) return;
    this.source && this.source.close();
    try {
      const answer = await fetch(`${this.base}/panel/apps/${slug}`);
      this.draw(await answer.json());
    } catch (error) {
      this.draw({ ok: false, app: slug, state: 'missing', headline: String(error) });
    }
    // Then keep it live: every stage of every build, as the journal records it.
    this.source = new EventSource(`${this.base}/panel/apps/${slug}/events`);
    this.source.addEventListener('card', (event) => this.draw(JSON.parse(event.data)));
  }

  draw(card) {
    const urls = card.urls || {};
    const view = this.getAttribute('view') || 'preview';
    const height = this.getAttribute('height');
    if (height) this.style.setProperty('--h', `${height}px`);
    const page = urls.preview || urls.live;
    let frame = '';
    if (view === 'preview' && page) frame = `<iframe class="frame" src="${page}"></iframe>`;
    else if (view !== 'none' && card.shot)
      frame = `<img class="frame" src="${this.base}${card.shot}?t=${card.shot_at}" alt="">`;
    const link = (key, text) => (urls[key] ? `<a href="${urls[key]}" target="_blank">${text}</a>` : '');
    const errors = (card.errors || []).slice(0, 5)
      .map((e) => `<li>${(e.file ? e.file + ': ' : '') + (e.message || '')}</li>`).join('');
    this.shadowRoot.querySelector('.card').innerHTML = `
      <div class="head">
        <span class="name">${card.name || card.app}</span>
        <span class="pill" data-state="${card.state}">${card.state}</span>
      </div>
      <div class="say">${card.headline || ''}</div>
      ${errors ? `<ul class="errs">${errors}</ul>` : ''}
      ${frame}
      <div class="links">
        ${link('live', 'Live')} ${link('preview', 'Preview')}
        ${link('repo', 'Repository')} ${link('pull_request', 'Pull request')}
      </div>`;
    this.dispatchEvent(new CustomEvent('applace:card', { detail: card, bubbles: true }));
  }
}

customElements.define('applace-app', ApplaceApp);
"""

INDEX = """\
<!doctype html>
<meta charset="utf-8">
<title>Applace</title>
<style>
  body { font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 52rem;
         color: #0f172a; background: #f8fafc }
  h1 { font-size: 1.4rem } p { color: #475569 }
  applace-app { margin: 1rem 0 }
</style>
<h1>Applace</h1>
<p>Everything this machine has built. The same cards a chat window embeds with
   <code>&lt;script src="/panel/embed.js"&gt;</code>.</p>
<div id="apps"></div>
<script src="/panel/embed.js"></script>
<script>
  fetch('/panel/apps').then((answer) => answer.json()).then(({ apps }) => {
    document.getElementById('apps').innerHTML = apps.length
      ? apps.map((app) => `<applace-app slug="${app.app}" height="360"></applace-app>`).join('')
      : '<p>No apps yet. Ask an agent for one.</p>';
  });
</script>
"""

__all__ = ["attach", "card", "cards", "routes"]
