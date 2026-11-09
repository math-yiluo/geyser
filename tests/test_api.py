"""The operator API: authentication, loopback checks and the main actions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.scripted import ScriptedBackend
from geyser.service import GeyserService

TOKEN = "test-token"


def client_for(hub: Hub) -> TestClient:
    from geyser.api.app import create_app

    service = GeyserService(hub, {"codex": ScriptedBackend({}, name="codex")}, FakeHost())
    return TestClient(create_app(service, TOKEN), base_url="http://127.0.0.1")


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_requests_need_the_token_and_a_loopback_host(hub: Hub) -> None:
    client = client_for(hub)
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=auth()).status_code == 200
    remote = TestClient(client.app, base_url="http://example.com")
    assert remote.get("/v1/status", headers=auth()).status_code == 403
    foreign = client.get("/v1/status", headers={**auth(), "Origin": "http://evil.example"})
    assert foreign.status_code == 403


def test_login_sets_a_cookie_and_serves_the_dashboard(hub: Hub) -> None:
    client = client_for(hub)
    assert client.get("/login", params={"token": "wrong"}).status_code == 401
    response = client.get("/login", params={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/")
    assert page.status_code == 200 and "<title>Geyser</title>" in page.text


def test_setup_run_and_errors(hub: Hub) -> None:
    client = client_for(hub)
    created = client.post("/v1/setup", headers=auth(), json={
        "title": "Goal", "goal": "Do it.", "acceptance": ["Done."]})
    assert created.status_code == 200 and created.json()["task_id"] == "task-0"
    again = client.post("/v1/setup", headers=auth(), json={
        "title": "Goal", "goal": "Do it.", "acceptance": ["Done."]})
    assert again.status_code == 409 and again.json()["error"] == "conflict"
    assert client.post("/v1/run/start", headers=auth()).json()["run"] == "running"
    assert client.post("/v1/run/pause-after-turn", headers=auth()).json()["run"] == \
        "pause_after_turn"
    tasks = client.get("/v1/tasks", headers=auth()).json()
    assert tasks[0]["id"] == "task-0"
    missing = client.post("/v1/messages", headers=auth(), json={"to": "worker-9", "text": "hi"})
    assert missing.status_code == 404
    status = client.get("/v1/status", headers=auth()).json()
    assert status["run"] == "pause_after_turn" and status["task0"]["state"] == "open"


async def test_turns_show_what_each_turn_received(hub: Hub) -> None:
    from geyser import operator
    from tests.scripted import AcceptingReviewer, SimpleWorker, no_decider

    hub.config.decisions.backend = "codex"
    backend = ScriptedBackend({"worker": SimpleWorker(), "reviewer": AcceptingReviewer()},
                              no_decider, name="codex")
    service = GeyserService(hub, {"codex": backend}, FakeHost())
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    first = operator.agent_turns(hub, "worker-0")[0]
    kinds = [event["type"] for event in first["events"]]
    assert kinds[:2] == ["start", "input"] and "tool_call" in kinds
    assert "task-0" in first["events"][1]["text"]
