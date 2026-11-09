"""Typed sequential IDs (DESIGN.md section 5.1).

Every object agents name has an ID ``<kind>-<n>``: ``task-0``, ``cand-12``,
``review-3``. Each kind has its own counter in the ledger, starting at 0 and never
reusing a number. A field accepts only its own kind; a bare number or an ID of
another kind is refused with an error that names the expected form.
"""

from __future__ import annotations

import re
from enum import StrEnum

from geyser.errors import InvalidArgument

_ID_PATTERN = re.compile(r"^([a-z]+)-(0|[1-9][0-9]*)$")


class Kind(StrEnum):
    """The ID kinds. The value is the textual prefix."""

    TASK = "task"
    CANDIDATE = "cand"
    REVIEW = "review"
    MESSAGE = "msg"
    DRAFT = "draft"
    RECORD = "record"
    SOURCE = "source"
    REQUEST = "request"
    DECLARATION = "decl"
    BATCH = "batch"
    ACTIVATION = "act"
    DECISION = "dec"
    VALIDATION = "val"
    PUBLICATION = "pub"
    NOTICE = "notice"
    WORKER = "worker"
    REVIEWER = "reviewer"
    INTEGRATOR = "integrator"


AGENT_KINDS = frozenset({Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR})

_KIND_NAMES = {
    Kind.TASK: "task",
    Kind.CANDIDATE: "candidate",
    Kind.REVIEW: "review",
    Kind.MESSAGE: "message",
    Kind.DRAFT: "draft",
    Kind.RECORD: "shared record",
    Kind.SOURCE: "source",
    Kind.REQUEST: "request",
    Kind.DECLARATION: "completion declaration",
    Kind.BATCH: "batch",
    Kind.ACTIVATION: "activation",
    Kind.DECISION: "decision",
    Kind.VALIDATION: "validation",
    Kind.PUBLICATION: "publication",
    Kind.NOTICE: "notice",
    Kind.WORKER: "worker",
    Kind.REVIEWER: "reviewer",
    Kind.INTEGRATOR: "integrator",
}


def format_id(kind: Kind, number: int) -> str:
    """Return the textual ID of object ``number`` of ``kind``."""
    if number < 0:
        raise ValueError("ID numbers start at 0")
    return f"{kind.value}-{number}"


def split_id(value: str) -> tuple[Kind, int] | None:
    """Split a well-formed ID into its kind and number, or return None."""
    match = _ID_PATTERN.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        kind = Kind(match.group(1))
    except ValueError:
        return None
    return kind, int(match.group(2))


def kind_of(value: str) -> Kind | None:
    """Return the kind of a well-formed ID, or None."""
    parts = split_id(value)
    return parts[0] if parts else None


def require_id(value: object, *kinds: Kind, field: str = "id") -> str:
    """Return ``value`` if it is an ID of one of ``kinds``; otherwise refuse it.

    The error names the expected form, so an agent that passed ``17`` or
    ``cand-17`` where a task belongs learns to write ``task-17``.
    """
    expected = " or ".join(f"`{kind.value}-<n>`" for kind in kinds)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidArgument(f"`{field}` must be an ID such as {expected}.")
    if isinstance(value, int) or str(value).isdigit():
        raise InvalidArgument(
            f"`{field}` must be a typed ID such as {expected}, not a bare number.",
            details={"field": field, "value": value},
        )
    parts = split_id(value)
    if parts is None:
        raise InvalidArgument(
            f"`{field}` is not a well-formed ID; expected {expected}.",
            details={"field": field, "value": value},
        )
    kind, _ = parts
    if kind not in kinds:
        raise InvalidArgument(
            f"`{value}` is a {_KIND_NAMES[kind]}; `{field}` takes {expected}.",
            details={"field": field, "value": value},
        )
    return value


def id_number(value: str) -> int:
    """Return the number of a well-formed ID (used for numeric ordering)."""
    parts = split_id(value)
    if parts is None:
        raise ValueError(f"not an ID: {value!r}")
    return parts[1]


def describe(value: str) -> str:
    """A human name for an ID's kind, such as ``candidate`` for ``cand-3``."""
    kind = kind_of(value)
    return _KIND_NAMES[kind] if kind else "object"
