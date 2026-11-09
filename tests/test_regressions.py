"""Regression tests for the robustness findings of the first code review."""

from __future__ import annotations

import asyncio
from pathlib import Path

from geyser import operator
from geyser.clock import ManualClock
from geyser.config import ProjectConfig
from geyser.core import activations, agents, completion, controls, invariants, tasks, teams
from geyser.core.actors import Actor, Capability
from geyser.core.models import EvidenceEntry, TaskContract, required_condition_keys
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.ids import Kind
from geyser.runtime.backends.scripted import ScriptedBackend
from geyser.service import GeyserService
from tests.conftest import chosen
from tests.scripted import AcceptingReviewer, SimpleWorker, no_decider


def make_service(hub: Hub, behaviors, decider=no_decider) -> GeyserService:
    hub.config.decisions.backend = "codex"
    service = GeyserService(hub, {"codex": ScriptedBackend(behaviors, decider, name="codex")},
                            FakeHost())
    service._loop = asyncio.get_running_loop()
    return service


async def rounds(service: GeyserService, count: int) -> None:
    for _ in range(count):
        await service.round()
        await asyncio.gather(*service._activation_tasks.values(), return_exceptions=True)


async def test_a_failing_drain_does_not_stall_the_service(hub: Hub, monkeypatch) -> None:
    import geyser.content.submit as submit

    def broken_capture(*args, **kwargs):
        raise PermissionError("a file in the workspace is unreadable")

    monkeypatch.setattr(submit, "capture", broken_capture)
    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()})
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    operator.run_action(hub, "start")
    await rounds(service, 2)
    operator.publish_task(hub, TaskContract(title="Other", objective="Do another thing.",
                                            acceptance=["Done."]))
    await rounds(service, 3)  # No exception escapes a round.
    with hub.read() as txn:
        assert txn.scalar("SELECT COUNT(*) FROM memberships WHERE task_id = 'task-1'") == 1
        drain = txn.one("SELECT state, drain FROM activations WHERE agent_id = 'worker-0' "
                        "AND purpose = 'task' ORDER BY planned_at LIMIT 1")
    assert drain["state"] == "draining" and '"attempts":1' in drain["drain"]
    # After the retries, the drain is abandoned with a reason and the agent is told.
    clock = hub.clock
    for minutes in (1, 4, 15):
        clock.advance(minutes * 60 + 1)  # type: ignore[attr-defined]
        await rounds(service, 1)
    with hub.read() as txn:
        outcome = txn.scalar("SELECT outcome FROM activations WHERE agent_id = 'worker-0' "
                             "ORDER BY planned_at LIMIT 1")
    assert outcome == "abandoned"


async def test_operator_cancel_resumes_the_declarer(hub: Hub) -> None:
    hub.config.teams.collaborators = 1
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    operator.run_action(hub, "start")
    with hub.transaction() as txn:
        w0 = agents.create(hub.ctx, txn, Kind.WORKER)
        place = txn.scalar("SELECT id FROM opportunities WHERE task_id = 'task-0'")
        teams.fill(hub.ctx, txn, place, w0, by="test")
        teams.record_recruitment(hub.ctx, txn, "task-0", True)
        w1 = agents.create(hub.ctx, txn, Kind.WORKER)
        place = txn.scalar("SELECT id FROM opportunities WHERE task_id = 'task-0' "
                           "AND state = 'open'")
        teams.fill(hub.ctx, txn, place, w1, by="test")
        row = activations.plan(txn, w0, "task", task_id="task-0")
        activations.mark_running(txn, row["id"], None)
        actor = Actor(w0, Capability.WORKER, row["id"], row["fence"], "task-0")
        keys = required_condition_keys(tasks.load_contract(hub.ctx, txn, "task-0", 0),
                                       tasks.load_contract(hub.ctx, txn, "task-0"))
        evidence = {k: EvidenceEntry(explanation="done", artifacts=[]) for k in keys}
        declared = completion.declare(hub.ctx, txn, actor, "task-0", evidence)
        activations.end(txn, row["id"], "completed")
    operator.cancel_declaration(hub, declared["declaration"], "worker-1 is unrecoverable")
    with hub.read() as txn:
        assert agents.agent_row(txn, w0)["intent"] == "resume"


async def test_feedback_lapses_when_the_teammate_pauses(hub: Hub) -> None:
    hub.config.teams.collaborators = 1
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    with hub.transaction() as txn:
        w0 = agents.create(hub.ctx, txn, Kind.WORKER)
        place = txn.scalar("SELECT id FROM opportunities WHERE task_id = 'task-0'")
        teams.fill(hub.ctx, txn, place, w0, by="test")
        teams.record_recruitment(hub.ctx, txn, "task-0", True)
        w1 = agents.create(hub.ctx, txn, Kind.WORKER)
        place = txn.scalar("SELECT id FROM opportunities WHERE task_id = 'task-0' "
                           "AND state = 'open'")
        teams.fill(hub.ctx, txn, place, w1, by="test")
        keys = required_condition_keys(tasks.load_contract(hub.ctx, txn, "task-0", 0),
                                       tasks.load_contract(hub.ctx, txn, "task-0"))
        evidence = {k: EvidenceEntry(explanation="done", artifacts=[]) for k in keys}
        completion.declare(hub.ctx, txn, Actor(w0, Capability.WORKER), "task-0", evidence)
        from geyser.core import waits
        from geyser.core.models import Checkpoint, Predicate

        declaration = txn.scalar("SELECT id FROM declarations WHERE task_id = 'task-0'")
        waits.pause(hub.ctx, txn, Actor(w1, Capability.WORKER), "task-0", "stepping back",
                    Checkpoint(summary="s", next_action="n"),
                    Predicate(completion_declaration_id=declaration))
        assert tasks.task_row(txn, "task-0")["state"] == "completed"


