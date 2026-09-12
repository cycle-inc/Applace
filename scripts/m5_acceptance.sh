#!/usr/bin/env bash
#
# M5 acceptance:
#   an app created in a chat exists on GitHub a moment later, with its history;
#   a push made behind Applace's back makes the next snapshot refuse (D13).
#
# GitHub is a stand-in on localhost, but everything else is real: real `npm
# install`, real vite build, real git, and repositories that are real bare
# repositories a real `git push` lands in. What the stand-in does not prove is
# github.com's own dialect -- for that, set APPLACE_ACCEPTANCE_ORG to an
# organisation you can create repositories in and the script also asks the real
# API whether it is reachable (read-only, it creates nothing there).
#
# Needs node, npm, git and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME
export APPLACE_GITHUB_TOKEN="acceptance-token"

cleanup() { rm -rf "$(dirname "$APPLACE_HOME")"; }
trap cleanup EXIT

exec uv run python - <<'PY'
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def run(*args, cwd=None, check=True):
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and done.returncode != 0:
        fail(f"`{' '.join(args)}` exited {done.returncode}:\n{done.stdout}{done.stderr}")
    return done


# -- a GitHub on localhost, whose repositories are real bare repositories -----

REPOS = {}
BARES = Path(os.environ["APPLACE_HOME"]).parent / "github"
BARES.mkdir(parents=True, exist_ok=True)


def make_repo(full_name):
    bare = BARES / f"{full_name.replace('/', '__')}.git"
    if not bare.exists():
        run("git", "init", "--bare", "-b", "main", str(bare))
    REPOS[full_name] = {
        "full_name": full_name,
        "name": full_name.split("/")[-1],
        "html_url": f"https://github.localhost/{full_name}",
        "clone_url": bare.as_uri(),
        "private": True,
        "default_branch": "main",
    }
    return REPOS[full_name]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/repos/"):
            name = self.path.removeprefix("/repos/")
            return self.reply(200, REPOS[name]) if name in REPOS else self.reply(404, {})
        self.reply(200, {"login": "acme"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        full_name = f"{self.path.split('/')[2]}/{payload['name']}"
        if full_name in REPOS:
            return self.reply(422, {"message": "name already exists on this account"})
        self.reply(201, make_repo(full_name))

    def do_PUT(self):
        self.reply(204, {})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
api_base = f"http://127.0.0.1:{server.server_address[1]}"


def bare_head(full_name):
    bare = REPOS[full_name]["clone_url"].removeprefix("file://")
    done = run("git", "-C", bare, "rev-parse", "main", check=False)
    return done.stdout.strip() if done.returncode == 0 else None


# -- the run -------------------------------------------------------------

step("applace init && applace github connect --org acme")
run("uv", "run", "applace", "init")
run("uv", "run", "applace", "github", "connect", "--org", "acme", "--api-base", api_base)
status = run("uv", "run", "applace", "github", "status")
print("  " + status.stdout.strip().replace("\n", "\n  "))

step('An app created in a chat exists on GitHub, with its history')
from applace.paths import ApplacePaths, applace_home  # noqa: E402
from applace.server import build_server  # noqa: E402
import asyncio  # noqa: E402

server_obj = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server_obj.call_tool(tool, kwargs))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[-1].text)


created = call("create_app", name="Chat App", description="Made in a chat")
if not created["ok"]:
    fail(created)
if not created["pushed"]:
    fail(f"the app was not pushed: {created['warnings']}")
app_path = Path(created["path"])
print(f"  local   {created['commit'][:12]}  {app_path}")
print(f"  github  {created['github_url']}")
if bare_head("acme/chat-app") != created["commit"]:
    fail("the repository does not have the app's first commit")
log = run("git", "-C", REPOS["acme/chat-app"]["clone_url"].removeprefix("file://"),
          "log", "--oneline")
print(f"  remote  {log.stdout.strip()}")

step("A green write lands there too")
written = call("write_files", app="chat-app", files={"src/App.tsx": """\
export default function App() {
  return <main className="p-8 text-2xl">Made in a chat</main>
}
"""}, message="Say what it is")
if not written["ok"]:
    fail(written)
if not written["github"]["pushed"]:
    fail(written["github"])
if bare_head("acme/chat-app") != written["commit"]:
    fail("the green commit is not on the remote")
print(f"  pushed  {written['commit'][:12]}")

step("Someone pushes behind Applace's back")
clone = Path(os.environ["APPLACE_HOME"]).parent / "colleague"
run("git", "clone", REPOS["acme/chat-app"]["clone_url"].removeprefix("file://"), str(clone))
(clone / "COLLEAGUE.md").write_text("I was here\n")
run("git", "add", "-A", cwd=clone)
run("git", "-c", "user.email=they@example.com", "-c", "user.name=They",
    "commit", "-m", "Their change", cwd=clone)
run("git", "push", cwd=clone)
theirs = bare_head("acme/chat-app")
print(f"  remote is now at {theirs[:12]}")

step("The next snapshot refuses, and nothing is overwritten (D13)")
refused = call("write_files", app="chat-app", files={"src/App.tsx": """\
export default function App() {
  return <main className="p-8 text-2xl">Written while the remote moved</main>
}
"""}, message="Write while the remote has moved")
if not refused["ok"]:
    fail(f"the gate should still be green: {refused}")
github = refused["github"]
if not github["diverged"]:
    fail(f"the push should have been refused: {github}")
print(f"  committed locally  {refused['commit'][:12]}")
print(f"  refused            {github['reason']}")
if bare_head("acme/chat-app") != theirs:
    fail("their commit was overwritten -- D13 is broken")
print("  their commit is still the remote tip")

step("Once a human reconciles, the backlog catches up")
run("git", "-c", "user.email=me@example.com", "-c", "user.name=Me",
    "pull", "--rebase", REPOS["acme/chat-app"]["clone_url"], "main", cwd=app_path)
pushed = run("uv", "run", "applace", "push", "chat-app")
print("  " + pushed.stdout.strip())
head = run("git", "-C", str(app_path), "rev-parse", "HEAD").stdout.strip()
if bare_head("acme/chat-app") != head:
    fail("the reconciled history did not land")
detail = call("get_app", app="chat-app")
if detail["unpushed"] != 0:
    fail(f"{detail['unpushed']} commits are still only local")
print(f"  remote and local agree at {head[:12]}, nothing outstanding")

real_org = os.environ.get("APPLACE_ACCEPTANCE_ORG")
if real_org:
    step(f"The real GitHub API answers for {real_org} (read-only)")
    run("uv", "run", "applace", "github", "connect", "--org", real_org)
    out = run("uv", "run", "applace", "github", "status").stdout.strip()
    print("  " + out.replace("\n", "\n  "))
    if "is reachable" not in out:
        fail("the real organisation did not answer")

print(f"\n{GREEN}M5 ACCEPTANCE: PASS{OFF}")
PY
