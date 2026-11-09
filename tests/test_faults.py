"""Crash the Hub right after a commit, restart it, and check that the story still finishes."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from geyser import operator
from geyser.clock import ManualClock
from geyser.config import ProjectConfig
from geyser.core import invariants, tasks
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.scripted import ScriptedBackend
from geyser.service import GeyserService
from tests.conftest import chosen
from tests.scripted import AcceptingReviewer, SimpleWorker, no_decider


class SimulatedCrash(BaseException):
    """Not an Exception: nothing in Geyser may swallow it, like a real crash."""


def service_for(hub: Hub) -> GeyserService:
    behaviors = {"worker": SimpleWorker(), "reviewer": AcceptingReviewer()}
    hub.config.decisions.backend = "codex"
    return GeyserService(hub, {"codex": ScriptedBackend(behaviors, no_decider, name="codex")},
                         FakeHost())


def fresh_project(path: Path) -> Hub:
    hub = Hub.initialize(path, chosen(ProjectConfig(name="faults")), clock=ManualClock())
    operator.setup_project(hub, title="Write a result", goal="Write a result file.",
                           acceptance=["The result file exists."])
    operator.run_action(hub, "start")
    return hub


async def run_with_crash(hub: Hub, crash_at: int, phase: str = "after") -> bool:
    """Drive the service until the ``crash_at``-th commit; returns whether it crashed.

    ``after`` crashes right after a durable commit; ``before`` crashes just before one,
    so that transaction rolls back.
    """
    commits = {"n": 0}
    crashed = {"yes": False}

    def hook() -> None:
        commits["n"] += 1
        if commits["n"] == crash_at:
            crashed["yes"] = True
            raise SimulatedCrash()

    if phase == "after":
        hub.ledger.after_commit_hook = hook
    else:
        hub.ledger.before_commit = hook
    service = service_for(hub)
    service._loop = asyncio.get_running_loop()
    try:
        for _ in range(200):
            try:
                progress = await service.round()
            except SimulatedCrash:
                break
            tasks_now = list(service._activation_tasks.values())
            if tasks_now:
                results = await asyncio.gather(*tasks_now, return_exceptions=True)
                if any(isinstance(result, SimulatedCrash) for result in results):
                    break
                progress = True
            if crashed["yes"] or not progress:
                break
    finally:
        hub.ledger.after_commit_hook = None
        hub.ledger.before_commit = None
        for task in list(service._activation_tasks.values()):
            task.cancel()
    return crashed["yes"]


async def count_commits(tmp_path: Path) -> int:
    hub = fresh_project(tmp_path / "count")
    count = {"n": 0}
    hub.ledger.after_commit_hook = lambda: count.__setitem__("n", count["n"] + 1)
    await service_for(hub).run_until_idle()
    hub.close()
    return count["n"]


@pytest.mark.parametrize(("phase", "stride"), [("after", 3), ("before", 5)])
async def test_crash_at_commits_recovers(tmp_path: Path, phase: str, stride: int) -> None:
    total = await count_commits(tmp_path)
    assert total > 20
    template = tmp_path / "template"
    fresh_project(template).close()
    for crash_at in range(1, total, stride):
        path = tmp_path / f"crash-{crash_at}"
        shutil.copytree(template, path)
        hub = Hub(path, clock=ManualClock())
        crashed = await run_with_crash(hub, crash_at, phase)
        hub.close()
        restarted = Hub(path, clock=ManualClock())
        await service_for(restarted).run_until_idle()
        with restarted.read() as txn:
            state = tasks.task_row(txn, "task-0")["state"]
        problems = invariants.check(restarted)
        restarted.close()
        assert state == "completed", (crash_at, crashed, state)
        assert problems == [], (crash_at, problems)
