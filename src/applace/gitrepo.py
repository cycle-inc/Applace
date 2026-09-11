"""Git, because git is the version store (D2).

There is no version table of Applace's own: a version of an app is a commit, a
rollback is a checkout, and a diff is a diff. Everything here is a thin,
honest wrapper -- if a call fails, the caller gets git's own message.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRANCH = "main"

# Used only when the machine has no git identity at all. Commits Applace makes
# on behalf of an agent are still the user's commits, so a configured identity
# always wins; this exists so that `applace new` on a fresh laptop does not
# fail on "Please tell me who you are".
FALLBACK_NAME = "Applace"
FALLBACK_EMAIL = "applace@localhost"


class GitError(Exception):
    """A git command failed. The message is git's own."""


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str


def git(*args: str, cwd: Path) -> str:
    """Run one git command and return its stdout, or raise with git's stderr."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is a hard dependency
        raise GitError("git is not on PATH, and Applace stores every app in it.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def init(path: Path) -> None:
    """Make ``path`` a repository on ``main``, with an identity that works.

    ``--no-verify`` is not available to ``git init``, but the equivalent concern
    is: a global ``init.templateDir`` can install hooks into every new
    repository, and a hook that fails must not make app creation fail. Commits
    made here pass ``--no-verify`` for that reason.
    """
    git("init", "-b", DEFAULT_BRANCH, cwd=path)
    if not _has_identity(path):
        git("config", "user.name", FALLBACK_NAME, cwd=path)
        git("config", "user.email", FALLBACK_EMAIL, cwd=path)


def _has_identity(path: Path) -> bool:
    for key in ("user.name", "user.email"):
        try:
            if not git("config", "--get", key, cwd=path).strip():
                return False
        except GitError:
            # `git config --get` exits 1 when the key is unset, which is not an
            # error here -- it is the answer.
            return False
    return True


def commit_all(path: Path, message: str) -> Commit:
    """Stage everything and commit. Raises if there is nothing to commit."""
    git("add", "-A", cwd=path)
    if not is_dirty(path):
        raise GitError("nothing to commit: the working tree matches the last commit")
    git("commit", "--no-verify", "-m", message, cwd=path)
    return head(path)


def head(path: Path) -> Commit:
    sha = git("rev-parse", "HEAD", cwd=path).strip()
    message = git("log", "-1", "--pretty=%s", cwd=path).strip()
    return Commit(sha=sha, message=message)


def has_commits(path: Path) -> bool:
    try:
        git("rev-parse", "--verify", "HEAD", cwd=path)
    except GitError:
        return False
    return True


def is_dirty(path: Path) -> bool:
    """True when the working tree or the index differs from HEAD.

    Untracked files count: an agent that adds a page and nothing else has
    changed the app, and D4 hangs on this answer being right.
    """
    return bool(git("status", "--porcelain", cwd=path).strip())


def status(path: Path) -> list[str]:
    """The porcelain lines, for reporting *what* is uncommitted."""
    return [line for line in git("status", "--porcelain", cwd=path).splitlines() if line]


def current_branch(path: Path) -> str:
    """The branch name, which a repository has before it has any commits.

    ``rev-parse --abbrev-ref HEAD`` is the reflex here and it is wrong: on an
    unborn branch it fails outright, and a freshly initialised app is exactly
    that until the first commit lands.
    """
    return git("branch", "--show-current", cwd=path).strip()


def log(path: Path, limit: int = 20) -> list[Commit]:
    """Recent commits, newest first -- including any a human made."""
    raw = git("log", f"-{limit}", "--pretty=%H%x00%s", cwd=path)
    commits: list[Commit] = []
    for line in raw.splitlines():
        if "\x00" not in line:
            continue
        sha, message = line.split("\x00", 1)
        commits.append(Commit(sha=sha, message=message))
    return commits


def tracked_files(path: Path) -> list[str]:
    """The repository's files, which is the app's file tree.

    Read from git rather than by walking the directory, so ``node_modules`` and
    ``dist`` are excluded by the same ``.gitignore`` a human sees.
    """
    listing = git("ls-files", "--cached", "--others", "--exclude-standard", cwd=path)
    return sorted(line for line in listing.splitlines() if line)
