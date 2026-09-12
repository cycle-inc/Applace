#!/usr/bin/env python
"""Drive a hosted model through the Applace tools, blind, and price the run.

The local-model run (`scripts/m6_local_model.py`) asks whether SKILL.md is
enough for a model with no prior knowledge of Applace. This asks the same thing
of a hosted model that is actually good at tool calling, and takes it one step
further: the task ends in a *deployment*, so the run exercises the whole harness
-- create, write, look, ship -- rather than only the compiler.

Blind means the model is given SKILL.md and the tool schemas, nothing else. No
hints about this repository, no example of a correct call, no retry advice.

Two things bound the cost, because a model in a loop with a build tool can spend
real money: a hard round cap, and a budget in dollars that ends the run the
moment the usage the API reports crosses it. Both are printed at the end
whether the run passed or not.

    uv run scripts/blind_model.py --model mistral-large-latest --budget 1.00

The key is read from the environment, or from an `.env` file named with
`--env-file`. It is never printed and never reaches the app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

# base URL, key variable, and dollars per million tokens (prompt, completion).
PROVIDERS: dict[str, dict[str, Any]] = {
    "mistral": {
        "base": "https://api.mistral.ai/v1",
        "key": "MISTRAL_API_KEY",
        "model": "mistral-large-latest",
        "price": (2.0, 6.0),
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "key": "OPENAI_API_KEY",
        "model": "gpt-4.1-mini",
        "price": (0.4, 1.6),
    },
}

OFFERED = [
    "get_skill",
    "list_stacks",
    "create_app",
    "get_app",
    "read_files",
    "write_files",
    "set_env",
    "screenshot_app",
    "deploy_app",
]

TASK = """\
Build a web app called "Deploy Demo" that shows the status of three internal
services on one page: Checkout is up, Billing is degraded, Search is up. Each
service is a row with its name and a coloured dot, and the page has a heading
saying how many of the three are up.

