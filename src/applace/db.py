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
    # v6 (M7): one row per deploy, started before the adapter runs and finished
    # after it, whichever way it went. `confirmed` records that a human said yes
    # to production (D6) -- the journal is where that answer survives, because
    # the question was asked once and the deployment outlives the conversation.
    # `detail_json` carries what the adapter needs to find its own work again: a
    # pid and a port for a local server, an id for a Vercel build.
    """CREATE TABLE IF NOT EXISTS deployments (
        id          TEXT PRIMARY KEY,
        app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
        commit_sha  TEXT NOT NULL,
        target      TEXT NOT NULL,          -- local | vercel
        environment TEXT NOT NULL,          -- preview | production
        status      TEXT NOT NULL,          -- running | live | failed | stopped
        url         TEXT,
        detail_json TEXT,
        confirmed   INTEGER NOT NULL DEFAULT 0,
        started_at  TEXT NOT NULL,
        finished_at TEXT
    )""",
    # v7 (M10): the pull request this app's work is waiting in, on a machine
    # that reviews (D15). It is remembered rather than asked for, because the
    # answer arrives once, in the report of a push, and is needed every time
    # afterwards -- by a chat window drawing a card, and by an agent that has to
    # say where its work is a dozen messages later.
    "ALTER TABLE apps ADD COLUMN pull_request_url TEXT",
    # v8 (M13): the upstreams an app is allowed to call (D24). Names, a base URL
    # and the *name* of the variable holding the credential -- the same division
    # as env_vars, for the same reason: everything in this row is safe to print,
    # to log and to return to a model, and the value it points at is not. The
    # whole declaration is kept as JSON rather than as columns because it is read
    # in one piece by three readers -- the gateway, the generated function and
    # the agent -- and none of them ever queries one field of it.
    """CREATE TABLE IF NOT EXISTS api_decls (
        app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        config_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (app_id, name)
    )""",
    # v9 (M13): the home's gateway process, at most one. Per home rather than
    # per app because it is a proxy, not a server: one port and one process
    # answer for every app here, and a home with forty apps does not need forty
    # of them. The row is a claim on a pid exactly as `previews` is, and is
    # reaped the same way when the process behind it is gone.
    """CREATE TABLE IF NOT EXISTS gateway (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        port       INTEGER NOT NULL,
        pid        INTEGER NOT NULL,
        url        TEXT NOT NULL,
        log_path   TEXT NOT NULL,
        started_at TEXT NOT NULL
    )""",
    # v10 (M14): the routes the gate opens in a browser, and what each one must
    # show (D25). Beside the API declarations rather than in the app's tree, for
    # the same reason (D1): this is Applace's opinion about the app, not the
    # app's own code, and an app that leaves the harness keeps building.
    """CREATE TABLE IF NOT EXISTS routes (
        app_id      TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
        path        TEXT NOT NULL,
        config_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (app_id, path)
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
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A dev-server supervisor and an MCP tool answering a question are two
    # connections to one file, and under the default rollback journal the reader
    # blocks the writer. WAL lets them get on with it; `busy_timeout` says to
    # wait for the write lock rather than raise "database is locked" -- and it
    # is set before the switch to WAL, which is itself a write that needs an
    # exclusive lock and will fail outright if this connection was never told
    # to wait for one.
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
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


def set_pull_request(conn: sqlite3.Connection, app_id: str, url: str | None) -> None:
    """Remember where this app's work is waiting to be merged (D15).

    Called with ``None`` when the pull request is gone -- merged, or closed by
    the person it was handed to -- so that nothing keeps pointing at it.
    """
    conn.execute("UPDATE apps SET pull_request_url = ? WHERE id = ?", (url, app_id))


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


def recent_gates(conn: sqlite3.Connection, app_id: str, limit: int = 20) -> list[sqlite3.Row]:
    """The last gates, newest first. Same ordering caveat as `latest_gate`."""
    return list(
        conn.execute(
            "SELECT * FROM gates WHERE app_id = ? ORDER BY rowid DESC LIMIT ?",
            (app_id, limit),
        )
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
    ports = {int(row["port"]) for row in conn.execute("SELECT port FROM previews")}
    return ports | {int(row["port"]) for row in conn.execute("SELECT port FROM gateway")}


# -- the gateway (M13) ------------------------------------------------------


def upsert_gateway(
    conn: sqlite3.Connection, *, port: int, pid: int, url: str, log_path: str
) -> None:
    """Claim the home's one gateway port and process, replacing any earlier claim."""
    conn.execute(
        """
        INSERT INTO gateway(id, port, pid, url, log_path, started_at)
        VALUES(1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            port = excluded.port, pid = excluded.pid, url = excluded.url,
            log_path = excluded.log_path, started_at = excluded.started_at
        """,
        (port, pid, url, log_path, now_iso()),
    )


