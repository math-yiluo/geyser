"""Contribution operations: stage, contribute, withdraw, wiki_submit, fetch."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from geyser.content.submit import submit_candidate
from geyser.core import candidates, tasks, waits
from geyser.core.actors import Capability
from geyser.core.ledger import Txn
from geyser.core.models import Checkpoint, Manifest, Predicate
from geyser.errors import Conflict
from geyser.gateway.base import READERS, Arguments, CallContext, Operation
from geyser.ids import Kind, require_id

WORKER_ONLY = frozenset({Capability.WORKER})


class SubmitArguments(Arguments):
    kind: str = Field(min_length=1, max_length=100)
    paths: list[str] = Field(min_length=1, max_length=500)
    introduction: str = Field(min_length=1, max_length=8_000)
    coauthors: list[str] = Field(default_factory=list, max_length=50)
    replaces: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    task_revision: int | None = Field(default=None, ge=0)
    """The revision you worked against; refused if the task moved on."""


def _manifest(call: CallContext, args: SubmitArguments, kind: str | None = None) -> Manifest:
    task_id = call.actor.task_id
    if task_id is None:
        raise Conflict("This turn has no current task to contribute to.")
    with call.hub.read() as txn:
        current = tasks.task_row(txn, task_id)["revision"]
    return Manifest(
        kind=kind or args.kind,
        task_id=task_id,
        task_revision=current if args.task_revision is None else args.task_revision,
        paths=args.paths,
        introduction=args.introduction,
        coauthors=args.coauthors,
        replaces=args.replaces,
        payload=args.payload,
    )


async def stage(call: CallContext, args: SubmitArguments) -> dict[str, Any]:
    """Freeze a candidate and continue the turn."""
    manifest = _manifest(call, args)
    return await submit_candidate(call.hub, call.actor, manifest, fenced=True,
                                  finish=lambda txn, reply: call._store_receipt(
                                      txn, reply, "committed"))


async def contribute(call: CallContext, args: SubmitArguments) -> dict[str, Any]:
    """Record the intent and drain; the candidate is frozen once processes are quiet."""
    manifest = _manifest(call, args)

    def check(txn: Txn) -> None:
        candidates.check_submission(call.ctx, txn, call.actor, manifest)

    return call.drain({"manifest": manifest.model_dump(mode="json")},
                      {"contribution": "accepted", "task": manifest.task_id,
                       "note": "The candidate is frozen once this turn's processes are quiet; "
                               "the task resumes when the candidate has its outcome "
                               "(published, rejected, returned or withdrawn)."},
                      check)


async def freeze_contribution(hub, actor, payload: dict[str, Any],
                              remember) -> dict[str, Any]:
    """The drained commit of ``contribute``, part 1: freeze and register the candidate.

    ``remember`` runs in the registering transaction and records the candidate in the
    drain, so a retry after a crash never freezes a second candidate.
    """
    manifest = Manifest.model_validate(payload["manifest"])
    return await submit_candidate(hub, actor, manifest, fenced=False, finish=remember)


def wait_for_outcome(hub_ctx, txn: Txn, actor, payload: dict[str, Any], candidate_id: str
                     ) -> None:
    """Part 2: the task waits on the candidate and resumes when its outcome arrives.

    The wait names the candidate's publication (section 12): it ends once the
    candidate is published, rejected, returned, deferred, withdrawn, invalidated or
    accepted under a policy that does not publish.
    """
    task_id = payload["manifest"]["task_id"]
    membership = txn.scalar("SELECT state FROM memberships WHERE task_id = ? AND agent_id = ?",
                            (task_id, actor.id))
    if membership != "active":
        return
    waits.pause(hub_ctx, txn, actor, task_id, f"awaiting the outcome of {candidate_id}",
                Checkpoint(summary=f"Contributed {candidate_id}.",
                           next_action=f"Continue once {candidate_id} is published or its "
                                       "review asks for a correction."),
                Predicate(publications=[candidate_id]), explicit=False)


class WithdrawArguments(Arguments):
    candidate_id: str
    reason: str = Field(min_length=1, max_length=4_000)


async def withdraw(call: CallContext, args: WithdrawArguments) -> dict[str, Any]:
    require_id(args.candidate_id, Kind.CANDIDATE, field="candidate_id")
    return call.commit(lambda txn: candidates.withdraw(call.ctx, txn, call.actor,
                                                       args.candidate_id, args.reason))


class WikiArguments(Arguments):
    paths: list[str] = Field(min_length=1, max_length=200)
    introduction: str = Field(min_length=1, max_length=8_000)
    coauthors: list[str] = Field(default_factory=list, max_length=50)
    replaces: str | None = None


async def wiki_submit(call: CallContext, args: WikiArguments) -> dict[str, Any]:
    """Freeze a Wiki note candidate (Markdown under wiki/ or notes/)."""
    submit = SubmitArguments(kind="note", paths=args.paths, introduction=args.introduction,
                             coauthors=args.coauthors, replaces=args.replaces)
    manifest = _manifest(call, submit, kind="note")
    return await submit_candidate(call.hub, call.actor, manifest, fenced=True,
                                  finish=lambda txn, reply: call._store_receipt(
                                      txn, reply, "committed"))


class FetchArguments(Arguments):
    candidate_id: str


async def fetch(call: CallContext, args: FetchArguments) -> dict[str, Any]:
    """Where a candidate's files, manifest, reports and review are."""
    require_id(args.candidate_id, Kind.CANDIDATE, field="candidate_id")
    layout = call.ctx.layout

    def read(txn: Txn) -> dict[str, Any]:
        row = candidates.candidate_row(txn, args.candidate_id)
        for folder in (layout.staging(row["id"]), layout.archive(row["id"]),
                       layout.history(row["id"])):
            if folder.exists():
                break
        result = candidates.summary(row)
        result["manifest"] = layout.relative(folder / "manifest.json")
        result["files"] = layout.relative(folder / "files")
        result["reports"] = layout.relative(folder / "reports")
        review = txn.one("SELECT report_path FROM reviews WHERE candidate_id = ?", (row["id"],))
        if review and review["report_path"]:
            result["review_report"] = review["report_path"]
        return result

    return call.read(read)


OPERATIONS = [
    Operation("stage", "contributions", "Freeze a candidate from listed workspace paths and "
              "continue the turn.", SubmitArguments, WORKER_ONLY, stage),
    Operation("contribute", "contributions", "Like stage, but ends the turn; the task resumes "
              "when the candidate has its outcome.", SubmitArguments, WORKER_ONLY, contribute,
              drains=True),
    Operation("withdraw", "contributions", "Withdraw a candidate you authored, with a reason.",
              WithdrawArguments, WORKER_ONLY, withdraw),
    Operation("wiki_submit", "contributions", "Submit Wiki pages (Markdown under wiki/ or "
              "notes/) for review.", WikiArguments, WORKER_ONLY, wiki_submit),
    Operation("fetch", "contributions", "Locate a candidate's files, manifest, reports and "
              "review report.", FetchArguments, READERS, fetch, mutating=False),
]
