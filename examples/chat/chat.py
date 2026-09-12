#!/usr/bin/env python
"""A company's chatbot, with Applace underneath it (M10, D16).

This is the demo and the documentation: the smallest chat window that can be
asked for an app in English, build it, and show it to the person who asked.
Everything specific to Applace is in three places, and they are the three things
an integrator has to copy:

1. the tools come from the MCP server, over HTTP, as they are (`tools()`);
2. the system prompt is `get_skill`, so the model learns the harness from the
   harness rather than from a prompt somebody has to maintain (`opening()`);
3. the app itself is not rendered here at all -- the page embeds
   `<applace-app>` from `/panel/embed.js` and the harness streams its own state
   into it (`PAGE`).

What is *not* here is as deliberate. There is no database, no session, no user:
one conversation, in memory, because a company's chatbot already has all of
that and none of it is what Applace adds. There is no rendering of a build, no
polling of git, no retry logic around a failed typecheck: the gate already
refuses bad code and says why, and the model reads that answer.

Run it against a harness that is already serving:

    applace serve --http 8848 &
    uv run examples/chat/chat.py --provider mistral   # or openai, google, custom

Then open http://127.0.0.1:8900. The key is read from the environment or from
the `--env-file` file. A demo that talks to a paid model in a loop can spend
real money, so the run has a round cap per message and a budget in dollars, and
both end the turn rather than the process.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from anyio import to_thread
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.routing import Route

PROVIDERS: dict[str, dict[str, str]] = {
    "mistral": {
        "base": "https://api.mistral.ai/v1",
        "key": "MISTRAL_API_KEY",
        "model": "mistral-large-latest",
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "key": "OPENAI_API_KEY",
        "model": "gpt-5.5",
    },
    # OpenAI's newest models take function tools on /responses and nowhere else.
    "openai-responses": {
        "base": "https://api.openai.com/v1",
        "key": "OPENAI_API_KEY",
        "model": "gpt-6-astra",
        "api": "responses",
    },
    # Gemini answers the same shape as everyone else at this address.
    "google": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key": "GEMINI_API_KEY",
        "model": "gemini-3.8-flash",
    },
    # A company's own gateway is the common case: it speaks the same shape.
    "custom": {"base": "", "key": "LLM_API_KEY", "model": ""},
}

# Dollars per million tokens (prompt, completion), for the budget line. An
# unknown model is priced pessimistically rather than as free.
PRICES: list[tuple[str, tuple[float, float]]] = [
    ("mistral-large", (2.0, 6.0)),
    ("mistral-medium", (0.4, 2.0)),
    ("mistral-small", (0.1, 0.3)),
    ("gpt-4.1-mini", (0.4, 1.6)),
    ("gpt-4.1", (2.0, 8.0)),
    ("gemini-3", (2.0, 12.0)),
]
UNKNOWN_PRICE = (3.0, 15.0)

ROUNDS = 12  # tool rounds per message; a build is a handful, a loop is endless
TIMEOUT = 300
EYES = 3  # screenshots kept in the conversation; the rest become this line
OLD_PICTURE = "[capture plus ancienne, retirée: prends-en une nouvelle si besoin]"

PREAMBLE = """\
You are the assistant of a company's internal chat. People ask you for small
web apps and you build them with the Applace tools, which are documented below.

