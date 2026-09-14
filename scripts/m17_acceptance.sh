#!/usr/bin/env bash
#
# M17 acceptance -- the delegated call (D28):
#   two homes under one root build the same app against the same declaration,
#   which names an exchange instead of a token -- and the company's API is
#   called twice with two different credentials, one minted for each person;
#   an assertion put on the request by a door is what gets exchanged, and it
#   stops at the gateway rather than going upstream;
#   a home that nobody named is a 401 that says so, and never the other home's
#   token;
#   a token is reused until the second the exchange said, and asked for again
#   after it;
#   the exchange secret and every minted token are absent from both journals,
#   from `machine audit`, from the gateway's own log and from everything the
#   browser downloads;
#   and the serverless function that ships in the repository -- run under node,
#   against the same two fakes, with no Applace anywhere near it -- answers the
#   same way the preview gateway did, which is the only way "one mechanism" is
#   a fact rather than an intention.
#
# Everything here is real: real npm install, real vite build, real git, real
# gateway processes, real HTTP. The exchange and the upstream are the only
# fakes, and they are real servers too. No model is called, so this costs
# nothing but time.
#
# Needs node (>= 18, for `fetch`), npm, git and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

WORK="$(mktemp -d)"
export WORK
APPLACE_ROOT="$WORK/srv"
export APPLACE_ROOT

cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from applace import Machine, apis, gateway
from applace.db import connect
from applace.init_cmd import run_init
from applace.machine import whose
from applace.paths import ApplacePaths

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
WORK = Path(os.environ["WORK"]).resolve()
ROOT = Path(os.environ["APPLACE_ROOT"])
ALICE, BOB = "alice@example.com", "bob@example.com"
# What authenticates Applace to the company's exchange. A human sets it, it
# lives at 0600 outside every repository, and the last step of this script is
# looking for it everywhere it must not be.
SECRET = "sk-exchange-m17-must-never-be-exported"
# What a door in front of a deployed app would have put on the request.
CAROL_SESSION = "Bearer session-of-carol-2f8c"


# A serverless host, in the twenty lines it actually takes. It turns an HTTP
# request into the (request, response) pair Vercel hands a function, and does
# nothing else -- the point is that the file under test runs unmodified, on a
# runtime that has never heard of Applace.
RUNNER = """\
import { createServer } from 'node:http';
import handler from './function.mjs';

const PREFIX = '/api/gateway/';

createServer(async (req, res) => {
  if (!req.url.startsWith(PREFIX)) {
    res.writeHead(404).end('{}');
    return;
  }
  const parts = req.url.slice(PREFIX.length).split('?')[0].split('/');
  const request = {
    query: { name: parts[0], path: parts.slice(1) },
    method: req.method,
    headers: req.headers,
    url: req.url,
    body: undefined,
  };
  const response = {
    code: 200,
    status(value) { this.code = value; return this; },
    setHeader(name, value) { res.setHeader(name, value); return this; },
    json(payload) { this.send(JSON.stringify(payload)); },
    send(text) {
      res.setHeader('content-type', res.getHeader('content-type') || 'application/json');
      res.writeHead(this.code).end(text);
    },
  };
  try {
    await handler(request, response);
  } catch (error) {
    res.writeHead(500).end(JSON.stringify({ error: String(error) }));
  }
}).listen(Number(process.env.PORT), '127.0.0.1');
"""


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


# -- the company's two endpoints --------------------------------------------


