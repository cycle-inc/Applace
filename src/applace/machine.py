"""Many people, one machine (M12).

A chatbot serving a company serves thousands of people, and the way to keep
their work apart is one Applace home per person under a root -- not a
``user_id`` column on every table (D20). Each home is a complete single-user
Applace: its own database, its own git repositories, its own ``env/`` at
``0700``. Isolation is then the filesystem's job rather than a ``WHERE`` clause
everybody has to remember.

What the homes share is what there is only one of: this machine's ports and
this machine's disk (D21). Both go through the root's ledger -- one SQLite file
holding who holds which port and what each home has spent -- and a port is
claimed inside a single transaction before anything binds, because probing a
port and then binding it is a race that shows up later as a dev server that
died for no reason.

Applace authenticates nobody. The caller says who the user is and is answerable
for having asked; this module only makes sure that answer lands in its own
directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from . import register
from .db import enable_wal, now_iso
from .paths import ApplacePaths

if TYPE_CHECKING:  # the Machine hands back an Applace; api.py imports this file
    from .api import Applace

LEDGER = "machine.db"
CONFIG = "machine.yaml"
USERS = "users"

# A claim nobody has started yet. Long enough for `npm install` to be irrelevant
# (the port is claimed just before the spawn), short enough that a crash between
# the two does not hold a port until the next reboot.
UNSTARTED_GRACE = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS ports (
    port     INTEGER PRIMARY KEY,
    home     TEXT NOT NULL,
    slug     TEXT NOT NULL,
    kind     TEXT NOT NULL,          -- preview | deploy
    pid      INTEGER,                -- NULL until the process is spawned
    taken_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spend (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    home TEXT NOT NULL,
    act  TEXT NOT NULL,              -- write | deploy | shot
    at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS spend_by_home ON spend(home, act, at);

CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,     -- exactly what the caller said
    home       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class MachineError(Exception):
    """The root, its configuration, or the identity given for a user is wrong."""


# -- limits (D22) ----------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """What one home may have and may do. Generous by default; a root says less.

    ``None`` anywhere means "no limit", which is what a single-user machine
    gets: `Applace()` on its own is not policed by anything here (D11).
    """

    apps: int | None = 20
    previews: int | None = 2
    disk_mb: int | None = 4000
    writes_per_hour: int | None = 240
    deploys_per_hour: int | None = 30
    shots_per_hour: int | None = 300

    RATES = {"write": "writes_per_hour", "deploy": "deploys_per_hour",
             "shot": "shots_per_hour"}

    def rate_for(self, act: str) -> int | None:
        field = self.RATES.get(act)
        return getattr(self, field) if field else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "apps": self.apps,
            "previews": self.previews,
            "disk_mb": self.disk_mb,
            "writes_per_hour": self.writes_per_hour,
            "deploys_per_hour": self.deploys_per_hour,
            "shots_per_hour": self.shots_per_hour,
        }


def limits_from(path: Path) -> Limits:
    """Read ``machine.yaml``. Absent is the defaults; broken is an error.

    The same stance as the policy file: a configuration that cannot be read is
    reported, never quietly replaced by something more permissive.
    """
    if not path.exists():
        return Limits()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise MachineError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise MachineError(f"{path} must be a mapping of settings.")
    given: dict[str, Any] = dict(raw.get("limits") or {})
    known = Limits().as_dict()
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise MachineError(
            f"{path}: unknown limit {', '.join(unknown)}. "
            f"Known limits are {', '.join(sorted(known))}."
        )
    for name, value in given.items():
        if value is not None and (not isinstance(value, int) or value < 0):
            raise MachineError(
                f"{path}: {name} must be a whole number of "
                f"{'megabytes' if name.endswith('_mb') else 'things'}, or null "
                f"for no limit. Got {value!r}."
            )
    return Limits(**{**known, **given})


# -- the ledger (D21) ------------------------------------------------------


def connect(path: Path) -> sqlite3.Connection:
    """Open the machine ledger, creating it if this is the first caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Several processes, one file: the same reasoning as the per-home database,
    # and the same care about the switch to WAL, which takes an exclusive lock
    # on a machine's first second -- when two people arrive at once.
    conn.execute("PRAGMA busy_timeout = 10000")
    enable_wal(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def alive(pid: int | None) -> bool:
    """Is that process still there? A pid we may not signal is somebody else's."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # running, and not ours
        return True
    # A pid that accepts a signal can still be a zombie -- an exit status nobody
    # has collected yet -- and a zombie is not holding a port. Only `ps` tells
    # the two apart, which is why the preview supervisor already asks it; the
    # question is the same one, so the answer is the same code.
    from .preview import process_command  # imported here: preview imports us

    return process_command(pid) is not None


def reap(conn: sqlite3.Connection) -> int:
    """Release every claim whose process is gone. Returns how many.

    This is the whole lifecycle of a claim: nothing has to remember to give a
    port back, because the truth about a port is the process holding it. A row
    with no pid yet is given a grace period and then treated the same way -- it
    means a claim was made and the spawn never happened.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=UNSTARTED_GRACE)
    gone: list[int] = []
    for row in conn.execute("SELECT port, pid, taken_at FROM ports"):
        pid = row["pid"]
        if pid is None:
            if _parse(str(row["taken_at"])) < cutoff:
                gone.append(int(row["port"]))
        elif not alive(int(pid)):
            gone.append(int(row["port"]))
    for port in gone:
        conn.execute("DELETE FROM ports WHERE port = ?", (port,))
    if gone:
        conn.commit()
    return len(gone)


