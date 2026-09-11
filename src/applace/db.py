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
MIGRATIONS: list[str] = []

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


def latest_snapshot(conn: sqlite3.Connection, app_id: str) -> sqlite3.Row | None:
    """The last commit Applace made.

    Ordered by rowid rather than ``created_at``: timestamps have second
    resolution, and two snapshots easily land in the same second.
    """
    return conn.execute(
        "SELECT * FROM snapshots WHERE app_id = ? ORDER BY rowid DESC LIMIT 1",
        (app_id,),
    ).fetchone()


def list_snapshots(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    """Snapshots oldest first."""
    return list(
        conn.execute(
            "SELECT * FROM snapshots WHERE app_id = ? ORDER BY rowid", (app_id,)
        )
    )
