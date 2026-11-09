"""Task operations: publish, propose_revision, inspect, invitation_update."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError

from geyser.core import candidates, tasks, teams
from geyser.core.actors import Capability
from geyser.core.ledger import Txn, loads
from geyser.core.models import TaskContract
from geyser.errors import Forbidden, InvalidArgument, NotFound
from geyser.gateway.base import (
    READERS,
    TASK_TURN_AND_MESSAGE,
    Arguments,
    CallContext,
    Operation,
)
from geyser.ids import Kind, kind_of, require_id


class PublishArguments(TaskContract):
    """A new task's contract (section 5.1)."""


async def publish(call: CallContext, args: PublishArguments) -> dict[str, Any]:
    contract = TaskContract.model_validate(args.model_dump())
    parent = call.actor.task_id
    return call.commit(lambda txn: tasks.publish(call.ctx, txn, call.actor, contract,
                                                 parent_task=parent))


class ReviseArguments(Arguments):
    task_id: str
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=4_000)
    changes: dict[str, Any] = Field(default_factory=dict)
    """Contract fields to replace; omitted fields keep their current value."""


async def propose_revision(call: CallContext, args: ReviseArguments) -> dict[str, Any]:
    require_id(args.task_id, Kind.TASK, field="task_id")
    if not args.changes:
        raise InvalidArgument("`changes` names at least one contract field to replace.")

    def apply(txn: Txn) -> dict[str, Any]:
        if call.actor.capability is Capability.MESSAGE_ONLY:
            publisher = tasks.task_row(txn, args.task_id)["publisher"]
            if publisher != call.actor.id:
                raise Forbidden("In a message turn only the publisher may revise a task.")
        current = tasks.load_contract(call.ctx, txn, args.task_id).model_dump()
        unknown = set(args.changes) - set(current)
        if unknown:
            raise InvalidArgument(f"Unknown contract fields: {sorted(unknown)}.")
        current.update(args.changes)
        try:
            contract = TaskContract.model_validate(current)
        except ValidationError as error:
            raise InvalidArgument(f"The revised contract is invalid: {error}") from error
        return tasks.revise(call.ctx, txn, call.actor, args.task_id, args.expected_revision,
                            contract, args.reason)

    return call.commit(apply)


class InspectArguments(Arguments):
    id: str | None = None
    """Any ID: task, candidate, review, agent, declaration, request or message."""
    listing: Literal["tasks", "candidates", "agents", "requests"] | None = None
    state: str | None = None
    task_id: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


async def inspect(call: CallContext, args: InspectArguments) -> dict[str, Any]:
    if (args.id is None) == (args.listing is None):
        raise InvalidArgument("Give exactly one of `id` or `listing`.")
    return call.read(lambda txn: _inspect_id(call, txn, args.id) if args.id
                     else _inspect_list(txn, args))


def _inspect_id(call: CallContext, txn: Txn, object_id: str) -> dict[str, Any]:
    kind = kind_of(object_id)
    row: Any
    layout = call.ctx.layout
    if kind is Kind.TASK:
        row = tasks.task_row(txn, object_id)
        result = tasks.task_summary(txn, row)
        result["contract"] = layout.relative(layout.revision_file(object_id, row["revision"]))
        result["readable"] = layout.relative(layout.task_markdown(object_id))
        result["recruitment"] = row["recruitment"]
        original = tasks.load_contract(call.ctx, txn, object_id, 0)
        current = tasks.load_contract(call.ctx, txn, object_id)
        from geyser.core.models import required_condition_keys

        result["condition_keys"] = sorted(required_condition_keys(original, current))
        result["candidates"] = [
            {"candidate": c["id"], "state": c["state"], "kind": c["kind"]}
            for c in txn.all("SELECT id, state, kind FROM candidates WHERE task_id = ? "
                             "ORDER BY created_at", (object_id,))
        ]
        declaration = txn.one("SELECT id, state, declarer FROM declarations WHERE task_id = ? "
                              "ORDER BY created_at DESC LIMIT 1", (object_id,))
        if declaration:
            result["declaration"] = dict(declaration)
        places = txn.all("SELECT role, state, invitation FROM opportunities WHERE task_id = ?",
                         (object_id,))
        result["places"] = [dict(place) for place in places]
        return result
    if kind is Kind.CANDIDATE:
        row = candidates.candidate_row(txn, object_id)
        result = candidates.summary(row)
        state = row["state"]
        folder = (layout.archive(object_id) if state == "published" else
                  layout.history(object_id) if state in ("rejected", "withdrawn", "invalidated")
                  else layout.staging(object_id))
        result["files"] = layout.relative(folder) if folder.exists() else None
        validation = candidates.newest_validation(txn, object_id)
        if validation:
            result["validation"] = {"verdict": validation["verdict"],
                                    "phase": validation["phase"],
                                    "report": validation["report_path"],
                                    "summary": validation["summary"]}
        review = txn.one("SELECT id, state, reviewer, report_path FROM reviews "
                         "WHERE candidate_id = ?", (object_id,))
        if review:
            result["review"] = dict(review)
        return result
    if kind is Kind.REVIEW:
        row = candidates.review_row(txn, object_id)
        return {"review": row["id"], "candidate": row["candidate_id"], "state": row["state"],
                "reviewer": row["reviewer"], "report": row["report_path"],
                "integration_notes": loads(row["integration_notes"])}
    if kind in {Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR}:
        row = txn.one("SELECT id, kind, backend, state, current_task, current_review "
                      "FROM agents WHERE id = ?", (object_id,))
        if row is None:
            raise NotFound(f"{object_id} does not exist.")
        result = dict(row)
        result["workspace"] = layout.relative(layout.workspace(object_id))
        return result
    if kind is Kind.DECLARATION:
        row = txn.one("SELECT * FROM declarations WHERE id = ?", (object_id,))
        if row is None:
            raise NotFound(f"{object_id} does not exist.")
        return {"declaration": row["id"], "task": row["task_id"], "state": row["state"],
                "declarer": row["declarer"], "feedback_from": row["feedback_from"],
                "evidence": loads(row["evidence"]), "feedback": loads(row["feedback"])}
    if kind is Kind.REQUEST:
        row = txn.one("SELECT * FROM requests WHERE id = ?", (object_id,))
        if row is None:
            raise NotFound(f"{object_id} does not exist.")
        return {"request": row["id"], "kind": row["kind"], "title": row["title"],
                "state": row["state"], "question": row["body_path"],
                "answer": row["answer_path"]}
    if kind is Kind.MESSAGE:
        row = txn.one("SELECT id, sender, sender_role, recipient, body_path, created_at "
                      "FROM messages WHERE id = ?", (object_id,))
        if row is None:
            raise NotFound(f"{object_id} does not exist.")
        return dict(row)
    raise InvalidArgument(f"`{object_id}` is not an ID that `inspect` reads.")


