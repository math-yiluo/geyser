"""Admission passes (DESIGN.md sections 6.5 to 6.7).

Each pass considers ready work in this order:

1. A due integration batch.
2. Reviews whose candidate passed machine validation.
3. Work in flight: continuations, resumptions whose predicate holds, message turns.
4. Open opportunities, higher priority first, then older first.

A pass admits for at most ``scheduler.admission_pass_seconds``. Every item is
handled in its own transaction; an item that raises is skipped and reported once,
and the pass continues (local isolation, section 14).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from sqlite3 import Row
from typing import TYPE_CHECKING

from geyser.core import (
    activations,
    agents,
    candidates,
    choices,
    controls,
    inbox,
    teams,
    waits,
)
from geyser.core.ledger import Txn
from geyser.ids import Kind

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)

ORDINARY = "worker"


class Admission:
    def __init__(self, hub: Hub, start: Callable[[str], None]):
        self.hub = hub
        self.start = start
        """Hands a planned activation to the runtime."""
        self._skipped: set[str] = set()
        self._last_launch: dict[str, float] = {}

    # ------------------------------------------------------------------ the pass
    def run_pass(self) -> int:
        """Plan every activation that is ready now; returns how many were planned."""
        clock = self.hub.clock
        deadline = clock.monotonic() + self.hub.config.scheduler.admission_pass_seconds
        planned = 0
        steps: list[Callable[[], int]] = [
            self._batch_step,
            self._review_step,
            self._resumption_step,
            self._in_flight_step,
            self._message_step,
            self._opportunity_step,
        ]
        for step in steps:
            if clock.monotonic() > deadline:
                break
            planned += self._isolated(step.__name__, step)
        return planned

    def _isolated(self, name: str, step: Callable[[], int]) -> int:
        try:
            return step()
        except Exception:  # noqa: BLE001 - a broken step must not stop admission
            log.exception("admission step %s failed", name)
            return 0

    def _item(self, key: str, action: Callable[[Txn], str | None]) -> int:
        """Run one item in its own transaction; report a failing item once."""
        try:
            with self.hub.transaction() as txn:
                activation_id = action(txn)
        except Exception as error:  # noqa: BLE001
            if key not in self._skipped:
                self._skipped.add(key)
                log.exception("admission item %s skipped", key)
                try:
                    with self.hub.transaction() as txn:
                        txn.event("admission.item_skipped", key, reason=repr(error)[:500])
                except Exception:  # noqa: BLE001
                    pass
            return 0
        if activation_id:
            self._last_launch_note(activation_id)
            self.start(activation_id)
            return 1
        return 0

    def _last_launch_note(self, activation_id: str) -> None:
        with self.hub.read() as txn:
            row = txn.one("SELECT a.backend, a.kind FROM activations v JOIN agents a "
                          "ON a.id = v.agent_id WHERE v.id = ?", (activation_id,))
        if row is not None and row["kind"] == ORDINARY:
            self._last_launch[row["backend"]] = self.hub.clock.monotonic()

    # ------------------------------------------------------------ permissions
    def _may_run(self, txn: Txn, agent: Row) -> bool:
        if agent["state"] not in ("active", "parked"):
            return False
        if activations.live_for(txn, agent["id"]) is not None:
            return False
        if agents.in_backoff(txn, agent):
            return False
        if controls.agent_blocked(txn, agent["id"], agent["backend"], agent["kind"]):
            return False
        return True

    def _session_available(self, txn: Txn, backend: str, kind: str) -> bool:
        """Per-account concurrency; reviewers and the Integrator are admitted at the limit."""
        if kind != ORDINARY:
            return True
        limit = self.hub.config.concurrency_limit(backend)
        if limit is not None:
            running = txn.scalar(
                "SELECT COUNT(*) FROM activations v JOIN agents a ON a.id = v.agent_id "
                "WHERE v.state IN ('planned', 'running', 'draining') AND a.backend = ?",
                (backend,))
            if running >= limit:
                return False
        spacing = self.hub.config.scheduler.launch_seconds
        last = self._last_launch.get(backend)
        if spacing and last is not None and self.hub.clock.monotonic() - last < spacing:
            return False
        return True

    def _new_agents_allowed(self, txn: Txn, kind: Kind) -> bool:
        backend = agents.backend_for_new(self.hub.ctx, txn, kind,
                                         txn.scalar("SELECT next FROM counters WHERE kind = ?",
                                                    (kind.value,)) or 0)
        role = agents.kind_name(kind)
        resolution = controls.resolve(txn, None, backend, role)
        return resolution.new_work and not controls.held(txn, backend)

    # ------------------------------------------------------------------- steps
    def _batch_step(self) -> int:
        if self.hub.config.integration.mode != "batch":
            return 0
        from geyser.integration import batch

        return self._item("batch", functools.partial(self._admit_batch, batch))

    def _admit_batch(self, batch_module, txn: Txn) -> str | None:
        return batch_module.admit_due(self.hub, txn, self._may_run)

    def _review_step(self) -> int:
        planned = 0
        with self.hub.read() as txn:
            waiting = candidates.waiting_reviews(txn)
        for review in waiting:
            planned += self._item(f"review:{review['id']}",
                                  functools.partial(self._dispatch_review, review=review))
        return planned

    def _dispatch_review(self, txn: Txn, *, review: Row) -> str | None:
        if not self._new_agents_allowed(txn, Kind.REVIEWER):
            return None
        current = txn.scalar("SELECT state FROM reviews WHERE id = ?", (review["id"],))
        if current != "waiting":
            return None
        reviewer = agents.create(self.hub.ctx, txn, Kind.REVIEWER)
        candidates.assign_reviewer(self.hub.ctx, txn, review["id"], reviewer)
        row = activations.plan(txn, reviewer, "review", review_id=review["id"],
                               new_conversation=True)
        return row["id"]

    def _resumption_step(self) -> int:
        with self.hub.transaction() as txn:
            agents.settle_moves(txn)
        with self.hub.read() as txn:
            ready = waits.ready_resumptions(self.hub.ctx, txn)
        for found in ready:
            self._item(f"resume:{found.agent_id}:{found.subject}",
                       functools.partial(self._resume, found=found))
        return 0  # Resumption only sets the intent; the next step plans the turn.

    def _resume(self, txn: Txn, *, found: waits.Resumption) -> None:
        # Evaluated again inside this transaction: the wait may have ended meanwhile.
        current = waits.resumption_for(self.hub.ctx, txn, found.agent_id, found.subject)
        if current is not None:
            waits.resume_membership(self.hub.ctx, txn, current.agent_id, current.subject,
                                    current.why, current.notice)

    def _in_flight_step(self) -> int:
        planned = 0
        with self.hub.read() as txn:
            rows = txn.all("SELECT * FROM agents WHERE state = 'active' AND intent IN "
                           "('start', 'continue', 'resume') ORDER BY kind DESC, number")
        for agent in rows:
            planned += self._item(f"turn:{agent['id']}",
                                  functools.partial(self._plan_turn, agent_id=agent["id"]))
        return planned

    def _plan_turn(self, txn: Txn, *, agent_id: str) -> str | None:
        agent = agents.agent_row(txn, agent_id)
        if agent["intent"] not in ("start", "continue", "resume") or not self._may_run(txn, agent):
            return None
        if not self._session_available(txn, agent["backend"], agent["kind"]):
            return None
        if agent["kind"] == "worker" and agent["current_task"]:
            state = txn.scalar("SELECT state FROM memberships WHERE task_id = ? AND "
                               "agent_id = ?", (agent["current_task"], agent_id))
            if state != "active":
                agents.set_intent(txn, agent_id, "none")
                return None
            row = activations.plan(txn, agent_id, "task", task_id=agent["current_task"],
                                   new_conversation=agent["intent"] == "start")
        elif agent["kind"] == "reviewer" and agent["current_review"]:
            if agent["review_wait"] is not None:
                return None
            row = activations.plan(txn, agent_id, "review", review_id=agent["current_review"])
        elif agent["kind"] == "integrator" and agent["current_batch"]:
            row = activations.plan(txn, agent_id, "integration", batch_id=agent["current_batch"])
        else:
            agents.set_intent(txn, agent_id, "none")
            return None
        return row["id"]

    def _message_step(self) -> int:
        planned = 0
        self._isolated("reconcile", self._reconcile_uncertain)
        with self.hub.read() as txn:
            rows = txn.all("SELECT DISTINCT a.* FROM agents a JOIN inbox i ON i.agent_id = a.id "
                           "WHERE i.state = 'pending' AND i.wakes = 1 AND a.state IN "
                           "('active', 'parked')")
        for agent in rows:
            planned += self._item(f"message:{agent['id']}",
                                  functools.partial(self._plan_message_turn,
                                                    agent_id=agent["id"]))
        return planned

    def _reconcile_uncertain(self) -> int:
        """Items with an unknown delivery are queued again, marked as possibly delivered,
        for agents with no live turn (so a waiting agent learns of them)."""
        with self.hub.transaction() as txn:
            rows = txn.all("SELECT DISTINCT agent_id FROM inbox WHERE state = 'uncertain'")
            for row in rows:
                if activations.live_for(txn, row["agent_id"]) is None:
                    inbox.reconcile_uncertain(txn, row["agent_id"])
        return 0

    def _plan_message_turn(self, txn: Txn, *, agent_id: str) -> str | None:
        agent = agents.agent_row(txn, agent_id)
        if not inbox.has_waking(txn, agent_id) or not self._may_run(txn, agent):
            return None
        if agent["intent"] in ("start", "continue", "resume"):
            return None  # Its next ordinary turn delivers the items.
        if agent["state"] == "parked" or (agent["kind"] == "integrator"
                                          and not agent["current_batch"]):
            # A question about a finished review starts a new conversation from its report.
            review_id = txn.scalar("SELECT id FROM reviews WHERE reviewer = ? ORDER BY "
                                   "decided_at DESC LIMIT 1", (agent_id,))
            row = activations.plan(txn, agent_id, "answer", review_id=review_id,
                                   new_conversation=True)
            return row["id"]
        if agent["kind"] == ORDINARY and not self._session_available(txn, agent["backend"],
                                                                     agent["kind"]):
            return None  # Message turns of ordinary workers count toward the limit.
        task_id = agent["current_task"]
        waiting_task = task_id and txn.scalar(
            "SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?",
            (task_id, agent_id)) == "waiting"
        waiting_review = agent["kind"] == "reviewer" and agent["review_wait"] is not None
        if waiting_task or waiting_review:
            row = activations.plan(txn, agent_id, "message", task_id=task_id if waiting_task
                                   else None, review_id=agent["current_review"])
            return row["id"]
        if agent["kind"] == "worker" and task_id is None:
            # An idle worker's Hub notices (a completed task, a left teammate) wake nobody;
            # a question, an answer or an operator message gives it one message turn.
            txn.execute("UPDATE inbox SET wakes = 0 WHERE agent_id = ? AND state = 'pending' "
                        "AND item != 'message'", (agent_id,))
            if inbox.has_waking(txn, agent_id):
                row = activations.plan(txn, agent_id, "message", new_conversation=True)
                return row["id"]
        return None

    def _opportunity_step(self) -> int:
        planned = 0
        with self.hub.read() as txn:
            places = teams.open_opportunities(txn)
            pending_decision = {row["opportunity_id"] for row in txn.all(
                "SELECT opportunity_id FROM decisions WHERE purpose = 'assignment' AND state "
                "IN ('pending', 'running', 'unknown')")}
        for place in places:
            if place["id"] in pending_decision:
                continue
            planned += self._item(f"place:{place['id']}",
                                  functools.partial(self._fill_place, place=place))
        return planned

    def _fill_place(self, txn: Txn, *, place: Row) -> str | None:
        """No idle worker: a new worker. Otherwise one decision chooses (section 6.5)."""
        if not self._new_agents_allowed(txn, Kind.WORKER):
            return None
        if txn.scalar("SELECT state FROM opportunities WHERE id = ?", (place["id"],)) != "open":
            return None
        idle = agents.idle_workers(txn)
        decision_backend = choices.role_choice(self.hub.ctx, txn, "dispatcher").backend
        if idle and not controls.held(txn, decision_backend):
            decision_id = txn.next_id(Kind.DECISION)
            txn.execute(
                "INSERT INTO decisions (id, purpose, task_id, opportunity_id, task_revision, "
                "state, created_at) VALUES (?, 'assignment', ?, ?, ?, 'pending', ?)",
                (decision_id, place["task_id"], place["id"], place["task_revision"], txn.now),
            )
            txn.after_commit(self.hub.ctx.wake)
            return None
        worker = agents.create(self.hub.ctx, txn, Kind.WORKER)
        teams.fill(self.hub.ctx, txn, place["id"], worker, by="dispatcher")
        return None  # The in-flight step plans its first turn.
