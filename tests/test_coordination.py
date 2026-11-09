"""Coordination stories: decisions, recruitment, feedback, requests, rotation, retirement."""

from __future__ import annotations

import pytest

from geyser import operator
from geyser.core import invariants, tasks
from geyser.core.models import TaskContract
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.scripted import ScriptedBackend, ScriptedTurn
from geyser.service import GeyserService
from tests.scripted import AcceptingReviewer, SimpleWorker, evidence_for


@pytest.fixture(autouse=True)
def invariants_hold(hub: Hub):
    yield
    assert invariants.check(hub) == []


def service(hub: Hub, behaviors, decider) -> GeyserService:
    hub.config.decisions.backend = "codex"
    return GeyserService(hub, {"codex": ScriptedBackend(behaviors, decider, name="codex")},
                         FakeHost())


def state(hub: Hub, task_id: str) -> str:
    with hub.read() as txn:
        return tasks.task_row(txn, task_id)["state"]


async def test_assignment_decision_reuses_an_idle_worker(hub: Hub) -> None:
    offered: list[list[str]] = []

    def decider(request):
        if '"collaborator"' in request.prompt:
            return {"collaborator": False}
        names = [line.strip().split('"')[1] for line in request.prompt.splitlines()
                 if line.strip().startswith('"worker-')]
        offered.append(names)
        return {"worker": names[0]}

    run = service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()}, decider)
    operator.publish_task(hub, TaskContract(title="First", objective="Write the first file.",
                                            acceptance=["Written."]))
    operator.run_action(hub, "start")
    await run.run_until_idle()
    assert state(hub, "task-0") == "completed"
    operator.publish_task(hub, TaskContract(title="Second", objective="Write the second file.",
                                            acceptance=["Written."]))
    await run.run_until_idle()
    assert state(hub, "task-1") == "open"  # Task 0's completion stopped autonomous work.
    operator.run_action(hub, "resume")
    await run.run_until_idle()
    assert state(hub, "task-1") == "completed"
    assert offered == [["worker-0"]]
    with hub.read() as txn:
        executor = txn.scalar("SELECT agent_id FROM memberships WHERE task_id = 'task-1'")
    assert executor == "worker-0"


class TeamWorker:
    """The executor contributes and completes; the collaborator keeps working and gives
    its one feedback opportunity without objections."""

    def __init__(self) -> None:
        self.simple = SimpleWorker()
        self.feedback_given = False

    async def __call__(self, turn: ScriptedTurn) -> None:
        info = await turn.call("inspect", {"id": "task-0"})
        if turn.agent_id == "worker-0":
            await self.simple(turn)
            return
        declaration = info.get("declaration")
        if declaration and declaration["state"] == "awaiting_feedback":
            await turn.call("completion_feedback", {"declaration_id": declaration["id"],
                                                    "objections": []})
            self.feedback_given = True
            return
        await turn.call("checkpoint", {"summary": "Reviewing the executor's work.",
                                       "next_action": "Give feedback when asked."})


async def test_recruitment_and_completion_feedback(hub: Hub) -> None:
    def decider(request):
        if '"collaborator"' in request.prompt:
            return {"collaborator": True}
        return {"worker": None}

    team = TeamWorker()
    run = service(hub, {"worker": team, "reviewer": AcceptingReviewer()}, decider)
    operator.setup_project(hub, title="Goal", goal="Write it together.", acceptance=["Done."])
    operator.run_action(hub, "start")
    await run.run_until_idle()
    assert state(hub, "task-0") == "completed"
    with hub.read() as txn:
        roles = {r["agent_id"]: r["role"] for r in txn.all(
            "SELECT agent_id, role FROM memberships WHERE task_id = 'task-0'")}
        declaration = txn.one("SELECT * FROM declarations WHERE task_id = 'task-0'")
    assert roles == {"worker-0": "executor", "worker-1": "collaborator"}
    assert declaration["feedback_from"] == "worker-1" and team.feedback_given


async def test_question_request_and_answer(hub: Hub) -> None:
    async def worker(turn: ScriptedTurn) -> None:
        info = await turn.call("inspect", {"listing": "requests"})
        if not info["requests"]:
            asked = await turn.call("request", {"kind": "question", "title": "Which format?",
                                                "text": "Markdown or plain text?"})
            await turn.call("pause", {
                "reason": "waiting for the operator",
                "checkpoint": {"summary": "Asked.", "next_action": "Read the answer."},
                "predicate": {"external_response_id": asked["request"]}})
            return
        assert info["requests"][0]["state"] == "answered"
        evidence = await evidence_for(turn, "task-0", [])
        await turn.call("complete", {"evidence": evidence})

    run = service(hub, {"worker": worker}, lambda request: {"worker": None,
                                                            "collaborator": False})
    operator.setup_project(hub, title="Goal", goal="Ask, then finish.", acceptance=["Done."])
    operator.run_action(hub, "start")
    await run.run_until_idle()
    assert state(hub, "task-0") == "waiting"
    assert [r["id"] for r in operator.requests_open(hub)] == ["request-0"]
    operator.answer_request(hub, "request-0", "Markdown.")
    await run.run_until_idle()
    assert state(hub, "task-0") == "completed"


async def test_conservative_rotation_after_idle_minutes(hub: Hub, clock) -> None:
    seen: list[bool] = []

    async def worker(turn: ScriptedTurn) -> None:
        seen.append(turn.new_conversation)
        await turn.call("checkpoint", {"summary": "s", "next_action": "n"})

    run = service(hub, {"worker": worker}, lambda request: {"worker": None,
                                                            "collaborator": False})
    operator.setup_project(hub, title="Goal", goal="Keep going.", acceptance=["Done."])
    operator.run_action(hub, "start")
    run._loop = __import__("asyncio").get_running_loop()
    for _ in range(4):
        await run.round()
        for task in list(run._activation_tasks.values()):
            await task
    assert seen[:3] == [True, False, False]
    clock.advance(41 * 60)
    await run.round()
    for task in list(run._activation_tasks.values()):
        await task
    assert seen[-1] is True


async def test_idle_workers_retire_after_the_configured_hours(hub: Hub, clock) -> None:
    run = service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()},
                  lambda request: {"worker": None, "collaborator": False})
    operator.publish_task(hub, TaskContract(title="Only", objective="Write the file.",
                                            acceptance=["Written."]))
    operator.run_action(hub, "start")
    await run.run_until_idle()
    clock.advance(49 * 3600)
    await run.round()
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM agents WHERE id = 'worker-0'") == "retired"
