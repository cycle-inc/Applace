#!/usr/bin/env bash
#
# M6 acceptance:
#   a dependency the policy denies is refused before npm ever runs;
#   a secret the human supplies reaches the real build and reaches nothing
#   the agent can read;
#   and a mid-sized local model builds a correct page on its first attempt.
#
# Runs in a throwaway Applace home. Needs node, npm, a network, and a chromium
# from `playwright install chromium`. The last step needs ollama with a model;
# without it the step says so and the rest of the run still stands.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_HOME="$(mktemp -d)/.applace"
export APPLACE_HOME

cleanup() {
  uv run applace stop env-probe >/dev/null 2>&1 || true
  uv run applace stop release-notes >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }
skip() { printf '\033[33mSKIPPED: %s\033[0m\n' "$1"; }

export SECRET="https://api.acme.internal/v3"

step "applace init"
uv run applace init >/dev/null
printf '  %s\n' "$APPLACE_HOME"

step "The skill says what this machine is, not just what to do"
uv run applace skill --brief
uv run applace skill --full | head -1 | grep -q '^---$' || fail "SKILL.md did not come out whole"

step "A policy this company wrote"
cat > "$APPLACE_HOME/policy.yaml" <<'YAML'
dependencies:
  deny:
    - left-pad
    - "@acme/legacy-ui"
exposure:
  public_repositories: deny
YAML
uv run applace policy

step "applace new \"Env Probe\""
uv run applace new "Env Probe" >/dev/null
printf '  installed and committed\n'

step "A denied dependency is refused before npm install could run it"
uv run python - <<'PY'
import asyncio, json, sys, time
from pathlib import Path
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

paths = ApplacePaths(applace_home())
server = build_server(paths)


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


root = paths.app("env-probe")
manifest = json.loads((root / "package.json").read_text())
manifest["dependencies"]["left-pad"] = "^1.3.0"

before = time.monotonic()
report = call("write_files", app="env-probe",
              files={"package.json": json.dumps(manifest, indent=2) + "\n"},
              message="Add left-pad")
took = time.monotonic() - before

if report["ok"]:
    sys.exit("the policy let a denied dependency through")
if report["stage"] != "policy":
    sys.exit(f"refused at the wrong stage: {report['stage']}")
if (root / "node_modules" / "left-pad").exists():
    sys.exit("npm installed it anyway -- the refusal came too late to matter")
if [s["stage"] for s in report["stages"]] != ["policy"]:
    sys.exit(f"stages after the refusal ran: {report['stages']}")

print(f"  stage     {report['stage']}, in {took:.2f}s -- no install, no postinstall script")
print(f"  refused   {report['refused_deps'][0]['name']} ({report['refused_deps'][0]['reason']})")
print(f"  told to   {report['errors'][0]['message']}")

# And the manifest the agent wrote is still on disk for it to undo.
if "left-pad" not in (root / "package.json").read_text():
    sys.exit("the refusal reverted the agent's file behind its back")
print("  the file is still there, dirty, for the agent to fix")
PY

step "The agent puts the manifest back"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

paths = ApplacePaths(applace_home())
server = build_server(paths)
root = paths.app("env-probe")

manifest = json.loads((root / "package.json").read_text())
del manifest["dependencies"]["left-pad"]
result = asyncio.run(server.call_tool("write_files", {
    "app": "env-probe",
    "files": {"package.json": json.dumps(manifest, indent=2) + "\n"},
}))
report = result.structured_content
if not report["ok"]:
    sys.exit(f"the app did not come back green: {report}")
print(f"  green again at {report['stage']}")
PY

step "The agent declares a variable and never learns its value (D8)"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

server = build_server(ApplacePaths(applace_home()))


def call(tool, **kwargs):
    result = asyncio.run(server.call_tool(tool, kwargs))
    return result.structured_content or json.loads(result.content[0].text)


declared = call("set_env", app="env-probe", name="VITE_API_BASE_URL",
                description="Where the internal API lives")
assert declared["ok"], declared
if "value" in declared:
    sys.exit("set_env handed back a value")
print(f"  declared  {declared['name']} (exposed={declared['exposed']}, set={declared['set']})")
print(f"  says      {declared['instruction']}")

detail = call("get_app", app="env-probe")
if detail["env_missing"] != ["VITE_API_BASE_URL"]:
    sys.exit(f"the app does not know what it is missing: {detail.get('env_missing')}")
print(f"  get_app   env_missing={detail['env_missing']}")
PY

step "The human supplies it"
uv run applace env set env-probe VITE_API_BASE_URL --value "$SECRET"
uv run applace env ls env-probe

