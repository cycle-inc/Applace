"""Storage. Small, because git holds everything that matters (D2)."""

from __future__ import annotations

import sqlite3

import pytest

from applace.db import (
    SCHEMA_VERSION,
    connect,
    find_app,
    insert_app,
    insert_snapshot,
    latest_snapshot,
    list_apps,
    list_snapshots,
)
from applace.paths import ApplacePaths


def _app(conn: sqlite3.Connection, slug: str = "team-dashboard") -> str:
    insert_app(
        conn,
        app_id=f"id-{slug}",
        slug=slug,
        name=slug.replace("-", " ").title(),
        description=None,
        stack="fake",
        stack_source="builtin",
        stack_commit=None,
        path=f"/tmp/{slug}",
    )
    return f"id-{slug}"


def test_connecting_twice_is_the_same_database(paths: ApplacePaths) -> None:
    first = connect(paths.db)
    _app(first)
    first.commit()
    first.close()

    second = connect(paths.db)
    assert find_app(second, "team-dashboard") is not None
    version = second.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert int(version["value"]) == SCHEMA_VERSION
    second.close()


def test_an_app_is_found_by_slug_or_by_id(paths: ApplacePaths) -> None:
    conn = connect(paths.db)
    app_id = _app(conn)
    assert find_app(conn, "team-dashboard") is not None
    assert find_app(conn, app_id) is not None
    assert find_app(conn, "nope") is None
    conn.close()


def test_two_apps_cannot_share_a_slug(paths: ApplacePaths) -> None:
    conn = connect(paths.db)
    _app(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _app(conn)
    conn.close()


def test_snapshots_come_back_in_order_and_the_latest_is_the_last(
    paths: ApplacePaths,
) -> None:
    conn = connect(paths.db)
    app_id = _app(conn)
    for index in range(3):
        insert_snapshot(
            conn,
            snapshot_id=f"s{index}",
            app_id=app_id,
            commit_sha=f"sha{index}",
            message=f"change {index}",
        )
    assert [s["commit_sha"] for s in list_snapshots(conn, app_id)] == [
        "sha0",
        "sha1",
        "sha2",
    ]
    latest = latest_snapshot(conn, app_id)
    assert latest is not None and latest["commit_sha"] == "sha2"
    conn.close()


def test_deleting_an_app_takes_its_snapshots_with_it(paths: ApplacePaths) -> None:
    conn = connect(paths.db)
    app_id = _app(conn)
    insert_snapshot(
        conn, snapshot_id="s", app_id=app_id, commit_sha="sha", message="first"
    )
    conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    assert list_snapshots(conn, app_id) == []
    assert list_apps(conn) == []
    conn.close()
