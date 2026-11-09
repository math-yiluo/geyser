"""The operator's task view: the publication layer and the dependency layer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from geyser import operator
from geyser.core import agents, completion, messages, tasks, teams, waits
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
from geyser.errors import Conflict
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.ids import Kind
from geyser.runtime.backends.scripted import ScriptedBackend
from geyser.service import GeyserService

CHECKPOINT = Checkpoint(summary="Half done.", next_action="Finish it.")


def contract(title: str, **fields) -> TaskContract:
    return TaskContract(title=title, objective=f"Do {title}.", acceptance=["Done."], **fields)


def publish(hub: Hub, title: str, *, by: Actor = OPERATOR, parent: str | None = None,
            **fields) -> str:
    with hub.transaction() as txn:
        reply = tasks.publish(hub.ctx, txn, by, contract(title, **fields), parent_task=parent)
    return reply["task_id"]


def executor(hub: Hub, task_id: str) -> Actor:
    """A new worker that executes ``task_id``, as its task turn's actor."""
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        teams.operator_claim(hub.ctx, txn, task_id, worker)
    return Actor(worker, Capability.WORKER, task_id=task_id)


def pause(hub: Hub, actor: Actor, predicate: Predicate | None) -> None:
    assert actor.task_id is not None
    with hub.transaction() as txn:
        waits.pause(hub.ctx, txn, actor, actor.task_id, "waiting", CHECKPOINT, predicate)


def links_into(graph: dict, task_id: str) -> list[dict]:
    return [link for link in graph["links"] if link["target"] == task_id]


def test_publication_layer_records_where_each_task_came_from(hub: Hub) -> None:
    operator.setup_project(hub, title="Goal", goal="Reach the goal.", acceptance=["Reached."])
    worker = executor(hub, "task-0")
    subtask = publish(hub, "Part", by=worker, parent="task-0")
    loose = publish(hub, "Side quest")
    with hub.transaction() as txn:
        tasks.mark_completed(hub.ctx, txn, subtask, 0)
        repair = completion.invalidate_evidence(hub.ctx, txn, subtask, "a file vanished",
                                                OPERATOR)
    nodes = {node["id"]: node for node in operator.task_graph(hub)["tasks"]}
    assert nodes["task-0"]["parent"] is None and nodes["task-0"]["publisher"] == "operator"
    assert nodes[subtask]["parent"] == "task-0" and nodes[subtask]["relation"] == "subtask"
    assert nodes[subtask]["publisher"] == worker.id
    assert nodes[loose]["parent"] is None
    assert repair is not None
    assert nodes[repair]["parent"] == subtask and nodes[repair]["relation"] == "repair"
    assert nodes["task-0"]["executors"][0]["agent"] == worker.id


def test_requirements_are_links_that_become_met(hub: Hub) -> None:
    provider = publish(hub, "Provider")
    dependent = publish(hub, "Dependent",
                        requirements=[Requirement(key="base", provider=provider)])
    [link] = links_into(operator.task_graph(hub), dependent)
    assert (link["source"], link["kind"], link["met"]) == (provider, "requirement", False)
    assert "requirement:base" in link["detail"]
    with hub.transaction() as txn:
        tasks.mark_completed(hub.ctx, txn, provider, 0)
    [link] = links_into(operator.task_graph(hub), dependent)
    assert link["met"] is True


def test_waits_on_the_operator_propagate_through_requirements(hub: Hub) -> None:
    asking = publish(hub, "Asking")
    downstream = publish(hub, "Downstream",
                         requirements=[Requirement(key="answer", provider=asking)])
    actor = executor(hub, asking)
    with hub.transaction() as txn:
        request = messages.create_request(hub.ctx, txn, actor, "question", "Which norm?",
                                          "Which norm should the estimate use?", asking, 0)
    pause(hub, actor, Predicate(external_response_id=request["request"]))
    graph = operator.task_graph(hub)
    [link] = links_into(graph, asking)
    assert (link["source"], link["kind"], link["agent"]) == ("operator", "wait", actor.id)
    assert "Which norm?" in link["detail"]
    assert graph["waiting_on_operator"] == [asking, downstream]
    operator.answer_request(hub, request["request"], "The L2 norm.")
    assert operator.task_graph(hub)["waiting_on_operator"] == []