How to talk here: a sentence or two, in plain words, about what you are doing or
what you need. The person can see the app itself in a panel beside this
conversation -- its state, its errors and its links are drawn from the harness,
so you never have to paste code, file lists or URLs into the chat. When a build
comes back red, fix it and say so in one line. When you are done, say what the
app does and stop.
"""

# The chat says what is happening in the words of the person asking, not the
# words of the tool. This mapping is the whole of that translation.
VERBS: dict[str, Any] = {
    "get_skill": lambda args: "Reading the manual",
    "list_stacks": lambda args: "Looking at what it can build with",
    "create_app": lambda args: f"Creating “{args.get('name', 'the app')}”",
    "list_apps": lambda args: "Looking at what already exists",
    "get_app": lambda args: "Checking where the app stands",
    "read_files": lambda args: f"Reading {len(args.get('paths') or [])} file(s)",
    "write_files": lambda args: (
        f"Writing {len(args.get('files') or {})} file(s), and building"
    ),
    "set_env": lambda args: f"Asking for the key {args.get('name', '')}".strip(),
    "start_preview": lambda args: "Starting it up",
    "stop_preview": lambda args: "Shutting it down",
    "screenshot_app": lambda args: "Looking at the page",
    "deploy_app": lambda args: "Putting it online",
    "rollback_app": lambda args: "Putting the previous version back",
}


def price_of(model: str) -> tuple[float, float]:
    for prefix, price in PRICES:
        if model.startswith(prefix):
            return price
    return UNKNOWN_PRICE


class Budget:
    """What the conversation has spent, and the line it must not cross."""

    def __init__(self, limit: float, model: str) -> None:
        self.limit = limit
        self.price = price_of(model)
        self.prompt = 0
        self.completion = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if usage:
            self.prompt += int(usage.get("prompt_tokens") or 0)
            self.completion += int(usage.get("completion_tokens") or 0)

    @property
    def spent(self) -> float:
        return (
            self.prompt * self.price[0] + self.completion * self.price[1]
        ) / 1_000_000

    @property
    def over(self) -> bool:
        return self.spent >= self.limit


def read_key(name: str, env_file: Path | None) -> str:
    """The API key, from the environment or from an `.env` file. Never printed."""
    found = os.environ.get(name)
    if not found and env_file is not None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            head, sep, tail = line.partition("=")
            if sep and head.strip() == name:
                found = tail.strip().strip("\"'")
                break
    if not found:
        raise SystemExit(f"No {name} in the environment or the env file.")
    return found


async def tools(session: ClientSession) -> list[dict[str, Any]]:
    """The harness's tools, in the shape every chat API expects.

    No filtering and no rewriting: what the agent may do is the server's
    decision, and a chatbot that edited the list would be a second policy.
    """
    listed = await session.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in listed.tools
    ]


async def opening(session: ClientSession) -> str:
    """The system prompt: our two paragraphs, then SKILL.md as the server tells it."""
    result = await session.call_tool("get_skill", {})
    return PREAMBLE + "\n\n" + text_of(result)


def text_of(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


def picture_of(result: Any) -> str | None:
    """The screenshot in a tool result, as a data URL, or nothing.

    `screenshot_app` answers with a report *and* a PNG, and neither API accepts a
    picture inside a tool result -- so the image comes back in the next message
    instead. Dropping it is the easy mistake and an expensive one: a build that
    passes and a page that looks right are two different facts (D5), and a model
    that only reads the report will tell you a chart is fine when its bars are
    invisible.
    """
    for block in result.content:
        data = getattr(block, "data", None)
        if data and getattr(block, "mime_type", "").startswith("image/"):
            return f"data:{block.mime_type};base64,{data}"
    return None


@dataclass
class Reply:
    """One answer from the model, whichever API said it.

    The two shapes a tool-calling model speaks today are `/chat/completions`
    (everyone) and OpenAI's `/responses` (its newest models take function tools
    nowhere else). They disagree about every field name and agree about what
    they mean, so the rest of this file only ever sees this.
    """

    text: str
    calls: list[tuple[str, str, dict[str, Any]]]  # id, tool, arguments
    usage: dict[str, Any] | None


class Talk:
    """One conversation, and the app it is about."""

    def __init__(
        self,
        session: ClientSession,
        offered: list[dict[str, Any]],
        system: str,
        base: str,
        model: str,
        key: str,
        budget: Budget,
        api: str = "chat",
    ) -> None:
        self.session = session
        self.offered = offered
        self.base = base
        self.model = model
        self.key = key
        self.budget = budget
        self.api = api
        self.system = system
        self.said: list[dict[str, Any]] = (
            [{"role": "system", "content": system}] if api == "chat" else []
        )
        self.app: str | None = None

    # -- the two shapes ----------------------------------------------------

    def ask(self) -> Reply:
        """One call to the model. Blocking on purpose: it runs in a thread."""
        if self.api == "responses":
            return self._responses()
        return self._chat()

    def _chat(self) -> Reply:
        answered = self._post(
            "/chat/completions",
            {
                "model": self.model,
                "messages": self.said,
                "tools": self.offered,
                "tool_choice": "auto",
            },
        )
        message = answered["choices"][0]["message"]
        self.said.append(message)
        return Reply(
            text=message.get("content") or "",
            calls=[
                (
                    call["id"],
                    call["function"]["name"],
                    json.loads(call["function"]["arguments"] or "{}"),
                )
                for call in (message.get("tool_calls") or [])
            ],
            usage=answered.get("usage"),
        )

    def _responses(self) -> Reply:
        answered = self._post(
            "/responses",
            {
                "model": self.model,
                "instructions": self.system,
                "input": self.said,
                "tools": [
                    {"type": "function", **tool["function"]} for tool in self.offered
                ],
                "store": False,
            },
        )
        # Everything it produced goes back in next time, reasoning included:
        # with `store: false` the model has no other memory of its own thinking.
        self.said.extend(answered["output"])
        text = "\n".join(
            part.get("text", "")
            for item in answered["output"]
            if item.get("type") == "message"
            for part in item.get("content", [])
        )
        usage = answered.get("usage") or {}
        return Reply(
            text=text.strip(),
            calls=[
                (item["call_id"], item["name"], json.loads(item["arguments"] or "{}"))
                for item in answered["output"]
                if item.get("type") == "function_call"
            ],
            usage={
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
            },
        )

    def result(self, call_id: str, payload: str) -> None:
        """Hand a tool's answer back, in the shape this API expects."""
        if self.api == "responses":
            self.said.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": payload,
                }
            )
        else:
            self.said.append(
                {"role": "tool", "tool_call_id": call_id, "content": payload}
            )

    def forget_pictures(self) -> None:
        """Keep the last few captures and replace the older ones with a line.

        Every provider caps the images in one request -- Mistral refuses the
        ninth -- and a capture from six edits ago is not what the page looks
        like now, so an agent that keeps them all pays to be confused.
        """
        kept = 0
        for message in reversed(self.said):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            parts: list[Any] = content
            if not any(
                isinstance(part, dict) and part.get("type") in ("image_url", "input_image")
                for part in parts
            ):
                continue
            kept += 1
            if kept < EYES:
                continue
            kind = "input_text" if self.api == "responses" else "text"
            message["content"] = [{"type": kind, "text": OLD_PICTURE}]

    def see(self, picture: str) -> None:
        """Show the model the page it just looked at."""
        self.forget_pictures()
        said = "Voici la capture que tu viens de prendre. Regarde-la."
        if self.api == "responses":
            self.said.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": said},
                        {"type": "input_image", "image_url": picture},
                    ],
                }
            )
        else:
            self.said.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": said},
                        {"type": "image_url", "image_url": {"url": picture}},
                    ],
                }
            )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                return json.loads(answer.read())
        except urllib.error.HTTPError as error:  # the body says why, the status does not
            raise RuntimeError(f"{error.code}: {error.read().decode()[:400]}") from None


