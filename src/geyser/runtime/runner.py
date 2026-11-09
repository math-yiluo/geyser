"""Running one activation end to end (DESIGN.md sections 7, 13.4 and 14).

``ActivationRunner.run`` never raises. Whatever happens inside one activation, a
model error, a transport failure or a bug in Geyser, ends that activation with a
recorded outcome and, for failures, a backoff for its agent. The run continues.

Steps of one activation:

1. Decide whether the turn continues the agent's conversation or starts a new one
   (new task, new batch, rotation, changed standing instructions).
2. Prepare the workspace: first checkout, or the three-way refresh of task turns.
3. Build the model-facing input, prepare its deliveries, record the scope.
4. Run the backend turn; gateway tool calls are dispatched with the fenced actor.
5. Settle deliveries, record the conversation, usage and outcome.
6. Commit a drained operation once the scope is quiet.
7. Set the agent's next intent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from geyser.clock import iso, parse_iso
from geyser.core import activations, agents, choices, controls, inbox, teams
from geyser.core.actors import Actor, Capability
from geyser.core.ledger import Txn, dumps, loads
from geyser.errors import GeyserError
from geyser.host.base import ProcessHost, ScopeRef
from geyser.runtime import handoff, instructions, views
from geyser.runtime.backends.base import (
    Backend,
    TurnControl,
    TurnOutcome,
    TurnRequest,
    TurnStatus,
    Usage,
)

if TYPE_CHECKING:
    from geyser.gateway import Gateway
    from geyser.hub import Hub

log = logging.getLogger(__name__)

PURPOSE_CAPABILITY = {
    "task": Capability.WORKER,
    "message": Capability.MESSAGE_ONLY,
    "review": Capability.REVIEWER,
    "integration": Capability.INTEGRATOR,
}
DRAIN_GRACE_SECONDS = 120.0
NOTICE_AFTER_FAILURES = 5
TRANSCRIPT_INPUT_LIMIT = 200_000
"""Characters of a turn's input kept in its transcript, for the dashboard (section 15.3)."""


@dataclass
class LiveTurn:
    activation_id: str
    agent_id: str
    backend: Backend
    control: TurnControl
    actor: Actor


