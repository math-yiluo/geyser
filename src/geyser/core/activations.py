"""Activations: one model turn in a fresh owned process tree (DESIGN.md sections 7 and 14).

Every activation carries a fencing token. Planning an activation increments the
agent's fence, so a stale execution (an older activation, or one whose review was
cancelled) is refused on its next mutation. Elapsed time never revokes authority.
"""

from __future__ import annotations

from sqlite3 import Row
from typing import Any

from geyser.core import inbox
from geyser.core.actors import Actor
from geyser.core.ledger import Txn, dumps, loads
from geyser.errors import StaleActor
from geyser.ids import Kind

LIVE_STATES = ("planned", "running", "draining")


def plan(txn: Txn, agent_id: str, purpose: str, *, task_id: str | None = None,
         review_id: str | None = None, batch_id: str | None = None,
         new_conversation: bool = False) -> Row:
    """Record an activation before any process exists, with a fresh fencing token."""
    txn.execute("UPDATE agents SET fence = fence + 1, last_activation_at = ? WHERE id = ?",
                (txn.now, agent_id))
    fence = txn.scalar("SELECT fence FROM agents WHERE id = ?", (agent_id,))
    activation_id = txn.next_id(Kind.ACTIVATION)
    txn.execute(
        "INSERT INTO activations (id, agent_id, purpose, task_id, review_id, batch_id, state, "
        "fence, new_conversation, planned_at) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)",
        (activation_id, agent_id, purpose, task_id, review_id, batch_id, fence,
         int(new_conversation), txn.now),
    )
    txn.event("activation.planned", activation_id, agent_id, purpose=purpose, task=task_id,
              review=review_id, batch=batch_id)
    row = txn.one("SELECT * FROM activations WHERE id = ?", (activation_id,))
    assert row is not None
    return row


def get(txn: Txn, activation_id: str) -> Row | None:
    return txn.one("SELECT * FROM activations WHERE id = ?", (activation_id,))


def live_for(txn: Txn, agent_id: str) -> Row | None:
    return txn.one(
        "SELECT * FROM activations WHERE agent_id = ? AND state IN ('planned', 'running', "
        "'draining') ORDER BY planned_at DESC LIMIT 1",
        (agent_id,),
    )


def all_live(txn: Txn) -> list[Row]:
    return txn.all("SELECT * FROM activations WHERE state IN ('planned', 'running', "
                   "'draining') ORDER BY planned_at")


def record_scope(txn: Txn, activation_id: str, scope: str | None) -> None:
    txn.execute("UPDATE activations SET scope = ? WHERE id = ?", (scope, activation_id))


def mark_running(txn: Txn, activation_id: str, conversation_id: str | None) -> None:
    txn.execute(
        "UPDATE activations SET state = 'running', started_at = ?, conversation_id = ? "
        "WHERE id = ? AND state = 'planned'",
        (txn.now, conversation_id, activation_id),
    )


def begin_drain(txn: Txn, activation_id: str, operation: str, payload: dict[str, Any]) -> None:
    """A draining operation's reply ends the turn; its commit waits for quiescence."""
    txn.execute(
        "UPDATE activations SET state = 'draining', drain = ? WHERE id = ? AND state = 'running'",
        (dumps({"operation": operation, "payload": payload}), activation_id),
    )


def drain_of(row: Row) -> dict[str, Any] | None:
    return loads(row["drain"])


def end(txn: Txn, activation_id: str, outcome: str, detail: dict[str, Any] | None = None
        ) -> None:
    """End an activation. Prepared deliveries are settled by outcome."""
    row = get(txn, activation_id)
    if row is None or row["state"] == "ended":
        return
    txn.execute(
        "UPDATE activations SET state = 'ended', outcome = ?, detail = ?, ended_at = ? "
        "WHERE id = ?",
        (outcome, dumps(detail), txn.now, activation_id),
    )
    if outcome == "closed_unknown":
        inbox.mark_uncertain(txn, activation_id)
    elif row["started_at"] is None:
        inbox.release(txn, activation_id)
    txn.event("activation.ended", activation_id, row["agent_id"], outcome=outcome,
              detail=detail or {})


def check_actor(txn: Txn, actor: Actor, *, allow_draining: bool = False) -> Row:
    """Fencing: the caller must be its agent's live, running activation."""
    if actor.activation_id is None:
        raise StaleActor("The call has no activation.")
    row = get(txn, actor.activation_id)
    current_fence = txn.scalar("SELECT fence FROM agents WHERE id = ?", (actor.id,))
    allowed = ("running", "draining") if allow_draining else ("running",)
    if (
        row is None
        or row["agent_id"] != actor.id
        or row["state"] not in allowed
        or row["fence"] != actor.fence
        or current_fence != actor.fence
    ):
        if row is not None and row["state"] == "draining":
            raise StaleActor("This turn is ending: a draining operation already replied. "
                             "End the turn now.")
        raise StaleActor("This execution is no longer the live one; end the turn.")
    return row
