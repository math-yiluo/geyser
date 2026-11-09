"""Whole stories with scripted agents, driven by the real service steps."""

from __future__ import annotations

import pytest

from geyser import operator
from geyser.core import controls, invariants, tasks
from geyser.core.models import TaskContract
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.scripted import ScriptedBackend, ScriptedTurn
from geyser.service import GeyserService
from tests.scripted import AcceptingReviewer, SimpleWorker, evidence_for, no_decider


@pytest.fixture(autouse=True)
def invariants_hold(hub: Hub):
    yield
    assert invariants.check(hub) == []


def make_service(hub: Hub, behaviors, decider=no_decider) -> GeyserService:
    backend = ScriptedBackend(behaviors, decider, name="codex")
    hub.config.decisions.backend = "codex"
    return GeyserService(hub, {"codex": backend}, FakeHost())


def task_state(hub: Hub, task_id: str) -> str:
    with hub.read() as txn:
        return tasks.task_row(txn, task_id)["state"]


async def test_task_zero_completes_with_one_worker(hub: Hub) -> None:
    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()})
    operator.setup_project(hub, title="Write a result", goal="Write a result file.",
                           acceptance=["The result file exists."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    assert task_state(hub, "task-0") == "completed"
    assert (hub.layout.library / "task-0" / "result.md").exists()
    with hub.read() as txn:
        assert controls.run_state(txn) == controls.COMPLETED


async def test_nothing_runs_before_the_operator_starts(hub: Hub) -> None:
    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()})
    operator.setup_project(hub, title="Goal", goal="Do the goal.", acceptance=["Done."])
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT COUNT(*) FROM activations") == 0


