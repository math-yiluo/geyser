"""The Hub: one project's ledger, files, Library and domain pack, wired together.

``Hub`` is the deterministic part of a running project. The service (``geyser.
service``) adds the scheduler, the runtime and the operator API around it. Tests
use a ``Hub`` directly with a manual clock and scripted agents.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from geyser.clock import Clock, SystemClock
from geyser.config import ProjectConfig, load_config, save_config
from geyser.content.library import Library
from geyser.core.context import HubContext
from geyser.core.ledger import Ledger, Txn
from geyser.domains import load_pack
from geyser.domains.base import DomainPack
from geyser.layout import Layout

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, root: Path, *, clock: Clock | None = None,
                 config: ProjectConfig | None = None, pack: DomainPack | None = None):
        root = root.resolve()
        self.config = config if config is not None else load_config(root / "geyser.toml")
        self.layout = Layout(root, self.config.state.dir)
        self.layout.ensure()
        self.clock = clock or SystemClock()
        self.pack = pack or load_pack(self.config.domain)
        self.ledger = Ledger(self.layout.ledger, self.clock)
        self.library = Library(self.layout)
        self._wake_callbacks: list[Callable[[], None]] = []
        self.ctx = HubContext(
            ledger=self.ledger,
            clock=self.clock,
            layout=self.layout,
            config=self.config,
            pack=self.pack,
            library=self.library,
            wake=self._wake,
        )

    @classmethod
    def initialize(cls, root: Path, config: ProjectConfig | None = None, *,
                   import_from: Path | None = None, clock: Clock | None = None,
                   pack: DomainPack | None = None) -> Hub:
        """Create a new project instance (configuration, directories, Library)."""
        root.mkdir(parents=True, exist_ok=True)
        config_path = root / "geyser.toml"
        if config is None:
            config = load_config(config_path)
        if not config_path.exists():
            save_config(config_path, config)
        hub = cls(root, clock=clock, config=config, pack=pack)
        hub.library.initialize(import_from)
        return hub

    def _wake(self) -> None:
        for callback in list(self._wake_callbacks):
            try:
                callback()
            except Exception:
                log.exception("wake callback failed")

    def on_wake(self, callback: Callable[[], None]) -> None:
        self._wake_callbacks.append(callback)

    @contextmanager
    def transaction(self) -> Iterator[Txn]:
        with self.ledger.transaction() as txn:
            yield txn

    @contextmanager
    def read(self) -> Iterator[Txn]:
        with self.ledger.read() as txn:
            yield txn

    def close(self) -> None:
        self.ledger.close()
