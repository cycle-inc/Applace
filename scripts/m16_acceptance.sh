#!/usr/bin/env bash
#
# M16 acceptance -- the register (D27):
#   three homes under one root, holding apps in three different conditions --
#   one green and live in production, one red because a policy refused it, and
#   one adopted from a repository that lives outside its home entirely;
#   `machine apps` lists every one against the right owner while shelling out to
#   nothing and taking no write lock -- it reads a home in the middle of a write
#   and answers anyway, and the handle it reads with cannot write;
#   `audit` holds the production deploy and the human confirmation that
#   permitted it, the upstream the app may call, and the write the policy
#   refused -- and no secret's value anywhere in any of it;
#   `spend` agrees with the ledger to the unit;
#   the root panel lists the homes the callable allows and 404s the one it does
#   not, with the same answer it gives for a person who does not exist;
#   and every one of those commands parses as JSON from the CLI.
#
# Everything here is real: real npm install, real vite build, real git, a real
# deploy, a real HTTP server for the panel. No model is called, so this costs
# nothing but time.
#
# Needs node, npm, git and a network.

set -euo pipefail

cd "$(dirname "$0")/.."

WORK="$(mktemp -d)"
export WORK
APPLACE_ROOT="$WORK/srv"
export APPLACE_ROOT

cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

exec uv run python - <<'PY'
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from applace import Applace, Machine, panel, register
from applace.apps import stop_deployments
from applace.db import connect
from applace.init_cmd import run_init
from applace.machine import folder, spent
from applace.paths import ApplacePaths

BOLD, RED, GREEN, OFF = "\033[1m", "\033[31m", "\033[32m", "\033[0m"
# Resolved: `mktemp -d` hands back /var/..., and a take-over records the
# checkout at its real path. Comparing the two unresolved would be a false alarm.
WORK = Path(os.environ["WORK"]).resolve()
ROOT = Path(os.environ["APPLACE_ROOT"])
FACTORY = WORK / "factory"
LEGACY = WORK / "legacy" / "legacy-board"
TRIPWIRE = WORK / "shelled-out"
ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"
NOBODY = "nobody@example.com"
# The value of a credential, set the way a human sets one. It lives in a file
# at 0600 outside every repository; the point of this script is that it is in
# none of the answers below.
SECRET = "sk-live-m16-must-never-be-exported"


def step(title):
    print(f"\n{BOLD}== {title}{OFF}")


def fail(why):
    sys.exit(f"{RED}FAIL: {why}{OFF}")


