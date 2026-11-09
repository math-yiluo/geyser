"""Tool-less decisions: assignment, recruitment and wakes (DESIGN.md sections 6.3, 6.5, 12).

A decision runs in a new conversation over an empty read-only directory, with no
tools and a minimal self-contained prompt. It answers one schema. Assignment and
recruitment decisions belong to the Dispatcher and run on its choice of backend and
model; wake decisions belong to the Waker and run on its own.

- A decision whose task changed before it ran ends without a model call. One
  assignment decision runs at a time, so each sees the workers still idle. Two failed
  decisions give the task a new worker (assignment) or no collaborator (recruitment).
- One wake decision runs at a time and covers every subscribed wait with
  publications not yet judged, read when it runs. Five failed asks wake every wait it
  covered, and the operator is told.
"""

from __future__ import annotations

import asyncio
import json
import logging
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from geyser.core import agents, choices, controls, tasks, teams, wakes
from geyser.core.ledger import Txn, dumps
from geyser.host.base import ProcessHost
from geyser.ids import Kind
from geyser.runtime.backends.base import Backend, DecisionRequest, TurnStatus

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)

MAX_FAILURES = 2
"""For assignment and recruitment decisions; wake decisions use ``wakes.MAX_ATTEMPTS``."""
ROLE_OF = {"assignment": "dispatcher", "recruitment": "dispatcher", "wake": "waker"}

ASSIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {"worker": {"type": ["string", "null"]}},
    "required": ["worker"],
    "additionalProperties": False,
}
RECRUITMENT_SCHEMA = {
    "type": "object",
    "properties": {"collaborator": {"type": "boolean"}},
    "required": ["collaborator"],
    "additionalProperties": False,
}


def assignment_prompt(task: dict[str, Any], workers: dict[str, list[str]]) -> str:
    lines = [
        "You assign one open task to the best-suited idle worker, or to none.",
        "Choose a worker only if the titles of the tasks it executed show clearly related "
        "work; its workspace and notes would then help. Otherwise answer null, and a new "
        "worker takes the task.",
        "",
        "Task:",
        json.dumps(task, ensure_ascii=False, indent=2),
        "",
        "Idle workers and the tasks each executed:",
        json.dumps(workers, ensure_ascii=False, indent=2),
        "",
        'Answer with JSON only: {"worker": "<one of the names above>"} or {"worker": null}.',
    ]
    return "\n".join(lines)


def recruitment_prompt(task: dict[str, Any], handoff: str) -> str:
    return "\n".join([
        "Decide whether one task needs a collaborator in addition to its executor.",
        "Say yes only if the task has a substantial, separable part that a second agent can "
        "do in parallel without waiting for the executor. Coordination has a cost.",
        "",
        "Task:",
        json.dumps(task, ensure_ascii=False, indent=2),
        "",
        "The executor's handoff file after its first turn:",
        handoff[:6000] if handoff.strip() else "(empty)",
        "",
        'Answer with JSON only: {"collaborator": true} or {"collaborator": false}.',
    ])


def wake_schema(count: int) -> dict[str, Any]:
    keys = [f"p{index}" for index in range(count)]
    return {"type": "object", "properties": {key: {"type": "boolean"} for key in keys},
            "required": keys, "additionalProperties": False}


def wake_prompt(packet: list[dict[str, Any]]) -> str:
    return "\n".join([
        "You judge whether waiting agents can go on. Each pair below is one agent's wait "
        "on a task it subscribed to: `need` is what the agent said it waits for, in its "
        "own words, and the list is what that task published to the Library since the "
        "wait began.",
        "Answer true if what was published plausibly meets the need: the agent then wakes "
        "and checks the Library itself. Answer false if it clearly does not. A wrong true "
        "costs the agent one turn; a wrong false keeps it waiting for the next publication "
        "or the task's completion.",
        "",
        json.dumps(packet, ensure_ascii=False, indent=2),
        "",
        "Answer with JSON only, one key per pair: "
        + json.dumps({entry["pair"]: "true or false" for entry in packet}) + ".",
    ])


