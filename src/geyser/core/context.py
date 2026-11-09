"""The context every core operation receives."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from geyser.clock import Clock
from geyser.config import ProjectConfig
from geyser.core.ledger import Ledger
from geyser.domains.base import DomainPack
from geyser.layout import Layout


class LibraryView(Protocol):
    """Read access to the published Library that the core needs."""

    def head(self) -> str | None:
        """The current published commit, or None before initialization."""
        ...

    def exists(self, path: str) -> bool:
        """Whether ``path`` (file or directory) exists in the current head."""
        ...


@dataclass
class HubContext:
    ledger: Ledger
    clock: Clock
    layout: Layout
    config: ProjectConfig
    pack: DomainPack
    library: LibraryView
    wake: Callable[[], None] = field(default=lambda: None)
    """Nudges the scheduler; called after commits that may make work ready."""
