"""Talk, operator messages and requests (DESIGN.md section 11).

Bodies are immutable files; the ledger keeps routing and delivery state. Every
message carries a trusted ``sender_role``; only ``operator`` carries human
authority. Peer text is collaboration between equals, never a command.
"""

from __future__ import annotations

from sqlite3 import Row
from typing import Any

from geyser.core import inbox
from geyser.core.actors import Actor
from geyser.core.context import HubContext
from geyser.core.ledger import Txn
from geyser.errors import Conflict, Forbidden, InvalidArgument, NotFound
from geyser.ids import Kind, require_id
from geyser.jsonio import atomic_write_text, sha256_text

MAX_MESSAGE_CHARS = 40_000


def _pair_name(first: str, second: str) -> str:
    return "__".join(sorted((first, second)))


def _check_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise InvalidArgument("A message needs nonempty text.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidArgument(f"A message is at most {MAX_MESSAGE_CHARS:,} characters; put "
                              "longer material in a file and name its path.")


def _existing_by_key(txn: Txn, sender: str, client_key: str | None, text: str
                     ) -> dict[str, Any] | None:
    if not client_key:
        return None
    row = txn.one("SELECT * FROM messages WHERE sender = ? AND client_key = ?",
                  (sender, client_key))
    if row is None:
        return None
    if row["body_hash"] != sha256_text(text):
        raise Conflict(f"message_id {client_key!r} was already used for a different text.")
    return {"message": row["id"], "to": row["recipient"], "replayed": True}


FORMS = ("notice", "question", "answer")
"""What a peer message is (section 11.2). Questions and answers wake their recipient;
a notice wakes no worker."""


def send_talk(ctx: HubContext, txn: Txn, actor: Actor, recipient: str, text: str,
              client_key: str | None = None, *, form: str = "notice",
              answers: str | None = None) -> dict[str, Any]:
    """Send a peer message; each pair of agents shares exactly one conversation."""
    require_id(recipient, Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR, field="to")
    _check_text(text)
    if recipient == actor.id:
        raise InvalidArgument("An agent does not message itself; write a note instead.")
    if form not in FORMS:
        raise InvalidArgument("`kind` is `notice`, `question` or `answer`.")
    replay = _existing_by_key(txn, actor.id, client_key, text)
    if replay:
        return replay
    target = txn.one("SELECT state, kind FROM agents WHERE id = ?", (recipient,))
    if target is None:
        raise NotFound(f"{recipient} does not exist.")
    if target["state"] == "retired":
        raise Conflict(f"{recipient} is retired and receives no messages.")
    if form == "answer":
        _check_answer(txn, actor.id, recipient, answers)
    elif answers is not None:
        raise InvalidArgument("Only an answer names the question it answers.")
    message_id = txn.next_id(Kind.MESSAGE)
    path = ctx.layout.talk / _pair_name(actor.id, recipient) / f"{message_id}.md"
    atomic_write_text(path, text)
    summary = inbox.one_line(text, 160)
    txn.execute(
        "INSERT INTO messages (id, kind, sender, sender_role, form, answers, recipient, "
        "body_path, body_hash, summary, client_key, created_at) "
        "VALUES (?, 'talk', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, actor.id, actor.sender_role, form, answers, recipient,
         ctx.layout.relative(path), sha256_text(text), summary, client_key, txn.now),
    )
    heading = {"notice": f"{message_id} from {actor.id}",
               "question": f"{message_id}, a question from {actor.id} (answer it with "
                           f"talk_send kind `answer`, answers `{message_id}`)",
               "answer": f"{message_id}, {actor.id}'s answer to your question {answers}"}[form]
    wakes = form != "notice" or target["kind"] != "worker"
    inbox.notify(txn, recipient, f"{heading}: {summary}", ref=message_id, item="message",
                 wakes=wakes)
    txn.event("talk.sent", message_id, actor.id, to=recipient, form=form, answers=answers)
    txn.after_commit(ctx.wake)
    reply: dict[str, Any] = {"message": message_id, "to": recipient, "kind": form}
    if form == "question":
        reply["wait_with"] = {"answers": [message_id]}
    return reply


def _check_answer(txn: Txn, sender: str, recipient: str, question_id: str | None) -> None:
    """Only the agent asked answers, and the answer goes to the asker."""
    if question_id is None:
        raise InvalidArgument("An answer names the question it answers in `answers`.")
    require_id(question_id, Kind.MESSAGE, field="answers")
    question = txn.one("SELECT sender, recipient, form FROM messages WHERE id = ?",
                       (question_id,))
    if question is None:
        raise NotFound(f"{question_id} does not exist.")
    if question["form"] != "question":
        raise InvalidArgument(f"{question_id} is not a question.")
    if question["recipient"] != sender:
        raise Forbidden(f"{question_id} was asked of {question['recipient']}; only it answers.")
    if question["sender"] != recipient:
        raise InvalidArgument(f"The answer to {question_id} goes to its asker, "
                              f"{question['sender']}.")


