"""Agents: creation, idleness and retirement (DESIGN.md section 6)."""

from __future__ import annotations

from datetime import timedelta
from sqlite3 import Row

from geyser.clock import iso, parse_iso
from geyser.core import choices, controls, inbox
from geyser.core.context import HubContext
from geyser.core.ledger import Txn
from geyser.errors import Conflict, InvalidArgument, NotFound
from geyser.ids import Kind, format_id


def agent_row(txn: Txn, agent_id: str) -> Row:
    row = txn.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if row is None:
        raise NotFound(f"{agent_id} does not exist.")
    return row


def backend_for_new(ctx: HubContext, txn: Txn, kind: Kind, number: int) -> str:
    """Pick the backend of a new agent (section 6.1).

    Reviewers and the Integrator run on their role's backend. A worker's backend is
    fixed by the assignment plan for its number, or else is the operator's runtime
    choice (``meta.new_backend``) or ``agents.new_backend``.
    """
    if kind is Kind.REVIEWER:
        return choices.role_choice(ctx, txn, "reviewers").backend
    if kind is Kind.INTEGRATOR:
        return choices.role_choice(ctx, txn, "integrator").backend
    plan = ctx.config.assignment
    if number < len(plan.worker_backends):
        return plan.worker_backends[number]
    return txn.meta("new_backend") or ctx.config.agents.new_backend


def create(ctx: HubContext, txn: Txn, kind: Kind) -> str:
    """Create an agent with the next number of its kind."""
    if kind is Kind.INTEGRATOR:
        existing = txn.scalar("SELECT id FROM agents WHERE kind = 'integrator'")
        if existing:
            return existing
    number = txn.next_number(kind)
    agent_id = format_id(kind, number)
    backend = backend_for_new(ctx, txn, kind, number)
    txn.execute(
        "INSERT INTO agents (id, kind, number, backend, state, created_at) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        (agent_id, kind_name(kind), number, backend, txn.now),
    )
    txn.event("agent.created", agent_id, backend=backend)
    return agent_id


def kind_name(kind: Kind) -> str:
    return {Kind.WORKER: "worker", Kind.REVIEWER: "reviewer", Kind.INTEGRATOR: "integrator"}[kind]


def has_saved_continuation(txn: Txn, agent_id: str) -> bool:
    """A parked interruption or a pending drained commit keeps an agent busy."""
    return bool(
        txn.scalar(
            "SELECT 1 FROM activations WHERE agent_id = ? AND state IN "
            "('planned', 'running', 'draining') LIMIT 1",
            (agent_id,),
        )
    )


def is_idle(txn: Txn, row: Row) -> bool:
    """Idle: a worker with no current task and nothing to resume (section 6.4)."""
    if row["kind"] != "worker" or row["state"] != "active":
        return False
    if row["current_task"] is not None:
        return False
    if has_saved_continuation(txn, row["id"]):
        return False
    if inbox.has_waking(txn, row["id"]):
        return False
    if controls.agent_blocked(txn, row["id"], row["backend"], "worker"):
        return False
    return True


def idle_workers(txn: Txn) -> list[Row]:
    rows = txn.all("SELECT * FROM agents WHERE kind = 'worker' AND state = 'active' "
                   "AND current_task IS NULL ORDER BY number")
    return [row for row in rows if is_idle(txn, row)]


def executed_titles(txn: Txn, agent_id: str, limit: int = 20) -> list[str]:
    rows = txn.all(
        "SELECT t.id, t.title FROM memberships m JOIN tasks t ON t.id = m.task_id "
        "WHERE m.agent_id = ? ORDER BY m.joined_at DESC LIMIT ?",
        (agent_id, limit),
    )
    return [f"{row['id']} ({row['title']})" for row in rows]


def has_nothing_left(txn: Txn, agent_id: str) -> bool:
    """The retirement condition of section 6.9."""
    row = agent_row(txn, agent_id)
    if row["current_task"] or has_saved_continuation(txn, agent_id):
        return False
    if txn.scalar(
        "SELECT 1 FROM memberships m JOIN tasks t ON t.id = m.task_id "
        "WHERE m.agent_id = ? AND m.state IN ('active', 'waiting') "
        "AND t.state NOT IN ('completed', 'retired') LIMIT 1",
        (agent_id,),
    ):
        return False
    if txn.scalar("SELECT 1 FROM inbox WHERE agent_id = ? AND state = 'pending' LIMIT 1",
                  (agent_id,)):
        return False
    if txn.scalar(
        "SELECT 1 FROM declarations WHERE feedback_from = ? AND state = 'awaiting_feedback'",
        (agent_id,),
    ):
        return False
    if txn.scalar(
        "SELECT 1 FROM tasks WHERE publisher = ? AND state NOT IN ('completed', 'retired') "
        "LIMIT 1",
        (agent_id,),
    ):
        return False
    return True


