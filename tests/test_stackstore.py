"""Company stacks: installing one from git, pinning it, and moving it.

The stack repositories here are real git repositories in `tmp_path`, cloned
over `file://`, because the thing being tested is the clone-validate-pin
sequence and a fake would only test the fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from applace import stackstore
from applace.apps import app_detail, create_app, require_app
from applace.db import Connection
from applace.paths import ApplacePaths
from applace.stacks import registry, resolve
from applace.stackstore import StackInstallError

Home = tuple[ApplacePaths, Connection]

STACK = """\
name: acme-web
title: Acme internal web app
description: The company's own stack.
runtime: none
commands:
  install: "true"
  dev: "true"
  build: "true"
manifest: manifest.json
dist: out
entry: src/App.txt
deploy:
  - local
"""


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def make_stack_repo(root: Path, *, manifest: str = STACK, subdir: str = "") -> Path:
    """A git repository holding a stack, the way a company would keep one."""
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "stack@acme.test", cwd=root)
    git("config", "user.name", "Acme", cwd=root)
    place = root / subdir if subdir else root
    (place / "template" / "src").mkdir(parents=True, exist_ok=True)
    (place / "stack.yaml").write_text(manifest, encoding="utf-8")
    (place / "template" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (place / "template" / "src" / "App.txt").write_text(
        "{{app_name}} on the Acme stack\n", encoding="utf-8"
    )
    git("add", "-A", cwd=root)
    git("commit", "-qm", "The Acme stack", cwd=root)
    return root


def move_stack(root: Path, text: str, message: str = "Move the stack on") -> str:
    (root / "template" / "src" / "App.txt").write_text(text, encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", message, cwd=root)
    return git("rev-parse", "HEAD", cwd=root).strip()


@pytest.fixture
def stack_repo(tmp_path: Path) -> Path:
    return make_stack_repo(tmp_path / "acme-stack")


def url(path: Path) -> str:
    return f"file://{path}"


# -- installing ------------------------------------------------------------


def test_a_company_stack_is_cloned_pinned_and_usable_to_create_an_app(
    home: Home, stack_repo: Path
) -> None:
    paths, conn = home
    sha = git("rev-parse", "HEAD", cwd=stack_repo).strip()

    installed = stackstore.add(paths, url(stack_repo))

    assert installed.name == "acme-web"          # the stack names itself
    assert installed.commit == sha
    assert installed.previous is None
    assert (paths.stacks / "acme-web" / "stack.yaml").is_file()

    stack = resolve(paths.stacks, "acme-web")
    assert stack.commit == sha
    assert stack.source == url(stack_repo)

    report = create_app(paths, conn, name="Billing Portal", stack_name="acme-web",
                        install=False)
    assert "Billing Portal on the Acme stack" in (
        Path(report.path) / "src" / "App.txt"
    ).read_text()
    assert str(require_app(conn, "billing-portal")["stack_commit"]) == sha


def test_the_clone_is_a_snapshot_with_a_pin_beside_it_not_a_second_checkout(
    home: Home, stack_repo: Path
) -> None:
    paths, _ = home
    stackstore.add(paths, url(stack_repo))
    root = paths.stacks / "acme-web"

    assert not (root / ".git").exists()
    pin = stackstore.read_pin(root)
    assert pin is not None
    assert pin.source == url(stack_repo)
    assert pin.added_at
    assert yaml.safe_load((root / stackstore.PIN).read_text())["commit"] == pin.commit


def test_a_stack_that_is_not_one_is_refused_and_nothing_is_installed(
    home: Home, tmp_path: Path
) -> None:
    """A broken stack in the registry breaks `list_stacks` for every app."""
    paths, _ = home
    repo = tmp_path / "not-a-stack"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "a@b.test", cwd=repo)
    git("config", "user.name", "A", cwd=repo)
    (repo / "README.md").write_text("just a repository\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "nothing here", cwd=repo)

    with pytest.raises(StackInstallError) as caught:
        stackstore.add(paths, url(repo))

    assert "stack.yaml" in str(caught.value)
    assert sorted(p.name for p in paths.stacks.iterdir()) == ["fake"]


def test_a_repository_that_does_not_exist_says_so_rather_than_raising_git(
    home: Home, tmp_path: Path
) -> None:
    paths, _ = home
    with pytest.raises(StackInstallError) as caught:
        stackstore.add(paths, url(tmp_path / "nowhere"))
    assert "could not clone" in str(caught.value)


def test_a_stack_can_live_in_a_subdirectory_of_a_bigger_repository(
    home: Home, tmp_path: Path
) -> None:
    paths, _ = home
    repo = make_stack_repo(tmp_path / "monorepo", subdir="packages/web-stack")

    installed = stackstore.add(paths, url(repo), subdir="packages/web-stack")

    assert installed.name == "acme-web"
    assert (paths.stacks / "acme-web" / "template" / "src" / "App.txt").is_file()
    pin = stackstore.read_pin(paths.stacks / "acme-web")
    assert pin is not None and pin.subdir == "packages/web-stack"


def test_the_same_name_twice_is_refused_and_names_the_command_that_is_meant(
    home: Home, stack_repo: Path
) -> None:
    paths, _ = home
    stackstore.add(paths, url(stack_repo))

    with pytest.raises(StackInstallError) as caught:
        stackstore.add(paths, url(stack_repo))

    assert "already installed" in str(caught.value)
    assert "stacks update" in str(caught.value)


def test_a_stack_can_be_installed_under_a_name_the_company_chose(
    home: Home, stack_repo: Path
) -> None:
    paths, _ = home
    stackstore.add(paths, url(stack_repo), name="acme-web-next")
    assert "acme-web-next" in registry(paths.stacks)
    assert "acme-web" not in registry(paths.stacks)


def test_a_company_stack_of_the_same_name_replaces_the_built_in_one(
    home: Home, tmp_path: Path
) -> None:
    """D7: a company that ships `fake` has decided what that name means here."""
    paths, _ = home
    repo = make_stack_repo(
        tmp_path / "theirs", manifest=STACK.replace("name: acme-web", "name: fake")
    )
    stackstore.add(paths, url(repo), force=True)

    stack = resolve(paths.stacks, "fake")
    assert stack.title == "Acme internal web app"
    assert stack.commit is not None


# -- updating --------------------------------------------------------------


def test_update_moves_the_stack_and_reports_both_commits(
    home: Home, stack_repo: Path
) -> None:
    paths, _ = home
    first = stackstore.add(paths, url(stack_repo))
    second_sha = move_stack(stack_repo, "{{app_name}} on the newer Acme stack\n")

    updated = stackstore.update(paths, "acme-web")

    assert updated.previous == first.commit
    assert updated.commit == second_sha
    assert updated.changed
    assert "newer" in (
        paths.stacks / "acme-web" / "template" / "src" / "App.txt"
    ).read_text()
    pin = stackstore.read_pin(paths.stacks / "acme-web")
    assert pin is not None
    assert pin.added_at == stackstore.read_pin(paths.stacks / "acme-web").added_at  # type: ignore[union-attr]
    assert pin.updated_at


def test_update_on_an_unmoved_stack_says_nothing_changed(
    home: Home, stack_repo: Path
) -> None:
    paths, _ = home
    stackstore.add(paths, url(stack_repo))
    assert stackstore.update(paths, "acme-web").changed is False


def test_a_built_in_stack_cannot_be_updated_because_it_ships_with_applace(
    home: Home,
) -> None:
    paths, _ = home
    with pytest.raises(StackInstallError) as caught:
        stackstore.update(paths, "fake")
    assert "built in" in str(caught.value)


def test_updating_something_that_is_not_installed_says_which_command_installs_it(
    home: Home,
) -> None:
    paths, _ = home
    with pytest.raises(StackInstallError) as caught:
        stackstore.update(paths, "acme-web")
    assert "stacks add" in str(caught.value)


# -- what it means for the apps --------------------------------------------


def test_an_app_keeps_the_stack_commit_it_was_born_from_when_the_stack_moves(
    home: Home, stack_repo: Path
) -> None:
    """The app is a repository (D1): its files are its own, not the template's."""
    paths, conn = home
    stackstore.add(paths, url(stack_repo))
    report = create_app(paths, conn, name="Billing Portal", stack_name="acme-web",
                        install=False)
    born = git("rev-parse", "HEAD", cwd=stack_repo).strip()
    move_stack(stack_repo, "{{app_name}} on the newer Acme stack\n")
    stackstore.update(paths, "acme-web")

    assert "newer" not in (Path(report.path) / "src" / "App.txt").read_text()
    detail = app_detail(paths, conn, require_app(conn, "billing-portal"))
    assert detail["stack_commit"] == born
    assert detail["stack_drifted"] is True
    assert born[:12] in detail["stack_note"]


