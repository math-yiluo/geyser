"""Team places, claims, recruitment and leaving (DESIGN.md sections 6.2 to 6.5)."""

from __future__ import annotations

from sqlite3 import Row
from typing import Any

from geyser.core import agents, inbox, tasks
from geyser.core.actors import Actor
from geyser.core.context import HubContext
from geyser.core.ledger import Txn
from geyser.errors import Conflict, Forbidden, InvalidArgument

_CLAIMABLE_STATES = ("open", "active", "waiting")


def default_invitation(task_id: str, executor: str) -> str:
    return (
        f"Join {task_id} as its collaborator. Read the contract and {executor}'s handoff "
        f"file, agree with {executor} through talk on a separable part of the work, and "
        "contribute that part as reviewed candidates."
    )


def open_opportunities(txn: Txn) -> list[Row]:
    """Open places on claimable tasks: higher priority first, then older first."""
    return txn.all(
        "SELECT o.*, t.priority, t.title, t.revision AS task_revision FROM opportunities o "
        "JOIN tasks t ON t.id = o.task_id "
        "WHERE o.state = 'open' AND t.state IN ('open', 'active', 'waiting') "
        "ORDER BY t.priority DESC, o.created_at, o.id"
    )


def opportunity(txn: Txn, opportunity_id: int) -> Row | None:
    return txn.one("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,))


def can_fill(txn: Txn, opportunity_id: int, agent_id: str) -> str | None:
    """Return why ``agent_id`` cannot take the place, or None if it can."""
    place = opportunity(txn, opportunity_id)
    if place is None or place["state"] != "open":
        return "the place is no longer open"
    task_state = txn.scalar("SELECT state FROM tasks WHERE id = ?", (place["task_id"],))
    if task_state not in _CLAIMABLE_STATES:
        return f"the task is {task_state}"
    agent = agents.agent_row(txn, agent_id)
    if agent["kind"] != "worker" or agent["state"] != "active":
        return "only an active worker can take a task place"
    if agent["current_task"] is not None:
        return f"{agent_id} already has a current task"
    previous = txn.scalar(
        "SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?",
        (place["task_id"], agent_id),
    )
    if previous is not None:
        return f"{agent_id} was already a member of {place['task_id']}"
    return None


def fill(ctx: HubContext, txn: Txn, opportunity_id: int, agent_id: str, *, by: str
         ) -> dict[str, Any]:
    """Fill a place with a worker. The claim rechecks eligibility atomically."""
    problem = can_fill(txn, opportunity_id, agent_id)
    if problem:
        raise Conflict(f"Cannot assign {agent_id}: {problem}.")
    place = opportunity(txn, opportunity_id)
    assert place is not None
    task_id = place["task_id"]
    txn.execute(
        "UPDATE opportunities SET state = 'filled', filled_by = ?, closed_at = ? WHERE id = ?",
        (agent_id, txn.now, opportunity_id),
    )
    txn.execute(
        "INSERT INTO memberships (task_id, agent_id, role, state, plan, joined_at) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (task_id, agent_id, place["role"], place["invitation"], txn.now),
    )
    txn.execute(
        "UPDATE agents SET current_task = ?, intent = 'start' WHERE id = ?", (task_id, agent_id)
    )
    tasks.refresh_state(ctx, txn, task_id)
    txn.event("task.joined", task_id, agent_id, role=place["role"], by=by)
    txn.after_commit(ctx.wake)
    return {"task": tasks.title_of(txn, task_id), "agent": agent_id, "role": place["role"]}


def operator_claim(ctx: HubContext, txn: Txn, task_id: str, agent_id: str) -> dict[str, Any]:
    """``task.claim`` / ``task.join``: the operator assigns a worker to an open place."""
    tasks.task_row(txn, task_id)
    place = txn.one(
        "SELECT id FROM opportunities WHERE task_id = ? AND state = 'open' ORDER BY id LIMIT 1",
        (task_id,),
    )
    if place is None:
        raise Conflict(f"{task_id} has no open place.")
    return fill(ctx, txn, place["id"], agent_id, by="operator")


# ---------------------------------------------------------------------- recruitment
def record_recruitment(ctx: HubContext, txn: Txn, task_id: str, needed: bool) -> None:
    """Apply the one-time recruitment decision (section 6.3)."""
    row = tasks.task_row(txn, task_id)
    if row["recruitment"] != "undecided":
        return
    if not needed or ctx.config.teams.collaborators < 1 or row["state"] in tasks.TERMINAL_STATES:
        txn.execute("UPDATE tasks SET recruitment = 'no' WHERE id = ?", (task_id,))
        txn.event("task.recruitment", task_id, decision="no")
        return
    executor = txn.scalar(
        "SELECT agent_id FROM memberships WHERE task_id = ? AND role = 'executor' "
        "ORDER BY joined_at LIMIT 1",
        (task_id,),
    )
    invitation = default_invitation(task_id, executor or "the executor")
    for _ in range(ctx.config.teams.collaborators):
        txn.execute(
            "INSERT INTO opportunities (task_id, role, state, invitation, created_at) "
            "VALUES (?, 'collaborator', 'open', ?, ?)",
            (task_id, invitation, txn.now),
        )
    txn.execute("UPDATE tasks SET recruitment = 'yes' WHERE id = ?", (task_id,))
    txn.event("task.recruitment", task_id, decision="yes")
    if executor:
        inbox.notify(
            txn, executor,
            f"{task_id} will recruit a collaborator. Edit its invitation with "
            "`invitation_update` if the standard one does not fit.",
            task_id=task_id,
        )
    txn.after_commit(ctx.wake)


def update_invitation(ctx: HubContext, txn: Txn, actor: Actor, task_id: str,
                      invitation: str | None) -> dict[str, Any]:
    """Edit or withdraw (``None``) the collaborator invitation of one's task."""
    if actor.id not in tasks.member_ids(txn, task_id) and not actor.is_operator:
        raise Forbidden(f"Only an executor of {task_id} may edit its invitation.")
    place = txn.one(
        "SELECT id FROM opportunities WHERE task_id = ? AND role = 'collaborator' "
        "AND state = 'open'",
        (task_id,),
    )
    if place is None:
        raise Conflict(f"{task_id} has no open collaborator place.")
    if invitation is None:
        cancel_collaborator_places(txn, task_id, "invitation withdrawn")
        return {"task": tasks.title_of(txn, task_id), "invitation": "withdrawn"}
    if not invitation.strip() or len(invitation) > 8_000:
        raise InvalidArgument("An invitation is 1 to 8,000 characters.")
    txn.execute("UPDATE opportunities SET invitation = ? WHERE id = ?", (invitation, place["id"]))
    txn.event("task.invitation", task_id, actor.id)
    return {"task": tasks.title_of(txn, task_id), "invitation": "updated"}


def cancel_collaborator_places(txn: Txn, task_id: str, reason: str) -> None:
    """Cancel open collaborator places permanently; they never reopen."""
    cursor = txn.execute(
        "UPDATE opportunities SET state = 'cancelled', closed_at = ? "
        "WHERE task_id = ? AND role = 'collaborator' AND state = 'open'",
        (txn.now, task_id),
    )
    if cursor.rowcount:
        txn.event("task.recruitment_cancelled", task_id, reason=reason)


# ------------------------------------------------------------------------- leaving
def leave(ctx: HubContext, txn: Txn, actor: Actor, task_id: str, reason: str) -> dict[str, Any]:
    """Abandon a task membership (section 12); only an invitation back undoes it.

    Failure is not departure.
    """
    membership = txn.one(
        "SELECT * FROM memberships WHERE task_id = ? AND agent_id = ? AND state IN "
        "('active', 'waiting')",
        (task_id, actor.id),
    )
    if membership is None:
        raise Conflict(f"{actor.id} is not a member of {task_id}.")
    if not reason.strip():
        raise InvalidArgument("Leaving needs a reason.")
    txn.execute(
        "UPDATE memberships SET state = 'left', ended_at = ?, predicate = NULL "
        "WHERE task_id = ? AND agent_id = ?",
        (txn.now, task_id, actor.id),
    )
    txn.execute(
        "UPDATE agents SET current_task = NULL, intent = 'none' WHERE id = ? AND current_task = ?",
        (actor.id, task_id),
    )
    from geyser.core import completion

    completion.lapse_feedback(ctx, txn, actor.id)
    others = _other_members(txn, task_id, actor.id)
    if not others:
        cancel_collaborator_places(txn, task_id, "every executor left")
    inbox.notify_many(txn, others, f"{actor.id} left {task_id}: {reason}", task_id=task_id,
                      wakes=True)
    txn.event("task.left", task_id, actor.id, reason=reason)
    tasks.refresh_state(ctx, txn, task_id)
    txn.after_commit(ctx.wake)
    return {"task": tasks.title_of(txn, task_id), "left": True}


def invite_back(ctx: HubContext, txn: Txn, actor: Actor, task_id: str, worker_id: str,
                reason: str) -> dict[str, Any]:
    """Bring back a worker that left the task (section 6.2).

    Only a current executor of the task, or the operator, invites; only a former
    executor returns, so the task's lifetime count of executors is unchanged. The
    worker becomes a member again like a paused member whose wait just ended.
    """
    task = tasks.task_row(txn, task_id)
    if task["state"] in tasks.TERMINAL_STATES:
        raise Conflict(f"{tasks.title_of(txn, task_id)} is {task['state']}; it takes no one "
                       "back.")
    if not actor.is_operator and actor.id not in tasks.member_ids(txn, task_id):
        raise Forbidden(f"Only a current executor of {task_id}, or the operator, invites a "
                        "worker back.")
    if not reason.strip():
        raise InvalidArgument("An invitation back needs a reason.")
    membership = txn.one("SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?",
                         (task_id, worker_id))
    if membership is None:
        raise Forbidden(f"{worker_id} never executed {task_id}; only a former executor "
                        "returns.")
    if membership["state"] in ("active", "waiting"):
        raise Conflict(f"{worker_id} is still a member of {task_id}.")
    if membership["state"] == "ended":
        raise Conflict(f"{worker_id}'s membership of {task_id} ended with its completion.")
    worker = agents.agent_row(txn, worker_id)
    if worker["state"] == "retired":
        raise Conflict(f"{worker_id} is retired.")
    if (worker["current_task"] is not None or agents.has_saved_continuation(txn, worker_id)
            or worker["intent"] != "none"):
        raise Conflict(f"{worker_id} is busy (in a turn, with work in flight, or on another "
                       "task). Message it with talk_send instead.",
                       details={"reason": "worker_busy"})
    by = "the operator" if actor.is_operator else actor.id
    txn.execute(
        "UPDATE memberships SET state = 'active', ended_at = NULL, predicate = NULL, "
        "waiting_since = NULL, wait_reason = ? WHERE task_id = ? AND agent_id = ?",
        (f"invited back by {by}: {reason}", task_id, worker_id),
    )
    txn.execute("UPDATE agents SET current_task = ?, intent = 'resume' WHERE id = ?",
                (task_id, worker_id))
    inbox.notify(txn, worker_id,
                 f"{by} invited you back to {tasks.title_of(txn, task_id)}: {reason}. Continue "
                 "from your last checkpoint and files.", task_id=task_id)
    others = _other_members(txn, task_id, worker_id)
    inbox.notify_many(txn, [member for member in others if member != actor.id],
                      f"{by} invited {worker_id} back to {task_id}: {reason}", task_id=task_id)
    txn.event("task.invited_back", task_id, actor.id, worker=worker_id, reason=reason)
    tasks.refresh_state(ctx, txn, task_id)
    txn.after_commit(ctx.wake)
    return {"task": tasks.title_of(txn, task_id), "invited_back": worker_id,
            "resumes": "at its next turn"}


def _other_members(txn: Txn, task_id: str, agent_id: str) -> list[str]:
    return [member for member in tasks.member_ids(txn, task_id) if member != agent_id]


def end_memberships(txn: Txn, task_id: str) -> list[str]:
    """End every membership of a completed task; its executors become idle."""
    members = tasks.member_ids(txn, task_id)
    txn.execute(
        "UPDATE memberships SET state = 'ended', ended_at = ?, predicate = NULL "
        "WHERE task_id = ? AND state IN ('active', 'waiting')",
        (txn.now, task_id),
    )
    for agent_id in members:
        txn.execute(
            "UPDATE agents SET current_task = NULL, intent = 'none' "
            "WHERE id = ? AND current_task = ?",
            (agent_id, task_id),
        )
    return members
