"""The backend protocol.

A backend runs one turn of one conversation in an owned scope: it starts the native
runtime, resumes (or starts) the conversation, sends the input, forwards gateway
tool calls to the Hub, honors steering and interruption, and reports the outcome.
The process tree is torn down at the turn boundary; the conversation persists in
the backend's home. A backend also answers tool-less decisions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from geyser.host.base import ProcessHost, ScopeRef


@dataclass(frozen=True)
class BackendCapabilities:
    steering: bool = False
    """Messages can be steered into a running turn."""
    compaction_keeps_inputs: bool = True
    """Compaction keeps earlier inputs verbatim (no need to resend the task view)."""


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    context_tokens: int | None = None
    """The largest context of the turn's requests, if the backend reports it."""

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.requests += other.requests
        if other.context_tokens is not None:
            self.context_tokens = max(self.context_tokens or 0, other.context_tokens)


@dataclass
class TurnOutcome:
    status: TurnStatus
    conversation_id: str | None
    input_delivered: bool
    """Whether the input certainly reached the backend (settles prepared deliveries)."""
    usage: Usage = field(default_factory=Usage)
    failure: str | None = None
    """A short failure kind, such as ``transport`` or ``model_error``."""
    detail: str = ""
    account_hold: str | None = None
    """Set when the backend reports a usage limit or a missing sign-in."""
    conversation_saved: bool = True
    """False when the backend never persisted the conversation (start a new one next)."""
    compacted: bool = False


class TurnControl:
    """Signals the runner sends into a running turn."""

    def __init__(self) -> None:
        self.interrupt = asyncio.Event()
        self.steering: asyncio.Queue[tuple[str, asyncio.Future[bool]]] = asyncio.Queue()
        self.drain_requested = asyncio.Event()

    async def steer(self, text: str) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await self.steering.put((text, future))
        return await future


ToolCallback = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
"""(tool name, arguments, call id) -> tool result."""


@dataclass
class TurnRequest:
    agent_id: str
    role: str
    conversation_id: str | None
    """None starts a new conversation."""
    standing_instructions: str
    tools: list[dict[str, Any]]
    input_text: str
    workspace: Path
    temporary: Path
    scope: ScopeRef
    host: ProcessHost
    environment: Mapping[str, str]
    model: str | None
    effort: str | None
    readable_roots: list[Path]
    protected_paths: list[Path]
    network: bool
    on_tool_call: ToolCallback
    control: TurnControl
    on_usage: Callable[[Usage], None] = lambda usage: None
    on_compaction: Callable[[], None] = lambda: None
    on_output: Callable[[str], None] = lambda text: None
    transcript: Callable[[dict[str, Any]], None] = lambda event: None


@dataclass
class DecisionRequest:
    prompt: str
    schema: dict[str, Any]
    directory: Path
    """An empty read-only directory to run in."""
    scope: ScopeRef
    host: ProcessHost
    model: str | None
    effort: str | None


@dataclass
class DecisionOutcome:
    answer: dict[str, Any] | None
    status: TurnStatus
    detail: str = ""
    usage: Usage = field(default_factory=Usage)
    account_hold: str | None = None


@dataclass
class SandboxReport:
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    detail: str = ""


class Backend(Protocol):
    name: str
    capabilities: BackendCapabilities

    async def run_turn(self, request: TurnRequest) -> TurnOutcome: ...

    async def decide(self, request: DecisionRequest) -> DecisionOutcome: ...

    async def probe_sandbox(self, request: TurnRequest) -> SandboxReport: ...
