"""Waits that name their end, the Waker, message kinds and invitations back (sections 6.2,
11 and 12)."""

from __future__ import annotations

import pytest

from geyser.core import agents, choices, inbox, messages, tasks, teams, waits, wakes
from geyser.core.actors import OPERATOR, Actor, Capability
from geyser.core.ledger import dumps
from geyser.core.models import (
    Checkpoint,
    Predicate,
    Requirement,
    RequirementTerm,
    Subscription,
    TaskContract,
)
from geyser.errors import Conflict, Forbidden, InvalidArgument
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.ids import Kind
from geyser.runtime.backends.scripted import ScriptedBackend
from geyser.scheduler.decisions import DecisionRunner

CHECKPOINT = Checkpoint(summary="Half done.", next_action="Finish it.")


def publish(hub: Hub, title: str, **fields) -> str:
    contract = TaskContract(title=title, objective=f"Do {title}.", acceptance=["Done."],
                            **fields)
    with hub.transaction() as txn:
        return tasks.publish(hub.ctx, txn, OPERATOR, contract)["task_id"]


def executor(hub: Hub, task_id: str) -> Actor:
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        teams.operator_claim(hub.ctx, txn, task_id, worker)
    return Actor(worker, Capability.WORKER, task_id=task_id)


def pause(hub: Hub, actor: Actor, predicate: Predicate | None) -> None:
    assert actor.task_id is not None
    with hub.transaction() as txn:
        waits.pause(hub.ctx, txn, actor, actor.task_id, "waiting", CHECKPOINT, predicate)


def ready(hub: Hub) -> dict[str, str]:
    with hub.read() as txn:
        return {found.agent_id: found.why for found in waits.ready_resumptions(hub.ctx, txn)}


def membership(hub: Hub, actor: Actor) -> str:
    with hub.read() as txn:
        return txn.scalar("SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?",
                          (actor.task_id, actor.id))


def published(hub: Hub, task_id: str, author: str) -> str:
    """Record a publication of a candidate of ``task_id``, as integration would."""
    with hub.transaction() as txn:
        candidate = txn.next_id(Kind.CANDIDATE)
        publication = txn.next_id(Kind.PUBLICATION)
        txn.execute("INSERT INTO publications (id, mode, members, commit_id, state, created_at, "
                    "committed_at) VALUES (?, 'direct', ?, ?, 'committed', ?, ?)",
                    (publication, dumps([candidate]), "c" * 40, txn.now, txn.now))
        txn.execute("INSERT INTO candidates (id, task_id, task_revision, kind, policy, "
                    "submitter, authors, paths, base_commit, manifest_hash, state, created_at, "
                    "published_commit, publication_id) VALUES (?, ?, 0, 'general.work', "
                    "'general.review@1', ?, ?, '[\"part.md\"]', 'b', 'h', 'published', ?, ?, ?)",
                    (candidate, task_id, author, dumps([author]), txn.now, "c" * 40,
                     publication))
    return candidate


# ------------------------------------------------------------------ pause rules
def test_a_pause_names_what_ends_it(hub: Hub) -> None:
    actor = executor(hub, publish(hub, "Work"))
    with pytest.raises(InvalidArgument) as refused:
        pause(hub, actor, None)
    assert refused.value.details["reason"] == "wait_condition_required"
    provider = publish(hub, "Provider")
    with pytest.raises(InvalidArgument) as refused:
        pause(hub, actor, Predicate(requirements=[RequirementTerm(provider=provider)]))
    assert refused.value.details["reason"] == "pause_requirements_removed"
    assert membership(hub, actor) == "active"


def test_publications_are_the_waiters_own_and_on_their_way(hub: Hub, clock) -> None:
    own_task, other_task = publish(hub, "Own"), publish(hub, "Other")
    actor = executor(hub, own_task)
    other = executor(hub, other_task)
    theirs = published(hub, other_task, other.id)
    with pytest.raises(Forbidden) as refused:
        pause(hub, actor, Predicate(publications=[theirs]))
    assert refused.value.details["reason"] == "publication_not_own"
    mine = published(hub, own_task, actor.id)
    with pytest.raises(Conflict) as settled:
        pause(hub, actor, Predicate(publications=[mine]))
    assert settled.value.details["reason"] == "publication_settled"


def test_subscriptions_to_oneself_or_a_finished_task_are_refused(hub: Hub) -> None:
    task, done = publish(hub, "Mine"), publish(hub, "Done")
    actor = executor(hub, task)
    with pytest.raises(InvalidArgument):
        pause(hub, actor, Predicate(subscriptions=[Subscription(task_id=task, need="x")]))
    with hub.transaction() as txn:
        tasks.mark_completed(hub.ctx, txn, done, 0)
    with pytest.raises(Conflict) as refused:
        pause(hub, actor, Predicate(subscriptions=[Subscription(task_id=done, need="x")]))
    assert refused.value.details["reason"] == "subscription_settled"


