"""Operator controls and account holds (DESIGN.md sections 14 and 15.1).

Scopes, narrowest first: ``agent:<id>``, ``group:<backend>.<role>s``,
``group:<backend>``, ``run``. The Integrator's group, ``group:integrator``, is a
group of its own in no backend group: for the Integrator it takes the place of the
backend groups. The narrowest explicit control applies. A control is kept only while
it differs from what applies around it. Run-wide actions (pause all, resume all,
stop, Task 0 completion) end every narrower control.
"""

from __future__ import annotations

from dataclasses import dataclass

from geyser.core.ledger import Txn
from geyser.errors import InvalidArgument

RUNNING = "running"
PAUSED = "paused"
PAUSE_AFTER_TURN = "pause_after_turn"
STOPPED = "stopped"
COMPLETED = "completed"
"""The run after Task 0 completed: nothing autonomous starts until the operator resumes."""

_RUN_STATES = {RUNNING, PAUSED, PAUSE_AFTER_TURN, STOPPED, COMPLETED}
_NARROW_STATES = {RUNNING, PAUSED, PAUSE_AFTER_TURN}
INTEGRATOR_GROUP = "integrator"


@dataclass(frozen=True)
class Resolution:
    state: str
    """running | paused | pause_after_turn | stopped | completed"""
    source: str
    """The scope that decided it."""
    new_work: bool
    """Whether new tasks and new agents may start under this resolution."""

    @property
    def may_start(self) -> bool:
        return self.state == RUNNING


def _get(txn: Txn, scope: str) -> str | None:
    return txn.scalar("SELECT state FROM controls WHERE scope = ?", (scope,))


def run_state(txn: Txn) -> str:
    return _get(txn, "run") or STOPPED


def set_run(txn: Txn, state: str, *, clear_narrower: bool = True) -> None:
    if state not in _RUN_STATES:
        raise InvalidArgument(f"Unknown run state {state!r}.")
    txn.execute(
        "INSERT INTO controls (scope, state, at) VALUES ('run', ?, ?) "
        "ON CONFLICT (scope) DO UPDATE SET state = excluded.state, at = excluded.at",
        (state, txn.now),
    )
    if clear_narrower:
        txn.execute("DELETE FROM controls WHERE scope != 'run'")
    txn.event("run.state", "run", state=state)


def group_scopes(backend: str, role: str) -> list[str]:
    if role == "integrator":
        return [f"group:{INTEGRATOR_GROUP}"]
    return [f"group:{backend}.{role}s", f"group:{backend}"]


def integration_paused(txn: Txn) -> bool:
    """Whether the operator paused the Integrator's group: no integration goes on."""
    return _get(txn, f"group:{INTEGRATOR_GROUP}") in (PAUSED, PAUSE_AFTER_TURN)


def resolve(txn: Txn, agent_id: str | None, backend: str, role: str) -> Resolution:
    """The control that applies to one agent (or a new agent of ``backend`` and ``role``)."""
    run = run_state(txn)
    scopes = ([f"agent:{agent_id}"] if agent_id else []) + group_scopes(backend, role)
    for scope in scopes:
        state = _get(txn, scope)
        if state is None:
            continue
        if scope.startswith("agent:") and state == RUNNING and run != RUNNING:
            # An agent resumed in a paused run continues only its own current work.
            return Resolution(RUNNING, scope, new_work=False)
        return Resolution(state, scope, new_work=state == RUNNING)
    return Resolution(run, "run", new_work=run == RUNNING)


def set_narrow(txn: Txn, scope: str, state: str | None) -> None:
    """Set or clear a narrower control, keeping it only if it differs from its surroundings."""
    if state is not None and state not in _NARROW_STATES:
        raise InvalidArgument(f"Unknown control state {state!r}.")
    txn.execute("DELETE FROM controls WHERE scope = ?", (scope,))
    if state is not None and _surrounding_state(txn, scope) != state:
        txn.execute("INSERT INTO controls (scope, state, at) VALUES (?, ?, ?)",
                    (scope, state, txn.now))
    txn.event("control.set", scope, state=state)


def _surrounding_state(txn: Txn, scope: str) -> str:
    if scope.startswith("agent:"):
        agent_id = scope.split(":", 1)[1]
        row = txn.one("SELECT backend, kind FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            return run_state(txn)
        return resolve(txn, None, row["backend"], row["kind"]).state
    if scope.startswith("group:") and "." in scope:
        backend = scope.split(":", 1)[1].split(".")[0]
        return _get(txn, f"group:{backend}") or run_state(txn)
    return run_state(txn)  # Backend groups and the Integrator's group sit under the run.


# --------------------------------------------------------------------------- holds
def hold(txn: Txn, account: str, reason: str) -> None:
    """Hold a backend account: no new model input until the operator resumes."""
    txn.execute(
        "INSERT INTO holds (account, reason, at) VALUES (?, ?, ?) "
        "ON CONFLICT (account) DO UPDATE SET reason = excluded.reason, at = excluded.at",
        (account, reason, txn.now),
    )
    txn.event("account.hold", account, reason=reason)


def held(txn: Txn, account: str) -> bool:
    return bool(txn.scalar("SELECT 1 FROM holds WHERE account = ?", (account,)))


def clear_holds(txn: Txn) -> list[str]:
    accounts = [row["account"] for row in txn.all("SELECT account FROM holds")]
    txn.execute("DELETE FROM holds")
    for account in accounts:
        txn.event("account.released", account)
    return accounts


def agent_blocked(txn: Txn, agent_id: str, backend: str, role: str) -> bool:
    """Whether a control or a hold keeps ``agent_id`` from starting a turn now."""
    return not resolve(txn, agent_id, backend, role).may_start or held(txn, backend)
