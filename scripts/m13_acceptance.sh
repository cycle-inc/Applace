#!/usr/bin/env bash
#
# M13 acceptance -- the gateway (D24):
#   a real company API on loopback that records every request it is sent;
#   a real app on the built-in stack that calls it through the gateway;
#   the token arrives at the upstream and nowhere else -- not in the bundle,
#   not in the repository, not in any answer the class gives;
#   a `fetch` to an undeclared host is a red gate that commits nothing;
#   the gateway refuses a method and a path nobody declared;
#   another process on the machine, without the key, is told "not for you";
#   and the declaration is committed as an ordinary serverless function that
#   imports nothing from Applace.
#
# Everything here is real: real npm install, real vite dev server, real HTTP,
# real git. No model is called, so this costs nothing but time.
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
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from applace import Applace, apis, gateway
from applace.db import connect
from applace.init_cmd import run_init
from applace.paths import ApplacePaths

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
HOME = Path(os.environ["APPLACE_HOME"])
TOKEN = "sk-acceptance-not-a-real-token"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def get(url, *, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# -- the company API, which records what it is sent ------------------------

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


# -- a home and an app -----------------------------------------------------

step("A home, and an app on the built-in stack")
report = run_init(ApplacePaths(home=HOME))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")

ap = Applace(HOME)
created = ap.create("Customer Board", description="What M13 can do with an API")
if not created.get("ok"):
    fail(f"create: {created}")
slug = created["app"]
root = HOME / "apps" / slug
print(f"   {slug} at {created['commit'][:12]}")


# -- the declaration -------------------------------------------------------

step("The agent declares the API by name, and never sees a value (D8, D24)")
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
if declared["fetch"] != "/api/gateway/crm/...":
    fail(f"the agent was told to fetch {declared['fetch']}")
if declared["token_set"] is not False or "applace env set" not in declared["needs"]:
    fail(f"the human's half is missing: {declared}")
print(f"   fetch {declared['fetch']}, and a human runs `{declared['needs']}`")

step("A token with the bundler's prefix is refused outright")
refused = ap.declare_api(slug, "bad", "https://crm.internal", token_env="VITE_TOKEN")
if refused.get("ok") or refused.get("code") != "invalid-api":
    fail(f"a prefixed token was accepted: {refused}")
print(f"   {refused['error'][:88]}")


# -- the guide: an undeclared call is a red gate ---------------------------

step("A fetch straight at a host nobody declared is red, and commits nothing")
DIRECT = """\
import { useEffect, useState } from 'react'

type Customer = { id: string; name: string }

export default function App() {
  const [customers, setCustomers] = useState<Customer[]>([])
  useEffect(() => {
    fetch('https://crm.internal/api/customers')
      .then((r) => r.json())
      .then((d: { customers: Customer[] }) => setCustomers(d.customers))
      .catch(() => setCustomers([]))
  }, [])
  return <main className="p-8">{customers.length} customers</main>
}
"""
before = ap.app(slug)["commit"]
red = ap.write(slug, {"src/App.tsx": DIRECT}, message="Call the CRM directly")
if red.get("ok"):
    fail("an undeclared host passed the gate")
if red.get("stage") != "apis":
    fail(f"the failing stage is {red.get('stage')}, not apis")
if red["undeclared_calls"][0]["host"] != "crm.internal":
    fail(f"the report does not name the host: {red.get('undeclared_calls')}")
if ap.app(slug)["commit"] != before:
    fail("a red write committed something (D4)")
if not (root / "src" / "App.tsx").exists():
    fail("the red write is not on disk; the agent cannot fix what it cannot read")
print(f"   {red['errors'][0]['file']}:{red['errors'][0]['line']} "
      f"{red['errors'][0]['message'][:70]}")


# -- the green write -------------------------------------------------------

step("Through the gateway, the same page is green")
VIA = """\
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
      <h1 className="text-xl font-semibold">{customers.length} customers</h1>
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
green = ap.write(slug, {"src/App.tsx": VIA}, message="List the customers")
if not green.get("ok"):
    fail(f"the gateway version did not pass the gate: {green.get('errors')}")
if green.get("generated") != apis.FUNCTION_PATH:
    fail(f"the function was not generated: {green.get('generated')}")
if ap.app(slug)["dirty"]:
    fail("the app is dirty after a green write")

function = (root / apis.FUNCTION_PATH).read_text(encoding="utf-8")
# Comments may say where the file came from; the code may not depend on it (D1).
code = "\n".join(
    line for line in function.splitlines() if not line.strip().startswith("//")
)
if "applace" in code.lower():
    fail("the generated function's code mentions Applace (D1)")
if "import " in code or "require(" in code:
    fail("the generated function imports something; D1 says it must not")
if TOKEN in function or "CRM_TOKEN" not in function:
    fail("the function should carry the variable's name and never its value")
print(f"   committed {apis.FUNCTION_PATH} ({len(function.splitlines())} lines, "
      f"no imports)")


# -- the human's half ------------------------------------------------------

step("A human supplies the value, and it stays on their machine (D8)")
stored = ap.set_env(slug, "CRM_TOKEN", TOKEN)
if not stored.get("ok"):
    fail(f"set_env: {stored}")
everywhere = json.dumps(
    [stored, ap.env(slug), ap.apis(slug), ap.app(slug), ap.card(slug), ap.skill()],
    default=str,
)
if TOKEN in everywhere:
    fail("a method handed the value back")
print("   six answers later, the value is only in env/<app>.env")


# -- the running thing -----------------------------------------------------

step("The page calls a relative path; the token is added one hop away")
preview = ap.preview(slug)
if not preview.get("ok"):
    fail(f"preview: {preview}")
url = preview["url"].rstrip("/")

status, body = get(f"{url}/api/gateway/crm/customers")
if status != 200:
    fail(f"the proxied call answered {status}: {body[:200]}")
if "Globex" not in body:
    fail(f"the upstream's answer did not come back: {body[:200]}")
if TOKEN in body:
    fail("the token came back to the browser")
if not SEEN:
    fail("the upstream was never called")
seen = SEEN[-1]
if seen["headers"].get("Authorization") != f"Bearer {TOKEN}":
    fail(f"the upstream did not get the credential: {seen['headers']}")
if any(name.lower() == gateway.KEY_HEADER.lower() for name in seen["headers"]):
    fail("the gateway's own key was forwarded upstream")
print(f"   {url}/api/gateway/crm/customers → {seen['path']} with Authorization")

step("What the declaration does not cover does not reach the upstream")
count = len(SEEN)
for path, method, expected in [
    ("/api/gateway/crm/admin", "GET", 403),
    ("/api/gateway/crm/customers/1", "DELETE", 403),
    ("/api/gateway/billing/invoices", "GET", 403),
]:
    status, _ = get(f"{url}{path}", method=method)
    if status != expected:
        fail(f"{method} {path} answered {status}, expected {expected}")
if len(SEEN) != count:
    fail("a refused call still reached the upstream")
print("   admin, DELETE and an undeclared API: three 403s, nothing forwarded")

step("Another process on this machine is not this home (D20)")
conn = connect(ApplacePaths(home=HOME).db)
running = gateway.status(conn)
if running is None:
    fail("the gateway is not running while a preview that needs it is")
status, body = get(f"{running.url}/{slug}/crm/customers")
if status != 403 or "not for you" not in body:
    fail(f"a keyless call to the gateway answered {status}: {body[:200]}")
print(f"   {running.url} without the key: {json.loads(body)['error']}")


# -- what ships ------------------------------------------------------------

step("Nothing a browser downloads holds the token or the key (D24)")
deployed = ap.deploy(slug, target="local")
if not deployed.get("ok"):
    fail(f"deploy: {deployed}")
dist = ApplacePaths(home=HOME).served(slug, "preview")
downloadable = [
    path for path in dist.rglob("*") if path.is_file() and path.suffix in
    (".js", ".css", ".html", ".map", ".json")
]
if not downloadable:
    fail(f"nothing was built into {dist}")
key = gateway.key_for(ApplacePaths(home=HOME))
for path in downloadable:
    content = path.read_text(encoding="utf-8", errors="replace")
    if TOKEN in content:
        fail(f"{path} holds the token")
    if key in content:
        fail(f"{path} holds the gateway key")
    if "127.0.0.1:53" in content:
        fail(f"{path} holds the gateway's local address")
print(f"   {len(downloadable)} built files, none of them holding a secret")
if not any("static" in w or "404" in w for w in deployed.get("warnings", [])):
    print(f"   warnings: {deployed.get('warnings')}")

step("Stopping the last preview stops the gateway")
ap.stop_preview(slug)
if gateway.status(connect(ApplacePaths(home=HOME).db)) is not None:
    fail("the gateway is still holding a credential with nothing running")
print("   the process holding the credential is gone")

print(f"\n{GREEN}{BOLD}M13 accepted.{OFF} The credential reached the upstream and "
      f"nothing else.\n")
PY
