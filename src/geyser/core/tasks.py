"""Tasks: publication, revisions and states (DESIGN.md section 5)."""

from __future__ import annotations

import re
from sqlite3 import Row
from typing import Any

from pydantic import ValidationError

from geyser.core import inbox
from geyser.core.actors import Actor, Capability
from geyser.core.context import HubContext
from geyser.core.ledger import Txn
from geyser.core.models import (
    RevisionClass,
    TaskContract,
    check_bar_kept,
    classify_revision,
)
from geyser.errors import Conflict, Forbidden, IntegrityError, InvalidArgument, NotFound
from geyser.ids import Kind, kind_of, require_id
from geyser.jsonio import atomic_write_json, atomic_write_text, read_json, sha256_json

TERMINAL_STATES = frozenset({"completed", "retired"})
_FILE_NAME = re.compile(r"^[\w./-]+\.[A-Za-z0-9]{1,8}$")


# ------------------------------------------------------------------------- reading
def task_row(txn: Txn, task_id: str) -> Row:
    require_id(task_id, Kind.TASK, field="task_id")
    row = txn.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise NotFound(f"{task_id} does not exist.")
    return row


def load_contract(ctx: HubContext, txn: Txn, task_id: str, revision: int | None = None
                  ) -> TaskContract:
    """Read a contract revision from its file and check it against the ledger hash."""
    row = task_row(txn, task_id)
    number = row["revision"] if revision is None else revision
    record = txn.one(
        "SELECT hash FROM task_revisions WHERE task_id = ? AND revision = ?", (task_id, number)
    )
    if record is None:
        raise NotFound(f"{task_id} has no revision {number}.")
    path = ctx.layout.revision_file(task_id, number)
    try:
        data = read_json(path)
    except (OSError, ValueError) as error:
        raise IntegrityError(f"The contract file of {task_id} revision {number} is unreadable."
                             ) from error
    if sha256_json(data) != record["hash"]:
        raise IntegrityError(f"The contract file of {task_id} revision {number} was changed.")
    return TaskContract.model_validate(data)


def title_of(txn: Txn, task_id: str) -> str:
    title = txn.scalar("SELECT title FROM tasks WHERE id = ?", (task_id,))
    return f"{task_id} ({title})" if title else task_id


def member_ids(txn: Txn, task_id: str, *, states: tuple[str, ...] = ("active", "waiting")
               ) -> list[str]:
    marks = ",".join("?" * len(states))
    rows = txn.all(
        f"SELECT agent_id FROM memberships WHERE task_id = ? AND state IN ({marks}) "
        "ORDER BY joined_at",
        (task_id, *states),
    )
    return [row["agent_id"] for row in rows]


def is_maintainer(txn: Txn, task_id: str, agent_id: str) -> bool:
    row = task_row(txn, task_id)
    return row["publisher"] == agent_id or agent_id in member_ids(txn, task_id)


# ---------------------------------------------------------------------- validation
def _check_contract(ctx: HubContext, txn: Txn, contract: TaskContract) -> None:
    """The mechanical checks at publication and revision (section 5.2)."""
    objective = contract.objective.strip()
    if _FILE_NAME.match(objective) and " " not in objective:
        raise InvalidArgument(
            "The objective is only a file name. State what the task must achieve; "
            "list files under `inputs`."
        )
    kinds = ctx.pack.task_kinds()
    if contract.kind not in kinds:
        raise InvalidArgument(
            f"Unknown task kind `{contract.kind}`.", details={"kinds": sorted(kinds)}
        )
    policies = ctx.pack.policies()
    if contract.policy is not None and contract.policy not in policies:
        raise InvalidArgument(
            f"Unknown policy `{contract.policy}`.", details={"policies": sorted(policies)}
        )
    for value in contract.inputs:
        _check_input_resolves(ctx, txn, value)
    for requirement in contract.requirements:
        provider = requirement.provider
        if kind_of(provider) is Kind.TASK:
            provider_row = txn.one("SELECT revision FROM tasks WHERE id = ?", (provider,))
            if provider_row is None:
                raise InvalidArgument(f"Requirement `{requirement.key}` names {provider}, "
                                      "which does not exist.")
            if requirement.revision is not None and requirement.revision > provider_row[0]:
                raise InvalidArgument(f"{provider} has no revision {requirement.revision}.")
        elif txn.one("SELECT 1 FROM candidates WHERE id = ?", (provider,)) is None:
            raise InvalidArgument(f"Requirement `{requirement.key}` names {provider}, "
                                  "which does not exist.")
    try:
        ctx.pack.validate_contract(contract)
    except (ValueError, ValidationError) as error:
        raise InvalidArgument(f"The domain pack refused the contract: {error}") from error


