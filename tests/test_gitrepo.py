"""Git, because git is the version store. Thin wrappers, but load-bearing ones."""

from __future__ import annotations

from pathlib import Path

import pytest

from applace import gitrepo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    path = tmp_path / "repo"
    path.mkdir()
    gitrepo.init(path)
    return path


def test_init_starts_on_main_with_no_commits(repo: Path) -> None:
    assert gitrepo.current_branch(repo) == "main"
    assert not gitrepo.has_commits(repo)


def test_a_machine_with_no_git_identity_can_still_commit(repo: Path) -> None:
    """`applace new` on a fresh laptop must not fail on "who are you"."""
    (repo / "a.txt").write_text("a", encoding="utf-8")
    commit = gitrepo.commit_all(repo, "first")
    assert commit.message == "first"
    assert len(commit.sha) == 40


def test_a_configured_identity_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Real Human\n\temail = human@example.com\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "none"))
    path = tmp_path / "repo"
    path.mkdir()
    gitrepo.init(path)
    (path / "a.txt").write_text("a", encoding="utf-8")
    gitrepo.commit_all(path, "first")

    author = gitrepo.git("log", "-1", "--pretty=%an <%ae>", cwd=path).strip()
    assert author == "Real Human <human@example.com>"


def test_untracked_files_count_as_dirty(repo: Path) -> None:
    """D4 hangs on this: an added page is a change even before it is staged."""
    (repo / "a.txt").write_text("a", encoding="utf-8")
    gitrepo.commit_all(repo, "first")
    assert not gitrepo.is_dirty(repo)

    (repo / "b.txt").write_text("b", encoding="utf-8")
    assert gitrepo.is_dirty(repo)
    assert gitrepo.status(repo) == ["?? b.txt"]


def test_committing_nothing_is_refused_rather_than_making_an_empty_commit(
    repo: Path,
) -> None:
    (repo / "a.txt").write_text("a", encoding="utf-8")
    gitrepo.commit_all(repo, "first")
    with pytest.raises(gitrepo.GitError):
        gitrepo.commit_all(repo, "again")


def test_tracked_files_excludes_what_gitignore_excludes(repo: Path) -> None:
    (repo / ".gitignore").write_text("out/\n", encoding="utf-8")
    (repo / "out").mkdir()
    (repo / "out" / "bundle.js").write_text("//", encoding="utf-8")
    (repo / "src.txt").write_text("s", encoding="utf-8")

    assert gitrepo.tracked_files(repo) == [".gitignore", "src.txt"]


def test_log_is_newest_first(repo: Path) -> None:
    for index in range(3):
        (repo / f"{index}.txt").write_text("x", encoding="utf-8")
        gitrepo.commit_all(repo, f"change {index}")
    assert [c.message for c in gitrepo.log(repo)] == [
        "change 2",
        "change 1",
        "change 0",
    ]


def test_a_failing_git_command_carries_gits_own_message(tmp_path: Path) -> None:
    with pytest.raises(gitrepo.GitError) as exc:
        gitrepo.head(tmp_path)
    assert "git rev-parse HEAD failed" in str(exc.value)
