#!/usr/bin/env bash
#
# M9 acceptance -- the handover:
#   an agent's work arrives as a pull request a person can read, and nothing
#   reaches the default branch without them merging it;
#   a person edits the repository by hand and the agent is told, refused, and
#   told what to say;
#   `applace open` puts a human in front of the thing itself;
#   and the whole harness runs from a built wheel, which is what `uvx applace`
#   installs.
#
# GitHub is a stand-in on localhost, as in M5, but its repositories are real
# bare repositories: the branch, the merge and the divergence are git's own.
# Everything else is real -- real npm install, real vite build, real git.
#
# Needs node, npm, git and a network.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
export APPLACE_ACCEPTANCE_REPO="$REPO"

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME
export APPLACE_GITHUB_TOKEN="acceptance-token"

cleanup() {
  uv run applace stop review-me >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
REPO = Path(os.environ["APPLACE_ACCEPTANCE_REPO"])


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def run(*args, cwd=None, check=True, env=None):
    done = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        env=None if env is None else {**os.environ, **env},
    )
    if check and done.returncode != 0:
        fail(f"`{' '.join(args)}` exited {done.returncode}:\n{done.stdout}{done.stderr}")
    return done


# -- a GitHub on localhost, with real bare repositories and real pull requests

REPOS = {}
PULLS = []
BARES = Path(os.environ["APPLACE_HOME"]).parent / "github"
BARES.mkdir(parents=True, exist_ok=True)


def bare_path(full_name):
    return BARES / f"{full_name.replace('/', '__')}.git"


def make_repo(full_name):
    bare = bare_path(full_name)
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


