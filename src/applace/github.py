"""GitHub: where the company's code lives (D2, D2b).

Every app is a real repository on real GitHub from birth, not an export at the
end. This module holds the organisation-level connection, creates repositories
through the REST API, and resolves the token to push with.

Two things are deliberate. The connection is **organisation-level**: it is
answered once, by a human, and every app follows it (D2b) -- an agent never
chooses where code lands. And the token is **never stored here**: it is read
from the environment or from `gh` at the moment it is used, so `config.json`
stays a file anyone can read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .paths import ApplacePaths

DEFAULT_HOST = "github.com"
DEFAULT_VISIBILITY = "private"
VISIBILITIES = ("private", "internal", "public")
API_VERSION = "2022-11-28"
TIMEOUT = 30.0

# Checked in order. The first two are how CI and containers pass a token; the
# last is what a developer's laptop already has.
TOKEN_VARIABLES = ("APPLACE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

NO_TOKEN = (
    "No GitHub token. Either `gh auth login` (Applace will use `gh auth token`), "
    "or set GITHUB_TOKEN to a token with `repo` scope in the organisation."
)


class GitHubError(Exception):
    """GitHub said no, or could not be reached. The message is GitHub's own."""


class NotConnected(GitHubError):
    """No organisation has been connected. `applace github connect` is the fix."""


@dataclass(frozen=True)
class GitHubLink:
    """The one connection, shared by every app (D2b)."""

    org: str
    visibility: str = DEFAULT_VISIBILITY
    prefix: str = ""
    team: str | None = None
    host: str = DEFAULT_HOST
    user: str | None = None
    # Only set by tests and by an unusual enterprise layout; derived otherwise.
    api_base: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "org": self.org,
            "visibility": self.visibility,
            "prefix": self.prefix,
            "team": self.team,
            "host": self.host,
            "user": self.user,
            "api_base": self.api_base,
        }

    @property
    def api(self) -> str:
        if self.api_base:
            return self.api_base.rstrip("/")
        if self.host == DEFAULT_HOST:
            return "https://api.github.com"
        # GitHub Enterprise Server, which is what a company that is not on
        # github.com is running.
        return f"https://{self.host}/api/v3"

    def repo_name(self, slug: str) -> str:
        return f"{self.prefix}{slug}"

    def full_name(self, slug: str) -> str:
        return f"{self.org}/{self.repo_name(slug)}"


@dataclass(frozen=True)
class Repository:
    full_name: str
    html_url: str
    clone_url: str
    private: bool
    default_branch: str
    # False when the repository was already there and Applace adopted it.
    created: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.full_name,
            "github_url": self.html_url,
            "private": self.private,
            "default_branch": self.default_branch,
            "created": self.created,
        }


