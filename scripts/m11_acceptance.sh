#!/usr/bin/env bash
#
# M11 acceptance -- Applace as a package:
#   one Python process, one temporary home, no terminal and no agent;
#   a red write is an answer with a stage and diagnostics, and commits nothing;
#   the green one commits;
#   card() is byte-for-byte what the panel serves over HTTP;
#   a secret value goes in through set_env and no method anywhere gives it back;
#   and the same question asked through the MCP server and through the class
#   gives the same answer.
#
# Everything here is real: real npm install, real vite build, real git, real
# HTTP against the panel's own routes.
#
# Needs node, npm, git and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from applace import Applace, panel
from applace.init_cmd import run_init
from applace.paths import ApplacePaths
from applace.server import build_server

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
HOME = Path(os.environ["APPLACE_HOME"])
SECRET = "sk-acceptance-not-a-real-token"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def tool(server, name, **kwargs):
    """Ask the MCP server the same question, the way a host would."""
    result = asyncio.run(server.call_tool(name, kwargs))
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    fail(f"{name} answered nothing readable")


# -- a home, made by the package itself ------------------------------------

step("A backend imports Applace and has a working home")
report = run_init(ApplacePaths(home=HOME))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")

ap = Applace(HOME)
if ap.apps() != {"ok": True, "apps": []}:
    fail(f"a fresh home should list nothing: {ap.apps()}")
if "vite-react-ts" not in [stack["name"] for stack in ap.stacks()["stacks"]]:
    fail("the built-in stack is not there")
print(f"   {ap!r}")


# -- an app, with no agent and no terminal ---------------------------------

step("The class creates an app: installed, built, committed")
created = ap.create("Service Board", description="What M11 can do on its own")
if not created.get("ok"):
    fail(f"create: {created}")
slug = created["app"]
if slug != "service-board" or len(created["commit"]) != 40:
    fail(f"create answered {created}")
print(f"   {slug} at {created['commit'][:12]}, entry {created['entry']}")


step("A red write is an answer, not an exception, and commits nothing")
BROKEN = """\
export default function App() {
  const open: number = "three";
  return <main className="p-8">{open} incidents</main>;
}
"""
red = ap.write(slug, {"src/App.tsx": BROKEN}, message="Break it")
if red.get("ok"):
    fail("a type error passed the gate")
if red.get("stage") != "typecheck":
    fail(f"the failing stage is {red.get('stage')}")
if not red.get("errors") or red["errors"][0]["file"] != "src/App.tsx":
    fail(f"no readable diagnostics: {red.get('errors')}")
if ap.app(slug)["commit"] != created["commit"]:
    fail("a red write committed something (D4)")
if not (HOME / "apps" / slug / "src" / "App.tsx").exists():
    fail("the red write is not on disk; the agent cannot fix what it cannot read")
first = red["errors"][0]
print(f"   {red['stage']}: {first['file']}:{first['line']} {first['message'][:60]}")


step("The green one commits")
GOOD = """\
const INCIDENTS = [
  { service: "Checkout", open: true },
  { service: "Billing", open: false },
  { service: "Search", open: true },
];

export default function App() {
  const open = INCIDENTS.filter((incident) => incident.open).length;
  return (
    <main className="p-8 font-sans">
      <h1 className="text-xl font-semibold">{open} open incidents</h1>
      <ul className="mt-4 space-y-2">
        {INCIDENTS.map((incident) => (
          <li key={incident.service}>{incident.service}</li>
        ))}
      </ul>
    </main>
  );
}
"""
green = ap.write(slug, {"src/App.tsx": GOOD}, message="Show the open incidents")
if not green.get("ok"):
    fail(f"the fix did not pass the gate: {green.get('errors')}")
if green["commit"] == created["commit"] or not green["committed"]:
    fail(f"a green write did not commit: {green}")
if ap.app(slug)["dirty"]:
    fail("the app is still dirty after a green write")
read = ap.read(slug, ["src/App.tsx"])["files"][0]
if "open incidents" not in read["content"]:
    fail("read_files does not see what write_files wrote")
print(f"   {green['commit'][:12]}: {', '.join(green['written'])}")


# -- the card, without a socket --------------------------------------------

step("card() is byte-for-byte what the panel serves over HTTP (D16)")
client = TestClient(Starlette(routes=panel.routes(ApplacePaths(home=HOME))))
over_http = client.get(f"/panel/apps/{slug}").json()
in_process = ap.card(slug)
if in_process != over_http:
    fail(f"the two cards differ:\n  class: {in_process}\n  http:  {over_http}")
if ap.cards()["apps"] != client.get("/panel/apps").json()["apps"]:
    fail("the list of cards differs between the two doors")
