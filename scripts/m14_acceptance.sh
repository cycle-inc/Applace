#!/usr/bin/env bash
#
# M14 acceptance -- the sensor (D25):
#   a real app on the built-in stack, built by vite and opened in a real
#   Chromium at the end of every write;
#   a component that renders nothing is a red gate that commits nothing, even
#   though typecheck, lint and build were all green;
#   a page that throws comes back with the browser's own words, and the line of
#   src/App.tsx -- not of a minified chunk -- because the gate builds with a
#   sourcemap and a deploy does not;
#   a declared route is opened, and what it was declared to show is checked;
#   an app with a declared API reaches it during the visit, with its token, so
#   the check sees the page a person would see (D24 + D25);
#   and turning the check off says so in the report rather than looking green.
#
# Everything here is real: real npm install, real vite build, real browser,
# real HTTP, real git. No model is called, so this costs nothing but time.
#
# Needs node, npm, git, a network, and `playwright install chromium`.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from applace import Applace, eyes
from applace.init_cmd import run_init
from applace.paths import ApplacePaths

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
HOME = Path(os.environ["APPLACE_HOME"])
TOKEN = "sk-acceptance-not-a-real-token"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def messages(report):
    return " | ".join(error["message"] for error in report.get("errors", []))


# -- the company API the page will call ------------------------------------

