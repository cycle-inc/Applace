#!/usr/bin/env bash
#
# M1 acceptance:
#   applace init && applace new "Team Dashboard"
#     -> an ordinary git repository, installed and committed
#     -> which a developer can run by hand, with no Applace involved
#
# Runs in a throwaway Applace home. Needs node, npm and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME
APP_DIR="$APPLACE_HOME/apps/team-dashboard"
DEV_PID=""

cleanup() {
  if [ -n "$DEV_PID" ]; then
    # The whole group, not the pid: `npm run dev` is a shell that spawns node,
    # and killing the parent leaves vite holding the port. M3's supervisor has
    # the same problem and will need the same answer.
    kill -- "-$DEV_PID" 2>/dev/null || kill "$DEV_PID" 2>/dev/null || true
    # Reap it before the shell does, or bash prints "Terminated" after the
    # PASS line and makes a green run look like a failed one.
    wait "$DEV_PID" 2>/dev/null || true
  fi
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

step "applace init"
uv run applace init

step "applace stacks"
uv run applace stacks

step "applace new \"Team Dashboard\""
uv run applace new "Team Dashboard"

step "applace ls"
uv run applace ls

step "What is on disk"
git -C "$APP_DIR" ls-files | sed "s|^|  |"

step "The repository, as git sees it"
git -C "$APP_DIR" log --oneline
git -C "$APP_DIR" status --short --branch

[ -d "$APP_DIR/node_modules" ] || fail "dependencies were not installed"
[ "$(git -C "$APP_DIR" rev-list --count HEAD)" = "1" ] || fail "expected exactly one commit"
[ -z "$(git -C "$APP_DIR" status --porcelain)" ] || fail "the first commit left the tree dirty"
git -C "$APP_DIR" ls-files | grep -q '^package-lock.json$' || fail "the lockfile was not committed"
if git -C "$APP_DIR" ls-files | grep -q '^node_modules/'; then fail "node_modules was committed"; fi
grep -q 'Team Dashboard' "$APP_DIR/index.html" || fail "the app's name never reached index.html"

step "By hand, with no Applace: npm run typecheck && npm run lint && npm run build"
(cd "$APP_DIR" && npm run typecheck && npm run lint && npm run build)
[ -f "$APP_DIR/dist/index.html" ] || fail "the build produced no dist/index.html"
[ -z "$(git -C "$APP_DIR" status --porcelain)" ] || fail "the build dirtied the repository"

# --host is not optional here: left to itself Vite binds `localhost`, which on
# a machine with IPv6 is ::1 and not 127.0.0.1. The stack's own `dev` command
# pins the interface for the same reason, and M3's supervisor will rely on it.
step "By hand, with no Applace: npm run dev"
# `set -m` puts the job in its own process group, which is what makes the
# group kill in cleanup() possible.
set -m
(cd "$APP_DIR" && npm run dev -- --port 5199 --strictPort --host 127.0.0.1 \
  >/tmp/applace-dev.log 2>&1) &
DEV_PID=$!
for _ in $(seq 1 40); do
  sleep 0.5
  if curl -fsS http://127.0.0.1:5199/ >/tmp/applace-dev.html 2>/dev/null; then break; fi
done
grep -q 'Team Dashboard' /tmp/applace-dev.html || {
  cat /tmp/applace-dev.log
  fail "the dev server did not serve the app"
}
printf '  served: %s\n' "$(grep -o '<title>[^<]*</title>' /tmp/applace-dev.html)"

step "The MCP surface an agent sees"
uv run python - <<'PY'
import asyncio, json, os
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))
tools = asyncio.run(server.list_tools())
print("tools:", ", ".join(sorted(t.name for t in tools)))
result = asyncio.run(server.call_tool("get_app", {"app": "team-dashboard"}))
detail = result.structured_content or json.loads(result.content[0].text)
print(json.dumps({k: detail[k] for k in
                  ("app", "stack", "commit", "dirty", "entry", "installed")}, indent=2))
assert detail["ok"] and not detail["dirty"], detail
PY

printf '\n\033[32mM1 ACCEPTANCE: PASS\033[0m\n'
