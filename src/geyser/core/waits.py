"""Pausing, wait conditions and resumption (DESIGN.md section 12).

A pause names what ends it: another task's results (``subscriptions``), the waiter's
own submission's result (``publications``, ``validations``,
``completion_declaration_id``), or an answer (``answers``,
``external_response_id``). The Hub evaluates conditions from its own records; a
message claiming success is not such a record. Waiting runs no inference: the
scheduler resumes a waiter when its condition holds, the Waker judges that a
subscribed task's publications meet its need, a subscribed task ends, an answer
deadline passes, a waking message arrives, or the operator resumes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from geyser.clock import parse_iso
from geyser.core import agents, candidates, choices, inbox, tasks
from geyser.core.actors import Actor
from geyser.core.context import HubContext
from geyser.core.ledger import Txn, dumps, loads
from geyser.core.models import Checkpoint, Predicate, RequirementTerm
from geyser.errors import Conflict, Forbidden, InvalidArgument
from geyser.ids import Kind, kind_of

log = logging.getLogger(__name__)

ANSWERED_REQUEST_STATES = frozenset({"answered", "resolved"})


# ------------------------------------------------------------------------- pausing
def pause(ctx: HubContext, txn: Txn, actor: Actor, task_id: str, reason: str,
          checkpoint: Checkpoint, predicate: Predicate | None, *, explicit: bool = True
          ) -> dict[str, Any]:
    """Pause one's task with a checkpoint and a condition that ends the wait.

    ``explicit`` is False for the implicit wait after ``contribute``, which the Hub
    builds itself; only an explicit pause is checked as the agent's own choice and
    cancels the task's unused recruitment place (section 6.3).
    """
    membership = txn.one(
        "SELECT * FROM memberships WHERE task_id = ? AND agent_id = ? AND state = 'active'",
        (task_id, actor.id),
    )
    if membership is None:
        raise Conflict(f"{actor.id} is not actively executing {task_id}.")
    if not reason.strip():
        raise InvalidArgument("A pause needs a reason.")
    if predicate is None:
        raise _condition_required()
    if explicit:
        check_condition(ctx, txn, actor.id, task_id, predicate)
    else:
        _check_resolves(txn, predicate)
    txn.execute(
        "UPDATE memberships SET state = 'waiting', checkpoint = ?, predicate = ?, "
        "wait_reason = ?, waiting_since = ? WHERE task_id = ? AND agent_id = ?",
        (dumps(checkpoint.model_dump()), dumps(predicate.model_dump()), reason, txn.now,
         task_id, actor.id),
    )
    agents.set_intent(txn, actor.id, "none")
    # Notices it has not read wake it no more; it reads them when it next works.
    txn.execute("UPDATE inbox SET wakes = 0 WHERE agent_id = ? AND state = 'pending' AND "
                "item = 'message' AND ref IN (SELECT id FROM messages WHERE form = 'notice')",
                (actor.id,))
    from geyser.core import completion

    completion.lapse_feedback(ctx, txn, actor.id)
    if explicit:
        from geyser.core import teams

        teams.cancel_collaborator_places(txn, task_id, "task paused")
        if txn.scalar("SELECT recruitment FROM tasks WHERE id = ?", (task_id,)) == "undecided":
            txn.execute("UPDATE tasks SET recruitment = 'no' WHERE id = ?", (task_id,))
    tasks.refresh_state(ctx, txn, task_id)
    txn.event("task.paused", task_id, actor.id, reason=reason, predicate=predicate.model_dump())
    txn.after_commit(ctx.wake)
    return {"task": tasks.title_of(txn, task_id), "paused": True,
            "wakes_on": describe(predicate)}


def pause_review(ctx: HubContext, txn: Txn, actor: Actor, review_id: str, reason: str,
                 checkpoint: Checkpoint, predicate: Predicate | None) -> dict[str, Any]:
    """A reviewer pauses its open review until an answer comes (section 8.4)."""
    review = txn.one("SELECT state, reviewer FROM reviews WHERE id = ?", (review_id,))
    if review is None or review["reviewer"] != actor.id or review["state"] != "open":
        raise Conflict(f"{actor.id} has no open review {review_id}.")
    if predicate is None:
        raise _condition_required()
    others = predicate.model_copy(update={"answers": [], "external_response_id": None})
    if others.has_terms():
        raise InvalidArgument(
            "A reviewer's pause waits for an answer: `answers` (questions you asked with "
            "talk_send) or `external_response_id` (your request to the operator).",
            details={"reason": "review_waits_for_answers"})
    _check_answer_terms(txn, actor.id, predicate)
    wait = {"reason": reason, "checkpoint": checkpoint.model_dump(),
            "predicate": predicate.model_dump(), "since": txn.now}
    txn.execute("UPDATE agents SET review_wait = ?, intent = 'none' WHERE id = ?",
                (dumps(wait), actor.id))
    txn.event("review.paused", review_id, actor.id, reason=reason)
    txn.after_commit(ctx.wake)
    return {"review": review_id, "paused": True, "wakes_on": describe(predicate)}


def _condition_required() -> InvalidArgument:
    return InvalidArgument(
        "A pause names what ends it: another task's results (`subscriptions`), your own "
        "submission's result (`publications`, `validations`, "
        "`completion_declaration_id`), or an answer (`answers`, `external_response_id`). "
        "With nothing left to do in the task, leave it instead; an executor can invite "
        "you back.", details={"reason": "wait_condition_required"})


def describe(predicate: Predicate) -> str:
    """What ends a wait, in words, for the pausing agent's reply."""
    parts = [f"{s.task_id} publishing what you need" for s in predicate.subscriptions]
    parts += [f"the outcome of {c}" for c in predicate.publications]
    parts += [f"the validation of {c}" for c in predicate.validations]
    parts += [f"the answer to {q}" for q in predicate.answers]
    if predicate.external_response_id:
        parts.append(f"the operator's answer to {predicate.external_response_id}")
    if predicate.completion_declaration_id:
        parts.append(f"the decision on {predicate.completion_declaration_id}")
    parts += [f"{term.provider} (a wait recorded before subscriptions)"
              for term in predicate.requirements]
    joined = "; ".join(parts)
    if predicate.waits_for_answer():
        joined += "; or the answer deadline"
    return joined