class ActivationRunner:
    def __init__(self, hub: Hub, gateway: Gateway, backends: dict[str, Backend],
                 host: ProcessHost):
        self.hub = hub
        self.gateway = gateway
        self.backends = backends
        self.host = host
        self.live: dict[str, LiveTurn] = {}
        """Running turns by agent ID (only while the backend turn runs)."""
        self._entered: set[str] = set()
        """Activations whose backend turn was entered (their input may have left the Hub)."""

    # ------------------------------------------------------------------ public
    async def run(self, activation_id: str) -> None:
        """Run one planned activation. Never raises."""
        agent_id = None
        try:
            with self.hub.read() as txn:
                activation = activations.get(txn, activation_id)
                if activation is None or activation["state"] != "planned":
                    return
                agent_id = activation["agent_id"]
            await self._run(activation_id)
        except Exception as error:  # noqa: BLE001 - one activation's failure stays local
            log.exception("activation %s failed inside Geyser", activation_id)
            self._record_internal_failure(activation_id, agent_id, error)
        finally:
            if agent_id is not None:
                self._drop_live(agent_id, activation_id)
            self._entered.discard(activation_id)

    def _drop_live(self, agent_id: str, activation_id: str) -> None:
        live = self.live.get(agent_id)
        if live is not None and live.activation_id == activation_id:
            del self.live[agent_id]

    def interrupt_all(self) -> None:
        for live in self.live.values():
            live.control.interrupt.set()

    def interrupt(self, agent_id: str) -> None:
        live = self.live.get(agent_id)
        if live is not None:
            live.control.interrupt.set()

    async def deliver_live(self, agent_id: str) -> int:
        """Steer pending messages into a running turn where the backend supports it."""
        live = self.live.get(agent_id)
        if live is None or not live.backend.capabilities.steering:
            return 0
        with self.hub.transaction() as txn:
            items = [item for item in inbox.pending(txn, agent_id) if item["item"] == "message"]
            if not items:
                return 0
            inbox.prepare(txn, [item["id"] for item in items], live.activation_id)
        text = "New messages for you (read each file in full before acting):\n" + "\n".join(
            f"- {item['summary']}" for item in items)
        transcript = self._transcript_writer(agent_id, live.activation_id)
        delivered = False
        try:
            delivered = await asyncio.wait_for(live.control.steer(text), timeout=60)
        except Exception:  # noqa: BLE001 - an uncertain steer is held, never resent
            log.exception("steering into %s failed", live.activation_id)
            with self.hub.transaction() as txn:
                inbox.mark_uncertain(txn, live.activation_id)
            transcript({"type": "steer", "text": text, "delivery": "uncertain"})
            return 0
        transcript({"type": "steer", "text": text,
                    "delivery": "delivered" if delivered else "not delivered"})
        with self.hub.transaction() as txn:
            if delivered:
                inbox.mark_delivered(txn, live.activation_id)
            else:
                inbox.release(txn, live.activation_id)
        return len(items) if delivered else 0

    # ------------------------------------------------------------------ the run
    async def _run(self, activation_id: str) -> None:
        hub = self.hub
        with hub.read() as txn:
            found = activations.get(txn, activation_id)
            assert found is not None
            activation = found
            agent = agents.agent_row(txn, activation["agent_id"])
        agent_id = agent["id"]
        backend = self.backends.get(agent["backend"])
        if backend is None:
            self._end_without_turn(activation_id, "failed",
                                   f"backend {agent['backend']!r} is not available")
            return
        purpose = activation["purpose"]
        role = agent["kind"]
        capability = self._capability(purpose, role)
        standing = instructions.standing_instructions(hub, self.gateway, role)
        # A change of the standing instructions or of the two tool definitions moves the
        # conversation to a new one (sections 7.3 and 13.1).
        standing_digest = instructions.digest(
            standing + json.dumps(self.gateway.tool_definitions(), sort_keys=True))
        subject = activation["task_id"] or activation["review_id"] or activation["batch_id"]
        intent = agent["intent"]
        new_conversation = self._needs_new_conversation(agent, activation, subject,
                                                        standing_digest, intent)
        refresh = await asyncio.to_thread(self._prepare_workspace, agent, activation)
        if new_conversation and activation["task_id"]:
            with hub.read() as txn:
                handoff.write_state_file(hub, txn, agent_id, activation["task_id"])
        on_demand = instructions.install_on_demand(hub, agent_id, role)
        scope = self.host.plan_scope(activation_id)
        with hub.transaction() as txn:
            inbox.reconcile_uncertain(txn, agent_id)
            agent = agents.agent_row(txn, agent_id)
            built = views.build(hub, txn, activation, agent, capability=capability.value,
                                intent=intent, new_conversation=new_conversation,
                                refresh=refresh, on_demand=on_demand)
            inbox.prepare(txn, built.item_ids, activation_id)
            activations.record_scope(txn, activation_id, scope.name)
            activations.mark_running(txn, activation_id,
                                     None if new_conversation else agent["conversation_id"])
            if new_conversation:
                txn.execute("UPDATE agents SET rotation_requested = 0 WHERE id = ?",
                            (agent_id,))
        actor = Actor(agent_id, capability, activation_id, activation["fence"],
                      activation["task_id"], activation["review_id"], activation["batch_id"])
        control = TurnControl()
        self.live[agent_id] = LiveTurn(activation_id, agent_id, backend, control, actor)
        transcript = self._transcript_writer(agent_id, activation_id)
        rotation_note = RotationWatch(hub, agent, new_conversation)
        compaction = CompactionWatch(backend.capabilities.compaction_keeps_inputs)
        request = self._turn_request(agent, role, actor, backend, new_conversation, standing,
                                     built, scope, control, transcript, rotation_note,
                                     compaction)
        transcript({"type": "start", "activation": activation_id, "purpose": purpose,
                    "new_conversation": new_conversation})
        transcript({"type": "input", "text": built.text[:TRANSCRIPT_INPUT_LIMIT]})
        self._entered.add(activation_id)
        try:
            outcome = await backend.run_turn(request)
        except Exception as error:  # noqa: BLE001 - a backend bug ends this turn only
            log.exception("backend %s raised during %s", backend.name, activation_id)
            outcome = TurnOutcome(TurnStatus.UNKNOWN, None, False, failure="backend_exception",
                                  detail=f"{type(error).__name__}: {error}")
        # Steering targets only a running backend turn; this one has returned.
        self._drop_live(agent_id, activation_id)
        transcript({"type": "outcome", "status": outcome.status.value,
                    "failure": outcome.failure, "detail": outcome.detail[:2000]})
        self._settle(activation_id, agent_id, outcome, built, new_conversation,
                     standing_digest, subject, rotation_note)
        await self._commit_drain_if_any(activation_id, scope)
        self._set_next_intent(activation_id, outcome)

    def _capability(self, purpose: str, role: str) -> Capability:
        if purpose == "answer":
            return (Capability.INTEGRATOR_ANSWER if role == "integrator"
                    else Capability.REVIEW_ANSWER)
        return PURPOSE_CAPABILITY[purpose]

    # ------------------------------------------------------------- conversation
    def _needs_new_conversation(self, agent, activation, subject: str | None,
                                standing_digest: str, intent: str) -> bool:
        """The rules of sections 6.8 and 7.5."""
        if agent["conversation_id"] is None:
            return True
        purpose = activation["purpose"]
        if purpose == "answer":
            return True
        if purpose == "task" and intent == "start":
            return True
        if agent["conversation_task"] != subject:
            return True
        if agent["conversation_digest"] != standing_digest:
            return True
        with self.hub.read() as txn:
            unsettled = txn.scalar("SELECT 1 FROM inbox WHERE agent_id = ? AND state = "
                                   "'uncertain' LIMIT 1", (agent["id"],))
        if unsettled:
            return False  # Nothing rotates while a delivery is unsettled.
        if purpose == "integration":
            return False  # The Integrator never rotates within a batch.
        if agent["rotation_requested"]:
            return self._handoff_ready(agent, activation)
        last = agent["conversation_last_turn"]
        rotation = self.hub.config.rotation
        if last:
            idle = self.hub.clock.now() - parse_iso(last)
            if idle >= timedelta(minutes=rotation.idle_minutes):
                return True
        return False

    def _handoff_ready(self, agent, activation) -> bool:
        """Check the handoff file before an aggressive rotation (section 7.4).

        An unusable file gets one more turn in the same conversation to be fixed;
        after that the new conversation starts from the state file alone.
        """
        task_id = activation["task_id"]
        if task_id is None or agent["rotation_requested"] >= 2:
            return True
        path = self.hub.layout.handoff_file(agent["id"], task_id)
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        workspace = self.hub.layout.workspace(agent["id"])
        problems = handoff.check_handoff(
            text, workspace_exists=lambda name: (workspace / name).exists()
            or (self.hub.layout.root / name).exists())
        if not problems:
            return True
        with self.hub.transaction() as txn:
            txn.execute("UPDATE agents SET rotation_requested = 2 WHERE id = ?", (agent["id"],))
            inbox.notify(txn, agent["id"],
                         "Before this conversation is replaced, fix your handoff file ("
                         + str(path) + "): " + "; ".join(problems[:5]) + ". Then end the turn.",
                         task_id=task_id)
        return False

    # ---------------------------------------------------------------- workspace
    def _prepare_workspace(self, agent, activation) -> dict[str, Any] | None:
        hub = self.hub
        agent_id = agent["id"]
        workspace = hub.layout.workspace(agent_id)
        hub.layout.private(agent_id).mkdir(parents=True, exist_ok=True)
        hub.layout.temporary_dir(agent_id).mkdir(parents=True, exist_ok=True)
        if agent["kind"] == "reviewer":
            return None  # Reviewers read the Library; they need no checkout.
        with hub.read() as txn:
            base = txn.scalar("SELECT commit_id FROM workspace_bases WHERE agent_id = ?",
                              (agent_id,))
        report = None
        if base is None:
            base = hub.library.checkout_workspace(workspace)
        elif activation["purpose"] == "task":
            result = hub.library.refresh_workspace(workspace, base)
            if result.updated or result.conflicts:
                report = result.to_dict()
            base = result.head or base
        with hub.transaction() as txn:
            txn.execute(
                "INSERT INTO workspace_bases (agent_id, commit_id, refreshed_at) VALUES "
                "(?, ?, ?) ON CONFLICT (agent_id) DO UPDATE SET commit_id = excluded.commit_id,"
                " refreshed_at = excluded.refreshed_at",
                (agent_id, base or "", txn.now),
            )
        return report

    # ------------------------------------------------------------------ turning
    def _turn_request(self, agent, role: str, actor: Actor, backend: Backend,
                      new_conversation: bool, standing: str, built: views.BuiltView,
                      scope: ScopeRef, control: TurnControl, transcript,
                      rotation: RotationWatch, compaction: CompactionWatch) -> TurnRequest:
        hub = self.hub
        with hub.read() as txn:
            settings = choices.for_agent(hub.ctx, txn, agent["kind"], agent["backend"])
        prefix = hub.config.gateway.prefix

        async def on_tool_call(name: str, arguments: dict[str, Any], call_id: str
                               ) -> dict[str, Any]:
            transcript({"type": "tool_call", "tool": name, "call_id": call_id,
                        "arguments": arguments})
            if name == f"{prefix}_actions":
                result = self.gateway.actions(actor, arguments.get("category"),
                                              arguments.get("operation"))
            elif name == f"{prefix}_call":
                operation = str(arguments.get("operation", ""))
                result = await self.gateway.call(actor, operation,
                                                 arguments.get("arguments") or {}, call_id,
                                                 runtime=self)
                op = self.gateway.operations.get(operation)
                if op is not None and op.drains and "error" not in result:
                    control.drain_requested.set()
            else:
                result = {"error": "not_found", "message": f"Unknown tool {name!r}."}
            note = rotation.note()
            if note and "error" not in result:
                result = {**result, "rotation": note}
            if compaction.pending and "error" not in result:
                compaction.pending = False
                result = {**result, "current_view": built.stable,
                          "note": "Your conversation was compacted; this is your current view."}
            transcript({"type": "tool_result", "call_id": call_id,
                        "result": _clip(result)})
            return result

        from geyser.domains.base import AgentView

        environment = dict(hub.pack.agent_environment(
            AgentView(agent["id"], role, agent["backend"], hub.layout.workspace(agent["id"]))))
        environment["TMPDIR"] = str(hub.layout.temporary_dir(agent["id"]))
        protected = [hub.layout.state] + [Path(path) for path in
                                          hub.config.sandbox.protected_paths]
        return TurnRequest(
            agent_id=agent["id"], role=role,
            conversation_id=None if new_conversation else agent["conversation_id"],
            standing_instructions=standing, tools=self.gateway.tool_definitions(),
            input_text=built.text, workspace=hub.layout.workspace(agent["id"]),
            temporary=hub.layout.temporary_dir(agent["id"]), scope=scope, host=self.host,
            environment=environment, model=settings.model, effort=settings.effort,
            readable_roots=[hub.layout.root], protected_paths=protected,
            network=hub.config.sandbox.network == "open", on_tool_call=on_tool_call,
            control=control, on_usage=rotation.observe, on_compaction=compaction.observe,
            transcript=transcript,
        )

    def _transcript_writer(self, agent_id: str, activation_id: str):
        path = self.hub.layout.transcripts / agent_id / f"{activation_id}.jsonl"

        def write(event: dict[str, Any]) -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"at": self.hub.clock.now().isoformat(), **event},
                                            ensure_ascii=False, default=str) + "\n")
            except OSError:
                log.warning("could not write transcript %s", path)  # Observation never gates.

        return write

    # ----------------------------------------------------------------- settling
    def _settle(self, activation_id: str, agent_id: str, outcome: TurnOutcome,
                built: views.BuiltView, new_conversation: bool, standing_digest: str,
                subject: str | None, rotation: RotationWatch) -> None:
        with self.hub.transaction() as txn:
            if outcome.input_delivered:
                inbox.mark_delivered(txn, activation_id)
            elif outcome.status is TurnStatus.UNKNOWN:
                inbox.mark_uncertain(txn, activation_id)
            else:
                inbox.release(txn, activation_id)
            self._record_conversation(txn, agent_id, outcome, built, new_conversation,
                                      standing_digest, subject, rotation)
            self._record_usage(txn, activation_id, agent_id, outcome.usage)
            agent = agents.agent_row(txn, agent_id)
            if outcome.account_hold:
                controls.hold(txn, agent["backend"], outcome.account_hold)
            row = activations.get(txn, activation_id)
            draining = row is not None and row["state"] == "draining"
            if outcome.status is TurnStatus.COMPLETED or (
                draining and outcome.status is TurnStatus.INTERRUPTED
            ):
                agents.clear_backoff(txn, agent_id)
                if not draining:
                    activations.end(txn, activation_id, "completed")
            elif outcome.status is TurnStatus.INTERRUPTED:
                activations.end(txn, activation_id, "interrupted")
            elif outcome.status is TurnStatus.UNKNOWN:
                if not draining:
                    activations.end(txn, activation_id, "closed_unknown",
                                    {"detail": outcome.detail[:1000]})
                    inbox.notify(txn, agent_id,
                                 "Your previous turn ended with an unknown outcome. Inspect "
                                 "your history and files before continuing; nothing was "
                                 "replayed.", task_id=agent["current_task"])
            else:
                if not draining:
                    activations.end(txn, activation_id, "failed",
                                    {"failure": outcome.failure, "detail": outcome.detail[:1000]})
                if not outcome.account_hold:
                    self._backoff(txn, agent_id, outcome.failure or "failed", outcome.detail)

    def _record_conversation(self, txn: Txn, agent_id: str, outcome: TurnOutcome,
                             built: views.BuiltView, new_conversation: bool,
                             standing_digest: str, subject: str | None,
                             rotation: RotationWatch) -> None:
        if not outcome.conversation_saved or outcome.conversation_id is None:
            txn.execute("UPDATE agents SET conversation_id = NULL, view_baseline = NULL "
                        "WHERE id = ?", (agent_id,))
            return
        tokens = outcome.usage.context_tokens
        if new_conversation:
            txn.execute(
                "UPDATE agents SET conversation_id = ?, conversation_task = ?, "
                "conversation_started_at = ?, conversation_digest = ?, "
                "conversation_start_tokens = ?, conversation_tokens = ?, "
                "conversation_last_turn = ? WHERE id = ?",
                (outcome.conversation_id, subject, txn.now, standing_digest, tokens, tokens,
                 txn.now, agent_id),
            )
        else:
            txn.execute(
                "UPDATE agents SET conversation_last_turn = ?, conversation_tokens = "
                "COALESCE(?, conversation_tokens) WHERE id = ?",
                (txn.now, tokens, agent_id),
            )
        if outcome.input_delivered:
            txn.execute("UPDATE agents SET view_baseline = ? WHERE id = ?",
                        (dumps(built.stable), agent_id))
        if outcome.compacted and not self.backends_keep_inputs(agent_id, txn):
            txn.execute("UPDATE agents SET view_baseline = NULL WHERE id = ?", (agent_id,))
        if rotation.requested:
            txn.execute("UPDATE agents SET rotation_requested = 1 WHERE id = ?", (agent_id,))

    def backends_keep_inputs(self, agent_id: str, txn: Txn) -> bool:
        backend = self.backends.get(agents.agent_row(txn, agent_id)["backend"])
        return backend is None or backend.capabilities.compaction_keeps_inputs

    def _record_usage(self, txn: Txn, activation_id: str, agent_id: str, usage: Usage) -> None:
        backend = txn.scalar("SELECT backend FROM agents WHERE id = ?", (agent_id,))
        txn.execute(
            "INSERT INTO usage (activation_id, agent_id, backend, input_tokens, cached_tokens, "
            "output_tokens, requests, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (activation_id, agent_id, backend, usage.input_tokens, usage.cached_tokens,
             usage.output_tokens, usage.requests, txn.now),
        )

    def _backoff(self, txn: Txn, agent_id: str, signature: str, detail: str) -> None:
        failures = agents.record_backoff(txn, agent_id, signature)
        if failures == NOTICE_AFTER_FAILURES:
            message = (f"{agent_id} failed {failures} times in a row ({signature}): "
                       f"{detail[:500]}")
            txn.execute(
                "INSERT INTO operator_notices (signature, subject, message, count, first_at, "
                "last_at) VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT (signature) DO UPDATE SET "
                "count = count + 1, last_at = excluded.last_at, message = excluded.message",
                (f"{agent_id}:{signature}", agent_id, message, txn.now, txn.now),
            )

    # ------------------------------------------------------------------ drains
    async def _commit_drain_if_any(self, activation_id: str, scope: ScopeRef) -> None:
        with self.hub.read() as txn:
            row = activations.get(txn, activation_id)
        if row is None or row["state"] != "draining":
            return
        quiet = await self.host.quiet(scope)
        if quiet is not True:
            await self.host.terminate(scope, DRAIN_GRACE_SECONDS)
            quiet = await self.host.quiet(scope)
        if quiet is not True:
            log.warning("scope %s of %s is not quiet; recovery commits the drain later",
                        scope.name, activation_id)
            return
        await commit_drain(self.hub, activation_id)

    # ------------------------------------------------------------------ intents
    def _set_next_intent(self, activation_id: str, outcome: TurnOutcome) -> None:
        with self.hub.transaction() as txn:
            row = activations.get(txn, activation_id)
            if row is None:
                return
            agent = agents.agent_row(txn, row["agent_id"])
            agent_id = agent["id"]
            purpose = row["purpose"]
            if agent["state"] != "active":
                return
            if purpose == "task":
                self._maybe_queue_recruitment(txn, row, agent)
                membership = None
                if agent["current_task"]:
                    membership = txn.one("SELECT state FROM memberships WHERE task_id = ? AND "
                                         "agent_id = ?", (agent["current_task"], agent_id))
                if membership is not None and membership["state"] == "active":
                    agents.set_intent(txn, agent_id, "continue")
                else:
                    agents.set_intent(txn, agent_id, "none")
            elif purpose == "review":
                review_open = agent["current_review"] and txn.scalar(
                    "SELECT state FROM reviews WHERE id = ?", (agent["current_review"],)
                ) == "open"
                agents.set_intent(txn, agent_id, "continue" if review_open and
                                  agent["review_wait"] is None else "none")
            elif purpose == "integration":
                batch_open = agent["current_batch"] and txn.scalar(
                    "SELECT state FROM batches WHERE id = ?", (agent["current_batch"],)) == "open"
                agents.set_intent(txn, agent_id, "continue" if batch_open else "none")
            del outcome

    def _maybe_queue_recruitment(self, txn: Txn, activation, agent) -> None:
        """At the end of the first executor's first turn, queue the recruitment decision."""
        task_id = activation["task_id"]
        if task_id is None:
            return
        task = txn.one("SELECT recruitment, state FROM tasks WHERE id = ?", (task_id,))
        if task is None or task["recruitment"] != "undecided":
            return
        role = txn.scalar("SELECT role FROM memberships WHERE task_id = ? AND agent_id = ?",
                          (task_id, agent["id"]))
        if role != "executor":
            return
        membership = txn.scalar("SELECT state FROM memberships WHERE task_id = ? AND "
                                "agent_id = ?", (task_id, agent["id"]))
        ended = membership not in ("active", "waiting")
        if (task["state"] in ("completed", "retired", "completing") or ended
                or self.hub.config.teams.collaborators < 1):
            teams.record_recruitment(self.hub.ctx, txn, task_id, False)
            return
        if txn.scalar("SELECT 1 FROM decisions WHERE task_id = ? AND purpose = 'recruitment'",
                      (task_id,)):
            return
        from geyser.ids import Kind

        decision_id = txn.next_id(Kind.DECISION)
        revision = txn.scalar("SELECT revision FROM tasks WHERE id = ?", (task_id,))
        txn.execute(
            "INSERT INTO decisions (id, purpose, task_id, task_revision, state, created_at) "
            "VALUES (?, 'recruitment', ?, ?, 'pending', ?)",
            (decision_id, task_id, revision, txn.now),
        )
        txn.after_commit(self.hub.ctx.wake)

    # ---------------------------------------------------------------- failures
    def _end_without_turn(self, activation_id: str, outcome: str, detail: str) -> None:
        with self.hub.transaction() as txn:
            row = activations.get(txn, activation_id)
            activations.end(txn, activation_id, outcome, {"detail": detail})
            if row is not None:
                self._backoff(txn, row["agent_id"], "unavailable_backend", detail)

    def _record_internal_failure(self, activation_id: str, agent_id: str | None,
                                 error: Exception) -> None:
        try:
            with self.hub.transaction() as txn:
                row = activations.get(txn, activation_id)
                if row is None or row["state"] == "ended":
                    return
                # Input that never reached a backend turn is released, not uncertain.
                entered = activation_id in self._entered
                outcome = "closed_unknown" if entered else "failed"
                if not entered:
                    inbox.release(txn, activation_id)
                if row["state"] != "draining":
                    activations.end(txn, activation_id, outcome,
                                    {"internal": f"{type(error).__name__}: {error}"})
                if agent_id:
                    self._backoff(txn, agent_id, f"internal:{type(error).__name__}", str(error))
                txn.event("internal.error", activation_id,
                          trace=traceback.format_exc()[-4000:])
        except Exception:  # noqa: BLE001 - never let failure recording raise
            log.exception("could not record the failure of %s", activation_id)

    # ------------------------------------------------------------ runtime hooks
    async def history_search(self, agent_id: str, query: str, limit: int) -> dict[str, Any]:
        """Search this agent's own transcripts (never another agent's)."""
        directory = self.hub.layout.transcripts / agent_id
        needle = query.lower()

        def search() -> list[dict[str, Any]]:
            hits: list[dict[str, Any]] = []
            for path in sorted(directory.glob("*.jsonl"), reverse=True):
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if needle in line.lower():
                        hits.append({"activation": path.stem, "event": _clip(json.loads(line),
                                                                              800)})
                        if len(hits) >= limit:
                            return hits
            return hits

        hits = await asyncio.to_thread(search) if directory.exists() else []
        return {"query": query, "hits": hits}


