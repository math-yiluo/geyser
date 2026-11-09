"""Completion: declaration, one feedback opportunity, finalization (DESIGN.md section 5.7)."""

from __future__ import annotations

from typing import Any

from geyser.core import candidates, controls, inbox, tasks, teams
from geyser.core.actors import Actor
from geyser.core.context import HubContext
from geyser.core.ledger import Txn, dumps, loads
from geyser.core.models import (
    EvidenceEntry,
    Objection,
    ObjectionResolution,
    RequirementTerm,
    required_condition_keys,
)
from geyser.core.waits import requirement_met
from geyser.errors import Conflict, Forbidden, InvalidArgument, NotFound
from geyser.ids import Kind, require_id

PENDING = ("awaiting_feedback", "deciding")


def declaration_row(txn: Txn, declaration_id: str):
    require_id(declaration_id, Kind.DECLARATION, field="declaration_id")
    row = txn.one("SELECT * FROM declarations WHERE id = ?", (declaration_id,))
    if row is None:
        raise NotFound(f"{declaration_id} does not exist.")
    return row


def _check_evidence(ctx: HubContext, txn: Txn, task_id: str,
                    evidence: dict[str, EvidenceEntry]) -> None:
    """Every condition key has evidence; every artifact is accepted and published."""
    original = tasks.load_contract(ctx, txn, task_id, 0)
    current = tasks.load_contract(ctx, txn, task_id)
    required = required_condition_keys(original, current)
    missing = sorted(set(required) - set(evidence))
    unknown = sorted(set(evidence) - set(required))
    if missing or unknown:
        raise InvalidArgument(
            "Completion needs evidence for every condition key, original and current.",
            details={"missing": missing, "unknown": unknown, "required": required},
        )
    for key, entry in evidence.items():
        for artifact in entry.artifacts:
            row = txn.one("SELECT state, policy FROM candidates WHERE id = ?", (artifact,))
            if row is None:
                raise InvalidArgument(f"Evidence for `{key}` names {artifact}, which does not "
                                      "exist.")
            publishes = candidates.policy_publishes(ctx, row["policy"])
            if (publishes and row["state"] != "published") or (
                not publishes and row["state"] != "accepted"
            ):
                raise InvalidArgument(
                    f"Evidence for `{key}` names {artifact}, which is {row['state']}. Only "
                    "accepted, published artifacts are evidence; submitted or pending work "
                    "is not."
                )
    for requirement in current.requirements:
        term = RequirementTerm(provider=requirement.provider, revision=requirement.revision)
        if not requirement_met(txn, term):
            raise InvalidArgument(
                f"`requirement:{requirement.key}` is not met: {requirement.provider} is not "
                "yet published or completed."
            )
    blocking = candidates.blocking_contributions(ctx, txn, task_id)
    if blocking:
        raise Conflict(
            f"{task_id} has contributions without acceptance and publication. Correct them "
            "or withdraw them with a reason before completing.",
            details={"blocking": blocking},
        )


def declare(ctx: HubContext, txn: Txn, actor: Actor, task_id: str,
            evidence: dict[str, EvidenceEntry]) -> dict[str, Any]:
    """``complete``: declare completion with evidence for every condition key."""
    task = tasks.task_row(txn, task_id)
    if task["state"] in tasks.TERMINAL_STATES:
        raise Conflict(f"{task_id} is already {task['state']}.")
    members = tasks.member_ids(txn, task_id, states=("active",))
    if actor.id not in members:
        raise Forbidden(f"Only an active executor of {task_id} declares its completion.")
    if txn.scalar(f"SELECT 1 FROM declarations WHERE task_id = ? AND state IN "
                  f"({','.join('?' * len(PENDING))})", (task_id, *PENDING)):
        raise Conflict(f"{task_id} already has a pending completion declaration.")
    _check_evidence(ctx, txn, task_id, evidence)
    declaration_id = txn.next_id(Kind.DECLARATION)
    others = [agent for agent in members if agent != actor.id]
    feedback_from = others[0] if others else None
    state = "awaiting_feedback" if feedback_from else "deciding"
    txn.execute(
        "INSERT INTO declarations (id, task_id, revision, declarer, evidence, state, "
        "feedback_from, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (declaration_id, task_id, task["revision"], actor.id,
         dumps({key: entry.model_dump() for key, entry in evidence.items()}), state,
         feedback_from, txn.now),
    )
    txn.event("completion.declared", declaration_id, actor.id, task=task_id)
    if feedback_from is None:
        _finalize(ctx, txn, declaration_id)
        return {"declaration": declaration_id, "task": tasks.title_of(txn, task_id),
                "state": "completed"}
    inbox.notify(
        txn, feedback_from,
        f"{actor.id} declared {task_id} complete ({declaration_id}). You have one feedback "
        "opportunity: answer with `completion_feedback`, naming any concrete unmet condition "
        "after checkpointing your work.",
        ref=declaration_id, task_id=task_id, wakes=True,
    )
    _wait_for_feedback(ctx, txn, actor.id, task_id, declaration_id)
    tasks.refresh_state(ctx, txn, task_id)
    txn.after_commit(ctx.wake)
    return {"declaration": declaration_id, "task": tasks.title_of(txn, task_id),
            "state": state, "feedback_from": feedback_from}