# ----------------------------------------------------------------------- checking
def check_condition(ctx: HubContext, txn: Txn, agent_id: str, task_id: str,
                    predicate: Predicate) -> None:
    """Refuse a pause condition that cannot end the way the agent means it to."""
    if predicate.requirements:
        raise InvalidArgument(
            "A pause takes no `requirements`. Subscribe to the task with `subscriptions` "
            "(its ID and your need in words), or wait on your own candidate with "
            "`publications`.", details={"reason": "pause_requirements_removed"})
    for candidate_id in predicate.publications:
        row = _own_candidate(txn, candidate_id, task_id)
        outcome = candidate_outcome(ctx, txn, row["id"])
        if outcome is not None:
            raise Conflict(f"{candidate_id} already has its outcome ({outcome}); there is "
                           "nothing to wait for.", details={"reason": "publication_settled"})
    for candidate_id in predicate.validations:
        _own_candidate(txn, candidate_id, task_id)
    _check_answer_terms(txn, agent_id, predicate)
    if predicate.completion_declaration_id and txn.one(
        "SELECT 1 FROM declarations WHERE id = ?", (predicate.completion_declaration_id,)
    ) is None:
        raise InvalidArgument(f"{predicate.completion_declaration_id} does not exist.")
    _check_subscriptions(ctx, txn, task_id, predicate)


def _own_candidate(txn: Txn, candidate_id: str, task_id: str):
    row = txn.one("SELECT id, task_id FROM candidates WHERE id = ?", (candidate_id,))
    if row is None:
        raise InvalidArgument(f"{candidate_id} does not exist.")
    if row["task_id"] != task_id:
        raise Forbidden(f"{candidate_id} belongs to {row['task_id']}, not your task. To wait "
                        f"for another task's results, subscribe to {row['task_id']}.",
                        details={"reason": "publication_not_own"})
    return row


def _check_answer_terms(txn: Txn, agent_id: str, predicate: Predicate) -> None:
    for question_id in predicate.answers:
        row = txn.one("SELECT sender, form FROM messages WHERE id = ?", (question_id,))
        if row is None:
            raise InvalidArgument(f"{question_id} does not exist.")
        if row["form"] != "question" or row["sender"] != agent_id:
            raise Forbidden(f"{question_id} is not a question you asked. Ask with talk_send "
                            "(kind `question`) and wait on its ID.",
                            details={"reason": "question_not_yours"})
    if predicate.external_response_id and txn.one(
        "SELECT 1 FROM requests WHERE id = ?", (predicate.external_response_id,)
    ) is None:
        raise InvalidArgument(f"{predicate.external_response_id} does not exist.")