def claim(
    paths: ApplacePaths,
    candidates: Iterable[int],
    *,
    slug: str,
    kind: str,
    free: Callable[[int], bool],
) -> int | None:
    """Take the first port nothing on this machine holds, atomically.

    Returns None when every candidate is taken, which the caller reports in its
    own words. On a machine with no ledger -- one home, one person -- there is
    nothing to arbitrate and this answers None too, leaving the caller's own
    probe in charge.
    """
    if paths.ledger is None:
        return None
    conn = connect(paths.ledger)
    try:
        reap(conn)
        # IMMEDIATE: the write lock is taken before the read, so two processes
        # cannot both see the same port free. The probe inside the transaction
        # is what makes the claim true about the machine rather than about the
        # ledger, and it costs microseconds.
        conn.execute("BEGIN IMMEDIATE")
        taken = {int(row["port"]) for row in conn.execute("SELECT port FROM ports")}
        for port in candidates:
            if port in taken or not free(port):
                continue
            conn.execute(
                "INSERT INTO ports(port, home, slug, kind, pid, taken_at) "
                "VALUES(?, ?, ?, ?, NULL, ?)",
                (port, str(paths.home), slug, kind, now_iso()),
            )
            conn.commit()
            return port
        conn.rollback()
        return None
    finally:
        conn.close()


def started(paths: ApplacePaths, port: int, pid: int) -> None:
    """Say which process took the port. Until this, the claim is on a grace timer."""
    if paths.ledger is None:
        return
    conn = connect(paths.ledger)
    try:
        conn.execute("UPDATE ports SET pid = ? WHERE port = ?", (pid, port))
        conn.commit()
    finally:
        conn.close()


def held(ledger: Path) -> list[dict[str, Any]]:
    """Every port this machine is holding, after releasing the dead ones."""
    conn = connect(ledger)
    try:
        reap(conn)
        return [
            {
                "port": int(row["port"]),
                "home": str(row["home"]),
                "app": str(row["slug"]),
                "kind": str(row["kind"]),
                "pid": int(row["pid"]) if row["pid"] is not None else None,
                "taken_at": str(row["taken_at"]),
            }
            for row in conn.execute("SELECT * FROM ports ORDER BY port")
        ]
    finally:
        conn.close()