def _wait_for_feedback(ctx: HubContext, txn: Txn, agent_id: str, task_id: str,
                       declaration_id: str) -> None:
    checkpoint = {"summary": f"Declared completion in {declaration_id}.",
                  "next_action": "Answer the feedback and decide with completion_finalize."}
    predicate: dict[str, Any] = {"requirements": [], "validations": [], "external_response_id": None,
                 "completion_declaration_id": declaration_id, "mode": "all"}
    txn.execute(
        "UPDATE memberships SET state = 'waiting', checkpoint = ?, predicate = ?, "
        "wait_reason = 'completion feedback', waiting_since = ? "
        "WHERE task_id = ? AND agent_id = ?",
        (dumps(checkpoint), dumps(predicate), txn.now, task_id, agent_id),
    )
    txn.execute("UPDATE agents SET intent = 'none' WHERE id = ?", (agent_id,))


def give_feedback(ctx: HubContext, txn: Txn, actor: Actor, declaration_id: str,
                  objections: list[Objection]) -> dict[str, Any]:
    """``completion_feedback``: the other executor's single feedback opportunity."""
    row = declaration_row(txn, declaration_id)
    if row["feedback_from"] != actor.id:
        raise Forbidden(f"{declaration_id} does not await feedback from {actor.id}.")
    if row["state"] != "awaiting_feedback":
        raise Conflict(f"{declaration_id} is {row['state']}.")
    txn.execute(
        "UPDATE declarations SET state = 'deciding', feedback = ? WHERE id = ?",
        (dumps([objection.model_dump() for objection in objections]), declaration_id),
    )
    txn.event("completion.feedback", declaration_id, actor.id, objections=len(objections))
    if not objections:
        _finalize(ctx, txn, declaration_id)
        return {"declaration": declaration_id, "state": "completed"}
    inbox.notify(
        txn, row["declarer"],
        f"{actor.id} objected to {declaration_id}: "
        + "; ".join(f"{o.key}: {o.explanation}" for o in objections)
        + ". Answer every objection with evidence and decide with `completion_finalize`.",
        ref=declaration_id, task_id=row["task_id"],
    )
    txn.after_commit(ctx.wake)
    return {"declaration": declaration_id, "state": "deciding"}


def finalize(ctx: HubContext, txn: Txn, actor: Actor, declaration_id: str, decision: str,
             resolutions: list[ObjectionResolution]) -> dict[str, Any]:
    """``completion_finalize``: the declarer answers every objection and decides."""
    row = declaration_row(txn, declaration_id)
    if row["declarer"] != actor.id:
        raise Forbidden(f"Only {row['declarer']} decides {declaration_id}.")
    if row["state"] != "deciding":
        raise Conflict(f"{declaration_id} is {row['state']}.")
    objections = {o["key"] for o in loads(row["feedback"], [])}
    answered = {resolution.key for resolution in resolutions}
    if decision == "complete" and objections - answered:
        raise InvalidArgument("Answer every objection before completing.",
                              details={"unanswered": sorted(objections - answered)})
    for resolution in resolutions:
        for artifact in resolution.artifacts:
            state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (artifact,))
            if state != "published":
                raise InvalidArgument(f"{artifact} is {state}; it cannot answer an objection.")
    txn.execute("UPDATE declarations SET resolutions = ? WHERE id = ?",
                (dumps([r.model_dump() for r in resolutions]), declaration_id))
    if decision == "continue":
        txn.execute("UPDATE declarations SET state = 'continued', decided_at = ? WHERE id = ?",
                    (txn.now, declaration_id))
        txn.event("completion.continued", declaration_id, actor.id)
        _resume_declarer(ctx, txn, row)
        return {"declaration": declaration_id, "state": "continued"}
    if decision != "complete":
        raise InvalidArgument("`decision` is `complete` or `continue`.")
    _finalize(ctx, txn, declaration_id)
    return {"declaration": declaration_id, "state": "completed"}


