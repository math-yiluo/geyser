"""Submitting a candidate: check, freeze off the event loop, then register atomically."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from geyser.content.snapshot import NOTE_SPEC, capture, install_staging
from geyser.core import activations, candidates
from geyser.core.actors import Actor
from geyser.core.ledger import Txn
from geyser.core.models import Manifest
from geyser.ids import Kind

if TYPE_CHECKING:
    from geyser.hub import Hub


def workspace_base(txn: Txn, agent_id: str) -> str | None:
    return txn.scalar("SELECT commit_id FROM workspace_bases WHERE agent_id = ?", (agent_id,))


async def submit_candidate(
    hub: Hub,
    actor: Actor,
    manifest: Manifest,
    *,
    fenced: bool,
    finish: Callable[[Txn, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Freeze and register one candidate.

    ``fenced`` rechecks the caller's activation in the final transaction (a direct
    ``stage``); a drained ``contribute`` runs after the turn and is not fenced.
    ``finish`` runs inside the final transaction (for example to store a receipt).
    """
    ctx = hub.ctx
    with hub.read() as txn:
        candidates.check_submission(ctx, txn, actor, manifest)
        base = workspace_base(txn, actor.id) or hub.library.head()
    if manifest.kind == NOTE_SPEC.name:
        spec = NOTE_SPEC
    else:
        spec = hub.pack.contribution_kinds()[manifest.kind]
    workspace = hub.layout.workspace(actor.id)
    frozen = await asyncio.to_thread(capture, hub.layout, hub.library, workspace, manifest,
                                     base, spec)
    try:
        with hub.transaction() as txn:
            if fenced:
                activations.check_actor(txn, actor)
            candidates.check_submission(ctx, txn, actor, manifest)
            candidate_id = txn.next_id(Kind.CANDIDATE)
            install_staging(frozen.directory, hub.layout.staging(candidate_id))
            reply = candidates.register(ctx, txn, actor, candidate_id, manifest,
                                        base_commit=frozen.base or "",
                                        manifest_hash=frozen.manifest_hash)
            reply["files"] = hub.layout.relative(hub.layout.staging(candidate_id))
            reply["changed"] = len(frozen.changed)
            reply["deleted"] = len(frozen.deleted)
            if finish is not None:
                finish(txn, reply)
            return reply
    finally:
        if frozen.directory.exists():
            shutil.rmtree(frozen.directory, ignore_errors=True)
