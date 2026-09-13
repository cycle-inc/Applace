#!/usr/bin/env bash
#
# M15 acceptance -- the take-over (D26):
#   a real front end that lives outside Applace, with commits of its own and a
#   hand-written file nobody's harness produced, is taken over;
#   the stack is recognised from its manifest, and a tree no stack recognises is
#   refused with what was looked for rather than built with a guess;
#   `git log`, `git status` and `git ls-files` are byte-identical afterwards, and
#   not one file was added to the tree -- the app is a row, nothing else;
#   the first gate runs and commits nothing, a preview serves it, and then an
#   ordinary write lands as a commit on top of the history it already had;
#   and a dry run says all of that while recording nothing at all.
#
# Everything here is real: real npm install, real vite build, real browser, real
# git, real HTTP. No model is called, so this costs nothing but time.
#
# Needs node, npm, git, a network, and `playwright install chromium`.

set -euo pipefail

cd "$(dirname "$0")/.."

WORK="$(mktemp -d)"
export WORK
APPLACE_HOME="$WORK/.applace"
export APPLACE_HOME

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
import time
import urllib.error
import urllib.request
from pathlib import Path

from applace import Applace, eyes, gitrepo
from applace.init_cmd import run_init
from applace.paths import ApplacePaths

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
# Resolved, because `mktemp -d` hands back /var/... and the take-over records the
# checkout at its real path: comparing the two unresolved would be a false alarm.
WORK = Path(os.environ["WORK"]).resolve()
HOME = Path(os.environ["APPLACE_HOME"])
FACTORY = WORK / ".applace-factory"
LEGACY = WORK / "legacy" / "customer-board"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def git(*args, cwd):
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


def facts(root):
    """What git says about a repository, to compare before with after."""
    return {
        name: git(*args, cwd=root)
        for name, args in {
            "log": ("log", "--format=%H %s"),
            "status": ("status", "--porcelain"),
            "files": ("ls-files",),
            "config": ("config", "--local", "--list"),
        }.items()
    }


# -- a front end that was written before Applace existed --------------------

step("A real front end living outside Applace, with a history of its own")
report = run_init(ApplacePaths(home=FACTORY))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")
if not eyes.available():
    fail("no chromium: run `uv sync --extra eyes` then `playwright install chromium`")

factory = Applace(FACTORY)
made = factory.create("Customer Board", description="Written by a team, not by Applace")
if not made.get("ok"):
    fail(f"create: {made}")
LEGACY.parent.mkdir(parents=True, exist_ok=True)
shutil.move(made["path"], LEGACY)
# The factory goes away entirely: what is left on disk is somebody's repository
# and nothing knows anything about it.
shutil.rmtree(FACTORY, ignore_errors=True)

(LEGACY / "src" / "App.tsx").write_text(
    """\
const teams = ['Checkout', 'Billing', 'Search']

export default function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold">Customer Board</h1>
      <ul className="mt-4 space-y-2" data-testid="teams">
        {teams.map((team) => (
          <li key={team} className="rounded bg-white p-3">{team}</li>
        ))}
      </ul>
    </main>
  )
}
""",
    encoding="utf-8",
)
(LEGACY / "NOTES.md").write_text("Owned by the platform team. No harness here.\n", encoding="utf-8")
git("add", "-A", cwd=LEGACY)
git("commit", "--no-verify", "-m", "The board the team actually wrote", cwd=LEGACY)
BEFORE = facts(LEGACY)
HEAD_BEFORE = gitrepo.head(LEGACY).sha
print(f"   {LEGACY} — {gitrepo.count_commits(LEGACY)} commits, no Applace anywhere")


# -- a home that has never seen it ------------------------------------------

step("A fresh home takes it over: recognised, not guessed")
if not run_init(ApplacePaths(home=HOME)).ready:
    fail("the second home is not ready")
ap = Applace(HOME)

seen = ap.take(str(LEGACY), dry_run=True)
if not seen.get("ok"):
    fail(f"dry run: {seen}")
if seen["stack"] != "vite-react-ts" or seen["recognised_by"] != ["vite", "react"]:
    fail(f"the dry run recognised {seen['stack']} by {seen['recognised_by']}")
if seen["taken"] or ap.apps()["apps"]:
    fail("a dry run recorded something")
print(f"   dry run: {seen['stack']} by {' + '.join(seen['recognised_by'])}, nothing recorded")

taken = ap.take(str(LEGACY))
if not taken.get("ok"):
    fail(f"take: {taken}")
slug = taken["app"]
if taken["path"] != str(LEGACY):
    fail(f"the checkout was moved to {taken['path']}")
if taken["commits"] != int(git("rev-list", "--count", "HEAD", cwd=LEGACY).strip()):
    fail("the take-over miscounted the history it adopted")
