"""Keeping an app and its GitHub repository the same thing (D2, D13, D15).

The rule this module exists to enforce is D13: **the remote wins**. If someone
else pushed -- a human, another machine -- the snapshot is refused and the
divergence is reported. Applace never force-pushes and never rewrites history,
so the worst case is an app whose commits are still local, which is recoverable,
rather than a colleague's work overwritten, which is not.

The second rule is that a push failure never undoes a green gate. The commit is
already made; whether it reached GitHub is a separate fact, recorded separately,
and retried by the next push.

The third is D15, and it is what makes this harness usable by a team that does
not merge machine-written code unread: when the connection says ``review: pr``,
green commits go to the app's own branch and one pull request stays open against
the default branch. Nothing else changes -- same repository, same commits, same
refusal to force -- and the default branch becomes what a human merged, which is
also what Vercel calls production (D12).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import github, gitrepo
from .db import Connection, mark_pushed, set_github, unpushed
from .github import GitHubError, GitHubLink, NotConnected, PullRequest, Repository
from .paths import ApplacePaths

DIVERGED = (
    "{repo} has moved ahead of this machine: its {branch} is at {remote}, which "
    "is not in the local history. Applace never force-pushes (D13). Pull or "
    "rebase the app at {path}, then push again."
)

PR_TITLE = "{name}, built with Applace"
PR_BODY = """\
This branch is where Applace pushes `{app}`. Every commit on it passed the gate
on the machine that made it: type-checked, linted and built before it was
committed (D3, D4).

Review it as you would any branch. Merging is what puts this work on `{base}`,
and on `{base}` is what production means (D12). The branch stays; the next green
change lands on it and this pull request comes back.

The app is an ordinary repository -- `git checkout {head}` and run it.
"""


@dataclass
class PushReport:
    app: str
    pushed: bool
    repo: str | None = None
    github_url: str | None = None
    commit: str | None = None
    branch: str = gitrepo.DEFAULT_BRANCH
    reason: str = ""
    diverged: bool = False
    remote_commit: str | None = None
    behind: int = 0
    # Set when the connection reviews work (D15). `pull_request` is None on a
    # branch with nothing new on it, which is the state after a human merged.
    reviewed: bool = False
    base: str | None = None
    pull_request: PullRequest | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pushed": self.pushed,
            "repo": self.repo,
            "github_url": self.github_url,
            "commit": self.commit,
            "branch": self.branch,
            "reason": self.reason,
            "diverged": self.diverged,
            "remote_commit": self.remote_commit,
            "unpushed": self.behind,
        }
        if self.reviewed:
            out["reviewed"] = True
            out["base"] = self.base
            out["pull_request"] = (
                self.pull_request.as_dict() if self.pull_request is not None else None
            )
            out["pull_request_url"] = (
                self.pull_request.html_url if self.pull_request is not None else None
            )
        return out


def connect_app(
    paths: ApplacePaths,
    conn: Connection,
    row: Any,
    *,
    description: str | None = None,
    link: GitHubLink | None = None,
) -> Repository:
    """Create (or adopt) this app's repository and wire the remote to it."""
    link = link or github.require(paths)
    repository = github.create_repository(
        link, str(row["slug"]), description=description or row["description"]
    )
    _attach(conn, row, repository)
    return repository


def adopt_app(
    paths: ApplacePaths,
    conn: Connection,
    row: Any,
    full_name: str,
    *,
    link: GitHubLink | None = None,
) -> Repository:
    """Point an app at a repository that already exists, by ``owner/name``."""
    link = link or github.require(paths)
    repository = github.get_repository(link, full_name)
    if repository is None:
        raise GitHubError(
            f"{full_name} does not exist, or the token cannot see it. Create it "
            f"first, or let `applace github connect` make one for you."
        )
    _attach(conn, row, repository)
    return repository


def _attach(conn: Connection, row: Any, repository: Repository) -> None:
    root = Path(str(row["path"]))
    gitrepo.set_remote(root, repository.clone_url)
    set_github(
        conn,
        app_id=str(row["id"]),
        repo=repository.full_name,
        url=repository.html_url,
        default_branch=repository.default_branch,
    )
    conn.commit()


def push_app(paths: ApplacePaths, conn: Connection, row: Any) -> PushReport:
    """Push the app's commits, unless the remote has moved (D13).

    Returns a report instead of raising: this runs at the end of every green
    gate, and "the code is committed but GitHub is unreachable" is news, not a
    failure of the write.
    """
    app = str(row["slug"])
    root = Path(str(row["path"]))
    base = str(row["default_branch"] or gitrepo.DEFAULT_BRANCH)
    report = PushReport(
        app=app,
        pushed=False,
        repo=row["github_repo"],
        github_url=row["github_url"],
        branch=base,
        behind=len(unpushed(conn, str(row["id"]))),
    )

    url = gitrepo.remote_url(root)
    if url is None or not report.repo:
        report.reason = (
            "this app has no GitHub repository. `applace github connect --org <org>` "
            "then `applace push " + app + "`."
        )
        return report
    if not gitrepo.has_commits(root):
        report.reason = "there is nothing committed to push yet"
        return report

    try:
        link = github.require(paths)
        token = github.token(link)
    except (NotConnected, GitHubError) as exc:
        report.reason = str(exc)
        return report

    branch = base
    if link.reviewed:
        report.reviewed = True
        report.base = base
        try:
            # A pull request needs a base branch to exist, and a repository
            # Applace has just created has no branches at all. The birth commit
            # goes to the default branch for that reason, and everything after
            # it is proposed rather than pushed.
            born = gitrepo.fetch(root, url, base, token=token) is not None
        except gitrepo.GitError as exc:
            report.reason = str(exc)
            return report
        if born:
            branch = link.work_branch(app)
        else:
            report.reviewed = False

    report.branch = branch
    try:
        remote_head = gitrepo.fetch(root, url, branch, token=token)
        if remote_head and not gitrepo.contains(root, remote_head):
            report.diverged = True
            report.remote_commit = remote_head
            report.reason = DIVERGED.format(
                repo=report.repo, branch=branch, remote=remote_head[:12], path=root
            )
            return report
        gitrepo.push(root, url, branch, token=token)
    except gitrepo.GitError as exc:
        report.reason = str(exc)
        return report

    local_head = gitrepo.head(root)
    mark_pushed(conn, str(row["id"]))
    conn.commit()
    report.pushed = True
    report.commit = local_head.sha
    report.behind = 0

    if report.reviewed:
        _propose(link, report, row, head=branch, base=base)
    return report


def _propose(
    link: GitHubLink, report: PushReport, row: Any, *, head: str, base: str
) -> None:
    """Open the app's pull request, or find the one already open (D15).

    Never fatal. The commits are on GitHub either way, and an organisation that
    forbids pull requests from this token has a configuration problem, not a
    lost piece of work -- so it comes back as a sentence next to a successful
    push.
    """
    full_name = str(report.repo)
    try:
        report.pull_request = github.open_pull_request(
            link,
            full_name,
            head=head,
            base=base,
            title=PR_TITLE.format(name=str(row["name"])),
            body=PR_BODY.format(app=str(row["slug"]), base=base, head=head),
        )
    except GitHubError as exc:
        report.reason = (
            f"the commits are on {full_name} at {head}, but the pull request "
            f"could not be opened: {exc}"
        )
        return
    if report.pull_request is None:
        report.reason = (
            f"{head} holds nothing that {base} does not, so there is nothing to "
            f"propose yet."
        )