def send_operator_message(ctx: HubContext, txn: Txn, recipient: str, text: str,
                          client_key: str | None = None) -> dict[str, Any]:
    """A directed message from the operator; the only kind with human authority."""
    require_id(recipient, Kind.WORKER, Kind.REVIEWER, Kind.INTEGRATOR, field="to")
    _check_text(text)
    replay = _existing_by_key(txn, "operator", client_key, text)
    if replay:
        return replay
    if txn.one("SELECT 1 FROM agents WHERE id = ? AND state != 'retired'", (recipient,)) is None:
        raise NotFound(f"{recipient} is not an active agent.")
    message_id = txn.next_id(Kind.MESSAGE)
    path = ctx.layout.correspondence / "messages" / f"{message_id}.md"
    atomic_write_text(path, text)
    summary = inbox.one_line(text, 160)
    txn.execute(
        "INSERT INTO messages (id, kind, sender, sender_role, recipient, body_path, body_hash, "
        "summary, client_key, created_at) VALUES (?, 'operator', 'operator', 'operator', ?, ?, "
        "?, ?, ?, ?)",
        (message_id, recipient, ctx.layout.relative(path), sha256_text(text), summary,
         client_key, txn.now),
    )
    inbox.notify(txn, recipient, f"{message_id} from the operator: {summary}", ref=message_id,
                 item="message", wakes=True)
    txn.event("operator.message", message_id, "operator", to=recipient)
    txn.after_commit(ctx.wake)
    return {"message": message_id, "to": recipient}


def send_to_operator(ctx: HubContext, txn: Txn, actor: Actor, text: str,
                     client_key: str | None = None) -> dict[str, Any]:
    """``message_send``: an agent writes to the operator."""
    _check_text(text)
    replay = _existing_by_key(txn, actor.id, client_key, text)
    if replay:
        return replay
    message_id = txn.next_id(Kind.MESSAGE)
    path = ctx.layout.correspondence / "inbound" / f"{message_id}.md"
    atomic_write_text(path, text)
    txn.execute(
        "INSERT INTO messages (id, kind, sender, sender_role, recipient, body_path, body_hash, "
        "summary, client_key, created_at) VALUES (?, 'operator', ?, ?, 'operator', ?, ?, ?, ?, "
        "?)",
        (message_id, actor.id, actor.sender_role, ctx.layout.relative(path), sha256_text(text),
         inbox.one_line(text, 160), client_key, txn.now),
    )
    txn.event("operator.inbound", message_id, actor.id)
    return {"message": message_id, "to": "operator"}


def withdraw_operator_message(txn: Txn, message_id: str) -> dict[str, Any]:
    """The operator may withdraw a message until a delivery of it is prepared."""
    row = txn.one("SELECT * FROM messages WHERE id = ? AND sender = 'operator'", (message_id,))
    if row is None:
        raise NotFound(f"{message_id} is not an operator message.")
    item = txn.one("SELECT state FROM inbox WHERE ref = ? AND item = 'message'", (message_id,))
    if item is not None and item["state"] != "pending":
        raise Conflict(f"{message_id} is already being delivered.")
    txn.execute("UPDATE messages SET withdrawn = 1 WHERE id = ?", (message_id,))
    txn.execute("UPDATE inbox SET state = 'dropped' WHERE ref = ? AND state = 'pending'",
                (message_id,))
    txn.event("operator.message_withdrawn", message_id, "operator")
    return {"message": message_id, "withdrawn": True}


def talk_conversations(txn: Txn, agent_id: str | None = None) -> list[dict[str, Any]]:
    """List pair conversations (every agent can read every talk history)."""
    rows = txn.all(
        "SELECT sender, recipient, COUNT(*) AS n, MAX(created_at) AS last FROM messages "
        "WHERE kind = 'talk' GROUP BY sender, recipient"
    )
    pairs: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = _pair_name(row["sender"], row["recipient"])
        entry = pairs.setdefault(name, {"between": sorted((row["sender"], row["recipient"])),
                                        "messages": 0, "last": row["last"]})
        entry["messages"] += row["n"]
        entry["last"] = max(entry["last"], row["last"])
    result = sorted(pairs.values(), key=lambda entry: entry["last"], reverse=True)
    if agent_id:
        result = [entry for entry in result if agent_id in entry["between"]]
    return result


def talk_history(txn: Txn, first: str, second: str, *, after: int = 0, limit: int = 20
                 ) -> list[Row]:
    """Messages between two agents, oldest first, after message number ``after``."""
    rows = txn.all(
        "SELECT * FROM messages WHERE kind = 'talk' AND ((sender = ? AND recipient = ?) "
        "OR (sender = ? AND recipient = ?)) ORDER BY created_at, id",
        (first, second, second, first),
    )
    selected = [row for row in rows if int(row["id"].split("-")[1]) > after] if after else rows
    return selected[:limit]


