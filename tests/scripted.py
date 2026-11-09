"""Scripted agent behaviors for scenario tests."""

from __future__ import annotations

from typing import Any

from geyser.runtime.backends.scripted import ScriptedTurn


async def evidence_for(turn: ScriptedTurn, task_id: str, artifacts: list[str]
                       ) -> dict[str, Any]:
    info = await turn.call("inspect", {"id": task_id})
    return {key: {"explanation": f"Satisfied by {', '.join(artifacts) or 'the subtasks'}.",
                  "artifacts": artifacts} for key in info["condition_keys"]}


def task_id_of(turn: ScriptedTurn) -> str:
    return turn.request.agent_id and turn.view.get("task", {}).get("task", "").split(" ")[0] \
        or ""


class SimpleWorker:
    """Writes one file, contributes it, completes once it is published."""

    def __init__(self, filename: str = "result.md", text: str = "done\n"):
        self.filename = filename
        self.text = text
        self.task_of: dict[str, str] = {}

    async def __call__(self, turn: ScriptedTurn) -> None:
        if "task" in turn.view:
            self.task_of[turn.agent_id] = turn.view["task"]["task"].split(" ")[0]
        task_id = self.task_of[turn.agent_id]
        info = await turn.call("inspect", {"id": task_id})
        published = [c["candidate"] for c in info["candidates"] if c["state"] == "published"]
        pending = [c for c in info["candidates"] if c["state"] in ("validating", "reviewing",
                                                                   "accepted")]
        if published:
            evidence = await evidence_for(turn, task_id, published)
            await turn.call("complete", {"evidence": evidence})
            return
        if pending:
            return
        turn.write(f"{task_id}/{self.filename}", self.text)
        rejected = [c["candidate"] for c in info["candidates"] if c["state"] == "rejected"]
        arguments = {"kind": "general.work", "paths": [f"{task_id}/{self.filename}"],
                     "introduction": f"The {self.filename} of {task_id}."}
        if rejected:
            arguments["replaces"] = rejected[-1]
        await turn.call("contribute", arguments)


class AcceptingReviewer:
    def __init__(self, reject_first: int = 0):
        self.reject_first = reject_first
        self.reviews = 0

    async def __call__(self, turn: ScriptedTurn) -> None:
        review = turn.view.get("review")
        if review is None:
            return
        self.reviews += 1
        if self.reviews <= self.reject_first:
            await turn.call("review", {
                "decision": "reject", "summary": "Not good enough.",
                "findings": [{"summary": "Missing content", "location": review["changed"][0]}],
                "suggestions": ["Write more."]})
            return
        await turn.call("review", {
            "decision": "accept", "summary": "It meets the contract.",
            "findings": [{"summary": "Present", "location": review["changed"][0] + ":1"}]})


def no_decider(request) -> dict[str, Any]:
    """No collaborator, a new worker for every task, and every wait woken."""
    if "collaborator" in request.prompt and '"collaborator"' in request.prompt:
        return {"collaborator": False}
    if request.prompt.startswith("You judge whether waiting agents can go on"):
        return {key: True for key in request.schema["required"]}
    return {"worker": None}
