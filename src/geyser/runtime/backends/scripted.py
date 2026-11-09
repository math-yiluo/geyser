"""A scripted backend: Python behaviors stand in for models (tests, demos, dry runs).

A behavior is an async function receiving a ``ScriptedTurn``. It reads the parsed
model-facing input and calls Hub operations through the same gateway tools a real
model uses. Behaviors are looked up by agent ID, then role, then ``"*"``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from geyser.runtime.backends.base import (
    BackendCapabilities,
    DecisionOutcome,
    DecisionRequest,
    SandboxReport,
    TurnOutcome,
    TurnRequest,
    TurnStatus,
    Usage,
)


class ScriptedFailure(Exception):
    """Raise from a behavior to simulate a backend failure of a given kind."""

    def __init__(self, kind: str = "model_error", *, account_hold: str | None = None,
                 unknown: bool = False):
        super().__init__(kind)
        self.kind = kind
        self.account_hold = account_hold
        self.unknown = unknown


@dataclass
class ScriptedTurn:
    request: TurnRequest
    view: dict[str, Any]
    conversation_id: str
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    _counter: int = 0

    @property
    def agent_id(self) -> str:
        return self.request.agent_id

    @property
    def role(self) -> str:
        return self.request.role

    @property
    def workspace(self) -> Path:
        return self.request.workspace

    @property
    def new_conversation(self) -> bool:
        return self.request.conversation_id is None

    def _tool(self, suffix: str) -> str:
        names = [tool["name"] for tool in self.request.tools]
        return next(name for name in names if name.endswith(suffix))

    async def call(self, operation: str, arguments: dict[str, Any] | None = None
                   ) -> dict[str, Any]:
        self._counter += 1
        call_id = f"{self.conversation_id}:{self._counter}"
        result = await self.request.on_tool_call(
            self._tool("_call"), {"operation": operation, "arguments": arguments or {}},
            call_id)
        self.calls.append((operation, arguments or {}, result))
        return result

    async def actions(self, **arguments: Any) -> dict[str, Any]:
        self._counter += 1
        return await self.request.on_tool_call(self._tool("_actions"), arguments,
                                               f"{self.conversation_id}:{self._counter}")

    def write(self, relative: str, text: str) -> Path:
        path = self.workspace / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


Behavior = Callable[[ScriptedTurn], Awaitable[None]]
Decider = Callable[[DecisionRequest], Awaitable[dict[str, Any]] | dict[str, Any]]


class ScriptedBackend:
    name = "scripted"
    capabilities = BackendCapabilities(steering=False, compaction_keeps_inputs=True)

    def __init__(self, behaviors: Mapping[str, Behavior], decider: Decider | None = None,
                 *, name: str = "scripted"):
        self.name = name
        self.behaviors = dict(behaviors)
        self.decider = decider
        self.turns: list[ScriptedTurn] = []

    def _behavior(self, request: TurnRequest) -> Behavior | None:
        return (self.behaviors.get(request.agent_id) or self.behaviors.get(request.role)
                or self.behaviors.get("*"))

    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        conversation = request.conversation_id or f"{self.name}-{uuid.uuid4().hex[:12]}"
        try:
            view = json.loads(request.input_text)
        except json.JSONDecodeError:
            view = {"text": request.input_text}
        turn = ScriptedTurn(request, view, conversation)
        self.turns.append(turn)
        behavior = self._behavior(request)
        usage = Usage(input_tokens=len(request.input_text) // 4, requests=1)
        if behavior is None:
            return TurnOutcome(TurnStatus.COMPLETED, conversation, True, usage)
        try:
            await behavior(turn)
        except ScriptedFailure as failure:
            status = TurnStatus.UNKNOWN if failure.unknown else TurnStatus.FAILED
            return TurnOutcome(status, conversation, True, usage, failure=failure.kind,
                               detail=str(failure), account_hold=failure.account_hold)
        if request.control.interrupt.is_set():
            return TurnOutcome(TurnStatus.INTERRUPTED, conversation, True, usage)
        return TurnOutcome(TurnStatus.COMPLETED, conversation, True, usage)

    async def decide(self, request: DecisionRequest) -> DecisionOutcome:
        if self.decider is None:
            return DecisionOutcome(None, TurnStatus.COMPLETED, "no decider configured")
        answer = self.decider(request)
        if hasattr(answer, "__await__"):
            answer = await answer  # type: ignore[misc]
        return DecisionOutcome(answer, TurnStatus.COMPLETED)  # type: ignore[arg-type]

    async def probe_sandbox(self, request: TurnRequest) -> SandboxReport:
        return SandboxReport(ok=True, checks={"scripted": True})
