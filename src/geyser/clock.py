"""Clocks. The core takes a clock as a parameter so tests control time."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware in UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; never goes backwards."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that moves only when a test advances it."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


def iso(moment: datetime) -> str:
    """Serialize a timestamp for the ledger: UTC, ISO 8601, microseconds."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