def _inspect_list(txn: Txn, args: InspectArguments) -> dict[str, Any]:
    if args.listing == "tasks":
        query, parameters = "SELECT * FROM tasks", []
        if args.state:
            query += " WHERE state = ?"
            parameters.append(args.state)
        rows = txn.all(query + " ORDER BY number DESC LIMIT ?", (*parameters, args.limit))
        return {"tasks": [tasks.task_summary(txn, row) for row in rows]}
    if args.listing == "candidates":
        query, parameters = "SELECT * FROM candidates WHERE 1 = 1", []
        if args.task_id:
            query += " AND task_id = ?"
            parameters.append(require_id(args.task_id, Kind.TASK, field="task_id"))
        if args.state:
            query += " AND state = ?"
            parameters.append(args.state)
        rows = txn.all(query + " ORDER BY created_at DESC LIMIT ?", (*parameters, args.limit))
        return {"candidates": [candidates.summary(row) for row in rows]}
    if args.listing == "agents":
        rows = txn.all("SELECT id, kind, backend, state, current_task, current_review FROM "
                       "agents WHERE state != 'retired' ORDER BY kind, number LIMIT ?",
                       (args.limit,))
        return {"agents": [dict(row) for row in rows]}
    rows = txn.all("SELECT id, kind, title, state FROM requests ORDER BY created_at DESC "
                   "LIMIT ?", (args.limit,))
    return {"requests": [dict(row) for row in rows]}


class InvitationArguments(Arguments):
    task_id: str
    invitation: str | None = Field(default=None, max_length=8_000)
    """The new invitation text; null withdraws the collaborator place permanently."""


async def invitation_update(call: CallContext, args: InvitationArguments) -> dict[str, Any]:
    require_id(args.task_id, Kind.TASK, field="task_id")
    return call.commit(lambda txn: teams.update_invitation(call.ctx, txn, call.actor,
                                                           args.task_id, args.invitation))


class InviteBackArguments(Arguments):
    worker_id: str
    """A worker that left the task."""
    reason: str = Field(min_length=1, max_length=4_000)
    task_id: str | None = None
    """Defaults to your current task."""


async def invite_back(call: CallContext, args: InviteBackArguments) -> dict[str, Any]:
    task_id = args.task_id or call.actor.task_id
    if task_id is None:
        raise InvalidArgument("Name the task with `task_id`.")
    require_id(task_id, Kind.TASK, field="task_id")
    require_id(args.worker_id, Kind.WORKER, field="worker_id")
    return call.commit(lambda txn: teams.invite_back(call.ctx, txn, call.actor, task_id,
                                                     args.worker_id, args.reason))


OPERATIONS = [
    Operation("publish", "tasks", "Publish a self-contained task with acceptance conditions.",
              PublishArguments,
              frozenset({Capability.WORKER, Capability.MESSAGE_ONLY, Capability.REVIEWER}),
              publish),
    Operation("propose_revision", "tasks",
              "Revise a task you maintain; name the revision you replace and a reason.",
              ReviseArguments, TASK_TURN_AND_MESSAGE, propose_revision),
    Operation("inspect", "tasks", "Read one object by ID, or list tasks, candidates, agents, "
              "requests.", InspectArguments, READERS, inspect, mutating=False),
    Operation("invitation_update", "tasks",
              "Edit (or withdraw with null) your task's collaborator invitation.",
              InvitationArguments, frozenset({Capability.WORKER}), invitation_update),
    Operation("invite_back", "tasks",
              "Bring back a worker that left your task, with a reason.",
              InviteBackArguments, TASK_TURN_AND_MESSAGE, invite_back),
]
