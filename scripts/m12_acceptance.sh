#!/usr/bin/env bash
#
# M12 acceptance -- many people, one machine:
#   three people build at the same instant under one root, each in their own
#   home, with the real stack -- real npm install, real vite build, real git;
#   their dev servers run at once and no two of them get the same port;
#   a secret one of them sets exists nowhere in anybody else's home;
#   a quota and a rate limit are answers with a code, not crashes and not queues;
#   and the collector stops what nobody is watching without deleting a line of
#   anybody's code.
#
# Needs node, npm, git and a network. Takes a few minutes: the installs are real
# and they run in parallel, which is the point.

set -euo pipefail

cd "$(dirname "$0")/.."

APPLACE_ROOT="$(mktemp -d)/srv"
export APPLACE_ROOT

cleanup() {
  rm -rf "$(dirname "$APPLACE_ROOT")"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import os
import sys
import threading
import traceback
import urllib.request
from pathlib import Path

from applace import Applace, Limits, Machine
from applace.init_cmd import run_init
from applace.machine import MachineError
from applace.paths import ApplacePaths

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
ROOT = Path(os.environ["APPLACE_ROOT"])
PEOPLE = ["alice@example.com", "bob@example.com", "carol@example.com"]
SECRET = "sk-alice-not-a-real-token"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


# -- one root, one home each (D20) -----------------------------------------

step("This machine can build apps at all")
scratch = ROOT.parent / "check"
report = run_init(ApplacePaths(home=scratch))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")
print("   " + ", ".join(f"{r.name} {r.detail}" for r in report.requirements))

machine = Machine(ROOT)
print(f"   {machine!r}")

step("Three people build the same app at the same instant, each in their home")
made = {}
broke = []
barrier = threading.Barrier(len(PEOPLE))


def build(who):
    try:
        ap = machine.user(who)
        barrier.wait(60)  # everybody hits the root, the ledger and npm together
        made[who] = (ap, ap.create("Team Dashboard", description=f"{who} built this"))
    except BaseException:
        # A thread that dies quietly would leave the others waiting at the
        # barrier until somebody noticed. It says so instead.
        broke.append(f"{who}: {traceback.format_exc()}")
        barrier.abort()


threads = [threading.Thread(target=build, args=(who,)) for who in PEOPLE]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
if broke:
    fail("\n".join(broke))

for who in PEOPLE:
    if who not in made:
        fail(f"{who} never finished")
    _, created = made[who]
    if not created.get("ok"):
        fail(f"{who}: {created}")
homes = {str(ap.paths.home) for ap, _ in made.values()}
if len(homes) != len(PEOPLE):
    fail(f"{len(PEOPLE)} people, {len(homes)} homes")
slugs = {created["app"] for _, created in made.values()}
if slugs != {"team-dashboard"}:
    fail(f"the same name should give the same slug in every home: {slugs}")
for who in PEOPLE:
    ap, _ = made[who]
    if [app["app"] for app in ap.apps()["apps"]] != ["team-dashboard"]:
        fail(f"{who} sees {ap.apps()}")
print(f"   {len(PEOPLE)} homes, each with its own team-dashboard")

alice, _ = made["alice@example.com"]
bob, _ = made["bob@example.com"]

step("Nobody can reach anybody else's app")
seen = bob.app("team-dashboard")["path"]
if seen != str(bob.paths.app("team-dashboard")):
    fail(f"bob's app is at {seen}")
if str(alice.paths.home) in seen:
    fail("bob is looking at alice's home")
print(f"   bob's app is {seen}")


# -- a secret is one home's (D8 under D20) ---------------------------------

step("A secret alice sets exists nowhere in anybody else's home")
alice.declare_env("team-dashboard", "API_TOKEN", "What the app calls the API with")
alice.set_env("team-dashboard", "API_TOKEN", SECRET)
env_file = alice.paths.env_file("team-dashboard")
if SECRET not in env_file.read_text(encoding="utf-8"):
    fail("the value did not land in alice's env file")
if (env_file.stat().st_mode & 0o777) != 0o600:
    fail(f"{env_file} is mode {env_file.stat().st_mode & 0o777:o}")
for who in PEOPLE[1:]:
    other, _ = made[who]
    for found in other.paths.home.rglob("*"):
        if not found.is_file():
            continue
        try:
            if SECRET in found.read_text(encoding="utf-8", errors="ignore"):
                fail(f"{who} can read alice's secret at {found}")
        except OSError:
            continue
print(f"   {env_file} at 600, and nothing of it anywhere else")


# -- the ports are the machine's (D21) -------------------------------------

step("Three dev servers at once, and no two of them on the same port")
running = {}
barrier = threading.Barrier(len(PEOPLE))


def serve(who):
    ap, _ = made[who]
    try:
        barrier.wait(60)
        running[who] = ap.preview("team-dashboard")
    except BaseException:
        broke.append(f"{who}: {traceback.format_exc()}")
        barrier.abort()


threads = [threading.Thread(target=serve, args=(who,)) for who in PEOPLE]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

if broke:
    fail("\n".join(broke))
for who in PEOPLE:
    if not running.get(who, {}).get("ok"):
        fail(f"{who}: {running.get(who)}")
ports = [answer["port"] for answer in running.values()]
if len(set(ports)) != len(PEOPLE):
    fail(f"two homes were handed the same port: {ports}")
for who, answer in running.items():
    with urllib.request.urlopen(answer["url"], timeout=30) as page:
        if page.status != 200:
            fail(f"{who}'s preview answered {page.status}")
held = machine.ports()
if sorted(hold["port"] for hold in held) != sorted(ports):
    fail(f"the ledger says {held}")
if any(hold["pid"] is None for hold in held):
    fail("a claim was never told which process took it")
print(f"   ports {sorted(ports)}, all answering, all in the ledger")


# -- a limit is an answer (D22) --------------------------------------------

step("A quota refuses before the work, with a code a backend can branch on")
tight = Machine(ROOT, limits=Limits(apps=1, previews=1))
refused = tight.user("alice@example.com").create("Second Board")
if refused.get("ok") or refused.get("code") != "quota":
    fail(f"a quota did not bite: {refused}")
if (alice.paths.apps / "second-board").exists():
    fail("the refusal still installed something")
print(f"   {refused['code']}: {refused['error']} ({refused['hint']})")

step("A rate limit says when to come back")
slow = Machine(ROOT, limits=Limits(writes_per_hour=1)).user("bob@example.com")
first = slow.write("team-dashboard", {"src/note.txt": "one\n"}, message="One")
if not first.get("ok"):
    fail(f"the first write should pass: {first}")
again = slow.write("team-dashboard", {"src/note.txt": "two\n"}, message="Two")
if again.get("ok") or again.get("code") != "rate-limit":
    fail(f"a rate limit did not bite: {again}")
if not 0 < again["retry_after"] <= 3601:
    fail(f"retry_after is {again['retry_after']}")
if "two" in (bob.paths.app("team-dashboard") / "src" / "note.txt").read_text():
    fail("a refused write still touched the disk")
print(f"   {again['code']}: retry in {again['retry_after']}s")

step("A broken machine.yaml is an error, not a quieter limit")
(ROOT / "machine.yaml").write_text("limits:\n  aps: 1\n", encoding="utf-8")
try:
    Machine(ROOT).limits
except MachineError as exc:
    print(f"   {exc}")
else:
    fail("an unknown limit was accepted")
(ROOT / "machine.yaml").unlink()


# -- collection (D23) ------------------------------------------------------

step("The collector stops what nobody is watching")
before = {
    who: sorted(p.name for p in ap.paths.app("team-dashboard").iterdir())
    for who, (ap, _) in made.items()
}
collected = machine.gc(preview_hours=0.0)
if len(collected["previews_stopped"]) != len(PEOPLE):
    fail(f"gc stopped {collected['previews_stopped']}")
if machine.ports():
    fail(f"gc left ports held: {machine.ports()}")
print(
    f"   {len(collected['previews_stopped'])} previews stopped, "
    f"{collected['ports_released']} ports released"
)

step("And deletes nobody's code")
for who, (ap, _) in made.items():
    app_dir = ap.paths.app("team-dashboard")
    if not (app_dir / ".git").is_dir():
        fail(f"{who} lost their repository")
    if sorted(p.name for p in app_dir.iterdir()) != before[who]:
        fail(f"{who}'s app lost files")
    if [app["app"] for app in ap.apps()["apps"]] != ["team-dashboard"]:
        fail(f"{who} lost an app from their database")
if SECRET not in alice.paths.env_file("team-dashboard").read_text(encoding="utf-8"):
    fail("gc deleted a secret")
if {record["user"] for record in machine.users()} != set(PEOPLE):
    fail("gc forgot somebody")
for record in machine.users():
    if record["apps"] != 1:
        fail(f"{record['user']} has {record['apps']} apps after gc")
print("   every repository, every app and every secret still there")

step("What one person is using is theirs alone")
usage = machine.usage("alice@example.com")
if usage["apps"] != 1 or usage["disk_mb"] <= 0:
    fail(f"usage: {usage}")
print(
    f"   alice: {usage['apps']} app, {usage['disk_mb']} MB, "
    f"{usage['spent']['write']} writes this hour"
)

step("And a single-user Applace is still policed by nothing (D11)")
lone = Applace(ROOT.parent / "lone")
if lone.paths.ledger is not None or lone.limits is not None:
    fail("a plain Applace grew a warden")
print(f"   {lone!r} answers to no root")

print(f"\n{GREEN}{BOLD}M12 ACCEPTANCE: PASS{OFF}")
PY
