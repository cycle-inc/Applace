from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Iterator

import pytest
from mcp.types import CallToolResult, TextContent

from applace.db import Connection, connect
from applace.paths import ApplacePaths

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