# ------------------------------------------------------------------------ requests
def create_request(ctx: HubContext, txn: Txn, actor: Actor, kind: str, title: str, text: str,
                   task_id: str | None, revision: int | None, source_key: str | None = None
                   ) -> dict[str, Any]:
    """``request``: ask the operator a question or for material (section 11.4)."""
    if kind not in {"question", "material"}:
        raise InvalidArgument("`kind` is `question` or `material`.")
    if not title.strip() or len(title) > 300:
        raise InvalidArgument("A request title is 1 to 300 characters.")
    _check_text(text)
    content_hash = sha256_text(f"{title}\n{text}")
    if kind == "question":
        if task_id is None:
            raise InvalidArgument("A question is asked within a task.")
        key = f"question:{task_id}:{revision}:{content_hash}"
    else:
        normalized = " ".join((source_key or title).lower().split())
        key = f"material:{normalized}"
    existing = txn.one("SELECT * FROM requests WHERE key = ?", (key,))
    if existing is not None:
        if existing["content_hash"] != content_hash:
            raise Conflict(
                f"{existing['id']} already requests this material with different text; "
                "join it with identical title and text, or name a different source.",
                details={"request": existing["id"]},
            )
        _add_waiter(txn, existing["id"], actor.id)
        return {"request": existing["id"], "state": existing["state"], "joined": True}
    request_id = txn.next_id(Kind.REQUEST)
    path = ctx.layout.correspondence / "requests" / request_id / "request.md"
    atomic_write_text(path, f"# {title}\n\n{text}\n")
    txn.execute(
        "INSERT INTO requests (id, kind, key, asker, task_id, revision, title, body_path, "
        "content_hash, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (request_id, kind, key, actor.id, task_id, revision, title, ctx.layout.relative(path),
         content_hash, txn.now),
    )
    _add_waiter(txn, request_id, actor.id)
    txn.event("request.created", request_id, actor.id, kind=kind, title=title)
    return {"request": request_id, "state": "open",
            "wait_with": {"external_response_id": request_id}}


def _add_waiter(txn: Txn, request_id: str, agent_id: str) -> None:
    txn.execute("INSERT OR IGNORE INTO request_waiters (request_id, agent_id) VALUES (?, ?)",
                (request_id, agent_id))


def request_row(txn: Txn, request_id: str) -> Row:
    require_id(request_id, Kind.REQUEST, field="request_id")
    row = txn.one("SELECT * FROM requests WHERE id = ?", (request_id,))
    if row is None:
        raise NotFound(f"{request_id} does not exist.")
    return row


def answer_request(ctx: HubContext, txn: Txn, request_id: str, text: str) -> dict[str, Any]:
    """The operator answers a request. Completed responses are immutable."""
    row = request_row(txn, request_id)
    if row["state"] != "open":
        raise Conflict(f"{request_id} is already {row['state']}.")
    _check_text(text)
    path = ctx.layout.correspondence / "requests" / request_id / "answer.md"
    atomic_write_text(path, text)
    txn.execute(
        "UPDATE requests SET state = 'answered', answer_path = ?, answered_by = 'operator', "
        "answered_at = ? WHERE id = ?",
        (ctx.layout.relative(path), txn.now, request_id),
    )
    _notify_waiters(txn, request_id, f"The operator answered {request_id} ({row['title']}). "
                    f"Answer: {ctx.layout.relative(path)}")
    txn.event("request.answered", request_id, "operator")
    txn.after_commit(ctx.wake)
    return {"request": request_id, "state": "answered"}


def resolve_material(ctx: HubContext, txn: Txn, actor: Actor, request_id: str,
                     source_id: str) -> dict[str, Any]:
    """``request_resolve``: anyone who obtained and registered the material resolves it."""
    row = request_row(txn, request_id)
    if row["kind"] != "material":
        raise InvalidArgument(f"{request_id} is a question; only the operator answers it.")
    if row["state"] != "open":
        raise Conflict(f"{request_id} is already {row['state']}.")
    require_id(source_id, Kind.SOURCE, field="source_id")
    if txn.one("SELECT 1 FROM sources WHERE id = ?", (source_id,)) is None:
        raise Forbidden(f"{source_id} is not registered; register the material first.")
    txn.execute(
        "UPDATE requests SET state = 'resolved', answer_path = ?, answered_by = ?, "
        "answered_at = ? WHERE id = ?",
        (source_id, actor.id, txn.now, request_id),
    )
    _notify_waiters(txn, request_id, f"{request_id} ({row['title']}) is resolved by "
                    f"{source_id}.")
    txn.event("request.resolved", request_id, actor.id, source=source_id)
    txn.after_commit(ctx.wake)
    return {"request": request_id, "state": "resolved", "source": source_id}


def _notify_waiters(txn: Txn, request_id: str, summary: str) -> None:
    waiters = [r["agent_id"] for r in txn.all(
        "SELECT agent_id FROM request_waiters WHERE request_id = ?", (request_id,))]
    inbox.notify_many(txn, waiters, summary, ref=request_id)


def message_row(txn: Txn, message_id: str) -> Row:
    row = txn.one("SELECT * FROM messages WHERE id = ?", (message_id,))
    if row is None:
        raise NotFound(f"{message_id} does not exist.")
    return row