class CompactionWatch:
    """After a compaction that summarizes inputs, the next tool result carries the current
    view (section 7.7). A backend that keeps inputs verbatim is sent nothing again."""

    def __init__(self, keeps_inputs: bool):
        self.keeps_inputs = keeps_inputs
        self.pending = False

    def observe(self) -> None:
        if not self.keeps_inputs:
            self.pending = True


class RotationWatch:
    """Watches context growth for aggressive rotation (section 7.5)."""

    def __init__(self, hub: Hub, agent, new_conversation: bool):
        settings = hub.config.rotation
        mode = settings.mode
        if agent["kind"] == "reviewer" and settings.reviewer_mode:
            mode = settings.reviewer_mode
        self.enabled = mode == "aggressive" and agent["kind"] != "integrator"
        self.limit = settings.context_tokens
        self.growth = settings.context_growth_tokens
        self.start = None if new_conversation else agent["conversation_start_tokens"]
        self.requested = False

    def observe(self, usage: Usage) -> None:
        if not self.enabled or usage.context_tokens is None:
            return
        if self.start is None:
            self.start = usage.context_tokens
        if usage.context_tokens > self.limit and usage.context_tokens - self.start > self.growth:
            self.requested = True

    def note(self) -> str | None:
        if not self.requested:
            return None
        return ("This conversation is long. Let your current command finish, update your "
                "handoff file and journal, and end the turn; the next turn continues in a "
                "new conversation.")


