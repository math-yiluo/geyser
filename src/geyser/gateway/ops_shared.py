"""Shared work and source operations: drafts, records, sources, request resolution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from geyser.content import shared
from geyser.core import messages
from geyser.core.actors import Capability
from geyser.gateway.base import (
    READERS,
    WORKERS_AND_REVIEWERS,
    Arguments,
    CallContext,
    Operation,
)


class DraftArguments(Arguments):
    name: str = Field(min_length=1, max_length=64)
    """The directory ``.private/drafts/<name>/`` to share."""
    expected_revision: int | None = Field(default=None, ge=0)
    """Your task's latest draft revision, or null for the first draft."""


async def draft_update(call: CallContext, args: DraftArguments) -> dict[str, Any]:
    return call.commit(lambda txn: shared.update_draft(call.ctx, txn, call.actor, args.name,
                                                       args.expected_revision))


class RecordArguments(Arguments):
    name: str = Field(min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=0)
    content: str = Field(min_length=1)
    format: Literal["json", "markdown"] = "markdown"


async def record_update(call: CallContext, args: RecordArguments) -> dict[str, Any]:
    return call.commit(lambda txn: shared.update_record(call.ctx, txn, call.actor, args.name,
                                                        args.expected_revision, args.content,
                                                        args.format))


class RecordStatusArguments(Arguments):
    name: str | None = Field(default=None, max_length=64)


async def record_status(call: CallContext, args: RecordStatusArguments) -> dict[str, Any]:
    return call.read(lambda txn: shared.record_status(call.ctx, txn, args.name))


class SourceArguments(Arguments):
    path: str = Field(min_length=1, max_length=1_000)
    """A file or folder under ``sources/``."""
    title: str = Field(min_length=1, max_length=300)


async def source_register(call: CallContext, args: SourceArguments) -> dict[str, Any]:
    return call.commit(lambda txn: shared.register_source(call.ctx, txn, call.actor, args.path,
                                                          args.title))


class ResolveArguments(Arguments):
    request_id: str
    source_id: str


async def request_resolve(call: CallContext, args: ResolveArguments) -> dict[str, Any]:
    return call.commit(lambda txn: messages.resolve_material(call.ctx, txn, call.actor,
                                                             args.request_id, args.source_id))


OPERATIONS = [
    Operation("draft_update", "shared work", "Share .private/drafts/<name>/ as an unreviewed "
              "draft revision of your task.", DraftArguments, frozenset({Capability.WORKER}),
              draft_update),
    Operation("record_update", "shared work", "Write a new revision of a project-wide shared "
              "record.", RecordArguments, WORKERS_AND_REVIEWERS, record_update),
    Operation("record_status", "shared work", "List shared records, or locate one's current "
              "revision.", RecordStatusArguments, READERS, record_status, mutating=False),
    Operation("source_register", "sources", "Register a source under sources/ before citing "
              "it.", SourceArguments, WORKERS_AND_REVIEWERS, source_register),
    Operation("request_resolve", "sources", "Resolve a material request with a registered "
              "source.", ResolveArguments, WORKERS_AND_REVIEWERS, request_resolve),
]
