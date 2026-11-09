"""How tasks relate to each other, for the operator's task view (DESIGN.md section 15.4).

The view has two layers over the same tasks:

- **Publication.** Where each task came from. A task published in a task turn hangs
  under that task, and a repair task under the task whose evidence it repairs. Any
  other task is a root: the operator published it, or an agent did outside a task turn.
- **Dependencies and waits.** What each task needs before it can go on. A contract
  requirement or an unpublished candidate input binds the task itself. A waiting
  executor's predicate, or a paused review of one of the task's candidates, binds
  that agent. Every link says whether it is met. Unmet links that form a cycle are a
  deadlock, and a task that reaches the operator through unmet links waits on the
  operator.

Everything here only reads. A task whose contract file cannot be read keeps its place
and is listed under ``unreadable``; a wait that cannot be evaluated becomes a note. Neither
hides the rest.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from sqlite3 import Row
from typing import Any

from geyser.core import tasks, waits
from geyser.core.context import HubContext
from geyser.core.ledger import Txn, loads
from geyser.core.models import Predicate, Requirement, RequirementTerm
from geyser.errors import GeyserError
from geyser.ids import Kind, kind_of, split_id

log = logging.getLogger(__name__)

OPERATOR = "operator"
"""The source of a link that only the operator's answer to a request can meet."""


@dataclass(frozen=True)
class Link:
    """``target`` needs ``source``: another task, or the operator."""

    source: str
    target: str
    kind: str
    """``requirement`` or ``input`` from the target's contract; ``wait`` from an agent's
    recorded predicate."""
    term: str
    """What exactly is needed: a task, candidate, request or declaration ID."""
    met: bool
    agent: str | None = None
    """For a wait: the agent that waits."""
    detail: str = ""


@dataclass(frozen=True)
class Note:
    """A wait or need of a task that involves no other task, e.g. on its own candidate."""

    task: str
    agent: str | None
    kind: str
    """A term kind (``publication``, ``validation``, ``declaration``, ``answer``,
    ``requirement``), ``resume`` for a pause recorded before waits named their end, or
    ``unreadable`` for a wait that cannot be evaluated."""
    term: str | None
    met: bool
    detail: str = ""


def task_graph(ctx: HubContext, txn: Txn) -> dict[str, Any]:
    """Both layers of the task view, in one consistent read."""
    rows = txn.all("SELECT * FROM tasks ORDER BY number")
    owners = _owners(txn)
    links: list[Link] = []
    notes: list[Note] = []
    unreadable: list[str] = []
    for row in rows:
        try:
            contract_links, contract_notes = _contract_needs(ctx, txn, row, owners)
        except GeyserError:
            unreadable.append(row["id"])
            continue
        links += contract_links
        notes += contract_notes
    wait_links, wait_notes = _split(list(_waits(ctx, txn, owners)))
    links += wait_links
    notes += wait_notes
    members = _members(txn)
    return {
        "tasks": [_node(row, members.get(row["id"], [])) for row in rows],
        "links": [asdict(link) for link in links],
        "notes": [asdict(note) for note in notes],
        "deadlocks": deadlocks(links),
        "waiting_on_operator": waiting_on_operator(links),
        "unreadable": unreadable,
    }


