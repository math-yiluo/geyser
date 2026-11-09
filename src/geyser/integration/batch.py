"""Batch integration by the singleton Integrator (DESIGN.md section 9.2).

- Admission: a batch starts when ``integration.batch_threshold`` eligible accepted
  contributions are queued, or when the queue is nonempty and
  ``integration.batch_delay_minutes`` passed since the previous non-notes batch
  finished (before the first batch, since the first eligible acceptance). Accepted
  Wiki notes form a separate batch as soon as the Integrator is idle.
- Membership is frozen at admission; later arrivals wait for the next batch.
- Work: prepare (combined workspace with explicit conflicts), resolve, validate
  (final checks on the frozen tree), publish (the exact validated round; drains).
- Deferral isolates a blocked contribution; ``batch_retry`` requeues it after new
  evidence. A Library head change alone is not a reason.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path, PurePosixPath
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from geyser.clock import parse_iso
from geyser.content.git import Entry, file_mode
from geyser.content.snapshot import staged_changes
from geyser.content.staging import candidate_folder, candidate_view
from geyser.core import activations, agents, candidates, choices, inbox
from geyser.core.actors import Actor
from geyser.core.ledger import Txn, dumps, loads
from geyser.core.models import ValidationReport, Verdict
from geyser.domains.base import TreeSnapshot, ValidationContext
from geyser.errors import Conflict, InvalidArgument
from geyser.ids import Kind
from geyser.integration import publication
from geyser.layout import PRIVATE_DIR

if TYPE_CHECKING:
    from geyser.hub import Hub

_MARKER = re.compile(rb"^(<<<<<<<|>>>>>>>) ", re.MULTILINE)


# ---------------------------------------------------------------------- admission
def eligible(hub: Hub, txn: Txn) -> list[Row]:
    rows = candidates.accepted_for_integration(hub.ctx, txn)
    return [row for row in rows if row["batch_id"] is None and row["deferred"] is None]


def open_batch(txn: Txn) -> Row | None:
    return txn.one("SELECT * FROM batches WHERE state = 'open' ORDER BY created_at LIMIT 1")


def admit_due(hub: Hub, txn: Txn, may_run: Callable[[Txn, Row], bool]) -> str | None:
    """Start a due batch for the Integrator; returns the planned activation, if any."""
    if open_batch(txn) is not None:
        return None
    queue = eligible(hub, txn)
    if not queue:
        return None
    notes = [row for row in queue if row["kind"] == "note"]
    others = [row for row in queue if row["kind"] != "note"]
    members: list[Row] = []
    notes_only = False
    if notes:
        members, notes_only = notes, True
    elif others and _non_notes_due(hub, txn, others):
        members = others
    if not members:
        return None
    integrator = agents.create(hub.ctx, txn, Kind.INTEGRATOR)
    agent = agents.agent_row(txn, integrator)
    if not may_run(txn, agent):
        return None
    backend = choices.role_choice(hub.ctx, txn, "integrator").backend
    if agent["backend"] != backend:
        # Each batch starts a new conversation, so the Integrator takes its role's
        # current backend here (section 6.1).
        txn.execute("UPDATE agents SET backend = ?, conversation_id = NULL, "
                    "view_baseline = NULL WHERE id = ?", (backend, integrator))
        txn.event("agent.moved", integrator, previous=agent["backend"], backend=backend)
    batch_id = txn.next_id(Kind.BATCH)
    member_ids = [row["id"] for row in members]
    txn.execute("INSERT INTO batches (id, state, notes_only, members, created_at) "
                "VALUES (?, 'open', ?, ?, ?)",
                (batch_id, int(notes_only), dumps(member_ids), txn.now))
    for candidate_id in member_ids:
        txn.execute("UPDATE candidates SET batch_id = ? WHERE id = ?", (batch_id, candidate_id))
    txn.execute("UPDATE agents SET current_batch = ?, intent = 'start' WHERE id = ?",
                (batch_id, integrator))
    txn.event("batch.admitted", batch_id, members=member_ids, notes_only=notes_only)
    row = activations.plan(txn, integrator, "integration", batch_id=batch_id,
                           new_conversation=True)
    return row["id"]


def _non_notes_due(hub: Hub, txn: Txn, queue: list[Row]) -> bool:
    settings = hub.config.integration
    if len(queue) >= settings.batch_threshold:
        return True
    last = txn.scalar("SELECT MAX(finished_at) FROM batches WHERE notes_only = 0 "
                      "AND state != 'open'")
    since = last or min(row["decided_at"] for row in queue)
    return txn.moment - parse_iso(since) >= timedelta(minutes=settings.batch_delay_minutes)


# ------------------------------------------------------------------- operations
def current_batch(txn: Txn, actor: Actor) -> Row:
    if actor.batch_id is None:
        raise Conflict("This turn has no batch.")
    if actor.is_agent:
        activations.check_actor(txn, actor)
    row = txn.one("SELECT * FROM batches WHERE id = ?", (actor.batch_id,))
    if row is None or row["state"] != "open":
        raise Conflict(f"{actor.batch_id} is not open.")
    return row


def members_of(row: Row) -> list[str]:
    return list(loads(row["members"], []))


def status(hub: Hub, txn: Txn, actor: Actor) -> dict[str, Any]:
    batch = current_batch(txn, actor)
    members = []
    for candidate_id in members_of(batch):
        candidate = candidates.candidate_row(txn, candidate_id)
        review = txn.one("SELECT reviewer, report_path, integration_notes FROM reviews "
                         "WHERE candidate_id = ?", (candidate_id,))
        members.append({
            "candidate": candidate_id, "task": candidate["task_id"], "kind": candidate["kind"],
            "files": str(candidate_folder(hub, candidate_id) / "files"),
            "review_report": str(hub.layout.root / review["report_path"]) if review and
            review["report_path"] else None,
            "integration_notes": loads(review["integration_notes"]) if review else None,
        })
    deferred = loads(batch["deferred"], [])
    return {"batch": batch["id"], "notes_only": bool(batch["notes_only"]), "members": members,
            "deferred": deferred, "workspace": str(hub.layout.workspace(actor.id)),
            "prepared": _batch_meta(txn, batch["id"]).get("prepared_head") is not None,
            "validated_tree": batch["final_tree"]}


def _batch_meta(txn: Txn, batch_id: str) -> dict[str, Any]:
    value = txn.meta(f"batch:{batch_id}")
    return loads(value, {}) if value else {}


def _set_meta(txn: Txn, batch_id: str, meta: dict[str, Any]) -> None:
    txn.set_meta(f"batch:{batch_id}", dumps(meta) or "{}")


async def prepare(hub: Hub, actor: Actor) -> dict[str, Any]:
    """Build the combined workspace: the head plus every member, conflicts marked."""
    with hub.read() as txn:
        batch = current_batch(txn, actor)
        rows = [candidates.candidate_row(txn, cid) for cid in members_of(batch)]
    repo = hub.library.repo
    head = hub.library.head()
    tree = repo.tree_of(head) if head else repo.build_tree(None, {})
    conflicts: dict[str, list[str]] = {}
    for row in rows:
        changed, deleted = staged_changes(candidate_folder(hub, row["id"]))
        base = row["base_commit"] or None
        member_tree = await asyncio.to_thread(repo.build_tree, base, changed, deleted)
        ours = await asyncio.to_thread(repo.commit_tree, tree, [], "batch accumulator\n")
        theirs = await asyncio.to_thread(repo.commit_tree, member_tree, [base] if base else [],
                                         f"{row['id']}\n")
        merge_base: str = base if base else await asyncio.to_thread(
            repo.commit_tree, repo.build_tree(None, {}), [], "empty\n")
        result = await asyncio.to_thread(repo.merge, merge_base, ours, theirs)
        if result.tree is None:
            raise Conflict(f"Merging {row['id']} failed without a result tree.")
        tree = result.tree
        if result.conflicts:
            conflicts[row["id"]] = result.conflicts
    workspace = hub.layout.workspace(actor.id)
    await asyncio.to_thread(repo.materialize, tree, workspace, (".git", PRIVATE_DIR))
    with hub.transaction() as txn:
        _set_meta(txn, batch["id"], {"prepared_head": head, "prepared_tree": tree,
                                     "conflicts": conflicts})
        txn.execute("UPDATE batches SET final_tree = NULL WHERE id = ?", (batch["id"],))
        txn.event("batch.prepared", batch["id"], actor.id, conflicts=conflicts)
    return {"batch": batch["id"], "workspace": str(workspace), "conflicts": conflicts,
            "next": "Resolve the conflicts and organize files, then call batch_resolve."}


def _workspace_tree(hub: Hub, workspace: Path) -> str:
    repo = hub.library.repo
    entries: dict[str, Entry] = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if relative.parts[0] in (PRIVATE_DIR, ".git") or not path.is_file():
            continue
        if path.is_symlink():
            raise InvalidArgument(f"`{relative.as_posix()}` is a link; links are not published.")
        entries[PurePosixPath(*relative.parts).as_posix()] = Entry(file_mode(path),
                                                                    repo.write_file(path))
    return repo.build_tree(None, entries)


async def resolve(hub: Hub, actor: Actor) -> dict[str, Any]:
    """Stage the workspace as the batch's resolved tree; no conflict marker may remain."""
    with hub.read() as txn:
        batch = current_batch(txn, actor)
        meta = _batch_meta(txn, batch["id"])
    if not meta.get("prepared_tree"):
        raise Conflict("Call batch_prepare first.")
    workspace = hub.layout.workspace(actor.id)
    conflicted = sorted({path for paths in meta.get("conflicts", {}).values() for path in paths})
    remaining = [path for path in conflicted
                 if (workspace / Path(*path.split("/"))).is_file()
                 and _MARKER.search((workspace / Path(*path.split("/"))).read_bytes())]
    if remaining:
        raise Conflict("Conflict markers remain.", details={"paths": remaining})
    tree = await asyncio.to_thread(_workspace_tree, hub, workspace)
    with hub.transaction() as txn:
        meta["resolved_tree"] = tree
        _set_meta(txn, batch["id"], meta)
        txn.execute("UPDATE batches SET final_tree = NULL WHERE id = ?", (batch["id"],))
        txn.event("batch.resolved", batch["id"], actor.id, tree=tree)
    return {"batch": batch["id"], "resolved_tree": tree[:12],
            "next": "Call batch_validate to freeze and check the final tree."}


