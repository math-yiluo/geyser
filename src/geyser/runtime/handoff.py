"""Memory across conversations: the state file and handoff checks (DESIGN.md section 7.4)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from geyser.core.ledger import Txn
from geyser.jsonio import atomic_write_text

if TYPE_CHECKING:
    from geyser.hub import Hub

HANDOFF_SECTIONS = ("Goal", "Current state", "Next step", "Remaining plan", "Key decisions",
                    "Dead ends", "Key facts", "Open questions")
REVIEW_SECTIONS = ("Goal", "Checked so far", "Findings", "Next step", "Open questions")
REQUIRED = 3
MAX_HANDOFF_CHARS = 12_000  # About 3,000 tokens.


def write_state_file(hub: Hub, txn: Txn, agent_id: str, task_id: str) -> str:
    """The Hub's own record of a task, written whenever a new conversation starts on it."""
    task = txn.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    lines = [f"# State of {task_id} for {agent_id}", "",
             "Written by the Hub when this conversation started. Your handoff file and "
             "journal are your own memory; this file is the Hub's record.", ""]
    if task is not None:
        lines += [f"- Task: {task_id} ({task['title']}), revision {task['revision']}, "
                  f"state {task['state']}"]
    team = txn.all("SELECT agent_id, role, state FROM memberships WHERE task_id = ?",
                   (task_id,))
    if team:
        lines.append("- Team: " + ", ".join(f"{m['agent_id']} ({m['role']}, {m['state']})"
                                            for m in team))
    rows = txn.all("SELECT id, state, kind, reason FROM candidates WHERE task_id = ? "
                   "ORDER BY created_at", (task_id,))
    if rows:
        lines += ["", "## Candidates", ""]
        lines += [f"- {r['id']} ({r['kind']}): {r['state']}"
                  + (f" — {r['reason']}" if r["reason"] else "") for r in rows]
    recent = txn.all(
        "SELECT kind, at, data FROM events WHERE (actor = ? OR subject = ?) "
        "ORDER BY seq DESC LIMIT 15", (agent_id, task_id))
    if recent:
        lines += ["", "## Recent events", ""]
        lines += [f"- {r['at'][:19]} {r['kind']}" for r in reversed(recent)]
    failures = txn.all(
        "SELECT ended_at, outcome, detail FROM activations WHERE agent_id = ? "
        "AND outcome IN ('failed', 'closed_unknown', 'interrupted') "
        "ORDER BY ended_at DESC LIMIT 5", (agent_id,))
    if failures:
        lines += ["", "## Recent interruptions and errors", ""]
        lines += [f"- {r['ended_at'][:19] if r['ended_at'] else '?'} {r['outcome']}"
                  for r in failures]
    path = hub.layout.state_file(agent_id, task_id)
    atomic_write_text(path, "\n".join(lines) + "\n")
    return str(path)


def check_handoff(text: str | None, sections: tuple[str, ...] = HANDOFF_SECTIONS,
                  workspace_exists=None) -> list[str]:
    """Problems with a handoff file; an empty list means it is usable."""
    if text is None:
        return ["the handoff file does not exist"]
    problems = []
    if len(text) > MAX_HANDOFF_CHARS:
        problems.append(f"it is longer than {MAX_HANDOFF_CHARS:,} characters")
    bodies = _sections(text)
    for name in sections[:REQUIRED]:
        if not bodies.get(name.lower(), "").strip():
            problems.append(f"the section '{name}' is empty or missing")
    if workspace_exists is not None:
        for path in re.findall(r"`([^`\s]+/[^`\s]+)`", text):
            if not workspace_exists(path):
                problems.append(f"it names `{path}`, which does not exist")
    return problems


def _sections(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if heading:
            current = heading.group(1).strip().lower()
            result[current] = ""
        elif current is not None:
            result[current] += line + "\n"
    return result