step "The agent uses the name, and the real build inlines the value"
uv run python - <<'PY'
import asyncio, json, sys
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

paths = ApplacePaths(applace_home())
server = build_server(paths)

PAGE = """\
const base = import.meta.env.VITE_API_BASE_URL

export default function App() {
  return (
    <main className="p-8">
      <h1 className="text-2xl font-semibold">Env Probe</h1>
      <p>The API is at {base ?? 'nowhere'}</p>
    </main>
  )
}
"""

result = asyncio.run(server.call_tool("write_files", {
    "app": "env-probe",
    "files": {"src/App.tsx": PAGE},
    "message": "Read the API base from the environment",
}))
report = result.structured_content
if not report["ok"]:
    sys.exit(f"the write was red at {report['stage']}: {report['errors']}")
print(f"  green, committed {report['commit'][:12]}")

# Everything the agent just read, in one string.
seen = json.dumps(report) + json.dumps(
    asyncio.run(server.call_tool("get_app", {"app": "env-probe"})).structured_content
) + json.dumps(
    asyncio.run(server.call_tool("read_files", {
        "app": "env-probe", "paths": ["src/App.tsx", "package.json"]
    })).structured_content
) + json.dumps(
    asyncio.run(server.call_tool("get_skill", {})).structured_content
)
(paths.home / "agent-saw.json").write_text(seen)
print(f"  {len(seen)} characters of tool results written out for the grep below")
PY

step "The value is in the bundle and nowhere the agent looks"
uv run python - <<'PY'
import os, sqlite3, subprocess, sys
from pathlib import Path
from applace.paths import ApplacePaths, applace_home

secret = os.environ["SECRET"]
paths = ApplacePaths(applace_home())
root = paths.app("env-probe")

bundles = [p for p in (root / "dist").rglob("*.js") if secret in p.read_text(errors="ignore")]
if not bundles:
    sys.exit("the build did not inline the value -- the dev server would differ from it")
print(f"  in        {bundles[0].relative_to(root)}")

seen = (paths.home / "agent-saw.json").read_text()
if secret in seen:
    sys.exit("the value came back through a tool result")
print("  not in    any tool result the agent read")

tracked = subprocess.run(["git", "-C", str(root), "grep", "-l", secret, "HEAD"],
                         capture_output=True, text=True)
if tracked.stdout.strip():
    sys.exit(f"the value is committed in {tracked.stdout.strip()}")
print("  not in    any committed file (and dist/ is gitignored)")

blob = Path(paths.db).read_bytes()
if secret.encode() in blob:
    sys.exit("the value is in the journal database")
print("  not in    applace.db, which only holds the name")

store = paths.env_file("env-probe")
mode = oct(store.stat().st_mode & 0o777)
print(f"  lives in  {store} ({mode}, in a {oct(store.parent.stat().st_mode & 0o777)} directory)")
if mode != "0o600":
    sys.exit("the value file is readable by somebody else")
PY

step "The dev server gets it too, and the page renders"
uv run python - <<'PY'
import asyncio, json, os, sys, urllib.request
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

secret = os.environ["SECRET"]
server = build_server(ApplacePaths(applace_home()))

running = asyncio.run(server.call_tool("start_preview", {"app": "env-probe"})).structured_content
assert running["ok"], running
url = running["url"].rstrip("/") + "/src/App.tsx"
with urllib.request.urlopen(url, timeout=30) as response:
    served = response.read().decode()
if secret not in served:
    sys.exit(f"the dev server is serving a module without the value: {url}")
print(f"  served    {url} with the value already substituted")

result = asyncio.run(server.call_tool("screenshot_app", {"app": "env-probe"}))
report = json.loads(result.content[1].text)
if report["blank"] or report["errors"]:
    sys.exit(f"the page is unhappy: {report}")
print(f"  rendered  {report['title']!r}, blank=False, console quiet")
PY
uv run applace stop env-probe >/dev/null

step "A mid-sized local model, with SKILL.md and nothing else"
MODEL="${APPLACE_ACCEPTANCE_MODEL:-qwen3:8b}"
if ! curl -fsS -m 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  skip "ollama is not answering on 11434; run \`ollama serve\` and \`ollama pull $MODEL\`"
elif ! curl -fsS -m 3 http://127.0.0.1:11434/api/tags | grep -q "\"$MODEL\""; then
  skip "ollama has no $MODEL; \`ollama pull $MODEL\`"
else
  uv run scripts/m6_local_model.py --model "$MODEL" || fail "$MODEL could not build the page"
fi

printf '\n\033[32mM6 ACCEPTANCE: PASS\033[0m\n'