async def validate(hub: Hub, actor: Actor) -> dict[str, Any]:
    """Freeze the resolved tree and run the final checks of its members' policies."""
    with hub.read() as txn:
        batch = current_batch(txn, actor)
        meta = _batch_meta(txn, batch["id"])
        rows = [candidates.candidate_row(txn, cid) for cid in members_of(batch)]
    tree: str | None = meta.get("resolved_tree")
    if not tree:
        raise Conflict("Call batch_resolve first.")
    assert tree is not None
    if meta.get("prepared_head") != hub.library.head():
        raise Conflict("The Library head moved since batch_prepare; prepare again.")
    scratch = hub.layout.scratch / f"batch-{batch['id']}-{uuid.uuid4().hex[:8]}"
    report: ValidationReport
    try:
        await asyncio.to_thread(hub.library.repo.materialize, tree, scratch / "tree", ())
        context = ValidationContext(hub.layout, hub.config, scratch)
        views = []
        managers = []
        for row in rows:
            manager = candidate_view(hub, row)
            views.append(manager.__enter__())
            managers.append(manager)
        try:
            report = await hub.pack.check_tree(context, TreeSnapshot(scratch / "tree", tree),
                                               views)
        except Exception as error:  # noqa: BLE001
            report = ValidationReport(verdict=Verdict.UNAVAILABLE,
                                      summary=f"The final check raised {error!r}.")
        finally:
            for manager in managers:
                manager.__exit__(None, None, None)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    with hub.transaction() as txn:
        if report.unqualified_acceptance:
            txn.execute("UPDATE batches SET final_tree = ? WHERE id = ?", (tree, batch["id"]))
        txn.event("batch.validated", batch["id"], actor.id, verdict=report.verdict.value)
    return {"batch": batch["id"], "verdict": report.verdict.value, "summary": report.summary,
            "details": report.details,
            "next": "batch_publish" if report.unqualified_acceptance else
            "Fix the tree (or defer the blocking member), resolve and validate again."}