def retirement_candidates(ctx: HubContext, txn: Txn) -> list[str]:
    """Workers idle for ``teams.idle_retirement_hours`` with nothing of their own left."""
    cutoff = txn.moment - timedelta(hours=ctx.config.teams.idle_retirement_hours)
    result = []
    for row in txn.all("SELECT * FROM agents WHERE kind = 'worker' AND state = 'active'"):
        last = row["last_activation_at"] or row["created_at"]
        if parse_iso(last) <= cutoff and has_nothing_left(txn, row["id"]):
            result.append(row["id"])
    return result


def retire(txn: Txn, agent_id: str, reason: str) -> None:
    txn.execute(
        "UPDATE agents SET state = 'retired', retired_at = ?, current_task = NULL, "
        "current_review = NULL, intent = 'none' WHERE id = ?",
        (txn.now, agent_id),
    )
    inbox.drop_for_agent(txn, agent_id)
    txn.event("agent.retired", agent_id, reason=reason)


# ------------------------------------------------------------------- backend moves
def request_move(ctx: HubContext, txn: Txn, agent_id: str, backend: str) -> dict[str, object]:
    """``agent.move``: move a worker to another backend (section 6.1).

    The move happens at once when no turn is in flight, otherwise when the turn ends.
    Asking for the worker's current backend cancels a requested move.
    """
    row = agent_row(txn, agent_id)
    if row["kind"] != "worker":
        raise InvalidArgument("Only workers move between backends; reviewers and the "
                              "Integrator run on their role's backend.")
    if row["state"] == "retired":
        raise Conflict(f"{agent_id} is retired.")
    if backend == row["backend"]:
        if not row["move_to"]:
            raise Conflict(f"{agent_id} already runs on {backend}.")
        txn.execute("UPDATE agents SET move_to = NULL WHERE id = ?", (agent_id,))
        txn.event("agent.move_cancelled", agent_id, "operator")
        return {"agent": agent_id, "backend": backend, "move": "cancelled"}
    if not choices.worker_choice(ctx, txn, backend).complete:
        raise Conflict(f"Choose the model and effort of workers on {backend} first.",
                       details={"reason": "choices_required",
                                "missing": [f"workers.{backend}"]})
    txn.execute("UPDATE agents SET move_to = ? WHERE id = ?", (backend, agent_id))
    txn.event("agent.move_requested", agent_id, "operator", backend=backend)
    moved = settle_move(txn, agent_id)
    return {"agent": agent_id, "backend": backend,
            "move": "done" if moved else "when its turn in flight ends"}


def settle_move(txn: Txn, agent_id: str) -> bool:
    """Carry out a requested move once nothing is bound to the old conversation.

    A turn in flight, or a delivery whose outcome is unknown, keeps the worker where
    it is; the move is tried again on every admission pass.
    """
    row = agent_row(txn, agent_id)
    target = row["move_to"]
    if not target or row["state"] == "retired":
        return False
    if has_saved_continuation(txn, agent_id):
        return False
    if txn.scalar("SELECT 1 FROM inbox WHERE agent_id = ? AND state IN ('prepared', "
                  "'uncertain') LIMIT 1", (agent_id,)):
        return False
    previous = row["backend"]
    txn.execute(
        "UPDATE agents SET backend = ?, move_to = NULL, conversation_id = NULL, "
        "view_baseline = NULL, rotation_requested = 0 WHERE id = ?",
        (target, agent_id),
    )
    if row["current_task"]:
        inbox.notify(txn, agent_id,
                     f"The operator moved you from {previous} to {target}, so this "
                     "conversation is new: your handoff file and the task's state file "
                     "carry your work.", task_id=row["current_task"])
    txn.event("agent.moved", agent_id, previous=previous, backend=target)
    return True


def settle_moves(txn: Txn) -> int:
    rows = txn.all("SELECT id FROM agents WHERE move_to IS NOT NULL")
    return sum(settle_move(txn, row["id"]) for row in rows)


def set_intent(txn: Txn, agent_id: str, intent: str) -> None:
    txn.execute("UPDATE agents SET intent = ? WHERE id = ?", (intent, agent_id))


def record_backoff(txn: Txn, agent_id: str, signature: str) -> int:
    """Record a failure; identical consecutive failures back off 1, 2, 4… up to 60 min."""
    row = agent_row(txn, agent_id)
    failures = row["failures"] + 1 if row["failure_signature"] == signature else 1
    minutes = min(60, 2 ** (failures - 1))
    until = iso(txn.moment + timedelta(minutes=minutes))
    txn.execute(
        "UPDATE agents SET failures = ?, failure_signature = ?, backoff_until = ? WHERE id = ?",
        (failures, signature, until, agent_id),
    )
    return failures


def clear_backoff(txn: Txn, agent_id: str) -> None:
    txn.execute(
        "UPDATE agents SET failures = 0, failure_signature = NULL, backoff_until = NULL "
        "WHERE id = ?",
        (agent_id,),
    )


def in_backoff(txn: Txn, row: Row) -> bool:
    until = row["backoff_until"]
    return bool(until) and parse_iso(until) > txn.moment