def frame(name: str, payload: Any) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


def slug_of(payload: str) -> str | None:
    """The app a tool result is about, so the panel can follow the conversation."""
    with contextlib.suppress(json.JSONDecodeError, AttributeError):
        found = json.loads(payload)
        if isinstance(found, dict) and isinstance(found.get("app"), str):
            return found["app"]
    return None


async def answer(talk: Talk, said: str) -> AsyncIterator[str]:
    """A turn: the model talks, calls tools, and the chat narrates both."""
    talk.said.append({"role": "user", "content": said})
    for _ in range(ROUNDS):
        if talk.budget.over:
            yield frame("error", {"text": "This demo has spent its budget."})
            return
        try:
            reply = await to_thread.run_sync(talk.ask)
        except (RuntimeError, OSError) as error:  # the model is somebody else's service
            yield frame("error", {"text": f"The model did not answer: {error}"})
            return
        talk.budget.add(reply.usage)
        if reply.text:
            yield frame("say", {"text": reply.text})
        if not reply.calls:
            yield frame("done", {"spent": round(talk.budget.spent, 4)})
            return
        for call_id, name, args in reply.calls:
            verb = VERBS.get(name)
            yield frame("doing", {"text": verb(args) if verb else name})
            result = await talk.session.call_tool(name, args)
            payload = text_of(result)
            slug = slug_of(payload)
            if slug and slug != talk.app:
                talk.app = slug
                yield frame("app", {"app": slug})
            talk.result(call_id, payload)
            picture = picture_of(result)
            if picture is not None:
                talk.see(picture)
    # Not an error the model can see: the cap exists precisely for the case
    # where it would keep going, so the turn ends and the person decides.
    yield frame("error", {"text": f"I stopped after {ROUNDS} steps. Ask me to go on."})


