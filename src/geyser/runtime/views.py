"""Model-facing inputs (DESIGN.md section 7.3).

The public view the model sees has ``identity``, ``instruction``, ``task`` (or
``review``), ``paths`` and ``reading``, plus ``messages`` and ``notifications``
when there are any. The private envelope (fencing tokens, delivery cursors, hashes,
recovery bindings) never enters it.

A conversation's first input carries the full view. Later inputs carry ``changes``
(key paths with new values) and ``removed_fields`` for the stable parts; per-turn
parts (instruction, messages, notifications, refresh report) are always included.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from geyser.content.staging import candidate_folder
from geyser.core import candidates, tasks, waits
from geyser.core.ledger import Txn, loads
from geyser.jsonio import pretty_json

if TYPE_CHECKING:
    from geyser.hub import Hub

STABLE_PARTS = ("identity", "task", "review", "batch", "paths", "reading", "waiting")
_EXCERPT = 600


@dataclass
class BuiltView:
    full: dict[str, Any]
    stable: dict[str, Any]
    text: str
    item_ids: list[int] = field(default_factory=list)


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _EXCERPT else flat[: _EXCERPT - 1] + "…"


def _paths(hub: Hub, agent_id: str, task_id: str | None) -> dict[str, Any]:
    layout = hub.layout
    paths: dict[str, Any] = {
        "workspace": str(layout.workspace(agent_id)),
        "library": str(layout.library),
        "journal": str(layout.journal(agent_id)),
    }
    readme = layout.library / "wiki" / "README.md"
    if readme.exists():
        paths["knowledge_readme"] = str(readme)
    if task_id:
        paths["handoff"] = str(layout.handoff_file(agent_id, task_id))
        state_file = layout.state_file(agent_id, task_id)
        if state_file.exists():
            paths["state_file"] = str(state_file)
    return paths


def task_block(hub: Hub, txn: Txn, agent_id: str, task_id: str) -> dict[str, Any]:
    row = tasks.task_row(txn, task_id)
    contract = tasks.load_contract(hub.ctx, txn, task_id)
    membership = txn.one("SELECT role, plan FROM memberships WHERE task_id = ? AND agent_id = ?",
                         (task_id, agent_id))
    team = [
        {"agent": member["agent_id"], "role": member["role"], "state": member["state"]}
        for member in txn.all("SELECT agent_id, role, state FROM memberships WHERE task_id = ? "
                              "AND state IN ('active', 'waiting') ORDER BY joined_at",
                              (task_id,))
    ]
    block: dict[str, Any] = {
        "task": f"{task_id} ({row['title']})",
        "revision": row["revision"],
        "state": row["state"],
        "kind": row["kind"],
        "objective_excerpt": _excerpt(contract.objective),
        "contract": str(hub.layout.task_markdown(task_id)),
        "condition_keys": sorted(contract.conditions()),
        "your_role": membership["role"] if membership else None,
        "team": team,
    }
    if membership and membership["plan"]:
        block["plan"] = membership["plan"]
    own = txn.all("SELECT id, state, kind FROM candidates WHERE task_id = ? AND "
                  "corrected_by IS NULL ORDER BY created_at", (task_id,))
    if own:
        block["candidates"] = [{"candidate": c["id"], "state": c["state"], "kind": c["kind"]}
                               for c in own]
    declaration = txn.one("SELECT id, state, declarer, feedback_from FROM declarations WHERE "
                          "task_id = ? AND state IN ('awaiting_feedback', 'deciding')",
                          (task_id,))
    if declaration:
        block["completion"] = dict(declaration)
    open_place = txn.one("SELECT role FROM opportunities WHERE task_id = ? AND state = 'open' "
                         "AND role = 'collaborator'", (task_id,))
    if open_place:
        block["recruiting"] = True
    return block


def review_block(hub: Hub, txn: Txn, review_id: str) -> dict[str, Any]:
    review = candidates.review_row(txn, review_id)
    candidate = candidates.candidate_row(txn, review["candidate_id"])
    folder = candidate_folder(hub, candidate["id"])
    manifest = loads((folder / "manifest.json").read_text(encoding="utf-8"), {})
    block: dict[str, Any] = {
        "review": review_id,
        "candidate": candidate["id"],
        "kind": candidate["kind"],
        "policy": candidate["policy"],
        "task": tasks.title_of(txn, candidate["task_id"]),
        "task_revision": candidate["task_revision"],
        "contract": str(hub.layout.revision_file(candidate["task_id"],
                                                  candidate["task_revision"])),
        "introduction": manifest.get("submission", {}).get("introduction", ""),
        "files": str(folder / "files"),
        "manifest": str(folder / "manifest.json"),
        "changed": sorted(manifest.get("changed", {})),
        "deleted": manifest.get("deleted", []),
        "reports": str(folder / "reports"),
        "authors": candidates.authors_of(candidate),
        "state": review["state"],
    }
    if review["report_path"]:
        block["report"] = str(hub.layout.root / review["report_path"])
    replaced = candidate["replaces"]
    earlier = []
    while replaced:
        report = txn.scalar("SELECT report_path FROM reviews WHERE candidate_id = ?",
                            (replaced,))
        if report:
            earlier.append({"candidate": replaced, "report": str(hub.layout.root / report)})
        replaced = txn.scalar("SELECT replaces FROM candidates WHERE id = ?", (replaced,))
    if earlier:
        block["earlier_reviews"] = earlier
    return block


def _instruction(purpose: str, intent: str, agent: Row, task_id: str | None,
                 review_id: str | None) -> str:
    if purpose == "message" and task_id is None and review_id is None:
        return ("You have no task in hand. Handle the messages below: answer each question "
                "asked of you with talk_send (kind `answer`). Then end the turn.")
    if purpose == "message":
        return ("You are waiting. Handle the messages and notifications below. End the turn "
                "to keep waiting, or call resume_work to resume your work after this turn.")
    if purpose == "review":
        if intent == "start":
            return (f"Review {review_id}: read the contract and the candidate, check every "
                    "condition, and record your verdict with `review`.")
        return f"Continue your review {review_id}."
    if purpose == "answer":
        return ("Answer the questions below about your review. Your report is your memory "
                "of it. Use talk_send to reply; you cannot change the verdict.")
    if purpose == "integration":
        return "Integrate the current batch (see `batch`), following the integrator guide."
    if intent == "start":
        return (f"Start {task_id}: read the contract, plan the work in your handoff file, "
                "and begin.")
    if intent == "resume":
        return (f"Resume {task_id}: the condition you waited for changed. Verify the exact "
                "result before relying on it, then continue from your checkpoint.")
    return f"Continue {task_id}."


def build(hub: Hub, txn: Txn, activation: Row, agent: Row, *, capability: str,
          intent: str, new_conversation: bool, refresh: dict[str, Any] | None,
          on_demand: list[str]) -> BuiltView:
    """Build the input of one activation, as a delta when the conversation continues."""
    from geyser.core import inbox

    agent_id = agent["id"]
    purpose = activation["purpose"]
    task_id = activation["task_id"]
    review_id = activation["review_id"]
    stable: dict[str, Any] = {
        "identity": {"agent": agent_id, "role": agent["kind"], "backend": agent["backend"],
                     "capability": capability},
        "paths": _paths(hub, agent_id, task_id),
        "reading": {"on_demand": on_demand} if on_demand else {},
    }
    if task_id:
        stable["task"] = task_block(hub, txn, agent_id, task_id)
        waiting = waits.waiting_context(txn, agent_id, task_id)
        if waiting and purpose in ("message", "task") and intent != "continue":
            stable["waiting"] = waiting
    if review_id:
        stable["review"] = review_block(hub, txn, review_id)
        if agent["review_wait"]:
            stable["waiting"] = loads(agent["review_wait"])
    items = inbox.pending(txn, agent_id)
    per_turn: dict[str, Any] = {"instruction": _instruction(purpose, intent, agent, task_id,
                                                             review_id)}
    message_items = [item for item in items if item["item"] == "message"]
    notice_items = [item for item in items if item["item"] != "message"]
    if message_items:
        per_turn["messages"] = [_message_entry(hub, txn, item) for item in message_items]
    if notice_items:
        per_turn["notifications"] = [item["summary"] for item in notice_items]
    if refresh:
        per_turn["workspace_refresh"] = refresh
    baseline = loads(agent["view_baseline"]) if not new_conversation else None
    if baseline is None:
        full = {"geyser": 1, **stable, **per_turn}
    else:
        changes, removed = diff_views(baseline, stable)
        full = {"geyser": 1, **per_turn}
        if changes:
            full["changes"] = changes
        if removed:
            full["removed_fields"] = removed
    return BuiltView(full=full, stable=stable, text=pretty_json(full),
                     item_ids=[item["id"] for item in items])


def _message_entry(hub: Hub, txn: Txn, item: Row) -> dict[str, Any]:
    message = txn.one("SELECT * FROM messages WHERE id = ?", (item["ref"],))
    if message is None:
        return {"summary": item["summary"]}
    entry: dict[str, Any] = {"message": message["id"], "from": message["sender"],
             "sender_role": message["sender_role"],
             "file": str(hub.layout.root / message["body_path"]),
             "summary": message["summary"]}
    if message["form"]:
        entry["kind"] = message["form"]
    if message["form"] == "question":
        entry["answer_with"] = {"kind": "answer", "answers": message["id"],
                                "to": message["sender"]}
    if message["answers"]:
        entry["answers"] = message["answers"]
    return entry


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}{key}"
        if isinstance(item, dict) and item:
            flat.update(_flatten(item, path + "."))
        else:
            flat[path] = item
    return flat


def diff_views(old: dict[str, Any], new: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Key-path changes and removed key paths between two stable views."""
    before, after = _flatten(old), _flatten(new)
    changes = {key: value for key, value in after.items() if before.get(key, object()) != value}
    removed = sorted(key for key in before if key not in after)
    return changes, removed
