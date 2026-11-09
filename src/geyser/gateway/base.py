"""Operation definitions and the per-call context handlers receive."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from geyser.core import activations
from geyser.core.actors import Actor, Capability
from geyser.core.ledger import Txn, dumps
from geyser.jsonio import sha256_json

if TYPE_CHECKING:
    from geyser.hub import Hub


class Arguments(BaseModel):
    """Base for operation arguments: unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")


class NoArguments(Arguments):
    pass


Handler = Callable[["CallContext", Any], Awaitable[dict[str, Any]]]

WORKER = frozenset({Capability.WORKER})
TASK_TURN_AND_MESSAGE = frozenset({Capability.WORKER, Capability.MESSAGE_ONLY})
WORKERS_AND_REVIEWERS = frozenset({Capability.WORKER, Capability.REVIEWER})
EVERY_AGENT = frozenset({
    Capability.WORKER, Capability.MESSAGE_ONLY, Capability.REVIEWER,
    Capability.REVIEW_ANSWER, Capability.INTEGRATOR, Capability.INTEGRATOR_ANSWER,
})
READERS = EVERY_AGENT


@dataclass(frozen=True)
class Operation:
    name: str
    category: str
    summary: str
    arguments: type[BaseModel]
    capabilities: frozenset[Capability]
    handler: Handler
    mutating: bool = True
    drains: bool = False


@dataclass
class CallContext:
    """Everything one tool call needs; ``commit`` makes its effect durable exactly once."""

    hub: Hub
    actor: Actor
    operation: Operation
    call_id: str
    arguments_hash: str
    runtime: Any = None
    """The runtime hooks of the running activation (sync, history, drains), if any."""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ctx(self):  # noqa: ANN201 - HubContext, imported lazily to avoid a cycle
        return self.hub.ctx

    @property
    def receipt_key(self) -> str:
        return f"{self.actor.activation_id or self.actor.id}:{self.call_id}"

    def read(self, function: Callable[[Txn], dict[str, Any]]) -> dict[str, Any]:
        with self.hub.read() as txn:
            return function(txn)

    def commit(self, function: Callable[[Txn], dict[str, Any]]) -> dict[str, Any]:
        """Run ``function`` in one transaction that rechecks fencing and stores the receipt."""
        with self.hub.transaction() as txn:
            if self.actor.is_agent:
                activations.check_actor(txn, self.actor)
            reply = function(txn)
            self._store_receipt(txn, reply, "committed")
            return reply

    def drain(self, payload: dict[str, Any], reply: dict[str, Any],
              check: Callable[[Txn], None] | None = None) -> dict[str, Any]:
        """Accept a draining operation: the reply ends the turn; the commit waits."""
        with self.hub.transaction() as txn:
            activation = activations.check_actor(txn, self.actor)
            if check is not None:
                check(txn)
            activations.begin_drain(txn, activation["id"], self.operation.name, payload)
            full_reply = dict(reply)
            full_reply["turn"] = "ending: stop now and end this turn"
            self._store_receipt(txn, full_reply, "draining")
            return full_reply

    def _store_receipt(self, txn: Txn, reply: dict[str, Any], state: str) -> None:
        txn.execute(
            "INSERT INTO receipts (key, operation, actor, arguments_hash, state, reply, "
            "activation_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.receipt_key, self.operation.name, self.actor.id, self.arguments_hash, state,
             dumps(reply), self.actor.activation_id, txn.now),
        )


def arguments_hash(operation: str, arguments: dict[str, Any]) -> str:
    return sha256_json({"operation": operation, "arguments": arguments})
