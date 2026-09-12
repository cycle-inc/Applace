"""The chat window's view of an app (M10, D16).

These tests ask the question an integrator asks: can a chat UI draw what is
happening from one request, and be told when it changes, without knowing
anything about git, the gate or SQLite?
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from applace import gate, panel
from applace.apps import create_app, require_app
from applace.db import Connection, set_pull_request
from applace.paths import ApplacePaths

Home = tuple[ApplacePaths, Connection]


@pytest.fixture
def client(home: Home) -> TestClient:
    paths, _ = home
    return TestClient(Starlette(routes=panel.routes(paths)))


def make_app(home: Home, name: str = "Team Dashboard") -> Any:
    paths, conn = home
    created = create_app(paths, conn, name=name, stack_name="fake", install=False)
    return require_app(conn, created.slug)


def test_a_new_app_has_a_card_a_chat_can_draw(home: Home, client: TestClient) -> None:
    make_app(home)
    answer = client.get("/panel/apps/team-dashboard")
    assert answer.status_code == 200
    card = answer.json()
    assert card["ok"] is True
    assert card["name"] == "Team Dashboard"
    assert card["state"] == "green"  # the birth commit is a snapshot
    assert card["urls"]["dir"].endswith("team-dashboard")
    assert card["headline"]
    # Nothing in the card requires a second request to be readable.
    assert set(card) >= {"state", "headline", "urls", "errors", "commit"}


def test_a_machine_with_no_github_is_not_behind_on_github(
    home: Home, client: TestClient
) -> None:
    """Local commits are only "unpushed" when there is somewhere to push them."""
    make_app(home)
    card = client.get("/panel/apps/team-dashboard").json()
    assert card["unpushed"] >= 1
    assert "GitHub" not in card["headline"]


def test_a_red_write_shows_up_as_red_with_its_errors(home: Home, client: TestClient) -> None:
    import yaml

    paths, conn = home
    make_app(home)
    manifest = paths.stacks / "fake" / "stack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["commands"]["typecheck"] = "sh -c 'echo \"src/a.ts(1,1): error TS1: no.\"; exit 1'"
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    gate.write_files(paths, conn, app="team-dashboard", files_to_write={"src/a.ts": "x\n"})

    card = client.get("/panel/apps/team-dashboard").json()
    assert card["state"] == "red"
    assert card["failed_stage"] == "typecheck"
    assert card["errors"][0]["file"] == "src/a.ts"
    assert "typecheck" in card["headline"]


def test_a_human_editing_shows_in_the_card(home: Home, client: TestClient) -> None:
    paths, conn = home
    row = make_app(home)
    (Path(str(row["path"])) / "src" / "main.txt").write_text("theirs\n", encoding="utf-8")

    card = client.get("/panel/apps/team-dashboard").json()
    assert card["human"]["edits"] == ["src/main.txt"]
    assert "person" in card["headline"].lower()


def test_the_pull_request_survives_the_conversation(home: Home, client: TestClient) -> None:
    """A URL said once, in one message, is a URL lost (D15)."""
    paths, conn = home
    row = make_app(home)
    set_pull_request(conn, str(row["id"]), "https://github.com/acme/dash/pull/7")
    conn.commit()

    card = client.get("/panel/apps/team-dashboard").json()
    assert card["urls"]["pull_request"] == "https://github.com/acme/dash/pull/7"


def test_the_list_is_one_request(home: Home, client: TestClient) -> None:
    make_app(home, "Team Dashboard")
    make_app(home, "Billing Portal")
    apps = client.get("/panel/apps").json()["apps"]
    assert {app["app"] for app in apps} == {"team-dashboard", "billing-portal"}
    assert all(app["headline"] for app in apps)


def test_an_unknown_app_is_a_404_not_a_traceback(client: TestClient) -> None:
    answer = client.get("/panel/apps/nope")
    assert answer.status_code == 404
    assert answer.json()["ok"] is False


def test_the_screenshot_is_served_once_somebody_has_looked(
    home: Home, client: TestClient
) -> None:
    paths, _ = home
    make_app(home)
    assert client.get("/panel/apps/team-dashboard/shot.png").status_code == 404
    assert client.get("/panel/apps/team-dashboard").json()["shot"] is None

    paths.shots.mkdir(parents=True, exist_ok=True)
    paths.shot("team-dashboard").write_bytes(b"\x89PNG\r\n\x1a\n")
    answer = client.get("/panel/apps/team-dashboard/shot.png")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/png"
    card = client.get("/panel/apps/team-dashboard").json()
    assert card["shot"] == "/panel/apps/team-dashboard/shot.png"
    assert card["shot_at"] is not None


def payload(chunk: str) -> dict[str, Any]:
    """The JSON out of one `event: card\\ndata: {...}` frame."""
    return json.loads(chunk.split("data: ", 1)[1])


def test_the_stream_says_the_state_and_then_the_change(home: Home) -> None:
    """A chat window should not have to poll, and should not wait in silence."""
    paths, conn = home
    make_app(home)
    seen: list[dict[str, Any]] = []

    async def watch() -> None:
        async for chunk in panel.events(paths, "team-dashboard"):
            if not chunk.startswith("event: card"):
                continue
            seen.append(payload(chunk))
            if len(seen) == 1:
                # Something happens, and nobody tells the stream about it.
                gate.write_files(
                    paths, conn, app="team-dashboard",
                    files_to_write={"src/new.txt": "hello\n"},
                )
            else:
                return

    anyio.run(watch)
    assert seen[0]["state"] == "green"
    assert seen[1]["commit"] != seen[0]["commit"]
    assert "src/new.txt" not in json.dumps(seen[0])


def test_the_stream_keeps_quiet_but_not_silent(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minute of nothing must not look like a dropped connection."""
    monkeypatch.setattr(panel, "POLL", 0.01)
    monkeypatch.setattr(panel, "HEARTBEAT", 0.02)
    paths, _ = home
    make_app(home)
    chunks: list[str] = []

    async def watch() -> None:
        async for chunk in panel.events(paths, "team-dashboard"):
            chunks.append(chunk)
            if len(chunks) == 3:
                return

    anyio.run(watch)
    assert chunks[0].startswith("event: card")
    assert chunks[1:] == [": still here\n\n", ": still here\n\n"]