def test_a_revision_that_closes_a_cycle_is_refused(hub: Hub) -> None:
    first = publish(hub, "First")
    second = publish(hub, "Second", requirements=[Requirement(key="first", provider=first)])
    with hub.transaction() as txn:
        contract = tasks.load_contract(hub.ctx, txn, first).model_copy(update={
            "requirements": [Requirement(key="second", provider=second)]})
        with pytest.raises(Conflict) as refused:
            tasks.revise(hub.ctx, txn, OPERATOR, first, 0, contract, "needs the second")
    assert refused.value.details == {"reason": "requirement_cycle",
                                     "cycle": [first, second, first]}


def test_a_requirement_names_no_policy() -> None:
    requirement = Requirement.model_validate({"key": "base", "provider": "cand-3",
                                              "policy": "general.review@1"})
    assert "policy" not in requirement.model_dump()
    contract = TaskContract.model_validate({
        "title": "T", "objective": "Do it.", "acceptance": ["Done."],
        "requirements": [{"key": "base", "provider": "task-2", "policy": None}]})
    assert contract.conditions()["requirement:base"] == "task-2"


# ------------------------------------------------------------- what ends a wait
def test_a_subscribed_task_ending_ends_the_wait(hub: Hub) -> None:
    provider, retired_provider = publish(hub, "Provider"), publish(hub, "Gone")
    first = executor(hub, publish(hub, "First"))
    second = executor(hub, publish(hub, "Second"))
    pause(hub, first, Predicate(subscriptions=[Subscription(task_id=provider, need="x")]))
    pause(hub, second, Predicate(subscriptions=[Subscription(task_id=retired_provider,
                                                             need="y")]))
    assert ready(hub) == {}
    leaver = executor(hub, retired_provider)
    with hub.transaction() as txn:
        tasks.mark_completed(hub.ctx, txn, provider, 0)
        teams.leave(hub.ctx, txn, leaver, retired_provider, "nothing left")
    assert ready(hub) == {first.id: "subscribed_task_completed",
                          second.id: "subscribed_task_retired"}


def test_an_answer_ends_the_wait_and_the_deadline_ends_it_without_one(hub: Hub,
                                                                       clock) -> None:
    asker = executor(hub, publish(hub, "Asks"))
    patient = executor(hub, publish(hub, "Waits long"))
    with hub.transaction() as txn:
        peer = agents.create(hub.ctx, txn, Kind.WORKER)
        question = messages.send_talk(hub.ctx, txn, asker, peer, "Which case?",
                                      form="question")
        unanswered = messages.send_talk(hub.ctx, txn, patient, peer, "And this?",
                                        form="question")["message"]
    assert question["wait_with"] == {"answers": [question["message"]]}
    pause(hub, asker, Predicate(answers=[question["message"]]))
    pause(hub, patient, Predicate(answers=[unanswered]))
    with hub.transaction() as txn:
        messages.send_talk(hub.ctx, txn, Actor(peer, Capability.WORKER), asker.id,
                           "The first one.", form="answer", answers=question["message"])
    assert ready(hub) == {asker.id: "predicate_held"}
    with hub.read() as txn:
        minutes = choices.answer_deadline_minutes(hub.ctx, txn)
    assert minutes is not None
    clock.advance((minutes + 1) * 60)
    assert ready(hub)[patient.id] == "answer_deadline"


# ---------------------------------------------------------------------- the Waker
def waker(hub: Hub, answers: list) -> DecisionRunner:
    """A decision runner whose Waker answers from ``answers`` in turn."""
    remaining = list(answers)

    def decide(request):
        answer = remaining.pop(0)
        return answer(request) if callable(answer) else answer

    backend = ScriptedBackend({}, decide, name="codex")
    return DecisionRunner(hub, {"codex": backend}, FakeHost())


def everything(value: bool):
    return lambda request: {key: value for key in request.schema["required"]}


async def test_the_waker_judges_each_publication_once(hub: Hub, clock) -> None:
    from geyser import operator

    operator.run_action(hub, "start")
    provider = publish(hub, "Provider")
    author = executor(hub, provider)
    actor = executor(hub, publish(hub, "Waiter"))
    pause(hub, actor, Predicate(subscriptions=[Subscription(task_id=provider,
                                                            need="the main lemma")]))
    runner = waker(hub, [everything(False), everything(True)])
    assert await runner.run_pending() == 0  # Nothing published yet: no decision.
    clock.advance(1)
    published(hub, provider, author.id)
    assert await runner.run_pending() == 1
    assert membership(hub, actor) == "waiting"  # Judged not to meet the need.
    with hub.read() as txn:
        assert wakes.pending_pairs(hub.ctx, txn) == []  # Not shown again.
    clock.advance(1)
    published(hub, provider, author.id)
    assert await runner.run_pending() == 1
    assert membership(hub, actor) == "active"
    with hub.read() as txn:
        why = txn.scalar("SELECT data FROM events WHERE kind = 'task.resumed' AND actor = ?",
                         (actor.id,))
        notices = [item["summary"] for item in inbox.pending(txn, actor.id)]
    assert "subscribed_task_changed" in why
    assert any("may meet your need" in notice for notice in notices)