# -- what a home has spent (D22) -------------------------------------------


def spend(paths: ApplacePaths, act: str) -> None:
    """Record one act against this home. Free and silent when there is no root."""
    if paths.ledger is None:
        return
    conn = connect(paths.ledger)
    try:
        conn.execute(
            "INSERT INTO spend(home, act, at) VALUES(?, ?, ?)",
            (str(paths.home), act, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def spent(paths: ApplacePaths, act: str, *, seconds: float = 3600.0) -> int:
    """How many of those this home has done in the last window."""
    if paths.ledger is None:
        return 0
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")
    conn = connect(paths.ledger)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM spend WHERE home = ? AND act = ? AND at >= ?",
            (str(paths.home), act, since),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def frees_in(paths: ApplacePaths, act: str, *, seconds: float = 3600.0) -> int:
    """Seconds until this home's oldest act of that kind falls out of the window.

    What a caller told to wait actually has to wait, rather than a round number
    somebody guessed.
    """
    if paths.ledger is None:
        return 0
    start = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    conn = connect(paths.ledger)
    try:
        row = conn.execute(
            "SELECT MIN(at) AS oldest FROM spend "
            "WHERE home = ? AND act = ? AND at >= ?",
            (str(paths.home), act, start.isoformat(timespec="seconds")),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["oldest"] is None:
        return 0
    left = _parse(str(row["oldest"])) + timedelta(seconds=seconds)
    return max(1, int((left - datetime.now(timezone.utc)).total_seconds()) + 1)


def forget(ledger: Path, *, seconds: float = 7 * 24 * 3600.0) -> int:
    """Drop spend older than the longest window anybody counts. Returns how many."""
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")
    conn = connect(ledger)
    try:
        done = conn.execute("DELETE FROM spend WHERE at < ?", (since,))
        conn.commit()
        return done.rowcount or 0
    finally:
        conn.close()


# -- whose home this is ----------------------------------------------------
#
# Until M17 a home was anonymous: the root ledger knew which id each directory
# belonged to, and the home itself knew nothing. That was enough while the only
# thing a home did with an identity was live in its own directory. A delegated
# call is not that (D28): previewing one means saying, to the company's own
# exchange, *who* the call is for, and the gateway runs inside the home with no
# ledger in reach. So the home keeps the answer, in the same `config.json` the
# GitHub link lives in, and only `Machine.user()` ever writes it.

WHOSE = "whose"


def whose(paths: ApplacePaths) -> str:
    """The user id this home belongs to, or "" for a home nobody named.

    Empty is a real answer, not a failure: a developer's own `~/.applace` was
    never created by a `Machine` and belongs to whoever is at the keyboard.
    What depends on it says so rather than guessing.
    """
    try:
        data = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get(WHOSE) or "") if isinstance(data, dict) else ""


def remember_whose(paths: ApplacePaths, user: str) -> str:
    """Record whose home this is. Once: a home does not change hands.

    Idempotent so `Machine.user()` can call it on every request, which is also
    what gives a home made before M17 its id the next time its owner appears.
    Refusing to overwrite is the control: nothing else in Applace may say that
    somebody else's home is theirs.
    """
    if not user or not user.strip():
        raise MachineError("a user id cannot be empty.")
    data: dict[str, Any] = {}
    if paths.config.exists():
        try:
            loaded = json.loads(paths.config.read_text(encoding="utf-8"))
        except ValueError:
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    already = str(data.get(WHOSE) or "")
    if already:
        return already
    data[WHOSE] = user
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return user


# -- the root --------------------------------------------------------------


def folder(user: str) -> str:
    """A directory name for an opaque user id: safe, stable, and never a surprise.

    The id is whatever the caller's own system calls a person -- an email, a
    UUID, a display name in any script. Two ids that differ only in characters a
    directory name cannot keep must not become one directory, so every name
    carries a digest of the id it came from; the readable part in front is a
    courtesy to whoever reads `ls`.
    """
    if not user or not user.strip():
        raise MachineError("a user id cannot be empty.")
    digest = hashlib.sha256(user.encode("utf-8")).hexdigest()[:10]
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", user).strip("-.")[:40].lower()
    return f"{readable}-{digest}" if readable else digest


class Machine:
    """A root holding one Applace home per person (D20).

        machine = Machine("/srv/applace")
        ap = machine.user("alice@example.com")
        ap.create("Team Dashboard")

    Everything a single-user Applace does, each of them does, in its own home.
    What the root adds is the arbitration: ports that cannot collide, limits
    that refuse rather than queue, and a collection that stops processes without
    ever deleting anybody's code.
    """

    def __init__(self, root: Path | str, *, limits: Limits | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / USERS).mkdir(exist_ok=True)
        self._limits = limits

    def __repr__(self) -> str:
        return f"Machine(root={str(self.root)!r})"

    @property
    def ledger(self) -> Path:
        return self.root / LEDGER

    @property
    def config(self) -> Path:
        return self.root / CONFIG

    @property
    def limits(self) -> Limits:
        """Read every time: an operator editing machine.yaml expects it to take."""
        return self._limits if self._limits is not None else limits_from(self.config)

    def home_of(self, user: str) -> ApplacePaths:
        """Where that user's Applace lives. Pure: it creates nothing."""
        return ApplacePaths(self.root / USERS / folder(user), ledger=self.ledger)

    def user(self, user: str) -> Applace:
        """That person's Applace, made on first use.

        Called on every request in a backend, so it is cheap and idempotent: the
        directories already exist the second time, and the row in the ledger is
        what lets `users()` show the id the caller actually used rather than a
        directory name.
        """
        from .api import Applace  # here, not at the top: api.py imports this file

        paths = self.home_of(user)
        first = not paths.db.exists()
        applace = Applace(paths, limits=self.limits)
        # Every time, not only the first: a home made before M17 has no id in
        # it, and the cost is one read of a hundred-byte file (D28).
        remember_whose(paths, user)
        if first:
            conn = connect(self.ledger)
            try:
                conn.execute(
                    "INSERT INTO users(id, home, created_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (user, str(paths.home), now_iso()),
                )
                conn.commit()
            finally:
                conn.close()
        return applace

    def homes(self) -> list[dict[str, Any]]:
        """Everyone with a home here, and where it is. Counts nothing.

        The cheap half of `users()`: one query against the ledger, no walk of
        anybody's disk. Every listing that is about the apps rather than about
        the quota reads this, because a page that stats four hundred thousand
        files of node_modules to draw a heading is a page nobody opens twice.
        """
        conn = connect(self.ledger)
        try:
            rows = list(conn.execute("SELECT * FROM users ORDER BY created_at"))
        finally:
            conn.close()
        return [
            {
                "user": str(row["id"]),
                "home": str(row["home"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def users(self) -> list[dict[str, Any]]:
        """Everyone with a home here, and how much of it they are using."""
        conn = connect(self.ledger)
        try:
            rows = list(conn.execute("SELECT * FROM users ORDER BY created_at"))
        finally:
            conn.close()
        return [
            {
                "user": str(row["id"]),
                "home": str(row["home"]),
                "created_at": str(row["created_at"]),
                **self._counts(Path(str(row["home"]))),
            }
            for row in rows
        ]

    def usage(self, user: str) -> dict[str, Any]:
        """One person: what they have, what they have spent, what they may do."""
        paths = self.home_of(user)
        limits = self.limits
        return {
            "ok": True,
            "user": user,
            "home": str(paths.home),
            **self._counts(paths.home),
            "spent": {
                act: spent(paths, act) for act in ("write", "deploy", "shot")
            },
            "limits": limits.as_dict(),
        }

    def ports(self) -> list[dict[str, Any]]:
        """Who holds which port, across every home (D21)."""
        return held(self.ledger)

    # -- the register (D27) ------------------------------------------------
    #
    # Three questions a person who runs the machine has and nobody living in a
    # home can answer: what is here, what was allowed to happen, and what it
    # cost. All three read every journal read-only and open nobody's files.

    def apps(self, *, user: str | None = None) -> list[dict[str, Any]]:
        """Every app of every home, with whose it is.

        No `git`, no `stat`, no directory walk: on a machine with four hundred
        apps a listing that shells out once per app is a listing nobody runs
        twice, and one that runs git inside a checkout somebody is working in
        can take their index lock. `dirty` is a question for the home that owns
        the app; this answers what the journal knows.
        """
        found: list[dict[str, Any]] = []
        for who, paths in self._homes(user):
            found.extend(
                {"user": who, "home": str(paths.home), **entry}
                for entry in self._read(paths, register.apps)
            )
        found.sort(key=lambda entry: (str(entry["user"]), str(entry["app"])))
        return found

    def audit(
        self,
        *,
        user: str | None = None,
        since: str | None = None,
        until: str | None = None,
        kinds: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """What happened here that somebody may later be asked about (D27).

        Newest first, because the question is nearly always "what has just
        happened on this machine" and the answer to "what happened in March" is
        `--since` away. `limit` is applied after the sort, so it is the last N
        events rather than the last N of whichever home was read first.
        """
        wanted = tuple(kinds) if kinds is not None else register.KINDS
        unknown = sorted(set(wanted) - set(register.KINDS))
        if unknown:
            raise MachineError(
                f"unknown audit kind {', '.join(unknown)}. "
                f"Known kinds are {', '.join(register.KINDS)}."
            )
        events: list[dict[str, Any]] = []
        for who, paths in self._homes(user):
            events.extend(
                {"user": who, **entry}
                for entry in self._read(
                    paths,
                    lambda conn: register.audit(
                        conn, since=since, until=until, kinds=wanted
                    ),
                )
            )
        events.sort(key=lambda entry: (str(entry["at"]), str(entry["user"])), reverse=True)
        return events[:limit] if limit else events

    def spend(
        self, *, user: str | None = None, hours: float = 1.0
    ) -> dict[str, Any]:
        """What each home has used, from both places that count it.

        ``window`` is the ledger's rolling count -- the one the limits are
        enforced against, kept for a week and no longer (D22). ``total`` is the
        journal's, which goes back to the day the app was created. They are
        reported side by side rather than added, because a number that is
        sometimes a week and sometimes a year is not a number.
        """
        seconds = hours * 3600.0
        limits = self.limits
        homes: list[dict[str, Any]] = []
        for who, paths in self._homes(user):
            per_app = self._read(paths, register.spend)
            homes.append({
                "user": who,
                "home": str(paths.home),
                "window": {
                    act: spent(paths, act, seconds=seconds)
                    for act in ("write", "deploy", "shot")
                },
                "total": {
                    "writes": sum(int(app["writes"]) for app in per_app),
                    "green": sum(int(app["green"]) for app in per_app),
                    "red": sum(int(app["red"]) for app in per_app),
                    "deploys": sum(int(app["deploys"]) for app in per_app),
                    "production_deploys": sum(
                        int(app["production_deploys"]) for app in per_app
                    ),
                    "build_seconds": round(
                        sum(float(app["build_seconds"]) for app in per_app), 1
                    ),
                },
                "apps": per_app,
                "disk_mb": disk_mb(paths.home),
            })
        return {
            "ok": True,
            "window_hours": hours,
            "limits": limits.as_dict(),
            "users": homes,
            "total": {
                key: sum(int(home["total"][key]) for home in homes)
                for key in ("writes", "green", "red", "deploys", "production_deploys")
            },
        }

    def _homes(self, user: str | None) -> list[tuple[str, ApplacePaths]]:
        """The homes a register call is about, in the order people arrived."""
        if user is not None:
            paths = self.home_of(user)
            return [(user, paths)] if paths.db.exists() else []
        return [
            (str(record["user"]), ApplacePaths(Path(str(record["home"])), ledger=self.ledger))
            for record in self.homes()
        ]

    def _read(
        self, paths: ApplacePaths, question: Callable[[sqlite3.Connection], Any]
    ) -> Any:
        """Ask one home's journal something, read-only, and let go of it.

        A home with no database yet is not an error: `Machine.user()` creates
        the row in the ledger before anything in the home is written, so a
        person who has connected and built nothing is a home with no journal.
        """
        if not paths.db.exists():
            return []
        conn = register.open_ro(paths.db)
        try:
            return question(conn)
        finally:
            conn.close()

    def gc(
        self, *, preview_hours: float = 2.0, scratch_hours: float = 24.0
    ) -> dict[str, Any]:
        """Stop what nobody is watching and free what nobody is holding (D23).

        Never deletes an app, a repository, a secret or a home: a person who
        comes back to a stopped preview is one URL from where they were, and a
        person who comes back to a deleted repository has lost work.
        """
        from .api import Applace

        stopped: list[dict[str, str]] = []
        scratch = 0
        for record in self.users():
            paths = ApplacePaths(Path(record["home"]), ledger=self.ledger)
            # Scratch first and unconditionally: a checkout left behind by a
            # deploy is dead weight whether or not the home ever got a database.
            scratch += _sweep(paths.work, scratch_hours)
            if not paths.db.exists():
                continue
            applace = Applace(paths)
            for old in self._idle_previews(paths, preview_hours):
                applace.stop_preview(old)
                stopped.append({"user": record["user"], "app": old})
        conn = connect(self.ledger)
        try:
            released = reap(conn)
        finally:
            conn.close()
        return {
            "ok": True,
            "previews_stopped": stopped,
            "ports_released": released,
            "scratch_removed": scratch,
            "spend_forgotten": forget(self.ledger),
        }

    # ----------------------------------------------------------------------

    def _counts(self, home: Path) -> dict[str, Any]:
        paths = ApplacePaths(home, ledger=self.ledger)
        if not paths.db.exists():
            return {"apps": 0, "previews": 0, "disk_mb": 0}
        from .db import connect as open_home

        conn = open_home(paths.db)
        try:
            apps = int(conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"])
            live = int(
                conn.execute("SELECT COUNT(*) AS n FROM previews").fetchone()["n"]
            )
        finally:
            conn.close()
        return {"apps": apps, "previews": live, "disk_mb": disk_mb(home)}

    def _idle_previews(self, paths: ApplacePaths, hours: float) -> list[str]:
        from .db import connect as open_home

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        conn = open_home(paths.db)
        try:
            rows = list(
                conn.execute(
                    "SELECT apps.slug AS slug, previews.started_at AS started_at "
                    "FROM previews JOIN apps ON apps.id = previews.app_id"
                )
            )
        finally:
            conn.close()
        return [
            str(row["slug"])
            for row in rows
            if _parse(str(row["started_at"])) < cutoff
        ]


def disk_mb(home: Path) -> int:
    """How much of the disk one home is using, node_modules and all.

    A walk, not a cached number: the thing that fills a disk is `npm install`,
    which Applace does not account for line by line. It is called where a walk
    is already cheap next to what follows it -- creating an app installs a
    dependency tree -- and never on a read.
    """
    total = 0
    for root, _, files in os.walk(home, onerror=lambda _: None):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:  # it went away while we were counting; it does not count
                continue
    return total // (1024 * 1024)


def _sweep(directory: Path, hours: float) -> int:
    """Remove scratch directories older than `hours`. Returns how many."""
    if not directory.is_dir():
        return 0
    cutoff = time.time() - hours * 3600
    removed = 0
    for child in directory.iterdir():
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
        removed += 1
    return removed


def _parse(stamp: str) -> datetime:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:  # a stamp we did not write; treat it as ancient
        return datetime.min.replace(tzinfo=timezone.utc)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