async def test_pending_decisions_respect_task_zero_completion(hub: Hub) -> None:
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    operator.run_action(hub, "start")
    with hub.transaction() as txn:
        agents.create(hub.ctx, txn, Kind.WORKER)  # An idle worker forces a decision.
    operator.publish_task(hub, TaskContract(title="Other", objective="Do another thing.",
                                            acceptance=["Done."]))
    service = make_service(hub, {"*": lambda turn: asyncio.sleep(0)})
    service.admission.run_pass()
    with hub.transaction() as txn:
        controls.set_run(txn, controls.COMPLETED)
        before = txn.scalar("SELECT COUNT(*) FROM memberships")
    await service.decisions.run_pending()
    with hub.read() as txn:
        assert txn.scalar("SELECT COUNT(*) FROM memberships") == before
        assert txn.scalar("SELECT COUNT(*) FROM decisions WHERE state = 'pending'") >= 1


async def test_contribute_recovery_never_duplicates_a_candidate(tmp_path: Path) -> None:
    path = tmp_path / "p"
    hub = Hub.initialize(path, chosen(ProjectConfig(name="dup")), clock=ManualClock())
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    operator.run_action(hub, "start")

    class Crash(BaseException):
        pass

    state = {"crashed": False}

    def hook() -> None:
        with hub.read() as txn:
            count = txn.scalar("SELECT COUNT(*) FROM candidates")
        if count == 1 and not state["crashed"]:
            state["crashed"] = True
            raise Crash()

    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()})
    hub.ledger.after_commit_hook = hook
    try:
        for _ in range(10):
            try:
                await service.round()
            except Crash:
                break
            results = await asyncio.gather(*service._activation_tasks.values(),
                                           return_exceptions=True)
            if any(isinstance(r, Crash) for r in results) or state["crashed"]:
                break
    finally:
        hub.ledger.after_commit_hook = None
        for task in list(service._activation_tasks.values()):
            task.cancel()
    assert state["crashed"]
    hub.close()
    restarted = Hub(path, clock=ManualClock())
    service = make_service(restarted, {"worker": SimpleWorker(),
                                       "reviewer": AcceptingReviewer()})
    await service.run_until_idle()
    with restarted.read() as txn:
        candidates = txn.scalar("SELECT COUNT(*) FROM candidates")
        state_now = tasks.task_row(txn, "task-0")["state"]
    problems = invariants.check(restarted)
    restarted.close()
    assert candidates == 1 and state_now == "completed" and problems == []


async def test_one_failing_candidate_does_not_block_direct_integration(
        hub: Hub, monkeypatch) -> None:
    from geyser.integration import direct

    original = direct._integrate
    failing = {"cand-0"}

    async def flaky(hub_arg, row):
        if row["id"] in failing:
            raise RuntimeError("git exploded")
        return await original(hub_arg, row)

    monkeypatch.setattr(direct, "_integrate", flaky)
    for title in ("A", "B"):
        operator.publish_task(hub, TaskContract(title=title, objective=f"Write {title}.",
                                                acceptance=["Written."]))
    service = make_service(hub, {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()})
    operator.run_action(hub, "start")
    await service.run_until_idle()
    with hub.read() as txn:
        states = {r["id"]: r["state"] for r in txn.all("SELECT id, state FROM candidates")}
    assert states["cand-0"] == "accepted" and states["cand-1"] == "published"
    failing.clear()
    hub.clock.advance(61)  # type: ignore[attr-defined]
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM candidates WHERE id = 'cand-0'") == "published"


async def test_invalidated_batch_members_are_not_published(hub: Hub) -> None:
    hub.config.integration.mode = "batch"
    hub.config.integration.batch_threshold = 1
    with hub.transaction() as txn:
        controls.set_run(txn, controls.RUNNING)
    from geyser.content.git import Entry
    from geyser.core import candidates
    from geyser.integration import batch, publication

    operator.publish_task(hub, TaskContract(title="A", objective="Write A.",
                                            acceptance=["Written."]))
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        teams.operator_claim(hub.ctx, txn, "task-0", worker)
        base = hub.library.head()
        txn.execute(
            "INSERT INTO candidates (id, task_id, task_revision, kind, policy, submitter, "
            "authors, base_commit, manifest_hash, state, created_at, decided_at) VALUES "
            "('cand-0', 'task-0', 0, 'general.work', 'general.review@1', ?, ?, ?, 'x', "
            "'accepted', ?, ?)", (worker, f'["{worker}"]', base, txn.now, txn.now))
        txn.execute("INSERT INTO batches (id, state, members, created_at) VALUES "
                    "('batch-0', 'open', '[\"cand-0\"]', ?)", (txn.now,))
        txn.execute("UPDATE candidates SET batch_id = 'batch-0' WHERE id = 'cand-0'")
        candidates.invalidate_for_material_revision(hub.ctx, txn, "task-0", 1)
        members = txn.scalar("SELECT members FROM batches WHERE id = 'batch-0'")
    assert members == "[]"
    repo = hub.library.repo
    tree = repo.build_tree(base, {"a.txt": Entry("100644", repo.write_blob(b"a\n"))})
    try:
        publication.publish(hub, "batch", ["cand-0"], tree, base, "should refuse")
    except Exception as error:  # noqa: BLE001
        assert "no longer accepted" in str(error)
    else:
        raise AssertionError("an invalidated candidate was published")
    assert hub.library.head() == base
    del batch
