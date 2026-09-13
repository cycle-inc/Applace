"""The developer's view, one level above a home (D27).

M12 gave a machine one home per person, and every one of those homes is a
complete Applace that answers only about itself. Nobody who runs the machine
lives in a home: the person who has to know what is on it needs the other
question -- what exists here, what reached production, who confirmed it, and
what it all cost -- across every home at once.

Three rules hold this module together.

**Every database is opened read-only.** A reporting tool that can write is a
reporting tool that can corrupt, and it would be doing it to somebody else's
data at the exact moment nobody is looking. SQLite is told twice: ``mode=ro``
on the handle and ``query_only`` on the connection.

**Nothing here touches an app's files.** No ``git``, no ``stat``, no walk of a
node_modules. A register that shells out once per app is a register that takes
minutes on a machine with four hundred apps, and one that runs `git` in
somebody's checkout while they are working in it is a register that can lose
their index lock. Everything below is SQL against the journal.

**One definition of the state, used by all three views.** The register, the
per-home listing (``ap.apps()``) and the chat card (``ap.cards()``) call the
same function here, so a machine that says an app is red and a chat window that
says it is green cannot both be Applace.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import latest_gate, latest_snapshot, list_apps

# What the audit answers about. Named so that `--kind deploy` is a thing an
# operator can guess, and so that adding a fifth kind is adding a function.
KINDS = ("create", "take", "deploy", "api", "refused")


def open_ro(db: Path) -> sqlite3.Connection:
    """Open a home's journal read-only. The handle cannot write; nor may it try.

    ``mode=ro`` is the real guard -- SQLite refuses the write at the file
    handle, so a bug here raises instead of corrupting -- and ``query_only`` is
    the same statement made where a reader of this code will see it.

    A home in WAL mode is still readable while its owner is working: the reader
    takes no lock the writer waits on, which is the whole reason the homes are
    in WAL to begin with.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    return conn


# -- one app, from the journal alone ---------------------------------------


def gate_of(conn: sqlite3.Connection, app_id: str) -> dict[str, Any] | None:
    """The last thing the compiler said, folded to what a listing shows."""
    row = latest_gate(conn, app_id)
    if row is None:
        return None
    errors = json.loads(str(row["errors_json"] or "[]"))
    return {
        "ok": bool(row["ok"]),
        "stage": str(row["stage"]),
        "at": str(row["created_at"]),
        "duration_ms": int(row["duration_ms"]),
        "errors": len(errors) if isinstance(errors, list) else 0,
    }


def exposure(conn: sqlite3.Connection, app_id: str) -> list[dict[str, Any]]:
    """Where this app can be reached from outside itself.

    A dev-server row is a *claim* on a port, not a probe of one: the register
    does not signal pids belonging to somebody else's session. `applace ls` in
    that home checks; this says what the journal was last told.
    """
    found: list[dict[str, Any]] = []
    live = conn.execute(
        "SELECT * FROM previews WHERE app_id = ?", (app_id,)
    ).fetchone()
    if live is not None:
        found.append(
            {
                "kind": "preview",
                "environment": "dev",
                "url": str(live["url"]),
                "since": str(live["started_at"]),
            }
        )
    for row in conn.execute(
        "SELECT * FROM deployments WHERE app_id = ? AND status = 'live' "
        "ORDER BY rowid DESC",
        (app_id,),
    ):
        found.append(
            {
                "kind": str(row["target"]),
                "environment": str(row["environment"]),
                "url": row["url"],
                "since": str(row["started_at"]),
                "commit": str(row["commit_sha"]),
            }
        )
    return found


def state_of(commit: str | None, gate: dict[str, Any] | None) -> str:
    """`new`, `green` or `red` -- the one definition, for every view (D27).

    Red is the *last* gate having failed, not an app that has ever failed: the
    question every view is really asking is "is it safe to build on this right
    now", and an app whose last write passed is, whatever happened before.
    """
    if gate is not None and not gate["ok"]:
        return "red"
    return "green" if commit else "new"


def summarise(conn: sqlite3.Connection, row: Any) -> dict[str, Any]:
    """One app as the register sees it: identity, state, and where it is exposed.

    Journal only. Whoever wants to know whether the working tree is dirty has
    to open the working tree, and that is the caller's decision, not this
    function's.
    """
    snapshot = latest_snapshot(conn, str(row["id"]))
    commit = str(snapshot["commit_sha"]) if snapshot is not None else None
    gate = gate_of(conn, str(row["id"]))
    return {
        "app": str(row["slug"]),
        "name": str(row["name"]),
        "description": row["description"],
        "stack": str(row["stack"]),
        "path": str(row["path"]),
        "created_at": str(row["created_at"]),
        "github_url": row["github_url"],
        "commit": commit,
        # Where an app came from when Applace did not make it (D26). None is the
        # usual answer; a value means the file tree is somebody else's and the
        # stack's template is a description of it at best.
        "taken_from": row["taken_from"],
        "state": state_of(commit, gate),
        "gate": gate,
        "exposed": exposure(conn, str(row["id"])),
    }


