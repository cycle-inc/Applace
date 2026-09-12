"""Where Applace keeps its state on the user's machine.

Every path derives from the Applace home directory, which is ``~/.applace``
unless ``APPLACE_HOME`` is set. Tests set that variable so they never touch the
real one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplacePaths:
    home: Path

    @property
    def db(self) -> Path:
        return self.home / "applace.db"

    @property
    def config(self) -> Path:
        return self.home / "config.json"

    @property
    def policy(self) -> Path:
        return self.home / "policy.yaml"

    @property
    def apps(self) -> Path:
        """One ordinary git repository per app (D1)."""
        return self.home / "apps"

    @property
    def stacks(self) -> Path:
        """Stacks installed from a git URL. The built-in ones ship in the package."""
        return self.home / "stacks"

    @property
    def env(self) -> Path:
        """Secret values, deliberately outside every repository (D8)."""
        return self.home / "env"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def serve(self) -> Path:
        """What the `local` deploy target has live: a copy of a built `dist/`.

        A copy, not the app's own `dist/`, because the next build overwrites
        that one -- and a deployment that changes when someone runs a build is
        not a deployment.
        """
        return self.home / "serve"

    @property
    def work(self) -> Path:
        """Scratch checkouts of older commits, made and removed by a deploy."""
        return self.home / "work"

    @property
    def shots(self) -> Path:
        """The last picture taken of each app, kept for whoever asks next.

        The agent gets the image in its tool result and forgets it; a chat
        window showing an app that is not running has nothing else to show.
        """
        return self.home / "shots"

    def app(self, slug: str) -> Path:
        return self.apps / slug

    def served(self, slug: str, environment: str) -> Path:
        return self.serve / slug / environment

    def env_file(self, slug: str) -> Path:
        return self.env / f"{slug}.env"

    def app_logs(self, slug: str) -> Path:
        return self.logs / slug

    def shot(self, slug: str) -> Path:
        return self.shots / f"{slug}.png"

    def create(self) -> None:
        """Create the directory layout. Safe to call on an existing home."""
        for directory in (self.home, self.apps, self.stacks, self.env, self.logs,
                          self.serve, self.shots):
            directory.mkdir(parents=True, exist_ok=True)
        # The env directory holds plaintext tokens. Nothing else on the machine
        # has any business reading it, and a home created by `init` is the only
        # chance we get to say so.
        self.env.chmod(0o700)


def applace_home() -> Path:
    override = os.environ.get("APPLACE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".applace"


def paths() -> ApplacePaths:
    return ApplacePaths(applace_home())
