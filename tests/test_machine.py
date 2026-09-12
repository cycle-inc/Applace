"""Many people, one machine (M12, D20 to D23).

The questions here are the ones a backend asks on its first busy afternoon: do
two people's homes stay apart, can two dev servers be handed the same port, what
happens when somebody asks for more than they may have, and does the thing that
tidies up ever delete somebody's code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from applace import Applace, Limits, Machine
from applace import machine as machine_mod
from applace.cli import app as cli
from applace.machine import MachineError
from applace.paths import ApplacePaths

from conftest import FAKE_STACK

# The same trick test_preview uses: a real long-lived child on a real port,
# cheap enough to start inside a test.
DEV_SERVER = f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine root, with git pinned so `create` behaves the same everywhere."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    # Nothing here should ever fall back to the real home; if a path is wrong we
    # want a test failure, not a write into ~/.applace.
    monkeypatch.setenv("APPLACE_HOME", str(tmp_path / "never-used"))
    place = tmp_path / "srv"
    place.mkdir()
    return place


def stocked(ap: Applace, *, dev: str | None = None) -> Applace:
    """Install the no-op stack into that person's home."""
    stack = ap.paths.stacks / "fake"
    (stack / "template" / "src").mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = yaml.safe_load(FAKE_STACK)
    if dev is not None:
        data["commands"]["dev"] = dev
    (stack / "stack.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (stack / "template" / "manifest.json").write_text(
        '{"name": "{{app_slug}}"}\n', encoding="utf-8"
    )
    (stack / "template" / "src" / "main.txt").write_text(
        "{{app_name}}: {{app_description}}\n", encoding="utf-8"
    )
    (stack / "template" / "_gitignore").write_text("out/\n", encoding="utf-8")
    return ap


def person(place: Machine, who: str, *, dev: str | None = None) -> Applace:
    return stocked(place.user(who), dev=dev)


def made(ap: Applace, name: str = "Team Dashboard") -> str:
    created = ap.create(name, stack="fake")
    assert created["ok"] is True, created
    return str(created["app"])


# -- one home each (D20) ---------------------------------------------------


def test_two_people_have_two_homes_and_neither_can_see_the_other(root: Path) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com")
    bob = person(place, "bob@example.com")

    assert alice.paths.home != bob.paths.home
    slug = made(alice, "Sales Board")
    made(bob, "Bob Board")

    assert [a["app"] for a in alice.apps()["apps"]] == [slug]
    assert [a["app"] for a in bob.apps()["apps"]] == ["bob-board"]
    # Not "she cannot read his": there is nothing there to read.
    assert bob.app(slug)["code"] == "unknown-app"


def test_a_secret_lands_in_one_home_and_exists_nowhere_else(root: Path) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com")
    bob = person(place, "bob@example.com")
    slug = made(alice)
    made(bob)

    alice.declare_env(slug, "TEST_API_KEY")
    alice.set_env(slug, "TEST_API_KEY", "sk-alice-only")

    assert "sk-alice-only" in alice.paths.env_file(slug).read_text(encoding="utf-8")
    assert not (bob.paths.env_file(slug)).exists()
    for found in bob.paths.home.rglob("*.env"):
        assert "sk-alice-only" not in found.read_text(encoding="utf-8")


def test_the_same_person_twice_is_the_same_home(root: Path) -> None:
    place = Machine(root)
    first = person(place, "alice@example.com")
    slug = made(first)
    again = place.user("alice@example.com")

    assert again.paths.home == first.paths.home
    assert [a["app"] for a in again.apps()["apps"]] == [slug]
    assert [u["user"] for u in place.users()] == ["alice@example.com"]


def test_two_ids_that_look_alike_are_not_the_same_home(root: Path) -> None:
    """`a@b.com` and `a-b-com` both flatten to the same readable name."""
    place = Machine(root)
    assert place.home_of("a@b.com").home != place.home_of("a-b-com").home


def test_a_hostile_id_stays_under_the_root(root: Path) -> None:
    place = Machine(root)
    for hostile in ("../../etc", "/etc/passwd", "..", "a/../../b"):
        home = place.home_of(hostile).home
        assert home.parent == root / "users", home
        assert root in home.parents


def test_an_empty_id_is_refused(root: Path) -> None:
    with pytest.raises(MachineError):
        Machine(root).user("   ")


# -- the ports (D21) -------------------------------------------------------


def test_the_ledger_hands_two_homes_different_ports_for_the_same_app(
    root: Path,
) -> None:
    place = Machine(root)
    alice, bob = place.home_of("alice"), place.home_of("bob")
    always_free = [5180, 5181, 5182]

    first = machine_mod.claim(
        alice, always_free, slug="board", kind="preview", free=lambda _: True
    )
    second = machine_mod.claim(
        bob, always_free, slug="board", kind="preview", free=lambda _: True
    )
    assert first is not None and second is not None and first != second
    assert {hold["port"] for hold in place.ports()} == {first, second}


