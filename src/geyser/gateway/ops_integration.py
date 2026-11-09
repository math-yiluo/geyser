"""Integration operations of the Integrator (batch mode, DESIGN.md section 9.2)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from geyser.core.actors import Capability
from geyser.gateway.base import Arguments, CallContext, NoArguments, Operation
from geyser.ids import Kind, require_id
from geyser.integration import batch

INTEGRATOR = frozenset({Capability.INTEGRATOR})
INTEGRATOR_ANY = frozenset({Capability.INTEGRATOR, Capability.INTEGRATOR_ANSWER})


async def batch_status(call: CallContext, args: NoArguments) -> dict[str, Any]:
    return call.read(lambda txn: batch.status(call.hub, txn, call.actor))


async def batch_prepare(call: CallContext, args: NoArguments) -> dict[str, Any]:
    return await batch.prepare(call.hub, call.actor)


async def batch_resolve(call: CallContext, args: NoArguments) -> dict[str, Any]:
    return await batch.resolve(call.hub, call.actor)


async def batch_validate(call: CallContext, args: NoArguments) -> dict[str, Any]:
    return await batch.validate(call.hub, call.actor)


async def batch_publish(call: CallContext, args: NoArguments) -> dict[str, Any]:
    with call.hub.read() as txn:
        payload = batch.check_publishable(call.hub, txn, call.actor)

    def recheck(txn) -> None:
        batch.check_publishable(call.hub, txn, call.actor)

    return call.drain(payload, {"batch": payload["batch_id"], "publication": "accepted"},
                      recheck)


class DeferArguments(Arguments):
    candidate_id: str
    reason: str = Field(min_length=1, max_length=4_000)


async def batch_defer(call: CallContext, args: DeferArguments) -> dict[str, Any]:
    require_id(args.candidate_id, Kind.CANDIDATE, field="candidate_id")
    return call.commit(lambda txn: batch.defer(call.hub, txn, call.actor, args.candidate_id,
                                               args.reason))


class RetryArguments(Arguments):
    candidate_id: str
    evidence: str = Field(min_length=1, max_length=4_000)
    """The new evidence that resolves the blocker (a head change alone is not one)."""


async def batch_retry(call: CallContext, args: RetryArguments) -> dict[str, Any]:
    require_id(args.candidate_id, Kind.CANDIDATE, field="candidate_id")
    return call.commit(lambda txn: batch.retry(call.hub, txn, call.actor, args.candidate_id,
                                               args.evidence))


async def batch_finish_empty(call: CallContext, args: NoArguments) -> dict[str, Any]:
    return call.commit(lambda txn: batch.finish_empty(call.hub, txn, call.actor))


OPERATIONS = [
    Operation("batch_status", "integration", "Show the current batch, its members and their "
              "integration notes.", NoArguments, INTEGRATOR, batch_status, mutating=False),
    Operation("batch_prepare", "integration", "Build the combined workspace with explicit "
              "conflicts.", NoArguments, INTEGRATOR, batch_prepare, mutating=False),
    Operation("batch_resolve", "integration", "Stage the workspace as the resolved tree.",
              NoArguments, INTEGRATOR, batch_resolve, mutating=False),
    Operation("batch_validate", "integration", "Freeze the resolved tree and run the final "
              "checks.", NoArguments, INTEGRATOR, batch_validate, mutating=False),
    Operation("batch_publish", "integration", "Publish the exact validated round.",
              NoArguments, INTEGRATOR, batch_publish, drains=True),
    Operation("batch_defer", "integration", "Isolate a blocked member with a reason.",
              DeferArguments, INTEGRATOR, batch_defer),
    Operation("batch_retry", "integration", "Requeue a deferred contribution after new "
              "evidence.", RetryArguments, INTEGRATOR_ANY, batch_retry),
    Operation("batch_finish_empty", "integration", "End a batch whose members were all "
              "deferred.", NoArguments, INTEGRATOR, batch_finish_empty),
]
