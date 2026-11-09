"""Project configuration: ``geyser.toml``.

Each setting's meaning and default are described in DESIGN.md where its behavior
is specified. Unknown keys are refused, so a typo fails at startup instead of
silently falling back to a default.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, field_validator

from geyser.errors import InvalidArgument
from geyser.jsonio import atomic_write_text

SCHEMA_VERSION = 1


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentsConfig(_Section):
    new_backend: str = "codex"
    """Backend for agents created next (section 6.1)."""


class BackendConfig(_Section):
    home: str | None = None
    """The backend's home directory (sign-ins, stored conversations). Protected."""
    command: list[str] = Field(default_factory=list)
    """Executable and leading arguments; empty means the backend's default."""
    model: str | None = None
    """The model of workers on this backend. No default: chosen before a run (15.1)."""
    effort: str | None = None
    """The effort of workers on this backend. No default either."""
    profiles: list[str] = Field(default_factory=list)
    """Further sign-in homes, used in order of preference (section 14)."""


class RoleConfig(_Section):
    """The backend, model and effort of one non-worker role (section 15.1)."""

    backend: str = "codex"
    model: str | None = None
    effort: str | None = None


class WaitsConfig(_Section):
    answer_deadline_minutes: float | None = Field(default=None, gt=0)
    """When a wait for an answer ends without one (section 12). No default."""


class TeamsConfig(_Section):
    collaborators: int = Field(default=1, ge=0)
    idle_retirement_hours: float = Field(default=48, gt=0)


class AssignmentConfig(_Section):
    worker_backends: list[str] = Field(default_factory=list)
    """Optional backend per worker number; missing entries use ``agents.new_backend``."""


class RotationConfig(_Section):
    mode: Literal["conservative", "aggressive"] = "conservative"
    reviewer_mode: Literal["conservative", "aggressive"] | None = None
    idle_minutes: float = Field(default=40, gt=0)
    context_tokens: int = Field(default=100_000, gt=0)
    context_growth_tokens: int = Field(default=50_000, gt=0)


class IntegrationConfig(_Section):
    mode: Literal["direct", "batch"] = "direct"
    batch_threshold: int = Field(default=5, ge=1)
    batch_delay_minutes: float = Field(default=13, ge=0)


class GatewayConfig(_Section):
    prefix: str = "geyser"

    @field_validator("prefix")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value.replace("_", "").isalnum() or not value[0].isalpha():
            raise ValueError("gateway.prefix must be a letter followed by letters, digits or _")
        return value


class SandboxConfig(_Section):
    network: Literal["open", "closed"] = "open"
    protected_paths: list[str] = Field(default_factory=list)


class StateConfig(_Section):
    dir: str = ".geyser"


class SchedulerConfig(_Section):
    admission_pass_seconds: float = Field(default=30, gt=0)
    tick_seconds: float = Field(default=2, gt=0)
    launch_seconds: float = Field(default=0, ge=0)


class HostConfig(_Section):
    kind: Literal["auto", "linux_systemd", "fake"] = "auto"


class CheckConfig(_Section):
    """An operator-defined check for ``general.command@1`` (section 17.1)."""

    command: list[str]
    timeout_minutes: float = Field(default=30, gt=0)

    @field_validator("command")
    @classmethod
    def _nonempty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("a check needs a command")
        return value


class ProjectConfig(_Section):
    schema_version: int = SCHEMA_VERSION
    name: str = "Geyser project"
    domain: str = "general"
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    codex: BackendConfig = Field(default_factory=BackendConfig)
    claude: BackendConfig = Field(default_factory=BackendConfig)
    reviewers: RoleConfig = Field(default_factory=RoleConfig)
    integrator: RoleConfig = Field(default_factory=RoleConfig)
    decisions: RoleConfig = Field(default_factory=RoleConfig)
    """The Dispatcher's assignment and recruitment decisions."""
    waker: RoleConfig = Field(default_factory=RoleConfig)
    waits: WaitsConfig = Field(default_factory=WaitsConfig)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    assignment: AssignmentConfig = Field(default_factory=AssignmentConfig)
    concurrency: dict[str, int] = Field(default_factory=dict)
    """Sessions running at once per backend account; -1 or missing means no limit."""
    rotation: RotationConfig = Field(default_factory=RotationConfig)
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    host: HostConfig = Field(default_factory=HostConfig)
    checks: dict[str, CheckConfig] = Field(default_factory=dict)

    def backend(self, name: str) -> BackendConfig:
        if name == "codex":
            return self.codex
        if name == "claude":
            return self.claude
        return BackendConfig()

    def concurrency_limit(self, backend: str) -> int | None:
        limit = self.concurrency.get(backend, -1)
        return None if limit < 0 else limit


def load_config(path: Path) -> ProjectConfig:
    """Read and validate ``geyser.toml``; a missing file means all defaults."""
    if not path.exists():
        return ProjectConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise InvalidArgument(f"{path.name} is not valid TOML: {error}") from error
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise InvalidArgument(
            f"{path.name} has schema_version {data.get('schema_version')}; "
            f"this Geyser reads version {SCHEMA_VERSION}."
        )
    try:
        return ProjectConfig.model_validate(data)
    except ValueError as error:
        raise InvalidArgument(f"{path.name} is invalid: {error}") from error


def save_config(path: Path, config: ProjectConfig) -> None:
    data = config.model_dump(mode="json", exclude_none=True)
    atomic_write_text(path, tomli_w.dumps(data))