There is no API: the three services are written in the code. Use the
vite-react-ts stack. When the page renders, deploy it to this machine and tell
me the URL, then stop.\
"""

MAX_ROUNDS = 16
REQUEST_TIMEOUT = 300
WALL_CLOCK = 1800  # seconds; a build is slow, a runaway loop is slower
REPEAT_LIMIT = 3   # the same call, with the same arguments, this many times


class Budget:
    """What the run has spent, and the line it must not cross."""

    def __init__(self, limit: float, price: tuple[float, float]) -> None:
        self.limit = limit
        self.price = price
        self.prompt = 0
        self.completion = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)

    @property
    def spent(self) -> float:
        return (
            self.prompt * self.price[0] + self.completion * self.price[1]
        ) / 1_000_000

    @property
    def blown(self) -> bool:
        return self.spent >= self.limit

    def line(self) -> str:
        return (
            f"{self.prompt + self.completion} tokens "
            f"({self.prompt} in, {self.completion} out) ≈ ${self.spent:.3f} "
            f"of a ${self.limit:.2f} budget"
        )


def read_key(name: str, env_file: Path | None) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if env_file is not None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, raw = line.partition("=")
            if key.strip() == name:
                return raw.strip().strip('"').strip("'")
    sys.exit(f"no {name} in the environment or in {env_file}")


def call_tool(
    server: MCPServer, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments or {}))
    if result.structured_content is not None:
        return result.structured_content
    for content in result.content:
        if isinstance(content, TextContent):
            return json.loads(content.text)
    return {"ok": False, "error": "the tool returned nothing readable"}


def brief(result: dict[str, Any]) -> str:
    if "stage" in result:
        head = f"{'green' if result.get('ok') else 'red'} at {result['stage']}"
        if result.get("commit"):
            head += f", committed {result['commit'][:12]}"
        if result.get("errors"):
            head += f" -- {str(result['errors'][0].get('message'))[:120]}"
        return head
    if "blank" in result:
        return (
            f"blank={result['blank']} errors={len(result.get('errors', []))} "
            f"title={result.get('title')!r}"
        )
    if result.get("deployment"):
        return f"{result.get('status')} {result.get('url')}"
    if not result.get("ok", True):
        return f"not ok: {result.get('code') or ''} {result.get('error') or ''}".strip()
    return "ok"


def schemas(server: MCPServer) -> list[dict[str, Any]]:
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    missing = [name for name in OFFERED if name not in tools]
    if missing:
        sys.exit(f"the server no longer has {missing}")
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": (tools[name].description or "").strip(),
                "parameters": tools[name].input_schema,
            },
        }
        for name in OFFERED
    ]


def chat(
    base: str, key: str, model: str, messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:400]
        sys.exit(f"the provider answered {exc.code}: {body}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="mistral", choices=sorted(PROVIDERS))
    parser.add_argument("--model", default=None)
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--budget", type=float, default=1.00,
                        help="dollars; the run stops when the reported usage crosses it")
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS)
    parser.add_argument("--env-file", type=Path,
                        default=Path.home() / "vega-api" / ".env")
    args = parser.parse_args()

    provider = PROVIDERS[args.provider]
    model = args.model or provider["model"]
    key = read_key(str(provider["key"]), args.env_file)
    budget = Budget(args.budget, provider["price"])

    paths = ApplacePaths(applace_home())
    server = build_server(paths)
    tools = schemas(server)
    skill = call_tool(server, "get_skill")["skill"]

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{skill}\n\n"
                "You are working on this machine through these tools. Call them "
                "one at a time and read what comes back. Do not write code in "
                "your replies: code only reaches the app through write_files."
            ),
        },
        {"role": "user", "content": args.task},
    ]

    print(f"  provider  {args.provider}")
    print(f"  model     {model}")
    print(f"  tools     {', '.join(OFFERED)}")
    print(f"  caps      {args.rounds} rounds, ${args.budget:.2f}, {WALL_CLOCK}s\n")

    created: list[str] = []
    writes: list[dict[str, Any]] = []
    deploys: list[dict[str, Any]] = []
    last_call: tuple[str, str] | None = None
    repeats = 0
    stopped = ""
    started = time.monotonic()
    rounds = 0

    for rounds in range(1, args.rounds + 1):
        if budget.blown:
            stopped = f"the budget ran out: {budget.line()}"
            break
        if time.monotonic() - started > WALL_CLOCK:
            stopped = f"the run passed {WALL_CLOCK}s"
            break

        answer = chat(provider["base"], key, model, messages, tools)
        budget.add(answer.get("usage"))
        message = answer["choices"][0]["message"]
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
        })
        calls = message.get("tool_calls") or []

        if not calls:
            said = (message.get("content") or "").strip().replace("\n", " ")
            print(f"  [{rounds:>2}] said: {said[:200]}")
            stopped = "the model said it was done"
            break

        for call in calls:
            name = call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError as exc:
                arguments = {}
                result: dict[str, Any] = {
                    "ok": False, "error": f"your arguments were not JSON: {exc}"
                }
            else:
                result = (
                    call_tool(server, name, arguments)
                    if name in OFFERED
                    else {"ok": False, "error": f"there is no tool called {name}"}
                )

            signature = (name, json.dumps(arguments, sort_keys=True)[:2000])
            repeats = repeats + 1 if signature == last_call else 0
            last_call = signature

            shown = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in arguments.items())
            print(f"  [{rounds:>2}] {name}({shown[:110]}) -> {brief(result)}")

            if name == "create_app" and result.get("ok"):
                created.append(result["app"])
            if name == "write_files":
                writes.append(result)
            if name == "deploy_app":
                deploys.append(result)

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", name),
                "name": name,
                "content": json.dumps(result)[:6000],
            })

        if repeats + 1 >= REPEAT_LIMIT:
            stopped = f"the model repeated the same call {REPEAT_LIMIT} times"
            break
    else:
        stopped = f"it used all {args.rounds} rounds"

    elapsed = int(time.monotonic() - started)
    print(f"\n  {rounds} rounds in {elapsed}s -- {stopped}")
    print(f"  cost      {budget.line()}")

    if not created:
        print("  VERDICT: no app was created")
        return 1
    app = created[0]
    greens = [w for w in writes if w.get("ok")]
    print(f"  writes    {len(writes)} ({len(greens)} green)")
    if not greens:
        print(f"  VERDICT: nothing green was ever written to {app}")
        return 1

    live = [d for d in deploys if d.get("ok")]
    if not live:
        print(f"  VERDICT: {app} was built but never deployed")
        return 1
    url = live[-1]["url"]

    report = call_tool(server, "screenshot_app", {"app": app})
    print(f"  page      title={report.get('title')!r} blank={report.get('blank')} "
          f"errors={len(report.get('errors', []))}")
    for error in report.get("errors", []):
        print(f"    console: {error['text'][:160]}")
    if report.get("blank") or report.get("errors"):
        print("  VERDICT: the page does not render")
        return 1

    import urllib.request as fetch
    with fetch.urlopen(url, timeout=10) as response:
        served = response.read().decode("utf-8", errors="ignore")
    if "<div id=\"root\">" not in served:
        print(f"  VERDICT: {url} is not serving a built page")
        return 1
    print(f"  deployed  {url} ({len(served)} bytes of built index.html)")

    source = call_tool(server, "read_files", {"app": app, "paths": ["src/App.tsx"]})
    print("\n  --- src/App.tsx as the model left it ---")
    for line in source["files"][0].get("content", "").splitlines():
        print(f"  {line}")

    print(f"\n  VERDICT: {model} built and shipped {app} in {rounds} rounds "
          f"for ${budget.spent:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