def legacy_wait(hub: Hub, actor: Actor, predicate: Predicate | None) -> None:
    """A wait recorded before waits named their end and cycles were refused."""
    with hub.transaction() as txn:
        txn.execute("UPDATE memberships SET state = 'waiting', checkpoint = ?, predicate = ?, "
                    "wait_reason = 'waiting', waiting_since = ? WHERE task_id = ? AND "
                    "agent_id = ?", (dumps(CHECKPOINT.model_dump()),
                                     dumps(predicate.model_dump()) if predicate else None,
                                     txn.now, actor.task_id, actor.id))


def test_a_subscription_that_closes_a_cycle_is_refused(hub: Hub) -> None:
    first = publish(hub, "First")
    second = publish(hub, "Second", requirements=[Requirement(key="first", provider=first)])
    actor = executor(hub, first)
    with pytest.raises(Conflict) as refused:
        pause(hub, actor, Predicate(subscriptions=[Subscription(task_id=second,
                                                                need="its result")]))
    assert refused.value.details == {"reason": "subscription_cycle",
                                     "cycle": [first, second, first]}
    assert operator.task_graph(hub)["deadlocks"] == []


def test_a_cycle_from_an_old_wait_is_shown_as_a_deadlock(hub: Hub) -> None:
    first = publish(hub, "First")
    second = publish(hub, "Second", requirements=[Requirement(key="first", provider=first)])
    actor = executor(hub, first)
    legacy_wait(hub, actor, Predicate(requirements=[RequirementTerm(provider=second)]))
    publish(hub, "Unrelated")
    graph = operator.task_graph(hub)
    assert graph["deadlocks"] == [[first, second]]


def test_a_subscription_links_with_its_need(hub: Hub) -> None:
    provider = publish(hub, "Provider")
    waiter = publish(hub, "Waiter")
    actor = executor(hub, waiter)
    pause(hub, actor, Predicate(subscriptions=[Subscription(task_id=provider,
                                                            need="the main lemma")]))
    [link] = links_into(operator.task_graph(hub), waiter)
    assert (link["source"], link["kind"], link["agent"], link["met"]) == (
        provider, "wait", actor.id, False)
    assert "the main lemma" in link["detail"]


def test_waits_that_involve_no_other_task_are_notes(hub: Hub) -> None:
    task = publish(hub, "Alone")
    actor = executor(hub, task)
    with hub.transaction() as txn:
        peer = agents.create(hub.ctx, txn, Kind.WORKER)
        question = messages.send_talk(hub.ctx, txn, actor, peer, "Which case first?",
                                      form="question")["message"]
    pause(hub, actor, Predicate(answers=[question]))
    graph = operator.task_graph(hub)
    assert links_into(graph, task) == []
    [note] = [note for note in graph["notes"] if note["task"] == task]
    assert (note["kind"], note["agent"], note["met"]) == ("answer", actor.id, False)
    assert peer in note["detail"]


def test_an_old_pause_without_a_condition_is_a_note(hub: Hub) -> None:
    task = publish(hub, "Alone")
    actor = executor(hub, task)
    legacy_wait(hub, actor, None)
    [note] = [note for note in operator.task_graph(hub)["notes"] if note["task"] == task]
    assert (note["kind"], note["agent"], note["met"]) == ("resume", actor.id, False)


def test_an_unreadable_contract_hides_nothing_else(hub: Hub) -> None:
    broken = publish(hub, "Broken")
    provider = publish(hub, "Provider")
    dependent = publish(hub, "Dependent",
                        requirements=[Requirement(key="base", provider=provider)])
    hub.layout.revision_file(broken, 0).write_text("{not json", encoding="utf-8")
    graph = operator.task_graph(hub)
    assert graph["unreadable"] == [broken]
    assert {node["id"] for node in graph["tasks"]} == {broken, provider, dependent}
    assert len(links_into(graph, dependent)) == 1


def test_the_dashboard_files_and_the_graph_are_served(hub: Hub) -> None:
    service = GeyserService(hub, {"codex": ScriptedBackend({}, name="codex")}, FakeHost())
    from geyser.api.app import create_app

    client = TestClient(create_app(service, "t"), base_url="http://127.0.0.1")
    headers = {"Authorization": "Bearer t"}
    page = client.get("/", headers=headers)
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert '<script src="/dashboard.js"' in page.text
    script = client.get("/dashboard.js", headers=headers)
    assert script.headers["content-type"].startswith("text/javascript")
    assert client.get("/dashboard.css", headers=headers).status_code == 200
    assert client.get("/dashboard.js").status_code == 401
    publish(hub, "Only")
    graph = client.get("/v1/task-graph", headers=headers).json()
    assert [node["id"] for node in graph["tasks"]] == ["task-0"]
