"""Owned process trees in cgroups of a delegated subtree (Linux, ported from FormalGeyser).

The Hub owns a delegated cgroup v2 subtree: its own systemd user unit's when that unit
has ``Delegate=yes``, or else a new delegated scope of the user's systemd manager that
it moves itself into. Every activation's process tree gets a cgroup of its own; the
tree's first process moves itself in before it runs the command, so no process of the
tree runs outside it. A tree has ended once its cgroup is empty, and it is ended as a
whole with ``cgroup.kill``. Scope names are absolute cgroup paths, so a restarted Hub
(in a new scope) can still probe and end the trees of the previous one.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from geyser.host.base import HostedProcess, ScopeRef

MOUNT = Path("/sys/fs/cgroup")
CONTROLLERS = ("cpu", "memory", "pids")
TREE = re.compile(r"(?:/[\w.@-]+)+/tree-[0-9a-f]{32}")
STREAM_LIMIT = 16 * 1024 * 1024


class HostUnavailable(OSError):
    """This system cannot give the Hub a cgroup subtree of its own."""


def _own_cgroup() -> str:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return line[3:]
    raise HostUnavailable("this system has no unified cgroup v2 hierarchy")


def _uid() -> int:
    return int(getattr(os, "getuid")())  # noqa: B009 - Linux only; keeps type checks portable


def _bus_environment() -> dict[str, str]:
    return {"XDG_RUNTIME_DIR": f"/run/user/{_uid()}", **os.environ}


def _manager_cgroup() -> str:
    uid = _uid()
    return f"/user.slice/user-{uid}.slice/user@{uid}.service"


def _delegated() -> bool:
    relative = _own_cgroup()
    unit = relative.rsplit("/", 1)[1]
    if not (relative.startswith(_manager_cgroup() + "/") and
            unit.endswith((".scope", ".service"))):
        return False
    try:
        shown = subprocess.run(["systemctl", "--user", "show", "--property=Delegate",
                                "--value", unit], capture_output=True, text=True, timeout=30,
                               env=_bus_environment())
    except (OSError, subprocess.SubprocessError):
        return False
    return shown.returncode == 0 and shown.stdout.strip() == "yes"


def _new_scope() -> str:
    """Move this process into a new delegated scope of the user's systemd manager."""
    unit = f"geyser-{uuid.uuid4().hex}.scope"
    command = ["busctl", "--user", "call", "org.freedesktop.systemd1",
               "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
               "StartTransientUnit", "ssa(sv)a(sa(sv))", unit, "fail", "3", "PIDs", "au", "1",
               str(os.getpid()), "Delegate", "b", "true", "CollectMode", "s",
               "inactive-or-failed", "0"]
    try:
        started = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                 env=_bus_environment())
    except (OSError, subprocess.SubprocessError) as error:
        raise HostUnavailable(f"the user's systemd manager is unreachable: {error}") from error
    if started.returncode != 0:
        raise HostUnavailable("the user's systemd manager started no scope: "
                              + started.stderr.strip()[-500:])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        relative = _own_cgroup()
        if relative.endswith("/" + unit):
            return relative
        time.sleep(0.01)
    raise HostUnavailable("this process did not enter its new scope")


def _pids(cgroup: Path) -> list[int]:
    return [int(line) for line in (cgroup / "cgroup.procs").read_text().split()]


def processes(cgroup: Path) -> list[int]:
    """Every process in a cgroup and below it."""
    found: list[int] = []
    for current, _, _ in os.walk(cgroup):
        try:
            found += _pids(Path(current))
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENODEV):
                raise
    return found


class SystemdHost:
    name = "linux_systemd"

    def __init__(self) -> None:
        self._root: Path | None = None
        self._lock = threading.Lock()

    def root(self) -> Path:
        """The Hub's owned cgroup, set up on first use."""
        with self._lock:
            if self._root is None:
                relative = _own_cgroup() if _delegated() else _new_scope()
                path = MOUNT / relative.lstrip("/")
                leaf = path / "owner"
                leaf.mkdir(exist_ok=True)
                for _ in range(100):  # A cgroup with processes cannot account its children.
                    pids = _pids(path)
                    if not pids:
                        break
                    for pid in pids:
                        try:
                            (leaf / "cgroup.procs").write_text(str(pid))
                        except ProcessLookupError:
                            pass
                available = (path / "cgroup.controllers").read_text().split()
                enabled = (path / "cgroup.subtree_control").read_text().split()
                wanted = [name for name in CONTROLLERS if name in available and name not in enabled]
                if wanted:
                    (path / "cgroup.subtree_control").write_text(
                        " ".join("+" + name for name in wanted))
                self._root = path
            return self._root

    def plan_scope(self, activation_id: str) -> ScopeRef:
        cgroup = self.root() / f"tree-{uuid.uuid4().hex}"
        return ScopeRef("/" + cgroup.relative_to(MOUNT).as_posix())

    @staticmethod
    def _located(scope: ScopeRef) -> Path | None:
        value = scope.name
        if not TREE.fullmatch(value) or {".", ".."} & set(value.split("/")):
            return None
        return MOUNT / value.lstrip("/")

    async def launch(self, scope: ScopeRef, argv: Sequence[str], env: Mapping[str, str],
                     cwd: Path) -> HostedProcess:
        cgroup = self._located(scope)
        if cgroup is None:
            raise HostUnavailable(f"not an owned scope: {scope.name}")
        cgroup.mkdir(exist_ok=True)
        entered = ["/bin/sh", "-c", 'echo 0 > "$0" && exec "$@"', str(cgroup / "cgroup.procs"),
                   *argv]
        process = await asyncio.create_subprocess_exec(
            *entered, cwd=cwd, env=dict(env), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=STREAM_LIMIT)
        return process  # type: ignore[return-value]

    async def quiet(self, scope: ScopeRef) -> bool | None:
        cgroup = self._located(scope)
        if cgroup is None:
            return None
        if not cgroup.exists():
            return True
        try:
            return not await asyncio.to_thread(processes, cgroup)
        except OSError:
            return None

    async def terminate(self, scope: ScopeRef, grace_seconds: float) -> None:
        cgroup = self._located(scope)
        if cgroup is None or not cgroup.exists():
            return
        for pid in await asyncio.to_thread(processes, cgroup):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not await asyncio.to_thread(processes, cgroup):
                break
            await asyncio.sleep(0.2)
        else:
            try:
                (cgroup / "cgroup.kill").write_text("1")
            except OSError:
                pass
            for _ in range(50):
                if not await asyncio.to_thread(processes, cgroup):
                    break
                await asyncio.sleep(0.1)
        try:
            for current, _, _ in sorted(os.walk(cgroup), key=lambda item: -len(item[0])):
                os.rmdir(current)
        except OSError:
            pass  # Removed later, once the kernel releases it.
