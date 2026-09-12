"""Apps and their repositories: creation, adoption, and the D13 refusal.

The GitHub here is fake but the git is real: every repository the fake creates
is an actual bare repository, so a push is a push and a divergence is a real one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeGitHub, connected

from applace import gate, github, gitrepo, sync
from applace.apps import create_app, require_app
from applace.db import Connection, unpushed
from applace.github import GitHubError, GitHubLink
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]


def make_app(home: Home, name: str = "Team Dashboard", **kwargs: Any) -> Any:
    paths, conn = home
    create_app(paths, conn, name=name, stack_name="fake", install=False, **kwargs)
    return require_app(conn, "team-dashboard")


def bare_head(bare: Path, branch: str = "main") -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", branch],
        capture_output=True, text=True, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def test_connecting_an_app_creates_the_repository_and_wires_the_remote(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, conn = home
    link = connected(paths, fake_github)
    row = make_app(home, publish=False)

    repository = sync.connect_app(paths, conn, row, link=link)
    assert repository.full_name == "acme/team-dashboard"
    assert gitrepo.remote_url(Path(str(row["path"]))) == repository.clone_url

    row = require_app(conn, "team-dashboard")
    assert row["github_repo"] == "acme/team-dashboard"
    assert row["github_url"] == "https://github.test/acme/team-dashboard"
    assert row["default_branch"] == "main"


def test_a_push_puts_the_history_on_the_remote(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, conn = home
    link = connected(paths, fake_github)
    row = make_app(home, publish=False)
    sync.connect_app(paths, conn, row, link=link)

    report = sync.push_app(paths, conn, require_app(conn, "team-dashboard"))
    assert report.pushed is True, report.reason
    local = gitrepo.head(Path(str(row["path"]))).sha
    assert bare_head(fake_github.bare("acme/team-dashboard")) == local
    assert report.commit == local
    assert unpushed(conn, str(row["id"])) == []


def test_an_app_with_no_repository_is_told_how_to_get_one(home: Home) -> None:
    paths, conn = home
    row = make_app(home, publish=False)
    report = sync.push_app(paths, conn, row)
    assert report.pushed is False
    assert "applace github connect" in report.reason


def test_the_remote_wins_when_someone_else_pushed(
    home: Home, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    """D13: never force, never rewrite -- report and stop."""
    paths, conn = home
    link = connected(paths, fake_github)
    row = make_app(home, publish=False)
    sync.connect_app(paths, conn, row, link=link)
    sync.push_app(paths, conn, require_app(conn, "team-dashboard"))

    # A colleague clones, commits, pushes. Now the remote is ahead of us.
    bare = fake_github.bare("acme/team-dashboard")
    elsewhere = tmp_path / "colleague"
    subprocess.run(["git", "clone", str(bare), str(elsewhere)], check=True, capture_output=True)
    (elsewhere / "THEIRS.md").write_text("their work\n", encoding="utf-8")
    for command in (
        ["git", "add", "-A"],
        ["git", "-c", "user.email=they@example.com", "-c", "user.name=They",
         "commit", "-m", "Their work"],
        ["git", "push"],
    ):
        subprocess.run(command, cwd=elsewhere, check=True, capture_output=True)
    theirs = bare_head(bare)

    # And we have a local commit of our own to send.
    root = Path(str(row["path"]))
    (root / "src" / "ours.txt").write_text("our work\n", encoding="utf-8")
    gitrepo.commit_all(root, "Our work")

    report = sync.push_app(paths, conn, require_app(conn, "team-dashboard"))
    assert report.pushed is False
    assert report.diverged is True
    assert report.remote_commit == theirs
    assert "never force-pushes" in report.reason
    # Their commit is still the tip: we did not overwrite anyone.
    assert bare_head(bare) == theirs


def test_a_rebase_does_not_leave_a_phantom_backlog(
    home: Home, fake_github: FakeGitHub, tmp_path: Path
) -> None:
    """The way out of a divergence is a rebase, which renames our commits.

    The snapshot we recorded keeps the sha it had before the rebase, so the
    backlog can only be settled by the push having succeeded, never by matching
    shas -- otherwise the app is "1 commit behind" forever.
    """
    paths, conn = home
    link = connected(paths, fake_github)
    row = make_app(home, publish=False)
    sync.connect_app(paths, conn, row, link=link)
    sync.push_app(paths, conn, require_app(conn, "team-dashboard"))

    bare = fake_github.bare("acme/team-dashboard")
    elsewhere = tmp_path / "colleague"
    subprocess.run(["git", "clone", str(bare), str(elsewhere)], check=True, capture_output=True)
    (elsewhere / "THEIRS.md").write_text("their work\n", encoding="utf-8")
    for command in (
        ["git", "add", "-A"],
        ["git", "-c", "user.email=they@example.com", "-c", "user.name=They",
         "commit", "-m", "Their work"],
        ["git", "push"],
    ):
        subprocess.run(command, cwd=elsewhere, check=True, capture_output=True)

    # Our own green write: committed, journalled, and refused by D13.
    root = Path(str(row["path"]))
    written = gate.write_files(
        paths, conn, app="team-dashboard",
        files_to_write={"src/ours.txt": "our work\n"}, message="Our work",
    )
    ours = written.commit
    assert written.push is not None and written.push.diverged is True
    assert len(unpushed(conn, str(row["id"]))) == 1

    # A human reconciles the only way Applace allows: by rebasing.
    subprocess.run(
        ["git", "-c", "user.email=me@example.com", "-c", "user.name=Me",
         "pull", "--rebase", str(bare), "main"],
        cwd=root, check=True, capture_output=True,
    )
    rebased = gitrepo.head(root).sha
    assert rebased != ours, "the rebase should have renamed our commit"

    report = sync.push_app(paths, conn, require_app(conn, "team-dashboard"))
    assert report.pushed is True, report.reason
    assert bare_head(bare) == rebased
    assert unpushed(conn, str(row["id"])) == []
    assert report.behind == 0


def test_a_push_that_was_refused_is_retried_by_the_next_one(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, conn = home
    link = connected(paths, fake_github)
    row = make_app(home, publish=False)
    sync.connect_app(paths, conn, row, link=link)

    # The first attempt cannot reach GitHub at all.
    broken = paths.app("team-dashboard")
    gitrepo.set_remote(broken, "file:///nowhere/at/all.git")
    first = sync.push_app(paths, conn, require_app(conn, "team-dashboard"))
    assert first.pushed is False
    assert len(unpushed(conn, str(row["id"]))) == 1

    gitrepo.set_remote(broken, fake_github.repos["acme/team-dashboard"]["clone_url"])
    second = sync.push_app(paths, conn, require_app(conn, "team-dashboard"))
    assert second.pushed is True
    assert unpushed(conn, str(row["id"])) == []


def test_adopting_an_existing_repository(home: Home, fake_github: FakeGitHub) -> None:
    paths, conn = home
    link = connected(paths, fake_github)
    fake_github.create("acme/already-there")
    row = make_app(home, publish=False)

    repository = sync.adopt_app(paths, conn, row, "acme/already-there", link=link)
    assert repository.full_name == "acme/already-there"
    assert require_app(conn, "team-dashboard")["github_repo"] == "acme/already-there"
    assert sync.push_app(paths, conn, require_app(conn, "team-dashboard")).pushed is True


def test_adopting_a_repository_that_is_not_there_says_so(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, conn = home
    link = connected(paths, fake_github)
    row = make_app(home, publish=False)
    with pytest.raises(GitHubError) as raised:
        sync.adopt_app(paths, conn, row, "acme/imaginary", link=link)
    assert "does not exist" in str(raised.value)


def test_a_new_app_is_a_repository_from_birth(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, conn = home
    connected(paths, fake_github)
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)

    assert report.repo == "acme/team-dashboard"
    assert report.github_url == "https://github.test/acme/team-dashboard"
    assert report.pushed is True
    assert report.warnings == []
    assert bare_head(fake_github.bare("acme/team-dashboard")) == report.commit


def test_github_being_down_does_not_cost_the_developer_their_app(
    home: Home, fake_github: FakeGitHub
) -> None:
    paths, conn = home
    connected(paths, fake_github)
    fake_github.refuse_creation = "Resource not accessible"
    report = create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)

    assert report.commit  # committed locally all the same
    assert report.repo is None
    assert any("applace push team-dashboard" in warning for warning in report.warnings)


def test_changing_the_connection_leaves_existing_apps_where_they_are(
    home: Home, fake_github: FakeGitHub
) -> None:
    """D2b: the connection is for apps yet to be created, not a rewrite."""
    paths, conn = home
    connected(paths, fake_github)
    create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    before = gitrepo.remote_url(paths.app("team-dashboard"))

    github.save(paths, GitHubLink(org="other", api_base=fake_github.api_base))
    assert gitrepo.remote_url(paths.app("team-dashboard")) == before
    assert require_app(conn, "team-dashboard")["github_repo"] == "acme/team-dashboard"


def test_a_green_write_lands_on_github(home: Home, fake_github: FakeGitHub) -> None:
    paths, conn = home
    connected(paths, fake_github)
    create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)

    report = gate.write_files(
        paths, conn, app="team-dashboard",
        files_to_write={"src/page.txt": "a page\n"}, message="Add a page",
    )
    assert report.ok and report.committed
    assert report.push is not None and report.push.pushed is True
    assert bare_head(fake_github.bare("acme/team-dashboard")) == report.commit
    assert report.as_dict()["github"]["pushed"] is True


def test_a_red_write_pushes_nothing(
    home: Home, fake_github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    paths, conn = home
    connected(paths, fake_github)
    create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    on_github = bare_head(fake_github.bare("acme/team-dashboard"))

    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["typecheck"] = "sh -c 'echo \"src/a.ts(1,1): error TS1: no.\"; exit 1'"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    report = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/page.txt": "x\n"}
    )
    assert report.ok is False
    assert report.push is None
    assert bare_head(fake_github.bare("acme/team-dashboard")) == on_github


def test_the_push_never_fails_the_write(home: Home, fake_github: FakeGitHub) -> None:
    """A green gate is green even when GitHub is unreachable."""
    paths, conn = home
    connected(paths, fake_github)
    create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    gitrepo.set_remote(paths.app("team-dashboard"), "file:///nowhere/at/all.git")

    report = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/page.txt": "x\n"}
    )
    assert report.ok is True
    assert report.committed is True
    assert report.push is not None and report.push.pushed is False
    assert report.push.reason