def test_drift_lists_the_apps_that_are_older_than_their_stack(
    home: Home, stack_repo: Path
) -> None:
    paths, conn = home
    stackstore.add(paths, url(stack_repo))
    create_app(paths, conn, name="Old One", stack_name="acme-web", install=False)
    assert stackstore.drift(conn, paths) == []

    moved = move_stack(stack_repo, "{{app_name}} again\n")
    stackstore.update(paths, "acme-web")
    create_app(paths, conn, name="New One", stack_name="acme-web", install=False)

    drifted = stackstore.drift(conn, paths)
    assert [entry["app"] for entry in drifted] == ["old-one"]
    assert drifted[0]["stack_at"] == moved


def test_removing_a_stack_leaves_the_apps_built_from_it_alone(
    home: Home, stack_repo: Path
) -> None:
    paths, conn = home
    stackstore.add(paths, url(stack_repo))
    report = create_app(paths, conn, name="Billing Portal", stack_name="acme-web",
                        install=False)

    stackstore.remove(paths, "acme-web")

    assert "acme-web" not in registry(paths.stacks)
    assert (Path(report.path) / "src" / "App.txt").is_file()
    # And the app still reads, which is what a human needs in order to fix this.
    detail = app_detail(paths, conn, require_app(conn, "billing-portal"))
    assert detail["stack"] == "acme-web"
    assert "entry" not in detail