def _check_subscriptions(ctx: HubContext, txn: Txn, task_id: str, predicate: Predicate
                         ) -> None:
    providers = [subscription.task_id for subscription in predicate.subscriptions]
    if len(providers) != len(set(providers)):
        raise InvalidArgument("Subscribe to each task once; put everything you need from it "
                              "in one `need`.")
    for provider in providers:
        if provider == task_id:
            raise InvalidArgument("A task does not subscribe to itself.",
                                  details={"reason": "subscription_cycle",
                                           "cycle": [task_id, task_id]})
        row = txn.one("SELECT state FROM tasks WHERE id = ?", (provider,))
        if row is None:
            raise InvalidArgument(f"{provider} does not exist.")
        if row["state"] in tasks.TERMINAL_STATES:
            raise Conflict(f"{tasks.title_of(txn, provider)} is {row['state']}; read what it "
                           "published in the Library instead of waiting for it.",
                           details={"reason": "subscription_settled"})
    if providers:
        from geyser.core import wait_graph

        cycle = wait_graph.cycle_through(ctx, txn, task_id, providers)
        if cycle:
            names = " → ".join(tasks.title_of(txn, node) for node in cycle)
            raise Conflict(f"This subscription would close a cycle of tasks waiting on each "
                           f"other: {names}. Do the work yourself, or change course with "
                           "the other tasks' executors.",
                           details={"reason": "subscription_cycle", "cycle": cycle})


def _check_resolves(txn: Txn, predicate: Predicate) -> None:
    """The existence checks for a condition the Hub built itself."""
    for candidate_id in [*predicate.publications, *predicate.validations]:
        if txn.one("SELECT 1 FROM candidates WHERE id = ?", (candidate_id,)) is None:
            raise InvalidArgument(f"The condition names {candidate_id}, which does not exist.")


# --------------------------------------------------------------------- evaluation
def requirement_met(txn: Txn, term: RequirementTerm) -> bool:
    """Whether a requirement holds (section 5.6).

    An artifact requirement holds once the artifact, or its newest correction through
    ``replaces``, is accepted and published. A task requirement holds once the task
    completed the revision or a cosmetic or additive successor.
    """
    if kind_of(term.provider) is Kind.TASK:
        row = txn.one("SELECT state, completed_revision FROM tasks WHERE id = ?",
                      (term.provider,))
        if row is None or row["state"] != "completed":
            return False
        wanted = term.revision
        if wanted is None:
            return True
        completed = row["completed_revision"]
        if completed is None or completed < wanted:
            return False
        changes = txn.all(
            "SELECT change FROM task_revisions WHERE task_id = ? AND revision > ? "
            "AND revision <= ?",
            (term.provider, wanted, completed),
        )
        return all(change["change"] in {"cosmetic", "additive"} for change in changes)
    candidate_id = newest_correction(txn, term.provider)
    return txn.scalar("SELECT state FROM candidates WHERE id = ?",
                      (candidate_id,)) == "published"


def newest_correction(txn: Txn, candidate_id: str) -> str:
    seen = {candidate_id}
    current = candidate_id
    while True:
        successor = txn.scalar("SELECT corrected_by FROM candidates WHERE id = ?", (current,))
        if not successor or successor in seen:
            return current
        seen.add(successor)
        current = successor


def candidate_outcome(ctx: HubContext, txn: Txn, candidate_id: str) -> str | None:
    """A candidate's outcome, or None while it is still on its way to publication."""
    row = txn.one("SELECT state, policy, corrected_by, deferred FROM candidates WHERE id = ?",
                  (candidate_id,))
    if row is None:
        return "missing"
    state = row["state"]
    if state in ("published", "rejected", "withdrawn", "invalidated", "returned"):
        return state
    if row["corrected_by"]:
        return "superseded"
    if state == "accepted":
        if row["deferred"]:
            return "deferred"
        if not candidates.policy_publishes(ctx, row["policy"]):
            return "accepted"
    return None


