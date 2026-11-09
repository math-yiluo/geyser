"""Per-agent delivery queue for messages and notices (DESIGN.md section 11.3).

An item is ``pending`` until a turn's input includes it (``prepared``), then
``delivered`` once that input reached the backend. An item whose transport outcome
is unknown becomes ``uncertain`` and is held for reconciliation, never resent
blindly. Items that ``wake`` start a message turn for a waiting agent.
"""

from __future__ import annotations

from collections.abc import Iterable
from sqlite3 import Row

from geyser.core.ledger import Txn

_SUMMARY_LIMIT = 300


def one_line(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def notify(
    txn: Txn,
    agent_id: str,
    summary: str,
    *,
    ref: str | None = None,
    task_id: str | None = None,
    wakes: bool = False,
    item: str = "notice",
) -> int:
    """Queue a one-line notice (or a message reference) for ``agent_id``."""
    cursor = txn.execute(
        "INSERT INTO inbox (agent_id, item, ref, task_id, summary, wakes, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (agent_id, item, ref, task_id, one_line(summary), int(wakes), txn.now),
    )
    return int(cursor.lastrowid or 0)


def notify_many(
    txn: Txn, agent_ids: Iterable[str], summary: str, **options: object
) -> None:
    for agent_id in sorted(set(agent_ids)):
        notify(txn, agent_id, summary, **options)  # type: ignore[arg-type]


def pending(txn: Txn, agent_id: str) -> list[Row]:
    return txn.all(
        "SELECT * FROM inbox WHERE agent_id = ? AND state = 'pending' ORDER BY id", (agent_id,)
    )


def has_waking(txn: Txn, agent_id: str) -> bool:
    return bool(
        txn.scalar(
            "SELECT 1 FROM inbox WHERE agent_id = ? AND state = 'pending' AND wakes = 1 LIMIT 1",
            (agent_id,),
        )
    )


def prepare(txn: Txn, item_ids: Iterable[int], activation_id: str) -> None:
    for item_id in item_ids:
        txn.execute(
            "UPDATE inbox SET state = 'prepared', activation_id = ? "
            "WHERE id = ? AND state = 'pending'",
            (activation_id, item_id),
        )


def mark_delivered(txn: Txn, activation_id: str) -> None:
    txn.execute(
        "UPDATE inbox SET state = 'delivered', delivered_at = ? "
        "WHERE activation_id = ? AND state = 'prepared'",
        (txn.now, activation_id),
    )


def release(txn: Txn, activation_id: str) -> None:
    """Return prepared items to the queue: their input never reached the backend."""
    txn.execute(
        "UPDATE inbox SET state = 'pending', activation_id = NULL "
        "WHERE activation_id = ? AND state = 'prepared'",
        (activation_id,),
    )


def mark_uncertain(txn: Txn, activation_id: str) -> None:
    txn.execute(
        "UPDATE inbox SET state = 'uncertain' WHERE activation_id = ? AND state = 'prepared'",
        (activation_id,),
    )


def reconcile_uncertain(txn: Txn, agent_id: str) -> int:
    """Queue items whose delivery is unknown again, marked as possibly delivered.

    The agent is told explicitly, so this is reconciliation, not a blind resend.
    """
    rows = txn.all("SELECT id, summary FROM inbox WHERE agent_id = ? AND state = 'uncertain'",
                   (agent_id,))
    for row in rows:
        txn.execute(
            "UPDATE inbox SET state = 'pending', activation_id = NULL, summary = ? WHERE id = ?",
            (one_line("Possibly delivered already: " + row["summary"]), row["id"]),
        )
    return len(rows)


def drop_for_agent(txn: Txn, agent_id: str) -> None:
    """Drop everything still queued for a retired agent."""
    txn.execute(
        "UPDATE inbox SET state = 'dropped' WHERE agent_id = ? AND state = 'pending'",
        (agent_id,),
    )
