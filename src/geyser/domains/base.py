"""The domain pack boundary (DESIGN.md section 17.1).

A domain pack supplies vocabulary (task kinds, contribution kinds, policies),
checks (contract, candidate and tree validation), agent-facing material (guides,
extra operations, environment) and instance preparation. A pack's report is
evidence; the Hub alone decides acceptance.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from geyser.core.models import Manifest, TaskContract, ValidationReport

if TYPE_CHECKING:
    from geyser.config import ProjectConfig
    from geyser.core.actors import Actor
    from geyser.layout import Layout


@dataclass(frozen=True)
class TaskKindSpec:
    name: str
    description: str
    payload_schema: dict[str, Any] | None = None


CompanionRule = Callable[[set[str]], list[str]]
"""Given the candidate's changed paths, return problems (such as a missing pair)."""


@dataclass(frozen=True)
class ContributionKindSpec:
    """File rules and the default policy of one contribution kind (section 8.1)."""

    name: str
    description: str
    default_policy: str
    allowed_prefixes: tuple[str, ...] = ()
    """Paths must start with one of these; empty means any path outside reserved ones."""
    excluded_prefixes: tuple[str, ...] = ()
    companions: CompanionRule | None = None
    max_files: int = 2_000
    max_file_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024
    payload_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class PolicySpec:
    """The checks a contribution passes (section 8.2)."""

    name: str
    validator: str | None
    """Pack validator run on the candidate before review; None means kind rules only."""
    final_check: str | None
    """Pack check run on the integrated tree before publication."""
    review_guide: str
    """The guide section reviewers apply."""
    publishes: bool = True
    """False for evidence-only policies, whose acceptances never publish."""


@dataclass(frozen=True)
class GuideSet:
    """Guides per role: required text goes into standing instructions, on-demand ones
    are copied into the agent's ``.private/instructions/``."""

    required: Mapping[str, Sequence[tuple[str, str]]] = field(default_factory=dict)
    on_demand: Mapping[str, Sequence[tuple[str, str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationSpec:
    """An extra gateway operation a pack declares."""

    name: str
    category: str
    summary: str
    arguments_schema: dict[str, Any]
    capabilities: frozenset[str]
    mutating: bool = False


@dataclass
class ValidationContext:
    layout: Layout
    config: ProjectConfig
    scratch: Path
    """A private directory the validator may use; removed afterwards."""
    attempt: int = 1
    wait_seconds: float | None = None


@dataclass
class CandidateView:
    """What a validator sees of one candidate."""

    id: str
    kind: str
    policy: str
    manifest: Manifest
    contract: TaskContract
    base_commit: str
    changed: Mapping[str, Path]
    """Added or modified project-relative paths, mapped to their frozen files."""
    deleted: Sequence[str]
    tree: Path
    """A materialized tree: the Library base plus the candidate's changes."""


@dataclass
class TreeSnapshot:
    path: Path
    tree_id: str


@dataclass
class OperationContext:
    actor: Actor
    layout: Layout
    config: ProjectConfig
    idempotency_key: str


@dataclass
class AgentView:
    id: str
    kind: str
    backend: str
    workspace: Path


@dataclass
class InstanceContext:
    layout: Layout
    config: ProjectConfig


@dataclass(frozen=True)
class Readiness:
    ready: bool
    detail: str = ""


class ValidationContinues(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised by a validator whose bounded wait ended before its result (section 8.3).

    The validation phase becomes ``queued``; the scheduler attaches to the same job
    later through ``resume_validation``.
    """

    def __init__(self, job: str):
        super().__init__(job)
        self.job = job


@runtime_checkable
class DomainPack(Protocol):
    name: str

    # Vocabulary
    def task_kinds(self) -> Mapping[str, TaskKindSpec]: ...
    def contribution_kinds(self) -> Mapping[str, ContributionKindSpec]: ...
    def policies(self) -> Mapping[str, PolicySpec]: ...
    def default_policy(self) -> str: ...

    # Checks; a report is evidence, and the Hub alone decides acceptance
    def validate_contract(self, contract: TaskContract) -> None: ...
    async def validate_candidate(
        self, ctx: ValidationContext, candidate: CandidateView
    ) -> ValidationReport: ...
    async def check_tree(
        self, ctx: ValidationContext, tree: TreeSnapshot, members: Sequence[CandidateView]
    ) -> ValidationReport: ...

    # Agent-facing material
    def guides(self) -> GuideSet: ...
    def operations(self) -> Sequence[OperationSpec]: ...
    async def handle_operation(
        self, ctx: OperationContext, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...
    def agent_environment(self, agent: AgentView) -> Mapping[str, str]: ...

    # Instance lifecycle
    async def prepare_instance(self, ctx: InstanceContext) -> Readiness: ...
    async def resource_status(self) -> dict[str, Any] | None: ...
