"""Direct integration: a serialized, mechanical publisher inside the Hub (section 9.2).

Accepted candidates are taken in acceptance order. Each is merged from its base onto
the current head with a three-way merge. A conflict returns the candidate to its
task (``returned``); the mechanical rule never guesses. A clean merge runs the
policy's final check on the merged tree, then publishes with an expected-head update.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from typing import TYPE_CHECKING

from geyser.content.snapshot import staged_changes
from geyser.content.staging import candidate_folder, candidate_view, write_report
from geyser.core import candidates
from geyser.core.models import ValidationReport, Verdict
from geyser.domains.base import TreeSnapshot, ValidationContext
from geyser.integration import publication

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)


class Backoff:
    """Per-candidate retry delays, so one failing candidate never blocks the others."""

    DELAYS_MINUTES = (1, 2, 4, 8, 16, 32, 60)

    def __init__(self, clock) -> None:
        self.clock = clock
        self._failures: dict[str, tuple[int, float]] = {}

    def ready(self, candidate_id: str) -> bool:
        entry = self._failures.get(candidate_id)
        return entry is None or self.clock.monotonic() >= entry[1]

    def failed(self, candidate_id: str) -> int:
        count = self._failures.get(candidate_id, (0, 0.0))[0] + 1
        delay = self.DELAYS_MINUTES[min(count, len(self.DELAYS_MINUTES)) - 1] * 60
        self._failures[candidate_id] = (count, self.clock.monotonic() + delay)
        return count

    def succeeded(self, candidate_id: str) -> None:
        self._failures.pop(candidate_id, None)


async def integrate_next(hub: Hub, backoff: Backoff | None = None) -> str | None:
    """Integrate the oldest ready accepted candidate; returns its ID, or None.

    A candidate whose integration fails (a Git error, missing staged files, an
    unavailable final check) backs off on its own; the next candidate is tried.
    """
    backoff = backoff or Backoff(hub.clock)
    with hub.read() as txn:
        queue = candidates.accepted_for_integration(hub.ctx, txn)
    for row in queue:
        if not backoff.ready(row["id"]):
            continue
        try:
            await asyncio.to_thread(_integrate_files_ready, hub, row["id"])
            result = await _integrate(hub, row)
        except Exception:  # noqa: BLE001 - one candidate's failure waits for its retry
            count = backoff.failed(row["id"])
            log.exception("integrating %s failed (attempt %d)", row["id"], count)
            continue
        backoff.succeeded(row["id"])
        return result
    return None


def _integrate_files_ready(hub: Hub, candidate_id: str) -> None:
    folder = candidate_folder(hub, candidate_id)
    if not (folder / "manifest.json").exists():
        raise FileNotFoundError(f"the staged files of {candidate_id} are missing")


async def _integrate(hub: Hub, row) -> str:
    repo = hub.library.repo
    candidate_id = row["id"]
    changed, deleted = staged_changes(candidate_folder(hub, candidate_id))
    base = row["base_commit"] or None
    head = hub.library.head()
    candidate_tree = await asyncio.to_thread(repo.build_tree, base, changed, deleted)
    merged: str | None
    if base == head:
        merged = candidate_tree
    else:
        candidate_commit = await asyncio.to_thread(
            repo.commit_tree, candidate_tree, [base] if base else [],
            f"Candidate {candidate_id}\n")
        if base is None:
            base_for_merge = await asyncio.to_thread(
                repo.commit_tree, await asyncio.to_thread(repo.build_tree, None, {}), [],
                "Empty base\n")
        else:
            base_for_merge = base
        result = await asyncio.to_thread(repo.merge, base_for_merge, head or base_for_merge,
                                         candidate_commit)
        if not result.clean:
            with hub.transaction() as txn:
                candidates.mark_returned(hub.ctx, txn, candidate_id, "merge conflict",
                                         result.conflicts)
            return candidate_id
        merged = result.tree
    assert merged is not None
    report = await _final_check(hub, row, merged)
    if report.verdict is Verdict.UNAVAILABLE:
        log.warning("final check of %s unavailable: %s", candidate_id, report.summary)
        raise RuntimeError(f"final check unavailable: {report.summary}")
    if not report.unqualified_acceptance:
        path = write_report(hub, candidate_id, "final-check", report.model_dump(mode="json"))
        with hub.transaction() as txn:
            candidates.mark_returned(hub.ctx, txn, candidate_id,
                                     f"final check failed: {report.summary[:300]} ({path})", [])
        return candidate_id
    if head is not None and repo.tree_of(head) == merged:
        log.info("%s changes nothing on the current head; publishing it as a no-op", candidate_id)
    await asyncio.to_thread(publication.publish, hub, "direct", [candidate_id], merged, head,
                            f"Publish {candidate_id} ({row['task_id']})")
    return candidate_id


async def _final_check(hub: Hub, row, tree_id: str) -> ValidationReport:
    spec = hub.pack.policies().get(row["policy"])
    if spec is None or spec.final_check is None:
        return ValidationReport(verdict=Verdict.ACCEPTED, summary="No final check.")
    scratch = hub.layout.scratch / f"final-{row['id']}-{uuid.uuid4().hex[:8]}"
    tree_dir = scratch / "tree"
    try:
        await asyncio.to_thread(hub.library.repo.materialize, tree_id, tree_dir, ())
        context = ValidationContext(hub.layout, hub.config, scratch)
        with candidate_view(hub, row) as view:
            try:
                return await hub.pack.check_tree(context, TreeSnapshot(tree_dir, tree_id), [view])
            except Exception as error:  # noqa: BLE001 - a pack bug is an unavailable check
                log.exception("final check of %s raised", row["id"])
                return ValidationReport(verdict=Verdict.UNAVAILABLE,
                                        summary=f"The final check raised {error!r}.")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