def check_publishable(hub: Hub, txn: Txn, actor: Actor) -> dict[str, Any]:
    batch = current_batch(txn, actor)
    meta = _batch_meta(txn, batch["id"])
    if not batch["final_tree"] or batch["final_tree"] != meta.get("resolved_tree"):
        raise Conflict("Validate the current resolved tree before publishing.")
    if meta.get("prepared_head") != hub.library.head():
        raise Conflict("The Library head moved since validation; prepare and validate again.")
    if not members_of(batch):
        raise Conflict("Every member was deferred; call batch_finish_empty.")
    for candidate_id in members_of(batch):
        state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (candidate_id,))
        if state != "accepted":
            raise Conflict(f"{candidate_id} is {state}; defer it and prepare again.")
    return {"batch_id": batch["id"], "tree": batch["final_tree"],
            "parent": meta.get("prepared_head")}


async def commit_publish(hub: Hub, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
    """The drained commit of ``batch_publish``: publish the exact validated round."""
    batch_id = payload["batch_id"]
    await asyncio.to_thread(publication.finish_pending, hub)
    with hub.read() as txn:
        batch = txn.one("SELECT * FROM batches WHERE id = ?", (batch_id,))
        if batch is not None and batch["state"] == "published":
            return {"batch": batch_id, "already_published": True}  # A retry after a crash.
        if batch is None or batch["state"] != "open" or batch["final_tree"] != payload["tree"]:
            raise Conflict(f"{batch_id} changed after its validation.")
        member_ids = members_of(batch)
    if hub.library.head() != payload["parent"]:
        raise Conflict("The Library head moved before publication; prepare again.")
    # The publication record and the batch become published in one transaction.
    commit = await asyncio.to_thread(publication.publish, hub, "batch", member_ids,
                                     payload["tree"], payload["parent"],
                                     f"Publish {batch_id}: {', '.join(member_ids)}")
    hub.ctx.wake()
    return {"batch": batch_id, "commit": commit}


def defer(hub: Hub, txn: Txn, actor: Actor, candidate_id: str, reason: str) -> dict[str, Any]:
    batch = current_batch(txn, actor)
    members = members_of(batch)
    if candidate_id not in members:
        raise InvalidArgument(f"{candidate_id} is not a member of {batch['id']}.")
    if not reason.strip():
        raise InvalidArgument("A deferral needs a reason.")
    members.remove(candidate_id)
    deferred = loads(batch["deferred"], [])
    deferred.append({"candidate": candidate_id, "reason": reason, "at": txn.now})
    txn.execute("UPDATE batches SET members = ?, deferred = ?, final_tree = NULL WHERE id = ?",
                (dumps(members), dumps(deferred), batch["id"]))
    txn.execute("UPDATE candidates SET batch_id = NULL, deferred = ? WHERE id = ?",
                (reason, candidate_id))
    candidate = candidates.candidate_row(txn, candidate_id)
    inbox.notify_many(txn, candidates.authors_of(candidate),
                      f"{candidate_id} was deferred by the Integrator: {reason}",
                      ref=candidate_id, task_id=candidate["task_id"])
    txn.event("batch.deferred", batch["id"], actor.id, candidate=candidate_id, reason=reason)
    return {"batch": batch["id"], "deferred": candidate_id,
            "next": "Prepare again: the remaining members are validated again."}


def retry(hub: Hub, txn: Txn, actor: Actor, candidate_id: str, evidence: str) -> dict[str, Any]:
    candidate = candidates.candidate_row(txn, candidate_id)
    if candidate["deferred"] is None:
        raise Conflict(f"{candidate_id} is not deferred.")
    if not evidence.strip():
        raise InvalidArgument("Name the new evidence that resolves the blocker.")
    txn.execute("UPDATE candidates SET deferred = NULL WHERE id = ?", (candidate_id,))
    txn.event("batch.retry", candidate_id, actor.id, evidence=evidence)
    txn.after_commit(hub.ctx.wake)
    return {"candidate": candidate_id, "requeued": True}


def finish_empty(hub: Hub, txn: Txn, actor: Actor) -> dict[str, Any]:
    batch = current_batch(txn, actor)
    if members_of(batch):
        raise Conflict(f"{batch['id']} still has members.")
    txn.execute("UPDATE batches SET state = 'empty', finished_at = ? WHERE id = ?",
                (txn.now, batch["id"]))
    txn.execute("UPDATE agents SET current_batch = NULL, intent = 'none' WHERE id = ?",
                (actor.id,))
    txn.event("batch.empty", batch["id"], actor.id)
    txn.after_commit(hub.ctx.wake)
    return {"batch": batch["id"], "state": "empty", "turn": "End this turn."}
