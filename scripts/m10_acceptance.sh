#!/usr/bin/env bash
#
# M10 acceptance -- the app in a chat window:
#   one port answers both the agent and the chat UI;
#   a card is what a chat message needs, in one request, with no git in it;
#   the stream says what the build is doing while it is doing it, unasked;
#   the screenshot and the preview are both reachable from that card;
#   the embed is a script tag with no framework in it;
#   and the reference chatbot really does stand up against a real harness.
#
# Everything here is real: a real `applace serve --http`, a real MCP client over
# that port, real npm install, real vite build, real HTTP for the panel.
#
# Needs node, npm, git and a network.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
export APPLACE_ACCEPTANCE_REPO="$REPO"

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  uv run applace stop chat-demo >/dev/null 2>&1 || true
  pkill -f "examples/chat/chat.py" >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import asyncio
import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
REPO = Path(os.environ["APPLACE_ACCEPTANCE_REPO"])
HOME = Path(os.environ["APPLACE_HOME"])


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def run(*args, check=True):
    done = subprocess.run(args, capture_output=True, text=True)
    if check and done.returncode != 0:
        fail(f"`{' '.join(args)}` exited {done.returncode}:\n{done.stdout}{done.stderr}")
    return done


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url, want=200):
    try:
        with urllib.request.urlopen(url, timeout=30) as answer:
            return answer.status, answer.headers, answer.read()
    except urllib.error.HTTPError as error:
        if error.code != want:
            fail(f"{url} answered {error.code}, expected {want}")
        return error.code, error.headers, error.read()
    except urllib.error.URLError:  # nothing listening yet; the caller is waiting
        return 0, None, b""


def json_of(url):
    status, _, body = get(url)
    if status != 200:
        fail(f"{url} answered {status}")
    return json.loads(body)


def waiting(what, until, seconds=90):
    """Poll until a condition holds; acceptance is allowed to wait, not to hang."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = until()
        if found:
            return found
        time.sleep(0.5)
    fail(f"waited {seconds}s for {what} and it never happened")


# -- the harness, serving, as a company would run it -----------------------

step("applace init && applace serve --http")
init = run("uv", "run", "applace", "init")
if "vite-react-ts" not in init.stdout:
    fail("init did not report the built-in stack")

PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
serving = subprocess.Popen(
    ["uv", "run", "applace", "serve", "--http", str(PORT)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
# A failed step must not leave a port listening: the shell's trap cannot see
# processes this one started.
atexit.register(serving.terminate)
waiting("the panel to come up", lambda: get(f"{BASE}/panel")[0] == 200, 60)
print(f"   one port: {BASE}/mcp and {BASE}/panel")


# -- the agent's half, over that same port ---------------------------------

class Agent:
    """An MCP client over HTTP, driven from ordinary blocking code."""

    def __init__(self, url):
        self.url = url
        self.calls = asyncio.Queue()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.loop = None
        self.thread.start()
        self.ready.wait(30)

    def _serve(self):
        async def main():
            async with streamable_http_client(self.url) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self.ready.set()
                    await asyncio.Event().wait()

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(main())

    def call(self, tool, **kwargs):
        done = asyncio.run_coroutine_threadsafe(
            self.session.call_tool(tool, kwargs), self.loop
        )
        result = done.result(600)
        # A screenshot answers with a picture *and* a report; the report is the
        # block that parses.
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        fail(f"{tool} answered nothing a chat could read")


agent = Agent(f"{BASE}/mcp")

step("An app is created by the agent, and the chat can draw it from one request")
created = agent.call(
    "create_app", name="Chat Demo", description="What a chat window shows"
)
if not created.get("ok"):
    fail(f"create_app: {created}")
card = json_of(f"{BASE}/panel/apps/chat-demo")
for key in ("state", "headline", "urls", "errors", "commit", "name"):
    if key not in card:
        fail(f"the card has no {key}: {sorted(card)}")
if card["state"] != "green":
    fail(f"a newborn app should be green, not {card['state']}")
if "sha" in json.dumps(card).lower() or "diff" in json.dumps(card).lower():
    fail("the card talks about git; a chat message should not have to")
print(f"   {card['state']}: {card['headline']}")


# -- the stream, opened before anything happens ----------------------------

step("A chat window opens the stream and is told what happens, unasked")
frames = []


def listen():
    with urllib.request.urlopen(f"{BASE}/panel/apps/chat-demo/events") as stream:
        buffer = ""
        for chunk in stream:
            buffer += chunk.decode()
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                frames.append(frame)


threading.Thread(target=listen, daemon=True).start()
waiting("the stream to say what the app is now", lambda: frames)
if not frames[0].startswith("event: card"):
    fail(f"the stream opened with {frames[0]!r}")


def latest(state):
    for frame in reversed(frames):
        if frame.startswith("event: card"):
            card = json.loads(frame.split("data: ", 1)[1])
            if card.get("state") == state:
                return card
    return None


step("A red write is red in the chat, with the errors, while the agent still works")
BROKEN = """\
export default function App() {
  const count: number = "three";
  return <main className="p-8 text-xl">{count} services</main>;
}
"""
written = agent.call("write_files", app="chat-demo", files={"src/App.tsx": BROKEN})
if written.get("ok"):
    fail("a type error passed the gate")
red = waiting("the stream to go red", lambda: latest("red"))
if red["failed_stage"] != "typecheck":
    fail(f"the card blames {red['failed_stage']}")
if not red["errors"] or red["errors"][0]["file"] != "src/App.tsx":
    fail(f"the errors are not readable: {red['errors']}")
print(f"   {red['headline']}")

step("The fix goes green in the same stream, with no request from the chat")
GOOD = """\
const SERVICES = [
  { name: "Checkout", up: true },
  { name: "Billing", up: false },
  { name: "Search", up: true },
];