# ------------------------------------------------------------------ publication layer
def _node(row: Row, members: list[dict[str, Any]]) -> dict[str, Any]:
    relation = "subtask" if row["parent_task"] else "repair" if row["repair_of"] else None
    return {
        "id": row["id"],
        "title": row["title"],
        "state": row["state"],
        "revision": row["revision"],
        "priority": row["priority"],
        "publisher": row["publisher"],
        "parent": row["parent_task"] or row["repair_of"],
        "relation": relation,
        "executors": members,
        "evidence_valid": row["evidence_valid"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _members(txn: Txn) -> dict[str, list[dict[str, Any]]]:
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in txn.all("SELECT task_id, agent_id, role, state, wait_reason, waiting_since "
                       "FROM memberships WHERE state IN ('active', 'waiting') "
                       "ORDER BY joined_at"):
        members[row["task_id"]].append({
            "agent": row["agent_id"], "role": row["role"], "state": row["state"],
            "reason": row["wait_reason"] if row["state"] == "waiting" else None,
            "since": row["waiting_since"] if row["state"] == "waiting" else None,
        })
    return members


# ------------------------------------------------------------------- dependency layer
def _owners(txn: Txn) -> dict[str, str]:
    """The task each candidate and each completion declaration belongs to."""
    owners = {row["id"]: row["task_id"] for row in txn.all("SELECT id, task_id FROM candidates")}
    owners.update({row["id"]: row["task_id"]
                   for row in txn.all("SELECT id, task_id FROM declarations")})
    return owners


def _contract_needs(ctx: HubContext, txn: Txn, row: Row, owners: dict[str, str]
                    ) -> tuple[list[Link], list[Note]]:
    """The requirements and candidate inputs of a task's current contract."""
    task_id = row["id"]
    contract = tasks.load_contract(ctx, txn, task_id)
    found: list[Link | Note] = []
    for requirement in contract.requirements:
        term = RequirementTerm(provider=requirement.provider, revision=requirement.revision)
        found.append(_place(task_id, None, "requirement", requirement.provider,
                            waits.requirement_met(txn, term), _requirement_detail(requirement),
                            owners, link_kind="requirement"))
    for value in contract.inputs:
        if kind_of(value) is Kind.CANDIDATE:
            state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (value,))
            found.append(_place(task_id, None, "input", value, state == "published",
                                f"input {value} ({state})", owners, link_kind="input"))
    return _split(found)


def _requirement_detail(requirement: Requirement) -> str:
    detail = f"requirement:{requirement.key} on {requirement.provider}"
    if requirement.revision is not None:
        detail += f" revision {requirement.revision}"
    return detail


def _waits(ctx: HubContext, txn: Txn, owners: dict[str, str]) -> Iterable[Link | Note]:
    """The recorded waits of executors, and of reviewers on a task's candidate."""
    for row in txn.all("SELECT task_id, agent_id, predicate, wait_reason FROM memberships "
                       "WHERE state = 'waiting' ORDER BY task_id, agent_id"):
        yield from _wait_terms(ctx, txn, row["task_id"], row["agent_id"],
                               loads(row["predicate"]), row["wait_reason"] or "", owners)
    for row in txn.all(
        "SELECT a.id, a.review_wait, r.id AS review_id, r.candidate_id FROM agents a "
        "JOIN reviews r ON r.id = a.current_review WHERE a.review_wait IS NOT NULL"
    ):
        task_id = owners.get(row["candidate_id"])
        if task_id is None:
            continue
        wait = loads(row["review_wait"], {})
        reason = f"review {row['review_id']} of {row['candidate_id']}: {wait.get('reason', '')}"
        yield from _wait_terms(ctx, txn, task_id, row["id"], wait.get("predicate"), reason,
                               owners)


def _wait_terms(ctx: HubContext, txn: Txn, task_id: str, agent_id: str, predicate: Any,
                reason: str, owners: dict[str, str]) -> Iterable[Link | Note]:
    if not predicate:
        yield Note(task_id, agent_id, "resume", None, False,
                   f"{reason} (a pause from before waits named their end: until it resumes "
                   "in a message turn or you resume it)")
        return
    try:
        parsed = Predicate.model_validate(predicate)
        terms = waits.predicate_terms(ctx, txn, parsed, agent_id)
    except Exception:  # noqa: BLE001 - one unevaluable wait never hides the others
        log.exception("the wait of %s on %s cannot be evaluated", agent_id, task_id)
        yield Note(task_id, agent_id, "unreadable", None, False,
                   "This wait cannot be evaluated.")
        return
    combined = [term for term in terms if term.kind != "subscription"]
    any_of = parsed.mode == "any" and len(combined) > 1
    for term in terms:
        detail = _term_detail(txn, term)
        if any_of and term.kind != "subscription":
            detail += " (any one of its terms)"
        yield _place(task_id, agent_id, term.kind, term.ref, term.met, detail, owners,
                     link_kind="wait")


def _term_detail(txn: Txn, term: waits.TermState) -> str:
    if term.kind == "subscription":
        return f"what {term.ref} publishes: {term.detail}"
    if term.kind == "response":
        title = txn.scalar("SELECT title FROM requests WHERE id = ?", (term.ref,))
        return f"your answer to {term.ref}" + (f" ({title})" if title else "")
    if term.kind == "answer":
        asked = txn.scalar("SELECT recipient FROM messages WHERE id = ?", (term.ref,))
        return f"{asked or 'a peer'}'s answer to question {term.ref}"
    if term.kind == "publication":
        return f"the outcome of {term.ref}" + (f": {term.detail}" if term.detail else "")
    if term.kind == "validation":
        return f"validation of {term.ref}"
    if term.kind == "declaration":
        return f"the decision on {term.ref}"
    if kind_of(term.ref) is Kind.TASK:
        revision = f" revision {term.revision}" if term.revision is not None else ""
        return f"completion of {term.ref}{revision}"
    return f"publication of {term.ref}"


def _place(task_id: str, agent_id: str | None, term_kind: str, ref: str, met: bool,
           detail: str, owners: dict[str, str], *, link_kind: str) -> Link | Note:
    """A link to the task (or operator) that provides ``ref``, or a note if none other does."""
    if term_kind == "response":
        source: str | None = OPERATOR
    elif term_kind == "answer":
        source = None  # A peer's answer comes from an agent, not from a task.
    elif kind_of(ref) is Kind.TASK:
        source = ref
    else:
        source = owners.get(ref)
    if source is None or source == task_id:
        return Note(task_id, agent_id, term_kind, ref, met, detail)
    return Link(source, task_id, link_kind, ref, met, agent_id, detail)


def _split(found: list[Link | Note]) -> tuple[list[Link], list[Note]]:
    return ([item for item in found if isinstance(item, Link)],
            [item for item in found if isinstance(item, Note)])


# --------------------------------------------------------------------- what blocks
def deadlocks(links: Iterable[Link]) -> list[list[str]]:
    """Groups of tasks that need each other through unmet links, so none can go on."""
    graph = _unmet_graph(links, include_operator=False)
    reach = {node: _reachable(graph, node) for node in list(graph)}
    groups: list[list[str]] = []
    seen: set[str] = set()
    for node in sorted(reach, key=_order):
        if node in seen or node not in reach[node]:
            continue
        group = sorted((other for other in reach[node] if node in reach.get(other, ())),
                       key=_order)
        seen.update(group)
        groups.append(group)
    return groups


def waiting_on_operator(links: Iterable[Link]) -> list[str]:
    """Tasks that need an answer from the operator, directly or through other tasks."""
    graph = _unmet_graph(links, include_operator=True)
    return sorted(_reachable(graph, OPERATOR), key=_order)


def _unmet_graph(links: Iterable[Link], *, include_operator: bool) -> dict[str, set[str]]:
    """Unmet links as a graph from what is needed to what needs it."""
    graph: dict[str, set[str]] = defaultdict(set)
    for link in links:
        if link.met or link.source == link.target:
            continue
        if link.source == OPERATOR and not include_operator:
            continue
        graph[link.source].add(link.target)
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    """Every node reachable from ``start`` through at least one edge."""
    found: set[str] = set()
    queue = deque(graph.get(start, ()))
    while queue:
        node = queue.popleft()
        if node not in found:
            found.add(node)
            queue.extend(graph.get(node, ()))
    return found


def _order(task_id: str) -> tuple[int, str]:
    parsed = split_id(task_id)
    return (parsed[1] if parsed else -1, task_id)
