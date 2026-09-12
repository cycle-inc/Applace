"""Git, because git is the version store (D2).

There is no version table of Applace's own: a version of an app is a commit, a
rollback is a checkout, and a diff is a diff. Everything here is a thin,
honest wrapper -- if a call fails, the caller gets git's own message.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRANCH = "main"

# Fed the token through the environment rather than the command line: argv is
# world-readable in `ps`, and a remote URL with the token in it would also be
# written into `.git/config` by anything that later saves it. The empty helper
# first resets the list, which is what stops the machine's own credential store
# from answering with the wrong account's token -- the failure this exists for.
TOKEN_VARIABLE = "APPLACE_GIT_TOKEN"
_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; '
    'echo "username=x-access-token"; '
    f'echo "password=${TOKEN_VARIABLE}"; }}; f'
)

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


def git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run one git command and return its stdout, or raise with git's stderr."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
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


def resolve(path: Path, ref: str) -> Commit | None:
    """The commit a ref names, or None when this repository has no such thing.

    Anything git accepts works -- a sha, a short sha, `HEAD~2`, a tag -- which is
    what makes `applace rollback <app> <sha>` forgiving about how the sha was
    copied out of `applace log`.
    """
    try:
        sha = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=path).strip()
    except GitError:
        return None
    message = git("log", "-1", "--pretty=%s", sha, cwd=path).strip()
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


def set_remote(path: Path, url: str, *, name: str = "origin") -> None:
    """Point ``name`` at ``url``, adding it if it is not there yet.

    The URL stored is the plain https one a human would use: their own
    credentials answer for it, and nothing secret is written into the
    repository's config.
    """
    if remote_url(path, name=name) is None:
        git("remote", "add", name, url, cwd=path)
    else:
        git("remote", "set-url", name, url, cwd=path)


def remote_url(path: Path, *, name: str = "origin") -> str | None:
    try:
        return git("remote", "get-url", name, cwd=path).strip() or None
    except GitError:
        return None


def _authenticated(token: str | None) -> dict[str, str]:
    """The environment a push needs: our token, and no prompt, ever."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if token:
        env[TOKEN_VARIABLE] = token
    return env


def _credential_args(token: str | None) -> list[str]:
    if not token:
        return []
    return ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]


def fetch(path: Path, url: str, branch: str, *, token: str | None = None) -> str | None:
    """Fetch ``branch`` from ``url`` and return the sha it points at.

    None means the remote has no such branch yet, which is the normal state of a
    repository Applace has just created. A remote that cannot be reached raises.
    """
    try:
        git(
            *_credential_args(token),
            "fetch",
            "--quiet",
            url,
            branch,
            cwd=path,
            env=_authenticated(token),
        )
    except GitError as exc:
        if "couldn't find remote ref" in str(exc):
            return None
        raise
    return git("rev-parse", "FETCH_HEAD", cwd=path).strip()


def contains(path: Path, sha: str) -> bool:
    """Is ``sha`` an ancestor of HEAD -- that is, do we already have that work?

    This is the D13 question. If the remote's tip is not in our history, someone
    else pushed and the snapshot is refused rather than forced over.
    """
    try:
        git("merge-base", "--is-ancestor", sha, "HEAD", cwd=path)
    except GitError:
        return False
    return True


def push(
    path: Path,
    url: str,
    branch: str = DEFAULT_BRANCH,
    *,
    token: str | None = None,
) -> None:
    """Push ``branch`` to ``url``. Never forced, never rewriting (D13)."""
    git(
        *_credential_args(token),
        "push",
        url,
        f"HEAD:refs/heads/{branch}",
        cwd=path,
        env=_authenticated(token),
    )


def add_worktree(path: Path, sha: str, destination: Path) -> None:
    """Check a commit out somewhere else, without touching the app's own tree.

    Deploying an *older* commit has to build that commit's files, and the one
    thing it must not do is move the app's working tree to get them: the agent
    may be mid-edit, and a rollback that silently checks out the past under
    someone's feet is how a harness eats work.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", "--force", str(destination), sha, cwd=path)


def remove_worktree(path: Path, destination: Path) -> None:
    """Take the worktree away again. Never fatal: it is a temporary directory."""
    try:
        git("worktree", "remove", "--force", str(destination), cwd=path)
    except GitError:
        shutil.rmtree(destination, ignore_errors=True)
        try:
            git("worktree", "prune", cwd=path)
        except GitError:  # pragma: no cover - prune fails only if git itself does
            pass


def tracked_files(path: Path) -> list[str]:
    """The repository's files, which is the app's file tree.

    Read from git rather than by walking the directory, so ``node_modules`` and
    ``dist`` are excluded by the same ``.gitignore`` a human sees.
    """
    listing = git("ls-files", "--cached", "--others", "--exclude-standard", cwd=path)
    return sorted(line for line in listing.splitlines() if line)
