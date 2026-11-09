"""Operator actions shared by the HTTP API, the CLI and tests (DESIGN.md section 15).

Only the operator controls the run. Starting the service never starts model work;
live execution starts only with an explicit ``run.start``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geyser.core import (
    agents,
    candidates,
    choices,
    completion,
    controls,
    messages,
    relations,
    tasks,
    teams,
)
from geyser.core.actors import OPERATOR
from geyser.core.ledger import loads
from geyser.core.models import TaskContract
from geyser.errors import Conflict, InvalidArgument
from geyser.ids import Kind, require_id

if TYPE_CHECKING:
    from geyser.hub import Hub
    from geyser.service import GeyserService


def setup_project(hub: Hub, *, title: str, goal: str, acceptance: list[str],
                  constraints: list[str] | None = None, context: str = "",
                  backend: str | None = None) -> dict[str, Any]:
    """Create Task 0 from the operator's goal, preserved verbatim as revision 0."""
    with hub.transaction() as txn:
        if txn.scalar("SELECT 1 FROM tasks WHERE id = 'task-0'"):
            raise Conflict("Task 0 already exists.")
        if backend:
            txn.set_meta("new_backend", backend)
        contract = TaskContract(title=title, objective=goal, acceptance=acceptance,
                                constraints=constraints or [], context=context, priority=3,
                                kind="planning" if "planning" in hub.pack.task_kinds()
                                else "work")
        return tasks.publish(hub.ctx, txn, OPERATOR, contract)


def publish_task(hub: Hub, contract: TaskContract) -> dict[str, Any]:
    with hub.transaction() as txn:
        return tasks.publish(hub.ctx, txn, OPERATOR, contract)


# ------------------------------------------------------------------- run control
def run_action(hub: Hub, action: str, service: GeyserService | None = None) -> dict[str, Any]:
    """``run.start``, ``run.pause``, ``run.pause_after_turn``, ``run.resume``, ``run.stop``.

    Starting or resuming is refused while any role's model or the answer deadline is
    still to be chosen (section 15.1).
    """
    with hub.transaction() as txn:
        if action in ("start", "resume"):
            choices.require(hub.ctx, txn)
            released = controls.clear_holds(txn)
            controls.set_run(txn, controls.RUNNING)
            result = {"run": controls.RUNNING, "holds_cleared": released}
        elif action == "pause":
            controls.set_run(txn, controls.PAUSED)
            result = {"run": controls.PAUSED}
        elif action == "pause_after_turn":
            controls.set_run(txn, controls.PAUSE_AFTER_TURN)
            result = {"run": controls.PAUSE_AFTER_TURN}
        elif action == "stop":
            controls.set_run(txn, controls.STOPPED)
            result = {"run": controls.STOPPED}
        else:
            raise InvalidArgument(f"Unknown run action {action!r}.")
        txn.after_commit(hub.ctx.wake)
    if service is not None and action in ("pause", "stop"):
        service.interrupt_for_pause()
    return result


def agent_action(hub: Hub, agent_id: str, action: str,
                 service: GeyserService | None = None) -> dict[str, Any]:
    """``agent.pause``, ``agent.pause_after_turn``, ``agent.resume``."""
    require_id(agent_id, Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR, field="agent_id")
    states = {"pause": controls.PAUSED, "pause_after_turn": controls.PAUSE_AFTER_TURN,
              "resume": controls.RUNNING}
    if action not in states:
        raise InvalidArgument(f"Unknown agent action {action!r}.")
    with hub.transaction() as txn:
        agents.agent_row(txn, agent_id)
        if action == "resume":
            choices.require(hub.ctx, txn)
        controls.set_narrow(txn, f"agent:{agent_id}", states[action])
        txn.after_commit(hub.ctx.wake)
    if service is not None and action == "pause":
        service.interrupt_for_pause(agent_id)
    return {"agent": agent_id, "control": states[action]}


def group_action(hub: Hub, group: str, action: str) -> dict[str, Any]:
    """``group.pause_after_turn`` and ``group.resume`` on ``<backend>[.workers|.reviewers]``
    or ``integrator`` (section 15.1)."""
    if action not in ("pause_after_turn", "resume"):
        raise InvalidArgument("Groups take `pause_after_turn` or `resume`.")
    backend, _, role = group.partition(".")
    if group != controls.INTEGRATOR_GROUP and (not backend or role not in
                                                ("", "workers", "reviewers")):
        raise InvalidArgument("A group is `<backend>`, `<backend>.workers`, "
                              "`<backend>.reviewers` or `integrator`.")
    with hub.transaction() as txn:
        if action == "resume":
            choices.require(hub.ctx, txn)
        controls.set_narrow(txn, f"group:{group}",
                            controls.PAUSE_AFTER_TURN if action == "pause_after_turn"
                            else controls.RUNNING)
        txn.after_commit(hub.ctx.wake)
    return {"group": group, "control": action}