def sha(full_name, ref):
    done = run("git", "-C", str(bare_path(full_name)), "rev-parse", "--verify", ref,
               check=False)
    return done.stdout.strip() or None


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
        route, _, query = self.path.partition("?")
        if route.startswith("/repos/") and route.endswith("/pulls"):
            full_name = route.removeprefix("/repos/").removesuffix("/pulls")
            wanted = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            head = wanted.get("head", "").split(":")[-1]
            return self.reply(200, [
                pull for pull in PULLS
                if pull["_repo"] == full_name and pull["state"] == "open"
                and pull["head"]["ref"] == head
            ])
        if route.startswith("/repos/"):
            name = route.removeprefix("/repos/")
            return self.reply(200, REPOS[name]) if name in REPOS else self.reply(404, {})
        self.reply(200, {"login": "acme"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/repos/") and self.path.endswith("/pulls"):
            full_name = self.path.removeprefix("/repos/").removesuffix("/pulls")
            head, base = payload["head"], payload["base"]
            if sha(full_name, head) == sha(full_name, base):
                return self.reply(422, {"message": f"No commits between {base} and {head}"})
            number = len(PULLS) + 1
            pull = {
                "_repo": full_name, "number": number, "state": "open",
                "title": payload["title"], "body": payload.get("body", ""),
                "head": {"ref": head}, "base": {"ref": base},
                "html_url": f"https://github.localhost/{full_name}/pull/{number}",
            }
            PULLS.append(pull)
            return self.reply(201, pull)
        full_name = f"{self.path.split('/')[2]}/{payload['name']}"
        if full_name in REPOS:
            return self.reply(422, {"message": "name already exists on this account"})
        self.reply(201, make_repo(full_name))

    def do_PUT(self):
        self.reply(204, {})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
api_base = f"http://127.0.0.1:{server.server_address[1]}"


def merge(full_name, number):
    """What a human does in the GitHub UI: the base branch moves to the branch."""
    pull = next(p for p in PULLS if p["number"] == number)
    run("git", "-C", str(bare_path(full_name)), "update-ref",
        f"refs/heads/{pull['base']['ref']}", f"refs/heads/{pull['head']['ref']}")
    pull["state"] = "closed"


# -- the run ---------------------------------------------------------------

from applace.paths import ApplacePaths, applace_home  # noqa: E402
from applace.server import build_server  # noqa: E402

step("applace init && applace github connect --org acme --review pr")
run("uv", "run", "applace", "init")
run("uv", "run", "applace", "github", "connect", "--org", "acme",
    "--review", "pr", "--api-base", api_base)
print("  " + run("uv", "run", "applace", "github", "status").stdout.strip().replace("\n", "\n  "))

paths = ApplacePaths(applace_home())
mcp = build_server(paths)


def call(tool, **kwargs):
    result = asyncio.run(mcp.call_tool(tool, kwargs))
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[-1].text)


step("An app is born on main, because a pull request needs somewhere to go")
created = call("create_app", name="Review Me", description="A page a human reads first")
if not created["ok"]:
    fail(created)
app_path = Path(created["path"])
full = "acme/review-me"
if sha(full, "main") != created["commit"]:
    fail("the birth commit is not on main")
if sha(full, "applace/review-me") is not None:
    fail("there should be no work branch before there is work")
if PULLS:
    fail("there is nothing to review yet")
print(f"  main    {created['commit'][:12]}  {created['github_url']}")
print("  branch  none yet, and no pull request")

PAGE = """\
type Item = { name: string; owner: string }

const items: Item[] = [
  { name: 'Checkout', owner: 'Payments' },
  { name: 'Billing', owner: 'Finance' },
]

export default function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold">Services</h1>
      <ul className="mt-4 space-y-2">
        {items.map((item) => (
          <li key={item.name} className="rounded bg-white p-3 shadow-sm">
            {item.name} -- {item.owner}
          </li>
        ))}
      </ul>
    </main>
  )
}
"""

step("A green write goes to a branch, and opens one pull request (D15)")
written = call("write_files", app="review-me", files={"src/App.tsx": PAGE},
               message="List the services")
if not written["ok"]:
    fail(written)
github = written["github"]
if not github["pushed"] or github["branch"] != "applace/review-me":
    fail(github)
if not github.get("pull_request_url"):
    fail(f"the agent was not given a pull request to hand over: {github}")
print(f"  pushed  {written['commit'][:12]} to {github['branch']}")
print(f"  review  {github['pull_request_url']}")
if sha(full, "main") != created["commit"]:
    fail("main moved without a human -- D15 is broken")
print(f"  main    still {created['commit'][:12]}: nothing landed without a person")

step("A second write lands on the same branch, in the same pull request")
again = call("write_files", app="review-me",
             files={"src/App.tsx": PAGE.replace("Services", "Our services")},
             message="Rename the heading")
if not again["ok"]:
    fail(again)
if again["github"]["pull_request"]["number"] != 1:
    fail("a second pull request was opened for the same work")
if len([p for p in PULLS if p["state"] == "open"]) != 1:
    fail(f"{len(PULLS)} pull requests for one app")
print(f"  pushed  {again['commit'][:12]} to the same pull request #1")

step("A human merges it. Only then does main move")
merge(full, 1)
if sha(full, "main") != again["commit"]:
    fail("the merge did not move main")
print(f"  main    {sha(full, 'main')[:12]}, because somebody merged it")
caught_up = run("uv", "run", "applace", "push", "review-me")
print("  " + caught_up.stdout.strip().replace("\n", "\n  "))
if "nothing to propose" not in caught_up.stdout:
    fail("a branch with nothing new on it should say so, not open an empty pull request")

step("A person edits the app by hand, and the agent is told before it writes")
page = app_path / "src" / "App.tsx"
theirs = page.read_text().replace("Our services", "Our services (I am rewording this)")
page.write_text(theirs)
detail = call("get_app", app="review-me")
if detail["human"]["edits"] != ["src/App.tsx"]:
    fail(f"the agent was not told about the human's edit: {detail['human']}")
print(f"  get_app {detail['human']['note']}")

step("And the write over it is refused, with their bytes untouched")
refused = call("write_files", app="review-me",
               files={"src/App.tsx": PAGE.replace("Services", "What the model wanted")})
if refused["ok"] or refused["stage"] != "handover":
    fail(f"the write should have been refused: {refused}")
if page.read_text() != theirs:
    fail("the agent overwrote a person's uncommitted work")
print(f"  refused {refused['errors'][0]['message'].splitlines()[0][:120]}...")
print("  file    still exactly what they typed")

step("A write somewhere else is not a conflict, and theirs rides along when it builds")
elsewhere = call("write_files", app="review-me", files={"src/lib/format.ts": """\
export function initials(name: string): string {
  return name.slice(0, 2).toUpperCase()
}
"""}, message="A helper, while a human edits the page")
if not elsewhere["ok"]:
    fail(elsewhere)
if "(I am rewording this)" not in page.read_text():
    fail("their edit was lost by a write elsewhere")
after = call("get_app", app="review-me")
if after["human"]["edits"]:
    fail(f"their work is committed now, so it is not outstanding: {after['human']}")
print(f"  green   {elsewhere['commit'][:12]}, carrying their wording with it")
print(f"  review  {elsewhere['github']['pull_request_url']}")

step("A commit a human made is news, not a refusal")
notes = app_path / "NOTES.md"
notes.write_text("# Notes\n\nWhy this page exists.\n")
run("git", "-C", str(app_path), "add", "-A")
run("git", "-C", str(app_path), "-c", "user.email=dev@acme.test",
    "-c", "user.name=A Developer", "commit", "-m", "Explain the page")
told = call("get_app", app="review-me")
if not told["human"]["commits"]:
    fail("a human's commit was not reported")
print(f"  get_app {told['human']['note'][:140]}...")
last = call("write_files", app="review-me",
            files={"src/App.tsx": page.read_text().replace("(I am rewording this)", "")},
            message="Tidy the heading")
if not last["ok"]:
    fail(f"a human's commit must not stop the agent: {last}")
print(f"  green   {last['commit'][:12]}: their commit is part of the app now")

step("applace open -- the human's end of the handover")
preview = call("start_preview", app="review-me")
if not preview["ok"]:
    fail(preview)
opened = run("uv", "run", "applace", "open", "review-me", "--print").stdout.strip()
print(f"  default {opened}")
if preview["url"] not in opened:
    fail(f"open should have offered the preview that is running: {opened}")
for what in ("repo", "dir"):
    line = run("uv", "run", "applace", "open", "review-me", "--what", what,
               "--print").stdout.strip()
    print(f"  {what:<7} {line.split()[-1]}")
run("uv", "run", "applace", "stop", "review-me")

step("The whole thing runs from a built wheel, which is what `uvx applace` is")
run("uv", "build", "--out-dir", str(Path(os.environ["APPLACE_HOME"]).parent / "dist"),
    cwd=str(REPO))
dist = Path(os.environ["APPLACE_HOME"]).parent / "dist"
wheel = next(dist.glob("applace-*.whl"))
print(f"  built   {wheel.name}")
elsewhere_home = Path(os.environ["APPLACE_HOME"]).parent / "uvx-home"
env = {"APPLACE_HOME": str(elsewhere_home)}
init = run("uvx", "--from", str(wheel), "applace", "init", env=env)
print("  " + init.stdout.strip().replace("\n", "\n  "))
if "vite-react-ts" not in init.stdout:
    fail("the built wheel does not carry the built-in stack's template")
skill = run("uvx", "--from", str(wheel), "applace", "skill", env=env)
if "Building an app with Applace" not in skill.stdout:
    fail("the built wheel does not carry SKILL.md")
print("  skill   SKILL.md ships inside the package")
if not (elsewhere_home / "applace.db").exists():
    fail("the wheel's `applace init` did not make a home")
print(f"  home    {elsewhere_home} made by the wheel, with no checkout in sight")

print(f"\n{GREEN}M9 ACCEPTANCE: PASS{OFF}")
PY
