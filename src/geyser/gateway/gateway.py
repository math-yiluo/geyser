"""Dispatching the two model-facing tools (DESIGN.md section 13).

``<prefix>_actions(category?, operation?)`` lists operations or shows one schema.
``<prefix>_call(operation, arguments)`` invokes one operation. Roles are enforced
here, by discovery and dispatch alike. A refused call returns a structured error;
an unexpected exception is logged and returned as ``internal`` so one bug fails one
call, never the turn or the run.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import jsonschema
from pydantic import BaseModel, ValidationError

from geyser.core import activations
from geyser.core.actors import Actor, Capability
from geyser.core.ledger import loads
from geyser.errors import Forbidden, GeyserError, InvalidArgument, NotFound
from geyser.gateway import (
    ops_communication,
    ops_completion,
    ops_contributions,
    ops_progress,
    ops_review,
    ops_shared,
    ops_tasks,
)
from geyser.gateway.base import Arguments, CallContext, Operation, arguments_hash
from geyser.jsonio import sha256_text

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)

CORE_OPERATIONS: list[Operation] = [
    *ops_tasks.OPERATIONS,
    *ops_progress.OPERATIONS,
    *ops_contributions.OPERATIONS,
    *ops_completion.OPERATIONS,
    *ops_shared.OPERATIONS,
    *ops_communication.OPERATIONS,
    *ops_review.OPERATIONS,
]

CATEGORY_ORDER = ["tasks", "progress", "contributions", "completion", "shared work",
                  "communication", "sources", "review", "integration", "domain"]


def _format_validation(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:10]:
        location = ".".join(str(part) for part in item["loc"]) or "arguments"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


INLINE_FIELDS = 7
"""An object argument with at most this many fields lists them in the cheat sheet."""


def _signature(model: type[BaseModel]) -> str:
    """``name(required, optional?, object{field, …})`` with bounds, for the cheat sheet."""
    schema = model.model_json_schema()
    return _fields(schema, schema.get("$defs", {}), nested=False)


def _fields(schema: dict[str, Any], definitions: dict[str, Any], *, nested: bool) -> str:
    required = set(schema.get("required", []))
    rendered = []
    for name, spec in schema.get("properties", {}).items():
        bound = ""
        if "maximum" in spec:
            bound = f"≤{spec['maximum']}"
        elif "maxItems" in spec:
            bound = f" ≤{spec['maxItems']} items"
        inner = ""
        target = None if nested else _object_schema(spec, definitions)
        if target is not None and len(target.get("properties", {})) <= INLINE_FIELDS:
            inner = "{" + _fields(target, definitions, nested=True) + "}"
        rendered.append(name + inner + ("" if name in required else "?")
                        + (f"[{bound}]" if bound else ""))
    return ", ".join(rendered)


def _object_schema(spec: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any] | None:
    """The object schema a property refers to, directly or as an optional value."""
    for candidate in (spec, *spec.get("anyOf", ()), *spec.get("allOf", ())):
        reference = candidate.get("$ref") if isinstance(candidate, dict) else None
        if reference:
            target = definitions.get(reference.rsplit("/", 1)[-1])
            if isinstance(target, dict) and target.get("properties"):
                return target
    return None


class Gateway:
    def __init__(self, hub: Hub, extra: Sequence[Operation] = ()):
        self.hub = hub
        operations = [*CORE_OPERATIONS, *extra, *self._pack_operations()]
        self.operations: dict[str, Operation] = {}
        for operation in operations:
            if operation.name in self.operations:
                raise ValueError(f"duplicate operation {operation.name!r}")
            self.operations[operation.name] = operation

    # ------------------------------------------------------------------ pack ops
    def _pack_operations(self) -> list[Operation]:
        result = []
        for spec in self.hub.pack.operations():
            schema = spec.arguments_schema

            async def handler(call: CallContext, args: Any, _spec=spec, _schema=schema
                              ) -> dict[str, Any]:
                from geyser.domains.base import OperationContext

                values = args if isinstance(args, dict) else {}
                try:
                    jsonschema.validate(values, _schema)
                except jsonschema.ValidationError as error:
                    raise InvalidArgument(f"Invalid arguments: {error.message}") from error
                if _spec.mutating and call.actor.is_agent:
                    with call.hub.read() as txn:
                        activations.check_actor(txn, call.actor)
                context = OperationContext(call.actor, call.ctx.layout, call.ctx.config,
                                           call.receipt_key)
                reply = await call.hub.pack.handle_operation(context, _spec.name, values)
                if _spec.mutating:
                    with call.hub.transaction() as txn:
                        call._store_receipt(txn, reply, "committed")
                return reply

            result.append(Operation(
                spec.name, "domain", spec.summary, Arguments,
                frozenset(Capability(value) for value in spec.capabilities), handler,
                mutating=spec.mutating,
            ))
        return result

    # ----------------------------------------------------------------- discovery
    def tool_definitions(self) -> list[dict[str, Any]]:
        prefix = self.hub.config.gateway.prefix
        return [
            {
                "name": f"{prefix}_actions",
                "description": "List the Hub operations you can call, or show one "
                               "operation's full argument schema.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "operation": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": f"{prefix}_call",
                "description": "Invoke one Hub operation with its arguments.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            },
        ]

    def allowed(self, capability: Capability) -> list[Operation]:
        ordered = sorted(self.operations.values(),
                         key=lambda op: (CATEGORY_ORDER.index(op.category)
                                         if op.category in CATEGORY_ORDER else 99, op.name))
        return [op for op in ordered if capability in op.capabilities]

    def cheat_sheet(self, capability: Capability) -> str:
        """One line per operation, generated from the schemas (section 7.3)."""
        lines = []
        for op in self.allowed(capability):
            arguments = "" if op.arguments is Arguments else _signature(op.arguments)
            suffix = " Ends the turn." if op.drains else ""
            lines.append(f"- {op.name}({arguments}): {op.summary}{suffix}")
        return "\n".join(lines)

    def actions(self, actor: Actor, category: str | None = None,
                operation: str | None = None) -> dict[str, Any]:
        if operation is not None:
            op = self.operations.get(operation)
            if op is None or actor.capability not in op.capabilities:
                return Forbidden(f"`{operation}` is not available to you now.").to_dict()
            schema = (op.arguments.model_json_schema() if op.arguments is not Arguments
                      else {"type": "object"})
            return {"operation": op.name, "category": op.category, "summary": op.summary,
                    "ends_turn": op.drains, "arguments": schema}
        allowed = self.allowed(actor.capability)
        if category is not None:
            allowed = [op for op in allowed if op.category == category]
        return {"operations": [{"operation": op.name, "category": op.category,
                                "summary": op.summary} for op in allowed]}

    # ------------------------------------------------------------------ dispatch
    async def call(self, actor: Actor, operation: str, arguments: dict[str, Any] | None,
                   call_id: str, runtime: Any = None) -> dict[str, Any]:
        """Invoke ``operation``; never raises."""
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            return await self._call(actor, operation, arguments, call_id, runtime)
        except GeyserError as error:
            return error.to_dict()
        except Exception as error:  # noqa: BLE001 - the boundary that contains bugs
            log.exception("operation %s failed for %s", operation, actor.id)
            self._record_internal_error(operation, error)
            return {"error": "internal",
                    "message": "The Hub hit an internal error on this call and logged it. "
                               "Retry later, or continue without this operation."}

    async def _call(self, actor: Actor, operation: str, arguments: dict[str, Any],
                    call_id: str, runtime: Any) -> dict[str, Any]:
        op = self.operations.get(operation)
        if op is None:
            raise NotFound(f"There is no operation `{operation}`; list them with "
                           f"`{self.hub.config.gateway.prefix}_actions`.")
        if actor.capability not in op.capabilities:
            raise Forbidden(f"`{operation}` is not available to you now "
                            f"(capability `{actor.capability.value}`).")
        digest = arguments_hash(operation, arguments)
        call = CallContext(self.hub, actor, op, call_id, digest, runtime)
        if op.mutating:
            replay = self._replay(call)
            if replay is not None:
                return replay
        if op.arguments is Arguments:
            parsed: Any = arguments
        else:
            try:
                parsed = op.arguments.model_validate(arguments)
            except ValidationError as error:
                raise InvalidArgument(f"Invalid arguments for `{operation}`: "
                                      f"{_format_validation(error)}") from error
        return await op.handler(call, parsed)

    def _replay(self, call: CallContext) -> dict[str, Any] | None:
        with self.hub.read() as txn:
            row = txn.one("SELECT * FROM receipts WHERE key = ?", (call.receipt_key,))
        if row is None:
            return None
        if row["arguments_hash"] != call.arguments_hash or row["operation"] != call.operation.name:
            raise InvalidArgument("This call ID was already used with different arguments.")
        reply = dict(loads(row["reply"], {}))
        reply["replayed"] = True
        return reply

    def _record_internal_error(self, operation: str, error: Exception) -> None:
        signature = sha256_text(f"{operation}:{type(error).__name__}")[:16]
        message = f"Operation {operation} raised {type(error).__name__}: {error}"
        try:
            with self.hub.transaction() as txn:
                txn.execute(
                    "INSERT INTO operator_notices (signature, subject, message, count, "
                    "first_at, last_at) VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT (signature) DO "
                    "UPDATE SET count = count + 1, last_at = excluded.last_at, "
                    "message = excluded.message",
                    (signature, operation, message[:2000], txn.now, txn.now),
                )
                txn.event("internal.error", operation, trace=traceback.format_exc()[-4000:])
        except Exception:  # noqa: BLE001 - recording must never raise
            log.exception("could not record an internal error")
