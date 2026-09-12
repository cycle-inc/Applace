#!/usr/bin/env bash
#
# M2 acceptance, in the milestone's own words:
#   write a type error, get the compiler's own message and line;
#   fix it, get a commit.
#
# Everything here goes through the MCP tools, because that is what an agent has.
# Runs in a throwaway Applace home. Needs node, npm and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME
APP_DIR="$APPLACE_HOME/apps/counter"

cleanup() { rm -rf "$(dirname "$APPLACE_HOME")"; }
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "applace init && applace new \"Counter\""
uv run applace init >/dev/null
uv run applace new "Counter"

BASE_COMMIT="$(git -C "$APP_DIR" rev-parse HEAD)"

step "The agent writes a type error"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


BROKEN = """\
export default function App() {
  const total: string = 41 + 1
  return <main className="p-8 text-2xl">{total}</main>
}
"""

red = call("write_files", app="counter", files={"src/App.tsx": BROKEN},
           message="Add a counter")
print(json.dumps({k: red[k] for k in ("ok", "stage", "errors", "commit", "dirty")},
                 indent=2))

assert red["ok"] is False, "a type error passed the gate"
assert red["stage"] == "typecheck", red["stage"]
assert red["commit"] is None, "a red gate committed"
assert red["dirty"] is True, "a red gate left the tree clean"

error = red["errors"][0]
assert error["file"] == "src/App.tsx", error
assert error["line"] == 2, error           # the compiler's own line, not ours
assert error["code"] == "TS2322", error
assert "not assignable" in error["message"], error

# The broken file is still on disk: the agent has to be able to see it.
seen = call("read_files", app="counter", paths=["src/App.tsx"])["files"][0]
assert "const total: string" in seen["content"], seen

# And get_app says why the app is dirty, without being asked twice.
detail = call("get_app", app="counter")
assert detail["dirty"] is True and detail["failed_stage"] == "typecheck", detail
assert detail["errors"][0]["line"] == 2, detail

print("\n\033[1m== The agent fixes it\033[0m")
FIXED = BROKEN.replace("const total: string", "const total: number")
green = call("write_files", app="counter", files={"src/App.tsx": FIXED},
             message="Add a counter")
print(json.dumps({k: green[k] for k in
                  ("ok", "stage", "written", "commit", "committed", "dirty",
                   "new_deps")}, indent=2))

assert green["ok"] is True, green
assert green["committed"] is True and green["commit"], green
assert green["dirty"] is False, green
assert green["new_deps"] == [], green
# install is skipped: the manifest did not move.
stages = {s["stage"]: s for s in green["stages"]}
assert stages["install"].get("skipped"), stages["install"]
assert stages["typecheck"]["ok"] and stages["lint"]["ok"] and stages["build"]["ok"]

print("\n\033[1m== The path lint\033[0m")
for path in ("../escape.txt", ".git/config", "package-lock.json",
             "node_modules/react/index.js"):
    refused = call("write_files", app="counter", files={path: "nope"})
    assert refused["ok"] is False and refused["stage"] == "paths", (path, refused)
    print(f"  refused {path}: {refused['errors'][0]['message']}")
PY

step "What git has, and only what git has"
git -C "$APP_DIR" log --oneline
git -C "$APP_DIR" status --short --branch

[ "$(git -C "$APP_DIR" rev-list --count HEAD)" = "2" ] || {
  echo "FAIL: expected exactly two commits -- the create and the green gate" >&2
  exit 1
}
[ "$(git -C "$APP_DIR" rev-parse HEAD~1)" = "$BASE_COMMIT" ] || {
  echo "FAIL: history was rewritten" >&2; exit 1; }
[ -z "$(git -C "$APP_DIR" status --porcelain)" ] || {
  echo "FAIL: the green gate left the tree dirty" >&2; exit 1; }
[ -z "$(git -C "$APP_DIR" ls-files -- escape.txt)" ] || {
  echo "FAIL: a refused path reached the repository" >&2; exit 1; }

step "A human reproduces the same gate: applace check"
uv run applace check counter

printf '\n\033[32mM2 ACCEPTANCE: PASS\033[0m\n'