def _clip(value: Any, limit: int = 4000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"clipped": text[:limit]}


DRAIN_RETRY_MINUTES = (1, 4, 15)
"""Delays before retrying a drained commit that failed unexpectedly; then it is abandoned."""


async def commit_drain(hub: Hub, activation_id: str) -> None:
    """Commit what a drained operation asked for, exactly once.

    Called once the activation's scope is confirmed quiet, by the runner or by recovery.
    Each commit ends the activation in the same transaction that makes its effect
    durable, so a retry after a crash finds it done. A contribution records its
    candidate in the drain record when it registers it, so a retry never freezes a
    second candidate. A refusal (``GeyserError``) abandons the drain with its reason;
    an unexpected error is retried a few times with backoff, then abandoned, and never
    stops the service.
    """
    with hub.read() as txn:
        row = activations.get(txn, activation_id)
        if row is None or row["state"] != "draining":
            return
        drain = loads(row["drain"], {})
        agent = agents.agent_row(txn, row["agent_id"])
        due = drain.get("next_attempt_at")
        if due and parse_iso(due) > txn.moment:
            return
    operation = drain.get("operation")
    actor = Actor(agent["id"], Capability.SYSTEM, activation_id, row["fence"], row["task_id"],
                  row["review_id"], row["batch_id"])
    try:
        await _commit(hub, activation_id, operation, drain, actor)
    except GeyserError as error:
        _abandon_drain(hub, activation_id, agent["id"], row["task_id"], operation,
                       error.message)
    except Exception as error:  # noqa: BLE001 - a drain bug stays with its activation
        log.exception("drained %s of %s failed", operation, activation_id)
        _retry_drain_later(hub, activation_id, agent["id"], row["task_id"], operation, drain,
                           error)


