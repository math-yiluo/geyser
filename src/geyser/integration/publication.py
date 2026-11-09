"""Publication with a durable intent and an expected-head update (DESIGN.md section 9.3).

1. Record an ``intent`` naming the parent head and the new commit.
2. Move the branch only if it still points at the parent (``update-ref`` with the
   expected old value).
3. Record the publication as ``committed`` and mark its candidates published.

A crash between 2 and 3 is recovered exactly: if the branch already points at the
intended commit, step 3 completes; if it still points at the parent, the intent is
abandoned and the candidates are integrated again later.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from geyser.content.git import GitError
from geyser.core import candidates
from geyser.core.ledger import dumps, loads
from geyser.errors import Conflict
from geyser.ids import Kind

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)


def commit_message(hub: Hub, member_ids: Sequence[str], title: str) -> str:
    lines = [title, ""]
    with hub.read() as txn:
        for candidate_id in member_ids:
            row = candidates.candidate_row(txn, candidate_id)
            review = txn.one("SELECT reviewer FROM reviews WHERE candidate_id = ?",
                             (candidate_id,))
            validation = candidates.newest_validation(txn, candidate_id)
            lines += [
                f"Candidate: {candidate_id}",
                f"Task: {row['task_id']}",
                f"Authors: {', '.join(candidates.authors_of(row))}",
                f"Reviewer: {review['reviewer'] if review else 'none'}",
                f"Validation: {validation['id'] if validation else 'none'}",
            ]
    return "\n".join(lines) + "\n"


def publish(hub: Hub, mode: str, member_ids: Sequence[str], tree: str, parent: str | None,
            title: str) -> str:
    """Publish ``tree`` on top of ``parent`` for ``member_ids``; returns the commit."""
    repo = hub.library.repo
    commit = repo.commit_tree(tree, [parent] if parent else [],
                              commit_message(hub, member_ids, title))
    with hub.transaction() as txn:
        for candidate_id in member_ids:
            state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (candidate_id,))
            if state != "accepted":
                raise Conflict(f"{candidate_id} is {state}, no longer accepted; it is not "
                               "published.")
        publication_id = txn.next_id(Kind.PUBLICATION)
        txn.execute(
            "INSERT INTO publications (id, mode, members, parent_commit, commit_id, state, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'intent', ?)",
            (publication_id, mode, dumps(list(member_ids)), parent, commit, txn.now),
        )
    try:
        repo.update_branch(commit, parent)
    except GitError:
        with hub.transaction() as txn:
            txn.execute("UPDATE publications SET state = 'abandoned' WHERE id = ?",
                        (publication_id,))
        hub.library.invalidate_cache()
        raise
    hub.library.invalidate_cache()
    _complete(hub, publication_id)
    return commit


def _complete(hub: Hub, publication_id: str) -> None:
    with hub.transaction() as txn:
        row = txn.one("SELECT * FROM publications WHERE id = ?", (publication_id,))
        if row is None or row["state"] != "intent":
            return
        txn.execute("UPDATE publications SET state = 'committed', committed_at = ? WHERE id = ?",
                    (txn.now, publication_id))
        members = loads(row["members"], [])
        if row["mode"] == "batch":
            _finish_batches(txn, members)
        candidates.mark_published(hub.ctx, txn, members, row["commit_id"], publication_id)
        txn.event("library.published", publication_id, commit=row["commit_id"],
                  members=members, mode=row["mode"])
    hub.library.sync_work_tree()


def _finish_batches(txn, members: list[str]) -> None:
    """A batch is published in the same transaction as its publication record."""
    marks = ",".join("?" * len(members)) or "NULL"
    for row in txn.all(f"SELECT DISTINCT batch_id FROM candidates WHERE id IN ({marks}) "
                       "AND batch_id IS NOT NULL", tuple(members)):
        txn.execute("UPDATE batches SET state = 'published', finished_at = ? WHERE id = ? "
                    "AND state = 'open'", (txn.now, row["batch_id"]))
        txn.execute("UPDATE agents SET current_batch = NULL, intent = 'none' "
                    "WHERE current_batch = ?", (row["batch_id"],))
        txn.event("batch.published", row["batch_id"])


def finish_pending(hub: Hub) -> None:
    """Finish publications whose branch update happened but whose record did not.

    Run before anything else is integrated, so a failure after the branch moved is
    completed, never republished. Each intent is handled on its own.
    """
    head = hub.library.repo.head()
    with hub.read() as txn:
        intents = txn.all("SELECT * FROM publications WHERE state = 'intent' ORDER BY id")
    for row in intents:
        if head == row["commit_id"]:
            _complete(hub, row["id"])
        elif head == row["parent_commit"]:
            with hub.transaction() as txn:
                txn.execute("UPDATE publications SET state = 'abandoned' WHERE id = ? AND "
                            "state = 'intent'", (row["id"],))
                txn.event("library.intent_abandoned", row["id"])
    if intents:
        hub.library.invalidate_cache()


def recover(hub: Hub) -> None:
    """Finish or abandon publications left as intents by a crash."""
    head = hub.library.repo.head()
    with hub.read() as txn:
        intents = txn.all("SELECT * FROM publications WHERE state = 'intent' ORDER BY id")
    for row in intents:
        if head == row["commit_id"]:
            log.info("completing publication %s after a restart", row["id"])
            _complete(hub, row["id"])
        else:
            with hub.transaction() as txn:
                txn.execute("UPDATE publications SET state = 'abandoned' WHERE id = ?",
                            (row["id"],))
                txn.event("library.intent_abandoned", row["id"])
    hub.library.invalidate_cache()
    hub.library.sync_work_tree()