export default function App() {
  const up = SERVICES.filter((service) => service.up).length;
  return (
    <main className="p-8 font-sans">
      <h1 className="text-xl font-semibold">{up} of 3 services are up</h1>
      <ul className="mt-4 space-y-2">
        {SERVICES.map((service) => (
          <li key={service.name} className="flex items-center gap-2">
            <span className={service.up ? "text-green-600" : "text-amber-600"}>●</span>
            {service.name}
          </li>
        ))}
      </ul>
    </main>
  );
}
"""
fixed = agent.call("write_files", app="chat-demo", files={"src/App.tsx": GOOD})
if not fixed.get("ok"):
    fail(f"the fix did not pass the gate: {fixed.get('errors')}")
def moved_on():
    seen = latest("green")
    return seen if seen and seen["commit"] != card["commit"] else None


green = waiting("the stream to go green again", moved_on)
if green["errors"]:
    fail("a green card still carries errors")
print(f"   {green['headline']}")


# -- the things a person clicks --------------------------------------------

step("The preview is a URL in the card, which is what an iframe needs")
preview = agent.call("start_preview", app="chat-demo")
if not preview.get("ok"):
    fail(f"start_preview: {preview}")
running = waiting(
    "the preview URL to reach the card",
    lambda: json_of(f"{BASE}/panel/apps/chat-demo")["urls"].get("preview"),
)
if get(running)[0] != 200:
    fail(f"the preview URL in the card does not answer: {running}")
print(f"   preview: {running}")

step("The screenshot the agent took is served to the chat")
shot = agent.call("screenshot_app", app="chat-demo", route="/")
if shot.get("ok"):
    status, headers, body = get(f"{BASE}/panel/apps/chat-demo/shot.png")
    if status != 200 or headers["content-type"] != "image/png" or len(body) < 1000:
        fail("the screenshot is not being served as a PNG")
    seen = json_of(f"{BASE}/panel/apps/chat-demo")
    if seen["shot"] != "/panel/apps/chat-demo/shot.png" or not seen["shot_at"]:
        fail(f"the card does not point at the screenshot: {seen['shot']}")
    print(f"   {len(body)} bytes of PNG, taken at {seen['shot_at']}")
else:
    # The eyes are an extra (D5); a machine without a browser still has a panel.
    print(f"   no browser on this machine, skipped: {shot.get('error')}")

step("A deployment is a link in the same card")
deployed = agent.call("deploy_app", app="chat-demo", target="local")
if not deployed.get("ok"):
    fail(f"deploy_app: {deployed}")
live = waiting(
    "the deployment to reach the card",
    lambda: json_of(f"{BASE}/panel/apps/chat-demo")["urls"].get("live"),
)
if get(live)[0] != 200:
    fail(f"the live URL in the card does not answer: {live}")
print(f"   live: {live}")


# -- what an integrator copies ---------------------------------------------

step("The embed is one script tag: no bundler, no framework, no import")
status, headers, body = get(f"{BASE}/panel/embed.js")
embed = body.decode()
if "javascript" not in headers["content-type"]:
    fail(f"embed.js is served as {headers['content-type']}")
if "customElements.define('applace-app'" not in embed:
    fail("embed.js does not define the element")
if "import " in embed or "react" in embed.lower():
    fail("embed.js pulls something in")
if headers["access-control-allow-origin"] != "*":
    fail("a chat UI on another origin cannot read the panel")
print(f"   {len(embed)} bytes, and one custom element")

step("The page a human opens lists what this machine has built")
status, _, body = get(f"{BASE}/panel")
if "chat-demo" not in json.dumps(json_of(f"{BASE}/panel/apps")):
    fail("the list does not have the app in it")
if b"panel/embed.js" not in body:
    fail("the panel page does not use the same element it tells others to use")


# -- the reference chatbot, against this very harness ----------------------

step("examples/chat/chat.py stands up against it and speaks to the same port")
CHAT_PORT = free_port()
chatting = subprocess.Popen(
    ["uv", "run", "examples/chat/chat.py",
     "--applace", BASE, "--port", str(CHAT_PORT),
     # An unreachable model: this checks the harness half, and costs nothing.
     "--provider", "custom", "--base", "http://127.0.0.1:1/v1",
     "--model", "none", "--key-name", "APPLACE_ACCEPTANCE_KEY"],
    cwd=REPO, env={**os.environ, "APPLACE_ACCEPTANCE_KEY": "not-a-key"},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
atexit.register(chatting.terminate)
chat = f"http://127.0.0.1:{CHAT_PORT}"
waiting("the chat to come up", lambda: get(f"{chat}/")[0] == 200, 60)
page = get(f"{chat}/")[2].decode()
if f"{BASE}/panel/embed.js" not in page or "applace-app" not in page:
    fail("the chat page does not embed the panel")
asked = urllib.request.Request(f"{chat}/say", data=b'{"said": "hello"}')
with urllib.request.urlopen(asked, timeout=60) as answer:
    said = answer.read().decode()
if "event: error" not in said or "did not answer" not in said:
    fail(f"the chat did not survive a model that is not there: {said!r}")
print("   it connected to the harness, read the skill, and served the page")
chatting.terminate()

step("And the whole thing is still one process")
if serving.poll() is not None:
    fail("the server died somewhere in there")
serving.terminate()

print(f"\n{GREEN}{BOLD}M10 ACCEPTANCE: PASS{OFF}")
PY