def _resume_declarer(ctx: HubContext, txn: Txn, row) -> None:
    """The declarer resumes its task: its next turn continues the work."""
    txn.execute(
        "UPDATE memberships SET state = 'active', predicate = NULL WHERE task_id = ? "
        "AND agent_id = ? AND state = 'waiting'",
        (row["task_id"], row["declarer"]),
    )
    txn.execute("UPDATE agents SET intent = 'resume' WHERE id = ? AND current_task = ?",
                (row["declarer"], row["task_id"]))
    tasks.refresh_state(ctx, txn, row["task_id"])
    txn.after_commit(ctx.wake)


def lapse_feedback(ctx: HubContext, txn: Txn, agent_id: str) -> None:
    """An executor that stops working (pauses or leaves) loses its feedback opportunity.

    The declaration proceeds as if no objection was raised; if a gate fails now, the
    declarer resumes to decide.
    """
    from geyser.errors import GeyserError

    for row in txn.all("SELECT * FROM declarations WHERE feedback_from = ? AND state = "
                       "'awaiting_feedback'", (agent_id,)):
        txn.execute("UPDATE declarations SET state = 'deciding', feedback = '[]' WHERE id = ?",
                    (row["id"],))
        txn.event("completion.feedback_lapsed", row["id"], agent_id)
        try:
            _finalize(ctx, txn, row["id"])
        except GeyserError as error:
            _resume_declarer(ctx, txn, row)
            inbox.notify(txn, row["declarer"],
                         f"{agent_id} stopped working before giving feedback on {row['id']}, "
                         f"and a completion gate failed: {error.message}. Decide with "
                         "`completion_finalize`.", ref=row["id"], task_id=row["task_id"])


def _finalize(ctx: HubContext, txn: Txn, declaration_id: str) -> None:
    """Recheck every gate, complete the task and free its executors."""
    row = declaration_row(txn, declaration_id)
    evidence = {key: EvidenceEntry.model_validate(value)
                for key, value in loads(row["evidence"], {}).items()}
    _check_evidence(ctx, txn, row["task_id"], evidence)
    txn.execute("UPDATE declarations SET state = 'finalized', decided_at = ? WHERE id = ?",
                (txn.now, declaration_id))
    task_id = row["task_id"]
    teams.cancel_collaborator_places(txn, task_id, "task completed")
    members = teams.end_memberships(txn, task_id)
    tasks.mark_completed(ctx, txn, task_id, row["revision"])
    inbox.notify_many(txn, [m for m in members if m != row["declarer"]],
                      f"{tasks.title_of(txn, task_id)} is completed.", task_id=task_id)
    txn.event("completion.finalized", declaration_id, task=task_id)
    if task_id == "task-0":
        controls.set_run(txn, controls.COMPLETED)
        txn.event("project.completed", "task-0")


def cancel(ctx: HubContext, txn: Txn, declaration_id: str, reason: str) -> dict[str, Any]:
    """``task.complete.cancel``: the operator cancels an unrecoverable declaration."""
    row = declaration_row(txn, declaration_id)
    if row["state"] not in PENDING:
        raise Conflict(f"{declaration_id} is {row['state']}.")
    txn.execute("UPDATE declarations SET state = 'cancelled', decided_at = ? WHERE id = ?",
                (txn.now, declaration_id))
    txn.event("completion.cancelled", declaration_id, "operator", reason=reason)
    _resume_declarer(ctx, txn, row)
    return {"declaration": declaration_id, "state": "cancelled"}


def invalidate_evidence(ctx: HubContext, txn: Txn, task_id: str, reason: str,
                        actor: Actor) -> str | None:
    """Lost evidence: mark it invalid and create exactly one linked repair task."""
    task = tasks.task_row(txn, task_id)
    if task["state"] != "completed":
        return None
    txn.execute("UPDATE tasks SET evidence_valid = 0 WHERE id = ?", (task_id,))
    existing = txn.scalar(
        "SELECT id FROM tasks WHERE repair_of = ? AND state NOT IN ('completed', 'retired')",
        (task_id,),
    )
    if existing:
        return existing
    from geyser.core.models import TaskContract

    contract = TaskContract(
        title=f"Repair the evidence of {task_id}",
        objective=(f"The completion evidence of {task_id} ({task['title']}) became invalid: "
                   f"{reason}. Restore valid, published evidence for its conditions."),
        acceptance=[f"Every condition of {task_id} again has accepted, published evidence."],
        inputs=[],
        priority=max(task["priority"], 2),
    )
    reply = tasks.publish(ctx, txn, actor, contract, repair_of=task_id)
    txn.event("task.evidence_invalid", task_id, reason=reason, repair=reply["task_id"])
    return reply["task_id"]