def set_new_backend(hub: Hub, backend: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        txn.set_meta("new_backend", backend)
    return {"new_backend": backend}


def move_agent(hub: Hub, agent_id: str, backend: str) -> dict[str, Any]:
    """``agent.move``: move a worker to another backend (section 6.1)."""
    require_id(agent_id, Kind.WORKER, field="agent_id")
    with hub.transaction() as txn:
        result = agents.request_move(hub.ctx, txn, agent_id, backend)
        txn.after_commit(hub.ctx.wake)
    return result


def choose(hub: Hub, role: str, *, backend: str | None = None, model: str | None = None,
           effort: str | None = None, minutes: float | None = None) -> dict[str, Any]:
    """``geyser choose``: a role's backend, model and effort, or the answer deadline."""
    with hub.transaction() as txn:
        result = choices.choose(hub.ctx, txn, role, backend=backend, model=model,
                                effort=effort, minutes=minutes)
        result["missing"] = choices.missing(hub.ctx, txn)
        txn.after_commit(hub.ctx.wake)
    return result


def choice_overview(hub: Hub) -> dict[str, Any]:
    with hub.read() as txn:
        return choices.overview(hub.ctx, txn)


# ------------------------------------------------------------------ messages
def send_message(hub: Hub, recipient: str, text: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        return messages.send_operator_message(hub.ctx, txn, recipient, text)


def withdraw_message(hub: Hub, message_id: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        return messages.withdraw_operator_message(txn, message_id)


def answer_request(hub: Hub, request_id: str, text: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        return messages.answer_request(hub.ctx, txn, request_id, text)


# --------------------------------------------------------------- task actions
def claim(hub: Hub, task_id: str, agent_id: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        return teams.operator_claim(hub.ctx, txn, task_id, agent_id)


def invite_back(hub: Hub, task_id: str, worker_id: str, reason: str) -> dict[str, Any]:
    """``task.invite_back``: bring back a worker that left the task (section 6.2)."""
    require_id(worker_id, Kind.WORKER, field="worker_id")
    with hub.transaction() as txn:
        return teams.invite_back(hub.ctx, txn, OPERATOR, task_id, worker_id, reason)


def cancel_declaration(hub: Hub, declaration_id: str, reason: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        return completion.cancel(hub.ctx, txn, declaration_id, reason)


def retry_validation(hub: Hub, candidate_id: str, reason: str) -> dict[str, Any]:
    with hub.transaction() as txn:
        return candidates.retry_validation(hub.ctx, txn, candidate_id, reason)


# ---------------------------------------------------------------------- status
def status(hub: Hub) -> dict[str, Any]:
    """What the project is doing, for the dashboard status bar and the CLI."""
    with hub.read() as txn:
        run = controls.run_state(txn)
        holds = [dict(row) for row in txn.all("SELECT account, reason, at FROM holds")]
        live = txn.scalar("SELECT COUNT(*) FROM activations WHERE state IN "
                          "('planned', 'running', 'draining')")
        counts = {row["state"]: row["n"] for row in txn.all(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
        open_requests = txn.scalar("SELECT COUNT(*) FROM requests WHERE state = 'open'")
        notices = [dict(row) for row in txn.all(
            "SELECT subject, message, count, last_at FROM operator_notices ORDER BY last_at "
            "DESC LIMIT 20")]
        task0 = txn.one("SELECT state, title FROM tasks WHERE id = 'task-0'")
        missing = choices.missing(hub.ctx, txn)
    if holds:
        summary = "account hold"
    elif run == controls.RUNNING:
        summary = "running" if live else "idle"
    else:
        summary = run
    return {"project": hub.config.name, "run": run, "summary": summary,
            "live_activations": live, "tasks": counts,
            "holds": holds, "open_requests": open_requests, "notices": notices,
            "task0": dict(task0) if task0 else None, "missing_choices": missing}


def agent_list(hub: Hub) -> list[dict[str, Any]]:
    with hub.read() as txn:
        rows = txn.all("SELECT * FROM agents ORDER BY kind, number")
        result = []
        for row in rows:
            entry = {key: row[key] for key in ("id", "kind", "backend", "move_to", "state",
                                               "current_task", "current_review",
                                               "current_batch", "intent", "failures",
                                               "backoff_until")}
            entry["control"] = controls.resolve(txn, row["id"], row["backend"],
                                                row["kind"]).state
            entry["live"] = bool(txn.scalar(
                "SELECT 1 FROM activations WHERE agent_id = ? AND state IN "
                "('planned', 'running', 'draining')", (row["id"],)))
            result.append(entry)
    return result


def task_list(hub: Hub) -> list[dict[str, Any]]:
    with hub.read() as txn:
        rows = txn.all("SELECT * FROM tasks ORDER BY number = 0 DESC, number")
        return [tasks.task_summary(txn, row) | {"id": row["id"], "title": row["title"]}
                for row in rows]


def requests_open(hub: Hub) -> list[dict[str, Any]]:
    with hub.read() as txn:
        rows = [dict(row) for row in txn.all(
            "SELECT id, kind, title, asker, task_id, body_path, created_at FROM requests "
            "WHERE state = 'open' ORDER BY created_at")]
    return [row | {"body": read_body(hub, row["body_path"])} for row in rows]


def messages_to_operator(hub: Hub, limit: int = 100) -> list[dict[str, Any]]:
    with hub.read() as txn:
        rows = [dict(row) for row in txn.all(
            "SELECT id, sender, summary, body_path, created_at FROM messages "
            "WHERE recipient = 'operator' ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [row | {"body": read_body(hub, row["body_path"])} for row in rows]


def queued_messages(hub: Hub, agent_id: str) -> list[dict[str, Any]]:
    """The operator's messages to an agent that are not delivered yet (withdrawable)."""
    with hub.read() as txn:
        return [dict(row) for row in txn.all(
            "SELECT m.id, m.summary, m.created_at, i.state FROM messages m "
            "JOIN inbox i ON i.ref = m.id AND i.item = 'message' "
            "WHERE m.sender = 'operator' AND m.recipient = ? AND m.withdrawn = 0 "
            "AND i.state IN ('pending', 'prepared', 'uncertain') ORDER BY m.created_at",
            (agent_id,))]


def read_body(hub: Hub, relative: str | None, limit: int = 20_000) -> str:
    """The text of a message or request body under the project root, clipped for display."""
    if not relative:
        return ""
    root = hub.layout.root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        return ""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return "(The body file cannot be read.)"
    return text if len(text) <= limit else text[:limit] + "\n…"


# ---------------------------------------------------------------- task views
def task_graph(hub: Hub) -> dict[str, Any]:
    """Both layers of the operator's task view (DESIGN.md section 15.4)."""
    with hub.read() as txn:
        return relations.task_graph(hub.ctx, txn)


def task_detail(hub: Hub, task_id: str) -> dict[str, Any]:
    with hub.read() as txn:
        row = tasks.task_row(txn, task_id)
        result = tasks.task_summary(txn, row) | {"id": row["id"], "title": row["title"]}
        result["members"] = [dict(member) for member in txn.all(
            "SELECT agent_id, role, state, wait_reason, waiting_since, joined_at, ended_at "
            "FROM memberships WHERE task_id = ? ORDER BY joined_at", (task_id,))]
        result["candidates"] = [dict(candidate) for candidate in txn.all(
            "SELECT id, state, kind, submitter, reason, created_at FROM candidates "
            "WHERE task_id = ? ORDER BY created_at", (task_id,))]
        result["declarations"] = [dict(declaration) for declaration in txn.all(
            "SELECT id, declarer, state, feedback_from, created_at FROM declarations "
            "WHERE task_id = ? ORDER BY created_at", (task_id,))]
    try:
        result["markdown"] = hub.layout.task_markdown(task_id).read_text("utf-8")
    except OSError:
        result["markdown"] = "(The contract file cannot be read.)"
    return result


# ---------------------------------------------------------------- agent turns
TRANSCRIPT_EVENTS = 400
"""The most transcript events shown for one turn, besides what the turn received."""


def agent_turns(hub: Hub, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """One entry per activation, oldest first: what it received and did (section 15.3)."""
    with hub.read() as txn:
        rows = txn.all("SELECT * FROM activations WHERE agent_id = ? ORDER BY planned_at DESC "
                       "LIMIT ?", (agent_id, limit))
        received = {row["id"]: [dict(item) for item in txn.all(
            "SELECT item, ref, summary, state, delivered_at FROM inbox "
            "WHERE activation_id = ? ORDER BY id", (row["id"],))] for row in rows}
    turns = []
    for row in reversed(rows):
        path = hub.layout.transcripts / agent_id / f"{row['id']}.jsonl"
        turns.append({
            "activation": row["id"], "purpose": row["purpose"], "state": row["state"],
            "outcome": row["outcome"], "task_id": row["task_id"],
            "review_id": row["review_id"], "planned_at": row["planned_at"],
            "ended_at": row["ended_at"], "detail": loads(row["detail"]),
            "received": received[row["id"]], "events": _read_transcript(path),
        })
    return turns


def _read_transcript(path: Path) -> list[dict[str, Any]]:
    """A turn's transcript: what it received in full, then its latest events."""
    try:
        lines = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # A torn last line of a turn still being written.
        if isinstance(event, dict):
            events.append(event)
    head = [event for event in events if event.get("type") in ("start", "input")]
    rest = [event for event in events if event.get("type") not in ("start", "input")]
    return head + rest[-TRANSCRIPT_EVENTS:]


def activity(hub: Hub, agent_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with hub.read() as txn:
        if agent_id:
            rows = txn.all("SELECT * FROM events WHERE actor = ? OR subject = ? ORDER BY seq "
                           "DESC LIMIT ?", (agent_id, agent_id, limit))
        else:
            rows = txn.all("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
        return [{"seq": row["seq"], "at": row["at"], "kind": row["kind"],
                 "subject": row["subject"], "actor": row["actor"],
                 "data": loads(row["data"], {})} for row in rows]