SEEN = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        SEEN.append({"path": self.path, "headers": dict(self.headers.items())})
        body = json.dumps(
            {"customers": [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Globex"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
threading.Thread(target=server.serve_forever, daemon=True).start()
UPSTREAM = "http://{}:{}".format(*server.server_address[:2])


# -- a home, a browser, and an app -----------------------------------------

step("A home, a browser, and an app on the built-in stack")
report = run_init(ApplacePaths(home=HOME))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")
if not eyes.available():
    fail("no chromium: run `uv sync --extra eyes` then `playwright install chromium`")

ap = Applace(HOME)
created = ap.create("Customer Board", description="What M14 looks at")
if not created.get("ok"):
    fail(f"create: {created}")
slug = created["app"]
root = HOME / "apps" / slug
print(f"   {slug} at {created['commit'][:12]}, and a browser to open it with")

state = ap.app(slug)["visit"]
if state["on"] is not True or [r["path"] for r in state["routes"]] != ["/"]:
    fail(f"an app that declared nothing should still have / opened: {state}")
print("   nothing declared, so the gate opens /")


# -- the failure every other stage calls a success -------------------------

step("Four green stages and a white screen is a red gate (D25)")
WHITE = """\
export default function App() {
  return null
}
"""
before = ap.app(slug)["commit"]
red = ap.write(slug, {"src/App.tsx": WHITE}, message="Render nothing at all")
if red.get("ok"):
    fail("a page that renders nothing passed the gate")
if red.get("stage") != "visit":
    fail(f"the failing stage is {red.get('stage')}, not visit")
stages = {stage["stage"]: stage for stage in red["stages"]}
if not all(stages[name]["ok"] for name in ("typecheck", "lint", "build")):
    fail(f"the stages before the visit were not all green: {red['stages']}")
if "the page is empty" not in messages(red):
    fail(f"the report does not say the page was empty: {messages(red)}")
if ap.app(slug)["commit"] != before:
    fail("a red write committed something (D4)")
if not (root / "src" / "App.tsx").read_text(encoding="utf-8").startswith("export"):
    fail("the red write is not on disk; the agent cannot fix what it cannot read")
print(f"   typecheck, lint and build green, and: {messages(red)[:90]}")


# -- the browser's own words, in the app's own file ------------------------

step("A page that throws comes back with the browser's words and a source line")
THROWS = """\
type Customer = { id: string; name: string }

export default function App() {
  const customers = null as unknown as Customer[]
  return (
    <main className="p-8">
      <h1 className="text-xl">Customers</h1>
      <ul>
        {customers.map((customer) => (
          <li key={customer.id}>{customer.name}</li>
        ))}
      </ul>
    </main>
  )
}
"""
red = ap.write(slug, {"src/App.tsx": THROWS}, message="Read a list that is not there")
if red.get("ok"):
    fail("a page that throws passed the gate")
if red.get("stage") != "visit":
    fail(f"the failing stage is {red.get('stage')}, not visit")
if "TypeError" not in messages(red):
    fail(f"the browser's own words are missing: {messages(red)}")
where = [error for error in red["errors"] if error.get("file")]
if not any(error["file"].endswith("App.tsx") for error in where):
    fail(f"the error does not name the app's own file: {[e.get('file') for e in red['errors']]}")
first = next(error for error in where if error["file"].endswith("App.tsx"))
print(f"   {first['file']}:{first['line']}  {first['message'][:70]}")


# -- green, and committed --------------------------------------------------

step("A page that renders is opened, judged, and committed")
WORKS = """\
import { useEffect, useState } from 'react'
import { viaGateway } from './lib/api'

type Customer = { id: string; name: string }

export default function App() {
  const [customers, setCustomers] = useState<Customer[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    viaGateway<{ customers: Customer[] }>('crm', 'customers')
      .then((d) => setCustomers(d.customers))
      .catch((e: Error) => setError(e.message))
  }, [])

  if (error) return <p className="p-8 text-red-600">{error}</p>
  return (
    <main className="p-8 font-sans">
      <h1 className="text-xl font-semibold">Customers</h1>
      <ul className="mt-4 space-y-2" data-testid="customers">
        {customers.map((customer) => (
          <li key={customer.id} className="rounded bg-white p-3 shadow-sm">
            {customer.name}
          </li>
        ))}
      </ul>
    </main>
  )
}
"""
declared = ap.declare_api(
    slug,
    "crm",
    f"{UPSTREAM}/api",
    token_env="CRM_TOKEN",
    paths=["customers/**"],
    methods=["GET"],
    description="The customer directory",
)
if not declared.get("ok"):
    fail(f"declare_api: {declared}")
if not ap.set_env(slug, "CRM_TOKEN", TOKEN).get("ok"):
    fail("the human's half of the credential could not be stored")

green = ap.write(slug, {"src/App.tsx": WORKS}, message="List the customers")
if not green.get("ok"):
    fail(f"a working page did not pass the gate: {green.get('errors')}")
if green.get("stage") != "visit":
    fail(f"a green gate stopped at {green.get('stage')}")
if not green.get("committed"):
    fail("a green gate committed nothing")
visited = green["visited"]
if [visit["route"] for visit in visited] != ["/"]:
    fail(f"the wrong routes were opened: {visited}")
if visited[0]["blank"] or not visited[0]["ok"]:
    fail(f"the page came back blank or red: {visited[0]}")
print(f"   / opened, titled {visited[0]['title']!r}, committed {green['commit'][:12]}")


# -- the M13 seam: the API answers during the visit ------------------------

step("The declared API answers during the visit, with its token (D24 + D25)")
if not SEEN:
    fail("the upstream was never called: the visit saw an app with no data")
seen = SEEN[-1]
if seen["path"] != "/api/customers":
    fail(f"the upstream was called at {seen['path']}")
if seen["headers"].get("Authorization") != f"Bearer {TOKEN}":
    fail(f"the upstream did not get the credential: {seen['headers']}")
if TOKEN in json.dumps(green):
    fail("the token came back in the gate's report")
print(f"   {seen['path']} with Authorization, and the report holds no token")


# -- what the app was declared to show -------------------------------------

step("A declared route is opened, and its promise is checked")
promised = ap.declare_route(
    slug, "/", selector="[data-testid='customers'] li", text="Globex",
    description="The list everyone opens first",
)
if not promised.get("ok") or promised.get("visited") is not True:
    fail(f"declare_route: {promised}")
again = ap.write(slug, {"src/App.tsx": WORKS}, message="Same page, now with a promise")
if not again.get("ok"):
    fail(f"the page shows what it promised, but the gate went red: {messages(again)}")
print("   two customers from the upstream, both of them on the page")

broken = ap.declare_route(slug, "/", selector="table tbody tr", text="Globex")
if not broken.get("ok"):
    fail(f"declare_route: {broken}")
red = ap.write(slug, {"src/App.tsx": WORKS}, message="A promise the page does not keep")
if red.get("ok"):
    fail("a route that does not show what it promised passed the gate")
if "table tbody tr" not in messages(red):
    fail(f"the report does not name the selector: {messages(red)}")
print(f"   {messages(red)[:90]}")

bad = ap.declare_route(slug, "https://example.com/customers")
if bad.get("ok") or bad.get("code") != "invalid-route":
    fail(f"a URL was accepted as a route: {bad}")
listed = ap.routes(slug)["routes"]
if [route["path"] for route in listed] != ["/"]:
    fail(f"the declarations are wrong: {listed}")
print(f"   a URL is not a route: {bad['error'][:70]}")


# -- the switch that is stated, not assumed --------------------------------

step("Turning the check off says so, rather than looking green (D25)")
ap.declare_route(slug, "/", selector="[data-testid='customers'] li", text="Globex")
(HOME / "policy.yaml").write_text("gate:\n  visit: false\n", encoding="utf-8")
off = ap.write(slug, {"src/App.tsx": WHITE}, message="Nothing rendered, nobody looking")
if not off.get("ok") or not off.get("committed"):
    fail(f"with the check off, the write should pass: {off.get('errors')}")
visit_stage = next(stage for stage in off["stages"] if stage["stage"] == "visit")
if not visit_stage.get("skipped") or "gate: visit: false" not in visit_stage.get("reason", ""):
    fail(f"a skipped check must say why: {visit_stage}")
state = ap.app(slug)["visit"]
if state["on"] is not False or "gate: visit: false" not in state["reason"]:
    fail(f"get_app must say the check is off: {state}")
print(f"   skipped: {visit_stage['reason']}")
(HOME / "policy.yaml").unlink()


# -- the sourcemap is for this machine, not for the internet ---------------

step("The gate builds with a sourcemap; what ships carries none")
ap.write(slug, {"src/App.tsx": WORKS}, message="Back to the working page")
built = list((root / "dist").rglob("*.map"))
if not built:
    fail("the gate built without a sourcemap, so its errors name minified chunks")
deployed = ap.deploy(slug, target="local")
if not deployed.get("ok"):
    fail(f"deploy: {deployed}")
served = ApplacePaths(home=HOME).served(slug, "preview")
shipped = list(served.rglob("*.map"))
if shipped:
    fail(f"a deploy shipped this app's sources: {[str(p.name) for p in shipped]}")
for path in served.rglob("*"):
    if path.is_file() and path.suffix in (".js", ".css", ".html", ".json"):
        if TOKEN in path.read_text(encoding="utf-8", errors="replace"):
            fail(f"{path} holds the token")
print(f"   {len(built)} map(s) where the gate reads them, none in what ships")

print(f"\n{GREEN}{BOLD}M14 accepted.{OFF} The gate ends in the browser, and says so "
      f"when it does not.\n")
PY