def _check_input_resolves(ctx: HubContext, txn: Txn, value: str) -> None:
    if kind_of(value) is Kind.CANDIDATE:
        if txn.one("SELECT 1 FROM candidates WHERE id = ?", (value,)) is None:
            raise InvalidArgument(f"Input {value} does not exist.")
        return
    if kind_of(value) is not None:
        raise InvalidArgument(f"Input `{value}` must be a Library path or a candidate ID.")
    from geyser.layout import safe_relative_path

    path = safe_relative_path(value, field="inputs").as_posix()
    if not ctx.library.exists(path):
        raise InvalidArgument(f"Input `{path}` does not exist in the published Library.")


def unavailable_inputs(txn: Txn, contract: TaskContract) -> list[str]:
    """Candidate inputs that are not published (a task with any is set aside)."""
    missing = []
    for value in contract.inputs:
        if kind_of(value) is Kind.CANDIDATE:
            state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (value,))
            if state != "published":
                missing.append(value)
    return missing


# ------------------------------------------------------------------- file writing
def _write_revision(ctx: HubContext, task_id: str, revision: int, contract: TaskContract,
                    meta: dict[str, Any]) -> str:
    data = contract.model_dump(mode="json")
    atomic_write_json(ctx.layout.revision_file(task_id, revision), data)
    atomic_write_text(ctx.layout.task_markdown(task_id), render_markdown(task_id, contract, meta))
    return sha256_json(data)


