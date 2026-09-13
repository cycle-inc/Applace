"""The compiler: what it runs, what it refuses, and what it commits.

These use the no-op stack, with individual commands swapped for a `sh -c` that
prints what a real tool would print and exits non-zero. That keeps the tests
about the pipeline's behaviour rather than about npm's, and it is the only
place in the suite that reaches for a shell.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from applace import gate, gitrepo
from applace.apps import app_detail, create_app, require_app
from applace.db import Connection, latest_gate, list_snapshots
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]


def _set_command(paths: ApplacePaths, name: str, command: str) -> None:
    """Rewrite one of the fake stack's commands, before the app reads it."""
    manifest = paths.stacks / "fake" / "stack.yaml"
    data: dict[str, Any] = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"][name] = command
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def app(home: Home) -> tuple[ApplacePaths, Connection, Path]:
    paths, conn = home
    report = create_app(paths, conn, name="Gate App", stack_name="fake", install=False)
    return paths, conn, report.path


def test_a_green_write_is_committed_and_leaves_the_tree_clean(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    report = gate.write_files(
        paths, conn, app="gate-app", files_to_write={"src/new.txt": "hello\n"}
    )
    assert report.ok
    assert report.stage == "build"
    assert report.written == ["src/new.txt"]
    assert report.committed and report.commit
    assert not report.dirty
    assert gitrepo.head(root).sha == report.commit


def test_the_agents_message_becomes_the_commit_subject(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    gate.write_files(
        paths,
        conn,
        app="gate-app",
        files_to_write={"src/new.txt": "hello\n"},
        message="Add the greeting",
    )
    assert gitrepo.head(root).message == "Add the greeting"


def test_a_write_with_no_message_still_says_what_it_touched(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    gate.write_files(paths, conn, app="gate-app", files_to_write={"src/new.txt": "x\n"})
    assert gitrepo.head(root).message == "Update src/new.txt"


def test_a_red_write_keeps_the_files_but_makes_no_commit(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    before = gitrepo.head(root).sha
    _set_command(paths, "typecheck", "false")

    report = gate.write_files(
        paths, conn, app="gate-app", files_to_write={"src/bad.txt": "oops\n"}
    )

    assert not report.ok
    assert report.stage == "typecheck"
    assert report.commit is None
    assert report.dirty
    assert (root / "src" / "bad.txt").read_text(encoding="utf-8") == "oops\n"
    assert gitrepo.head(root).sha == before


def test_the_stage_after_a_failure_is_not_run(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    _set_command(paths, "typecheck", "false")
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    assert [stage.name for stage in report.stages] == [
        "policy", "apis", "install", "typecheck"
    ]


def test_a_failing_stage_comes_back_with_the_tools_own_file_and_line(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    _set_command(
        paths,
        "typecheck",
        "sh -c 'echo \"src/App.tsx(4,11): error TS2322: Type mismatch.\"; exit 1'",
    )
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    error = report.errors[0]
    assert (error.file, error.line, error.column) == ("src/App.tsx", 4, 11)
    assert error.code == "TS2322"
    assert error.message == "Type mismatch."


def test_a_stack_with_no_linter_is_not_a_failure(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    skipped = {stage.name for stage in report.stages if stage.skipped}
    assert "lint" in skipped
    assert report.ok


def test_a_refused_path_writes_nothing_at_all(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    report = gate.write_files(
        paths,
        conn,
        app="gate-app",
        files_to_write={"src/fine.txt": "ok\n", "../escape.txt": "no\n"},
    )
    assert not report.ok
    assert report.stage == "paths"
    assert not (root / "src" / "fine.txt").exists()
    assert not (root.parent / "escape.txt").exists()


def test_deleting_a_file_is_a_change_the_gate_commits(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    report = gate.write_files(paths, conn, app="gate-app", delete=["src/main.txt"])
    assert report.ok
    assert report.deleted == ["src/main.txt"]
    assert not (root / "src" / "main.txt").exists()
    assert "src/main.txt" not in gitrepo.tracked_files(root)


def test_writing_what_is_already_there_is_green_without_a_commit(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    existing = (root / "src" / "main.txt").read_text(encoding="utf-8")
    report = gate.write_files(
        paths, conn, app="gate-app", files_to_write={"src/main.txt": existing}
    )
    assert report.ok
    assert not report.committed
    assert len(list_snapshots(conn, str(require_app(conn, "gate-app")["id"]))) == 1


def test_install_is_skipped_when_the_manifest_did_not_move(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    (root / "node_modules").mkdir()
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    install = next(stage for stage in report.stages if stage.name == "install")
    assert install.skipped


def test_touching_the_manifest_forces_an_install(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    (root / "node_modules").mkdir()
    report = gate.write_files(
        paths,
        conn,
        app="gate-app",
        files_to_write={"manifest.json": '{"dependencies": {"zod": "^4.0.0"}}'},
    )
    install = next(stage for stage in report.stages if stage.name == "install")
    assert not install.skipped


def test_a_new_dependency_is_reported_to_the_agent(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    (root / "node_modules").mkdir()
    report = gate.write_files(
        paths,
        conn,
        app="gate-app",
        files_to_write={"manifest.json": '{"dependencies": {"zod": "^4.0.0"}}'},
    )
    assert report.new_deps == [
        {"name": "zod", "version": "^4.0.0", "section": "dependencies"}
    ]


def test_a_dependency_the_policy_refuses_never_reaches_npm(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    """D9: the refusal happens before install, which is when scripts run."""
    paths, conn, root = app
    paths.policy.write_text("dependencies:\n  deny: ['left-pad']\n", encoding="utf-8")
    _set_command(paths, "install", "false")  # would fail the gate if it ran
    before = gitrepo.head(root).sha

    report = gate.write_files(
        paths,
        conn,
        app="gate-app",
        files_to_write={"manifest.json": '{"dependencies": {"left-pad": "^1.0.0"}}'},
    )
    assert report.ok is False
    assert report.stage == "policy"
    assert [refusal.name for refusal in report.refused] == ["left-pad"]
    assert "denylist" in report.errors[0].message
    # And the agent is told what to do instead, not just told no.
    assert "policy.yaml" in report.errors[0].message
    assert [stage.name for stage in report.stages] == ["policy"]
    # D4 holds: the file is still on disk and nothing was committed.
    assert report.dirty is True
    assert gitrepo.head(root).sha == before
    assert report.as_dict()["refused_deps"][0]["name"] == "left-pad"


def test_a_dependency_the_policy_allows_goes_straight_through(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    paths.policy.write_text("dependencies:\n  allow: ['zod']\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    report = gate.write_files(
        paths,
        conn,
        app="gate-app",
        files_to_write={"manifest.json": '{"dependencies": {"zod": "^4.0.0"}}'},
    )
    assert report.ok is True
    policy_stage = next(stage for stage in report.stages if stage.name == "policy")
    assert policy_stage.ok and not policy_stage.skipped


def test_a_write_with_no_new_dependency_skips_the_policy_stage(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    policy_stage = next(stage for stage in report.stages if stage.name == "policy")
    assert policy_stage.skipped and "no dependencies" in policy_stage.reason


def test_a_broken_policy_file_stops_the_write_instead_of_allowing_everything(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    paths.policy.write_text("dependencies: 42\n", encoding="utf-8")
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    assert report.ok is False
    assert report.stage == "policy"
    assert "must be a mapping" in report.errors[0].message


def test_every_pass_is_journaled_green_or_red(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    app_id = str(require_app(conn, "gate-app")["id"])
    _set_command(paths, "typecheck", "false")
    gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    red = latest_gate(conn, app_id)
    assert red is not None and not red["ok"] and red["stage"] == "typecheck"
    assert red["snapshot_id"] is None

    _set_command(paths, "typecheck", "true")
    gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "y"})
    green = latest_gate(conn, app_id)
    assert green is not None and green["ok"] and green["snapshot_id"]


def test_get_app_hands_back_the_errors_that_are_still_outstanding(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    _set_command(
        paths, "typecheck", "sh -c 'echo \"src/a.ts(1,1): error TS1005: nope.\"; exit 1'"
    )
    gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})

    detail = app_detail(paths, conn, require_app(conn, "gate-app"))
    assert detail["dirty"]
    assert detail["failed_stage"] == "typecheck"
    assert detail["errors"][0]["file"] == "src/a.ts"


def test_a_green_gate_clears_the_outstanding_errors(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    _set_command(paths, "typecheck", "false")
    gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    _set_command(paths, "typecheck", "true")
    gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "y"})

    detail = app_detail(paths, conn, require_app(conn, "gate-app"))
    assert detail["errors"] == []
    assert not detail["dirty"]


def test_a_check_with_no_files_still_runs_the_pipeline(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, root = app
    (root / "src" / "byhand.txt").write_text("a human wrote this\n", encoding="utf-8")
    report = gate.write_files(paths, conn, app="gate-app", message="Keep the hand edit")
    assert report.ok
    assert report.committed
    assert gitrepo.head(root).message == "Keep the hand edit"


def test_a_command_the_machine_does_not_have_is_an_error_not_a_crash(
    app: tuple[ApplacePaths, Connection, Path],
) -> None:
    paths, conn, _ = app
    _set_command(paths, "build", "definitely-not-a-real-program")
    report = gate.write_files(paths, conn, app="gate-app", files_to_write={"a.txt": "x"})
    assert not report.ok
    assert report.stage == "build"
    assert "not on PATH" in report.errors[0].message
