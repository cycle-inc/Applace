#!/usr/bin/env python
"""Drive a small local model through the Applace tools, with SKILL.md as its brief.

The M6 question is not "can a frontier model use this harness" -- it can use
anything. It is whether the *document* and the *tool results* are enough on
their own, which is what a mid-sized local model tests: it has no prior
knowledge of Applace, a short attention span, and no patience for a red build.

Talks to ollama over HTTP. The model gets the same tools an MCP host would
expose, minus the screenshot's picture: qwen3 has no eyes, so it gets the JSON
report instead, which is the half of a screenshot that a model acts on anyway.

Exit code 0 means the run met the acceptance: one app, its first write green,
and a page that renders.
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
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("APPLACE_ACCEPTANCE_MODEL", "qwen3:8b")

# No start_preview: the run is headless and screenshot_app starts one anyway.
OFFERED = [
    "get_skill",
    "list_stacks",
    "create_app",
    "get_app",
    "read_files",
    "write_files",
    "set_env",
    "screenshot_app",
]

# Deliberately not the shape of the example in SKILL.md: the question is
# whether the document taught the model how this harness works, not whether it
# can copy a snippet.
TASK = """\
Build a web app called "Release Notes" that lists our last three releases,
newest first. Release 2.4.0 shipped on 2026-08-30 and added single sign-on;
2.3.1 shipped on 2026-08-12 and fixed a billing rounding bug; 2.3.0 shipped on
2026-07-29 and added the audit log. Each release is a card showing its version,
its date and its one-line summary, and the page has a heading.

There is no API: the three releases are written in the code. Use the
vite-react-ts stack. When the page renders, say you are done and stop.\
"""

MAX_ROUNDS = 14
ROUND_TIMEOUT = 900


def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{OLLAMA}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=ROUND_TIMEOUT) as response:
        return json.loads(response.read())


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


def coerce(arguments: Any) -> dict[str, Any]:
    """Undo the two things small models do to tool arguments.

    Both are transport mistakes, not reasoning mistakes: a JSON object sent as
    a string, and a file's content sent as a list of lines. Fixing them here
    keeps the run about whether the model understood the skill.
    """
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        return {}
    fixed: dict[str, Any] = dict(arguments)
    files = fixed.get("files")
    if isinstance(files, str):
        files = json.loads(files)
    if isinstance(files, dict):
        fixed["files"] = {
            path: "\n".join(body) if isinstance(body, list) else body
            for path, body in files.items()
        }
    return fixed


def brief(result: dict[str, Any]) -> str:
    """One line of the tool result, for the transcript a human reads."""
    if "code" in result or "error" in result:
        return f"not ok: {result.get('code') or ''} {result.get('error') or ''}".strip()
    if "stage" in result:
        head = f"{'green' if result['ok'] else 'red'} at {result['stage']}"
        if result.get("commit"):
            head += f", committed {result['commit'][:12]}"
        if result.get("errors"):
            head += f" -- {result['errors'][0]['message'][:120]}"
        return head
    if "blank" in result:
        return (
            f"blank={result['blank']} errors={len(result['errors'])} "
            f"title={result.get('title')!r}"
        )
    return "ok"


def tool_schemas(server: MCPServer) -> list[dict[str, Any]]:
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


def chat(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "think": False,
        "options": {"num_ctx": 16384, "temperature": 0.2},
    }
    try:
        return post("/api/chat", payload)["message"]
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        payload.pop("think")  # an older ollama, or a model with no think switch
        return post("/api/chat", payload)["message"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--task", default=TASK)
    args = parser.parse_args()

    paths = ApplacePaths(applace_home())
    server = build_server(paths)
    tools = tool_schemas(server)

    system = call_tool(server, "get_skill")["skill"]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{system}\n\n"
                "You are working on this machine through these tools. Call them "
                "one at a time and read what comes back. Do not write code in "
                "your replies: code only reaches the app through write_files."
            ),
        },
        {"role": "user", "content": args.task},
    ]

    print(f"  model     {args.model}")
    print(f"  tools     {', '.join(OFFERED)}")
    print(f"  skill     {len(system)} characters of SKILL.md as the system prompt\n")

    created: list[str] = []
    writes: list[dict[str, Any]] = []
    started = time.monotonic()

    for round_number in range(1, MAX_ROUNDS + 1):
        message = chat(args.model, messages, tools)
        messages.append(message)
        calls = message.get("tool_calls") or []

        if not calls:
            said = (message.get("content") or "").strip().replace("\n", " ")
            print(f"  [{round_number:>2}] said: {said[:160]}")
            break

        for call in calls:
            name = call["function"]["name"]
            try:
                arguments = coerce(call["function"].get("arguments"))
            except json.JSONDecodeError as exc:
                result = {"ok": False, "error": f"your arguments were not JSON: {exc}"}
            else:
                if name not in OFFERED:
                    result = {"ok": False, "error": f"there is no tool called {name}"}
                else:
                    result = call_tool(server, name, arguments)

            shown = ", ".join(
                f"{key}={str(value)[:40]!r}" for key, value in arguments.items()
            ) if isinstance(arguments, dict) else ""
            print(f"  [{round_number:>2}] {name}({shown[:100]}) -> {brief(result)}")

            if name == "create_app" and result.get("ok"):
                created.append(result["app"])
            if name == "write_files":
                writes.append(result)

            messages.append(
                {"role": "tool", "tool_name": name, "content": json.dumps(result)[:6000]}
            )
    else:
        print(f"  gave up after {MAX_ROUNDS} rounds")

    elapsed = int(time.monotonic() - started)
    print(f"\n  {round_number} rounds in {elapsed}s")

    if not created:
        print("  VERDICT: no app was created")
        return 1
    app = created[0]

    first = next((w for w in writes if w.get("app") == app), None)
    if first is None:
        print(f"  VERDICT: {app} was created and never written to")
        return 1
    if not first.get("ok"):
        print(f"  VERDICT: the first write to {app} was red at {first['stage']}")
        return 1
    print(f"  first write to {app}: green at {first['stage']}")

    report = call_tool(server, "screenshot_app", {"app": app})
    if not report.get("ok"):
        print(f"  VERDICT: could not look at the page: {report}")
        return 1
    print(f"  page: title={report['title']!r} blank={report['blank']} "
          f"errors={len(report['errors'])} failed_requests={len(report['failed_requests'])}")
    for error in report["errors"]:
        print(f"    console: {error['text'][:160]}")
    if report["blank"] or report["errors"]:
        print("  VERDICT: the page does not render")
        return 1

    source = call_tool(server, "read_files", {"app": app, "paths": ["src/App.tsx"]})
    print("\n  --- src/App.tsx as the model left it ---")
    for line in source["files"][0].get("content", "").splitlines():
        print(f"  {line}")

    print(f"\n  VERDICT: {args.model} built {app} on its first attempt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