class DecisionRunner:
    def __init__(self, hub: Hub, backends: dict[str, Backend], host: ProcessHost):
        self.hub = hub
        self.backends = backends
        self.host = host

    async def run_pending(self) -> int:
        """Run one wake decision, pending recruitment decisions and one assignment."""
        count = await self._wake_step()
        with self.hub.read() as txn:
            recruitment = txn.all("SELECT * FROM decisions WHERE purpose = 'recruitment' AND "
                                  "state IN ('pending', 'unknown') ORDER BY created_at")
            assignment = txn.one("SELECT * FROM decisions WHERE purpose = 'assignment' AND "
                                 "state IN ('pending', 'unknown') ORDER BY created_at LIMIT 1")
        if not self._new_work_allowed():
            return count  # Paused, stopped, held or after Task 0: decisions stay pending.
        for row in recruitment:
            await self._guarded(self._recruitment, row)
            count += 1
        if assignment is not None:
            await self._guarded(self._assignment, assignment)
            count += 1
        return count

    def _new_work_allowed(self, txn: Txn | None = None) -> bool:
        """Whether run controls and holds allow new task places to be filled now."""
        if txn is None:
            with self.hub.read() as reading:
                return self._new_work_allowed(reading)
        number = txn.scalar("SELECT next FROM counters WHERE kind = 'worker'") or 0
        backend = agents.backend_for_new(self.hub.ctx, txn, Kind.WORKER, number)
        resolution = controls.resolve(txn, None, backend, "worker")
        return resolution.new_work and not controls.held(txn, backend)

    async def _guarded(self, function, row: Row) -> None:
        try:
            await function(row)
        except Exception:  # noqa: BLE001 - a decision bug counts as one failed decision
            log.exception("decision %s raised", row["id"])
            try:
                with self.hub.transaction() as txn:
                    # Count the failure even if it happened before the model was asked.
                    txn.execute("UPDATE decisions SET attempts = MAX(attempts, ?) WHERE id = ?",
                                (self._attempts(txn, row["id"]) + 1, row["id"]))
                    self._failed(txn, row["id"], "internal error")
            except Exception:  # noqa: BLE001 - never leave a decision running forever
                log.exception("recording the failure of decision %s raised", row["id"])
                with self.hub.transaction() as txn:
                    txn.execute("UPDATE decisions SET state = 'obsolete', finished_at = ? "
                                "WHERE id = ?", (txn.now, row["id"]))

    @staticmethod
    def _attempts(txn: Txn, decision_id: str) -> int:
        return int(txn.scalar("SELECT attempts FROM decisions WHERE id = ?",
                              (decision_id,)) or 0)

    # -------------------------------------------------------------- assignment
    async def _assignment(self, row: Row) -> None:
        hub = self.hub
        with hub.read() as txn:
            place = teams.opportunity(txn, row["opportunity_id"])
            task = tasks.task_row(txn, row["task_id"])
            if place is None or place["state"] != "open" or task["revision"] != row[
                    "task_revision"] or task["state"] not in ("open", "active", "waiting"):
                self._finish_obsolete(row["id"])
                return
            idle = agents.idle_workers(txn)
            offered = {worker["id"]: agents.executed_titles(txn, worker["id"]) for worker in idle}
            contract = tasks.load_contract(hub.ctx, txn, row["task_id"])
            held = controls.held(txn, choices.role_choice(hub.ctx, txn, "dispatcher").backend)
        if not offered or held:
            self._new_worker(row)
            return
        summary = {"task": f"{task['id']} ({task['title']})", "kind": task["kind"],
                   "objective_excerpt": contract.objective[:800],
                   "invitation": place["invitation"]}
        answer = await self._ask(row, assignment_prompt(summary, offered), ASSIGNMENT_SCHEMA)
        if answer is None:
            return
        chosen = answer.get("worker")
        with hub.transaction() as txn:
            txn.execute("UPDATE decisions SET state = 'done', answer = ?, offered = ?, "
                        "finished_at = ? WHERE id = ?",
                        (dumps(answer), dumps(sorted(offered)), txn.now, row["id"]))
            if not self._new_work_allowed(txn):
                txn.execute("UPDATE decisions SET state = 'pending' WHERE id = ?", (row["id"],))
            elif isinstance(chosen, str) and chosen in offered and teams.can_fill(
                    txn, row["opportunity_id"], chosen) is None and agents.is_idle(
                    txn, agents.agent_row(txn, chosen)):
                teams.fill(hub.ctx, txn, row["opportunity_id"], chosen, by="decision")
            else:
                self._fill_with_new_worker(txn, row)

    def _new_worker(self, row: Row) -> None:
        with self.hub.transaction() as txn:
            txn.execute("UPDATE decisions SET state = 'done', answer = ?, finished_at = ? "
                        "WHERE id = ?", (dumps({"worker": None, "reason": "no model call"}),
                                         txn.now, row["id"]))
            self._fill_with_new_worker(txn, row)

    def _fill_with_new_worker(self, txn: Txn, row: Row) -> None:
        place = teams.opportunity(txn, row["opportunity_id"])
        task_state = txn.scalar("SELECT state FROM tasks WHERE id = ?", (row["task_id"],))
        if (place is None or place["state"] != "open"
                or task_state not in ("open", "active", "waiting")):
            return  # The place is gone or the task cannot take a worker; nothing to fill.
        if not self._new_work_allowed(txn):
            txn.execute("UPDATE decisions SET state = 'pending', answer = NULL WHERE id = ?",
                        (row["id"],))
            return
        worker = agents.create(self.hub.ctx, txn, Kind.WORKER)
        teams.fill(self.hub.ctx, txn, row["opportunity_id"], worker, by="dispatcher")

    # ------------------------------------------------------------- recruitment
    async def _recruitment(self, row: Row) -> None:
        hub = self.hub
        with hub.read() as txn:
            task = tasks.task_row(txn, row["task_id"])
            if task["recruitment"] != "undecided" or task["state"] not in ("active", "waiting"):
                self._finish_obsolete(row["id"])
                return
            executor = txn.scalar("SELECT agent_id FROM memberships WHERE task_id = ? AND "
                                  "role = 'executor' ORDER BY joined_at LIMIT 1", (task["id"],))
            contract = tasks.load_contract(hub.ctx, txn, task["id"])
        handoff_text = ""
        if executor:
            path = hub.layout.handoff_file(executor, task["id"])
            if path.exists():
                handoff_text = path.read_text(encoding="utf-8", errors="replace")
        summary = {"task": f"{task['id']} ({task['title']})", "kind": task["kind"],
                   "objective": contract.objective[:2000],
                   "acceptance": contract.acceptance[:20]}
        answer = await self._ask(row, recruitment_prompt(summary, handoff_text),
                                 RECRUITMENT_SCHEMA)
        if answer is None:
            return
        with hub.transaction() as txn:
            txn.execute("UPDATE decisions SET state = 'done', answer = ?, finished_at = ? "
                        "WHERE id = ?", (dumps(answer), txn.now, row["id"]))
            teams.record_recruitment(hub.ctx, txn, task["id"], bool(answer.get("collaborator")))

    # ------------------------------------------------------------------ wakes
    def _wakes_allowed(self, txn: Txn) -> bool:
        """Wake decisions run while the run runs and the Waker's account is not held."""
        backend = choices.role_choice(self.hub.ctx, txn, "waker").backend
        return controls.run_state(txn) == controls.RUNNING and not controls.held(txn, backend)

    async def _wake_step(self) -> int:
        """Run one wake decision if any subscribed wait has publications to judge."""
        hub = self.hub
        with hub.transaction() as txn:
            if not self._wakes_allowed(txn):
                return 0
            wakes.drop_stale_marks(txn)
            row = txn.one("SELECT * FROM decisions WHERE purpose = 'wake' AND state IN "
                          "('pending', 'unknown') ORDER BY created_at LIMIT 1")
            if row is None:
                if not wakes.pending_pairs(hub.ctx, txn):
                    return 0
                decision_id = txn.next_id(Kind.DECISION)
                txn.execute("INSERT INTO decisions (id, purpose, task_id, task_revision, state, "
                            "created_at) VALUES (?, 'wake', '', 0, 'pending', ?)",
                            (decision_id, txn.now))
                row = txn.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        assert row is not None
        await self._guarded(self._wake, row)
        return 1

    async def _wake(self, row: Row) -> None:
        hub = self.hub
        with hub.read() as txn:
            pairs = wakes.pending_pairs(hub.ctx, txn)
        if not pairs:
            self._finish_obsolete(row["id"])
            return
        await asyncio.to_thread(wakes.introduce, hub.ctx, pairs)  # Files, off the ledger.
        with hub.read() as txn:
            packet = wakes.packet(txn, pairs)
        answer = await self._ask(row, wake_prompt(packet), wake_schema(len(pairs)))
        if answer is None:
            return
        if not all(isinstance(answer.get(entry["pair"]), bool) for entry in packet):
            with hub.transaction() as txn:
                self._failed(txn, row["id"], "an answer was not true or false")
            return
        with hub.transaction() as txn:
            txn.execute("UPDATE decisions SET state = 'done', answer = ?, offered = ?, "
                        "finished_at = ? WHERE id = ?",
                        (dumps(answer), dumps([vars(pair) for pair in pairs]), txn.now,
                         row["id"]))
            wakes.apply(hub.ctx, txn, pairs, answer)
            txn.after_commit(hub.ctx.wake)

    def _wake_everyone(self, txn: Txn, decision_id: str) -> None:
        """After the last failed ask: wake every pair rather than let a wait never end."""
        pairs = wakes.pending_pairs(self.hub.ctx, txn)
        woken = wakes.apply(self.hub.ctx, txn, pairs, {f"p{index}": True
                                                       for index in range(len(pairs))})
        message = (f"The Waker could not answer wake decision {decision_id} after "
                   f"{wakes.MAX_ATTEMPTS} attempts, so the waits it covered were ended: "
                   f"{', '.join(woken) or 'none still waiting'}. Check the Waker's model "
                   "choice and account.")
        txn.execute(
            "INSERT INTO operator_notices (signature, subject, message, count, first_at, "
            "last_at) VALUES ('waker.failed', 'waker', ?, 1, ?, ?) ON CONFLICT (signature) "
            "DO UPDATE SET message = excluded.message, count = count + 1, "
            "last_at = excluded.last_at", (message, txn.now, txn.now))
        txn.after_commit(self.hub.ctx.wake)

    # ----------------------------------------------------------------- asking
    async def _ask(self, row: Row, prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        """Ask the decision's role; returns the answer, or None after recording a failure."""
        hub = self.hub
        with hub.read() as txn:
            settings = choices.role_choice(hub.ctx, txn, ROLE_OF[row["purpose"]])
        backend = self.backends.get(settings.backend)
        if backend is None:
            with hub.transaction() as txn:
                self._failed(txn, row["id"], f"decision backend {settings.backend!r} missing")
            return None
        directory = hub.layout.scratch / "decision-empty"
        directory.mkdir(parents=True, exist_ok=True)
        with hub.transaction() as txn:
            txn.execute("UPDATE decisions SET state = 'running', attempts = attempts + 1 "
                        "WHERE id = ?", (row["id"],))
        request = DecisionRequest(prompt=prompt, schema=schema, directory=directory,
                                  scope=self.host.plan_scope(row["id"]), host=self.host,
                                  model=settings.model, effort=settings.effort)
        outcome = await backend.decide(request)
        with hub.transaction() as txn:
            if outcome.account_hold:
                controls.hold(txn, settings.backend, outcome.account_hold)
            waker_held = (row["purpose"] == "wake" and bool(outcome.account_hold)
                          and outcome.status is not TurnStatus.COMPLETED)
            if outcome.status is TurnStatus.INTERRUPTED or waker_held:
                # A pause is not a failure, nor is a hold of the Waker's account: the wake
                # decision waits until the operator resumes the account.
                txn.execute("UPDATE decisions SET state = 'pending', attempts = attempts - 1 "
                            "WHERE id = ?", (row["id"],))
                return None
            answer = outcome.answer
            valid = isinstance(answer, dict) and all(key in answer for key in schema["required"])
            if outcome.status is not TurnStatus.COMPLETED or not valid:
                self._failed(txn, row["id"], outcome.detail or "invalid answer")
                return None
        return answer

    def _failed(self, txn: Txn, decision_id: str, detail: str) -> None:
        row = txn.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        if row is None:
            return
        attempts = row["attempts"]
        txn.event("decision.failed", decision_id, detail=detail[:500], attempts=attempts)
        limit = wakes.MAX_ATTEMPTS if row["purpose"] == "wake" else MAX_FAILURES
        if attempts < limit:
            txn.execute("UPDATE decisions SET state = 'pending' WHERE id = ?", (decision_id,))
            return
        txn.execute("UPDATE decisions SET state = 'failed', finished_at = ? WHERE id = ?",
                    (txn.now, decision_id))
        if row["purpose"] == "assignment":
            self._fill_with_new_worker(txn, row)
        elif row["purpose"] == "wake":
            self._wake_everyone(txn, decision_id)
        else:
            teams.record_recruitment(self.hub.ctx, txn, row["task_id"], False)

    def _finish_obsolete(self, decision_id: str) -> None:
        with self.hub.transaction() as txn:
            txn.execute("UPDATE decisions SET state = 'obsolete', finished_at = ? WHERE id = ?",
                        (txn.now, decision_id))
            txn.after_commit(self.hub.ctx.wake)
