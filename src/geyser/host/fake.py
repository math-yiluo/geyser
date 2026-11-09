"""An in-memory process host for tests and the scripted backend.

Processes launched here are ordinary child processes tracked per scope; scopes that
never launched anything are always quiet. Tests can mark a scope as not quiet to
exercise recovery paths.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from geyser.host.base import HostedProcess, ScopeRef

STREAM_LIMIT = 16 * 1024 * 1024


class FakeHost:
    name = "fake"

    def __init__(self) -> None:
        self._processes: dict[str, list[asyncio.subprocess.Process]] = {}
        self.stuck: set[str] = set()
        """Scopes a test declares busy (``quiet`` returns False)."""
        self.unknown: set[str] = set()
        """Scopes a test declares unobservable (``quiet`` returns None)."""

    def plan_scope(self, activation_id: str) -> ScopeRef:
        return ScopeRef(f"fake-{activation_id}")

    async def launch(self, scope: ScopeRef, argv: Sequence[str], env: Mapping[str, str],
                     cwd: Path) -> HostedProcess:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=dict(env), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt", limit=STREAM_LIMIT,
        )
        self._processes.setdefault(scope.name, []).append(process)
        return process  # type: ignore[return-value]

    async def quiet(self, scope: ScopeRef) -> bool | None:
        if scope.name in self.unknown:
            return None
        if scope.name in self.stuck:
            return False
        return all(process.returncode is not None
                   for process in self._processes.get(scope.name, []))

    async def terminate(self, scope: ScopeRef, grace_seconds: float) -> None:
        self.stuck.discard(scope.name)
        for process in self._processes.get(scope.name, []):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=grace_seconds)
                except TimeoutError:
                    process.kill()
                    await process.wait()
