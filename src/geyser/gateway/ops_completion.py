"""Completion operations: complete, completion_feedback, completion_finalize."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from geyser.core import completion, tasks
from geyser.core.actors import Actor, Capability
from geyser.core.ledger import Txn
from geyser.core.models import EvidenceEntry, Objection, ObjectionResolution
from geyser.errors import Conflict, Forbidden
from geyser.gateway.base import Arguments, CallContext, Operation
from geyser.ids import Kind, require_id

EXECUTORS = frozenset({Capability.WORKER})


class CompleteArguments(Arguments):
    evidence: dict[str, EvidenceEntry] = Field(min_length=1, max_length=400)
    """Evidence for every condition key, original and current."""


async def complete(call: CallContext, args: CompleteArguments) -> dict[str, Any]:
    task_id = call.actor.task_id
    if task_id is None:
        raise Conflict("This turn has no current task.")

    def check(txn: Txn) -> None:
        if call.actor.id not in tasks.member_ids(txn, task_id, states=("active",)):
            raise Forbidden(f"Only an active executor of {task_id} declares its completion.")
        completion._check_evidence(call.ctx, txn, task_id, args.evidence)

    payload = {"task_id": task_id,
               "evidence": {key: entry.model_dump() for key, entry in args.evidence.items()}}
    return call.drain(payload, {"task": task_id, "completion": "declared; it is recorded once "
                                "this turn's processes are quiet"}, check)


def commit_complete(hub_ctx, txn: Txn, actor: Actor, payload: dict[str, Any]
                    ) -> dict[str, Any]:
    evidence = {key: EvidenceEntry.model_validate(value)
                for key, value in payload["evidence"].items()}
    return completion.declare(hub_ctx, txn, actor, payload["task_id"], evidence)


class FeedbackArguments(Arguments):
    declaration_id: str
    objections: list[Objection] = Field(default_factory=list, max_length=100)
    """Concrete unmet conditions; empty means no objection."""


async def completion_feedback(call: CallContext, args: FeedbackArguments) -> dict[str, Any]:
    require_id(args.declaration_id, Kind.DECLARATION, field="declaration_id")
    return call.commit(lambda txn: completion.give_feedback(call.ctx, txn, call.actor,
                                                            args.declaration_id,
                                                            args.objections))


class FinalizeArguments(Arguments):
    declaration_id: str
    decision: Literal["complete", "continue"]
    resolutions: list[ObjectionResolution] = Field(default_factory=list, max_length=100)


async def completion_finalize(call: CallContext, args: FinalizeArguments) -> dict[str, Any]:
    require_id(args.declaration_id, Kind.DECLARATION, field="declaration_id")
    return call.commit(lambda txn: completion.finalize(call.ctx, txn, call.actor,
                                                       args.declaration_id, args.decision,
                                                       args.resolutions))


OPERATIONS = [
    Operation("complete", "completion", "Declare your task complete with evidence for every "
              "condition key; ends the turn.", CompleteArguments, EXECUTORS, complete,
              drains=True),
    Operation("completion_feedback", "completion", "Give your one feedback on a teammate's "
              "completion declaration.", FeedbackArguments, EXECUTORS, completion_feedback),
    Operation("completion_finalize", "completion", "Answer every objection and decide: "
              "complete or continue.", FinalizeArguments, EXECUTORS, completion_finalize),
]
