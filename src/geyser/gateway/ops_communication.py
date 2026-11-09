"""Communication operations: talk_send, talk_read, message_send, request."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from geyser.core import messages
from geyser.core.ledger import Txn
from geyser.gateway.base import EVERY_AGENT, Arguments, CallContext, Operation
from geyser.ids import Kind, require_id


class TalkSendArguments(Arguments):
    to: str
    text: str = Field(min_length=1, max_length=messages.MAX_MESSAGE_CHARS)
    kind: Literal["notice", "question", "answer"] = "notice"
    """A notice wakes no worker; a question and an answer wake their recipient."""
    answers: str | None = None
    """For an answer: the question (``msg-<n>``) it answers. Only the agent asked
    answers, to the asker."""
    message_id: str | None = Field(default=None, max_length=100)
    """A stable key of yours: resending the same key and text returns the first receipt."""


async def talk_send(call: CallContext, args: TalkSendArguments) -> dict[str, Any]:
    return call.commit(lambda txn: messages.send_talk(
        call.ctx, txn, call.actor, args.to, args.text, args.message_id, form=args.kind,
        answers=args.answers))


class TalkReadArguments(Arguments):
    with_agent: str | None = None
    """Read your conversation with this agent."""
    between: list[str] | None = Field(default=None, min_length=2, max_length=2)
    """Read any other pair's conversation."""
    after: int = Field(default=0, ge=0)
    """Only messages numbered after this one (``msg-<n>``)."""
    limit: int = Field(default=20, ge=1, le=50)


async def talk_read(call: CallContext, args: TalkReadArguments) -> dict[str, Any]:
    layout = call.ctx.layout

    def read(txn: Txn) -> dict[str, Any]:
        if args.with_agent is None and args.between is None:
            return {"conversations": messages.talk_conversations(txn)}
        first, second = ((call.actor.id, args.with_agent) if args.with_agent
                         else tuple(args.between or ()))
        for agent in (first, second):
            require_id(agent, Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR, field="agent")
        rows = messages.talk_history(txn, first, second, after=args.after, limit=args.limit)
        return {"between": [first, second], "messages": [
            {"message": row["id"], "from": row["sender"], "to": row["recipient"],
             "kind": row["form"] or "notice", "answers": row["answers"],
             "at": row["created_at"], "body": row["body_path"],
             "text": (layout.root / row["body_path"]).read_text(encoding="utf-8")}
            for row in rows]}

    return call.read(read)


class OperatorMessageArguments(Arguments):
    text: str = Field(min_length=1, max_length=messages.MAX_MESSAGE_CHARS)
    message_id: str | None = Field(default=None, max_length=100)


async def message_send(call: CallContext, args: OperatorMessageArguments) -> dict[str, Any]:
    return call.commit(lambda txn: messages.send_to_operator(call.ctx, txn, call.actor,
                                                             args.text, args.message_id))


class RequestArguments(Arguments):
    kind: Literal["question", "material"]
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=messages.MAX_MESSAGE_CHARS)
    source: str | None = Field(default=None, max_length=300)
    """For material: the exact source and edition, such as a citation."""


async def request(call: CallContext, args: RequestArguments) -> dict[str, Any]:
    def apply(txn: Txn) -> dict[str, Any]:
        task_id = call.actor.task_id
        revision = None
        if task_id:
            revision = txn.scalar("SELECT revision FROM tasks WHERE id = ?", (task_id,))
        elif call.actor.review_id:
            task_id = txn.scalar(
                "SELECT c.task_id FROM reviews r JOIN candidates c ON c.id = r.candidate_id "
                "WHERE r.id = ?", (call.actor.review_id,))
        return messages.create_request(call.ctx, txn, call.actor, args.kind, args.title,
                                       args.text, task_id, revision, args.source)

    return call.commit(apply)


OPERATIONS = [
    Operation("talk_send", "communication", "Message another agent (reviewers and the "
              "Integrator included): a notice, a question you can wait on with `answers`, or "
              "an answer to a question asked of you.", TalkSendArguments, EVERY_AGENT,
              talk_send),
    Operation("talk_read", "communication", "List conversations, or read one pair's messages.",
              TalkReadArguments, EVERY_AGENT, talk_read, mutating=False),
    Operation("message_send", "communication", "Write to the operator.",
              OperatorMessageArguments, EVERY_AGENT, message_send),
    Operation("request", "communication", "Ask the operator a question or for material; "
              "wait on it with external_response_id.", RequestArguments,
              frozenset(EVERY_AGENT), request),
]