def get(url):
    """Status and body. A server that is not up yet is a 0, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=10) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError):
        return 0, b""


def json_of(url):
    status, body = get(url)
    if status != 200:
        fail(f"{url} answered {status}")
    return json.loads(body)


def cli(*args, home=None):
    """Run the real command line, and parse what `--json` printed."""
    env = {**os.environ}
    if home is not None:
        env["APPLACE_HOME"] = str(home)
        env.pop("APPLACE_ROOT", None)
    done = subprocess.run(
        ["uv", "run", "applace", *args], capture_output=True, text=True, env=env
    )
    if done.returncode != 0:
        fail(f"`applace {' '.join(args)}` exited {done.returncode}:\n{done.stderr}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        fail(f"`applace {' '.join(args)} --json` printed something else:\n{done.stdout[:400]}")


# -- a machine with three people on it --------------------------------------

step("This machine can build apps at all")
report = run_init(ApplacePaths(home=WORK / "check"))
if not report.ready:
    fail(f"this machine cannot build apps: {report.error or report.missing}")
print("   " + ", ".join(f"{r.name} {r.detail}" for r in report.requirements))

machine = Machine(ROOT)
built = {}
broke = []


def run(who, work):
    try:
        work()
    except BaseException:
        broke.append(f"{who}: {traceback.format_exc()}")


def alice_builds():
    """Green, deployed to production by a human, and calling one upstream."""
    ap = machine.user(ALICE)
    made = ap.create("Sales Board", description="What the sales team looks at")
    if not made.get("ok"):
        fail(f"alice create: {made}")
    written = ap.write(
        "sales-board",
        {"src/App.tsx": (
            "export default function App() {\n"
            "  return <main className=\"p-8\">Sales</main>;\n"
            "}\n"
        )},
        message="Say what it is",
    )
    if not written.get("ok"):
        fail(f"alice write: {written}")
    declared = ap.declare_api(
        "sales-board",
        "billing",
        base_url="https://billing.example.com",
        token_env="BILLING_TOKEN",
        paths=["invoices/*"],
        methods=["GET"],
    )
    if not declared.get("ok"):
        fail(f"alice declare_api: {declared}")
    stored = ap.set_env("sales-board", "BILLING_TOKEN", SECRET)
    if not stored.get("ok"):
        fail(f"alice set_env: {stored}")
    shipped = ap.deploy(
        "sales-board", target="local", environment="production", confirm=True
    )
    if not shipped.get("ok"):
        fail(f"alice deploy: {shipped}")
    built["alice"] = (ap, shipped)


def bob_builds():
    """Red: a policy his company wrote refused what the agent tried to add."""
    ap = machine.user(BOB)
    made = ap.create("Ops Notes", description="On call notes")
    if not made.get("ok"):
        fail(f"bob create: {made}")
    ap.paths.policy.write_text(
        "dependencies:\n  deny: ['left-pad']\n", encoding="utf-8"
    )
    manifest = json.loads(
        (ap.paths.app("ops-notes") / "package.json").read_text(encoding="utf-8")
    )
    manifest.setdefault("dependencies", {})["left-pad"] = "^1.3.0"
    refused = ap.write(
        "ops-notes",
        {"package.json": json.dumps(manifest, indent=2) + "\n"},
        message="Add a dependency the company does not allow",
    )
    if refused.get("ok"):
        fail("a denied dependency passed the gate")
    if refused.get("stage") != "policy":
        fail(f"bob's write failed at {refused.get('stage')}, not the policy")
    built["bob"] = (ap, refused)


def factory_builds():
    """A front end that exists before anybody adopts it (D26)."""
    if not run_init(ApplacePaths(home=FACTORY)).ready:
        fail("the factory home is not ready")
    made = Applace(FACTORY).create("Legacy Board", description="Not built here")
    if not made.get("ok"):
        fail(f"factory create: {made}")
    LEGACY.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(made["path"], LEGACY)
    # The factory goes entirely: what is left is somebody's repository, and
    # nothing on this machine knows anything about it.
    shutil.rmtree(FACTORY, ignore_errors=True)


step("Three homes, three different conditions, built at once")
threads = [
    threading.Thread(target=run, args=(name, work))
    for name, work in (
        ("alice", alice_builds), ("bob", bob_builds), ("factory", factory_builds)
    )
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
if broke:
    fail("\n".join(broke))

alice, shipped = built["alice"]
bob, _ = built["bob"]
carol = machine.user(CAROL)
taken = carol.take(str(LEGACY), check=False)
if not taken.get("ok"):
    fail(f"carol take: {taken}")
if taken["path"] != str(LEGACY):
    fail(f"the take-over moved somebody's repository to {taken['path']}")
print(f"   {ALICE}: sales-board, green, live at {shipped['url']}")
print(f"   {BOB}: ops-notes, red at the policy stage")
print(f"   {CAROL}: {taken['app']}, adopted from {LEGACY}")


# -- what is on this machine ------------------------------------------------

step("`machine apps` lists every app against its owner, and shells out to nothing")
# A `git` that cannot run and leaves a note if anybody calls it. The register
# says it opens no app's files; this is that claim made falsifiable.
fake = WORK / "no-shell"
fake.mkdir(exist_ok=True)
for name in ("git", "npm", "node"):
    (fake / name).write_text(
        f'#!/bin/sh\necho "{name}" >> "{TRIPWIRE}"\n'
        f'echo "the register ran {name}" >&2\nexit 127\n',
        encoding="utf-8",
    )
    (fake / name).chmod(0o755)
was = os.environ["PATH"]
os.environ["PATH"] = f"{fake}{os.pathsep}{was}"
try:
    listed = machine.apps()
    audit = machine.audit()
    costs = machine.spend()
finally:
    os.environ["PATH"] = was
if TRIPWIRE.exists():
    fail(f"the register shelled out: {TRIPWIRE.read_text().split()}")

owners = {(entry["user"], entry["app"]): entry for entry in listed}
if set(owners) != {
    (ALICE, "sales-board"), (BOB, "ops-notes"), (CAROL, "legacy-board")
}:
    fail(f"the register lists {sorted(owners)}")
if owners[(ALICE, "sales-board")]["state"] != "green":
    fail(f"alice's app is {owners[(ALICE, 'sales-board')]['state']}")
if owners[(BOB, "ops-notes")]["state"] != "red":
    fail(f"bob's app is {owners[(BOB, 'ops-notes')]['state']}")
if owners[(BOB, "ops-notes")]["gate"]["stage"] != "policy":
    fail(f"bob's app is red for the wrong reason: {owners[(BOB, 'ops-notes')]['gate']}")
if owners[(CAROL, "legacy-board")]["taken_from"] != str(LEGACY):
    fail(f"carol's app does not say where it came from: {owners[(CAROL, 'legacy-board')]}")
if str(carol.paths.home) in owners[(CAROL, "legacy-board")]["path"]:
    fail("the adopted repository was moved into the home after all")
live = [
    where for where in owners[(ALICE, "sales-board")]["exposed"]
    if where["environment"] == "production"
]
if not live or live[0]["url"] != shipped["url"]:
    fail(f"the register does not know where alice's app is serving: {live}")
if "dirty" in owners[(ALICE, "sales-board")]:
    fail("the register answered a question only a working tree can answer")
print(f"   {len(listed)} apps, {len(machine.homes())} homes, no git and no stat")

# The home's own listing answers the other half, and the two agree about state.
from_home = alice.apps()["apps"][0]
if from_home["state"] != owners[(ALICE, "sales-board")]["state"]:
    fail("the home and the machine disagree about the state of the same app")
if "dirty" not in from_home or "branch" not in from_home:
    fail(f"the home's own listing lost the working tree: {from_home}")
print(f"   the home adds dirty={from_home['dirty']} branch={from_home['branch']}")


step("It reads a home in the middle of a write, and cannot write to one")
busy = sqlite3.connect(alice.paths.db, timeout=10.0)
busy.execute("BEGIN IMMEDIATE")  # alice's agent, holding the write lock
try:
    began = time.monotonic()
    while_busy = machine.apps(user=ALICE)
    took = time.monotonic() - began
finally:
    busy.rollback()
    busy.close()
if len(while_busy) != 1 or took > 3.0:
    fail(f"reading a busy home took {took:.1f}s and gave {len(while_busy)} apps")
print(f"   alice's home read in {took * 1000:.0f}ms while her own agent held the lock")

readonly = register.open_ro(alice.paths.db)
try:
    readonly.execute("DELETE FROM apps")
except sqlite3.OperationalError as exc:
    print(f"   and a write through that handle: {exc}")
else:
    fail("the register can write to somebody's journal")
finally:
    readonly.close()


# -- what was allowed to happen ---------------------------------------------

step("The audit holds the deploy, the human who allowed it, and no secret")
kinds = {}
for event in audit:
    kinds.setdefault(event["kind"], []).append(event)
if sorted(kinds) != ["api", "create", "deploy", "refused", "take"]:
    fail(f"the audit holds {sorted(kinds)}")
if [event["at"] for event in audit] != sorted(
    (event["at"] for event in audit), reverse=True
):
    fail("the audit is not newest first")

deploys = kinds["deploy"]
if len(deploys) != 1:
    fail(f"{len(deploys)} deploys in the audit")
if deploys[0]["environment"] != "production" or deploys[0]["confirmed"] is not True:
    fail(f"the production deploy does not carry its confirmation: {deploys[0]}")
if deploys[0]["user"] != ALICE or deploys[0]["url"] != shipped["url"]:
    fail(f"the deploy is recorded against the wrong home: {deploys[0]}")
print(f"   deploy  {deploys[0]['environment']} by {deploys[0]['user']}, confirmed by a human")

refused = kinds["refused"]
if len(refused) != 1 or refused[0]["stage"] != "policy" or refused[0]["user"] != BOB:
    fail(f"the refusal is not in the audit: {refused}")
if "left-pad" not in " ".join(refused[0]["why"]):
    fail(f"the refusal does not say what was refused: {refused[0]}")
print(f"   refused {refused[0]['stage']}: {refused[0]['why'][0][:70]}")

upstream = kinds["api"][0]
if upstream["token_env"] != "BILLING_TOKEN" or upstream["methods"] != ["GET"]:
    fail(f"the upstream is wrong in the audit: {upstream}")
print(f"   api     {upstream['name']} → {upstream['base_url']}, credential from "
      f"${upstream['token_env']}")

adopted = kinds["take"][0]
if adopted["user"] != CAROL or adopted["from"] != str(LEGACY):
    fail(f"the take-over is wrong in the audit: {adopted}")
if len(kinds["create"]) != 2:
    fail(f"{len(kinds['create'])} creates for two apps built here")
print(f"   take    {adopted['app']} adopted from {adopted['from']}")

# The whole export, every view, not just the row that mentions the variable.
env_file = alice.paths.env_file("sales-board")
if SECRET not in env_file.read_text(encoding="utf-8"):
    fail("the secret was never really set, so this proves nothing")
everything = json.dumps([audit, listed, costs, alice.env("sales-board")])
if SECRET in everything:
    fail("a secret's value reached the register")
print(f"   the value is in {env_file} at {oct(env_file.stat().st_mode)[-3:]}, and in "
      f"none of {len(everything)} bytes of answers")

narrowed = machine.audit(user=BOB)
if {event["user"] for event in narrowed} != {BOB}:
    fail(f"--user did not narrow the audit: {narrowed}")
only = machine.audit(kinds=["deploy"], limit=1)
if len(only) != 1 or only[0]["kind"] != "deploy":
    fail(f"--kind and --limit: {only}")
today = time.strftime("%Y-%m-%d", time.gmtime())
if len(machine.audit(until=today)) != len(audit):
    fail("`--until today` dropped events that happened today")
print(f"   --user, --kind, --limit and --until {today} all narrow it")


# -- what it cost -----------------------------------------------------------

step("`spend` agrees with the ledger to the unit")
for home in costs["users"]:
    paths = machine.home_of(home["user"])
    for act in ("write", "deploy", "shot"):
        ledger = spent(paths, act, seconds=3600.0)
        if home["window"][act] != ledger:
            fail(f"{home['user']} {act}: register says {home['window'][act]}, "
                 f"the ledger says {ledger}")
by_user = {home["user"]: home for home in costs["users"]}
if by_user[ALICE]["total"]["production_deploys"] != 1:
    fail(f"alice's production deploy is not counted: {by_user[ALICE]['total']}")
if by_user[BOB]["total"]["red"] != 1 or by_user[BOB]["total"]["green"] != 0:
    fail(f"bob's refused write is not counted red: {by_user[BOB]['total']}")
if by_user[CAROL]["total"]["writes"] != 0:
    fail(f"carol has written nothing: {by_user[CAROL]['total']}")
if [app["app"] for app in by_user[ALICE]["apps"]] != ["sales-board"]:
    fail(f"spend is not per app: {by_user[ALICE]['apps']}")
if costs["total"]["writes"] != sum(
    home["total"]["writes"] for home in costs["users"]
):
    fail("the machine total is not the sum of the homes")
for home in costs["users"]:
    print(f"   {home['user']:<20} window {home['window']['write']} writes, "
          f"total {home['total']['writes']} ({home['total']['red']} red), "
          f"{home['disk_mb']} MB")


# -- the panel a developer browses ------------------------------------------

step("The root panel shows the homes the callable allows, and 404s the one it does not")


def visible(request, user):
    """What a company's backend does after reading its own session (D11)."""
    return user != CAROL


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    PORT = probe.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"