def render_markdown(task_id: str, contract: TaskContract, meta: dict[str, Any]) -> str:
    """A readable rendering of the current contract (``tasks/<id>/task.md``)."""
    lines = [f"# {task_id}: {contract.title}", ""]
    lines.append(
        f"Revision {meta['revision']} · kind `{contract.kind}` · priority {contract.priority}"
        + (f" · policy `{contract.policy}`" if contract.policy else "")
        + f" · published by {meta['publisher']}"
    )
    lines += ["", "## Objective", "", contract.objective.strip(), ""]
    if contract.inputs:
        lines += ["## Inputs", ""] + [f"- `{value}`" for value in contract.inputs] + [""]
    if contract.requirements:
        lines += ["## Requirements", ""]
        for requirement in contract.requirements:
            detail = requirement.provider
            if requirement.revision is not None:
                detail += f" revision {requirement.revision}"
            lines.append(f"- `requirement:{requirement.key}`: {detail}")
        lines.append("")
    if contract.constraints:
        lines += ["## Constraints", ""]
        lines += [f"{i}. {text}" for i, text in enumerate(contract.constraints, start=1)]
        lines.append("")
    lines += ["## Acceptance conditions", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(contract.acceptance, start=1)]
    lines.append("")
    if contract.context.strip():
        lines += ["## Context", "", contract.context.strip(), ""]
    if contract.payload:
        from geyser.jsonio import pretty_json

        lines += ["## Payload", "", "```json", pretty_json(contract.payload).rstrip(), "```", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------- publication
def _similar_titles(txn: Txn, title: str, exclude: str | None = None) -> list[str]:
    """Open tasks whose titles share most words with ``title`` (a warning, section 5.3)."""
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    if not words:
        return []
    similar = []
    for row in txn.all(
        "SELECT id, title FROM tasks WHERE state NOT IN ('completed', 'retired')"
    ):
        if row["id"] == exclude:
            continue
        other = set(re.findall(r"[a-z0-9]+", row["title"].lower()))
        if other and len(words & other) / len(words | other) >= 0.6:
            similar.append(f"{row['id']} ({row['title']})")
    return similar[:10]


def publish(ctx: HubContext, txn: Txn, actor: Actor, contract: TaskContract,
            *, parent_task: str | None = None, repair_of: str | None = None) -> dict[str, Any]:
    """Publish a task (section 5.3). Assignment then fills its executor place at once."""
    _check_contract(ctx, txn, contract)
    similar = _similar_titles(txn, contract.title)
    task_id = txn.next_id(Kind.TASK)
    number = int(task_id.split("-")[1])
    digest = _write_revision(ctx, task_id, 0, contract,
                             {"revision": 0, "publisher": actor.id})
    state = "set_aside" if unavailable_inputs(txn, contract) else "open"
    txn.execute(
        "INSERT INTO tasks (id, number, title, kind, policy, priority, state, revision, "
        "publisher, parent_task, repair_of, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
        (task_id, number, contract.title, contract.kind, contract.policy, contract.priority,
         state, actor.id, parent_task, repair_of, txn.now, txn.now),
    )
    txn.execute(
        "INSERT INTO task_revisions (task_id, revision, hash, author, reason, change, "
        "created_at) VALUES (?, 0, ?, ?, 'published', 'initial', ?)",
        (task_id, digest, actor.id, txn.now),
    )
    txn.execute(
        "INSERT INTO opportunities (task_id, role, state, created_at) "
        "VALUES (?, 'executor', 'open', ?)",
        (task_id, txn.now),
    )
    txn.event("task.published", task_id, actor.id, title=contract.title, state=state)
    txn.after_commit(ctx.wake)
    reply: dict[str, Any] = {"task": f"{task_id} ({contract.title})", "task_id": task_id,
                             "revision": 0, "state": state}
    if similar:
        reply["similar_open_tasks"] = similar
        reply["warning"] = ("Open tasks with similar titles exist. Check that this task does "
                            "not duplicate one of them.")
    return reply


# ----------------------------------------------------------------------- revisions
def revise(ctx: HubContext, txn: Txn, actor: Actor, task_id: str, expected_revision: int,
           contract: TaskContract, reason: str) -> dict[str, Any]:
    """Revise a task's contract (section 5.4)."""
    row = task_row(txn, task_id)
    if row["state"] in TERMINAL_STATES:
        raise Conflict(f"{title_of(txn, task_id)} is {row['state']} and cannot be revised.")
    _check_revision_authority(txn, actor, row)
    if not reason.strip():
        raise InvalidArgument("A revision needs a reason.")
    if expected_revision != row["revision"]:
        raise Conflict(
            f"{task_id} is at revision {row['revision']}, not {expected_revision}. "
            "Read it again and revise the current revision.",
            details={"current_revision": row["revision"]},
        )
    original = load_contract(ctx, txn, task_id, 0)
    previous = load_contract(ctx, txn, task_id)
    if contract == previous:
        raise InvalidArgument("The revision changes nothing.")
    try:
        check_bar_kept(original, previous, contract)
    except ValueError as error:
        raise InvalidArgument(str(error)) from error
    _check_contract(ctx, txn, contract)
    _check_no_cycle(ctx, txn, task_id, previous, contract)
    change = classify_revision(previous, contract)
    revision = row["revision"] + 1
    digest = _write_revision(ctx, task_id, revision, contract,
                             {"revision": revision, "publisher": row["publisher"]})
    txn.execute(
        "UPDATE tasks SET title = ?, kind = ?, policy = ?, priority = ?, revision = ?, "
        "updated_at = ? WHERE id = ?",
        (contract.title, contract.kind, contract.policy, contract.priority, revision, txn.now,
         task_id),
    )
    txn.execute(
        "INSERT INTO task_revisions (task_id, revision, hash, author, reason, change, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, revision, digest, actor.id, reason, change.value, txn.now),
    )
    txn.event("task.revised", task_id, actor.id, revision=revision, change=change.value,
              reason=reason)
    if change is RevisionClass.MATERIAL:
        from geyser.core import candidates

        candidates.invalidate_for_material_revision(ctx, txn, task_id, revision)
    executors = [agent for agent in member_ids(txn, task_id) if agent != actor.id]
    inbox.notify_many(
        txn, executors,
        f"{task_id} was revised to revision {revision} ({change.value}): {reason}. "
        "Reconsider your plan against the current contract.",
        task_id=task_id, wakes=True,
    )
    _notify_dependents(txn, task_id, f"{task_id} was revised ({change.value}) to revision "
                       f"{revision}. Check whether your requirement on it still holds.")
    refresh_state(ctx, txn, task_id)
    txn.after_commit(ctx.wake)
    return {"task": f"{task_id} ({contract.title})", "revision": revision,
            "change": change.value}


def _check_no_cycle(ctx: HubContext, txn: Txn, task_id: str, previous: TaskContract,
                    contract: TaskContract) -> None:
    """Refuse a revision whose new needs would close a cycle of waiting tasks (5.6)."""
    from geyser.core import wait_graph

    added = sorted(wait_graph.contract_needs(txn, contract)
                   - wait_graph.contract_needs(txn, previous))
    if not added:
        return
    cycle = wait_graph.cycle_through(ctx, txn, task_id, added)
    if cycle:
        names = " → ".join(title_of(txn, node) for node in cycle)
        raise Conflict(f"This revision would close a cycle of tasks waiting on each other: "
                       f"{names}.", details={"reason": "requirement_cycle", "cycle": cycle})


def _check_revision_authority(txn: Txn, actor: Actor, row: Row) -> None:
    if actor.is_operator:
        return
    task_id = row["id"]
    is_publisher = row["publisher"] == actor.id
    is_member = actor.id in member_ids(txn, task_id)
    if not (is_publisher or is_member):
        raise Forbidden(f"Only the publisher and the executors of {task_id} may revise it.")
    if is_publisher:
        return
    if actor.capability is not Capability.WORKER or actor.task_id != task_id:
        raise Forbidden(
            f"An executor that did not publish {task_id} revises it only in its own task turn."
        )


def _notify_dependents(txn: Txn, task_id: str, summary: str) -> None:
    """Notify publishers of tasks whose requirements name ``task_id`` and agents waiting on it."""
    waiting = txn.all(
        "SELECT agent_id, predicate FROM memberships WHERE state = 'waiting' "
        "AND predicate IS NOT NULL"
    )
    agents = {row["agent_id"] for row in waiting if f'"{task_id}"' in row["predicate"]}
    publisher = txn.scalar("SELECT publisher FROM tasks WHERE id = ?", (task_id,))
    if publisher and kind_of(publisher) in {Kind.WORKER, Kind.REVIEWER}:
        agents.add(publisher)
    inbox.notify_many(txn, agents, summary, task_id=task_id)


# -------------------------------------------------------------------------- states
def refresh_state(ctx: HubContext, txn: Txn, task_id: str) -> str:
    """Recompute and store a task's state from its facts (section 5.5)."""
    row = task_row(txn, task_id)
    state = row["state"]
    if state in TERMINAL_STATES:
        return state
    declaration = txn.scalar(
        "SELECT 1 FROM declarations WHERE task_id = ? AND state IN "
        "('awaiting_feedback', 'deciding')",
        (task_id,),
    )
    members = txn.all(
        "SELECT state FROM memberships WHERE task_id = ? AND state IN ('active', 'waiting')",
        (task_id,),
    )
    ever_joined = bool(txn.scalar("SELECT 1 FROM memberships WHERE task_id = ?", (task_id,)))
    if declaration:
        new_state = "completing"
    elif members:
        new_state = "active" if any(m["state"] == "active" for m in members) else "waiting"
    elif ever_joined:
        new_state = "retired"
    else:
        contract = load_contract(ctx, txn, task_id)
        new_state = "set_aside" if unavailable_inputs(txn, contract) else "open"
    if new_state != state:
        txn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                    (new_state, txn.now, task_id))
        txn.event("task.state", task_id, state=new_state, previous=state)
        if new_state == "retired":
            _on_retired(txn, task_id)
    return new_state