def test_a_machine_with_no_free_port_says_so_rather_than_guessing(root: Path) -> None:
    place = Machine(root)
    paths = place.home_of("alice")
    assert (
        machine_mod.claim(
            paths, [5180], slug="one", kind="preview", free=lambda _: True
        )
        == 5180
    )
    assert (
        machine_mod.claim(
            paths, [5180], slug="two", kind="preview", free=lambda _: True
        )
        is None
    )


def test_a_claim_whose_process_is_gone_is_reaped(root: Path) -> None:
    place = Machine(root)
    paths = place.home_of("alice")
    port = machine_mod.claim(
        paths, [5180], slug="board", kind="preview", free=lambda _: True
    )
    assert port is not None

    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    machine_mod.started(paths, port, done.pid)

    # Nobody releases anything: the truth about a port is the process holding it.
    assert place.ports() == []
    assert (
        machine_mod.claim(
            place.home_of("bob"), [5180], slug="other", kind="preview",
            free=lambda _: True,
        )
        == 5180
    )


def test_a_claim_nobody_ever_started_is_let_go(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = Machine(root)
    paths = place.home_of("alice")
    assert machine_mod.claim(
        paths, [5180], slug="board", kind="preview", free=lambda _: True
    )
    assert len(place.ports()) == 1  # still within its grace

    monkeypatch.setattr(machine_mod, "UNSTARTED_GRACE", -1.0)
    assert place.ports() == []


def test_a_single_user_machine_has_nobody_to_arbitrate_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Applace()` alone keeps the behaviour it had before there was a root (D11)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    paths = ApplacePaths(tmp_path / "home")
    assert paths.ledger is None
    assert machine_mod.claim(
        paths, [5180], slug="x", kind="preview", free=lambda _: True
    ) is None
    assert machine_mod.spent(paths, "write") == 0
    machine_mod.spend(paths, "write")  # a no-op, not a crash
    assert machine_mod.spent(paths, "write") == 0


# -- the limits (D22) ------------------------------------------------------


def test_a_quota_refuses_before_the_work_rather_than_after_it(root: Path) -> None:
    place = Machine(root, limits=Limits(apps=1))
    alice = person(place, "alice@example.com")
    made(alice, "First App")

    refused = alice.create("Second App", stack="fake")
    assert refused["ok"] is False
    assert refused["code"] == "quota"
    assert refused["limit"] == 1 and refused["used"] == 1
    assert not (alice.paths.apps / "second-app").exists()

    # Somebody else's limit is their own.
    bob = person(place, "bob@example.com")
    assert bob.create("First App", stack="fake")["ok"] is True


def test_a_rate_limit_says_when_to_come_back(root: Path) -> None:
    place = Machine(root, limits=Limits(writes_per_hour=1))
    alice = person(place, "alice@example.com")
    slug = made(alice)

    assert alice.write(slug, {"src/a.txt": "one\n"})["ok"] is True
    refused = alice.write(slug, {"src/b.txt": "two\n"})
    assert refused["ok"] is False
    assert refused["code"] == "rate-limit"
    assert refused["window"] == "hour"
    assert 0 < refused["retry_after"] <= 3601
    # Refused, not silently dropped: the file was never written.
    assert not (alice.paths.app(slug) / "src" / "b.txt").exists()


def test_a_limit_is_an_answer_and_never_an_exception(root: Path) -> None:
    place = Machine(root, limits=Limits(apps=0, writes_per_hour=0))
    alice = person(place, "alice@example.com")
    answer = alice.create("Anything", stack="fake")
    assert answer["ok"] is False and answer["code"] == "quota"
    assert "hint" in answer


def test_limits_come_from_the_file_and_a_broken_file_is_an_error(root: Path) -> None:
    config = root / "machine.yaml"
    assert Machine(root).limits.apps == Limits().apps  # absent is the defaults

    config.write_text("limits:\n  apps: 3\n", encoding="utf-8")
    assert Machine(root).limits.apps == 3

    config.write_text("limits:\n  apps: [1, 2\n", encoding="utf-8")
    with pytest.raises(MachineError):
        Machine(root).limits

    config.write_text("limits:\n  aps: 3\n", encoding="utf-8")
    with pytest.raises(MachineError) as typo:
        Machine(root).limits
    assert "aps" in str(typo.value)

    config.write_text("limits:\n  apps: -1\n", encoding="utf-8")
    with pytest.raises(MachineError):
        Machine(root).limits


def test_an_operator_editing_the_file_does_not_restart_anything(root: Path) -> None:
    place = Machine(root)
    assert place.limits.apps == 20
    (root / "machine.yaml").write_text("limits:\n  apps: 1\n", encoding="utf-8")
    assert place.limits.apps == 1


# -- everybody at once -----------------------------------------------------


def test_everybody_arriving_at_the_same_instant_still_gets_a_home(root: Path) -> None:
    """Opening the ledger is itself a write, and twelve callers do it at once."""
    place = Machine(root)
    people = [f"person-{index}@example.com" for index in range(12)]
    broke: list[BaseException] = []
    barrier = threading.Barrier(len(people))

    def arrive(who: str) -> None:
        try:
            barrier.wait(timeout=30)
            place.user(who)
        except BaseException as exc:  # a thread that dies must fail the test
            broke.append(exc)

    threads = [threading.Thread(target=arrive, args=(who,)) for who in people]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert broke == [], broke
    assert {record["user"] for record in place.users()} == set(people)


def test_many_people_building_at_the_same_time_do_not_trip_over_each_other(
    root: Path,
) -> None:
    place = Machine(root)
    people = [f"person-{index}@example.com" for index in range(6)]
    answers: dict[str, Any] = {}
    barrier = threading.Barrier(len(people))

    def build(who: str) -> None:
        try:
            ap = person(place, who)
            barrier.wait(timeout=30)  # everybody hits SQLite in the same instant
            answers[who] = ap.create("Team Dashboard", stack="fake")
        except BaseException as exc:
            answers[who] = {"ok": False, "error": repr(exc)}

    threads = [threading.Thread(target=build, args=(who,)) for who in people]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert set(answers) == set(people), answers
    assert all(answer["ok"] is True for answer in answers.values()), answers
    homes = {str(place.home_of(who).home) for who in people}
    assert len(homes) == len(people)
    assert {record["user"] for record in place.users()} == set(people)
    assert all(record["apps"] == 1 for record in place.users())


def test_what_a_home_is_using_is_counted_from_the_home_itself(root: Path) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com")
    made(alice)
    alice.write("team-dashboard", {"src/a.txt": "hello\n"})

    usage = place.usage("alice@example.com")
    assert usage["ok"] is True
    assert usage["apps"] == 1 and usage["previews"] == 0
    assert usage["spent"]["write"] >= 0
    assert usage["limits"]["apps"] == 20

    unknown = place.usage("nobody@example.com")
    assert unknown["apps"] == 0 and unknown["disk_mb"] == 0


# -- collection (D23) ------------------------------------------------------


def test_collection_stops_an_idle_preview_and_deletes_nobody_s_code(
    root: Path,
) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com", dev=DEV_SERVER)
    slug = made(alice)
    running = alice.preview(slug)
    assert running["ok"] is True, running
    port = int(str(running["url"]).rsplit(":", 1)[1].rstrip("/"))
    assert [hold["port"] for hold in place.ports()] == [port]

    try:
        report = place.gc(preview_hours=0.0)
    finally:
        alice.stop_preview(slug)

    assert report["previews_stopped"] == [{"user": "alice@example.com", "app": slug}]
    assert place.ports() == []
    # The whole point: nothing of theirs is gone.
    assert (alice.paths.app(slug) / ".git").is_dir()
    assert [a["app"] for a in alice.apps()["apps"]] == [slug]
    assert alice.paths.home.is_dir()


def test_collection_leaves_a_preview_somebody_is_still_watching(root: Path) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com", dev=DEV_SERVER)
    slug = made(alice)
    assert alice.preview(slug)["ok"] is True
    try:
        report = place.gc(preview_hours=2.0)
        assert report["previews_stopped"] == []
        assert len(place.ports()) == 1
    finally:
        alice.stop_preview(slug)


def test_collection_removes_scratch_and_forgets_stale_spend(root: Path) -> None:
    place = Machine(root)
    alice = person(place, "alice@example.com")
    old = alice.paths.work / "team-dashboard-abc123"
    old.mkdir(parents=True)
    (old / "file.txt").write_text("a checkout nobody finished\n", encoding="utf-8")
    two_days_ago = time.time() - 48 * 3600
    os.utime(old, (two_days_ago, two_days_ago))

    report = place.gc(scratch_hours=24.0)
    assert report["scratch_removed"] == 1
    assert not old.exists()
    assert alice.paths.work.is_dir()


def test_collection_on_an_empty_root_does_nothing_loudly(root: Path) -> None:
    report = Machine(root).gc()
    assert report["ok"] is True
    assert report["previews_stopped"] == [] and report["ports_released"] == 0


# -- the operator's terminal -----------------------------------------------


runner = CliRunner()


def test_the_cli_shows_who_is_here_and_what_they_hold(root: Path) -> None:
    place = Machine(root)
    made(person(place, "alice@example.com"))

    listed = runner.invoke(cli, ["machine", "users", "--root", str(root)])
    assert listed.exit_code == 0, listed.output
    assert "alice@example.com" in listed.output
    assert "1 apps" in listed.output

    ports = runner.invoke(cli, ["machine", "ports", "--root", str(root)])
    assert ports.exit_code == 0
    assert "No port is held" in ports.output

    collected = runner.invoke(cli, ["machine", "gc", "--root", str(root)])
    assert collected.exit_code == 0
    assert "0 previews stopped" in collected.output


def test_the_cli_refuses_a_root_that_is_not_there(tmp_path: Path) -> None:
    missing = runner.invoke(cli, ["machine", "users", "--root", str(tmp_path / "no")])
    assert missing.exit_code == 1
    assert "No machine root" in missing.output


def test_the_cli_reports_a_broken_config_instead_of_ignoring_it(root: Path) -> None:
    (root / "machine.yaml").write_text("limits:\n  nope: 1\n", encoding="utf-8")
    broken = runner.invoke(cli, ["machine", "users", "--root", str(root)])
    assert broken.exit_code == 1
    assert "nope" in broken.output