print(f"   {slug}: {taken['stack']}, {taken['commits']} commits, kept at {taken['path']}")


# -- nothing was written into the tree --------------------------------------

step("The repository is exactly as it was found (D1 + D26)")
after = facts(LEGACY)
for name, was in BEFORE.items():
    if after[name] != was:
        fail(f"git {name} changed:\n--- before\n{was}\n--- after\n{after[name]}")
if gitrepo.head(LEGACY).sha != HEAD_BEFORE:
    fail("the take-over moved HEAD")
for suspect in (".applace", ".applace.yaml", "applace.json"):
    if (LEGACY / suspect).exists():
        fail(f"the take-over wrote {suspect} into somebody's repository")
if "applace" in (LEGACY / "package.json").read_text(encoding="utf-8").lower():
    fail("the take-over edited package.json")
print("   log, status, ls-files and .git/config all byte-identical; no file added")


# -- the first gate: a report, not a verdict --------------------------------

step("The first gate runs on it, and commits nothing")
gate = taken.get("gate")
if gate is None:
    fail("the take-over ran no gate")
stages = {stage["stage"]: stage for stage in gate["stages"]}
if not gate["ok"]:
    fail(f"this app builds; the gate said {gate['stage']}: {gate['errors']}")
if gate["committed"]:
    fail("the first gate committed a tree it had just met")
if not stages["visit"]["ok"] or stages["visit"].get("skipped"):
    fail(f"the browser never opened the adopted app: {stages['visit']}")
if gitrepo.head(LEGACY).sha != HEAD_BEFORE:
    fail("the first gate added a commit")
print(f"   {' '.join(name for name in stages if stages[name]['ok'])} — and HEAD did not move")


# -- it is an app now, like any other ---------------------------------------

step("From here it is an ordinary app: listed, previewed, written to")
listed = ap.apps()["apps"]
if [entry["app"] for entry in listed] != [slug]:
    fail(f"the register does not hold it: {listed}")
if listed[0]["path"] != str(LEGACY) or listed[0]["dirty"]:
    fail(f"the listing is wrong about where it lives or how it is: {listed[0]}")

live = ap.preview(slug)
if not live.get("ok"):
    fail(f"preview: {live}")
served = ""
for _ in range(60):
    try:
        with urllib.request.urlopen(live["url"], timeout=2) as answer:
            served = answer.read().decode("utf-8", "replace")
        break
    except (urllib.error.URLError, OSError):
        time.sleep(0.5)
if "<div id=\"root\">" not in served and "<script" not in served:
    fail(f"the dev server served nothing recognisable: {served[:200]!r}")
ap.stop_preview(slug)
print(f"   previewed at {live['url']}, and stopped again")

written = ap.write(
    slug,
    {"NOTES.md": "Owned by the platform team. Applace builds it now.\n"},
    message="Say who builds this",
)
if not written.get("ok") or not written.get("committed"):
    fail(f"a write on the adopted app did not land: {written}")
log = git("log", "--format=%H %s", cwd=LEGACY)
if HEAD_BEFORE not in log:
    fail("the write rewrote the history it was given")
if not log.startswith(written["commit"]):
    fail("the write is not the newest commit")
print(f"   {written['commit'][:12]} sits on top of the {taken['commits']} commits it found")


# -- what is refused, and why -----------------------------------------------

step("A tree no stack recognises is refused with what was looked for")
other = WORK / "server-thing"
(other / "src").mkdir(parents=True)
(other / "package.json").write_text(
    json.dumps({"name": "server-thing", "dependencies": {"express": "^4", "pg": "^8"}}),
    encoding="utf-8",
)
subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
git("add", "-A", cwd=other)
git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "api", cwd=other)

refused = ap.take(str(other))
if refused.get("ok") or refused.get("code") != "take-refused":
    fail(f"a backend was taken over as a front end: {refused}")
if "vite-react-ts looks for vite + react" not in refused["error"]:
    fail(f"the refusal does not say what was looked for: {refused['error']}")
if "express" not in refused["error"]:
    fail(f"the refusal does not say what it found: {refused['error']}")
print(f"   {refused['error'][:110]}")

loose = WORK / "no-git"
(loose).mkdir()
(loose / "package.json").write_text('{"dependencies": {"vite": "5", "react": "19"}}', encoding="utf-8")
refused = ap.take(str(loose))
if refused.get("ok") or "git init" not in refused["error"]:
    fail(f"a directory with no history was taken over: {refused}")
print(f"   {refused['error'][:110]}")

if [entry["app"] for entry in ap.apps()["apps"]] != [slug]:
    fail("a refused take-over left a row behind")

print(f"\n{GREEN}{BOLD}M15 accepted.{OFF} A front end that already existed is an app, "
      f"and its repository cannot tell.\n")
PY
