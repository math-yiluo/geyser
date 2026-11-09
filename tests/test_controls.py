"""Operator controls: pause interrupts turns; pause after turn lets them finish."""

from __future__ import annotations

import asyncio

from geyser import operator
from geyser.core import controls, invariants
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.scripted import ScriptedBackend, ScriptedTurn
from geyser.service import GeyserService
from tests.scripted import AcceptingReviewer, SimpleWorker, no_decider


def make(hub: Hub, worker) -> GeyserService:
    hub.config.decisions.backend = "codex"
    backend = ScriptedBackend({"worker": worker, "reviewer": AcceptingReviewer()}, no_decider,
                              name="codex")
    return GeyserService(hub, {"codex": backend}, FakeHost())


async def wait_until_live(service: GeyserService) -> None:
    for _ in range(200):
        if service.runner.live:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no turn started")


async def test_pause_interrupts_and_resume_continues(hub: Hub) -> None:
    simple = SimpleWorker()
    first = {"waited": False}

    async def worker(turn: ScriptedTurn) -> None:
        if not first["waited"]:
            first["waited"] = True
            try:
                await asyncio.wait_for(turn.request.control.interrupt.wait(), timeout=5)
            except TimeoutError:
                pass
            return
        await simple(turn)

    service = make(hub, worker)
    operator.setup_project(hub, title="Goal", goal="Write it.", acceptance=["Written."])
    operator.run_action(hub, "start")
    driving = asyncio.create_task(service.run_until_idle())
    await wait_until_live(service)
    operator.run_action(hub, "pause", service)
    await driving
    with hub.read() as txn:
        outcomes = [r["outcome"] for r in txn.all("SELECT outcome FROM activations")]
        intent = txn.scalar("SELECT intent FROM agents WHERE id = 'worker-0'")
        assert controls.run_state(txn) == controls.PAUSED
    assert outcomes == ["interrupted"] and intent == "continue"
    operator.run_action(hub, "resume")
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM tasks WHERE id = 'task-0'") == "completed"
    assert invariants.check(hub) == []


async def test_agent_pause_keeps_the_run_going(hub: Hub) -> None:
    service = make(hub, SimpleWorker())
    operator.setup_project(hub, title="Goal", goal="Write it.", acceptance=["Written."])
    with hub.transaction() as txn:
        from geyser.core import agents, teams
        from geyser.ids import Kind

        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        teams.operator_claim(hub.ctx, txn, "task-0", worker)
    operator.run_action(hub, "start")  # Starting (like resume all) ends narrower controls,
    operator.agent_action(hub, "worker-0", "pause")  # so the agent is paused afterwards.
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT COUNT(*) FROM activations WHERE agent_id = 'worker-0'") == 0
    operator.agent_action(hub, "worker-0", "resume")
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM tasks WHERE id = 'task-0'") == "completed"


def test_narrow_controls_follow_precedence(hub: Hub) -> None:
    from geyser.core import agents
    from geyser.ids import Kind

    with hub.transaction() as txn:
        assert agents.create(hub.ctx, txn, Kind.WORKER) == "worker-0"
        controls.set_run(txn, controls.PAUSED)
        controls.set_narrow(txn, "group:codex.workers", controls.RUNNING)
        resolution = controls.resolve(txn, None, "codex", "worker")
        assert resolution.may_start and resolution.new_work
        controls.set_narrow(txn, "agent:worker-0", controls.RUNNING)
        assert txn.scalar("SELECT COUNT(*) FROM controls WHERE scope = 'agent:worker-0'") == 0
        controls.set_narrow(txn, "agent:worker-0", controls.PAUSED)
        assert not controls.resolve(txn, "worker-0", "codex", "worker").may_start
        controls.set_run(txn, controls.RUNNING)
        assert txn.scalar("SELECT COUNT(*) FROM controls WHERE scope != 'run'") == 0
