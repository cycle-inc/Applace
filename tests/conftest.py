from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
from mcp.types import CallToolResult, TextContent

from applace import vercel
from applace.db import Connection, connect
from applace.github import GitHubLink
from applace.github import save as save_link
from applace.paths import ApplacePaths
from applace.vercel import VercelLink
from applace.vercel import save as save_vercel

# A stack that needs nothing but `true` on PATH. Almost every test wants to know
# what Applace does with a stack, not what npm does with a package.json, and
# real installs are minutes.
FAKE_STACK = """\
name: fake
title: A stack that installs nothing
description: For tests. Its install and build commands are no-ops.
runtime: none
commands:
  install: "true"
  dev: "true"
  build: "true"
  typecheck: "true"
manifest: manifest.json
dist: out
env_prefix: TEST_
entry: src/main.txt
deploy:
  - local
"""


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApplacePaths:
    """An isolated Applace home, so tests never touch the real ~/.applace."""
    home = tmp_path / "applace-home"
    monkeypatch.setenv("APPLACE_HOME", str(home))
    # A test machine's global git identity is not ours to assume, and neither is
    # its absence: pin both so `gitrepo.init` takes the same branch every time.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    p = ApplacePaths(home)
    p.create()
    return p


@pytest.fixture
def fake_stack(paths: ApplacePaths) -> Path:
    """Install the no-op stack into the home, with a small template tree."""
    root = paths.stacks / "fake"
    (root / "template" / "src").mkdir(parents=True)
    (root / "stack.yaml").write_text(FAKE_STACK, encoding="utf-8")
    (root / "template" / "manifest.json").write_text(
        '{"name": "{{app_slug}}"}\n', encoding="utf-8"
    )
    (root / "template" / "src" / "main.txt").write_text(
        "{{app_name}}: {{app_description}}\n", encoding="utf-8"
    )
    (root / "template" / "_gitignore").write_text("out/\n", encoding="utf-8")
    return root


@pytest.fixture
def home(paths: ApplacePaths, fake_stack: Path) -> Iterator[tuple[ApplacePaths, Connection]]:
    """A ready home: the layout, the database, and the no-op stack installed."""
    conn = connect(paths.db)
    try:
        yield paths, conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def npm() -> str:
    executable = shutil.which("npm")
    if executable is None:
        pytest.skip("npm is not on PATH")
    return executable


