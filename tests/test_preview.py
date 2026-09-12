"""The preview supervisor, against a real detached process on a real port.

The fake stack's `dev` is swapped for a python http server: cheap enough to
start in a test, and a genuine long-lived child in its own process group, which
is the only way to find out whether stopping one actually works.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import call_tool

from applace import preview
from applace.apps import app_detail, create_app, remove_app, require_app, start_preview, stop_preview
from applace.db import Connection, find_preview, upsert_preview
from applace.paths import ApplacePaths
from applace.preview import PreviewError

Home = tuple[ApplacePaths, Connection]

# `-d {port}`-free on purpose: http.server takes the port positionally, and the
# supervisor substitutes it exactly as it would for vite.
DEV_SERVER = f"{sys.executable} -m http.server {{port}} --bind 127.0.0.1"


def _set_dev(paths: ApplacePaths, command: str) -> None:
    manifest = paths.stacks / "fake" / "stack.yaml"
    data: dict[str, Any] = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["dev"] = command
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def app(home: Home) -> Any:
    paths, conn = home
    _set_dev(paths, DEV_SERVER)
    report = create_app(paths, conn, name="Preview App", stack_name="fake", install=False)
    yield paths, conn, report.path
    # Never leave a server holding a port, whatever the test did.
    try:
        stop_preview(conn, "preview-app")
    except Exception:  # pragma: no cover - the app may already be gone
        pass


def _alive(pid: int) -> bool:
    return preview.process_command(pid) is not None


def test_starting_a_preview_gives_a_url_that_answers(app: Any) -> None:
    paths, conn, _ = app
    running = start_preview(paths, conn, "preview-app")
    assert running.ready
    assert running.url.startswith("http://127.0.0.1:")
    assert preview.responds(running.url)


def test_the_preview_outlives_the_call_that_started_it(app: Any) -> None:
    paths, conn, _ = app
    running = start_preview(paths, conn, "preview-app")
    time.sleep(0.2)
    assert _alive(running.pid)


def test_starting_twice_hands_back_the_same_server(app: Any) -> None:
    paths, conn, _ = app
    first = start_preview(paths, conn, "preview-app")
    second = start_preview(paths, conn, "preview-app")
    assert (second.port, second.pid) == (first.port, first.pid)


def test_stopping_frees_the_port_and_kills_the_whole_group(app: Any) -> None:
    paths, conn, _ = app
    running = start_preview(paths, conn, "preview-app")
    assert stop_preview(conn, "preview-app") is True
    assert not _alive(running.pid)
    assert preview._is_free(running.port)
    assert not preview.responds(running.url)


def test_stopping_something_that_is_not_running_is_not_an_error(app: Any) -> None:
    _, conn, _ = app
    assert stop_preview(conn, "preview-app") is False


def test_two_apps_do_not_get_the_same_port(home: Home) -> None:
    paths, conn = home
    _set_dev(paths, DEV_SERVER)
    create_app(paths, conn, name="One", stack_name="fake", install=False)
    create_app(paths, conn, name="Two", stack_name="fake", install=False)
    try:
        first = start_preview(paths, conn, "one")
        second = start_preview(paths, conn, "two")
        assert first.port != second.port
    finally:
        stop_preview(conn, "one")
        stop_preview(conn, "two")


def test_a_preview_survives_the_supervisor_and_is_found_again(app: Any) -> None:
    """A new connection -- a restarted server -- still knows about the preview."""
    paths, conn, root = app
    running = start_preview(paths, conn, "preview-app")

    from applace.db import connect

    fresh = connect(paths.db)
    try:
        row = require_app(fresh, "preview-app")
        found = preview.status(fresh, str(row["id"]), "preview-app", root)
        assert found is not None
        assert (found.port, found.pid) == (running.port, running.pid)
        assert found.ready
    finally:
        fresh.close()


def test_a_claim_whose_process_is_gone_is_reaped_rather_than_believed(app: Any) -> None:
    paths, conn, root = app
    running = start_preview(paths, conn, "preview-app")
    os.killpg(os.getpgid(running.pid), 9)
    for _ in range(100):
        if not _alive(running.pid):
            break
        time.sleep(0.05)

    row = require_app(conn, "preview-app")
    assert preview.status(conn, str(row["id"]), "preview-app", root) is None
    assert find_preview(conn, str(row["id"])) is None


def test_a_recycled_pid_is_not_mistaken_for_our_dev_server(app: Any) -> None:
    """The claim names a live process that is not ours; it must not be believed."""
    paths, conn, root = app
    row = require_app(conn, "preview-app")
    upsert_preview(
        conn,
        app_id=str(row["id"]),
        port=5999,
        pid=os.getpid(),  # alive, and very much not a dev server for this app
        url="http://127.0.0.1:5999/",
        command="npm run dev",
        log_path=str(paths.app_logs("preview-app") / "dev.log"),
    )
    conn.commit()
    assert preview.status(conn, str(row["id"]), "preview-app", root) is None


def test_a_dev_server_that_exits_immediately_reports_its_own_log(app: Any) -> None:
    paths, conn, _ = app
    _set_dev(paths, "sh -c 'echo port 5173 is already in use; exit 1'")
    with pytest.raises(PreviewError, match="already in use"):
        start_preview(paths, conn, "preview-app")


def test_asking_for_a_port_something_else_holds_says_so(app: Any) -> None:
    import socket

    paths, conn, _ = app
    with socket.socket() as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        with pytest.raises(PreviewError, match="already in use"):
            start_preview(paths, conn, "preview-app", port=taken)


def test_get_app_says_where_the_preview_is(app: Any) -> None:
    paths, conn, _ = app
    running = start_preview(paths, conn, "preview-app")
    detail = app_detail(paths, conn, require_app(conn, "preview-app"))
    assert detail["preview"]["url"] == running.url
    assert detail["preview"]["ready"] is True


def test_get_app_says_nothing_when_no_preview_is_running(app: Any) -> None:
    paths, conn, _ = app
    assert app_detail(paths, conn, require_app(conn, "preview-app"))["preview"] is None


def test_removing_an_app_stops_its_preview_first(app: Any) -> None:
    paths, conn, _ = app
    running = start_preview(paths, conn, "preview-app")
    remove_app(paths, conn, "preview-app", delete_files=True)
    assert not _alive(running.pid)


def test_the_log_is_where_the_dev_server_wrote(app: Any) -> None:
    paths, conn, _ = app
    running = start_preview(paths, conn, "preview-app")
    assert running.log == paths.app_logs("preview-app") / "dev.log"
    assert Path(running.log).exists()


# -- the surfaces ----------------------------------------------------------


def test_the_mcp_tools_start_and_stop_the_same_preview(app: Any) -> None:
    from applace.server import build_server

    paths, conn, _ = app
    server = build_server(paths)

    def call(tool: str, **arguments: Any) -> dict[str, Any]:
        return call_tool(server, tool, **arguments)

    started = call("start_preview", app="preview-app")
    assert started["ok"] and started["ready"]
    assert preview.responds(started["url"])

    again = call("start_preview", app="preview-app")
    assert again["url"] == started["url"]

    assert call("get_app", app="preview-app")["preview"]["url"] == started["url"]

    assert call("stop_preview", app="preview-app") == {
        "ok": True, "app": "preview-app", "stopped": True
    }
    assert call("stop_preview", app="preview-app")["stopped"] is False
    assert not _alive(started["pid"])


def test_a_preview_that_will_not_start_is_a_result_not_an_exception(app: Any) -> None:
    from applace.server import build_server

    paths, conn, _ = app
    _set_dev(paths, "sh -c 'echo EADDRINUSE; exit 1'")
    body = call_tool(build_server(paths), "start_preview", app="preview-app")
    assert body["ok"] is False
    assert body["code"] == "preview-failed"
    assert "EADDRINUSE" in body["error"]


def test_the_cli_dev_and_stop_verbs_drive_the_same_supervisor(app: Any) -> None:
    from typer.testing import CliRunner

    from applace.cli import app as cli

    runner = CliRunner()
    started = runner.invoke(cli, ["dev", "preview-app"])
    assert started.exit_code == 0, started.output
    url = started.output.splitlines()[0]
    assert preview.responds(url)

    listed = runner.invoke(cli, ["ls"])
    assert url in listed.output

    stopped = runner.invoke(cli, ["stop", "preview-app"])
    assert stopped.exit_code == 0
    assert "Stopped" in stopped.output
    assert not preview.responds(url)
