"""The process host protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ScopeRef:
    """An owned process scope, named before any process exists."""

    name: str


class HostedProcess(Protocol):
    """A process started inside a scope, with pipes for a JSON-RPC peer."""

    stdin: asyncio.StreamWriter | None
    stdout: asyncio.StreamReader | None
    stderr: asyncio.StreamReader | None
    pid: int

    async def wait(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...


class ProcessHost(Protocol):
    name: str

    def plan_scope(self, activation_id: str) -> ScopeRef:
        """Name the scope of an activation; recorded in the ledger before launch."""
        ...

    async def launch(self, scope: ScopeRef, argv: Sequence[str], env: Mapping[str, str],
                     cwd: Path) -> HostedProcess:
        ...

    async def quiet(self, scope: ScopeRef) -> bool | None:
        """True when no process of the scope remains; None when it cannot tell."""
        ...

    async def terminate(self, scope: ScopeRef, grace_seconds: float) -> None:
        ...
