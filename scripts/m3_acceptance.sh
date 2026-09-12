#!/usr/bin/env bash
#
# M3 acceptance:
#   the URL returns the app's HTML;
#   restart the harness and the preview is still reachable;
#   stop it and the port comes back.
#
# Runs in a throwaway Applace home. Needs node, npm and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  uv run applace stop preview-demo >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

step "applace init && applace new \"Preview Demo\""
uv run applace init >/dev/null
uv run applace new "Preview Demo" >/dev/null
printf '  created\n'

step "An agent starts a preview and gets a URL"
URL="$(uv run python - <<'PY'
import asyncio, json
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


first = call("start_preview", app="preview-demo")
assert first["ok"] and first["ready"], first

# Idempotent: a second call is the same server, not a second one.
again = call("start_preview", app="preview-demo")
assert (again["url"], again["pid"]) == (first["url"], first["pid"]), (first, again)

print(first["url"])
PY
)"
printf '  %s\n' "$URL"

step "The URL returns the app's HTML"
curl -fsS "$URL" -o /tmp/applace-m3.html
grep -q 'Preview Demo' /tmp/applace-m3.html || fail "the preview did not serve the app"
printf '  served: %s\n' "$(grep -o '<title>[^<]*</title>' /tmp/applace-m3.html)"

step "The harness restarts; the preview is found again, not restarted"
uv run python - <<'PY'
import asyncio, json
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

# A brand-new server object over the same home: exactly what an MCP host does
# when it reconnects.
server = build_server(ApplacePaths(applace_home()))
result = asyncio.run(server.call_tool("get_app", {"app": "preview-demo"}))
detail = result.structured_content or json.loads(result.content[0].text)
live = detail["preview"]
assert live is not None, "the restarted supervisor lost the preview"
assert live["ready"], live
print(f"  still at {live['url']} (pid {live['pid']})")
PY

step "It survives a write, and the write still gates"
uv run python - <<'PY'
import asyncio, json, urllib.request
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


written = call("write_files", app="preview-demo", files={"src/App.tsx": """\
export default function App() {
  return <main className="p-8 text-2xl">Hello from the gate</main>
}
"""}, message="Say hello")
assert written["ok"], written

live = call("get_app", app="preview-demo")["preview"]
assert live is not None and live["ready"], live
with urllib.request.urlopen(live["url"], timeout=5) as response:
    assert response.status == 200
print(f"  still serving after a green write, on {live['url']}")
PY

step "Stopping gives the port back"
PORT="${URL##*:}"; PORT="${PORT%/}"
uv run applace stop preview-demo
sleep 0.5
if curl -fsS --max-time 2 "$URL" >/dev/null 2>&1; then
  fail "the preview is still answering after stop"
fi
uv run python - <<PY
import socket, sys
probe = socket.socket()
probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    probe.bind(("127.0.0.1", $PORT))
except OSError as exc:
    sys.exit(f"port $PORT is still held: {exc}")
finally:
    probe.close()
print("  port $PORT is free again")
PY

step "Stopping again is not an error"
uv run applace stop preview-demo

printf '\n\033[32mM3 ACCEPTANCE: PASS\033[0m\n'