class Exchange:
    """The endpoint the company hosts: one token per person, and it counts asks.

    It is strict on purpose. It checks the secret, it refuses a request that
    names neither a subject nor an assertion, and it mints a token that has
    nothing of the assertion in it -- otherwise "the assertion did not go
    upstream" would be unprovable by looking at the upstream.
    """

    def __init__(self):
        self.asked = []
        self.ttl = 300
        self.minted = {}
        self._n = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/applace/token"

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def token_for(self, who):
        """The token this exchange would mint for that subject or assertion."""
        if who not in self.minted:
            self._n += 1
            self.minted[who] = f"minted-{self._n:02d}-{os.urandom(4).hex()}"
        return self.minted[who]

    def _handler(self):
        exchange = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                asked = json.loads(self.rfile.read(length) or b"{}")
                asked["_authorization"] = self.headers.get("Authorization", "")
                asked["_at"] = time.monotonic()
                exchange.asked.append(asked)

                if asked["_authorization"] != f"Bearer {SECRET}":
                    return self._say(401, {"error": "not Applace"})
                who = asked.get("subject") or asked.get("assertion")
                if not who:
                    return self._say(400, {"error": "nobody to mint for"})
                self._say(
                    200,
                    {"token": exchange.token_for(who), "expires_in": exchange.ttl},
                )

            def _say(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


class Upstream:
    """The company's API. It answers with the credential it was handed."""

    def __init__(self):
        self.seen = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                seen = {"path": self.path, "headers": dict(self.headers.items())}
                upstream.seen.append(seen)
                raw = json.dumps(
                    {"as": self.headers.get("Authorization", ""), "invoices": []}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


def call(url, key, assertion=None):
    """Status and body, through a gateway, the way a dev server proxies."""
    request = urllib.request.Request(url, method="GET")
    request.add_header(gateway.KEY_HEADER, key)
    if assertion is not None:
        request.add_header("Authorization", assertion)
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def cli(*args, home=None):
    env = {**os.environ}
    if home is not None:
        env["APPLACE_HOME"] = str(home)
        env.pop("APPLACE_ROOT", None)
    done = subprocess.run(
        ["uv", "run", "applace", *args], capture_output=True, text=True, env=env
    )
    if done.returncode != 0:
        fail(f"`applace {' '.join(args)}` exited {done.returncode}:\n{done.stderr}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        fail(f"`applace {' '.join(args)}` printed something else:\n{done.stdout[:400]}")


# -- a machine, and a company that hands out tokens -------------------------

step("This machine can build apps at all")
report = run_init(ApplacePaths(home=WORK / "check"))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")
node = shutil.which("node")
if node is None:
    fail("node is not on PATH, and half of this script is the file that ships")
print("   " + ", ".join(f"{r.name} {r.detail}" for r in report.requirements))

exchange, upstream = Exchange(), Upstream()
step("The company hosts two things, and Applace hosts neither")
print(f"   exchange  {exchange.url}  (mints a token per person)")
print(f"   api       {upstream.base}  (answers with the credential it was given)")

APP_SOURCE = (
    "// The whole app's knowledge of who is asking: none. It fetches a relative\n"
    "// path, and the identity is the page's.\n"
    "export async function invoices() {\n"
    "  const answer = await fetch('/api/gateway/billing/invoices/42');\n"
    "  return answer.json();\n"
    "}\n"
)

machine = Machine(ROOT)
built = {}
broke = []


def builds(user):
    def work():
        ap = machine.user(user)
        made = ap.create("Billing Desk", description="What an accountant opens")
        if not made.get("ok"):
            fail(f"{user} create: {made}")
        written = ap.write(
            "billing-desk",
            {"src/invoices.ts": APP_SOURCE},
            message="Read the invoices through the gateway",
        )
        if not written.get("ok"):
            fail(f"{user} write: {written}")
        declared = ap.declare_api(
            "billing-desk",
            "billing",
            base_url=upstream.base,
            on_behalf_of={"url": exchange.url, "secret_env": "EXCHANGE_SECRET"},
            paths=["invoices/**"],
            methods=["GET"],
        )
        if not declared.get("ok"):
            fail(f"{user} declare_api: {declared}")
        if not declared["delegated"] or declared["token_env"]:
            fail(f"{user}: the declaration is not delegated: {declared}")
        if "EXCHANGE_SECRET" not in (declared.get("needs") or ""):
            fail(f"{user}: nothing told the human what to supply: {declared}")
        stored = ap.set_env("billing-desk", "EXCHANGE_SECRET", SECRET)
        if not stored.get("ok"):
            fail(f"{user} set_env: {stored}")
        # The declaration changed after the last gate, so the generated function
        # is written on the next write -- which is the one that ships it.
        again = ap.write(
            "billing-desk",
            {"src/invoices.ts": APP_SOURCE.replace("42", "42?limit=20")},
            message="Ask for a page of them",
        )
        if not again.get("ok"):
            fail(f"{user} second write: {again}")
        built[user] = ap

    return work


def run(who, work):
    try:
        work()
    except BaseException:
        broke.append(f"{who}: {traceback.format_exc()}")


step("Two people build the same app, and neither app holds a credential")
threads = [
    threading.Thread(target=run, args=(user, builds(user))) for user in (ALICE, BOB)
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
if broke:
    fail("\n".join(broke))

alice, bob = built[ALICE], built[BOB]
for user, ap in ((ALICE, alice), (BOB, bob)):
    if whose(ap.paths) != user:
        fail(f"{user}'s home does not know whose it is: {whose(ap.paths)!r}")
    generated = ap.paths.app("billing-desk") / apis.FUNCTION_PATH
    if not generated.exists():
        fail(f"{user}: nothing was committed to do this in production")
print(f"   two homes, each knowing whose it is, each with {apis.FUNCTION_PATH}")
print(f"   and one declaration naming an exchange, not a token")


# -- the call ---------------------------------------------------------------

gateways = {}


def gateway_for(ap):
    conn = connect(ap.paths.db)
    try:
        running = gateway.ensure(ap.paths, conn)
    finally:
        conn.close()
    return running.url, gateway.key_for(ap.paths)


def stop_gateways():
    for paths in list(gateways.values()):
        conn = connect(paths.db)
        try:
            gateway.stop(conn)
        finally:
            conn.close()


step("The same app, two people, two credentials")
answers = {}
for user, ap in ((ALICE, alice), (BOB, bob)):
    gateways[user] = ap.paths
    url, key = gateway_for(ap)
    status, body = call(f"{url}/billing-desk/billing/invoices/42", key)
    if status != 200:
        stop_gateways()
        fail(f"{user}: the gateway answered {status}: {body[:300]}")
    answers[user] = json.loads(body)["as"]

try:
    if answers[ALICE] == answers[BOB]:
        fail(f"both people were the same to the API: {answers[ALICE]}")
    for user in (ALICE, BOB):
        if answers[user] != f"Bearer {exchange.token_for(user)}":
            fail(f"{user} was not who the API was called as: {answers[user]}")
    if len(exchange.asked) != 2:
        fail(f"the exchange was asked {len(exchange.asked)} times, not twice")
    for asked in exchange.asked:
        if asked["app"] != "billing-desk" or asked["api"] != "billing":
            fail(f"the exchange was not told what was asking: {asked}")
        if asked["asserted_by"] != "applace" or "assertion" in asked:
            fail(f"preview must say who vouched, and it is Applace: {asked}")
    print(f"   alice → {answers[ALICE]}")
    print(f"   bob   → {answers[BOB]}")
    print("   the exchange was asked twice, each time with the subject and "
          "asserted_by=applace")

    # -- an assertion from a real door --------------------------------------

    step("A door's assertion is what gets exchanged, and it stops at the gateway")
    url, key = gateway_for(alice)
    before = len(upstream.seen)
    status, body = call(
        f"{url}/billing-desk/billing/invoices/42", key, assertion=CAROL_SESSION
    )
    if status != 200:
        fail(f"an asserted call answered {status}: {body[:300]}")
    got = json.loads(body)["as"]
    if got != f"Bearer {exchange.token_for(CAROL_SESSION)}":
        fail(f"the call was not made as carol: {got}")
    if got == answers[ALICE]:
        fail("the assertion was ignored and the home's own id used instead")
    asked = exchange.asked[-1]
    if asked.get("asserted_by") != "runtime" or asked.get("assertion") != CAROL_SESSION:
        fail(f"the exchange was not handed the assertion: {asked}")
    if "subject" in asked:
        fail("Applace named a subject for a call that already had one")
    arrived = json.dumps(upstream.seen[before:])
    if CAROL_SESSION in arrived:
        fail("the assertion was forwarded to the upstream")
    print(f"   carol's session → {got}, and the upstream never saw the session")
    print(f"   asserted_by=runtime, and the home's own id was not used")

    # -- nobody's home ------------------------------------------------------

    step("A home nobody named is a 401, and never the other home's token")
    # Everything alice has -- the same app, the same declaration, the same
    # answered secret -- except the one line that says whose home it is. Not
    # her gateway, though: its claim and its key are hers, and a copy of those
    # would be this script calling her process and believing it had proved
    # something.
    unnamed = ApplacePaths(WORK / "unnamed")
    unnamed.create()
    shutil.copy2(alice.paths.db, unnamed.db)
    for name in os.listdir(alice.paths.env):
        if name.endswith(".env"):
            shutil.copy2(alice.paths.env / name, unnamed.env / name)
    if unnamed.config.exists():
        unnamed.config.unlink()
    conn = connect(unnamed.db)
    try:
        conn.execute("DELETE FROM gateway")
        conn.commit()
        running = gateway.ensure(unnamed, conn)
    finally:
        conn.close()
    if running.url == gateway_for(alice)[0]:
        fail("the unnamed home is sharing alice's gateway; nothing is proved")
    if whose(unnamed):
        fail(f"the unnamed home is named after all: {whose(unnamed)}")
    gateways["unnamed"] = unnamed
    asks = len(exchange.asked)
    calls = len(upstream.seen)
    status, body = call(
        f"{running.url}/billing-desk/billing/invoices/42", gateway.key_for(unnamed)
    )
    if status != 401:
        fail(f"an unattributable call answered {status}, not 401: {body[:300]}")
    said = json.loads(body)["error"]
    if "whose it is" not in said or "whoami" not in said:
        fail(f"the 401 does not say what is missing: {said}")
    if len(exchange.asked) != asks or len(upstream.seen) != calls:
        fail("a call with nobody behind it still reached the company")
    print(f"   401 — {said[:96]}…")
    print("   the exchange was not asked, and the API was not called")

    # -- the cache ----------------------------------------------------------

    step("A token is reused until it expires, and asked for again after it")
    url, key = gateway_for(alice)
    asks = len(exchange.asked)
    for _ in range(3):
        if call(f"{url}/billing-desk/billing/invoices/7", key)[0] != 200:
            fail("a repeated call failed")
    if len(exchange.asked) != asks:
        fail(f"the exchange was asked {len(exchange.asked) - asks} more times")
    print(f"   3 more calls as alice, 0 more asks: the token was still good")

    exchange.ttl = int(apis.CLOCK_SKEW) + 2  # good for two seconds, then not
    short = "Bearer session-that-expires"
    if call(f"{url}/billing-desk/billing/invoices/7", key, assertion=short)[0] != 200:
        fail("the short-lived call failed")
    asks = len(exchange.asked)
    if call(f"{url}/billing-desk/billing/invoices/7", key, assertion=short)[0] != 200:
        fail("the immediate repeat failed")
    if len(exchange.asked) != asks:
        fail("a token was thrown away while it was still good")
    time.sleep(2.5)
    if call(f"{url}/billing-desk/billing/invoices/7", key, assertion=short)[0] != 200:
        fail("the call after the expiry failed")
    if len(exchange.asked) != asks + 1:
        fail("an expired token was used again")
    exchange.ttl = 300
    print(f"   a {int(apis.CLOCK_SKEW) + 2}s token: reused at once, asked for "
          f"again 2.5s later")

    # -- nothing is kept ----------------------------------------------------

    step("The secret and every minted token are in nothing that is written down")
    tokens = sorted(exchange.minted.values())
    haystacks = {}
    for user, ap in ((ALICE, alice), (BOB, bob)):
        haystacks[f"{user}'s journal"] = ap.paths.db.read_bytes()
        log = ap.paths.logs / gateway.LOG_NAME
        if not log.exists():
            fail(f"{user}'s gateway wrote no log at all, so nothing was proved")
        haystacks[f"{user}'s gateway log"] = log.read_bytes()
        root = ap.paths.app("billing-desk")
        shipped = bytearray()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            parts = set(path.relative_to(root).parts)
            if parts & {"node_modules", ".git"}:
                continue
            shipped += path.read_bytes()
        haystacks[f"{user}'s repository and build"] = bytes(shipped)
    haystacks["machine audit"] = json.dumps(
        cli("machine", "audit", "--json")
    ).encode()

    for where, raw in haystacks.items():
        if SECRET.encode() in raw:
            fail(f"the exchange secret is in {where}")
        for token in tokens:
            if token.encode() in raw:
                fail(f"a minted token is in {where}")
    if b"EXCHANGE_SECRET" not in haystacks[f"{ALICE}'s repository and build"]:
        fail("the shipped function does not even name the variable it needs")
    print(f"   {len(tokens)} minted tokens and one secret, in none of: "
          + ", ".join(sorted(haystacks)))

    audit = cli("machine", "audit", "--json", "-k", "api")["events"]
    if len(audit) != 2 or not all(event["delegated"] for event in audit):
        fail(f"the audit does not record that these are delegated: {audit}")
    if any(event["token_env"] for event in audit):
        fail("the audit thinks these apps hold a credential")
    print(f"   and `machine audit -k api` says both are delegated, via "
          f"{audit[0]['exchange']}")

    # -- the same thing, in production --------------------------------------

    step("The function that ships answers the same way, under node")
    ship = WORK / "ship"
    ship.mkdir()
    committed = alice.paths.app("billing-desk") / apis.FUNCTION_PATH
    # The file out of the repository, byte for byte -- not a re-render of it.
    shutil.copy2(committed, ship / "function.mjs")
    (ship / "run.mjs").write_text(RUNNER, encoding="utf-8")
    child = subprocess.Popen(
        [node, str(ship / "run.mjs")],
        cwd=ship,
        env={
            "PATH": os.environ["PATH"],
            "EXCHANGE_SECRET": SECRET,
            "PORT": "8877",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        base = "http://127.0.0.1:8877"
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{base}/__up", timeout=1)
                break
            except urllib.error.HTTPError:
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        else:
            fail("the shipped function never came up under node")

        def node_call(path, assertion=None):
            request = urllib.request.Request(f"{base}{path}", method="GET")
            if assertion is not None:
                request.add_header("Authorization", assertion)
            try:
                with urllib.request.urlopen(request, timeout=20) as answer:
                    return answer.status, answer.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        asks = len(exchange.asked)
        status, body = node_call(
            "/api/gateway/billing/invoices/42", assertion=CAROL_SESSION
        )
        if status != 200:
            fail(f"node answered {status}: {body[:300]}")
        if json.loads(body)["as"] != f"Bearer {exchange.token_for(CAROL_SESSION)}":
            fail(f"the shipped function called the API as somebody else: {body[:200]}")
        if exchange.asked[-1]["asserted_by"] != "runtime":
            fail("the shipped function invented a subject")
        if len(exchange.asked) - asks != 1:
            fail("the shipped function asked more than once for one call")

        status, body = node_call("/api/gateway/billing/invoices/42")
        if status != 401:
            fail(f"node answered {status} to a call with no assertion, not 401")
        if "nobody to call it as" not in json.loads(body)["error"]:
            fail(f"the 401 from node says something else: {body[:200]}")

        asks = len(exchange.asked)
        node_call("/api/gateway/billing/invoices/43", assertion=CAROL_SESSION)
        if len(exchange.asked) != asks:
            fail("the shipped function does not reuse a token it was given")

        status, body = node_call(
            "/api/gateway/billing/admin/wipe", assertion=CAROL_SESSION
        )
        if status != 403:
            fail(f"node allowed a path nobody declared: {status}")

        print(f"   carol's session → Bearer {exchange.token_for(CAROL_SESSION)}, "
              f"which is the token the preview gateway got for her")
        print("   no assertion → 401, undeclared path → 403, second call → no ask")
        print("   and the file that did all that imports nothing")
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
finally:
    stop_gateways()
    exchange.close()
    upstream.close()

print(f"\n{GREEN}{BOLD}M17 accepted.{OFF} The app calls the company's API as the "
      f"person using it, in preview and in production, and Applace keeps "
      f"nothing of either.\n")
PY