def _validation_settled(txn: Txn, candidate_id: str) -> bool:
    phase = txn.scalar(
        "SELECT phase FROM validations WHERE candidate_id = ? ORDER BY attempt DESC LIMIT 1",
        (candidate_id,),
    )
    if phase == "done":
        verdict = txn.scalar(
            "SELECT verdict FROM validations WHERE candidate_id = ? ORDER BY attempt DESC "
            "LIMIT 1",
            (candidate_id,),
        )
        return verdict != "unavailable"
    state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (candidate_id,))
    return state in {"withdrawn", "invalidated", "rejected"}


def question_answered(txn: Txn, question_id: str) -> bool:
    return bool(txn.scalar("SELECT 1 FROM messages WHERE form = 'answer' AND answers = ? "
                           "LIMIT 1", (question_id,)))


@dataclass(frozen=True)
class TermState:
    """One term of a wait condition and whether it holds now."""

    kind: str
    """``subscription``, ``publication``, ``validation``, ``answer``, ``response``,
    ``declaration``, or ``requirement`` (a wait recorded before subscriptions)."""
    ref: str
    """The task, candidate, message, request or declaration the term names."""
    met: bool
    revision: int | None = None
    detail: str = ""
    """A subscription's need, or a candidate's outcome."""


def predicate_terms(ctx: HubContext, txn: Txn, predicate: Predicate, agent_id: str
                    ) -> list[TermState]:
    """Evaluate each term of a condition for the agent that waits on it.

    A subscription counts as met once its task is completed; while it is open, only
    the Waker ends that part of the wait.
    """
    terms = [TermState("requirement", term.provider, requirement_met(txn, term), term.revision)
             for term in predicate.requirements]
    for subscription in predicate.subscriptions:
        state = txn.scalar("SELECT state FROM tasks WHERE id = ?", (subscription.task_id,))
        terms.append(TermState("subscription", subscription.task_id, state == "completed",
                               detail=subscription.need))
    for candidate_id in predicate.publications:
        outcome = candidate_outcome(ctx, txn, candidate_id)
        terms.append(TermState("publication", candidate_id, outcome is not None,
                               detail=outcome or ""))
    terms += [TermState("validation", candidate, _validation_settled(txn, candidate))
              for candidate in predicate.validations]
    terms += [TermState("answer", question, question_answered(txn, question))
              for question in predicate.answers]
    if predicate.external_response_id:
        state = txn.scalar("SELECT state FROM requests WHERE id = ?",
                           (predicate.external_response_id,))
        terms.append(TermState("response", predicate.external_response_id,
                               state in ANSWERED_REQUEST_STATES))
    if predicate.completion_declaration_id:
        declaration = predicate.completion_declaration_id
        row = txn.one("SELECT * FROM declarations WHERE id = ?", (declaration,))
        if row is None:
            met = True
        elif row["declarer"] == agent_id:
            met = row["feedback"] is not None or row["state"] != "awaiting_feedback"
        else:
            met = row["state"] not in {"awaiting_feedback", "deciding"}
        terms.append(TermState("declaration", declaration, met))
    return terms


def _combined(predicate: Predicate, terms: list[TermState]) -> bool:
    """Whether the terms other than subscriptions hold, combined by ``mode``."""
    met = [term.met for term in terms if term.kind != "subscription"]
    if not met:
        return False
    return all(met) if predicate.mode == "all" else any(met)


def predicate_holds(ctx: HubContext, txn: Txn, predicate: Predicate, agent_id: str) -> bool:
    return _combined(predicate, predicate_terms(ctx, txn, predicate, agent_id))


def predicate_obsolete(txn: Txn, predicate: Predicate) -> bool:
    """An old requirement wait on a provider that can no longer deliver is reconsidered."""
    for term in predicate.requirements:
        if kind_of(term.provider) is Kind.CANDIDATE:
            current = newest_correction(txn, term.provider)
            state = txn.scalar("SELECT state FROM candidates WHERE id = ?", (current,))
            if state in {"rejected", "withdrawn", "invalidated"}:
                return True
        else:
            state = txn.scalar("SELECT state FROM tasks WHERE id = ?", (term.provider,))
            if state == "retired":
                return True
    return False


# ---------------------------------------------------------------------- resuming
@dataclass(frozen=True)
class Resumption:
    agent_id: str
    subject: str
    """The task, or the reviewer's review."""
    why: str
    notice: str


