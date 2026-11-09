"""Recovery after a restart, and the periodic retry of unsettled drains (section 14).

- A planned activation that never launched is released at once.
- A running activation whose scope is quiet is recorded ``closed_unknown``: that
  ends execution ownership without claiming success. Its conversation continues
  with an input saying what happened. The old turn is never replayed.
- A tree whose owner died is terminated first; only an empty scope is recovered.
- A draining activation commits what the Hub accepted once its scope is quiet.
- Running validations and decisions return to pending; publication intents are
  finished or abandoned exactly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from geyser.core import activations, inbox
from geyser.host.base import ProcessHost, ScopeRef
from geyser.integration import publication
from geyser.runtime.runner import commit_drain

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)

TERMINATE_GRACE_SECONDS = 30.0


async def recover_on_start(hub: Hub, host: ProcessHost) -> None:
    publication.recover(hub)
    with hub.transaction() as txn:
        txn.execute("UPDATE validations SET phase = 'pending' WHERE phase = 'running'")
        txn.execute("UPDATE decisions SET state = 'pending' WHERE state = 'running'")
    with hub.read() as txn:
        live = activations.all_live(txn)
    for row in live:
        try:
            await recover_activation(hub, host, row["id"])
        except Exception:  # noqa: BLE001 - one unrecoverable activation stays quarantined
            log.exception("could not recover %s", row["id"])


async def recover_activation(hub: Hub, host: ProcessHost, activation_id: str) -> None:
    with hub.read() as txn:
        row = activations.get(txn, activation_id)
    if row is None or row["state"] == "ended":
        return
    if row["state"] == "planned":
        with hub.transaction() as txn:
            activations.end(txn, activation_id, "abandoned", {"reason": "never launched"})
        return
    scope = ScopeRef(row["scope"]) if row["scope"] else None
    if scope is not None:
        quiet = await host.quiet(scope)
        if quiet is not True:
            await host.terminate(scope, TERMINATE_GRACE_SECONDS)
            quiet = await host.quiet(scope)
        if quiet is not True:
            log.warning("scope of %s is still not quiet; retrying later", activation_id)
            return
    if row["state"] == "draining":
        await commit_drain(hub, activation_id)
        return
    with hub.transaction() as txn:
        activations.end(txn, activation_id, "closed_unknown",
                        {"reason": "the Hub restarted during this turn"})
        agent = txn.one("SELECT current_task FROM agents WHERE id = ?", (row["agent_id"],))
        inbox.notify(txn, row["agent_id"],
                     "Your previous turn was cut off by a restart. Inspect your history and "
                     "files to see what happened before continuing; nothing was replayed.",
                     task_id=agent["current_task"] if agent else None)


async def retry_unsettled_drains(hub: Hub, host: ProcessHost, running: set[str]) -> int:
    """Commit drains whose turn ended but whose scope was not yet quiet."""
    with hub.read() as txn:
        rows = txn.all("SELECT id FROM activations WHERE state = 'draining'")
    count = 0
    for row in rows:
        if row["id"] in running:
            continue
        await recover_activation(hub, host, row["id"])
        count += 1
    return count