def load(paths: ApplacePaths) -> GitHubLink | None:
    """The connection, or None when no one has connected an organisation yet."""
    if not paths.config.exists():
        return None
    try:
        data = json.loads(paths.config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GitHubError(f"{paths.config} is not readable JSON: {exc}") from exc
    section = data.get("github")
    if not section:
        return None
    known = {field: section.get(field) for field in GitHubLink.__annotations__}
    known = {key: value for key, value in known.items() if value is not None}
    return GitHubLink(**known)


def require(paths: ApplacePaths) -> GitHubLink:
    link = load(paths)
    if link is None:
        raise NotConnected(
            "No GitHub organisation is connected. Run "
            "`applace github connect --org <org>` once, and every app follows it."
        )
    return link


def save(paths: ApplacePaths, link: GitHubLink) -> None:
    """Write the connection, keeping whatever else lives in config.json.

    Changing it never touches an app that already exists (D2b): the remotes are
    in the repositories, and rewriting them behind a developer's back would be
    the kind of surprise this harness exists to avoid.
    """
    data: dict[str, Any] = {}
    if paths.config.exists():
        data = json.loads(paths.config.read_text(encoding="utf-8"))
    data["github"] = link.as_dict()
    paths.config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def token(link: GitHubLink) -> str:
    """The token to talk to GitHub with, found at the moment it is needed."""
    for variable in TOKEN_VARIABLES:
        value = os.environ.get(variable)
        if value:
            return value.strip()
    found = _gh_token(link)
    if found:
        return found
    raise GitHubError(NO_TOKEN)


def _gh_token(link: GitHubLink) -> str | None:
    """Ask the `gh` CLI, naming the account and host the connection was made for.

    Naming the user matters: a laptop logged into both a personal account and a
    company one hands out whichever `gh` last called active, and the wrong one
    fails with a 403 that reads like a permissions problem rather than an
    identity one.
    """
    if shutil.which("gh") is None:
        return None
    command = ["gh", "auth", "token", "--hostname", link.host]
    if link.user:
        command += ["--user", link.user]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def api(
    link: GitHubLink,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    auth: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """One REST call. Returns the status and the decoded body, and never raises
    for a status: 404 and 422 are answers this module acts on."""
    url = path if path.startswith("http") else f"{link.api}{path}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {auth or token(link)}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", API_VERSION)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except urllib.error.URLError as exc:
        raise GitHubError(f"{link.api} could not be reached: {exc.reason}") from exc


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {"data": decoded}


def get_repository(link: GitHubLink, full_name: str, *, auth: str | None = None) -> Repository | None:
    status, body = api(link, "GET", f"/repos/{full_name}", auth=auth)
    if status == 404:
        return None
    if status >= 400:
        raise GitHubError(_message(body, f"GitHub refused to read {full_name}"))
    return _repository(body, created=False)


def create_repository(
    link: GitHubLink,
    slug: str,
    *,
    description: str | None = None,
    auth: str | None = None,
) -> Repository:
    """Create the app's repository in the connected organisation.

    A name that is already taken is adopted rather than refused: an app being
    re-created after a local mishap should land back on its own history, and
    Applace never deletes a repository to make room.
    """
    auth = auth or token(link)
    full_name = link.full_name(slug)
    status, body = api(
        link,
        "POST",
        f"/orgs/{link.org}/repos",
        {
            "name": link.repo_name(slug),
            "description": description or "",
            "private": link.visibility != "public",
            "visibility": link.visibility,
            "auto_init": False,
        },
        auth=auth,
    )
    if status in (200, 201):
        repository = _repository(body, created=True)
    elif status == 422:
        existing = get_repository(link, full_name, auth=auth)
        if existing is None:
            raise GitHubError(_message(body, f"GitHub refused to create {full_name}"))
        repository = existing
    elif status in (401, 403):
        raise GitHubError(
            _message(
                body,
                f"the token is not allowed to create repositories in {link.org}",
            )
        )
    else:
        raise GitHubError(_message(body, f"GitHub refused to create {full_name}"))

    if link.team:
        grant_team(link, repository.full_name, auth=auth)
    return repository


def grant_team(link: GitHubLink, full_name: str, *, auth: str | None = None) -> bool:
    """Give the configured team write access. A failure here is not fatal.

    The repository exists and the code is safe; a team that could not be granted
    is a message to a human, not a reason to lose the app.
    """
    if not link.team:
        return False
    status, _ = api(
        link,
        "PUT",
        f"/orgs/{link.org}/teams/{link.team}/repos/{full_name}",
        {"permission": "push"},
        auth=auth,
    )
    return status < 300


def _repository(body: dict[str, Any], *, created: bool) -> Repository:
    full_name = str(body.get("full_name") or body.get("name") or "")
    return Repository(
        full_name=full_name,
        html_url=str(body.get("html_url") or ""),
        clone_url=str(body.get("clone_url") or ""),
        private=bool(body.get("private", True)),
        default_branch=str(body.get("default_branch") or "main"),
        created=created,
    )


def _message(body: dict[str, Any], fallback: str) -> str:
    detail = body.get("message")
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("message"):
            detail = f"{detail}: {first['message']}" if detail else str(first["message"])
    return f"{fallback}: {detail}" if detail else fallback
