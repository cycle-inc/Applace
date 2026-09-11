"""M1's acceptance, against the real stack: npm, node and all.

The claim being checked is D1 -- that what Applace produces is an ordinary
repository. So this test creates an app through Applace and then does
everything else *by hand*, the way a developer who had never heard of Applace
would.

Slow (a cold npm install is a minute or more) and online, hence the marker.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from applace import gitrepo
from applace.apps import create_app
from applace.db import Connection
from applace.paths import ApplacePaths

pytestmark = pytest.mark.needs_npm

Home = tuple[ApplacePaths, Connection]


@pytest.fixture
def node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("node is not on PATH")
    return executable


def test_a_real_app_is_installed_committed_and_builds_by_hand(
    home: Home, npm: str, node: str
) -> None:
    paths, conn = home
    report = create_app(paths, conn, name="Team Dashboard", install=True)

    assert report.stack == "vite-react-ts"
    assert report.installed, report.warnings
    assert (report.path / "node_modules").is_dir()

    # One commit, holding the source and the lockfile -- and not node_modules.
    assert len(gitrepo.log(report.path)) == 1
    assert not gitrepo.is_dirty(report.path)
    tracked = gitrepo.tracked_files(report.path)
    assert "package-lock.json" in tracked
    assert "src/App.tsx" in tracked
    assert not any(f.startswith("node_modules/") for f in tracked)

    # The app's own name reached the files a human will read first.
    assert "Team Dashboard" in (report.path / "index.html").read_text()
    assert "Team Dashboard" in (report.path / "src" / "App.tsx").read_text()

    # And now, by hand: no Applace involved.
    for script in ("typecheck", "lint", "build"):
        completed = subprocess.run(
            [npm, "run", script],
            cwd=report.path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"npm run {script}\n{completed.stderr}"

    built: Path = report.path / "dist" / "index.html"
    assert built.is_file()
    assert "Team Dashboard" in built.read_text()
    # The build output is not the app: it stayed out of the repository.
    assert not gitrepo.is_dirty(report.path)