async def test_a_waker_that_cannot_answer_wakes_every_wait(hub: Hub, clock) -> None:
    from geyser import operator

    operator.run_action(hub, "start")
    provider = publish(hub, "Provider")
    author = executor(hub, provider)
    actor = executor(hub, publish(hub, "Waiter"))
    pause(hub, actor, Predicate(subscriptions=[Subscription(task_id=provider, need="x")]))
    clock.advance(1)
    published(hub, provider, author.id)
    runner = waker(hub, [{"p0": "maybe"}] * wakes.MAX_ATTEMPTS)
    for _ in range(wakes.MAX_ATTEMPTS):
        assert membership(hub, actor) == "waiting"
        await runner.run_pending()
    assert membership(hub, actor) == "active"
    with hub.read() as txn:
        notice = txn.scalar("SELECT message FROM operator_notices WHERE signature = "
                            "'waker.failed'")
    assert actor.id in notice


# ------------------------------------------------------------------ message kinds
def test_a_notice_wakes_no_worker_but_a_question_does(hub: Hub) -> None:
    sender = executor(hub, publish(hub, "Sender"))
    waiter = executor(hub, publish(hub, "Waiter"))
    with hub.transaction() as txn:
        request = messages.create_request(hub.ctx, txn, waiter, "question", "Q?", "Which?",
                                          waiter.task_id, 0)["request"]
    pause(hub, waiter, Predicate(external_response_id=request))
    with hub.transaction() as txn:
        messages.send_talk(hub.ctx, txn, sender, waiter.id, "FYI: I renamed a file.")
        assert not inbox.has_waking(txn, waiter.id)
        messages.send_talk(hub.ctx, txn, sender, waiter.id, "Can you check it?",
                           form="question")
        assert inbox.has_waking(txn, waiter.id)


def test_only_the_agent_asked_answers_and_only_to_the_asker(hub: Hub) -> None:
    asker = executor(hub, publish(hub, "Asks"))
    with hub.transaction() as txn:
        asked = agents.create(hub.ctx, txn, Kind.WORKER)
        stranger = agents.create(hub.ctx, txn, Kind.WORKER)
        question = messages.send_talk(hub.ctx, txn, asker, asked, "Which?",
                                      form="question")["message"]
    with hub.transaction() as txn, pytest.raises(Forbidden):
        messages.send_talk(hub.ctx, txn, Actor(stranger, Capability.WORKER), asker.id,
                           "Mine.", form="answer", answers=question)
    with hub.transaction() as txn, pytest.raises(InvalidArgument):
        messages.send_talk(hub.ctx, txn, Actor(asked, Capability.WORKER), stranger,
                           "Yours.", form="answer", answers=question)
    with hub.transaction() as txn:
        reply = messages.send_talk(hub.ctx, txn, Actor(asked, Capability.WORKER), asker.id,
                                   "This one.", form="answer", answers=question)
        assert waits.question_answered(txn, question)
    assert reply["kind"] == "answer"


# ------------------------------------------------------------------ invite back
def test_a_worker_that_left_can_be_invited_back(hub: Hub) -> None:
    hub.config.teams.collaborators = 1
    task = publish(hub, "Shared")
    first = executor(hub, task)
    with hub.transaction() as txn:
        second_id = agents.create(hub.ctx, txn, Kind.WORKER)
        txn.execute("INSERT INTO opportunities (task_id, role, state, created_at) VALUES "
                    "(?, 'collaborator', 'open', ?)", (task, txn.now))
        teams.operator_claim(hub.ctx, txn, task, second_id)
        teams.leave(hub.ctx, txn, Actor(second_id, Capability.WORKER, task_id=task),
                    task, "my part is done")
    with hub.transaction() as txn:
        stranger = agents.create(hub.ctx, txn, Kind.WORKER)
        with pytest.raises(Forbidden):
            teams.invite_back(hub.ctx, txn, first, task, stranger, "help")
        with pytest.raises(Forbidden):
            teams.invite_back(hub.ctx, txn, Actor(stranger, Capability.WORKER), task,
                              second_id, "come back")
        reply = teams.invite_back(hub.ctx, txn, first, task, second_id, "a new defect")
        assert reply["invited_back"] == second_id
        row = agents.agent_row(txn, second_id)
        assert (row["current_task"], row["intent"]) == (task, "resume")
        assert txn.scalar("SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?",
                          (task, second_id)) == "active"
        with pytest.raises(Conflict):
            teams.invite_back(hub.ctx, txn, first, task, second_id, "again")


def test_a_busy_worker_is_not_invited_back(hub: Hub) -> None:
    task, other = publish(hub, "Shared"), publish(hub, "Other")
    first = executor(hub, task)
    with hub.transaction() as txn:
        second = agents.create(hub.ctx, txn, Kind.WORKER)
        txn.execute("INSERT INTO memberships (task_id, agent_id, role, state, joined_at, "
                    "ended_at) VALUES (?, ?, 'collaborator', 'left', ?, ?)",
                    (task, second, txn.now, txn.now))
        teams.operator_claim(hub.ctx, txn, other, second)
        with pytest.raises(Conflict) as refused:
            teams.invite_back(hub.ctx, txn, first, task, second, "come back")
    assert refused.value.details["reason"] == "worker_busy"
