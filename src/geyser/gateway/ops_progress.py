"""Progress operations: checkpoint, pause, resume_work, leave, sync, history_search."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from geyser.core import inbox, tasks, teams, waits
from geyser.core.actors import Capability
from geyser.core.ledger import Txn, dumps
from geyser.core.models import Checkpoint, Predicate, WaitCondition
from geyser.errors import Conflict, InvalidArgument
from geyser.gateway.base import (
    EVERY_AGENT,
    WORKERS_AND_REVIEWERS,
    Arguments,
    CallContext,
    NoArguments,
    Operation,
)
from geyser.ids import Kind, require_id


def _current_task(call: CallContext) -> str:
    if call.actor.task_id is None:
        raise Conflict("This turn has no current task.")
    return call.actor.task_id


class CheckpointArguments(Checkpoint):
    """Record where the work stands without pausing."""


async def checkpoint(call: CallContext, args: CheckpointArguments) -> dict[str, Any]:
    task_id = _current_task(call)

    def apply(txn: Txn) -> dict[str, Any]:
        cursor = txn.execute(
            "UPDATE memberships SET checkpoint = ? WHERE task_id = ? AND agent_id = ? "
            "AND state = 'active'",
            (dumps(Checkpoint.model_validate(args.model_dump()).model_dump()), task_id,
             call.actor.id),
        )
        if not cursor.rowcount:
            raise Conflict(f"{call.actor.id} is not actively executing {task_id}.")
        return {"task": tasks.title_of(txn, task_id), "checkpoint": "saved"}

    return call.commit(apply)


class PauseArguments(Arguments):
    reason: str = Field(min_length=1, max_length=4_000)
    checkpoint: Checkpoint
    predicate: WaitCondition
    """What ends the wait: subscriptions (another task's results), publications,
    validations or completion_declaration_id (your own submission), answers or
    external_response_id (an answer)."""


async def pause(call: CallContext, args: PauseArguments) -> dict[str, Any]:
    predicate = Predicate.model_validate(args.predicate.model_dump())
    if call.actor.review_id is not None:
        return _pause_review(call, args, predicate)
    task_id = _current_task(call)

    def check(txn: Txn) -> None:
        if txn.one("SELECT 1 FROM memberships WHERE task_id = ? AND agent_id = ? "
                   "AND state = 'active'", (task_id, call.actor.id)) is None:
            raise Conflict(f"{call.actor.id} is not actively executing {task_id}.")
        waits.check_condition(call.ctx, txn, call.actor.id, task_id, predicate)

    payload = {"task_id": task_id, "reason": args.reason,
               "checkpoint": args.checkpoint.model_dump(), "predicate": predicate.model_dump()}
    return call.drain(payload, {"task": task_id, "pause": "accepted",
                                "wakes_on": waits.describe(predicate)}, check)


def _pause_review(call: CallContext, args: PauseArguments, predicate: Predicate
                  ) -> dict[str, Any]:
    review_id = call.actor.review_id
    payload = {"review_id": review_id, "reason": args.reason,
               "checkpoint": args.checkpoint.model_dump(), "predicate": predicate.model_dump()}
    return call.drain(payload, {"review": review_id, "pause": "accepted",
                                "wakes_on": waits.describe(predicate)})


def commit_pause(hub_ctx, txn: Txn, actor, payload: dict[str, Any]) -> dict[str, Any]:
    """The drained commit of ``pause``, run once the turn's processes are quiet."""
    predicate = Predicate.model_validate(payload["predicate"]) if payload["predicate"] else None
    checkpoint_value = Checkpoint.model_validate(payload["checkpoint"])
    if payload.get("review_id"):
        return waits.pause_review(hub_ctx, txn, actor, payload["review_id"],
                                  payload["reason"], checkpoint_value, predicate)
    return waits.pause(hub_ctx, txn, actor, payload["task_id"], payload["reason"],
                       checkpoint_value, predicate)


async def resume_work(call: CallContext, args: NoArguments) -> dict[str, Any]:
    return call.commit(lambda txn: waits.resume_work(call.ctx, txn, call.actor))


class LeaveArguments(Arguments):
    reason: str = Field(min_length=1, max_length=4_000)
    task_id: str | None = None


async def leave(call: CallContext, args: LeaveArguments) -> dict[str, Any]:
    task_id = args.task_id or _current_task(call)
    require_id(task_id, Kind.TASK, field="task_id")

    def apply(txn: Txn) -> dict[str, Any]:
        reply = teams.leave(call.ctx, txn, call.actor, task_id, args.reason)
        reply["turn"] = "You left the task; end this turn."
        return reply

    return call.commit(apply)


async def sync(call: CallContext, args: NoArguments) -> dict[str, Any]:
    """Deliver notifications and messages not yet delivered, with the task's state."""
    if call.runtime is not None and hasattr(call.runtime, "sync"):
        return await call.runtime.sync(call)

    def apply(txn: Txn) -> dict[str, Any]:
        items = inbox.pending(txn, call.actor.id)
        if call.actor.activation_id:
            inbox.prepare(txn, [item["id"] for item in items], call.actor.activation_id)
            inbox.mark_delivered(txn, call.actor.activation_id)
        reply: dict[str, Any] = {"notifications": [item["summary"] for item in items]}
        if call.actor.task_id:
            row = tasks.task_row(txn, call.actor.task_id)
            reply["task"] = {"task": call.actor.task_id, "revision": row["revision"],
                             "state": row["state"]}
        return reply

    with call.hub.transaction() as txn:
        return apply(txn)


class HistoryArguments(Arguments):
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)


async def history_search(call: CallContext, args: HistoryArguments) -> dict[str, Any]:
    """Search this agent's own earlier conversations (section 7.4)."""
    if call.runtime is not None and hasattr(call.runtime, "history_search"):
        return await call.runtime.history_search(call.actor.id, args.query, args.limit)
    raise InvalidArgument("History search is unavailable outside a running activation.")


OPERATIONS = [
    Operation("checkpoint", "progress", "Save a checkpoint (summary, next_action) without "
              "pausing.", CheckpointArguments, frozenset({Capability.WORKER}), checkpoint),
    Operation("pause", "progress", "Pause your task with a checkpoint and a predicate naming "
              "what ends the wait (another task's results, your own submission, or an "
              "answer); ends the turn.", PauseArguments, WORKERS_AND_REVIEWERS, pause,
              drains=True),
    Operation("resume_work", "progress", "From a message turn: resume your paused work after "
              "this turn.", NoArguments, frozenset({Capability.MESSAGE_ONLY}), resume_work),
    Operation("leave", "progress", "Leave a task, after preserving your work, when nothing is "
              "left for you to do in it; an executor can invite you back.",
              LeaveArguments, frozenset({Capability.WORKER}), leave),
    Operation("sync", "progress", "Receive notifications and messages not yet delivered.",
              NoArguments, EVERY_AGENT, sync, mutating=False),
    Operation("history_search", "progress", "Search your own earlier conversations.",
              HistoryArguments, EVERY_AGENT, history_search, mutating=False),
]
