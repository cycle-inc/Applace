"""Creating and describing apps.

An app is a directory under ``~/.applace/apps/`` that is an ordinary git
repository (D1) and a row in the database saying what it was made from. The row
is bookkeeping; the repository is the truth, and every question about the app's
*content* is answered by asking git rather than by reading the row.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitrepo, preview, shell
from .db import Connection, find_app, insert_app, insert_snapshot, latest_gate, latest_snapshot
from .db import list_apps as db_list_apps
from .db import list_snapshots
from .naming import display_name, slugify
from .paths import ApplacePaths
from .stacks import Stack, resolve

FIRST_COMMIT = "Create {name} from the {stack} stack"

# `npm install` on a cold cache is minutes, not seconds.
INSTALL_TIMEOUT = 900.0


class AppError(Exception):
    """Something about the request makes the app impossible to create."""


class AppExists(AppError):
    pass


class UnknownApp(AppError):
    pass


@dataclass
class CreateReport:
    slug: str
    name: str
    path: Path
    stack: str
    entry: str
    commit: str
    files: list[str]
    installed: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "app": self.slug,
            "name": self.name,
            "stack": self.stack,
            "path": str(self.path),
            "entry": self.entry,
            "commit": self.commit,
            "files": self.files,
            "installed": self.installed,
            "warnings": self.warnings,
        }


def create_app(
    paths: ApplacePaths,
    conn: Connection,
    *,
    name: str,
    stack_name: str | None = None,
    description: str | None = None,
    install: bool = True,
) -> CreateReport:
    """Render a stack into a new repository and record it.

    Failures before the first commit remove the directory again: a half-rendered
    app that cannot be created and cannot be used is worse than no app, and the
    caller's obvious next move is to try again with a different name.

    A failed ``install`` is *not* one of those failures. The source is valid, it
    is committed, and the fix is one command -- deleting the work because npm
    could not reach the registry would be the wrong call.
    """
    slug = slugify(name)
    title = display_name(name)
    stack = resolve(paths.stacks, stack_name)
    target = paths.app(slug)

    if find_app(conn, slug) is not None:
        raise AppExists(f"an app called {slug!r} already exists")
    if target.exists() and any(target.iterdir()):
        raise AppExists(
            f"{target} already exists and is not empty, but no app called "
            f"{slug!r} is registered. Move it aside or pick another name."
        )

    warnings: list[str] = []
    try:
        files = stack.render(
            target,
            context={
                "app_slug": slug,
                "app_name": title,
                "app_description": description or f"{title}, built with Applace.",
            },
        )
        gitrepo.init(target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    installed = False
    if install:
        result = shell.run(
            stack.commands["install"], cwd=target, timeout=INSTALL_TIMEOUT
        )
        installed = result.ok
        if not result.ok:
            warnings.append(
                f"`{result.command}` failed, so the app has no dependencies yet. "
                f"Run it again in {target} once the problem is fixed.\n{result.output}"
            )

    commit = gitrepo.commit_all(target, FIRST_COMMIT.format(name=title, stack=stack.name))

    app_id = uuid.uuid4().hex
    insert_app(
        conn,
        app_id=app_id,
        slug=slug,
        name=title,
        description=description,
        stack=stack.name,
        stack_source=stack.source,
        stack_commit=stack.commit,
        path=str(target),
    )
    insert_snapshot(
        conn,
        snapshot_id=uuid.uuid4().hex,
        app_id=app_id,
        commit_sha=commit.sha,
        message=commit.message,
    )
    conn.commit()

    return CreateReport(
        slug=slug,
        name=title,
        path=target,
        stack=stack.name,
        entry=stack.entry,
        commit=commit.sha,
        files=files,
        installed=installed,
        warnings=warnings,
    )


def require_app(conn: Connection, key: str) -> Any:
    row = find_app(conn, key)
    if row is None:
        known = ", ".join(str(r["slug"]) for r in db_list_apps(conn)) or "none yet"
        raise UnknownApp(f"no app called {key!r}. Known apps: {known}")
    return row


def app_summary(conn: Connection, row: Any) -> dict[str, Any]:
    """What `list_apps` says about one app: state, not content."""
    path = Path(str(row["path"]))
    present = path.is_dir()
    snapshot = latest_snapshot(conn, str(row["id"]))
    summary: dict[str, Any] = {
        "app": str(row["slug"]),
        "name": str(row["name"]),
        "description": row["description"],
        "stack": str(row["stack"]),
        "path": str(path),
        "created_at": str(row["created_at"]),
        "github_url": row["github_url"],
        "commit": snapshot["commit_sha"] if snapshot is not None else None,
    }
    if not present:
        # The row outlived the directory. Say so plainly rather than raising:
        # `list_apps` failing wholesale because one app was deleted by hand
        # would be a poor trade.
        summary["missing"] = True
        return summary
    summary["dirty"] = gitrepo.is_dirty(path)
    summary["branch"] = gitrepo.current_branch(path)
    return summary


def app_detail(
    paths: ApplacePaths, conn: Connection, row: Any, *, stack: Stack | None = None
) -> dict[str, Any]:
    """What `get_app` says: everything an agent needs before it writes code."""
    detail = app_summary(conn, row)
    path = Path(str(row["path"]))
    if detail.get("missing"):
        return detail
    if stack is None:
        try:
            stack = resolve(paths.stacks, str(row["stack"]))
        except Exception:  # the stack was uninstalled; the app is still readable
            stack = None
    detail["files"] = gitrepo.tracked_files(path)
    detail["uncommitted"] = gitrepo.status(path)
    detail["snapshots"] = len(list_snapshots(conn, str(row["id"])))
    detail["installed"] = (path / "node_modules").is_dir()
    detail.update(outstanding(conn, str(row["id"])))
    live = preview.status(conn, str(row["id"]), str(row["slug"]), path)
    detail["preview"] = live.as_dict() if live is not None else None
    if stack is not None:
        detail["entry"] = stack.entry
        detail["env_prefix"] = stack.env_prefix
        detail["deploy"] = list(stack.deploy)
    return detail


def outstanding(conn: Connection, app_id: str) -> dict[str, Any]:
    """The errors the last gate left behind, if it left any (D4).

    An app is dirty either because the agent wrote something that did not pass
    or because a human edited the repository. Only the first of those has
    errors attached, and saying which one it is saves the agent a guess.
    """
    gate = latest_gate(conn, app_id)
    if gate is None or bool(gate["ok"]):
        return {"errors": []}
    return {
        "errors": json.loads(str(gate["errors_json"] or "[]")),
        "failed_stage": str(gate["stage"]),
    }


def start_preview(
    paths: ApplacePaths, conn: Connection, key: str, *, port: int | None = None
) -> preview.Preview:
    """Run the app's dev server, or hand back the one already running."""
    row = require_app(conn, key)
    return preview.start(
        paths,
        conn,
        app_id=str(row["id"]),
        slug=str(row["slug"]),
        root=Path(str(row["path"])),
        stack=resolve(paths.stacks, str(row["stack"])),
        port=port,
    )


def stop_preview(conn: Connection, key: str) -> bool:
    """Stop the app's dev server. False when there was nothing running."""
    row = require_app(conn, key)
    return preview.stop(conn, str(row["id"]), str(row["slug"]), Path(str(row["path"])))


def remove_app(
    paths: ApplacePaths, conn: Connection, key: str, *, delete_files: bool
) -> Path:
    """Forget an app, and optionally delete its repository."""
    row = require_app(conn, key)
    path = Path(str(row["path"]))
    # Before the row goes: after it, nothing knows which process to signal and
    # the port stays held until someone finds it by hand.
    preview.stop(conn, str(row["id"]), str(row["slug"]), path)
    conn.execute("DELETE FROM apps WHERE id = ?", (str(row["id"]),))
    conn.commit()
    if delete_files and path.is_dir() and path.parent == paths.apps:
        shutil.rmtree(path, ignore_errors=True)
    return path
