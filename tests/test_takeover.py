"""The take-over (D26): a front end that exists becoming an app.

The invariant every test here circles is that the repository does not change.
Its history, its files and its working tree are what they were; the only thing
that moved is a row in a database somewhere else.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import FAKE_STACK

from applace import gitrepo, takeover
from applace.api import Applace
from applace.apps import remove_app
from applace.db import Connection, list_apps
from applace.paths import ApplacePaths
from applace.takeover import TakeError

Home = tuple[ApplacePaths, Connection]

MANIFEST = "manifest.json"


def existing(
    root: Path,
    *,
    deps: dict[str, str] | None = None,
    manifest: str = MANIFEST,
    commits: int = 1,
) -> Path:
    """A front end that was written before Applace existed: real git, real history."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / manifest).write_text(
        json.dumps({"name": root.name, "dependencies": deps or {"fakedep": "^1.0.0"}}),
        encoding="utf-8",
    )
    (root / "src" / "main.txt").write_text("A page somebody wrote by hand\n", encoding="utf-8")
    gitrepo.init(root)
    gitrepo.commit_all(root, "Initial commit")
    for number in range(1, commits):
        (root / "src" / f"page-{number}.txt").write_text(f"page {number}\n", encoding="utf-8")
        gitrepo.commit_all(root, f"Add page {number}")
    return root


def snapshot_of(root: Path) -> dict[str, str]:
    """What `git` says about the repository, to compare before and after."""
    return {
        name: subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=True,
        ).stdout
        for name, args in {
            "log": ["log", "--format=%H %s"],
            "status": ["status", "--porcelain"],
            "files": ["ls-files"],
            "config": ["config", "--local", "--list"],
        }.items()
    }


def install_stack(paths: ApplacePaths, name: str, **overrides: str) -> Path:
    """A second stack in this home, so recognition has something to choose from."""
    root = paths.stacks / name
    (root / "template").mkdir(parents=True)
    text = FAKE_STACK.replace("name: fake\n", f"name: {name}\n")
    for key, value in overrides.items():
        text = text.replace(key, value)
    (root / "stack.yaml").write_text(text, encoding="utf-8")
    return root


# -- recognition -------------------------------------------------------------


