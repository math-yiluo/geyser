"""Which tasks wait on which, and the cycles a new wait would close (DESIGN.md section 12).

Task A waits on task B while:

- a waiting member of A subscribes to B;
- a wait recorded before subscriptions names B (or a candidate of B) in an unmet
  requirement;
- A is open and its current contract requires B, or a candidate of B, and that is not
  met; or A's contract takes an unpublished candidate of B as an input.

The graph is derived from these records whenever it is needed, so it never drifts
from them. A task whose contract cannot be read contributes no contract edges; that
never blocks the check for the others.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque

from geyser.core import tasks, waits
from geyser.core.context import HubContext
from geyser.core.ledger import Txn, loads
from geyser.core.models import Predicate, RequirementTerm, TaskContract
from geyser.errors import GeyserError
from geyser.ids import Kind, kind_of

log = logging.getLogger(__name__)

Graph = dict[str, set[str]]
"""A task, to the tasks it waits on."""


def waits_on(ctx: HubContext, txn: Txn) -> Graph:
    owners = {row["id"]: row["task_id"] for row in txn.all("SELECT id, task_id FROM candidates")}
    graph: Graph = defaultdict(set)
    for row in txn.all("SELECT task_id, predicate FROM memberships WHERE state = 'waiting' "
                       "AND predicate IS NOT NULL"):
        try:
            predicate = Predicate.model_validate(loads(row["predicate"]))
        except ValueError:
            continue
        for subscription in predicate.subscriptions:
            graph[row["task_id"]].add(subscription.task_id)
        for term in predicate.requirements:
            provider = _provider_task(term.provider, owners)
            if provider and not waits.requirement_met(txn, term):
                graph[row["task_id"]].add(provider)
    for row in txn.all("SELECT id FROM tasks WHERE state NOT IN ('completed', 'retired')"):
        try:
            contract = tasks.load_contract(ctx, txn, row["id"])
        except GeyserError:
            continue
        graph[row["id"]].update(contract_needs(txn, contract, owners))
    for task_id in list(graph):
        graph[task_id].discard(task_id)
    return graph


def contract_needs(txn: Txn, contract: TaskContract, owners: dict[str, str] | None = None
                   ) -> set[str]:
    """The tasks a contract's unmet requirements and unpublished inputs wait on."""
    if owners is None:
        owners = {row["id"]: row["task_id"]
                  for row in txn.all("SELECT id, task_id FROM candidates")}
    found = set()
    for requirement in contract.requirements:
        term = RequirementTerm(provider=requirement.provider, revision=requirement.revision)
        provider = _provider_task(requirement.provider, owners)
        if provider and not waits.requirement_met(txn, term):
            found.add(provider)
    for value in tasks.unavailable_inputs(txn, contract):
        if value in owners:
            found.add(owners[value])
    return found


def _provider_task(provider: str, owners: dict[str, str]) -> str | None:
    if kind_of(provider) is Kind.TASK:
        return provider
    return owners.get(provider)


def cycle_through(ctx: HubContext, txn: Txn, waiter: str, providers: list[str]
                  ) -> list[str] | None:
    """The cycle that ``waiter`` waiting on ``providers`` would close, or None.

    The cycle is returned as ``[waiter, provider, …, waiter]``.
    """
    graph = waits_on(ctx, txn)
    for provider in providers:
        path = _path(graph, provider, waiter)
        if path is not None:
            return [waiter, *path]
    return None


def _path(graph: Graph, start: str, goal: str) -> list[str] | None:
    """A shortest path from ``start`` to ``goal`` along waits, both ends included."""
    if start == goal:
        return [start]
    previous: dict[str, str] = {}
    queue = deque([start])
    seen = {start}
    while queue:
        node = queue.popleft()
        for following in sorted(graph.get(node, ())):
            if following in seen:
                continue
            previous[following] = node
            if following == goal:
                path = [goal]
                while path[-1] != start:
                    path.append(previous[path[-1]])
                return list(reversed(path))
            seen.add(following)
            queue.append(following)
    return None


def cycles(ctx: HubContext, txn: Txn) -> list[list[str]]:
    """Every task that waits on itself through other tasks, for the invariant check."""
    graph = waits_on(ctx, txn)
    found = []
    for task_id in sorted(graph):
        for provider in sorted(graph[task_id]):
            path = _path(graph, provider, task_id)
            if path is not None:
                found.append([task_id, *path])
                break
    return found
