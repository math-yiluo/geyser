"""The operator's choices before a run (DESIGN.md section 15.1).

No role has a default model or effort, and no role takes another's: workers on each
backend, reviewers, the Integrator, the Dispatcher and the Waker each have their own,
as does the answer deadline. ``geyser.toml`` holds them; a choice the operator makes
at runtime (``geyser choose``) is kept in the ledger and overrides the file from each
agent's next turn. The Hub starts without them, but a run starts or resumes only once
every one is chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from geyser.core.context import HubContext
from geyser.core.ledger import Txn, dumps, loads
from geyser.errors import Conflict, InvalidArgument

ROLES = ("reviewers", "integrator", "dispatcher", "waker")
"""Non-worker roles. Workers are chosen per backend as ``workers.<backend>``."""

ANSWER_DEADLINE = "answer_deadline"


@dataclass(frozen=True)
class RoleChoice:
    backend: str
    model: str | None
    effort: str | None

    @property
    def complete(self) -> bool:
        return bool(self.model) and bool(self.effort)


def _override(txn: Txn, key: str) -> dict[str, Any]:
    value = loads(txn.meta(f"choice:{key}"), {})
    return value if isinstance(value, dict) else {}


def worker_choice(ctx: HubContext, txn: Txn, backend: str) -> RoleChoice:
    """The model and effort of workers on ``backend``."""
    section = ctx.config.backend(backend)
    override = _override(txn, f"workers.{backend}")
    return RoleChoice(backend, override.get("model", section.model),
                      override.get("effort", section.effort))


def role_choice(ctx: HubContext, txn: Txn, role: str) -> RoleChoice:
    """The backend, model and effort of a non-worker role."""
    config = ctx.config
    sections = {"reviewers": config.reviewers, "integrator": config.integrator,
                "dispatcher": config.decisions, "waker": config.waker}
    if role not in sections:
        raise InvalidArgument(f"Unknown role {role!r}.",
                              details={"roles": [*ROLES, "workers.<backend>"]})
    section = sections[role]
    override = _override(txn, role)
    return RoleChoice(override.get("backend", section.backend),
                      override.get("model", section.model),
                      override.get("effort", section.effort))


def for_agent(ctx: HubContext, txn: Txn, kind: str, backend: str) -> RoleChoice:
    """The choice an agent's turn runs with."""
    if kind == "worker":
        return worker_choice(ctx, txn, backend)
    return role_choice(ctx, txn, "reviewers" if kind == "reviewer" else "integrator")


def answer_deadline_minutes(ctx: HubContext, txn: Txn) -> float | None:
    value = txn.meta(f"choice:{ANSWER_DEADLINE}")
    if value is not None:
        return float(value)
    return ctx.config.waits.answer_deadline_minutes


def worker_backends(ctx: HubContext, txn: Txn) -> list[str]:
    """Every backend workers use or can start on."""
    found = {txn.meta("new_backend") or ctx.config.agents.new_backend,
             *ctx.config.assignment.worker_backends}
    found.update(row[0] for row in txn.all(
        "SELECT DISTINCT backend FROM agents WHERE kind = 'worker' AND state != 'retired'"))
    found.update(row[0] for row in txn.all(
        "SELECT DISTINCT move_to FROM agents WHERE move_to IS NOT NULL"))
    return sorted(found)


def missing(ctx: HubContext, txn: Txn) -> list[str]:
    """What the operator still has to choose before a run, in words."""
    result = []
    for backend in worker_backends(ctx, txn):
        if not worker_choice(ctx, txn, backend).complete:
            result.append(f"workers.{backend}: the model and effort of workers on {backend}")
    roles = [role for role in ROLES
             if role != "integrator" or ctx.config.integration.mode == "batch"]
    for role in roles:
        if not role_choice(ctx, txn, role).complete:
            result.append(f"{role}: its model and effort")
    if answer_deadline_minutes(ctx, txn) is None:
        result.append(f"{ANSWER_DEADLINE}: how long a wait for an answer lasts")
    return result


def require(ctx: HubContext, txn: Txn) -> None:
    """Refuse a run (re)start while any choice is missing."""
    absent = missing(ctx, txn)
    if absent:
        raise Conflict("Choose these before a run: " + "; ".join(absent) + ".",
                       details={"reason": "choices_required", "missing": absent})


def choose(ctx: HubContext, txn: Txn, role: str, *, backend: str | None = None,
           model: str | None = None, effort: str | None = None,
           minutes: float | None = None) -> dict[str, Any]:
    """Record the operator's choice for one role, or the answer deadline."""
    if role == ANSWER_DEADLINE:
        if minutes is None or minutes <= 0:
            raise InvalidArgument("The answer deadline is a positive number of minutes.")
        txn.set_meta(f"choice:{ANSWER_DEADLINE}", str(float(minutes)))
        txn.event("choice.made", ANSWER_DEADLINE, "operator", minutes=minutes)
        return {"role": ANSWER_DEADLINE, "minutes": float(minutes)}
    for name, value in (("model", model), ("effort", effort), ("backend", backend)):
        if value is not None and not value.strip():
            raise InvalidArgument(f"An empty {name} is not a choice.")
    if role.startswith("workers."):
        if backend is not None:
            raise InvalidArgument("A worker choice is per backend: name it in the role, "
                                  "as `workers.<backend>`.")
        current = worker_choice(ctx, txn, role.split(".", 1)[1])
    else:
        current = role_choice(ctx, txn, role)
    override = _override(txn, role)
    for name, value in (("backend", backend), ("model", model), ("effort", effort)):
        if value is not None:
            override[name] = value
    txn.set_meta(f"choice:{role}", dumps(override) or "{}")
    txn.event("choice.made", role, "operator", **override)
    chosen = worker_choice(ctx, txn, current.backend) if role.startswith("workers.") \
        else role_choice(ctx, txn, role)
    return {"role": role, "backend": chosen.backend, "model": chosen.model,
            "effort": chosen.effort}


def overview(ctx: HubContext, txn: Txn) -> dict[str, Any]:
    """Every choice in force, for the status and the dashboard."""
    workers = {backend: vars(worker_choice(ctx, txn, backend))
               for backend in worker_backends(ctx, txn)}
    roles = {role: vars(role_choice(ctx, txn, role)) for role in ROLES}
    return {"workers": workers, "roles": roles,
            "answer_deadline_minutes": answer_deadline_minutes(ctx, txn),
            "missing": missing(ctx, txn)}
