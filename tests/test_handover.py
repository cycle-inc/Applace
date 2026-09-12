"""A human in the repository: their commits, and their unfinished work (M9).

An app is an ordinary repository somebody can open (D1), so the question these
tests ask is the one that decides whether that invitation is safe: what happens
when the agent writes a file a person is in the middle of editing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from applace import gate, gitrepo, handover
from applace.apps import app_detail, create_app, require_app
from applace.db import Connection
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]


def make_app(home: Home) -> Any:
    paths, conn = home
    create_app(paths, conn, name="Team Dashboard", stack_name="fake", install=False)
    return require_app(conn, "team-dashboard")


def human_commit(root: Path, message: str) -> str:
    """A commit made by a person, with their own identity, outside Applace."""
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=dev@acme.test", "-c", "user.name=A Developer",
         "commit", "-m", message],
        cwd=root, check=True, capture_output=True,
    )
    return gitrepo.head(root).sha


def test_an_untouched_app_has_no_handover(home: Home) -> None:
    _, conn = home
    row = make_app(home)
    found = handover.survey(conn, row)
    assert found.touched is False
    assert found.commits == []
    assert found.edits == []


def test_a_commit_a_human_made_is_reported_but_not_refused(home: Home) -> None:
    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    (root / "src" / "notes.txt").write_text("mine\n", encoding="utf-8")
    sha = human_commit(root, "I fixed the copy")

    found = handover.survey(conn, row)
    assert [commit["sha"] for commit in found.commits] == [sha]
    assert found.edits == []
    assert "A human made 1 commit" in found.note

    # And it does not stop the agent: the tree is clean and green.
    report = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/page.txt": "x\n"}
    )
    assert report.ok is True
    assert report.committed is True


def test_a_write_over_somebodys_uncommitted_edit_is_refused(home: Home) -> None:
    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    (root / "src" / "main.txt").write_text("a paragraph I am still writing\n", encoding="utf-8")

    report = gate.write_files(
        paths, conn, app="team-dashboard",
        files_to_write={"src/main.txt": "what the model would have written\n"},
    )
    assert report.ok is False
    assert report.stage == "handover"
    assert report.written == []
    assert "src/main.txt" in str(report.errors[0].message)
    assert "commit or discard" in str(report.errors[0].message)
    # The point of the refusal: their work is still there, exactly as it was.
    assert (root / "src" / "main.txt").read_text(encoding="utf-8").startswith("a paragraph")


def test_a_write_somewhere_else_in_the_same_app_is_not_a_conflict(home: Home) -> None:
    """A harness that stopped for any open file is one nobody leaves running."""
    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    (root / "src" / "main.txt").write_text("still writing this\n", encoding="utf-8")

    report = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/page.txt": "the agent's\n"}
    )
    assert report.ok is True
    assert report.committed is True
    # Green stages the whole tree, so their edit is committed too -- and it is
    # no longer something to warn about.
    assert report.human is not None and report.human.edits == []
    assert gitrepo.is_dirty(root) is False


def test_a_new_file_the_agent_has_not_seen_is_not_a_handover(home: Home) -> None:
    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    (root / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    assert handover.survey(conn, row).edits == []
    report = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"scratch.txt": "the agent's\n"}
    )
    assert report.ok is True


def test_the_agents_own_red_write_is_not_reported_as_a_humans_work(
    home: Home, monkeypatch: Any
) -> None:
    """D4 leaves broken files on disk on purpose. That is the agent's mess."""
    import yaml

    paths, conn = home
    make_app(home)
    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["typecheck"] = "sh -c 'echo \"src/a.ts(1,1): error TS1: no.\"; exit 1'"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    red = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/main.txt": "broken\n"}
    )
    assert red.ok is False
    assert red.stage == "typecheck"
    assert red.human is not None and red.human.edits == []

    # And the agent can write the same file again to fix it.
    data["commands"]["typecheck"] = "true"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    green = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/main.txt": "fixed\n"}
    )
    assert green.ok is True


def test_get_app_tells_the_agent_before_it_tries(home: Home) -> None:
    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    (root / "src" / "notes.txt").write_text("mine\n", encoding="utf-8")
    human_commit(root, "I fixed the copy")
    (root / "src" / "main.txt").write_text("and I am editing this\n", encoding="utf-8")

    detail = app_detail(paths, conn, require_app(conn, "team-dashboard"))
    assert detail["human"]["touched"] is True
    assert detail["human"]["edits"] == ["src/main.txt"]
    assert len(detail["human"]["commits"]) == 1
    assert "Do not write those files" in detail["human"]["note"]


def test_a_check_with_no_files_commits_what_a_human_left_behind(home: Home) -> None:
    """`applace check` is the way out of the refusal, and it is one command."""
    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    (root / "src" / "main.txt").write_text("their finished work\n", encoding="utf-8")

    report = gate.write_files(paths, conn, app="team-dashboard")
    assert report.ok is True
    assert report.committed is True
    assert gitrepo.is_dirty(root) is False
    assert handover.survey(conn, require_app(conn, "team-dashboard")).touched is False


def test_a_red_write_does_not_hide_a_humans_edit_to_another_file(home: Home) -> None:
    """Both can be true at once, and only one of them is a refusal."""
    import yaml

    paths, conn = home
    row = make_app(home)
    root = Path(str(row["path"]))
    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["typecheck"] = "sh -c 'echo \"src/a.ts(1,1): error TS1: no.\"; exit 1'"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")

    red = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/page.txt": "broken\n"}
    )
    assert red.ok is False
    # Meanwhile a person starts editing a different file.
    (root / "src" / "main.txt").write_text("theirs, in progress\n", encoding="utf-8")

    found = handover.survey(conn, require_app(conn, "team-dashboard"))
    assert found.edits == ["src/main.txt"]
    assert found.agent_is_dirty is True

    blocked = gate.write_files(
        paths, conn, app="team-dashboard", files_to_write={"src/main.txt": "the model's\n"}
    )
    assert blocked.stage == "handover"
    assert (root / "src" / "main.txt").read_text(encoding="utf-8") == "theirs, in progress\n"