def serve():
    try:
        panel.serve_machine(machine, port=PORT, visible=visible)
    except BaseException:
        broke.append(traceback.format_exc())


threading.Thread(target=serve, daemon=True).start()
for _ in range(60):
    if broke:
        fail("\n".join(broke))
    if get(f"{BASE}/panel")[0] == 200:
        break
    time.sleep(0.5)
else:
    fail("the root panel never came up")

status, page = get(f"{BASE}/panel")
if b"panel/embed.js" not in page:
    fail("the root page does not use the element it tells others to use")
people = json_of(f"{BASE}/panel/users")["users"]
if {person["user"] for person in people} != {ALICE, BOB}:
    fail(f"the panel lists {[person['user'] for person in people]}")
if {person["key"] for person in people} != {folder(ALICE), folder(BOB)}:
    fail(f"the panel hands out no linkable key: {people}")
seen = json_of(f"{BASE}/panel/apps")["apps"]
if {entry["app"] for entry in seen} != {"sales-board", "ops-notes"}:
    fail(f"the index shows {[entry['app'] for entry in seen]}")
print(f"   {len(people)} homes, {len(seen)} apps, and carol in neither")

cards = json_of(f"{BASE}/panel/users/{folder(ALICE)}/apps")
card = cards["apps"][0]
if card["state"] != "green" or card["app"] != "sales-board":
    fail(f"the card disagrees with the register: {card}")