def test_the_stream_ends_when_the_app_does(home: Home) -> None:
    paths, _ = home
    chunks: list[str] = []

    async def watch() -> None:
        async for chunk in panel.events(paths, "never-existed"):
            chunks.append(chunk)

    anyio.run(watch)
    assert chunks == ['event: gone\ndata: {"app": "never-existed"}\n\n']


def test_the_stream_stops_when_the_reader_goes_away(home: Home) -> None:
    paths, _ = home
    make_app(home)
    chunks: list[str] = []

    async def gone() -> bool:
        return True

    async def watch() -> None:
        async for chunk in panel.events(paths, "team-dashboard", gone):
            chunks.append(chunk)

    anyio.run(watch)
    assert chunks == []


def test_the_embed_is_a_script_tag_and_nothing_else(client: TestClient) -> None:
    answer = client.get("/panel/embed.js")
    assert answer.status_code == 200
    assert "javascript" in answer.headers["content-type"]
    body = answer.text
    assert "customElements.define('applace-app'" in body
    # No bundler, no framework, no import: that is the whole point of D16.
    assert "import " not in body
    assert "react" not in body.lower()


def test_a_chat_ui_on_another_origin_can_read_it(client: TestClient) -> None:
    for route in ("/panel/apps", "/panel/embed.js"):
        assert client.get(route).headers["access-control-allow-origin"] == "*"


def test_the_page_a_human_opens_lists_the_apps(client: TestClient) -> None:
    answer = client.get("/panel")
    assert answer.status_code == 200
    assert "<applace-app" in answer.text or "panel/apps" in answer.text
