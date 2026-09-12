"""Vercel, reached through GitHub (D12).

The link is the product here. A Vercel project is attached to the app's
repository, and a deployment names a commit in that repository -- so what is
live is a sha a human can `git show`, the dashboard and the repository agree,
and anyone can redeploy without Applace. Uploading a directory of built files
would be quicker to write and would throw all of that away, so it exists only
for an app that has no repository at all.

As with GitHub, the token is never stored: it is read from the environment at
the moment it is needed, and `config.json` stays a file anyone can read.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .paths import ApplacePaths

API = "https://api.vercel.com"
TIMEOUT = 60.0

# How long to wait for a build, and how often to ask. Vercel builds a small Vite
# app in well under a minute; the ceiling is for a cold cache and a queue.
BUILD_TIMEOUT = 900.0
POLL_INTERVAL = 3.0

TOKEN_VARIABLES = ("APPLACE_VERCEL_TOKEN", "VERCEL_TOKEN")

NO_TOKEN = (
    "No Vercel token. Create one at https://vercel.com/account/tokens and put "
    "it in VERCEL_TOKEN, or run `applace vercel connect --token ...`."
)

# Vercel's own vocabulary for how a deployment ended.
DONE = {"READY", "ERROR", "CANCELED", "DELETED"}


class VercelError(Exception):
    """Vercel said no, or could not be reached. The message is Vercel's own."""


class NotConnected(VercelError):
    """No Vercel account is configured. `applace vercel connect` is the fix."""


@dataclass(frozen=True)
class VercelLink:
    """Which Vercel account deploys the apps on this machine."""

    team: str | None = None
    # Only set by tests and by a proxy in front of the API; derived otherwise.
    api_base: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"team": self.team, "api_base": self.api_base}

    @property
    def api(self) -> str:
        return (self.api_base or API).rstrip("/")

    def scoped(self, path: str) -> str:
        """Vercel takes the team as a query parameter on every single call."""
        if not self.team:
            return path
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}teamId={self.team}"


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    repo_id: str | None
    repo: str | None
    created: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "repo": self.repo,
                "created": self.created}


@dataclass(frozen=True)
class Build:
    """One Vercel deployment, as it stands right now."""

    id: str
    state: str
    url: str | None
    inspector: str | None = None
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.state in DONE

    @property
    def ok(self) -> bool:
        return self.state == "READY"


