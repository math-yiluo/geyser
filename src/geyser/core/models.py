"""Value models shared by the core, the gateway and domain packs.

These are plain data with validation. State lives in the ledger; these models
describe contracts, manifests, reports, predicates and evidence.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from geyser.ids import Kind, require_id, split_id

_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- tasks
def _without_policy(data: Any) -> Any:
    """A requirement names no policy; one from before that rule is ignored (section 5.6)."""
    if isinstance(data, dict) and "policy" in data:
        data = {key: value for key, value in data.items() if key != "policy"}
    return data


class Requirement(_Model):
    """A keyed dependency on one provider (section 5.6)."""

    key: str
    provider: str
    """``task-<n>`` (a task revision) or ``cand-<n>`` (an artifact)."""
    revision: int | None = Field(default=None, ge=0)
    """For a task provider: the revision whose completion is required."""

    @model_validator(mode="before")
    @classmethod
    def _ignore_policy(cls, data: Any) -> Any:
        return _without_policy(data)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _KEY_PATTERN.match(value):
            raise ValueError("requirement keys are lowercase letters, digits, `_`, `.`, `-`")
        return value

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        return require_id(value, Kind.TASK, Kind.CANDIDATE, field="provider")

    @model_validator(mode="after")
    def _consistent(self) -> Requirement:
        kind = split_id(self.provider)
        if kind and kind[0] is Kind.CANDIDATE and self.revision is not None:
            raise ValueError("an artifact provider takes no `revision`")
        return self


class TaskContract(_Model):
    """A task's contract (section 5.1)."""

    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=20_000)
    kind: str = "work"
    policy: str | None = None
    inputs: list[str] = Field(default_factory=list, max_length=200)
    requirements: list[Requirement] = Field(default_factory=list, max_length=100)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    acceptance: list[str] = Field(min_length=1, max_length=100)
    context: str = Field(default="", max_length=40_000)
    priority: int = Field(default=1, ge=0, le=3)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "objective")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("constraints", "acceptance")
    @classmethod
    def _conditions(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.strip():
                raise ValueError("conditions must not be blank")
            if len(value) > 4_000:
                raise ValueError("a condition is at most 4,000 characters")
        return values

    @model_validator(mode="after")
    def _unique_requirement_keys(self) -> TaskContract:
        keys = [requirement.key for requirement in self.requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("requirement keys must be unique")
        return self

    def conditions(self) -> dict[str, str]:
        """Every condition key with its text (section 5.1)."""
        result = {"objective": self.objective}
        for index, text in enumerate(self.constraints, start=1):
            result[f"constraint:{index}"] = text
        for requirement in self.requirements:
            target = requirement.provider
            if requirement.revision is not None:
                target += f" revision {requirement.revision}"
            result[f"requirement:{requirement.key}"] = target
        for index, text in enumerate(self.acceptance, start=1):
            result[f"acceptance:{index}"] = text
        return result


class RevisionClass(StrEnum):
    """How a revision relates to the one it replaces (section 5.4)."""

    COSMETIC = "cosmetic"
    ADDITIVE = "additive"
    MATERIAL = "material"


_COSMETIC_FIELDS = {"title", "context", "priority"}
_ADDITIVE_LISTS = ("requirements", "constraints", "acceptance")


def classify_revision(old: TaskContract, new: TaskContract) -> RevisionClass:
    """Classify a revision as cosmetic, additive or material."""
    old_data, new_data = old.model_dump(), new.model_dump()
    changed = {name for name in old_data if old_data[name] != new_data[name]}
    if changed <= _COSMETIC_FIELDS:
        return RevisionClass.COSMETIC
    core_changed = changed - _COSMETIC_FIELDS
    if core_changed <= set(_ADDITIVE_LISTS) and all(
        new_data[name][: len(old_data[name])] == old_data[name] for name in _ADDITIVE_LISTS
    ):
        return RevisionClass.ADDITIVE
    return RevisionClass.MATERIAL


def check_bar_kept(original: TaskContract, previous: TaskContract, new: TaskContract) -> None:
    """Refuse a revision that removes a condition (section 5.4, "the bar never drops").

    Conditions are positional, so a revision may not shorten a condition list or drop
    a requirement key. Texts may be edited; the original revision's conditions stay
    in the completion map whatever later revisions say.
    """
    for name in ("constraints", "acceptance"):
        if len(getattr(new, name)) < len(getattr(previous, name)):
            raise ValueError(f"a revision cannot remove {name}; add new ones instead")
    previous_keys = {requirement.key for requirement in previous.requirements}
    new_keys = {requirement.key for requirement in new.requirements}
    missing = previous_keys - new_keys
    if missing:
        raise ValueError(f"a revision cannot remove requirements: {sorted(missing)}")
    del original  # The original's conditions are enforced at completion, not here.


def required_condition_keys(original: TaskContract, current: TaskContract) -> dict[str, str]:
    """The keys completion must cover: current ones, plus changed original ones.

    An original condition whose text differs from the current one under the same key
    stays required as ``original:<key>``, so no revision lowers the bar.
    """
    current_conditions = current.conditions()
    required = dict(current_conditions)
    for key, text in original.conditions().items():
        if current_conditions.get(key) != text:
            required[f"original:{key}"] = text
    return required


# ---------------------------------------------------------------------- candidates
class Manifest(_Model):
    """A candidate submission (section 8.1)."""

    kind: str
    task_id: str
    task_revision: int = Field(ge=0)
    paths: list[str] = Field(min_length=1, max_length=500)
    introduction: str = Field(min_length=1, max_length=8_000)
    coauthors: list[str] = Field(default_factory=list, max_length=50)
    replaces: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id")
    @classmethod
    def _task(cls, value: str) -> str:
        return require_id(value, Kind.TASK, field="task_id")

    @field_validator("replaces")
    @classmethod
    def _replaces(cls, value: str | None) -> str | None:
        return None if value is None else require_id(value, Kind.CANDIDATE, field="replaces")

    @field_validator("coauthors")
    @classmethod
    def _coauthors(cls, values: list[str]) -> list[str]:
        return [
            require_id(value, Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR, field="coauthors")
            for value in values
        ]


class Verdict(StrEnum):
    """Machine validation verdicts (section 8.2)."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    NEEDS_OPERATOR = "needs_operator"


class ValidationReport(_Model):
    verdict: Verdict
    summary: str = Field(default="", max_length=20_000)
    details: dict[str, Any] = Field(default_factory=dict)
    qualifications: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _accepted_is_unqualified(self) -> ValidationReport:
        if self.verdict is Verdict.ACCEPTED and self.qualifications:
            raise ValueError("an accepted report must not carry qualifications")
        return self

    @property
    def unqualified_acceptance(self) -> bool:
        return self.verdict is Verdict.ACCEPTED and not self.qualifications


class Overlap(_Model):
    item: str = Field(min_length=1, max_length=1_000)
    relation: Literal["duplicate", "generalizes", "specializes", "related"]
    note: str = Field(default="", max_length=2_000)


class IntegrationNotes(_Model):
    """Guidance a reviewer attaches to an acceptance (section 8.4). Never evidence."""

    overlaps: list[Overlap] = Field(default_factory=list, max_length=50)
    placement: str = Field(default="", max_length=4_000)
    remarks: str = Field(default="", max_length=4_000)


class Finding(_Model):
    summary: str = Field(min_length=1, max_length=4_000)
    location: str = Field(min_length=1, max_length=1_000)
    """Exact ``path:line`` range or source locator the finding rests on."""


class ReviewVerdict(_Model):
    """A reviewer's recorded verdict (section 8.4)."""

    decision: Literal["accept", "reject"]
    summary: str = Field(min_length=1, max_length=20_000)
    findings: list[Finding] = Field(default_factory=list, max_length=200)
    suggestions: list[str] = Field(default_factory=list, max_length=100)
    integration_notes: IntegrationNotes | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ReviewVerdict:
        if self.decision == "reject" and not self.findings:
            raise ValueError("a rejection names at least one demonstrated defect in `findings`")
        if self.decision == "reject" and self.integration_notes is not None:
            raise ValueError("integration notes accompany acceptances only")
        return self


# ------------------------------------------------------------------- waits and work
class Checkpoint(_Model):
    summary: str = Field(min_length=1, max_length=8_000)
    next_action: str = Field(min_length=1, max_length=4_000)


class RequirementTerm(_Model):
    """A term of a wait recorded before subscriptions: an artifact accepted and
    published, or a task revision completed. New pauses take none (section 12)."""

    provider: str
    revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _ignore_policy(cls, data: Any) -> Any:
        return _without_policy(data)

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        return require_id(value, Kind.TASK, Kind.CANDIDATE, field="provider")


class Subscription(_Model):
    """A wait for another task's results, described in words (section 12)."""

    task_id: str
    need: str = Field(min_length=1, max_length=2_000)
    """The results awaited: what the subscribed task must publish for you to go on."""

    @field_validator("task_id")
    @classmethod
    def _task(cls, value: str) -> str:
        return require_id(value, Kind.TASK, field="task_id")

    @field_validator("need")
    @classmethod
    def _need(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a subscription states its need")
        return value


class WaitCondition(_Model):
    """What ends a pause (section 12): another task's results, the waiter's own
    submission's result, or an answer. At least one term is required."""

    subscriptions: list[Subscription] = Field(default_factory=list, max_length=20)
    """Another open task and the results you need from it, in words."""
    publications: list[str] = Field(default_factory=list, max_length=50)
    """Candidates of your task on their way: ends once each has an outcome."""
    validations: list[str] = Field(default_factory=list, max_length=50)
    """Candidates of your task: ends once each machine validation has a result."""
    answers: list[str] = Field(default_factory=list, max_length=20)
    """Questions you asked in talk (`msg-<n>`): ends once each is answered."""
    external_response_id: str | None = None
    """Your request to the operator: ends once it is answered or resolved."""
    completion_declaration_id: str | None = None
    mode: Literal["all", "any"] = "all"
    """How the terms other than subscriptions combine; each subscription ends the
    wait by itself."""

    @field_validator("publications", "validations")
    @classmethod
    def _candidates(cls, values: list[str]) -> list[str]:
        return [require_id(value, Kind.CANDIDATE, field="candidates") for value in values]

    @field_validator("answers")
    @classmethod
    def _questions(cls, values: list[str]) -> list[str]:
        return [require_id(value, Kind.MESSAGE, field="answers") for value in values]

    @field_validator("external_response_id")
    @classmethod
    def _request(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_id(value, Kind.REQUEST, field="external_response_id")

    @field_validator("completion_declaration_id")
    @classmethod
    def _declaration(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return require_id(value, Kind.DECLARATION, field="completion_declaration_id")

    def has_terms(self) -> bool:
        return bool(self.subscriptions or self.publications or self.validations
                    or self.answers or self.external_response_id
                    or self.completion_declaration_id)

    def waits_for_answer(self) -> bool:
        return bool(self.answers or self.external_response_id)

    @model_validator(mode="after")
    def _nonempty(self) -> WaitCondition:
        if not self.has_terms():
            raise ValueError("a wait names what ends it: subscriptions, publications, "
                             "validations, answers, external_response_id or "
                             "completion_declaration_id")
        return self


class Predicate(WaitCondition):
    """The recorded condition a paused agent waits for (section 12).

    ``requirements`` survive only in waits recorded before subscriptions; they are
    still evaluated, but a new pause cannot name them.
    """

    requirements: list[RequirementTerm] = Field(default_factory=list, max_length=50)

    def has_terms(self) -> bool:
        return bool(self.requirements) or super().has_terms()


class EvidenceEntry(_Model):
    explanation: str = Field(min_length=1, max_length=8_000)
    artifacts: list[str] = Field(default_factory=list, max_length=100)
    """IDs of accepted, published candidates."""

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, values: list[str]) -> list[str]:
        return [require_id(value, Kind.CANDIDATE, field="artifacts") for value in values]


class Objection(_Model):
    key: str = Field(min_length=1, max_length=200)
    """The unmet condition key."""
    explanation: str = Field(min_length=1, max_length=8_000)


class ObjectionResolution(_Model):
    key: str
    outcome: Literal["satisfied", "optional_extension"]
    explanation: str = Field(min_length=1, max_length=8_000)
    artifacts: list[str] = Field(default_factory=list)
