"""The whole core path without models: publish, contribute, review, publish, complete."""

from __future__ import annotations

from geyser.core import agents, candidates, controls, tasks, teams
from geyser.core.actors import OPERATOR
from geyser.core.models import TaskContract
from geyser.gateway import Gateway
from geyser.gateway.ops_completion import commit_complete
from geyser.hub import Hub
from geyser.ids import Kind
from geyser.integration.direct import integrate_next
from tests.conftest import end_turn, prepare_workspace, start_turn


def publish_task_zero(hub: Hub) -> str:
    contract = TaskContract(title="Write a greeting", objective="Write hello.txt saying hello.",
                            acceptance=["hello.txt contains the word hello"])
    with hub.transaction() as txn:
        return tasks.publish(hub.ctx, txn, OPERATOR, contract)["task_id"]


def assign_new_worker(hub: Hub, task_id: str) -> str:
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        place = txn.scalar("SELECT id FROM opportunities WHERE task_id = ? AND state = 'open'",
                           (task_id,))
        teams.fill(hub.ctx, txn, place, worker, by="test")
    prepare_workspace(hub, worker)
    return worker


async def test_publish_contribute_review_publish_complete(hub: Hub, gateway: Gateway) -> None:
    task_id = publish_task_zero(hub)
    assert task_id == "task-0"
    worker = assign_new_worker(hub, task_id)
    assert worker == "worker-0"

    actor = start_turn(hub, worker, task_id=task_id)
    (hub.layout.workspace(worker) / "hello.txt").write_text("hello\n", encoding="utf-8")
    staged = await gateway.call(actor, "stage", {
        "kind": "general.work", "paths": ["hello.txt"], "introduction": "The greeting."},
        call_id="c1")
    assert staged.get("candidate") == "cand-0", staged
    assert staged["state"] == "reviewing"  # general.review@1 has no machine validator

    # A retry of the same call returns the retained reply.
    again = await gateway.call(actor, "stage", {
        "kind": "general.work", "paths": ["hello.txt"], "introduction": "The greeting."},
        call_id="c1")
    assert again["candidate"] == "cand-0" and again["replayed"] is True

    # The author may not review its own candidate; a fresh reviewer does.
    with hub.transaction() as txn:
        review = candidates.waiting_reviews(txn)[0]
        reviewer = agents.create(hub.ctx, txn, Kind.REVIEWER)
        candidates.assign_reviewer(hub.ctx, txn, review["id"], reviewer)
    reviewer_actor = start_turn(hub, reviewer, "review", review_id=review["id"])
    verdict = await gateway.call(reviewer_actor, "review", {
        "decision": "accept", "summary": "hello.txt says hello.",
        "findings": [{"summary": "Contains hello", "location": "hello.txt:1"}]},
        call_id="r1")
    assert verdict["decision"] == "accept", verdict

    published = await integrate_next(hub)
    assert published == "cand-0"
    assert (hub.layout.library / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    with hub.read() as txn:
        assert candidates.candidate_row(txn, "cand-0")["state"] == "published"
        assert agents.agent_row(txn, reviewer)["state"] == "retired"

    # Completion drains: the reply ends the turn, the commit follows quiescence.
    evidence = {"objective": {"explanation": "Written.", "artifacts": ["cand-0"]},
                "acceptance:1": {"explanation": "It says hello.", "artifacts": ["cand-0"]}}
    reply = await gateway.call(actor, "complete", {"evidence": evidence}, call_id="c2")
    assert reply["turn"].startswith("ending"), reply
    end_turn(hub, actor)
    with hub.transaction() as txn:
        result = commit_complete(hub.ctx, txn, agents_actor(actor), {
            "task_id": task_id, "evidence": evidence})
    assert result["state"] == "completed"
    with hub.read() as txn:
        assert tasks.task_row(txn, task_id)["state"] == "completed"
        assert controls.run_state(txn) == controls.COMPLETED
        assert agents.agent_row(txn, worker)["current_task"] is None


def agents_actor(actor):
    return actor


async def test_fencing_refuses_a_stale_turn(hub: Hub, gateway: Gateway) -> None:
    task_id = publish_task_zero(hub)
    worker = assign_new_worker(hub, task_id)
    old = start_turn(hub, worker, task_id=task_id)
    end_turn(hub, old)
    start_turn(hub, worker, task_id=task_id)
    reply = await gateway.call(old, "checkpoint", {"summary": "s", "next_action": "n"},
                               call_id="x")
    assert reply["error"] == "stale_actor"


async def test_capabilities_are_enforced(hub: Hub, gateway: Gateway) -> None:
    task_id = publish_task_zero(hub)
    worker = assign_new_worker(hub, task_id)
    actor = start_turn(hub, worker, task_id=task_id)
    reply = await gateway.call(actor, "review", {"decision": "accept", "summary": "x"},
                               call_id="x")
    assert reply["error"] == "forbidden"
    listed = gateway.actions(actor)["operations"]
    assert "review" not in {entry["operation"] for entry in listed}
    assert "stage" in {entry["operation"] for entry in listed}


async def test_material_revision_invalidates_candidates(hub: Hub, gateway: Gateway) -> None:
    task_id = publish_task_zero(hub)
    worker = assign_new_worker(hub, task_id)
    actor = start_turn(hub, worker, task_id=task_id)
    (hub.layout.workspace(worker) / "hello.txt").write_text("hi\n", encoding="utf-8")
    await gateway.call(actor, "stage", {"kind": "general.work", "paths": ["hello.txt"],
                                        "introduction": "x"}, call_id="s")
    revised = await gateway.call(actor, "propose_revision", {
        "task_id": task_id, "expected_revision": 0, "reason": "be precise",
        "changes": {"objective": "Write hello.txt saying exactly 'hello'."}}, call_id="r")
    assert revised["change"] == "material", revised
    with hub.read() as txn:
        assert candidates.candidate_row(txn, "cand-0")["state"] == "invalidated"
    removed = await gateway.call(actor, "propose_revision", {
        "task_id": task_id, "expected_revision": 1, "reason": "drop it",
        "changes": {"acceptance": []}}, call_id="r2")
    assert removed["error"] == "invalid_argument"
