"""A parked reviewer answers a question about its review in a new conversation."""

from __future__ import annotations

from geyser import operator
from geyser.core import invariants
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.scripted import ScriptedBackend, ScriptedTurn
from geyser.service import GeyserService
from tests.scripted import AcceptingReviewer, no_decider


async def test_parked_reviewer_answers_with_its_report(hub: Hub) -> None:
    hub.config.integration.mode = "batch"  # Keep the candidate accepted, not yet published.
    hub.config.integration.batch_threshold = 99
    hub.config.integration.batch_delay_minutes = 10_000
    answers: list[dict] = []
    reviewer = AcceptingReviewer()

    async def worker(turn: ScriptedTurn) -> None:
        info = await turn.call("inspect", {"id": "task-0"})
        if not info["candidates"]:
            turn.write("task-0/result.md", "done\n")
            await turn.call("contribute", {"kind": "general.work",
                                           "paths": ["task-0/result.md"],
                                           "introduction": "The result."})

    async def review(turn: ScriptedTurn) -> None:
        if turn.request.role == "reviewer" and turn.view.get("identity", {}).get(
                "capability") == "review_answer":
            answers.append(turn.view)
            await turn.call("talk_send", {"to": "worker-0", "text": "It meets condition 1."})
            return
        await reviewer(turn)

    backend = ScriptedBackend({"worker": worker, "reviewer": review}, no_decider, name="codex")
    hub.config.decisions.backend = "codex"
    service = GeyserService(hub, {"codex": backend}, FakeHost())
    operator.setup_project(hub, title="Goal", goal="Write it.", acceptance=["Written."])
    operator.run_action(hub, "start")
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM agents WHERE id = 'reviewer-0'") == "parked"
    with hub.transaction() as txn:
        from geyser.core import messages
        from geyser.core.actors import Actor, Capability

        messages.send_talk(hub.ctx, txn, Actor("worker-0", Capability.WORKER), "reviewer-0",
                           "Why did you accept condition 1?")
    await service.run_until_idle()
    assert len(answers) == 1
    view = answers[0]
    assert view["review"]["review"] == "review-0"
    assert view["review"]["report"].endswith("report.md")
    assert view["messages"][0]["from"] == "worker-0"
    assert invariants.check(hub) == []