def load(paths: ApplacePaths) -> VercelLink | None:
    if not paths.config.exists():
        return None
    try:
        data = json.loads(paths.config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VercelError(f"{paths.config} is not readable JSON: {exc}") from exc
    section = data.get("vercel")
    if section is None:
        return None
    return VercelLink(team=section.get("team"), api_base=section.get("api_base"))


def require(paths: ApplacePaths) -> VercelLink:
    link = load(paths)
    if link is None:
        raise NotConnected(
            "Vercel is not connected on this machine. Run "
            "`applace vercel connect` once, then deploy."
        )
    return link


def save(paths: ApplacePaths, link: VercelLink) -> None:
    data: dict[str, Any] = {}
    if paths.config.exists():
        data = json.loads(paths.config.read_text(encoding="utf-8"))
    data["vercel"] = link.as_dict()
    paths.config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def token() -> str:
    for variable in TOKEN_VARIABLES:
        value = os.environ.get(variable)
        if value:
            return value.strip()
    raise VercelError(NO_TOKEN)


def api(
    link: VercelLink,
    method: str,
    path: str,
    payload: dict[str, Any] | list[Any] | None = None,
    *,
    auth: str | None = None,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One REST call. Returns the status and the body, and never raises for a
    status: 404 and 409 are answers this module acts on."""
    url = f"{link.api}{link.scoped(path)}"
    body = raw if raw is not None else (
        None if payload is None else json.dumps(payload).encode("utf-8")
    )
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {auth or token()}")
    if raw is None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except urllib.error.URLError as exc:
        raise VercelError(f"{link.api} could not be reached: {exc.reason}") from exc


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {"data": decoded}


def _message(body: dict[str, Any], fallback: str) -> str:
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        return f"{fallback}: {error['message']}"
    if body.get("message"):
        return f"{fallback}: {body['message']}"
    return fallback


# -- projects ---------------------------------------------------------------


def ensure_project(
    link: VercelLink,
    *,
    name: str,
    repo: str | None,
    settings: dict[str, Any],
    auth: str | None = None,
) -> Project:
    """The app's project, created on first deploy and reused after that.

    Creating it with `gitRepository` is what makes D12 true: from then on the
    project has a repository, and a deployment is a commit in it rather than a
    bundle Applace happened to upload.
    """
    auth = auth or token()
    existing = get_project(link, name, auth=auth)
    if existing is not None:
        return existing

    payload: dict[str, Any] = {"name": name, **settings}
    if repo:
        payload["gitRepository"] = {"type": "github", "repo": repo}
    status, body = api(link, "POST", "/v11/projects", payload, auth=auth)
    if status not in (200, 201):
        if status == 409:  # a race with another deploy; whoever won is fine
            found = get_project(link, name, auth=auth)
            if found is not None:
                return found
        raise VercelError(_message(body, f"Vercel refused to create project {name}"))
    return _project(body, created=True)


def get_project(
    link: VercelLink, name: str, *, auth: str | None = None
) -> Project | None:
    status, body = api(link, "GET", f"/v9/projects/{name}", auth=auth)
    if status == 404:
        return None
    if status >= 400:
        raise VercelError(_message(body, f"Vercel refused to read project {name}"))
    return _project(body, created=False)


def _project(body: dict[str, Any], *, created: bool) -> Project:
    connection = body.get("link") or {}
    repo = None
    if connection.get("org") and connection.get("repo"):
        repo = f"{connection['org']}/{connection['repo']}"
    repo_id = connection.get("repoId")
    return Project(
        id=str(body.get("id") or ""),
        name=str(body.get("name") or ""),
        repo_id=None if repo_id is None else str(repo_id),
        repo=repo,
        created=created,
    )


# -- environment variables (D8) ---------------------------------------------


def sync_env(
    link: VercelLink,
    project: Project,
    variables: dict[str, str],
    *,
    targets: list[str],
    auth: str | None = None,
) -> list[str]:
    """Upload the app's declared values, and return only their **names**.

    The values pass from the human's `~/.applace/env/<app>.env` to Vercel
    without ever being a return value, a log line or a tool result (D8). What
    the caller gets back is the list of names, which is what it may print.
    """
    auth = auth or token()
    if not variables:
        return []
    payload = [
        {
            "key": name,
            "value": value,
            "type": "encrypted",
            "target": targets,
        }
        for name, value in sorted(variables.items())
    ]
    status, body = api(
        link,
        "POST",
        f"/v10/projects/{project.id}/env?upsert=true",
        payload,
        auth=auth,
    )
    if status >= 400:
        raise VercelError(_message(body, "Vercel refused the environment variables"))
    return sorted(variables)


# -- deployments ------------------------------------------------------------


def deploy_commit(
    link: VercelLink,
    project: Project,
    *,
    repo_id: str,
    ref: str,
    sha: str,
    production: bool,
    auth: str | None = None,
) -> Build:
    """Ask Vercel to build a commit of the linked repository (D12)."""
    payload: dict[str, Any] = {
        "name": project.name,
        "project": project.id,
        "gitSource": {"type": "github", "repoId": repo_id, "ref": ref, "sha": sha},
    }
    if production:
        payload["target"] = "production"
    status, body = api(link, "POST", "/v13/deployments", payload, auth=auth)
    if status not in (200, 201, 202):
        raise VercelError(_message(body, f"Vercel refused to deploy {sha[:12]}"))
    return _build(body)


def deploy_files(
    link: VercelLink,
    project: Project,
    root: Path,
    *,
    production: bool,
    auth: str | None = None,
) -> Build:
    """The fallback for an app with no repository: upload the built directory.

    Deliberately second-class. Nothing about what goes live is recoverable from
    git afterwards, so an app that has a GitHub repository never comes here.
    """
    auth = auth or token()
    files: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        content = path.read_bytes()
        digest = hashlib.sha1(content).hexdigest()
        status, body = api(
            link,
            "POST",
            "/v2/files",
            auth=auth,
            raw=content,
            headers={
                "Content-Type": "application/octet-stream",
                "x-vercel-digest": digest,
                "Content-Length": str(len(content)),
            },
        )
        if status >= 400:
            raise VercelError(
                _message(body, f"Vercel refused the upload of {path.name}")
            )
        files.append(
            {
                "file": str(path.relative_to(root)),
                "sha": digest,
                "size": len(content),
            }
        )

    payload: dict[str, Any] = {
        "name": project.name,
        "project": project.id,
        "files": files,
        # Nothing to build: what is uploaded is already the build output.
        "projectSettings": {"framework": None, "buildCommand": None,
                            "outputDirectory": None, "installCommand": None},
    }
    if production:
        payload["target"] = "production"
    status, body = api(link, "POST", "/v13/deployments", payload, auth=auth)
    if status not in (200, 201, 202):
        raise VercelError(_message(body, "Vercel refused the upload"))
    return _build(body)


def get_build(link: VercelLink, build_id: str, *, auth: str | None = None) -> Build:
    status, body = api(link, "GET", f"/v13/deployments/{build_id}", auth=auth)
    if status >= 400:
        raise VercelError(_message(body, f"Vercel refused to report on {build_id}"))
    return _build(body)


def wait(
    link: VercelLink,
    build: Build,
    *,
    timeout: float | None = None,
    interval: float | None = None,
    auth: str | None = None,
) -> Build:
    """Poll until the build is done, or until the wait is longer than useful.

    A deploy that is not waited on is a deploy nobody can report on: D12 says
    `deploy_app` triggers the build *and waits for its outcome*, because "I
    asked Vercel" is not the same fact as "it is live".

    The two timings are read here rather than defaulted in the signature, so
    that setting them on the module -- which is what a test does -- is obeyed.
    """
    timeout = BUILD_TIMEOUT if timeout is None else timeout
    interval = POLL_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    current = build
    while not current.done:
        if time.monotonic() > deadline:
            return Build(
                id=current.id,
                state="TIMEOUT",
                url=current.url,
                inspector=current.inspector,
                error=(
                    f"the build was still {current.state.lower()} after "
                    f"{int(timeout)}s. It may still finish; look at the inspector."
                ),
            )
        time.sleep(interval)
        current = get_build(link, current.id, auth=auth)
    return current


def _build(body: dict[str, Any]) -> Build:
    state = str(body.get("readyState") or body.get("status") or "QUEUED").upper()
    url = body.get("url")
    # Production deployments answer on their aliases; `url` is the immutable
    # per-build hostname, which is the right thing for a preview.
    aliases = body.get("alias") or []
    if body.get("target") == "production" and isinstance(aliases, list) and aliases:
        url = aliases[0]
    error = body.get("errorMessage") or (body.get("error") or {}).get("message")
    return Build(
        id=str(body.get("id") or body.get("uid") or ""),
        state=state,
        url=None if not url else f"https://{url}",
        inspector=body.get("inspectorUrl"),
        error=None if error is None else str(error),
    )


def logs(
    link: VercelLink, build_id: str, *, auth: str | None = None, limit: int = 60
) -> list[str]:
    """The tail of a build's own log, which is where a failure says why."""
    status, body = api(
        link, "GET", f"/v2/deployments/{build_id}/events?limit={limit}", auth=auth
    )
    if status >= 400:
        return []
    events = body.get("data") if isinstance(body.get("data"), list) else []
    lines: list[str] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        text = event.get("text") or (event.get("payload") or {}).get("text")
        if text:
            lines.append(str(text).rstrip())
    return lines[-limit:]


def _files(root: Path) -> Iterator[Path]:  # pragma: no cover - used by tests only
    yield from (path for path in sorted(root.rglob("*")) if path.is_file())


__all__ = [
    "Build",
    "NotConnected",
    "Project",
    "VercelError",
    "VercelLink",
    "deploy_commit",
    "deploy_files",
    "ensure_project",
    "get_build",
    "get_project",
    "load",
    "logs",
    "require",
    "save",
    "sync_env",
    "token",
    "wait",
]