async def _commit(hub: Hub, activation_id: str, operation: str | None,
                  drain: dict[str, Any], actor: Actor) -> None:
    from geyser.gateway import ops_completion, ops_contributions, ops_progress

    payload = drain.get("payload", {})
    if operation == "contribute":
        candidate_id = drain.get("result", {}).get("candidate")
        if candidate_id is None:
            def remember(txn: Txn, reply: dict[str, Any]) -> None:
                recorded = {**drain, "result": {"candidate": reply["candidate"]}}
                txn.execute("UPDATE activations SET drain = ? WHERE id = ?",
                            (dumps(recorded), activation_id))

            reply = await ops_contributions.freeze_contribution(hub, actor, payload, remember)
            candidate_id = reply["candidate"]
        with hub.transaction() as txn:
            ops_contributions.wait_for_outcome(hub.ctx, txn, actor, payload, candidate_id)
            _finish_drain(txn, activation_id, actor.id, operation, {"candidate": candidate_id})
    elif operation == "pause":
        with hub.transaction() as txn:
            result = ops_progress.commit_pause(hub.ctx, txn, actor, payload)
            _finish_drain(txn, activation_id, actor.id, operation, result)
    elif operation == "complete":
        with hub.transaction() as txn:
            result = ops_completion.commit_complete(hub.ctx, txn, actor, payload)
            _finish_drain(txn, activation_id, actor.id, operation, result)
    elif operation == "batch_publish":
        from geyser.integration import batch

        result = await batch.commit_publish(hub, actor, payload)
        with hub.transaction() as txn:
            _finish_drain(txn, activation_id, actor.id, operation, result)
    else:
        raise GeyserError(f"Unknown drained operation {operation!r}.")