def _on_retired(txn: Txn, task_id: str) -> None:
    txn.execute(
        "UPDATE opportunities SET state = 'cancelled', closed_at = ? "
        "WHERE task_id = ? AND state = 'open'",
        (txn.now, task_id),
    )
    _notify_dependents(txn, task_id, f"{task_id} was retired: every executor left. "
                       "A requirement on it will not be met unless it is republished.")


def refresh_set_aside(ctx: HubContext, txn: Txn) -> None:
    """Return set-aside tasks to ``open`` once their inputs are published.

    A task that cannot be evaluated (a damaged contract file) is skipped and reported
    once; it never blocks the others or the publication that triggered the refresh.
    """
    for row in txn.all("SELECT id FROM tasks WHERE state IN ('set_aside', 'open')"):
        try:
            refresh_state(ctx, txn, row["id"])
        except IntegrityError as error:
            key = f"task.unevaluable:{row['id']}"
            if not txn.scalar("SELECT 1 FROM events WHERE kind = 'admission.item_skipped' "
                              "AND subject = ?", (key,)):
                txn.event("admission.item_skipped", key, reason=error.message)


def mark_completed(ctx: HubContext, txn: Txn, task_id: str, revision: int) -> None:
    txn.execute(
        "UPDATE tasks SET state = 'completed', completed_revision = ?, completed_at = ?, "
        "evidence_valid = 1, updated_at = ? WHERE id = ?",
        (revision, txn.now, txn.now, task_id),
    )
    txn.execute(
        "UPDATE opportunities SET state = 'cancelled', closed_at = ? "
        "WHERE task_id = ? AND state = 'open'",
        (txn.now, task_id),
    )
    txn.event("task.completed", task_id, revision=revision)
    _notify_dependents(txn, task_id, f"{title_of(txn, task_id)} is completed.")
    txn.after_commit(ctx.wake)


def task_summary(txn: Txn, row: Row) -> dict[str, Any]:
    return {
        "task": f"{row['id']} ({row['title']})",
        "state": row["state"],
        "revision": row["revision"],
        "priority": row["priority"],
        "publisher": row["publisher"],
        "executors": member_ids(txn, row["id"]),
    }
