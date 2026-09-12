"""SQLite storage.

The tables come straight from the spec. What is deliberately *not* here is a
version table: a version of an app is a git commit (D2), and ``snapshots`` only
records which commits Applace itself produced and why.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Connection = sqlite3.Connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Apps (M1)

CREATE TABLE IF NOT EXISTS apps (
    id             TEXT PRIMARY KEY,
    slug           TEXT NOT NULL UNIQUE,      -- identity: directory, repo, project
    name           TEXT NOT NULL,             -- as a human typed it
    description    TEXT,
    stack          TEXT NOT NULL,
    stack_source   TEXT NOT NULL,             -- builtin | <git url>
    stack_commit   TEXT,                      -- pinned when the stack came from git
    path           TEXT NOT NULL,
    github_repo    TEXT,                      -- owner/name, once M5 wires one up
    github_url     TEXT,
    default_branch TEXT NOT NULL DEFAULT 'main',
    created_at     TEXT NOT NULL
);

-- One row per commit Applace made: the first one, and every green gate after
-- it. A commit a human made in the repository is not here, and that asymmetry
-- is the point -- it is how M5 notices the tree moved without us.
CREATE TABLE IF NOT EXISTS snapshots (
    id         TEXT PRIMARY KEY,
    app_id     TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(app_id, commit_sha)
);
"""


# SCHEMA above is version 1. Every change since is a statement here, applied in
# order to whatever version a database is already at. Only additive changes
# belong in this list: an Applace home is the user's data.
MIGRATIONS: list[str] = [
    # v2 (M2): one row per write_files, green or red. This is the answer to
    # "why is this app dirty" and to "what did the agent try before it worked",
    # so a failed gate is journaled exactly as carefully as a successful one.
    # `snapshot_id` is NULL for a red gate -- there is no commit to point at.
    """CREATE TABLE IF NOT EXISTS gates (
        id          TEXT PRIMARY KEY,
        app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
        snapshot_id TEXT REFERENCES snapshots(id) ON DELETE SET NULL,
        stage       TEXT NOT NULL,        -- where it stopped, or 'build' when it passed
        ok          INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,
        errors_json TEXT,
        new_deps_json TEXT,
        written_json  TEXT,
        created_at  TEXT NOT NULL
    )""",
    # v3 (M3): the dev server an app currently has, at most one. The row is a
    # claim on a port and a process, not a guarantee: the process may be gone by
    # the time anyone reads it, which is why `command` is stored -- it is how a
    # restarted supervisor tells its own process from whatever reused the pid.
    """CREATE TABLE IF NOT EXISTS previews (
        app_id     TEXT PRIMARY KEY REFERENCES apps(id) ON DELETE CASCADE,
        port       INTEGER NOT NULL,
        pid        INTEGER NOT NULL,
        url        TEXT NOT NULL,
        command    TEXT NOT NULL,
        log_path   TEXT NOT NULL,
        started_at TEXT NOT NULL
    )""",
    # v4 (M5): when a snapshot reached GitHub. NULL means it is still only here,
    # which happens whenever a push was refused or the machine was offline --
    # and the next successful push carries it along with the ones after it.
    "ALTER TABLE snapshots ADD COLUMN pushed_at TEXT",
    # v5 (M6): the environment variables an app says it needs. Names and a
    # description only -- the values live in ~/.applace/env/<slug>.env, outside
    # both the repository and the model's context (D8). A row here with no value
    # in that file is exactly the state "the agent asked, the human has not
    # answered yet", which is the state an agent has to be able to report.
    """CREATE TABLE IF NOT EXISTS env_vars (
        app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        description TEXT,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (app_id, name)
    )""",
]

