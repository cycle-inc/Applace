#!/usr/bin/env bash
#
# M8 acceptance:
#   a company stack is installed from git and pinned to the commit it came from;
#   an app created from it is born with the company's design system and API
#   client already in it, and builds green without a model writing a line;
#   the stack moves, and the app that predates it keeps its own files and is
#   told it predates them.
#
# Runs in a throwaway Applace home, with the example stack in examples/
# published as a real git repository in a temporary directory. Needs git, node,
# npm and a network.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"

APPLACE_HOME="$(mktemp -d)/.applace"
STACK_REPO="$(mktemp -d)/acme-stack"
export APPLACE_HOME

cleanup() {
  uv run applace stop billing-portal >/dev/null 2>&1 || true
  rm -rf "$(dirname "$APPLACE_HOME")" "$(dirname "$STACK_REPO")"
}
trap cleanup EXIT

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

step "The company publishes its stack (examples/acme-stack, as a git repository)"
cp -R "$REPO/examples/acme-stack" "$STACK_REPO"
git -C "$STACK_REPO" init -q -b main
git -C "$STACK_REPO" -c user.email=stack@acme.test -c user.name=Acme add -A
git -C "$STACK_REPO" -c user.email=stack@acme.test -c user.name=Acme commit -qm "The Acme stack"
FIRST="$(git -C "$STACK_REPO" rev-parse HEAD)"
printf '  %s at %s\n' "$STACK_REPO" "${FIRST:0:12}"

step "applace init"
uv run applace init >/dev/null

step "applace stacks add file://\$STACK_REPO"
uv run applace stacks add "file://$STACK_REPO"

step "applace stacks"
uv run applace stacks

step "It is a snapshot with a pin, not a second checkout"
uv run python - <<PY
import sys
from pathlib import Path
from applace import stackstore
from applace.paths import ApplacePaths, applace_home

root = ApplacePaths(applace_home()).stacks / "acme-web"
if (root / ".git").exists():
    sys.exit("the clone kept its own git history")
pin = stackstore.read_pin(root)
if pin is None or pin.commit != "$FIRST":
    sys.exit(f"the pin does not name the commit that was cloned: {pin}")
print(f"  pinned    {pin.commit[:12]} from {pin.source}")
print(f"  files     {len(list(root.rglob('*')))} in {root.name}/")
PY

step "applace new \"Billing Portal\" --stack acme-web (a real install and build)"
uv run applace new "Billing Portal" --stack acme-web

step "The app is born with the company's components, and they are the ones that build"
uv run python - <<PY
import json, subprocess, sys
from pathlib import Path
from applace.apps import require_app
from applace.db import connect
from applace.paths import ApplacePaths, applace_home

paths = ApplacePaths(applace_home())
conn = connect(paths.db)
row = require_app(conn, "billing-portal")
root = Path(str(row["path"]))

for expected in ("src/ui/index.tsx", "src/lib/acme.ts"):
    if not (root / expected).is_file():
        sys.exit(f"the app was not born with {expected}")
page = (root / "src" / "App.tsx").read_text()
if "from './ui'" not in page:
    sys.exit("the first page does not use the design system")
if "Billing Portal" not in page:
    sys.exit("the template was not rendered")
print("  has       src/ui/index.tsx, src/lib/acme.ts, and a page that imports them")

if str(row["stack"]) != "acme-web":
    sys.exit(f"the app records the wrong stack: {row['stack']}")
if str(row["stack_commit"]) != "$FIRST":
    sys.exit("the app does not record the stack commit it was born from")
print(f"  born from acme-web at {str(row['stack_commit'])[:12]}")

tracked = subprocess.run(["git", "-C", str(root), "ls-files"],
                         capture_output=True, text=True).stdout.split()
print(f"  committed {len(tracked)} files in its own repository")
PY

step "The gate agrees: typecheck, lint and build pass on what the stack shipped"
uv run applace check billing-portal

step "The company moves its stack on"
uv run python - <<PY
from pathlib import Path

ui = Path("$STACK_REPO/template/src/ui/index.tsx")
text = ui.read_text()
text = text.replace(
    "export function Empty(",
    "export function Toolbar({ children }: { children: ReactNode }) {\n"
    "  return <div className=\"flex gap-2\">{children}</div>\n"
    "}\n\n"
    "export function Empty(",
)
ui.write_text(text)
print("  added     <Toolbar> to the design system")
PY
git -C "$STACK_REPO" -c user.email=stack@acme.test -c user.name=Acme commit -qam "Add a Toolbar"
SECOND="$(git -C "$STACK_REPO" rev-parse HEAD)"

step "applace stacks update acme-web"
uv run applace stacks update acme-web

step "The app that predates it keeps its own files, and is told that it does"
uv run python - <<PY
import asyncio, json, sys
from pathlib import Path
from applace.apps import require_app
from applace.db import connect
from applace.paths import ApplacePaths, applace_home
from applace.server import build_server

paths = ApplacePaths(applace_home())
conn = connect(paths.db)
root = Path(str(require_app(conn, "billing-portal")["path"]))
if "Toolbar" in (root / "src" / "ui" / "index.tsx").read_text():
    sys.exit("updating the stack rewrote an app's committed files")
print("  app       still on its own copy: no Toolbar, nothing overwritten")

detail = asyncio.run(
    build_server(paths).call_tool("get_app", {"app": "billing-portal"})
).structured_content
if not detail["stack_drifted"]:
    sys.exit("the agent is not told that the stack has moved")
if detail["stack_commit"] != "$FIRST":
    sys.exit("the app forgot which commit it was born from")
print(f"  get_app   stack_drifted=True")
print(f"            {detail['stack_note']}")

installed = asyncio.run(build_server(paths).call_tool("list_stacks", {})).structured_content
acme = [s for s in installed["stacks"] if s["name"] == "acme-web"][0]
if acme["commit"] != "$SECOND":
    sys.exit("list_stacks does not report the new commit")
print(f"  stacks    acme-web is now {acme['commit'][:12]}")
PY

step "applace stacks drift"
uv run applace stacks drift

step "A new app gets the new stack, and still builds"
uv run applace new "Ops Console" --stack acme-web >/dev/null
uv run python - <<PY
import sys
from pathlib import Path
from applace.apps import require_app
from applace.db import connect
from applace.paths import ApplacePaths, applace_home

paths = ApplacePaths(applace_home())
conn = connect(paths.db)
root = Path(str(require_app(conn, "ops-console")["path"]))
if "Toolbar" not in (root / "src" / "ui" / "index.tsx").read_text():
    sys.exit("a new app did not get the newer stack")
if str(require_app(conn, "ops-console")["stack_commit"]) != "$SECOND":
    sys.exit("a new app recorded the wrong stack commit")
print(f"  ops-console born from {str(require_app(conn, 'ops-console')['stack_commit'])[:12]}, with the Toolbar")
PY
uv run applace check ops-console

step "A repository that is not a stack is refused, and the registry survives it"
refusal="$(uv run applace stacks add "file:///nowhere/at/all" 2>&1 || true)"
printf '  said      %s\n' "${refusal%%$'\n'*}"
case "$refusal" in
  *"could not clone"*) ;;
  *) fail "the refusal did not say what went wrong: $refusal" ;;
esac
listed="$(uv run applace stacks)"
case "$listed" in
  *acme-web*) printf '  stacks    still lists acme-web\n' ;;
  *) fail "a failed install took the registry with it" ;;
esac

printf '\n\033[32mM8 ACCEPTANCE: PASS\033[0m\n'