class FakeGitHub:
    """A GitHub that answers on localhost and whose repositories are real.

    Every repository it "creates" is an actual bare git repository in a
    temporary directory, and its `clone_url` is a `file://` URL pointing at it.
    That is what lets the push path be tested end to end -- fetch, divergence
    and all -- with git doing its own work rather than a mock agreeing with us.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repos: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.tokens: list[str] = []
        self.refuse_creation: str | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def api_base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def bare(self, full_name: str) -> Path:
        return self.root / f"{full_name.replace('/', '__')}.git"

    def create(self, full_name: str, *, private: bool = True) -> dict[str, Any]:
        bare = self.bare(full_name)
        if not bare.exists():
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(bare)],
                check=True, capture_output=True,
            )
        record = {
            "full_name": full_name,
            "name": full_name.split("/")[-1],
            "html_url": f"https://github.test/{full_name}",
            "clone_url": bare.as_uri(),
            "private": private,
            "default_branch": "main",
        }
        self.repos[full_name] = record
        return record

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                fake.tokens.append(header.removeprefix("Bearer "))
                return True

            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                if not self._authorised():
                    return self._reply(401, {"message": "Bad credentials"})
                if self.path.startswith("/orgs/") and self.path.count("/") == 2:
                    return self._reply(200, {"login": self.path.split("/")[2]})
                if self.path.startswith("/repos/"):
                    full_name = self.path.removeprefix("/repos/")
                    record = fake.repos.get(full_name)
                    if record is None:
                        return self._reply(404, {"message": "Not Found"})
                    return self._reply(200, record)
                self._reply(404, {"message": "Not Found"})

            def do_POST(self) -> None:
                fake.requests.append(("POST", self.path))
                payload = self._body()
                if not self._authorised():
                    return self._reply(401, {"message": "Bad credentials"})
                if fake.refuse_creation:
                    return self._reply(403, {"message": fake.refuse_creation})
                org = self.path.split("/")[2]
                full_name = f"{org}/{payload['name']}"
                if full_name in fake.repos:
                    return self._reply(
                        422,
                        {"message": "Repository creation failed",
                         "errors": [{"message": "name already exists on this account"}]},
                    )
                self._reply(201, fake.create(full_name, private=payload.get("private", True)))

            def do_PUT(self) -> None:
                fake.requests.append(("PUT", self.path))
                if not self._authorised():
                    return self._reply(401, {"message": "Bad credentials"})
                self._reply(204, {})

        return Handler


@pytest.fixture
def fake_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """A GitHub on localhost, with a token in the environment to reach it."""
    monkeypatch.setenv("APPLACE_GITHUB_TOKEN", "t0ken-for-tests")
    root = tmp_path / "github"
    root.mkdir()
    fake = FakeGitHub(root)
    try:
        yield fake
    finally:
        fake.close()


def connected(paths: ApplacePaths, fake: FakeGitHub, **overrides: Any) -> GitHubLink:
    """Connect the home to the fake GitHub, as `applace github connect` does."""
    link = GitHubLink(org="acme", api_base=fake.api_base, **overrides)
    save_link(paths, link)
    return link


class FakeVercel:
    """A Vercel that answers on localhost and remembers what it was asked.

    It keeps the shape of the real API -- projects by name, deployments that are
    not ready the instant they are created, env values it never gives back -- so
    a test can assert the two things that matter: that a deployment names a
    commit of the app's repository (D12), and that a value reached the provider
    without reaching anything an agent reads (D8).
    """

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, dict[str, Any]] = {}
        self.env: list[dict[str, Any]] = []
        self.uploads: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.tokens: list[str] = []
        # What every build ends as, and how many polls it takes to get there.
        self.ready_state = "READY"
        self.polls_before_ready = 1
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def api_base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def last_deployment(self) -> dict[str, Any]:
        return list(self.deployments.values())[-1]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            @property
            def route(self) -> str:
                return self.path.split("?", 1)[0]

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _raw(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length)

            def _reply(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                fake.tokens.append(header.removeprefix("Bearer "))
                return True

            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                if not self._authorised():
                    return self._reply(403, {"error": {"message": "Not authorized"}})
                if self.route == "/v2/user":
                    return self._reply(200, {"user": {"username": "acme-ci"}})
                if self.route.startswith("/v9/projects/"):
                    name = self.route.removeprefix("/v9/projects/")
                    record = fake.projects.get(name)
                    if record is None:
                        return self._reply(404, {"error": {"message": "Not found"}})
                    return self._reply(200, record)
                if self.route.startswith("/v13/deployments/"):
                    record = fake.deployments.get(self.route.split("/")[-1])
                    if record is None:
                        return self._reply(404, {"error": {"message": "Not found"}})
                    record["polls"] += 1
                    if record["polls"] >= fake.polls_before_ready:
                        record["readyState"] = fake.ready_state
                        if fake.ready_state == "ERROR":
                            record["errorMessage"] = "the build failed on Vercel"
                    return self._reply(200, record)
                if "/events" in self.route:
                    return self._reply(
                        200, {"data": [{"text": "npm run build"}, {"text": "error"}]}
                    )
                self._reply(404, {"error": {"message": "Not found"}})

            def do_POST(self) -> None:
                fake.requests.append(("POST", self.path))
                if not self._authorised():
                    return self._reply(403, {"error": {"message": "Not authorized"}})
                if self.route == "/v11/projects":
                    return self._reply(200, fake._create_project(self._body()))
                if self.route.endswith("/env"):
                    payload = self._body()
                    fake.env.extend(payload if isinstance(payload, list) else [payload])
                    return self._reply(200, {"created": payload})
                if self.route == "/v2/files":
                    fake.uploads.append(self.headers.get("x-vercel-digest", ""))
                    self._raw()
                    return self._reply(200, {})
                if self.route == "/v13/deployments":
                    return self._reply(200, fake._create_deployment(self._body()))
                self._reply(404, {"error": {"message": "Not found"}})

        return Handler

    def _create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload["name"])
        record: dict[str, Any] = {
            "id": f"prj_{name}",
            "name": name,
            "buildCommand": payload.get("buildCommand"),
            "outputDirectory": payload.get("outputDirectory"),
        }
        repository = payload.get("gitRepository")
        if repository:
            org, _, repo = str(repository["repo"]).partition("/")
            record["link"] = {
                "type": "github",
                "org": org,
                "repo": repo,
                "repoId": f"repo-{repo}",
            }
        self.projects[name] = record
        return record

    def _create_deployment(self, payload: dict[str, Any]) -> dict[str, Any]:
        identifier = f"dpl_{len(self.deployments) + 1}"
        record = {
            "id": identifier,
            "name": payload.get("name"),
            "readyState": "BUILDING",
            "url": f"{payload.get('name')}-{identifier}.vercel.app",
            "target": payload.get("target"),
            "alias": [f"{payload.get('name')}.vercel.app"],
            "inspectorUrl": f"https://vercel.test/{identifier}",
            "gitSource": payload.get("gitSource"),
            "files": payload.get("files", []),
            "polls": 0,
        }
        self.deployments[identifier] = record
        return record


@pytest.fixture
def fake_vercel(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeVercel]:
    """A Vercel on localhost, with a token in the environment to reach it."""
    monkeypatch.setenv("APPLACE_VERCEL_TOKEN", "vercel-t0ken")
    # The real interval is three seconds, which is right for a real build and
    # absurd for a test that finishes in milliseconds.
    monkeypatch.setattr(vercel, "POLL_INTERVAL", 0.01)
    fake = FakeVercel()
    try:
        yield fake
    finally:
        fake.close()


def on_vercel(paths: ApplacePaths, fake: FakeVercel, **overrides: Any) -> VercelLink:
    """Connect the home to the fake Vercel, as `applace vercel connect` does."""
    link = VercelLink(api_base=fake.api_base, **overrides)
    save_vercel(paths, link)
    return link


def call_tool(server: Any, tool: str, /, **arguments: Any) -> dict[str, Any]:
    """Call an MCP tool the way a host does, and return its structured result.

    Shared by every test that goes through the server rather than around it, so
    the unwrapping of a CallToolResult is written once.
    """
    result = asyncio.run(server.call_tool(tool, arguments))
    assert isinstance(result, CallToolResult), result
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return dict(result.structured_content)
    content = result.content[0]
    assert isinstance(content, TextContent), content
    return dict(json.loads(content.text))