SCHEMA_VERSION = 1 + len(MIGRATIONS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating and migrating the schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A dev-server supervisor and an MCP tool answering a question are two
    # connections to one file, and under the default rollback journal the reader
    # blocks the writer. WAL lets them get on with it; `busy_timeout` says to
    # wait for the write lock rather than raise "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to :data:`SCHEMA_VERSION`.

    A database that ``executescript(SCHEMA)`` just created is at version 1, the
    same as one written by an older Applace, so both take the same path.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    current = int(row["value"]) if row is not None else 1
    for statement in MIGRATIONS[current - 1 :]:
        conn.execute(statement)


# -- apps ------------------------------------------------------------------


def insert_app(
    conn: sqlite3.Connection,
    *,
    app_id: str,
    slug: str,
    name: str,
    description: str | None,
    stack: str,
    stack_source: str,
    stack_commit: str | None,
    path: str,
) -> None:
    conn.execute(
        """
        INSERT INTO apps(id, slug, name, description, stack, stack_source,
                         stack_commit, path, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            app_id,
            slug,
            name,
            description,
            stack,
            stack_source,
            stack_commit,
            path,
            now_iso(),
        ),
    )


def find_app(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """Look an app up by id or by slug -- agents reliably have one or the other."""
    return conn.execute(
        "SELECT * FROM apps WHERE id = ? OR slug = ?", (key, key)
    ).fetchone()


def list_apps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM apps ORDER BY slug"))


def set_github(
    conn: sqlite3.Connection,
    *,
    app_id: str,
    repo: str,
    url: str,
    default_branch: str,
) -> None:
    """Record which repository an app belongs to, once it has one."""
    conn.execute(
        "UPDATE apps SET github_repo = ?, github_url = ?, default_branch = ? WHERE id = ?",
        (repo, url, default_branch, app_id),
    )


def mark_pushed(conn: sqlite3.Connection, app_id: str) -> None:
    """Say that everything local is on the remote, because a push just sent it.

    Not "the snapshot whose sha is HEAD": a human who rebased to reconcile with
    the remote has given our commits new shas, and matching on sha would leave
    the old ones outstanding forever. A push sends the whole reachable history,
    so after one succeeds there is nothing left behind.
    """
    conn.execute(
        "UPDATE snapshots SET pushed_at = ? WHERE app_id = ? AND pushed_at IS NULL",
        (now_iso(), app_id),
    )


def unpushed(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    """Snapshots that are not known to be on the remote, oldest first."""
    return list(
        conn.execute(
            "SELECT * FROM snapshots WHERE app_id = ? AND pushed_at IS NULL ORDER BY rowid",
            (app_id,),
        )
    )


def delete_app(conn: sqlite3.Connection, app_id: str) -> None:
    """Forget an app. Removing its directory is the caller's decision, not ours."""
    conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


# -- snapshots -------------------------------------------------------------


def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    app_id: str,
    commit_sha: str,
    message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO snapshots(id, app_id, commit_sha, message, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (snapshot_id, app_id, commit_sha, message, now_iso()),
    )


def insert_gate(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    app_id: str,
    snapshot_id: str | None,
    stage: str,
    ok: bool,
    duration_ms: int,
    errors: list[dict[str, Any]],
    new_deps: list[dict[str, Any]],
    written: list[str],
) -> None:
    """Journal one pass of the compiler, whichever way it went."""
    conn.execute(
        """
        INSERT INTO gates(id, app_id, snapshot_id, stage, ok, duration_ms,
                          errors_json, new_deps_json, written_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gate_id,
            app_id,
            snapshot_id,
            stage,
            int(ok),
            duration_ms,
            canonical_json(errors),
            canonical_json(new_deps),
            canonical_json(written),
            now_iso(),
        ),
    )


def latest_gate(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row | None:
    """The last thing the compiler said about this app.

    Ordered by rowid: two gates on a fast machine land in the same second.
    """
    return conn.execute(
        "SELECT * FROM gates WHERE app_id = ? ORDER BY rowid DESC LIMIT 1",
        (app_id,),
    ).fetchone()


def latest_snapshot(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row | None:
    """The last commit Applace made.

    Ordered by rowid rather than ``created_at``: timestamps have second
    resolution, and two snapshots easily land in the same second.
    """
    return conn.execute(
        "SELECT * FROM snapshots WHERE app_id = ? ORDER BY rowid DESC LIMIT 1",
        (app_id,),
    ).fetchone()


# -- previews --------------------------------------------------------------


def upsert_preview(
    conn: sqlite3.Connection,
    *,
    app_id: str,
    port: int,
    pid: int,
    url: str,
    command: str,
    log_path: str,
) -> None:
    """Claim a port and a process for an app, replacing any earlier claim."""
    conn.execute(
        """
        INSERT INTO previews(app_id, port, pid, url, command, log_path, started_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_id) DO UPDATE SET
            port = excluded.port, pid = excluded.pid, url = excluded.url,
            command = excluded.command, log_path = excluded.log_path,
            started_at = excluded.started_at
        """,
        (app_id, port, pid, url, command, log_path, now_iso()),
    )


def find_preview(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM previews WHERE app_id = ?", (app_id,)
    ).fetchone()


def list_previews(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every claimed preview, with the app it belongs to."""
    return list(
        conn.execute(
            "SELECT previews.*, apps.slug, apps.path FROM previews "
            "JOIN apps ON apps.id = previews.app_id ORDER BY apps.slug"
        )
    )


def delete_preview(conn: sqlite3.Connection, app_id: str) -> None:
    conn.execute("DELETE FROM previews WHERE app_id = ?", (app_id,))


def claimed_ports(conn: sqlite3.Connection) -> set[int]:
    return {int(row["port"]) for row in conn.execute("SELECT port FROM previews")}


# -- environment variables (D8) --------------------------------------------


def declare_env(
    conn: sqlite3.Connection, *, app_id: str, name: str, description: str | None
) -> None:
    """Record that an app needs a variable. Re-declaring updates the description."""
    conn.execute(
        """
        INSERT INTO env_vars(app_id, name, description, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(app_id, name) DO UPDATE SET
            description = COALESCE(excluded.description, env_vars.description)
        """,
        (app_id, name, description, now_iso()),
    )


def list_env(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM env_vars WHERE app_id = ? ORDER BY name", (app_id,)
        )
    )


def delete_env(conn: sqlite3.Connection, app_id: str, name: str) -> None:
    conn.execute("DELETE FROM env_vars WHERE app_id = ? AND name = ?", (app_id, name))


def list_snapshots(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    """Snapshots oldest first."""
    return list(
        conn.execute(
            "SELECT * FROM snapshots WHERE app_id = ? ORDER BY rowid", (app_id,)
        )
    )
