#!/usr/bin/env bash
#
# M4 acceptance:
#   an app that builds green but throws at runtime is diagnosed from the
#   console alone, with no human looking at the screen;
#   the fix is confirmed the same way.
#
# Runs in a throwaway Applace home. Needs node, npm, a network, and a chromium
# from `playwright install chromium`.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  uv run applace stop eyes-demo >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

step "applace init reports the browser"
uv run applace init | grep -A2 'Optional:' || fail "init said nothing about a browser"

step "applace new \"Eyes Demo\""
uv run applace new "Eyes Demo" >/dev/null
printf '  created\n'

step "A change that builds green but is broken at runtime"
uv run python - <<'PY'
import asyncio, json
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


BROKEN = """\
type Team = { name: string }

// Typechecks: JSON.parse is `any`, so the cast is believed. At runtime the
// object has no `teams`, and `.map` throws.
const data = JSON.parse('{}') as { teams: Team[] }

export default function App() {
  void fetch('/api/teams')
  return (
    <main className="p-8">
      <ul>
        {data.teams.map((team) => (
          <li key={team.name}>{team.name}</li>
        ))}
      </ul>
    </main>
  )
}
"""

report = call("write_files", app="eyes-demo", files={"src/App.tsx": BROKEN},
              message="List the teams")
assert report["ok"], report
assert report["committed"], report
print(f"  the gate is green and committed {report['commit'][:12]}")
PY

step "The eyes diagnose it without anyone looking at the screen"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))
result = asyncio.run(server.call_tool("screenshot_app", {"app": "eyes-demo"}))
assert not result.is_error, result.content

kinds = [content.type for content in result.content]
assert kinds == ["image", "text"], kinds
report = json.loads(result.content[1].text)
png_bytes = len(result.content[0].data)

if not report["blank"]:
    sys.exit(f"the page was expected to be blank: {report}")
if not report["errors"]:
    sys.exit(f"the console said nothing about the crash: {report}")

first = report["errors"][0]
print(f"  picture   {png_bytes} bytes of png reached the model")
print(f"  blank     {report['blank']}")
print(f"  error     {first['text']}")
print(f"  at        {first['source']}")
if "TypeError" not in first["text"]:
    sys.exit("the error was not named")
if "App.tsx" not in first["source"]:
    sys.exit("the error did not say which file")
for failed in report["failed_requests"]:
    print(f"  failed    {failed['method']} {failed['url']} -> {failed['status'] or failed['error']}")
PY

step "The fix is confirmed the same way"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


FIXED = """\
type Team = { name: string }

const teams: Team[] = [{ name: 'Platform' }, { name: 'Growth' }]

export default function App() {
  return (
    <main className="p-8">
      <h1 className="text-2xl font-semibold">Teams</h1>
      <ul>
        {teams.map((team) => (
          <li key={team.name}>{team.name}</li>
        ))}
      </ul>
    </main>
  )
}
"""

written = call("write_files", app="eyes-demo", files={"src/App.tsx": FIXED},
               message="Render the teams we have")
assert written["ok"], written

result = asyncio.run(server.call_tool("screenshot_app", {"app": "eyes-demo"}))
report = json.loads(result.content[1].text)
if report["blank"] or report["errors"]:
    sys.exit(f"the fixed page is still unhappy: {report}")
print(f"  title     {report['title']}")
print("  blank     False, and the console is quiet")
PY

step "The dev server it started is ours to stop"
uv run applace stop eyes-demo

printf '\n\033[32mM4 ACCEPTANCE: PASS\033[0m\n'