def apps(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every app in one home, from its journal."""
    return [summarise(conn, row) for row in list_apps(conn)]


# -- the audit -------------------------------------------------------------


def audit(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    kinds: tuple[str, ...] = KINDS,
) -> list[dict[str, Any]]:
    """The rows of one home's journal that govern, newest first.

    Governing means: an app came into existence, something was published, a
    human permitted a production deploy, an app was allowed to call an upstream,
    or a policy refused a write. Everything else in the journal is the harness
    talking to itself.

    What is not here is as deliberate as what is. **No value of any secret**:
    the journal has never held one (D8, D24) -- ``token_env`` is a variable's
    name, and the ``env/`` directory this module never opens is where a value
    lives. And **a refusal that never became a row is not a row**: a production
    deploy stopped by policy raises before anything is written, so what proves a
    company's rule held is the absence of the deploy, plus the gate refusals
    below, which are written down because a write got that far.
    """
    lower, upper = _bound(since), _bound(until, end=True)
    found: list[dict[str, Any]] = []
    for row in list_apps(conn):
        slug, app_id = str(row["slug"]), str(row["id"])
        taken = row["taken_from"]
        if taken:
            found.append(
                _event("take", str(row["created_at"]), slug,
                       {"from": str(taken), "stack": str(row["stack"]),
                        "path": str(row["path"])})
            )
        else:
            found.append(
                _event("create", str(row["created_at"]), slug,
                       {"stack": str(row["stack"]), "path": str(row["path"])})
            )
        for deployment in conn.execute(
            "SELECT * FROM deployments WHERE app_id = ? ORDER BY rowid", (app_id,)
        ):
            found.append(
                _event("deploy", str(deployment["started_at"]), slug, {
                    "target": str(deployment["target"]),
                    "environment": str(deployment["environment"]),
                    "status": str(deployment["status"]),
                    "url": deployment["url"],
                    "commit": str(deployment["commit_sha"]),
                    # D6: the answer a human gave once, kept where it outlives
                    # the conversation it was given in.
                    "confirmed": bool(deployment["confirmed"]),
                    "finished_at": deployment["finished_at"],
                })
            )
        for decl in conn.execute(
            "SELECT * FROM api_decls WHERE app_id = ? ORDER BY rowid", (app_id,)
        ):
            config = json.loads(str(decl["config_json"]))
            found.append(
                _event("api", str(decl["created_at"]), slug, {
                    "name": str(decl["name"]),
                    "base_url": config.get("base_url"),
                    # A name, never a value -- this is the whole point of D24.
                    "token_env": config.get("token_env"),
                    "paths": config.get("paths"),
                    "methods": config.get("methods"),
                })
            )
        for gate in conn.execute(
            "SELECT * FROM gates WHERE app_id = ? AND ok = 0 AND stage IN "
            "('policy', 'apis', 'paths') ORDER BY rowid",
            (app_id,),
        ):
            errors = json.loads(str(gate["errors_json"] or "[]"))
            found.append(
                _event("refused", str(gate["created_at"]), slug, {
                    "stage": str(gate["stage"]),
                    "why": [
                        str(error.get("message", error))
                        if isinstance(error, dict) else str(error)
                        for error in (errors if isinstance(errors, list) else [])
                    ],
                })
            )
    kept = [
        event for event in found
        if event["kind"] in kinds
        and (lower is None or event["at"] >= lower)
        and (upper is None or event["at"] <= upper)
    ]
    kept.sort(key=lambda event: (event["at"], event["app"]), reverse=True)
    return kept


def _event(kind: str, at: str, app: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "at": at, "app": app, **detail}


def _bound(value: str | None, *, end: bool = False) -> str | None:
    """A date or a timestamp, as something comparable to an ISO stamp.

    The stamps in the journal are ISO-8601 in UTC, which sorts lexically, so a
    filter is a string comparison and not a parse of four hundred rows. A bare
    date given as `--until` means the whole of that day: an operator asking for
    "up to the 3rd" does not mean "up to midnight at the start of the 3rd".
    """
    if not value:
        return None
    text = value.strip()
    if len(text) == 10 and end:  # YYYY-MM-DD
        return f"{text}T23:59:59+00:00"
    return text


# -- what it cost ----------------------------------------------------------


def spend(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per app: how much the agent did, all of it, since the app existed.

    This is the journal's count, not the ledger's. The ledger holds a rolling
    window and forgets it (M12 keeps a week, because that is all a rate limit
    needs); the journal holds every gate and every deploy for as long as the
    home does. They answer different questions and this module reports both
    rather than adding them into one number that would be wrong twice.
    """
    counted: list[dict[str, Any]] = []
    for row in list_apps(conn):
        app_id = str(row["id"])
        gates = conn.execute(
            "SELECT COUNT(*) AS n, SUM(ok) AS green, SUM(duration_ms) AS ms, "
            "MAX(created_at) AS last FROM gates WHERE app_id = ?",
            (app_id,),
        ).fetchone()
        deploys = conn.execute(
            "SELECT COUNT(*) AS n, SUM(environment = 'production') AS production "
            "FROM deployments WHERE app_id = ?",
            (app_id,),
        ).fetchone()
        writes = int(gates["n"] or 0)
        green = int(gates["green"] or 0)
        counted.append({
            "app": str(row["slug"]),
            "writes": writes,
            "green": green,
            "red": writes - green,
            "build_seconds": round(int(gates["ms"] or 0) / 1000, 1),
            "deploys": int(deploys["n"] or 0),
            "production_deploys": int(deploys["production"] or 0),
            "last_write": gates["last"],
        })
    return counted


__all__ = [
    "KINDS",
    "apps",
    "audit",
    "exposure",
    "gate_of",
    "open_ro",
    "spend",
    "state_of",
    "summarise",
]
