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
from starlette.applications import Starlette
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
from .machine import Machine, folder
from .paths import ApplacePaths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.mcpserver import MCPServer

# Who may see whose work, answered by the backend that mounts the root panel.
# Applace authenticates nobody (D11); this is where somebody who does says so.
Visible = Callable[[Request, str], bool]

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


def card(
    paths: ApplacePaths, conn: Any, row: Any, *, prefix: str = "/panel"
) -> dict[str, Any]:
    """One app, as a chat message needs it: a state, a sentence, and the links.

    This is `get_app` folded down. The agent's version is exhaustive because it
    is about to write code; this one is about to be rendered next to a human's
    question, so it answers "what is it doing" before "what is in it".

    ``prefix`` is where this card is served from, because the only URL a card
    carries that is Applace's own is the screenshot: one home answers at
    ``/panel``, a machine's register answers at ``/panel/users/<user>``, and the
    picture has to be fetchable from wherever the card was read.
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
    # The register's definition, not a second one that could drift from it: a
    # machine-wide listing calling this app red while its own card says green
    # would be two Applaces (D27).
    state = str(detail.get("state") or "new")
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
        # Somebody else's repository, adopted as it stood (D26). A chat window
        # showing this app should not offer to regenerate it from a template.
        "taken_from": detail.get("taken_from"),
        "shot": f"{prefix}/apps/{slug}/shot.png" if shot.exists() else None,
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


def cards(
    paths: ApplacePaths, conn: Any, *, prefix: str = "/panel"
) -> list[dict[str, Any]]:
    return [card(paths, conn, row, prefix=prefix) for row in list_apps(conn)]


def fingerprint(payload: Any) -> str:
    """What "nothing has changed" means for the stream."""
    return json.dumps(payload, sort_keys=True, default=str)


async def events(
    paths: ApplacePaths,
    slug: str,
    disconnected: Callable[[], Awaitable[bool]] | None = None,
    prefix: str = "/panel",
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
        found = await to_thread.run_sync(_read, paths, slug, prefix)
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


def _read(
    paths: ApplacePaths, slug: str, prefix: str = "/panel"
) -> dict[str, Any] | None:
    """One card, on its own connection. None when there is no such app."""
    conn = connect(paths.db)
    try:
        return card(paths, conn, require_app(conn, slug), prefix=prefix)
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


# -- the same panel, one level up (D27) ------------------------------------
#
# A machine has one home per person and nobody lives in the machine. The
# developer who runs it needs the same cards, indexed by whose they are -- and
# needs to be able to say which of them a given request may see, because the
# root panel is the one view that spans people.


def machine_routes(machine: Machine, *, visible: Visible | None = None) -> list[Route]:
    """The panel for a whole root: an index of homes, and every home's cards.

    ``visible(request, user)`` is the backend's answer to "may this request see
    that person's work". Applace authenticates nobody (D11) and is not about to
    start: the caller has a session, a header or an SSO assertion, and only the
    caller can read it. Absent, every home is visible, which is the right
    default for the machine an operator runs on their own laptop and the wrong
    one for anything else -- so the CLI says which it is.

    A home the callable refuses answers **404, not 403**: a 403 confirms that
    the person exists, and on a machine whose home names are derived from email
    addresses that is a directory of the company for anyone who can guess.
    """

    def allowed(request: Request, user: str) -> bool:
        return visible is None or bool(visible(request, user))

    def home_of(key: str) -> tuple[str, ApplacePaths] | None:
        """The user id behind a URL segment, and where their Applace is.

        The segment is the home's directory name rather than the raw id: an id
        is whatever the caller's own system calls a person -- an email, a UUID,
        a display name with a slash in it -- and a URL cannot carry all of
        those. `/panel/users` hands out both, so a link is always makeable.
        """
        for record in machine.homes():
            if folder(str(record["user"])) == key:
                return str(record["user"]), ApplacePaths(
                    Path(str(record["home"])), ledger=machine.ledger
                )
        return None

    def resolve(request: Request) -> tuple[str, str, ApplacePaths] | None:
        key = str(request.path_params["user"])
        found = home_of(key)
        if found is None:
            return None
        user, paths = found
        if not allowed(request, user) or not paths.db.exists():
            return None
        return key, user, paths

    def prefix_for(key: str) -> str:
        return f"/panel/users/{key}"

    async def users_route(request: Request) -> Response:
        # `homes` and not `users`: the quota view walks every home's disk, and
        # an index page is not worth four hundred thousand stat calls.
        people = await to_thread.run_sync(machine.homes)
        every = await to_thread.run_sync(machine.apps)
        listed = [
            {
                "user": record["user"],
                "key": folder(str(record["user"])),
                "apps": sum(1 for app in every if app["user"] == record["user"]),
                "created_at": record["created_at"],
            }
            for record in people
            if allowed(request, str(record["user"]))
        ]
        return JSONResponse({"ok": True, "users": listed}, headers=CORS)

    async def all_apps_route(request: Request) -> Response:
        """Every app on the machine, from the journals alone.

        The register rather than the cards: this is the index of a machine that
        may hold hundreds of apps, and a card opens a repository.
        """
        found = await to_thread.run_sync(machine.apps)
        listed = [
            {**entry, "key": folder(str(entry["user"]))}
            for entry in found
            if allowed(request, str(entry["user"]))
        ]
        return JSONResponse({"ok": True, "apps": listed}, headers=CORS)

    async def home_apps_route(request: Request) -> Response:
        found = await to_thread.run_sync(resolve, request)
        if found is None:
            return _absent()
        key, user, paths = found

        def read() -> list[dict[str, Any]]:
            conn = connect(paths.db)
            try:
                return cards(paths, conn, prefix=prefix_for(key))
            finally:
                conn.close()

        return JSONResponse(
            {"ok": True, "user": user, "apps": await to_thread.run_sync(read)},
            headers=CORS,
        )

    async def home_card_route(request: Request) -> Response:
        found = await to_thread.run_sync(resolve, request)
        if found is None:
            return _absent()
        key, user, paths = found
        one = await to_thread.run_sync(
            _read, paths, str(request.path_params["slug"]), prefix_for(key)
        )
        if one is None:
            return _absent()
        return JSONResponse({"ok": True, "user": user, **one}, headers=CORS)

    async def home_events_route(request: Request) -> Response:
        found = await to_thread.run_sync(resolve, request)
        if found is None:
            return _absent()
        key, _, paths = found
        return StreamingResponse(
            events(
                paths,
                str(request.path_params["slug"]),
                request.is_disconnected,
                prefix_for(key),
            ),
            media_type="text/event-stream",
            headers={**CORS, "X-Accel-Buffering": "no"},
        )

    async def home_shot_route(request: Request) -> Response:
        found = await to_thread.run_sync(resolve, request)
        if found is None:
            return _absent()
        _, _, paths = found
        shot = paths.shot(str(request.path_params["slug"]))
        if not shot.exists():
            return _absent("nobody has looked at this app yet")
        return FileResponse(shot, media_type="image/png", headers=CORS)

    async def embed_route(request: Request) -> Response:
        return PlainTextResponse(
            EMBED, media_type="application/javascript", headers=CORS
        )

    async def index_route(request: Request) -> Response:
        return HTMLResponse(ROOT_INDEX, headers=CORS)

    return [
        Route("/panel", index_route, methods=["GET"]),
        Route("/panel/embed.js", embed_route, methods=["GET"]),
        Route("/panel/users", users_route, methods=["GET"]),
        Route("/panel/apps", all_apps_route, methods=["GET"]),
        Route("/panel/users/{user}/apps", home_apps_route, methods=["GET"]),
        Route("/panel/users/{user}/apps/{slug}", home_card_route, methods=["GET"]),
        Route(
            "/panel/users/{user}/apps/{slug}/events",
            home_events_route,
            methods=["GET"],
        ),
        Route(
            "/panel/users/{user}/apps/{slug}/shot.png",
            home_shot_route,
            methods=["GET"],
        ),
    ]


def machine_app(machine: Machine, *, visible: Visible | None = None) -> Starlette:
    """The root panel as an ASGI app, for a backend to mount or the CLI to serve.

    A company's own backend does the mounting in production -- it is the thing
    that knows who is asking -- and gets to pass ``visible``. What this gives
    the developer who runs the machine is the same view without writing a
    backend first.
    """
    return Starlette(routes=machine_routes(machine, visible=visible))


def serve_machine(
    machine: Machine,
    *,
    port: int,
    host: str = "127.0.0.1",
    visible: Visible | None = None,
) -> None:  # pragma: no cover - a blocking server
    """Serve the root panel. Read-only (D16): there is no tool on this port."""
    import uvicorn

    uvicorn.run(machine_app(machine, visible=visible), host=host, port=port,
                log_level="warning")


def _absent(why: str = "no such app") -> Response:
    """The one answer for "there is nothing here" and "not for you" (D27)."""
    return JSONResponse({"ok": False, "error": why}, status_code=404, headers=CORS)


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
//           user="alice-9f2c1b0d4e"  -- on a root panel, whose app it is (D27)
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
  static get observedAttributes() { return ['slug', 'base', 'height', 'view', 'user']; }

  connectedCallback() {
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<style>${CSS}</style><div class="card"></div>`;
    this.load();
  }

  disconnectedCallback() { this.source && this.source.close(); }
  attributeChangedCallback() { this.isConnected && this.load(); }

  get base() { return (this.getAttribute('base') || '').replace(/\\/$/, ''); }

  // One home answers at /panel; a machine's register answers at
  // /panel/users/<key>, and the same element draws a card from either.
  get prefix() {
    const user = this.getAttribute('user');
    return user ? `${this.base}/panel/users/${encodeURIComponent(user)}` : `${this.base}/panel`;
  }

  async load() {
    const slug = this.getAttribute('slug');
    if (!slug) return;
    this.source && this.source.close();
    try {
      const answer = await fetch(`${this.prefix}/apps/${slug}`);
      this.draw(await answer.json());
    } catch (error) {
      this.draw({ ok: false, app: slug, state: 'missing', headline: String(error) });
    }
    // Then keep it live: every stage of every build, as the journal records it.
    this.source = new EventSource(`${this.prefix}/apps/${slug}/events`);
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

ROOT_INDEX = """\
<!doctype html>
<meta charset="utf-8">
<title>Applace — this machine</title>
<style>
  body { font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
         color: #0f172a; background: #f8fafc }
  h1 { font-size: 1.4rem } h2 { font-size: 1rem; margin: 2rem 0 .25rem } p { color: #475569 }
  table { border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #e2e8f0;
          border-radius: 10px; overflow: hidden }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #f1f5f9; font-size: 14px }
  th { color: #64748b; font-weight: 600 }
  tr:last-child td { border-bottom: 0 }
  td.state span { font-size: 12px; padding: 1px 8px; border-radius: 999px; background: #f1f5f9 }
  td.state span[data-state=green] { background: #dcfce7; color: #166534 }
  td.state span[data-state=red] { background: #fee2e2; color: #991b1b }
  td.state span[data-state=new] { background: #e0f2fe; color: #075985 }
  a { color: #2563eb; text-decoration: none } a:hover { text-decoration: underline }
  applace-app { display: block; margin: 1rem 0 }
</style>
<h1>Applace</h1>
<p>Every app on this machine, whose it is, and where it can be reached. Read
   from each home's journal and nobody's files. Click an app to watch it live.</p>
<div id="homes"></div>
<div id="open"></div>
<script src="/panel/embed.js"></script>
<script>
  const escape = (text) => String(text ?? '').replace(/[<>&"]/g, (c) =>
    ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));

  const where = (app) => (app.exposed || [])
    .map((e) => `<a href="${e.url}" target="_blank">${escape(e.environment)}</a>`).join(' ');

  Promise.all([
    fetch('/panel/users').then((a) => a.json()),
    fetch('/panel/apps').then((a) => a.json()),
  ]).then(([{ users }, { apps }]) => {
    const homes = document.getElementById('homes');
    if (!users.length) { homes.innerHTML = '<p>No homes here yet.</p>'; return; }
    homes.innerHTML = users.map((user) => {
      const mine = apps.filter((app) => app.key === user.key);
      const rows = mine.map((app) => `
        <tr>
          <td><a href="#" data-user="${escape(app.key)}" data-app="${escape(app.app)}">${escape(app.app)}</a></td>
          <td>${escape(app.stack)}</td>
          <td class="state"><span data-state="${escape(app.state)}">${escape(app.state)}</span></td>
          <td>${escape((app.commit || '').slice(0, 12))}</td>
          <td>${where(app)}</td>
        </tr>`).join('');
      return `<h2>${escape(user.user)} — ${user.apps} apps</h2>
        <table><tr><th>App</th><th>Stack</th><th>State</th><th>Commit</th><th>Where</th></tr>
        ${rows || '<tr><td colspan="5">Nothing built here yet.</td></tr>'}</table>`;
    }).join('');
    homes.addEventListener('click', (event) => {
      const link = event.target.closest('a[data-app]');
      if (!link) return;
      event.preventDefault();
      document.getElementById('open').innerHTML =
        `<applace-app user="${link.dataset.user}" slug="${link.dataset.app}" height="420"></applace-app>`;
    });
  });
</script>
"""

__all__ = [
    "Visible",
    "attach",
    "card",
    "cards",
    "machine_app",
    "machine_routes",
    "routes",
    "serve_machine",
]
