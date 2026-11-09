"""Shared fixtures: a Hub on a temporary project with a manual clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from geyser.clock import ManualClock
from geyser.config import ProjectConfig
from geyser.core import activations
from geyser.core.actors import Actor, Capability
from geyser.gateway import Gateway
from geyser.hub import Hub


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def chosen(config: ProjectConfig) -> ProjectConfig:
    """Make every choice a run needs (DESIGN.md section 15.1), as an operator would."""
    for section in (config.codex, config.claude, config.reviewers, config.integrator,
                    config.decisions, config.waker):
        section.model = "test-model"
        section.effort = "low"
    config.waits.answer_deadline_minutes = 60
    return config


@pytest.fixture
def hub(tmp_path: Path, clock: ManualClock) -> Hub:
    config = chosen(ProjectConfig(name="test project"))
    instance = Hub.initialize(tmp_path / "project", config, clock=clock)
    yield instance
    instance.close()


@pytest.fixture
def gateway(hub: Hub) -> Gateway:
    return Gateway(hub)


def prepare_workspace(hub: Hub, agent_id: str) -> None:
    """What the runtime does before an agent's first turn."""
    workspace = hub.layout.workspace(agent_id)
    base = hub.library.checkout_workspace(workspace)
    with hub.transaction() as txn:
        txn.execute(
            "INSERT INTO workspace_bases (agent_id, commit_id, refreshed_at) VALUES (?, ?, ?) "
            "ON CONFLICT (agent_id) DO UPDATE SET commit_id = excluded.commit_id",
            (agent_id, base, txn.now),
        )


def start_turn(hub: Hub, agent_id: str, purpose: str = "task", *, task_id: str | None = None,
               review_id: str | None = None, capability: Capability | None = None) -> Actor:
    """Plan and start an activation; return the actor its tool calls use."""
    with hub.transaction() as txn:
        row = activations.plan(txn, agent_id, purpose, task_id=task_id, review_id=review_id)
        activations.mark_running(txn, row["id"], None)
    if capability is None:
        capability = {"task": Capability.WORKER, "review": Capability.REVIEWER,
                      "message": Capability.MESSAGE_ONLY}[purpose]
    return Actor(agent_id, capability, row["id"], row["fence"], task_id, review_id)


def end_turn(hub: Hub, actor: Actor, outcome: str = "completed") -> None:
    with hub.transaction() as txn:
        activations.end(txn, actor.activation_id, outcome)
