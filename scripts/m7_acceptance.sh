#!/usr/bin/env bash
#
# M7 acceptance:
#   an app built by the real stack becomes an address a human can open;
#   what is live is a commit, not whatever is on disk;
#   production needs a human, and a policy can refuse it even when one says yes;
#   a rollback puts the previous commit back without moving anyone's worktree;
#   and the secret that reached the build reaches nothing the agent can read.
#
# Runs in a throwaway Applace home. Needs node, npm and a network. Nothing here
# touches Vercel: the `vercel` target is exercised against a fake API in
# tests/test_vercel.py, because an acceptance run must not create projects in
# somebody's account.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  uv run applace stop release-notes >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

export SECRET="https://releases.acme.internal/v7"

step "applace init"
uv run applace init >/dev/null
printf '  %s\n' "$APPLACE_HOME"

step "applace new \"Release Notes\" (the real stack, a real install)"
uv run applace new "Release Notes" >/dev/null
uv run applace env set release-notes VITE_API_BASE_URL --value "$SECRET" >/dev/null
printf '  installed, committed, and a value only the human typed\n'

step "The agent writes the page it wants to ship"
uv run python - <<'PY'
import asyncio, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))

PAGE = """\
const base = import.meta.env.VITE_API_BASE_URL

export default function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold">Version one</h1>
      <p className="mt-2 text-slate-600">Notes from {base ?? 'nowhere'}</p>
    </main>
  )
}
"""

report = asyncio.run(server.call_tool("write_files", {
    "app": "release-notes",
    "files": {"src/App.tsx": PAGE},
    "message": "The first release notes page",
})).structured_content
if not report["ok"]:
    sys.exit(f"red at {report['stage']}: {report['errors']}")
print(f"  green, committed {report['commit'][:12]}")
PY

step "applace deploy release-notes  (no account needed)"
uv run applace deploy release-notes

step "That URL answers, with the production bundle and the human's value"
uv run python - <<'PY'
import json, os, sys, urllib.request
from applace import deploy
from applace.db import connect, list_deployments
from applace.apps import require_app
from applace.paths import ApplacePaths, applace_home

secret = os.environ["SECRET"]
paths = ApplacePaths(applace_home())
conn = connect(paths.db)
row = require_app(conn, "release-notes")
live = [d for d in list_deployments(conn, str(row["id"])) if d["status"] == "live"][0]
url, detail = str(live["url"]), json.loads(str(live["detail_json"]))

page = urllib.request.urlopen(url, timeout=10).read().decode()
if "<div id=\"root\">" not in page:
    sys.exit("that is not the built index.html")
print(f"  {url} -> {len(page)} bytes of built index.html")

bundle = next(
    urllib.request.urlopen(url.rstrip('/') + '/' + src, timeout=10).read().decode()
    for src in [page.split('src="/')[1].split('"')[0]]
)
if "Version one" not in bundle:
    sys.exit("the bundle is not the page the agent wrote")
if secret not in bundle:
    sys.exit("the build did not get the value")
print("  the bundle holds 'Version one' and the value the human typed")

port = int(detail["port"])
if port not in deploy.PORT_RANGE:
    sys.exit(f"port {port} is outside the deployment range")
if port in __import__("applace.preview", fromlist=["x"]).PORT_RANGE:
    sys.exit("a deployment took a preview's port")
print(f"  port      {port}, clear of the dev servers")

# It is serving a copy, not the app directory: the agent can keep working.
if str(paths.app("release-notes")) in str(detail["root"]):
    sys.exit("the deployment is serving out of the app's own tree")
print(f"  serving   {detail['root']}")
PY

step "A deployment is a commit, not the working tree"
uv run python - <<'PY'
import sys, urllib.request
from applace.apps import deploy_app
from applace.db import connect
from applace.paths import ApplacePaths, applace_home

paths = ApplacePaths(applace_home())
root = paths.app("release-notes")
(root / "src" / "App.tsx").write_text(
    "export default function App() { return <h1>Half written</h1> }\n", encoding="utf-8"
)
conn = connect(paths.db)
report = deploy_app(paths, conn, "release-notes")
if not report.ok:
    sys.exit(report.message)

index = urllib.request.urlopen(str(report.url), timeout=10).read().decode()
src = index.split('src="/')[1].split('"')[0]
bundle = urllib.request.urlopen(str(report.url).rstrip('/') + '/' + src, timeout=10).read().decode()
if "Half written" in bundle:
    sys.exit("it deployed the working tree")
if "Version one" not in bundle:
    sys.exit("it did not deploy the committed page either")
print(f"  deployed  {report.commit[:12]}, the last green commit")
print(f"  said      {report.warnings[0]}")
PY
# And the half-written file is still there, for whoever wrote it to finish.
git -C "$APPLACE_HOME/apps/release-notes" checkout -- src/App.tsx

