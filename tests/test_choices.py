"""Choices before a run, backend moves, the Integrator's group, and smaller compatibility
rules (sections 6.1, 7.3 and 15.1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from geyser import operator
from geyser.clock import ManualClock
from geyser.config import ProjectConfig
from geyser.core import activations, agents, controls, teams
from geyser.core.actors import Capability
from geyser.core.ledger import Ledger
from geyser.errors import Conflict, InvalidArgument
from geyser.gateway import Gateway
from geyser.hub import Hub
from geyser.ids import Kind


@pytest.fixture
def bare_hub(tmp_path: Path) -> Hub:
    """A project whose operator has chosen nothing yet."""
    instance = Hub.initialize(tmp_path / "bare", ProjectConfig(name="bare"), clock=ManualClock())
    yield instance
    instance.close()


def test_a_run_starts_only_once_every_choice_is_made(bare_hub: Hub) -> None:
    hub = bare_hub
    with pytest.raises(Conflict) as refused:
        operator.run_action(hub, "start")
    missing = refused.value.details["missing"]
    assert [entry.split(":")[0] for entry in missing] == [
        "workers.codex", "reviewers", "dispatcher", "waker", "answer_deadline"]
    assert operator.status(hub)["missing_choices"] == missing
    operator.choose(hub, "workers.codex", model="m", effort="high")
    for role in ("reviewers", "dispatcher", "waker"):
        operator.choose(hub, role, model="m", effort="medium")
    left = operator.choose(hub, "answer_deadline", minutes=90)["missing"]
    assert left == []
    assert operator.run_action(hub, "start")["run"] == controls.RUNNING


def test_the_integrator_is_chosen_in_batch_mode_and_every_worker_backend(bare_hub: Hub) -> None:
    hub = bare_hub
    hub.config.integration.mode = "batch"
    hub.config.assignment.worker_backends = ["codex", "claude"]
    roles = [entry.split(":")[0] for entry in operator.choice_overview(hub)["missing"]]
    assert "integrator" in roles and "workers.claude" in roles


def test_a_runtime_choice_overrides_the_file(hub: Hub) -> None:
    operator.choose(hub, "workers.codex", model="other-model")
    with hub.read() as txn:
        from geyser.core import choices

        chosen = choices.for_agent(hub.ctx, txn, "worker", "codex")
    assert (chosen.model, chosen.effort) == ("other-model", "low")
    with pytest.raises(InvalidArgument):
        operator.choose(hub, "workers.codex", model=" ")


def test_resuming_an_agent_or_a_group_also_needs_the_choices(bare_hub: Hub) -> None:
    with bare_hub.transaction() as txn:
        worker = agents.create(bare_hub.ctx, txn, Kind.WORKER)
    with pytest.raises(Conflict):
        operator.agent_action(bare_hub, worker, "resume")
    with pytest.raises(Conflict):
        operator.group_action(bare_hub, "codex.workers", "resume")
    operator.group_action(bare_hub, "integrator", "pause_after_turn")  # Pausing needs none.


# ------------------------------------------------------------------- backend moves
def test_an_idle_worker_moves_at_once(hub: Hub) -> None:
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        txn.execute("UPDATE agents SET conversation_id = 'thread-1' WHERE id = ?", (worker,))
    assert operator.move_agent(hub, worker, "claude")["move"] == "done"
    with hub.read() as txn:
        row = agents.agent_row(txn, worker)
    assert (row["backend"], row["move_to"], row["conversation_id"]) == ("claude", None, None)


def test_a_worker_in_a_turn_moves_when_the_turn_ends(hub: Hub) -> None:
    operator.setup_project(hub, title="Goal", goal="Do it.", acceptance=["Done."])
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        teams.operator_claim(hub.ctx, txn, "task-0", worker)
        activation = activations.plan(txn, worker, "task", task_id="task-0")["id"]
    assert operator.move_agent(hub, worker, "claude")["move"] != "done"
    with hub.transaction() as txn:
        assert agents.agent_row(txn, worker)["backend"] == "codex"
        activations.end(txn, activation, "completed")
        assert agents.settle_moves(txn) == 1
        row = agents.agent_row(txn, worker)
        notices = txn.scalar("SELECT summary FROM inbox WHERE agent_id = ? ORDER BY id DESC",
                             (worker,))
    assert row["backend"] == "claude" and "moved you from codex to claude" in notices


def test_a_move_needs_the_target_choice_and_a_worker(bare_hub: Hub) -> None:
    with bare_hub.transaction() as txn:
        worker = agents.create(bare_hub.ctx, txn, Kind.WORKER)
        reviewer = agents.create(bare_hub.ctx, txn, Kind.REVIEWER)
    with pytest.raises(Conflict) as refused:
        operator.move_agent(bare_hub, worker, "claude")
    assert refused.value.details["reason"] == "choices_required"
    with pytest.raises(InvalidArgument):
        operator.move_agent(bare_hub, reviewer, "claude")


# --------------------------------------------------------------- the Integrator
def test_the_integrator_has_a_group_of_its_own(hub: Hub) -> None:
    operator.run_action(hub, "start")
    operator.group_action(hub, "integrator", "pause_after_turn")
    with hub.read() as txn:
        assert controls.integration_paused(txn)
        assert not controls.resolve(txn, None, "codex", "integrator").may_start
        assert controls.resolve(txn, None, "codex", "worker").may_start
    operator.run_action(hub, "pause")
    operator.group_action(hub, "integrator", "resume")
    with hub.read() as txn:
        assert controls.resolve(txn, None, "codex", "integrator").may_start
        assert not controls.resolve(txn, None, "codex", "worker").may_start
    with pytest.raises(InvalidArgument):
        operator.group_action(hub, "codex.integrator", "resume")


async def test_a_paused_integrator_group_holds_direct_integration(hub: Hub) -> None:
    from geyser.host.fake import FakeHost
    from geyser.runtime.backends.scripted import ScriptedBackend
    from geyser.service import GeyserService
    from tests.scripted import AcceptingReviewer, SimpleWorker, no_decider

    backend = ScriptedBackend({"worker": SimpleWorker(), "reviewer": AcceptingReviewer()},
                              no_decider, name="codex")
    service = GeyserService(hub, {"codex": backend}, FakeHost())
    operator.setup_project(hub, title="Goal", goal="Write a result file.",
                           acceptance=["Written."])
    operator.run_action(hub, "start")
    operator.group_action(hub, "integrator", "pause_after_turn")
    await service.run_until_idle()
    with hub.read() as txn:
        states = [row[0] for row in txn.all("SELECT state FROM candidates")]
    assert states == ["accepted"]
    operator.group_action(hub, "integrator", "resume")
    await service.run_until_idle()
    with hub.read() as txn:
        assert txn.scalar("SELECT state FROM tasks WHERE id = 'task-0'") == "completed"


# ------------------------------------------------------------------- the rest
def test_the_cheat_sheet_lists_what_a_pause_can_wait_for(hub: Hub) -> None:
    sheet = Gateway(hub).cheat_sheet(Capability.WORKER)
    line = next(line for line in sheet.splitlines() if line.startswith("- pause("))
    for term in ("subscriptions", "publications", "answers", "external_response_id"):
        assert term in line
    assert "checkpoint{summary, next_action}" in line


def test_an_older_ledger_gains_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        connection.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, kind TEXT NOT NULL, sender TEXT NOT "
            "NULL, sender_role TEXT NOT NULL, recipient TEXT NOT NULL, body_path TEXT NOT "
            "NULL, body_hash TEXT NOT NULL, summary TEXT NOT NULL, client_key TEXT, withdrawn "
            "INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    ledger = Ledger(path, ManualClock())
    try:
        with ledger.read() as txn:
            columns = {row[1] for row in txn.all("PRAGMA table_info(messages)")}
            agent_columns = {row[1] for row in txn.all("PRAGMA table_info(agents)")}
    finally:
        ledger.close()
    assert {"form", "answers"} <= columns and "move_to" in agent_columns
