"""The running service: scheduler, runtime and loops around one Hub.

Every step is isolated: an exception in one step (or one item of a step) is logged,
reported once, and the other steps keep running. Slow work never blocks admission:
in a live service, validations, decisions and integration run in loops of their
own, each supervised with backoff (DESIGN.md section 14, "failures stay local").

``run_until_idle`` drives the same steps deterministically for tests and dry runs.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from geyser.content.staging import housekeep
from geyser.core import agents, controls, inbox
from geyser.gateway import Gateway
from geyser.host import default_host
from geyser.host.base import ProcessHost
from geyser.integration import direct, publication
from geyser.runtime.backends.base import Backend
from geyser.runtime.runner import ActivationRunner
from geyser.scheduler import recovery, validation
from geyser.scheduler.admission import Admission
from geyser.scheduler.decisions import DecisionRunner

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)

LOOP_BACKOFF_SECONDS = (1, 2, 5, 10, 30, 60)
RECOVERY_BACKOFF_SECONDS = (5, 10, 20, 40, 80, 160, 300)
VALIDATION_CONCURRENCY = 4


class GeyserService:
    def __init__(self, hub: Hub, backends: dict[str, Backend],
                 host: ProcessHost | None = None):
        from geyser.gateway import ops_integration

        self.hub = hub
        self.backends = backends
        self.host = host or default_host(hub.config.host.kind)
        self.gateway = Gateway(hub, extra=ops_integration.OPERATIONS)
        self.runner = ActivationRunner(hub, self.gateway, backends, self.host)
        self.admission = Admission(hub, self._start_activation)
        self.decisions = DecisionRunner(hub, backends, self.host)
        self.integration_backoff = direct.Backoff(hub.clock)
        self._activation_tasks: dict[str, asyncio.Task[None]] = {}
        self._validation_tasks: dict[str, asyncio.Task[None]] = {}
        self._loops: list[asyncio.Task[None]] = []
        self._wake_events: list[asyncio.Event] = []
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reported: set[str] = set()
        hub.on_wake(self._wake_threadsafe)

    # ----------------------------------------------------------------- waking
    def _wake_threadsafe(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake_all)
        except RuntimeError:
            pass  # The loop is closed.

    def _wake_all(self) -> None:
        for event in self._wake_events:
            event.set()

    def _start_activation(self, activation_id: str) -> None:
        task = asyncio.get_running_loop().create_task(self.runner.run(activation_id),
                                                      name=f"activation {activation_id}")
        self._activation_tasks[activation_id] = task
        task.add_done_callback(functools.partial(self._activation_done, activation_id))

    def _activation_done(self, activation_id: str, _task: object = None) -> None:
        self._activation_tasks.pop(activation_id, None)
        self._wake_all()

    # ---------------------------------------------------------------- isolation
    async def _step(self, name: str, step: Callable[[], Awaitable[object]]) -> bool:
        """Run one step; a failure is logged (reported once per kind) and counts as idle."""
        try:
            return bool(await step())
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one broken step never stops the others
            signature = f"{name}:{type(error).__name__}"
            log.exception("service step %s failed", name)
            if signature not in self._reported:
                self._reported.add(signature)
                self._notice(signature, f"The {name} step failed: {type(error).__name__}: "
                             f"{str(error)[:500]}")
            return False

    def _notice(self, signature: str, message: str) -> None:
        try:
            with self.hub.transaction() as txn:
                txn.execute(
                    "INSERT INTO operator_notices (signature, subject, message, count, "
                    "first_at, last_at) VALUES (?, 'service', ?, 1, ?, ?) ON CONFLICT "
                    "(signature) DO UPDATE SET count = count + 1, last_at = excluded.last_at",
                    (signature, message, txn.now, txn.now))
        except Exception:  # noqa: BLE001 - reporting must never raise
            log.exception("could not record an operator notice")

    # ------------------------------------------------------------------- steps
    async def _drains(self) -> int:
        return await recovery.retry_unsettled_drains(self.hub, self.host,
                                                     set(self._activation_tasks))

    async def _admit(self) -> int:
        return self.admission.run_pass()

    async def _steer_live(self) -> int:
        delivered = 0
        for agent_id in list(self.runner.live):
            with self.hub.read() as txn:
                has_messages = any(item["item"] == "message"
                                   for item in inbox.pending(txn, agent_id))
            if has_messages:
                delivered += await self.runner.deliver_live(agent_id)
        return delivered

    async def _housekeep(self) -> int:
        moved = await asyncio.to_thread(housekeep, self.hub)
        with self.hub.transaction() as txn:
            for agent_id in agents.retirement_candidates(self.hub.ctx, txn):
                agents.retire(txn, agent_id, "idle with nothing of its own left")
        del moved
        return 0  # Housekeeping is never progress by itself.

    async def _integrate(self) -> bool:
        if self.hub.config.integration.mode != "direct":
            return False
        with self.hub.read() as txn:
            if controls.run_state(txn) == controls.STOPPED:
                return False
        # A publication already decided is finished even while integration is paused.
        await asyncio.to_thread(publication.finish_pending, self.hub)
        with self.hub.read() as txn:
            if controls.integration_paused(txn):
                return False
        progressed = False
        for _ in range(20):
            published = await direct.integrate_next(self.hub, self.integration_backoff)
            if published is None:
                return progressed
            progressed = True
        return progressed

    async def _validate_inline(self) -> int:
        count = await validation.run_due(self.hub)
        return count + await validation.resume_queued(self.hub)

    def _start_validations(self) -> int:
        """Start due validations as background tasks, up to the concurrency bound."""
        with self.hub.read() as txn:
            due = [row["id"] for row in validation.due(txn)]
        started = 0
        for validation_id in due:
            if validation_id in self._validation_tasks:
                continue
            if len(self._validation_tasks) >= VALIDATION_CONCURRENCY:
                break
            task = asyncio.get_running_loop().create_task(
                self._guarded_validation(validation_id), name=f"validation {validation_id}")
            self._validation_tasks[validation_id] = task
            task.add_done_callback(functools.partial(self._validation_done, validation_id))
            started += 1
        return started

    async def _guarded_validation(self, validation_id: str) -> None:
        await self._step(f"validation {validation_id}",
                         lambda: validation.run_one(self.hub, validation_id))

    def _validation_done(self, validation_id: str, _task: object = None) -> None:
        self._validation_tasks.pop(validation_id, None)
        self._wake_all()

    async def round(self) -> bool:
        """One pass of every step, each isolated; returns whether anything happened."""
        progress = False
        progress |= await self._step("drains", self._drains)
        progress |= await self._step("validation", self._validate_inline)
        progress |= await self._step("decisions", self.decisions.run_pending)
        progress |= await self._step("integration", self._integrate)
        progress |= await self._step("admission", self._admit)
        progress |= await self._step("steering", self._steer_live)
        await self._step("housekeeping", self._housekeep)
        return progress

    async def run_until_idle(self, *, timeout: float = 60.0, max_rounds: int = 500) -> int:
        """Drive rounds until nothing is left to do (tests and dry runs)."""
        self._loop = asyncio.get_running_loop()
        await self._step("recovery", lambda: recovery.recover_on_start(self.hub, self.host))
        rounds = 0

        async def drive() -> None:
            nonlocal rounds
            idle_rounds = 0
            while rounds < max_rounds:
                rounds += 1
                progress = await self.round()
                if self._activation_tasks:
                    await asyncio.gather(*list(self._activation_tasks.values()),
                                         return_exceptions=True)
                    progress = True
                idle_rounds = 0 if progress else idle_rounds + 1
                if idle_rounds >= 2:
                    return

        await asyncio.wait_for(drive(), timeout=timeout)
        return rounds

    # ------------------------------------------------------------- live loops
    async def start(self) -> None:
        """Start the live service; recovery is retried with backoff until it succeeds."""
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._loops = [asyncio.create_task(self._run(), name="geyser service")]

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                await recovery.recover_on_start(self.hub, self.host)
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - startup recovery is retried with backoff
                log.exception("startup recovery failed; retrying")
                await asyncio.sleep(RECOVERY_BACKOFF_SECONDS[
                    min(attempt, len(RECOVERY_BACKOFF_SECONDS) - 1)])
                attempt += 1
        tick = self.hub.config.scheduler.tick_seconds
        loops = {
            "scheduler": self._scheduler_step,
            "validation": self._validation_step,
            "decisions": self.decisions.run_pending,
            "integration": self._integrate,
        }
        await asyncio.gather(*(self._supervised(name, step, tick)
                               for name, step in loops.items()))

    async def _scheduler_step(self) -> None:
        await self._step("drains", self._drains)
        await self._step("admission", self._admit)
        await self._step("steering", self._steer_live)
        await self._step("housekeeping", self._housekeep)

    async def _validation_step(self) -> None:
        self._start_validations()
        await validation.resume_queued(self.hub)

    async def _supervised(self, name: str, step: Callable[[], Awaitable[object]],
                          interval: float) -> None:
        wake = asyncio.Event()
        self._wake_events.append(wake)
        failures = 0
        while not self._stopping:
            wake.clear()
            try:
                await step()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a loop survives its own bugs
                log.exception("loop %s failed", name)
                delay = LOOP_BACKOFF_SECONDS[min(failures, len(LOOP_BACKOFF_SECONDS) - 1)]
                failures += 1
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(wake.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def stop(self, *, interrupt: bool = True, grace_seconds: float = 30.0) -> None:
        self._stopping = True
        self._wake_all()
        if interrupt:
            self.runner.interrupt_all()
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        pending_work = [*self._activation_tasks.values(), *self._validation_tasks.values()]
        if pending_work:
            _done, pending = await asyncio.wait(pending_work, timeout=grace_seconds)
            for task in pending:
                task.cancel()
        self._loops = []

    # --------------------------------------------------------- operator hooks
    def interrupt_for_pause(self, agent_id: str | None = None) -> None:
        if agent_id is None:
            self.runner.interrupt_all()
        else:
            self.runner.interrupt(agent_id)
        self._wake_all()