one = json_of(f"{BASE}/panel/users/{folder(ALICE)}/apps/sales-board")
if one["shot"] and not str(one["shot"]).startswith(f"/panel/users/{folder(ALICE)}/"):
    fail(f"a card served from the root points at the wrong panel: {one['shot']}")
if SECRET in json.dumps([cards, one]):
    fail("a secret's value reached the panel")
print(f"   {ALICE} → {card['app']} ({card['state']}) at "
      f"/panel/users/{folder(ALICE)}/apps/{card['app']}")

hidden = get(f"{BASE}/panel/users/{folder(CAROL)}/apps")
invented = get(f"{BASE}/panel/users/{folder(NOBODY)}/apps")
if hidden[0] != 404 or invented[0] != 404:
    fail(f"a refused home answered {hidden[0]} and an invented one {invented[0]}")
if hidden[1] != invented[1]:
    fail(f"a refused home is distinguishable from one that does not exist:\n"
         f"{hidden[1]!r}\n{invented[1]!r}")
if get(f"{BASE}/panel/users/{folder(CAROL)}/apps/legacy-board")[0] != 404:
    fail("a refused home's app is reachable by name")
print(f"   carol: {hidden[0]} {hidden[1].decode()} — byte-identical to a person "
      f"who does not exist")


# -- the same answers from a terminal ---------------------------------------

