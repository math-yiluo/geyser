"""The review operation: record a verdict on the exact candidate under review."""

from __future__ import annotations

from typing import Any

from geyser.core import candidates
from geyser.core.actors import Capability
from geyser.core.ledger import Txn
from geyser.core.models import ReviewVerdict
from geyser.errors import Conflict
from geyser.gateway.base import CallContext, Operation
from geyser.jsonio import atomic_write_text


class ReviewArguments(ReviewVerdict):
    """Accept with concrete findings, or reject demonstrated defects (section 8.4)."""


def render_report(review_id: str, candidate_id: str, reviewer: str, verdict: ReviewVerdict
                  ) -> str:
    lines = [f"# Review {review_id} of {candidate_id}", "",
             f"Reviewer: {reviewer} · Decision: **{verdict.decision}**", "",
             "## Summary", "", verdict.summary.strip(), ""]
    if verdict.findings:
        lines += ["## Findings", ""]
        lines += [f"- `{finding.location}`: {finding.summary}" for finding in verdict.findings]
        lines.append("")
    if verdict.suggestions:
        lines += ["## Suggestions", ""] + [f"- {text}" for text in verdict.suggestions] + [""]
    notes = verdict.integration_notes
    if notes is not None:
        lines += ["## Integration notes", ""]
        lines += [f"- {o.relation} `{o.item}`: {o.note}" for o in notes.overlaps]
        if notes.placement:
            lines.append(f"- Placement: {notes.placement}")
        if notes.remarks:
            lines.append(f"- Remarks: {notes.remarks}")
        lines.append("")
    return "\n".join(lines)


async def review(call: CallContext, args: ReviewArguments) -> dict[str, Any]:
    review_id = call.actor.review_id
    if review_id is None:
        raise Conflict("This turn has no open review.")
    verdict = ReviewVerdict.model_validate(args.model_dump())
    layout = call.ctx.layout

    def apply(txn: Txn) -> dict[str, Any]:
        row = candidates.review_row(txn, review_id)
        path = layout.review_report(call.actor.id, review_id)
        atomic_write_text(path, render_report(review_id, row["candidate_id"], call.actor.id,
                                              verdict))
        reply = candidates.record_verdict(call.ctx, txn, call.actor, review_id, verdict,
                                          layout.relative(path))
        reply["turn"] = "The verdict is recorded; end this turn."
        return reply

    return call.commit(apply)


OPERATIONS = [
    Operation("review", "review", "Record your verdict (accept or reject) on the candidate "
              "under review.", ReviewArguments, frozenset({Capability.REVIEWER}), review),
]
