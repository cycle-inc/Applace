"""The register: the developer's view, one level above a home (M16, D27).

The questions here are the ones asked by the person who runs the machine rather
than by anyone living on it: what is on this box, who does it belong to, what
was allowed to happen, and what did it cost. Two properties matter more than the
shapes of the answers -- the register must never write to anybody's home, and it
must never disagree with what that home says about itself.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.requests import Request
from starlette.testclient import TestClient
from typer.testing import CliRunner

from applace import Applace, Machine, gitrepo, panel, register
from applace.apps import stop_deployments
from applace.cli import app as cli
from applace.db import connect
from applace.machine import MachineError, folder

from conftest import FAKE_STACK

runner = CliRunner()

# The fake stack builds nothing, which is all most of these tests need. The one
# that deploys needs a `dist` directory to exist, so it writes one.
BUILD = "sh -c 'mkdir -p out && echo hi > out/index.html'"
# A real child on a real port, the same trick test_preview and test_machine use.
DEV_SERVER = f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    monkeypatch.setenv("APPLACE_HOME", str(tmp_path / "never-used"))
    place = tmp_path / "srv"
    place.mkdir()
    return place


def stocked(
    ap: Applace, *, build: str | None = None, dev: str | None = None
) -> Applace:
    stack = ap.paths.stacks / "fake"
    (stack / "template" / "src").mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = yaml.safe_load(FAKE_STACK)
    if build is not None:
        data["commands"]["build"] = build
    if dev is not None:
        data["commands"]["dev"] = dev
    (stack / "stack.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (stack / "template" / "manifest.json").write_text(
        '{"name": "{{app_slug}}"}\n', encoding="utf-8"
    )
    (stack / "template" / "src" / "main.txt").write_text("{{app_name}}\n", encoding="utf-8")
    (stack / "template" / "_gitignore").write_text("out/\n", encoding="utf-8")
    return ap


def person(
    place: Machine, who: str, *, build: str | None = None, dev: str | None = None
) -> Applace:
    return stocked(place.user(who), build=build, dev=dev)


def made(ap: Applace, name: str) -> str:
    created = ap.create(name, stack="fake")
    assert created["ok"] is True, created
    return str(created["app"])


@pytest.fixture
def machine(root: Path) -> Machine:
    """A machine with two people on it and one app each, one of them red."""
    place = Machine(root)
    alice = person(place, "alice@example.com")
    bob = person(place, "bob@example.com")
    made(alice, "Sales Board")
    made(bob, "Bob Board")
    alice.write("sales-board", {"src/main.txt": "green\n"}, message="A green write")
    return place


# -- what is on this machine ----------------------------------------------


def test_every_app_of_every_home_is_listed_against_its_owner(machine: Machine) -> None:
    listed = machine.apps()

    assert [(entry["user"], entry["app"]) for entry in listed] == [
        ("alice@example.com", "sales-board"),
        ("bob@example.com", "bob-board"),
    ]
    assert {entry["stack"] for entry in listed} == {"fake"}
    assert all(entry["state"] == "green" for entry in listed)
    assert all(entry["home"].endswith(folder(str(entry["user"]))) for entry in listed)


def test_one_person_can_be_asked_for_on_their_own(machine: Machine) -> None:
    assert [entry["app"] for entry in machine.apps(user="bob@example.com")] == [
        "bob-board"
    ]
    # Somebody with no home at all is an empty answer, not an error: a backend
    # asking about a user who has never built anything is a normal Tuesday.
    assert machine.apps(user="nobody@example.com") == []


def test_the_register_opens_no_app_s_files(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D27: no `git`, no `stat`, no walk. The journal is the whole answer."""

    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("the register shelled out")

    for name in ("is_dirty", "current_branch", "head", "tracked_files", "status"):
        monkeypatch.setattr(gitrepo, name, refuse)

    listed = machine.apps()
    assert [entry["app"] for entry in listed] == ["sales-board", "bob-board"]
    assert listed[0]["commit"]


def test_a_home_someone_is_writing_to_is_still_readable(machine: Machine) -> None:
    """A reporting tool that has to wait for a lock is a reporting tool that hangs."""
    alice = machine.home_of("alice@example.com")
    writer = connect(alice.db)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE apps SET description = 'mid-write'")
    try:
        assert [entry["app"] for entry in machine.apps()] == [
            "sales-board",
            "bob-board",
        ]
    finally:
        writer.rollback()
        writer.close()