step("The command line says the same thing, and `--json` parses")
from_cli = cli("machine", "apps", "--json")["apps"]
if {(entry["user"], entry["app"]) for entry in from_cli} != set(owners):
    fail(f"`applace machine apps --json` shows {from_cli}")
events = cli("machine", "audit", "--json", "--limit", "0")["events"]
if len(events) != len(audit):
    fail(f"the CLI audit has {len(events)} events, the API {len(audit)}")
if SECRET in json.dumps(events):
    fail("a secret's value reached the command line")
money = cli("machine", "spend", "--json")
if money["total"]["production_deploys"] != 1:
    fail(f"`machine spend --json`: {money['total']}")
narrow = cli("machine", "audit", "--json", "--user", BOB, "-k", "refused")["events"]
if len(narrow) != 1 or narrow[0]["stage"] != "policy":
    fail(f"`machine audit -k refused --user`: {narrow}")
print(f"   machine apps ({len(from_cli)}), audit ({len(events)}), spend "
      f"({money['total']['writes']} writes) — all JSON")

mine = cli("ls", "--json", home=alice.paths.home)["apps"]
if [entry["app"] for entry in mine] != ["sales-board"]:
    fail(f"`applace ls --json` in alice's home: {mine}")
if "dirty" not in mine[0]:
    fail("a home's own listing should still answer for its working tree")
if mine[0]["state"] != owners[(ALICE, "sales-board")]["state"]:
    fail("`applace ls` and `applace machine apps` disagree")
print(f"   applace ls --json in one home: {mine[0]['app']} {mine[0]['state']}, "
      f"dirty={mine[0]['dirty']}")

conn = connect(alice.paths.db)
try:
    stop_deployments(alice.paths, conn, "sales-board")
finally:
    conn.close()

print(f"\n{GREEN}{BOLD}M16 accepted.{OFF} The person who runs the machine can see "
      f"all of it, change none of it, and read no secret.\n")
PY