if in_process["state"] != "green":
    fail(f"the card says {in_process['state']} about an app that just built")
print(f"   {in_process['state']}: {in_process['headline']}")


# -- the developer's half (D19) --------------------------------------------

step("A secret value goes in, and no method anywhere gives it back (D8)")
ap.declare_env(slug, "API_TOKEN", "The token the backend calls the API with")
stored = ap.set_env(slug, "API_TOKEN", SECRET)
if not stored.get("ok"):
    fail(f"set_env: {stored}")
env_file = ApplacePaths(home=HOME).env_file(slug)
if SECRET not in env_file.read_text(encoding="utf-8"):
    fail(f"the value never reached {env_file}")
mode = oct(env_file.stat().st_mode)[-3:]
if mode != "600":
    fail(f"{env_file} is {mode}, not 600")

everywhere = json.dumps(
    [
        stored,
        ap.env(slug),
        ap.app(slug),
        ap.card(slug),
        ap.cards(),
        ap.skill(),
        ap.apps(),
        ap.deployments(slug),
    ],
    default=str,
)
if SECRET in everywhere:
    fail("a method handed the value back")
listed = ap.env(slug)["env"]
if [v["name"] for v in listed] != ["API_TOKEN"] or not listed[0]["set"]:
    fail(f"env does not report the name as set: {listed}")
print(f"   {env_file} is {mode}, and 8 methods later the value is still only there")


step("The developer's half is not reachable from the agent's door (D19)")
server = build_server(ApplacePaths(home=HOME))
tools = {t.name: t for t in asyncio.run(server.list_tools())}
forbidden = {
    "unset_env", "connect_github", "adopt", "push", "connect_vercel",
    "install_stack", "update_stack", "remove",
}
if tools.keys() & forbidden:
    fail(f"an agent can call {sorted(tools.keys() & forbidden)}")
if "value" in tools["set_env"].input_schema["properties"]:
    fail("the set_env tool takes a value")
print(f"   {len(tools)} tools, and none of them authorises anything")


# -- the two doors ---------------------------------------------------------

step("The same question through the MCP server and through the class")
for name, asked, method in (
    ("get_app", {"app": slug}, lambda: ap.app(slug)),
    ("list_apps", {}, ap.apps),
    ("list_stacks", {}, ap.stacks),
    ("get_skill", {}, ap.skill),
    (
        "read_files",
        {"app": slug, "paths": ["src/App.tsx"]},
        lambda: ap.read(slug, ["src/App.tsx"]),
    ),
):
    through_mcp = tool(server, name, **asked)
    through_class = method()
    if through_mcp != through_class:
        fail(
            f"{name} and the class disagree:\n"
            f"  mcp:   {json.dumps(through_mcp, default=str)[:400]}\n"
            f"  class: {json.dumps(through_class, default=str)[:400]}"
        )
print("   get_app, list_apps, list_stacks, get_skill, read_files: identical")


step("An unknown app is a code, not a traceback, on every read")
for answer in (
    ap.app("nope"), ap.read("nope", ["src/App.tsx"]), ap.card("nope"),
    ap.env("nope"), ap.deployments("nope"), ap.write("nope", {"a.txt": "x"}),
):
    if answer.get("ok") is not False or answer.get("code") != "unknown-app":
        fail(f"a handler would have to catch this: {answer}")
print("   unknown-app, six times, with a sentence a UI can show")


# -- what a product ships --------------------------------------------------

step("The package deploys the commit it built")
deployed = ap.deploy(slug, target="local")
if not deployed.get("ok") or deployed["status"] != "live":
    fail(f"deploy: {deployed}")
url = deployed["url"]
if deployed["commit"] != green["commit"]:
    fail("what went live is not the commit that passed the gate")
with urllib.request.urlopen(url, timeout=30) as answer:
    page = answer.read().decode()
if "<div id=\"root\">" not in page and "<script" not in page:
    fail(f"{url} did not serve the built app")
if SECRET in page:
    fail("a name without the stack's prefix still reached the browser")
print(f"   live at {url}, serving {len(page)} bytes")

step("And production still needs a human, even from Python (D6)")
asked = ap.deploy(slug, target="local", environment="production")
if asked.get("ok") or asked.get("code") not in ("confirm-required", "policy"):
    fail(f"a backend deployed to production on its own: {asked}")
print(f"   {asked['code']}: {asked['error']}")

step("Cleaning up what this run started")
ap.remove(slug)
if ap.apps()["apps"]:
    fail("the app is still listed")
if not (HOME / "apps" / slug / ".git").is_dir():
    fail("removing an app deleted somebody's code")

print(f"\n{GREEN}{BOLD}M11 ACCEPTANCE: PASS{OFF}")
PY