PAGE = """\
<!doctype html>
<meta charset="utf-8">
<title>Company chat</title>
<style>
  body {{ margin:0; font:15px/1.55 ui-sans-serif, system-ui, sans-serif; color:#0f172a;
         background:#f8fafc; display:grid; grid-template-columns: 1fr 1fr; height:100vh }}
  #talk {{ display:flex; flex-direction:column; border-right:1px solid #e2e8f0; background:#fff }}
  #log {{ flex:1; overflow:auto; padding:1.2rem }}
  .me, .bot {{ max-width:34rem; padding:.5rem .8rem; border-radius:12px; margin:.4rem 0; white-space:pre-wrap }}
  .me {{ background:#2563eb; color:#fff; margin-left:auto }}
  .bot {{ background:#f1f5f9 }}
  .doing {{ color:#64748b; font-size:13px; margin:.3rem 0 }}
  .doing::before {{ content:'· ' }}
  .bad {{ color:#991b1b; font-size:13px; margin:.3rem 0 }}
  form {{ display:flex; gap:.5rem; padding:.8rem; border-top:1px solid #e2e8f0 }}
  input {{ flex:1; padding:.6rem .8rem; border:1px solid #cbd5e1; border-radius:10px; font:inherit }}
  button {{ padding:.6rem 1rem; border:0; border-radius:10px; background:#2563eb; color:#fff; font:inherit }}
  button[disabled] {{ background:#94a3b8 }}
  #side {{ padding:1.2rem; overflow:auto }}
  #side p {{ color:#64748b }}
</style>
<div id="talk">
  <div id="log"><div class="bot">Ask me for an app. “A page showing which of our
    three services are up”, for instance.</div></div>
  <form id="form">
    <input id="said" autocomplete="off" placeholder="Ask for an app…">
    <button id="send">Send</button>
  </form>
</div>
<div id="side"><p>The app appears here as it is built.</p></div>

<!-- The whole of the app view: one script tag, one element (D16). -->
<script src="{applace}/panel/embed.js"></script>
<script>
  const log = document.getElementById('log');
  const side = document.getElementById('side');
  const send = document.getElementById('send');
  const add = (kind, text) => {{
    const node = document.createElement('div');
    node.className = kind; node.textContent = text;
    log.append(node); log.scrollTop = log.scrollHeight;
  }};

  document.getElementById('form').onsubmit = async (event) => {{
    event.preventDefault();
    const said = document.getElementById('said');
    if (!said.value.trim()) return;
    add('me', said.value);
    const body = JSON.stringify({{ said: said.value }});
    said.value = ''; send.disabled = true;

    // The answer arrives as it happens: the same server-sent events the panel
    // uses, read straight off the POST so a demo needs no socket and no queue.
    const answer = await fetch('/say', {{ method: 'POST', body }});
    const reader = answer.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {{
      const {{ value, done }} = await reader.read();
      if (done) break;
      buffer += value;
      const frames = buffer.split('\\n\\n'); buffer = frames.pop();
      for (const frame of frames) {{
        const name = frame.match(/^event: (\\w+)/)[1];
        const data = JSON.parse(frame.split('data: ')[1]);
        if (name === 'say') add('bot', data.text);
        else if (name === 'doing') add('doing', data.text);
        else if (name === 'error') add('bad', data.text);
        else if (name === 'app') show(data.app);
      }}
    }}
    send.disabled = false;
  }};

  function show(slug) {{
    if (side.querySelector(`applace-app[slug="${{slug}}"]`)) return;
    side.innerHTML = '';
    const card = document.createElement('applace-app');
    card.setAttribute('slug', slug);
    card.setAttribute('base', '{applace}');
    card.setAttribute('height', '520');
    side.append(card);
  }}
</script>
"""


def parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--applace", default="http://127.0.0.1:8848")
    parser.add_argument("--provider", default="mistral", choices=sorted(PROVIDERS))
    parser.add_argument("--model", default=None)
    parser.add_argument("--base", default=None, help="OpenAI-compatible base URL.")
    parser.add_argument(
        "--api", default=None, choices=["chat", "responses"],
        help="Which OpenAI shape to speak. Defaults to the provider's.",
    )
    parser.add_argument("--key-name", default=None, help="Environment variable to read.")
    parser.add_argument(
        "--rounds", type=int, default=ROUNDS, help="Tool rounds per message."
    )
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".env")
    parser.add_argument("--budget", type=float, default=2.0, help="Dollars, whole run.")
    parser.add_argument("--port", type=int, default=8900)
    return parser.parse_args()


def main() -> int:
    global ROUNDS
    args = parse()
    ROUNDS = args.rounds
    provider = PROVIDERS[args.provider]
    base = (args.base or provider["base"]).rstrip("/")
    model = args.model or provider["model"]
    api = args.api or provider.get("api", "chat")
    if not base or not model:
        raise SystemExit("A custom provider needs --base and --model.")
    key = read_key(args.key_name or provider["key"], args.env_file)
    applace = args.applace.rstrip("/")
    talk: dict[str, Talk] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """One MCP connection, for as long as the chat is up."""
        async with streamable_http_client(f"{applace}/mcp") as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                talk["it"] = Talk(
                    session,
                    await tools(session),
                    await opening(session),
                    base,
                    model,
                    key,
                    Budget(args.budget, model),
                    api,
                )
                print(f"Model  {model} ({api})")
                print(f"Chat   http://127.0.0.1:{args.port}")
                print(f"Panel  {applace}/panel")
                yield

    async def page(request: Request) -> Response:
        return HTMLResponse(PAGE.format(applace=applace))

    async def say(request: Request) -> Response:
        said = (await request.json()).get("said", "")
        return StreamingResponse(
            answer(talk["it"], said), media_type="text/event-stream"
        )

    app = Starlette(
        routes=[
            Route("/", page, methods=["GET"]),
            Route("/say", say, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