step "Production without a human is refused, and says why (D6)"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))
result = asyncio.run(server.call_tool("deploy_app", {
    "app": "release-notes", "environment": "production",
}))
answer = result.structured_content or json.loads(result.content[0].text)
if answer["ok"]:
    sys.exit("the agent shipped production on its own")
if answer["code"] != "confirm-required":
    sys.exit(f"refused for the wrong reason: {answer}")
print(f"  code      {answer['code']}")
print(f"  says      {answer['error']}")
PY
printf 'n\n' | uv run applace deploy release-notes --production >/dev/null 2>&1 \
  && fail "the terminal shipped production without an answer" \
  || printf '  the terminal asks first, and "n" stops there\n'

step "A policy can take production off the table even when a human says yes"
cat > "$APPLACE_HOME/policy.yaml" <<'YAML'
exposure:
  production_deploys: deny
YAML
uv run applace deploy release-notes --production --yes >/dev/null 2>&1 \
  && fail "the policy was ignored" \
  || printf '  refused: production_deploys: deny\n'
rm "$APPLACE_HOME/policy.yaml"

step "With the policy gone and a human saying yes, it ships"
uv run applace deploy release-notes --production --yes

step "Both are live, on their own ports, and the journal knows which is which"
uv run applace deployments release-notes
uv run applace ls

step "A regression, shipped to production"
uv run python - <<'PY'
import asyncio, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))
PAGE = """\
export default function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold">Version two</h1>
    </main>
  )
}
"""
report = asyncio.run(server.call_tool("write_files", {
    "app": "release-notes",
    "files": {"src/App.tsx": PAGE},
    "message": "A regression nobody spotted",
})).structured_content
if not report["ok"]:
    sys.exit(f"red at {report['stage']}: {report['errors']}")
print(f"  green, committed {report['commit'][:12]}")
PY
uv run applace deploy release-notes --production --yes

step "applace rollback release-notes  (production, so it asks; --yes answers)"
uv run applace rollback release-notes --yes

step "Version one is back, and nobody's working tree moved"
uv run python - <<'PY'
import json, sys, urllib.request
from applace import gitrepo
from applace.apps import require_app
from applace.db import connect, list_deployments
from applace.paths import ApplacePaths, applace_home

paths = ApplacePaths(applace_home())
root = paths.app("release-notes")
conn = connect(paths.db)
rows = list_deployments(conn, str(require_app(conn, "release-notes")["id"]))
live = [d for d in rows if d["status"] == "live" and d["environment"] == "production"][0]

index = urllib.request.urlopen(str(live["url"]), timeout=10).read().decode()
src = index.split('src="/')[1].split('"')[0]
bundle = urllib.request.urlopen(str(live["url"]).rstrip('/') + '/' + src, timeout=10).read().decode()
if "Version one" not in bundle or "Version two" in bundle:
    sys.exit("the rollback did not put the earlier commit back")
print(f"  live      {live['url']} serving {str(live['commit_sha'])[:12]}")

if "Version two" not in (root / "src" / "App.tsx").read_text():
    sys.exit("the rollback checked out the past under the agent's feet")
if gitrepo.is_dirty(root):
    sys.exit("the rollback left the app dirty")
print("  the app   is still on Version two, clean, ready to be fixed")
if list(paths.work.glob("*")):
    sys.exit(f"a build worktree was left behind in {paths.work}")
print("  no worktree left behind")
PY

step "The value reached the build and nothing the agent can read (D8)"
uv run python - <<'PY'
import asyncio, json, os, subprocess, sys
from pathlib import Path
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

secret = os.environ["SECRET"]
paths = ApplacePaths(applace_home())
root = paths.app("release-notes")
server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


seen = json.dumps([
    call("deploy_app", app="release-notes"),
    call("get_app", app="release-notes"),
    call("read_files", app="release-notes", paths=["src/App.tsx", "package.json"]),
    call("get_skill"),
])
if secret in seen:
    sys.exit("the value came back through a tool result")
print(f"  not in    {len(seen)} characters of tool results, deploy_app included")

served = [
    p for p in paths.serve.rglob("*.js") if secret in p.read_text(errors="ignore")
]
if not served:
    sys.exit("the deployed bundle does not hold the value -- the build never got it")
print(f"  in        {served[0].relative_to(paths.serve)} (what a browser downloads)")

tracked = subprocess.run(["git", "-C", str(root), "grep", "-l", secret, "HEAD"],
                         capture_output=True, text=True)
if tracked.stdout.strip():
    sys.exit(f"the value is committed in {tracked.stdout.strip()}")
print("  not in    any committed file")

if secret.encode() in Path(paths.db).read_bytes():
    sys.exit("the value is in the journal database")
print("  not in    applace.db")

log = paths.app_logs("release-notes") / "deploy.log"
if secret in log.read_text(errors="ignore"):
    sys.exit("the value is in the deploy log")
print(f"  not in    {log.name}")
PY

step "applace stop takes both of them down"
uv run applace stop release-notes
uv run applace deployments release-notes -n 3

printf '\n\033[32mM7 ACCEPTANCE: PASS\033[0m\n'