def test_the_register_cannot_write_even_if_it_tried(machine: Machine) -> None:
    conn = register.open_ro(machine.home_of("alice@example.com").db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM apps")
    finally:
        conn.close()


def test_a_home_with_no_journal_yet_is_not_an_error(root: Path) -> None:
    place = Machine(root)
    place.user("alice@example.com")  # connected, never built anything
    (place.home_of("alice@example.com").db).unlink(missing_ok=True)

    assert place.apps() == []
    assert place.audit() == []
    assert place.spend()["users"][0]["total"]["writes"] == 0


# -- the two views agree (D27) ---------------------------------------------


def test_the_machine_and_the_home_agree_that_an_app_is_red(machine: Machine) -> None:
    alice = machine.user("alice@example.com")
    alice.paths.policy.write_text(
        "dependencies:\n  deny: ['left-pad']\n", encoding="utf-8"
    )
    refused = alice.write(
        "sales-board", {"manifest.json": '{"dependencies": {"left-pad": "^1.0.0"}}'}
    )
    assert refused["ok"] is False

    from_home = alice.apps()["apps"][0]
    from_card = alice.cards()["apps"][0]
    from_machine = machine.apps(user="alice@example.com")[0]

    assert from_home["state"] == "red"
    assert from_card["state"] == "red"
    assert from_machine["state"] == "red"
    # And the same gate is behind all three.
    assert from_home["gate"]["stage"] == "policy"
    assert from_machine["gate"] == from_home["gate"]


def test_a_home_s_listing_carries_what_only_a_working_tree_knows(
    machine: Machine,
) -> None:
    alice = machine.user("alice@example.com")
    (Path(alice.app("sales-board")["path"]) / "src" / "main.txt").write_text(
        "edited by a person\n", encoding="utf-8"
    )

    mine = alice.apps()["apps"][0]
    theirs = machine.apps(user="alice@example.com")[0]

    assert mine["dirty"] is True
    assert mine["branch"]
    # The register does not answer that question, and does not pretend to.
    assert "dirty" not in theirs
    assert mine["state"] == theirs["state"] == "green"


def test_where_an_app_is_exposed_is_the_same_answer_in_both(root: Path) -> None:
    machine = Machine(root)
    alice = person(machine, "alice@example.com", dev=DEV_SERVER)
    made(alice, "Sales Board")
    started = alice.preview("sales-board")
    try:
        assert started["ok"] is True, started
        exposed = machine.apps(user="alice@example.com")[0]["exposed"]
        assert [entry["kind"] for entry in exposed] == ["preview"]
        assert exposed[0]["url"] == started["url"]
    finally:
        alice.stop_preview("sales-board")


# -- the audit -------------------------------------------------------------


def test_the_audit_says_who_created_what_and_when(machine: Machine) -> None:
    events = machine.audit()

    created = [event for event in events if event["kind"] == "create"]
    assert {(event["user"], event["app"]) for event in created} == {
        ("alice@example.com", "sales-board"),
        ("bob@example.com", "bob-board"),
    }
    assert all(event["stack"] == "fake" for event in created)
    # Newest first, so the top of the list is what just happened.
    assert [event["at"] for event in events] == sorted(
        (event["at"] for event in events), reverse=True
    )


def test_the_audit_holds_the_production_deploy_and_the_human_who_allowed_it(
    root: Path,
) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com", build=BUILD)
    made(alice, "Sales Board")
    shipped = alice.deploy(
        "sales-board", target="local", environment="production", confirm=True
    )
    try:
        assert shipped["ok"] is True, shipped
        deploys = [e for e in place.audit() if e["kind"] == "deploy"]
        assert len(deploys) == 1
        assert deploys[0]["environment"] == "production"
        assert deploys[0]["confirmed"] is True
        assert deploys[0]["status"] == "live"
        assert deploys[0]["url"] == shipped["url"]
        assert deploys[0]["user"] == "alice@example.com"
    finally:
        conn = connect(alice.paths.db)
        try:
            stop_deployments(alice.paths, conn, "sales-board")
        finally:
            conn.close()


def test_the_audit_names_the_upstreams_an_app_may_call_and_no_value(
    machine: Machine,
) -> None:
    alice = machine.user("alice@example.com")
    alice.declare_api(
        "sales-board",
        name="crm",
        base_url="https://crm.internal/api",
        token_env="CRM_TOKEN",
        paths=["customers/**"],
        methods=["GET"],
    )
    alice.set_env("sales-board", "CRM_TOKEN", "sk-live-do-not-print-me")

    events = [event for event in machine.audit() if event["kind"] == "api"]
    assert events[0]["name"] == "crm"
    assert events[0]["token_env"] == "CRM_TOKEN"
    assert events[0]["methods"] == ["GET"]
    # The whole export, not just that row: a value has never been in a journal
    # and must not arrive in one by way of a report (D8, D24).
    assert "sk-live-do-not-print-me" not in json.dumps(machine.audit())


def test_the_audit_says_what_a_policy_refused(machine: Machine) -> None:
    alice = machine.user("alice@example.com")
    alice.paths.policy.write_text(
        "dependencies:\n  deny: ['left-pad']\n", encoding="utf-8"
    )
    alice.write(
        "sales-board", {"manifest.json": '{"dependencies": {"left-pad": "^1.0.0"}}'}
    )

    refused = [event for event in machine.audit() if event["kind"] == "refused"]
    assert len(refused) == 1
    assert refused[0]["stage"] == "policy"
    assert "left-pad" in " ".join(refused[0]["why"])


def test_an_app_that_was_taken_over_says_so_in_the_audit(
    machine: Machine, tmp_path: Path
) -> None:
    outside = tmp_path / "legacy" / "customer-board"
    (outside / "src").mkdir(parents=True)
    (outside / "manifest.json").write_text(
        '{"dependencies": {"fakedep": "^1.0.0"}}', encoding="utf-8"
    )
    (outside / "src" / "main.txt").write_text("theirs\n", encoding="utf-8")
    gitrepo.init(outside)
    gitrepo.commit_all(outside, "Work that predates Applace")

    alice = machine.user("alice@example.com")
    assert alice.take(str(outside), check=False)["ok"] is True

    taken = [event for event in machine.audit() if event["kind"] == "take"]
    assert taken[0]["app"] == "customer-board"
    assert taken[0]["from"] == str(outside)
    assert "create" not in {
        event["kind"] for event in machine.audit() if event["app"] == "customer-board"
    }


def test_the_audit_narrows_by_user_and_by_date(machine: Machine) -> None:
    only = machine.audit(user="bob@example.com")
    assert {event["user"] for event in only} == {"bob@example.com"}

    assert machine.audit(since="2999-01-01") == []
    assert machine.audit(until="1999-12-31") == []
    # A bare date as `--until` means the whole of that day, not its midnight.
    today = machine.audit()[0]["at"][:10]
    assert machine.audit(until=today)

    assert {event["kind"] for event in machine.audit(kinds=("create",))} == {"create"}
    with pytest.raises(MachineError):
        machine.audit(kinds=("mischief",))


def test_the_audit_limit_is_the_last_n_of_the_machine(machine: Machine) -> None:
    everything = machine.audit()
    assert machine.audit(limit=1) == everything[:1]


# -- what it cost ----------------------------------------------------------


def test_spend_agrees_with_the_ledger_to_the_unit(machine: Machine) -> None:
    alice = machine.user("alice@example.com")
    alice.write("sales-board", {"src/main.txt": "two\n"})
    alice.write("sales-board", {"src/main.txt": "three\n"})

    report = machine.spend()
    hers = next(home for home in report["users"] if home["user"] == "alice@example.com")

    ledger = connect(machine.ledger)
    try:
        counted = ledger.execute(
            "SELECT COUNT(*) AS n FROM spend WHERE home = ? AND act = 'write'",
            (str(alice.paths.home),),
        ).fetchone()["n"]
    finally:
        ledger.close()
    assert hers["window"]["write"] == counted == 3


def test_spend_counts_each_app_from_the_journal(machine: Machine) -> None:
    alice = machine.user("alice@example.com")
    alice.paths.policy.write_text(
        "dependencies:\n  deny: ['left-pad']\n", encoding="utf-8"
    )
    alice.write(
        "sales-board", {"manifest.json": '{"dependencies": {"left-pad": "^1.0.0"}}'}
    )

    hers = next(
        home
        for home in machine.spend(user="alice@example.com")["users"]
        if home["user"] == "alice@example.com"
    )
    app = hers["apps"][0]
    assert app["app"] == "sales-board"
    assert app["writes"] == 2
    assert (app["green"], app["red"]) == (1, 1)
    assert hers["total"]["writes"] == 2
    # The window and the total answer different questions and are not added.
    assert hers["window"]["write"] == 2
    assert machine.spend()["total"]["red"] == 1


# -- the panel at the root -------------------------------------------------


def client(place: Machine, visible: panel.Visible | None = None) -> TestClient:
    return TestClient(panel.machine_app(place, visible=visible))


def test_the_root_panel_lists_every_home_and_its_apps(machine: Machine) -> None:
    with client(machine) as http:
        people = http.get("/panel/users").json()
        assert [entry["user"] for entry in people["users"]] == [
            "alice@example.com",
            "bob@example.com",
        ]
        assert people["users"][0]["apps"] == 1

        every = http.get("/panel/apps").json()["apps"]
        assert [entry["app"] for entry in every] == ["sales-board", "bob-board"]
        assert every[0]["key"] == folder("alice@example.com")

        page = http.get("/panel")
        assert page.status_code == 200
        assert "/panel/users" in page.text


def test_a_card_is_reachable_by_user_and_slug(machine: Machine) -> None:
    key = folder("alice@example.com")
    with client(machine) as http:
        card = http.get(f"/panel/users/{key}/apps/sales-board").json()
        assert card["ok"] is True
        assert card["user"] == "alice@example.com"
        assert card["state"] == "green"
        assert card["headline"]

        listed = http.get(f"/panel/users/{key}/apps").json()
        assert [entry["app"] for entry in listed["apps"]] == ["sales-board"]


def test_a_home_the_callable_refuses_is_a_404_and_not_a_403(machine: Machine) -> None:
    """D27: a 403 confirms the person exists, which is a staff directory."""

    def only_alice(request: Request, user: str) -> bool:
        return user == "alice@example.com"

    alice, bob = folder("alice@example.com"), folder("bob@example.com")
    with client(machine, visible=only_alice) as http:
        assert http.get(f"/panel/users/{alice}/apps/sales-board").status_code == 200
        hidden = http.get(f"/panel/users/{bob}/apps/bob-board")
        invented = http.get("/panel/users/nobody-0000000000/apps/bob-board")
        assert hidden.status_code == 404
        assert hidden.json() == invented.json()

        assert [u["user"] for u in http.get("/panel/users").json()["users"]] == [
            "alice@example.com"
        ]
        assert [a["app"] for a in http.get("/panel/apps").json()["apps"]] == [
            "sales-board"
        ]
        assert http.get(f"/panel/users/{bob}/apps").status_code == 404
        assert http.get(f"/panel/users/{bob}/apps/bob-board/shot.png").status_code == 404


def test_a_screenshot_is_fetched_from_where_the_card_was_read(
    machine: Machine,
) -> None:
    key = folder("alice@example.com")
    shot = machine.home_of("alice@example.com").shot("sales-board")
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    with client(machine) as http:
        card = http.get(f"/panel/users/{key}/apps/sales-board").json()
        assert card["shot"] == f"/panel/users/{key}/apps/sales-board/shot.png"
        assert http.get(card["shot"]).content == b"\x89PNG\r\n\x1a\n"


def test_the_embedded_element_can_be_pointed_at_one_person(machine: Machine) -> None:
    with client(machine) as http:
        script = http.get("/panel/embed.js").text
    assert "panel/users/" in script
    assert "'user'" in script


# -- the command line ------------------------------------------------------


def test_machine_apps_prints_and_exports(root: Path, machine: Machine) -> None:
    said = runner.invoke(cli, ["machine", "apps", "--root", str(root)])
    assert said.exit_code == 0, said.output
    assert "alice@example.com" in said.output
    assert "sales-board" in said.output

    exported = runner.invoke(cli, ["machine", "apps", "--root", str(root), "--json"])
    payload = json.loads(exported.output)
    assert [entry["app"] for entry in payload["apps"]] == ["sales-board", "bob-board"]

    narrowed = runner.invoke(
        cli, ["machine", "apps", "--root", str(root), "-u", "bob@example.com", "--json"]
    )
    assert [entry["app"] for entry in json.loads(narrowed.output)["apps"]] == [
        "bob-board"
    ]


def test_machine_audit_prints_and_exports(root: Path, machine: Machine) -> None:
    said = runner.invoke(cli, ["machine", "audit", "--root", str(root)])
    assert said.exit_code == 0, said.output
    assert "create" in said.output
    assert "created from the fake stack" in said.output

    exported = runner.invoke(
        cli, ["machine", "audit", "--root", str(root), "-k", "create", "--json"]
    )
    events = json.loads(exported.output)["events"]
    assert {event["kind"] for event in events} == {"create"}

    wrong = runner.invoke(cli, ["machine", "audit", "--root", str(root), "-k", "oops"])
    assert wrong.exit_code == 1
    assert "Known kinds" in wrong.output


def test_machine_spend_prints_and_exports(root: Path, machine: Machine) -> None:
    said = runner.invoke(cli, ["machine", "spend", "--root", str(root)])
    assert said.exit_code == 0, said.output
    assert "alice@example.com" in said.output
    assert "1 writes" in said.output

    exported = runner.invoke(cli, ["machine", "spend", "--root", str(root), "--json"])
    payload = json.loads(exported.output)
    assert payload["total"]["writes"] == 1
    assert payload["users"][0]["window"]["write"] == 1


def test_a_developer_can_list_their_own_apps_as_json(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same fields, in the home the developer is actually standing in."""
    monkeypatch.setenv("APPLACE_HOME", str(machine.home_of("alice@example.com").home))
    said = runner.invoke(cli, ["ls", "--json"])
    assert said.exit_code == 0, said.output
    listed = json.loads(said.output)["apps"]
    assert listed[0]["app"] == "sales-board"
    assert listed[0]["state"] == "green"
    assert listed[0]["dirty"] is False

    plain = runner.invoke(cli, ["ls"])
    assert "sales-board" in plain.output
    assert "green" in plain.output
