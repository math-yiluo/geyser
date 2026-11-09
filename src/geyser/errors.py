"""Errors that cross the gateway or the operator API.

A ``GeyserError`` is an expected refusal: the caller gets its code, message and
details. Anything else is a bug; the boundary that catches it logs a traceback and
returns a generic ``internal`` error instead, so a bug fails one call, not the run.
"""

from __future__ import annotations

from typing import Any


class GeyserError(Exception):
    """An expected refusal with a stable code."""

    code = "error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class InvalidArgument(GeyserError):
    """The arguments are malformed or violate a documented bound."""

    code = "invalid_argument"


class NotFound(GeyserError):
    """The named object does not exist."""

    code = "not_found"


class Conflict(GeyserError):
    """The object is not in a state that allows the request (stale revision, wrong state)."""

    code = "conflict"


class Forbidden(GeyserError):
    """The caller's capability or relationship does not allow the request."""

    code = "forbidden"


class StaleActor(GeyserError):
    """The caller's execution is no longer the live one (fencing, section 14)."""

    code = "stale_actor"


class Unavailable(GeyserError):
    """A dependency could not answer now. Not a defect of the request."""

    code = "unavailable"


class IntegrityError(GeyserError):
    """An authoritative file is missing or does not match its recorded hash."""

    code = "integrity"