def _held_resumption(agent_id: str, subject: str, terms: list[TermState]) -> Resumption:
    outcomes = sorted({term.detail for term in terms if term.kind == "publication"})
    if outcomes:
        why = f"candidate_{outcomes[0]}" if len(outcomes) == 1 else "candidates_settled"
    else:
        why = "predicate_held"
    parts = [f"{term.ref}: {term.detail}" if term.kind == "publication" else
             f"{term.ref} answered" if term.kind in ("answer", "response") else
             f"{term.ref} done"
             for term in terms if term.met and term.kind != "subscription"]
    return Resumption(agent_id, subject, why,
                      "Your wait ended: " + "; ".join(parts) + ". Verify the exact result "
                      "before relying on it.")


def _evaluate(ctx: HubContext, txn: Txn, agent_id: str, subject: str, predicate: Predicate,
              since: str | None, deadline: timedelta | None) -> Resumption | None:
    """Whether one wait ends now, and why."""
    terms = predicate_terms(ctx, txn, predicate, agent_id)
    if _combined(predicate, terms):
        return _held_resumption(agent_id, subject, terms)
    for subscription in predicate.subscriptions:
        state = txn.scalar("SELECT state FROM tasks WHERE id = ?", (subscription.task_id,))
        if state == "completed":
            return Resumption(agent_id, subject, "subscribed_task_completed",
                              f"{tasks.title_of(txn, subscription.task_id)}, which you "
                              "subscribed to, is completed. Read its results in the Library.")
        if state == "retired":
            return Resumption(agent_id, subject, "subscribed_task_retired",
                              f"{tasks.title_of(txn, subscription.task_id)}, which you "
                              "subscribed to, was retired: every executor left, so its "
                              "results will not come unless someone republishes it.")
    if predicate_obsolete(txn, predicate):
        return Resumption(agent_id, subject, "predicate_obsolete",
                          "What your wait named can no longer be delivered (rejected, "
                          "withdrawn or retired). Reconsider your plan.")
    unanswered = [term for term in terms if term.kind in ("answer", "response") and not term.met]
    if unanswered and deadline is not None and since is not None and \
            parse_iso(since) + deadline <= txn.moment:
        minutes = int(deadline.total_seconds() // 60)
        return Resumption(agent_id, subject, "answer_deadline",
                          f"No answer to {', '.join(term.ref for term in unanswered)} came "
                          f"within the answer deadline ({minutes} minutes). Go on without "
                          "it, or ask again.")
    return None


_MEMBER_WAITS = (
    "SELECT m.task_id, m.agent_id, m.predicate, m.waiting_since FROM memberships m "
    "JOIN agents a ON a.id = m.agent_id "
    "WHERE m.state = 'waiting' AND m.predicate IS NOT NULL AND a.state = 'active' "
    "AND a.current_task = m.task_id"
)
_REVIEW_WAITS = ("SELECT id, current_review, review_wait FROM agents "
                 "WHERE review_wait IS NOT NULL AND state = 'active'")


def _deadline(ctx: HubContext, txn: Txn) -> timedelta | None:
    minutes = choices.answer_deadline_minutes(ctx, txn)
    return timedelta(minutes=minutes) if minutes else None


def _member_wait(ctx: HubContext, txn: Txn, row: Any, deadline: timedelta | None
                 ) -> Resumption | None:
    try:
        predicate = Predicate.model_validate(loads(row["predicate"]))
        return _evaluate(ctx, txn, row["agent_id"], row["task_id"], predicate,
                         row["waiting_since"], deadline)
    except Exception:  # noqa: BLE001 - one unevaluable wait never blocks the others
        _report_once(f"{row['agent_id']}:{row['task_id']}")
        return None


def _review_wait(ctx: HubContext, txn: Txn, row: Any, deadline: timedelta | None
                 ) -> Resumption | None:
    try:
        wait = loads(row["review_wait"], {})
        if not wait.get("predicate"):
            return None
        predicate = Predicate.model_validate(wait["predicate"])
        return _evaluate(ctx, txn, row["id"], row["current_review"], predicate,
                         wait.get("since"), deadline)
    except Exception:  # noqa: BLE001
        _report_once(f"{row['id']}:{row['current_review']}")
        return None


def ready_resumptions(ctx: HubContext, txn: Txn) -> list[Resumption]:
    """Waits that end now, each with why and what the waiter is told."""
    deadline = _deadline(ctx, txn)
    found = [_member_wait(ctx, txn, row, deadline) for row in txn.all(_MEMBER_WAITS)]
    found += [_review_wait(ctx, txn, row, deadline) for row in txn.all(_REVIEW_WAITS)]
    return [item for item in found if item is not None]


def resumption_for(ctx: HubContext, txn: Txn, agent_id: str, subject: str
                   ) -> Resumption | None:
    """Whether one agent's wait on ``subject`` ends now (evaluated in this transaction)."""
    deadline = _deadline(ctx, txn)
    if kind_of(subject) is Kind.REVIEW:
        row = txn.one(_REVIEW_WAITS + " AND id = ? AND current_review = ?", (agent_id, subject))
        return None if row is None else _review_wait(ctx, txn, row, deadline)
    row = txn.one(_MEMBER_WAITS + " AND m.agent_id = ? AND m.task_id = ?", (agent_id, subject))
    return None if row is None else _member_wait(ctx, txn, row, deadline)


_REPORTED: set[str] = set()


def _report_once(key: str) -> None:
    if key not in _REPORTED:
        _REPORTED.add(key)
        log.exception("the wait of %s cannot be evaluated; it is skipped", key)


def resume_membership(ctx: HubContext, txn: Txn, agent_id: str, task_id: str, why: str,
                      notice: str | None = None) -> None:
    """Turn a waiting membership (or a reviewer's pause) back into active work."""
    if notice:
        inbox.notify(txn, agent_id, notice, task_id=None if kind_of(task_id) is Kind.REVIEW
                     else task_id)
    if kind_of(task_id) is Kind.REVIEW:
        txn.execute("UPDATE agents SET review_wait = NULL, intent = 'resume' WHERE id = ?",
                    (agent_id,))
        txn.event("review.resumed", task_id, agent_id, why=why)
        return
    txn.execute("DELETE FROM subscription_marks WHERE agent_id = ? AND task_id = ?",
                (agent_id, task_id))
    txn.execute(
        "UPDATE memberships SET state = 'active', predicate = NULL, waiting_since = NULL "
        "WHERE task_id = ? AND agent_id = ? AND state = 'waiting'",
        (task_id, agent_id),
    )
    agents.set_intent(txn, agent_id, "resume")
    tasks.refresh_state(ctx, txn, task_id)
    txn.event("task.resumed", task_id, agent_id, why=why)


def resume_work(ctx: HubContext, txn: Txn, actor: Actor) -> dict[str, Any]:
    """``resume_work`` from a message turn: change the work intention once (section 12).

    It never satisfies or waives a requirement; the agent verifies results itself.
    """
    row = agents.agent_row(txn, actor.id)
    if row["review_wait"] is not None and row["current_review"]:
        resume_membership(ctx, txn, actor.id, row["current_review"], "resume_work")
        txn.after_commit(ctx.wake)
        return {"review": row["current_review"], "resumes": "after this message turn"}
    task_id = row["current_task"]
    if task_id is None:
        raise Conflict(f"{actor.id} has no current task to resume.")
    membership = txn.one(
        "SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?", (task_id, actor.id)
    )
    if membership is None or membership["state"] != "waiting":
        raise Conflict(f"{actor.id} is not waiting on {task_id}.")
    resume_membership(ctx, txn, actor.id, task_id, "resume_work")
    txn.after_commit(ctx.wake)
    return {"task": tasks.title_of(txn, task_id), "resumes": "after this message turn"}


def waiting_context(txn: Txn, agent_id: str, task_id: str) -> dict[str, Any] | None:
    row = txn.one(
        "SELECT checkpoint, predicate, wait_reason, waiting_since FROM memberships "
        "WHERE task_id = ? AND agent_id = ?",
        (task_id, agent_id),
    )
    if row is None or row["checkpoint"] is None:
        return None
    return {
        "reason": row["wait_reason"],
        "checkpoint": loads(row["checkpoint"]),
        "predicate": loads(row["predicate"]),
        "since": row["waiting_since"],
    }


def notify_request_waiters(txn: Txn, request_id: str, summary: str) -> None:
    waiters = [r["agent_id"] for r in txn.all(
        "SELECT agent_id FROM request_waiters WHERE request_id = ?", (request_id,))]
    inbox.notify_many(txn, waiters, summary, ref=request_id)