def test_a_front_end_is_recognised_from_its_manifest(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = existing(tmp_path / "legacy" / "customer-board")

    report = takeover.take(paths, conn, source=str(root), check=False)

    assert report.stack == "fake"
    assert report.recognised_by == ["fakedep"]
    assert report.slug == "customer-board"
    assert report.taken is True


def test_a_tree_no_stack_recognises_is_refused_with_what_was_looked_for(
    home: Home, tmp_path: Path
) -> None:
    """D26: refuse rather than guess. Building a Next.js app with Vite is not help."""
    paths, conn = home
    root = existing(tmp_path / "nextish", deps={"next": "15", "react": "19"})

    with pytest.raises(TakeError) as raised:
        takeover.take(paths, conn, source=str(root))

    said = str(raised.value)
    assert "fake looks for fakedep" in said      # what each stack wanted
    assert "next" in said and "react" in said    # what the manifest actually has
    assert "--stack" in said                     # and the way out
    assert list_apps(conn) == []


def test_a_directory_with_no_manifest_is_not_a_front_end(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = tmp_path / "notes"
    (root).mkdir()
    (root / "README.md").write_text("just some notes\n", encoding="utf-8")
    gitrepo.init(root)
    gitrepo.commit_all(root, "Notes")

    with pytest.raises(TakeError, match="manifest.json"):
        takeover.take(paths, conn, source=str(root))


def test_the_more_specific_stack_wins(home: Home, tmp_path: Path) -> None:
    """A company stack that names its own design system beats the generic one."""
    paths, conn = home
    install_stack(
        paths,
        "acme-web",
        **{"    - fakedep\n": "    - fakedep\n    - acme-ui\n"},
    )
    root = existing(tmp_path / "orders", deps={"fakedep": "1", "acme-ui": "2"})

    report = takeover.take(paths, conn, source=str(root), check=False)

    assert report.stack == "acme-web"
    assert report.recognised_by == ["fakedep", "acme-ui"]


def test_two_stacks_with_an_equal_claim_are_a_refusal_not_a_coin_toss(
    home: Home, tmp_path: Path
) -> None:
    paths, conn = home
    install_stack(paths, "fake-two")
    root = existing(tmp_path / "orders")

    with pytest.raises(TakeError) as raised:
        takeover.take(paths, conn, source=str(root))

    assert "fake, fake-two" in str(raised.value)
    assert "--stack" in str(raised.value)


def test_a_human_may_name_the_stack_recognition_could_not_find(
    home: Home, tmp_path: Path
) -> None:
    paths, conn = home
    root = existing(tmp_path / "mystery", deps={"nothing-known": "1"})

    report = takeover.take(paths, conn, source=str(root), stack_name="fake", check=False)

    assert report.stack == "fake"
    assert report.recognised_by == ["--stack"]


def test_a_named_stack_that_does_not_exist_fails_before_anything_is_recorded(
    home: Home, tmp_path: Path
) -> None:
    from applace.stacks import StackError

    paths, conn = home
    root = existing(tmp_path / "mystery")
    with pytest.raises(StackError, match="unknown stack"):
        takeover.take(paths, conn, source=str(root), stack_name="not-installed")
    assert list_apps(conn) == []


# -- what a take-over does, and does not, touch ------------------------------


def test_the_repository_is_not_changed_in_any_way(home: Home, tmp_path: Path) -> None:
    """The whole of D26 in one assertion: history, tree and config are untouched."""
    paths, conn = home
    root = existing(tmp_path / "legacy" / "board", commits=3)
    before = snapshot_of(root)

    report = takeover.take(paths, conn, source=str(root), check=False)

    assert snapshot_of(root) == before
    assert report.commits == 3
    assert report.path == root                      # recorded where it lives
    assert not (root / ".applace").exists()
    assert "applace" not in (root / MANIFEST).read_text(encoding="utf-8").lower()


def test_a_local_checkout_stays_where_it_is(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = existing(tmp_path / "elsewhere" / "board")

    takeover.take(paths, conn, source=str(root), check=False)

    row = list_apps(conn)[0]
    assert Path(str(row["path"])) == root
    assert not paths.app("board").exists()


def test_removing_a_taken_app_never_deletes_somebody_elses_directory(
    home: Home, tmp_path: Path
) -> None:
    """`rm --delete-files` deletes what Applace made, and an adopted tree is not."""
    paths, conn = home
    root = existing(tmp_path / "elsewhere" / "board")
    takeover.take(paths, conn, source=str(root), check=False)

    remove_app(paths, conn, "board", delete_files=True)

    assert (root / "src" / "main.txt").is_file()
    assert list_apps(conn) == []


def test_the_history_it_found_becomes_the_apps_first_snapshot(
    home: Home, tmp_path: Path
) -> None:
    paths, conn = home
    root = existing(tmp_path / "board", commits=2)
    head = gitrepo.head(root)

    takeover.take(paths, conn, source=str(root), check=False)

    from applace.db import latest_snapshot

    row = list_apps(conn)[0]
    snapshot = latest_snapshot(conn, str(row["id"]))
    assert snapshot is not None
    assert str(snapshot["commit_sha"]) == head.sha
    assert str(snapshot["message"]) == head.message


# -- refusals about the source itself ----------------------------------------


def test_a_directory_that_is_not_a_repository_is_refused(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = tmp_path / "loose"
    (root / "src").mkdir(parents=True)
    (root / MANIFEST).write_text('{"dependencies": {"fakedep": "1"}}', encoding="utf-8")

    with pytest.raises(TakeError, match="git init"):
        takeover.take(paths, conn, source=str(root))


def test_a_repository_with_no_commits_is_refused(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = tmp_path / "fresh"
    (root).mkdir()
    (root / MANIFEST).write_text('{"dependencies": {"fakedep": "1"}}', encoding="utf-8")
    gitrepo.init(root)

    with pytest.raises(TakeError, match="no commits"):
        takeover.take(paths, conn, source=str(root))


def test_a_path_that_is_not_there_says_so(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    with pytest.raises(TakeError, match="not a directory"):
        takeover.take(paths, conn, source=str(tmp_path / "nowhere"))


def test_the_same_directory_cannot_be_taken_over_twice(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = existing(tmp_path / "board")
    takeover.take(paths, conn, source=str(root), check=False)

    with pytest.raises(TakeError, match="already exists"):
        takeover.take(paths, conn, source=str(root), check=False)
    with pytest.raises(TakeError, match="already the app 'board'"):
        takeover.take(paths, conn, source=str(root), name="Board Again", check=False)


def test_a_link_out_of_the_repository_is_refused_rather_than_repaired(
    home: Home, tmp_path: Path
) -> None:
    """D26: an adoption that cannot pass the path lint is refused."""
    paths, conn = home
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "id_rsa").write_text("not really a key\n", encoding="utf-8")
    root = existing(tmp_path / "linky")
    (root / "src" / "keys").symlink_to(outside)
    gitrepo.commit_all(root, "Add a link")

    with pytest.raises(TakeError, match="link out of the repository"):
        takeover.take(paths, conn, source=str(root))
    assert list_apps(conn) == []


# -- the dry run -------------------------------------------------------------


def test_a_dry_run_recognises_and_records_nothing(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = existing(tmp_path / "board")
    before = snapshot_of(root)

    report = takeover.take(paths, conn, source=str(root), dry_run=True)

    assert report.dry_run is True
    assert report.taken is False
    assert report.stack == "fake"
    assert report.gate is None
    assert list_apps(conn) == []
    assert snapshot_of(root) == before


def test_a_dry_run_of_a_tree_that_would_be_refused_still_refuses(
    home: Home, tmp_path: Path
) -> None:
    """A dry run that says yes and a take-over that then says no would be useless."""
    paths, conn = home
    root = existing(tmp_path / "mystery", deps={"unknown": "1"})
    with pytest.raises(TakeError):
        takeover.take(paths, conn, source=str(root), dry_run=True)


# -- the first gate: a report, not a verdict ---------------------------------


def test_the_first_gate_runs_and_commits_nothing(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    root = existing(tmp_path / "board")
    # Somebody's uncommitted work, which a committing gate would swallow.
    (root / "src" / "half-done.txt").write_text("still writing this\n", encoding="utf-8")
    head = gitrepo.head(root).sha

    report = takeover.take(paths, conn, source=str(root))

    assert report.gate is not None
    assert report.gate.ok is True
    assert report.gate.committed is False
    assert gitrepo.head(root).sha == head
    assert gitrepo.is_dirty(root) is True


def test_a_codebase_that_is_already_red_is_taken_over_anyway(
    home: Home, tmp_path: Path
) -> None:
    """Refusing red would be refusing every real codebase in the building (D26)."""
    paths, conn = home
    install_stack(paths, "fake-red", **{"  build: \"true\"": "  build: \"false\""})
    root = existing(tmp_path / "broken", deps={"fakedep": "1"})

    report = takeover.take(paths, conn, source=str(root), stack_name="fake-red")

    assert report.taken is True
    assert report.gate is not None and report.gate.ok is False
    assert report.gate.stage == "build"
    assert list_apps(conn)[0]["slug"] == "broken"


def test_the_gate_can_be_skipped_for_a_take_over_that_should_be_quick(
    home: Home, tmp_path: Path
) -> None:
    paths, conn = home
    root = existing(tmp_path / "board")
    assert takeover.take(paths, conn, source=str(root), check=False).gate is None


# -- what the take-over warns about ------------------------------------------


def test_a_lockfile_from_another_package_manager_is_a_warning_not_a_refusal(
    home: Home, tmp_path: Path
) -> None:
    paths, conn = home
    install_stack(paths, "fake-npm", **{'  install: "true"': "  install: npm install"})
    root = existing(tmp_path / "board")
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    gitrepo.commit_all(root, "Add the lockfile")

    report = takeover.take(paths, conn, source=str(root), stack_name="fake-npm", check=False)

    assert report.taken is True
    assert any("pnpm-lock.yaml" in warning and "npm" in warning for warning in report.warnings)


def test_a_tree_that_does_not_ignore_the_build_output_is_warned_about(
    home: Home, tmp_path: Path
) -> None:
    """Applace writes no .gitignore into somebody's repository -- it says so (D26)."""
    paths, conn = home
    root = existing(tmp_path / "board")

    report = takeover.take(paths, conn, source=str(root), check=False)
    assert any("out and node_modules are not ignored" in w for w in report.warnings)

    tidy = existing(tmp_path / "tidy")
    (tidy / ".gitignore").write_text("out/\nnode_modules/\n", encoding="utf-8")
    gitrepo.commit_all(tidy, "Ignore the build")
    second = takeover.take(paths, conn, source=str(tidy), check=False)
    assert not any("not ignored" in w for w in second.warnings)


def test_dependencies_the_policy_would_refuse_are_reported_not_removed(
    home: Home, tmp_path: Path
) -> None:
    paths, conn = home
    paths.policy.write_text("dependencies:\n  deny: ['left-pad']\n", encoding="utf-8")
    root = existing(tmp_path / "board", deps={"fakedep": "1", "left-pad": "1.3.0"})

    report = takeover.take(paths, conn, source=str(root), check=False)

    assert report.taken is True
    assert any("left-pad" in warning for warning in report.warnings)
    assert "left-pad" in (root / MANIFEST).read_text(encoding="utf-8")


# -- a URL --------------------------------------------------------------------


def test_a_url_is_cloned_into_the_home_with_its_history(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    origin = existing(tmp_path / "origin" / "customer-board", commits=2)
    bare = tmp_path / "remote" / "customer-board.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", str(origin), str(bare)], check=True, capture_output=True
    )

    report = takeover.take(paths, conn, source=bare.as_uri(), check=False)

    assert report.cloned is True
    assert report.path == paths.app("customer-board")
    assert report.commits == 2
    assert report.remote == bare.as_uri()
    assert (report.path / "src" / "main.txt").is_file()


def test_a_dry_run_of_a_url_leaves_nothing_behind(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    origin = existing(tmp_path / "origin" / "board")
    report = takeover.take(paths, conn, source=origin.as_uri(), dry_run=True)

    assert report.taken is False
    assert not paths.app("board").exists()
    assert list_apps(conn) == []


def test_the_name_comes_from_the_url_or_from_the_caller(home: Home, tmp_path: Path) -> None:
    paths, conn = home
    origin = existing(tmp_path / "origin" / "customer-board")
    report = takeover.take(
        paths, conn, source=origin.as_uri(), name="The Old Board", check=False
    )
    assert report.slug == "the-old-board"
    assert report.name == "The Old Board"


# -- the doors ----------------------------------------------------------------


def test_the_python_door_answers_instead_of_raising(home: Home, tmp_path: Path) -> None:
    paths, _ = home
    ap = Applace(paths)
    root = existing(tmp_path / "board")

    taken = ap.take(str(root), check=False)
    assert taken["ok"] is True
    assert taken["app"] == "board"
    assert taken["taken"] is True
    assert [app["app"] for app in ap.apps()["apps"]] == ["board"]

    refused = ap.take(str(tmp_path / "nowhere"))
    assert refused["ok"] is False
    assert refused["code"] == "take-refused"
    assert "not a directory" in refused["error"]


def test_taking_over_is_not_a_tool_an_agent_has(home: Home) -> None:
    """Pointing Applace at a path on the developer's disk is theirs to do (D19)."""
    import asyncio

    from applace.server import build_server

    paths, _ = home
    tools = {tool.name for tool in asyncio.run(build_server(paths).list_tools())}
    assert "take" not in tools


def test_the_cli_takes_over_and_says_what_it_found(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from applace.cli import app as cli

    paths, _ = home
    monkeypatch.setenv("APPLACE_HOME", str(paths.home))
    root = existing(tmp_path / "legacy" / "customer-board", commits=2)
    runner = CliRunner()

    dry = runner.invoke(cli, ["take", str(root), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "recognised by fakedep" in dry.output
    assert "2 commits" in dry.output
    assert "nothing was recorded" in dry.output

    done = runner.invoke(cli, ["take", str(root), "--no-check"])
    assert done.exit_code == 0, done.output
    assert str(root) in done.output
    listed = runner.invoke(cli, ["ls"])
    assert "customer-board" in listed.output


def test_the_cli_refuses_a_tree_it_does_not_recognise(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from applace.cli import app as cli

    paths, _ = home
    monkeypatch.setenv("APPLACE_HOME", str(paths.home))
    root = existing(tmp_path / "mystery", deps={"unknown": "1"})
    result = CliRunner().invoke(cli, ["take", str(root)])
    assert result.exit_code == 1
    assert "no installed stack recognises this app" in result.output


def test_a_taken_app_behaves_like_any_other(home: Home, tmp_path: Path) -> None:
    """The point of the row: from here on there is nothing special about it."""
    paths, _ = home
    ap = Applace(paths)
    root = existing(tmp_path / "board")
    ap.take(str(root), check=False)

    detail: dict[str, Any] = ap.app("board")
    assert detail["ok"] is True
    assert detail["stack"] == "fake"
    assert "src/main.txt" in detail["files"]
    # The tree cannot tell that Applace exists, and the journal has to (D26).
    assert detail["taken_from"] == str(root)
    assert ap.apps()["apps"][0]["taken_from"] == str(root)

    written = ap.write("board", {"src/main.txt": "Now with Applace in the loop\n"},
                       message="Change the page")
    assert written["ok"] is True and written["committed"] is True
    assert gitrepo.count_commits(root) == 2
    assert gitrepo.head(root).message == "Change the page"
