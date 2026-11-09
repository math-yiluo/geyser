"""Who is calling: an agent execution, the operator, or the Hub itself."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    """Server-side roles (DESIGN.md section 13.3)."""

    WORKER = "worker"
    MESSAGE_ONLY = "message_only"
    REVIEWER = "reviewer"
    REVIEW_ANSWER = "review_answer"
    INTEGRATOR = "integrator"
    INTEGRATOR_ANSWER = "integrator_answer"
    OPERATOR = "operator"
    SYSTEM = "system"


AGENT_CAPABILITIES = frozenset(
    {
        Capability.WORKER,
        Capability.MESSAGE_ONLY,
        Capability.REVIEWER,
        Capability.REVIEW_ANSWER,
        Capability.INTEGRATOR,
        Capability.INTEGRATOR_ANSWER,
    }
)

OPERATOR_ID = "operator"
HUB_ID = "hub"


@dataclass(frozen=True)
class Actor:
    """The identity behind one request.

    Agent actors carry the activation and fencing token of the execution that made
    the call; every mutation checks both against the ledger (section 14).
    """

    id: str
    capability: Capability
    activation_id: str | None = None
    fence: int | None = None
    task_id: str | None = None
    review_id: str | None = None
    batch_id: str | None = None

    @property
    def is_agent(self) -> bool:
        return self.capability in AGENT_CAPABILITIES

    @property
    def is_operator(self) -> bool:
        return self.capability is Capability.OPERATOR

    @property
    def sender_role(self) -> str:
        """The trusted sender role recorded on messages (section 11.1)."""
        if self.capability is Capability.OPERATOR:
            return "operator"
        if self.is_agent:
            return "agent"
        return "unknown"


OPERATOR = Actor(OPERATOR_ID, Capability.OPERATOR)
SYSTEM = Actor(HUB_ID, Capability.SYSTEM)