def find_gateway(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM gateway WHERE id = 1").fetchone()


def delete_gateway(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM gateway WHERE id = 1")


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


# -- declared APIs (M13) ----------------------------------------------------


def declare_api(
    conn: sqlite3.Connection, *, app_id: str, name: str, config: dict[str, Any]
) -> None:
    """Record an upstream this app may call. Re-declaring replaces it outright.

    Replaces rather than merges: a second declaration of the same name is the
    agent correcting itself, and a half-updated allowlist -- new paths, old
    methods -- is a rule nobody wrote and nobody can read.
    """
    conn.execute(
        """
        INSERT INTO api_decls(app_id, name, config_json, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(app_id, name) DO UPDATE SET config_json = excluded.config_json
        """,
        (app_id, name, canonical_json(config), now_iso()),
    )


def list_apis(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM api_decls WHERE app_id = ? ORDER BY name", (app_id,)
        )
    )


def delete_api(conn: sqlite3.Connection, app_id: str, name: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM api_decls WHERE app_id = ? AND name = ?", (app_id, name)
    )
    return cursor.rowcount > 0


# -- declared routes (M14) --------------------------------------------------


def declare_route(
    conn: sqlite3.Connection, *, app_id: str, path: str, config: dict[str, Any]
) -> None:
    """Record a route the gate visits, replacing what that path said before."""
    conn.execute(
        """
        INSERT INTO routes(app_id, path, config_json, created_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(app_id, path) DO UPDATE SET config_json = excluded.config_json
        """,
        (app_id, path, canonical_json(config), now_iso()),
    )


def list_routes(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM routes WHERE app_id = ? ORDER BY path", (app_id,))
    )


def delete_route(conn: sqlite3.Connection, app_id: str, path: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM routes WHERE app_id = ? AND path = ?", (app_id, path)
    )
    return cursor.rowcount > 0


# -- deployments (M7) -------------------------------------------------------


def insert_deployment(
    conn: sqlite3.Connection,
    *,
    deployment_id: str,
    app_id: str,
    commit_sha: str,
    target: str,
    environment: str,
    confirmed: bool,
) -> None:
    """Open a deployment row before the adapter is called.

    Before, not after: an adapter that hangs or crashes has still changed the
    world, and a row that says `running` with nothing under it is the truth --
    a row that only appears on success would hide exactly the deploys someone
    needs to go and look at.
    """
    conn.execute(
        """
        INSERT INTO deployments(id, app_id, commit_sha, target, environment,
                                status, confirmed, started_at)
        VALUES(?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (deployment_id, app_id, commit_sha, target, environment,
         int(confirmed), now_iso()),
    )


def finish_deployment(
    conn: sqlite3.Connection,
    deployment_id: str,
    *,
    status: str,
    url: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE deployments
           SET status = ?, url = ?, detail_json = ?, finished_at = ?
         WHERE id = ?
        """,
        (status, url, canonical_json(detail or {}), now_iso(), deployment_id),
    )


def find_deployment(conn: sqlite3.Connection, deployment_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
    ).fetchone()


def latest_deployment(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    target: str | None = None,
    environment: str | None = None,
    status: str | None = None,
) -> sqlite3.Row | None:
    """The most recent deployment matching whatever is asked, or None."""
    query = "SELECT * FROM deployments WHERE app_id = ?"
    parameters: list[Any] = [app_id]
    for column, value in (("target", target), ("environment", environment),
                          ("status", status)):
        if value is not None:
            query += f" AND {column} = ?"
            parameters.append(value)
    query += " ORDER BY rowid DESC LIMIT 1"
    return conn.execute(query, parameters).fetchone()


def list_deployments(
    conn: sqlite3.Connection, app_id: str, *, limit: int = 20
) -> list[sqlite3.Row]:
    """An app's deployments, most recent first."""
    return list(
        conn.execute(
            "SELECT * FROM deployments WHERE app_id = ? ORDER BY rowid DESC LIMIT ?",
            (app_id, limit),
        )
    )


def live_deployments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every deployment still claiming a process, with the app it belongs to."""
    return list(
        conn.execute(
            "SELECT deployments.*, apps.slug, apps.path FROM deployments "
            "JOIN apps ON apps.id = deployments.app_id "
            "WHERE deployments.status IN ('running', 'live') ORDER BY apps.slug"
        )
    )


def find_snapshot(
    conn: sqlite3.Connection, app_id: str, commit_sha: str
) -> sqlite3.Row | None:
    """The snapshot for a commit, or None if Applace never made that commit."""
    return conn.execute(
        "SELECT * FROM snapshots WHERE app_id = ? AND commit_sha = ?",
        (app_id, commit_sha),
    ).fetchone()


def list_snapshots(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    """Snapshots oldest first."""
    return list(
        conn.execute(
            "SELECT * FROM snapshots WHERE app_id = ? ORDER BY rowid", (app_id,)
        )
    )
