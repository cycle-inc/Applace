"""Creating an app: what lands on disk, what lands in git, what is refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from applace import gitrepo
from applace.apps import (
    AppExists,
    UnknownApp,
    app_detail,
    app_summary,
    create_app,
    remove_app,
    require_app,
)
from applace.db import Connection, find_app, list_snapshots
from applace.naming import InvalidName
from applace.paths import ApplacePaths
from applace.stacks import StackError

Home = tuple[ApplacePaths, Connection]


def make(home: Home, name: str = "Team Dashboard", **kwargs: object) -> object:
    paths, conn = home
    kwargs.setdefault("stack_name", "fake")
    kwargs.setdefault("install", False)
    return create_app(paths, conn, name=name, **kwargs)  # type: ignore[arg-type]


def test_a_new_app_is_a_committed_repository(home: Home) -> None:
    paths, conn = home
    report = create_app(
        paths, conn, name="Team Dashboard", stack_name="fake", install=False
    )

    assert report.slug == "team-dashboard"
    assert report.path == paths.app("team-dashboard")
    assert (report.path / "manifest.json").read_text() == '{"name": "team-dashboard"}\n'
    assert (report.path / ".git").is_dir()
    assert not gitrepo.is_dirty(report.path)
    assert gitrepo.current_branch(report.path) == "main"
    assert gitrepo.head(report.path).sha == report.commit
    assert "the fake stack" in gitrepo.head(report.path).message


def test_the_app_is_recorded_with_its_first_snapshot(home: Home) -> None:
    paths, conn = home
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)

    row = find_app(conn, "team-dashboard")
    assert row is not None
    assert row["stack"] == "fake"
    assert row["name"] == "Team Dashboard"
    assert row["default_branch"] == "main"
    snapshots = list_snapshots(conn, str(row["id"]))
    assert [s["commit_sha"] for s in snapshots] == [report.commit]


def test_the_description_reaches_the_rendered_files(home: Home) -> None:
    paths, conn = home
    report = create_app(
        paths,
        conn,
        name="Team Dashboard",
        stack_name="fake",
        description="Who is on call.",
        install=False,
    )
    assert (report.path / "src" / "main.txt").read_text() == (
        "Team Dashboard: Who is on call.\n"
    )


def test_the_same_name_twice_is_refused_without_touching_the_first(home: Home) -> None:
    paths, conn = home
    first = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    with pytest.raises(AppExists):
        create_app(paths, conn, name="team dashboard", stack_name="fake", install=False)
    assert (first.path / "manifest.json").exists()


def test_a_directory_in_the_way_is_refused_rather_than_written_into(home: Home) -> None:
    paths, conn = home
    squatter = paths.app("team-dashboard")
    squatter.mkdir(parents=True)
    (squatter / "someone-elses-work.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(AppExists):
        create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    assert (squatter / "someone-elses-work.txt").read_text() == "mine"
    assert not (squatter / "manifest.json").exists()


def test_an_unusable_name_is_refused_before_anything_is_written(home: Home) -> None:
    paths, conn = home
    with pytest.raises(InvalidName):
        create_app(paths, conn, name="!!!", stack_name="fake", install=False)
    assert list(paths.apps.iterdir()) == []


def test_an_unknown_stack_is_refused_before_anything_is_written(home: Home) -> None:
    paths, conn = home
    with pytest.raises(StackError):
        create_app(paths, conn, name="Team Dashboard", stack_name="nope", install=False)
    assert list(paths.apps.iterdir()) == []


def test_a_failed_install_keeps_the_app_and_says_so(home: Home) -> None:
    """The source is valid and committed; only the dependencies are missing."""
    paths, conn = home
    (paths.stacks / "fake" / "stack.yaml").write_text(
        "name: fake\ntitle: t\ncommands:\n"
        "  install: 'false'\n  dev: 'true'\n  build: 'true'\n",
        encoding="utf-8",
    )
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=True)

    assert report.installed is False
    assert report.warnings and "false" in report.warnings[0]
    assert find_app(conn, "team-dashboard") is not None
    assert gitrepo.has_commits(report.path)


def test_a_stack_whose_install_program_is_missing_is_a_hard_failure(home: Home) -> None:
    paths, conn = home
    (paths.stacks / "fake" / "stack.yaml").write_text(
        "name: fake\ntitle: t\ncommands:\n"
        "  install: applace-no-such-program\n  dev: 'true'\n  build: 'true'\n",
        encoding="utf-8",
    )
    from applace.shell import CommandNotFound

    with pytest.raises(CommandNotFound):
        create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=True)


def test_get_app_reports_the_file_tree_and_the_dirt(home: Home) -> None:
    paths, conn = home
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    row = require_app(conn, "team-dashboard")

    detail = app_detail(paths, conn, row)
    assert detail["app"] == "team-dashboard"
    assert detail["dirty"] is False
    assert detail["entry"] == "src/main.txt"
    assert detail["env_prefix"] == "TEST_"
    assert set(detail["files"]) == {".gitignore", "manifest.json", "src/main.txt"}
    assert detail["snapshots"] == 1

    (report.path / "src" / "main.txt").write_text("edited", encoding="utf-8")
    assert app_detail(paths, conn, require_app(conn, "team-dashboard"))["dirty"] is True


def test_ignored_files_are_not_part_of_the_app(home: Home) -> None:
    """`out/` is in the stack's .gitignore, so it is build output, not the app."""
    paths, conn = home
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    (report.path / "out").mkdir()
    (report.path / "out" / "bundle.js").write_text("//", encoding="utf-8")

    detail = app_detail(paths, conn, require_app(conn, "team-dashboard"))
    assert "out/bundle.js" not in detail["files"]
    assert detail["dirty"] is False


def test_an_app_whose_directory_vanished_is_reported_not_raised(home: Home) -> None:
    import shutil

    paths, conn = home
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    shutil.rmtree(report.path)

    summary = app_summary(conn, require_app(conn, "team-dashboard"))
    assert summary["missing"] is True


def test_an_unknown_app_names_the_ones_that_exist(home: Home) -> None:
    paths, conn = home
    create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    with pytest.raises(UnknownApp) as exc:
        require_app(conn, "nope")
    assert "team-dashboard" in str(exc.value)


def test_removing_an_app_can_keep_or_delete_its_repository(home: Home) -> None:
    paths, conn = home
    create_app(paths, conn, name="Kept", stack_name="fake", install=False)
    create_app(paths, conn, name="Gone", stack_name="fake", install=False)

    kept: Path = remove_app(paths, conn, "kept", delete_files=False)
    gone: Path = remove_app(paths, conn, "gone", delete_files=True)

    assert kept.is_dir()
    assert not gone.exists()
    assert find_app(conn, "kept") is None
