"""The Codex backend against a fake app-server speaking the same protocol."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from geyser import operator
from geyser.config import BackendConfig
from geyser.core import controls
from geyser.host.fake import FakeHost
from geyser.hub import Hub
from geyser.runtime.backends.codex import CodexBackend
from geyser.service import GeyserService

FAKE = str(Path(__file__).with_name("fake_codex.py"))


def codex_service(hub: Hub, tmp_path: Path) -> GeyserService:
    settings = BackendConfig(command=[sys.executable, FAKE], home=str(tmp_path / "codex-home"),
                             model="test-model", effort="low")
    hub.config.codex = settings
    return GeyserService(hub, {"codex": CodexBackend(settings)}, FakeHost())


async def run_one_turn(hub: Hub, service: GeyserService) -> dict:
    operator.setup_project(hub, title="Goal", goal="Do the goal.", acceptance=["Done."])
    operator.run_action(hub, "start")
    with hub.transaction() as txn:
        # Keep the worker to a single turn for the test: pause after it.
        controls.set_run(txn, controls.RUNNING)
    await service.round()
    await service.round()
    tasks = list(service._activation_tasks.values())
    for task in tasks:
        await task
    with hub.read() as txn:
        return dict(txn.one("SELECT * FROM activations ORDER BY planned_at LIMIT 1"))


async def test_a_turn_with_a_tool_call(hub: Hub, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "normal")
    service = codex_service(hub, tmp_path)
    activation = await run_one_turn(hub, service)
    assert activation["outcome"] == "completed", activation
    with hub.read() as txn:
        agent = txn.one("SELECT conversation_id FROM agents WHERE id = 'worker-0'")
        usage = txn.one("SELECT SUM(input_tokens) AS i, SUM(output_tokens) AS o FROM usage")
    assert agent["conversation_id"] == "thread-fake"
    assert usage["i"] == 1000 and usage["o"] == 50
    transcript = (hub.layout.transcripts / "worker-0" / f"{activation['id']}.jsonl").read_text()
    assert '"tool_call"' in transcript and "synced:" in transcript


async def test_usage_limit_holds_the_account(hub: Hub, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "usage_limit")
    service = codex_service(hub, tmp_path)
    activation = await run_one_turn(hub, service)
    assert activation["outcome"] == "failed"
    with hub.read() as txn:
        assert controls.held(txn, "codex")


@pytest.mark.parametrize("mode", ["crash"])
async def test_a_crashed_server_ends_with_an_unknown_outcome(hub: Hub, tmp_path: Path,
                                                             monkeypatch, mode: str) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    service = codex_service(hub, tmp_path)
    activation = await run_one_turn(hub, service)
    assert activation["outcome"] == "closed_unknown", activation
