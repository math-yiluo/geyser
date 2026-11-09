"""Wake decisions for subscribed waits: the ledger side (DESIGN.md section 12).

A worker that subscribed to another task waits until the Waker judges that what the
task published meets the need the worker described. This module finds the (wait,
task) pairs with publications not yet judged, builds what the Waker sees, and applies
its answers. The model call itself is the decision runner's.

Each pair is judged on the subscribed task's candidates published (Wiki notes aside)
since the wait began or since the Waker last looked at that pair, whichever is later;
``subscription_marks`` remembers the latter, per wait.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from geyser.core import tasks, waits
from geyser.core.context import HubContext
from geyser.core.ledger import Txn, loads
from geyser.core.models import Predicate

MAX_PAIRS = 40
"""Pairs in one decision; the rest wait for the next."""
MAX_ATTEMPTS = 5
"""Asks of one decision before every pair it covers wakes."""
MAX_PUBLICATIONS = 20
"""Publications shown per pair, newest last."""


@dataclass
class Pair:
    agent_id: str
    task_id: str
    """The waiter's task."""
    provider: str
    """The subscribed task."""
    need: str
    since: str
    through: str
    """The newest publication time this pair covers."""
    publications: list[dict[str, Any]] = field(default_factory=list)


def pending_pairs(ctx: HubContext, txn: Txn) -> list[Pair]:
    """Subscribed waits whose task published something not yet judged for them."""
    pairs = []
    for row in txn.all(
        "SELECT m.task_id, m.agent_id, m.predicate, m.waiting_since FROM memberships m "
        "JOIN agents a ON a.id = m.agent_id WHERE m.state = 'waiting' "
        "AND m.predicate LIKE '%\"subscriptions\":[{%' AND a.state = 'active' "
        "AND a.current_task = m.task_id ORDER BY m.waiting_since, m.agent_id"
    ):
        try:
            predicate = Predicate.model_validate(loads(row["predicate"]))
        except ValueError:
            continue  # Reported by the resumption step, which evaluates every wait.
        for subscription in predicate.subscriptions:
            pair = _pair(ctx, txn, row, subscription.task_id, subscription.need)
            if pair is not None:
                pairs.append(pair)
            if len(pairs) >= MAX_PAIRS:
                return pairs
    return pairs


def _pair(ctx: HubContext, txn: Txn, row: Any, provider: str, need: str) -> Pair | None:
    since = row["waiting_since"]
    mark = txn.scalar("SELECT through FROM subscription_marks WHERE agent_id = ? AND "
                      "task_id = ? AND provider = ? AND since = ?",
                      (row["agent_id"], row["task_id"], provider, since))
    published = txn.all(
        "SELECT c.id, c.kind, c.paths, p.committed_at FROM candidates c "
        "JOIN publications p ON p.id = c.publication_id "
        "WHERE c.task_id = ? AND c.state = 'published' AND c.kind != 'note' "
        "AND p.committed_at >= ? AND (? IS NULL OR p.committed_at > ?) "
        "ORDER BY p.committed_at, c.id",
        (provider, since, mark, mark),
    )
    if not published:
        return None
    shown = published[-MAX_PUBLICATIONS:]
    return Pair(row["agent_id"], row["task_id"], provider, need, since,
                through=published[-1]["committed_at"],
                publications=[_publication(candidate) for candidate in shown])


def _publication(row: Any) -> dict[str, Any]:
    paths = loads(row["paths"], [])
    return {"candidate": row["id"], "kind": row["kind"], "published_at": row["committed_at"],
            "introduction": "", "paths": paths[:20], "more_paths": max(0, len(paths) - 20)}


def introduce(ctx: HubContext, pairs: list[Pair]) -> None:
    """Fill in each publication's introduction from its manifest.

    This reads files, so callers run it outside any ledger transaction (section 4.3).
    A manifest that cannot be read leaves the introduction empty.
    """
    for pair in pairs:
        for publication in pair.publications:
            candidate = publication["candidate"]
            for folder in (ctx.layout.archive(candidate), ctx.layout.staging(candidate)):
                try:
                    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                submission = manifest.get("submission", {}) if isinstance(manifest, dict) \
                    else {}
                publication["introduction"] = str(submission.get("introduction", ""))[:1500]
                break


def packet(txn: Txn, pairs: list[Pair]) -> list[dict[str, Any]]:
    """What the Waker sees, one entry per pair, keyed ``p0``, ``p1``, …"""
    return [{"pair": f"p{index}", "need": pair.need,
             "subscribed_task": tasks.title_of(txn, pair.provider),
             "published_since_the_wait_began": pair.publications}
            for index, pair in enumerate(pairs)]


def apply(ctx: HubContext, txn: Txn, pairs: list[Pair], answers: dict[str, bool]) -> list[str]:
    """Record what was judged; wake the waits judged met. Returns the agents woken.

    A pair whose wait ended or changed meanwhile is skipped: its answer was about a
    wait that no longer exists.
    """
    woken = []
    for index, pair in enumerate(pairs):
        current = txn.one("SELECT m.state, m.waiting_since, a.current_task FROM memberships m "
                          "JOIN agents a ON a.id = m.agent_id WHERE m.task_id = ? AND "
                          "m.agent_id = ?", (pair.task_id, pair.agent_id))
        if (current is None or current["state"] != "waiting"
                or current["waiting_since"] != pair.since
                or current["current_task"] != pair.task_id):
            continue
        txn.execute(
            "INSERT INTO subscription_marks (agent_id, task_id, provider, since, through) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (agent_id, task_id, provider, since) "
            "DO UPDATE SET through = excluded.through",
            (pair.agent_id, pair.task_id, pair.provider, pair.since, pair.through))
        if answers.get(f"p{index}") is True and pair.agent_id not in woken:
            waits.resume_membership(
                ctx, txn, pair.agent_id, pair.task_id, "subscribed_task_changed",
                f"{tasks.title_of(txn, pair.provider)}, which you subscribed to, published "
                f"something that may meet your need ({_clip(pair.need)}). Check the Library "
                "before relying on it.")
            woken.append(pair.agent_id)
    txn.event("wake.decided", None, "waker", pairs=len(pairs), woken=woken)
    return woken


def _clip(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def drop_stale_marks(txn: Txn) -> None:
    """Forget the marks of waits that ended."""
    txn.execute(
        "DELETE FROM subscription_marks WHERE NOT EXISTS (SELECT 1 FROM memberships m "
        "WHERE m.task_id = subscription_marks.task_id AND m.agent_id = "
        "subscription_marks.agent_id AND m.state = 'waiting' AND m.waiting_since = "
        "subscription_marks.since)")