async def test_rejection_and_correction(hub: Hub) -> None:
    reviewer = AcceptingReviewer(reject_first=1)
    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": reviewer})
    operator.setup_project(hub, title="Write a result", goal="Write a result file.",
                           acceptance=["The result file exists."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    assert task_state(hub, "task-0") == "completed"
    with hub.read() as txn:
        rows = txn.all("SELECT id, state, replaces FROM candidates ORDER BY id")
    assert [(r["id"], r["state"], r["replaces"]) for r in rows] == [
        ("cand-0", "rejected", None), ("cand-1", "published", "cand-0")]


class Decomposer:
    """Worker 0 publishes a subtask, waits for it, then completes Task 0."""

    def __init__(self) -> None:
        self.simple = SimpleWorker()

    async def __call__(self, turn: ScriptedTurn) -> None:
        if turn.agent_id != "worker-0":
            await self.simple(turn)
            return
        info = await turn.call("inspect", {"id": "task-0"})
        listing = await turn.call("inspect", {"listing": "tasks"})
        sub = [t for t in listing["tasks"] if t["task"].startswith("task-1 ")]
        if not sub:
            published = await turn.call("publish", {
                "title": "Write the part", "objective": "Write the part file.",
                "acceptance": ["The part file exists."]})
            assert published["task_id"] == "task-1", published
            await self.subscribe(turn, "the part file")
            return
        if sub[0]["state"] != "completed":
            await self.subscribe(turn, "its completion")  # Published, not yet completed.
            return
        evidence = await evidence_for(turn, "task-0", [])
        del info
        await turn.call("complete", {"evidence": evidence})

    @staticmethod
    async def subscribe(turn: ScriptedTurn, need: str) -> None:
        reply = await turn.call("pause", {
            "reason": "waiting for task-1",
            "checkpoint": {"summary": "Published task-1.", "next_action": "Complete."},
            "predicate": {"subscriptions": [{"task_id": "task-1", "need": need}]}})
        assert "error" not in reply, reply


async def test_subtask_wait_and_resume(hub: Hub) -> None:
    service = make_service(hub, {"worker": Decomposer(), "reviewer": AcceptingReviewer()})
    operator.setup_project(hub, title="Goal", goal="Do the goal via a subtask.",
                           acceptance=["The subtask is completed."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    assert task_state(hub, "task-1") == "completed"
    assert task_state(hub, "task-0") == "completed"
    with hub.read() as txn:
        executor = txn.scalar("SELECT agent_id FROM memberships WHERE task_id = 'task-1'")
        resumed = [row[0] for row in txn.all("SELECT data FROM events WHERE kind = "
                                             "'task.resumed' AND subject = 'task-0'")]
        failures = txn.scalar("SELECT COUNT(*) FROM activations WHERE outcome = 'failed'")
    assert executor == "worker-1"
    assert any("subscribed_task_changed" in data for data in resumed)  # The Waker woke it.
    assert failures == 0


async def test_idle_workers_answer_questions_but_notices_wake_nobody(hub: Hub) -> None:
    from geyser.core import agents, messages
    from geyser.core.actors import Actor, Capability
    from geyser.ids import Kind

    turns: list[tuple[str, str]] = []

    async def worker(turn: ScriptedTurn) -> None:
        for message in turn.view.get("messages", []):
            turns.append((turn.agent_id, message.get("kind", "")))
            if message.get("kind") == "question":
                assert "no task in hand" in turn.view["instruction"]
                await turn.call("talk_send", {"to": message["from"], "text": "The first.",
                                              "kind": "answer", "answers": message["message"]})

    service = make_service(hub, {"worker": worker})
    operator.run_action(hub, "start")
    with hub.transaction() as txn:
        asked = agents.create(hub.ctx, txn, Kind.WORKER)
        asker = agents.create(hub.ctx, txn, Kind.WORKER)
        messages.send_talk(hub.ctx, txn, Actor(asker, Capability.WORKER), asked,
                           "FYI, I keep notes in wiki/.")
        messages.send_talk(hub.ctx, txn, Actor(asker, Capability.WORKER), asked,
                           "Which method did you use?", form="question")
    await service.run_until_idle()
    # One message turn for the question (the notice rides along); the answer wakes the asker.
    assert turns == [(asked, "notice"), (asked, "question"), (asker, "answer")]


async def test_operator_message_wakes_a_waiting_worker(hub: Hub) -> None:
    seen: list[str] = []

    async def worker(turn: ScriptedTurn) -> None:
        capability = (turn.view.get("identity") or {}).get("capability")
        if turn.view.get("messages"):
            seen.append(turn.view["messages"][0]["sender_role"])
            await turn.call("resume_work")
            return
        if capability == "worker" and not seen:
            asked = await turn.call("request", {"kind": "question", "title": "Which way?",
                                                "text": "Which approach should I take?"})
            await turn.call("pause", {"reason": "need guidance",
                                      "checkpoint": {"summary": "s", "next_action": "ask"},
                                      "predicate": {"external_response_id": asked["request"]}})
            return
        evidence = await evidence_for(turn, "task-0", [])
        await turn.call("complete", {"evidence": evidence})

    service = make_service(hub, {"worker": worker})
    operator.setup_project(hub, title="Goal", goal="Do the goal.", acceptance=["Done."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    assert task_state(hub, "task-0") == "waiting"
    operator.send_message(hub, "worker-0", "Go ahead.")
    await service.run_until_idle()
    assert seen == ["operator"]
    assert task_state(hub, "task-0") == "completed"


async def test_a_failing_behavior_backs_off_without_stopping_the_run(hub: Hub) -> None:
    from geyser.runtime.backends.scripted import ScriptedFailure

    async def broken(turn: ScriptedTurn) -> None:
        raise ScriptedFailure("model_error")

    service = make_service(hub, {"worker": broken})
    operator.setup_project(hub, title="Goal", goal="Do the goal.", acceptance=["Done."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    with hub.read() as txn:
        agent = txn.one("SELECT failures, backoff_until FROM agents WHERE id = 'worker-0'")
        outcomes = [r["outcome"] for r in txn.all("SELECT outcome FROM activations")]
    assert agent["failures"] == 1 and agent["backoff_until"] is not None
    assert outcomes == ["failed"]


async def test_account_hold_stops_new_input_until_resume(hub: Hub) -> None:
    from geyser.runtime.backends.scripted import ScriptedFailure

    calls = {"n": 0}

    async def worker(turn: ScriptedTurn) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ScriptedFailure("usage_limit", account_hold="usage limit reached")
        await SimpleWorker()(turn)

    service = make_service(hub, {"worker": worker, "reviewer": AcceptingReviewer()})
    operator.setup_project(hub, title="Goal", goal="Write it.", acceptance=["Written."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    with hub.read() as txn:
        assert controls.held(txn, "codex")
    assert calls["n"] == 1
    operator.run_action(hub, "resume")
    await service.run_until_idle()
    assert task_state(hub, "task-0") == "completed"


async def test_batch_integration(hub: Hub) -> None:
    hub.config.integration.mode = "batch"
    hub.config.integration.batch_threshold = 1

    async def integrator(turn: ScriptedTurn) -> None:
        status = await turn.call("batch_status")
        if "error" in status:
            return
        prepared = await turn.call("batch_prepare")
        assert not prepared.get("conflicts"), prepared
        await turn.call("batch_resolve")
        validated = await turn.call("batch_validate")
        assert validated["verdict"] == "accepted", validated
        await turn.call("batch_publish")

    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer(),
                                 "integrator": integrator})
    operator.setup_project(hub, title="Goal", goal="Write it.", acceptance=["Written."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    assert task_state(hub, "task-0") == "completed"
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM batches WHERE id = 'batch-0'") == "published"


async def test_publication_of_a_conflicting_candidate_returns_it(hub: Hub) -> None:
    contract = TaskContract(title="A", objective="Write shared.txt as A.", acceptance=["x"])
    operator.publish_task(hub, contract)
    operator.publish_task(hub, TaskContract(title="B", objective="Write shared.txt as B.",
                                            acceptance=["x"]))

    async def writer(turn: ScriptedTurn) -> None:
        if "task" not in turn.view:
            return
        task_id = turn.view["task"]["task"].split(" ")[0]
        info = await turn.call("inspect", {"id": task_id})
        if info["candidates"]:
            return
        turn.write("shared.txt", f"{task_id}\n")
        await turn.call("stage", {"kind": "general.work", "paths": ["shared.txt"],
                                  "introduction": "x"})
        await turn.call("pause", {"reason": "done for now",
                                  "checkpoint": {"summary": "s", "next_action": "n"}})

    service = make_service(hub, {"worker": writer, "reviewer": AcceptingReviewer()})
    operator.run_action(hub, "start")
    await service.run_until_idle()
    with hub.read() as txn:
        states = sorted(r["state"] for r in txn.all("SELECT state FROM candidates"))
    assert states == ["published", "returned"]