def test_describe_tells_a_human_which_stacks_are_theirs_and_where_from(
    home: Home, stack_repo: Path
) -> None:
    paths, _ = home
    stackstore.add(paths, url(stack_repo))

    entries = {entry["name"]: entry for entry in stackstore.describe(paths)}

    assert entries["fake"]["builtin"] is True
    assert entries["fake"]["commit"] is None
    theirs = entries["acme-web"]
    assert theirs["builtin"] is False
    assert theirs["source"] == url(stack_repo)
    assert theirs["commit"] and theirs["added_at"]


# -- from a terminal -------------------------------------------------------


def test_the_cli_installs_lists_updates_and_reports_drift(
    home: Home, stack_repo: Path
) -> None:
    from typer.testing import CliRunner

    from applace.cli import app as cli

    paths, conn = home
    runner = CliRunner()

    added = runner.invoke(cli, ["stacks", "add", url(stack_repo)])
    assert added.exit_code == 0, added.output
    assert "Installed acme-web" in added.output

    listed = runner.invoke(cli, ["stacks"])
    assert "acme-web" in listed.output
    assert "pinned at" in listed.output

    create_app(paths, conn, name="Billing Portal", stack_name="acme-web", install=False)
    move_stack(stack_repo, "{{app_name}} again\n")

    updated = runner.invoke(cli, ["stacks", "update", "acme-web"])
    assert updated.exit_code == 0, updated.output
    assert "→" in updated.output
    assert "billing-portal" in updated.output   # it says what that means for the apps

    drifted = runner.invoke(cli, ["stacks", "drift"])
    assert "billing-portal" in drifted.output

    removed = runner.invoke(cli, ["stacks", "rm", "acme-web", "--yes"])
    assert removed.exit_code == 0, removed.output
    assert "acme-web" not in runner.invoke(cli, ["stacks"]).output


def test_the_cli_refuses_a_bad_url_with_a_message_not_a_traceback(home: Home) -> None:
    from typer.testing import CliRunner

    from applace.cli import app as cli

    result = CliRunner().invoke(cli, ["stacks", "add", "file:///nowhere/at/all"])
    assert result.exit_code == 1
    assert "could not clone" in result.output


def _unused(value: Any) -> None:  # pragma: no cover - keeps pyright honest
    return None