def _finish_drain(txn: Txn, activation_id: str, agent_id: str, operation: str | None,
                  result: dict[str, Any]) -> None:
    txn.execute("UPDATE receipts SET state = 'committed' WHERE activation_id = ? "
                "AND state = 'draining'", (activation_id,))
    activations.end(txn, activation_id, "completed", {"operation": operation,
                                                      "result": _clip(result, 2000)})
    txn.event("drain.committed", activation_id, agent_id, operation=operation)


def _abandon_drain(hub: Hub, activation_id: str, agent_id: str, task_id: str | None,
                   operation: str | None, reason: str) -> None:
    with hub.transaction() as txn:
        activations.end(txn, activation_id, "abandoned",
                        {"operation": operation, "reason": reason})
        inbox.notify(txn, agent_id, f"Your `{operation}` could not be committed after the "
                     f"turn: {reason}", task_id=task_id, wakes=True)
        txn.event("drain.abandoned", activation_id, agent_id, operation=operation,
                  reason=reason)


def _retry_drain_later(hub: Hub, activation_id: str, agent_id: str, task_id: str | None,
                       operation: str | None, drain: dict[str, Any], error: Exception) -> None:
    attempts = int(drain.get("attempts", 0)) + 1
    if attempts > len(DRAIN_RETRY_MINUTES):
        _abandon_drain(hub, activation_id, agent_id, task_id, operation,
                       f"an internal error persisted ({type(error).__name__}: {error})")
        return
    with hub.transaction() as txn:
        retry = {**drain, "attempts": attempts,
                 "next_attempt_at": iso(txn.moment + timedelta(
                     minutes=DRAIN_RETRY_MINUTES[attempts - 1])),
                 "last_error": f"{type(error).__name__}: {str(error)[:500]}"}
        txn.execute("UPDATE activations SET drain = ? WHERE id = ?",
                    (dumps(retry), activation_id))
        txn.event("drain.retry", activation_id, agent_id, operation=operation,
                  attempts=attempts, error=retry["last_error"])
