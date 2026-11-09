"""Candidates, machine validation records and independent reviews (DESIGN.md section 8).

Lifecycle of a candidate::

    validating --accepted--> reviewing --accept--> accepted --integrated--> published
        |                        |                     |
        +--rejected              +--reject             +--conflict--> returned
        +--withdrawn             +--withdrawn
    any unpublished state --material revision--> invalidated

A rejected, withdrawn, returned or invalidated candidate stays a required
contribution of its task until a correction (``replaces``) is published or it is
withdrawn with a reason (section 5.7).
"""

from __future__ import annotations

from datetime import timedelta
from sqlite3 import Row
from typing import Any

from geyser.core import agents, inbox, tasks
from geyser.core.actors import Actor
from geyser.core.context import HubContext
from geyser.core.ledger import Txn, dumps, loads
from geyser.core.models import Manifest, ReviewVerdict, ValidationReport, Verdict
from geyser.errors import Conflict, Forbidden, InvalidArgument, NotFound
from geyser.ids import Kind, require_id

NOTE_KIND = "note"
NOTE_POLICY = "core.note@1"
UNPUBLISHED_LIVE = ("validating", "reviewing", "accepted", "returned")
"""States in which a candidate may still be published."""
CORRECTABLE = ("rejected", "withdrawn", "invalidated", "returned")

_UNAVAILABLE_FIRST_DELAY = timedelta(minutes=5)
_UNAVAILABLE_MAX_DELAY = timedelta(hours=3)


def candidate_row(txn: Txn, candidate_id: str) -> Row:
    require_id(candidate_id, Kind.CANDIDATE, field="candidate_id")
    row = txn.one("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    if row is None:
        raise NotFound(f"{candidate_id} does not exist.")
    return row


def authors_of(row: Row) -> list[str]:
    return list(loads(row["authors"], []))


def policy_for(ctx: HubContext, txn: Txn, manifest: Manifest) -> str:
    """The policy a submission is checked under (section 8.2)."""
    if manifest.kind == NOTE_KIND:
        return NOTE_POLICY
    contract_policy = txn.scalar("SELECT policy FROM tasks WHERE id = ?", (manifest.task_id,))
    if contract_policy:
        return contract_policy
    kinds = ctx.pack.contribution_kinds()
    if manifest.kind in kinds:
        return kinds[manifest.kind].default_policy
    return ctx.pack.default_policy()


def policy_publishes(ctx: HubContext, policy: str) -> bool:
    if policy == NOTE_POLICY:
        return True
    spec = ctx.pack.policies().get(policy)
    return spec is None or spec.publishes


def policy_validator(ctx: HubContext, policy: str) -> str | None:
    if policy == NOTE_POLICY:
        return None
    spec = ctx.pack.policies().get(policy)
    return None if spec is None else spec.validator


# ---------------------------------------------------------------------- submission
def check_submission(ctx: HubContext, txn: Txn, actor: Actor, manifest: Manifest) -> None:
    """Refuse a submission before any file is frozen."""
    task = tasks.task_row(txn, manifest.task_id)
    if task["state"] in tasks.TERMINAL_STATES:
        raise Conflict(f"{manifest.task_id} is {task['state']}; it takes no contributions.")
    if manifest.task_revision != task["revision"]:
        raise Conflict(
            f"{manifest.task_id} is at revision {task['revision']}; bind the candidate to it.",
            details={"current_revision": task["revision"]},
        )
    if actor.id not in tasks.member_ids(txn, manifest.task_id) and manifest.kind != NOTE_KIND:
        raise Forbidden(f"Only executors of {manifest.task_id} contribute to it.")
    kinds = ctx.pack.contribution_kinds()
    if manifest.kind != NOTE_KIND and manifest.kind not in kinds:
        raise InvalidArgument(f"Unknown contribution kind `{manifest.kind}`.",
                              details={"kinds": sorted(kinds) + [NOTE_KIND]})
    for coauthor in manifest.coauthors:
        if txn.one("SELECT 1 FROM agents WHERE id = ?", (coauthor,)) is None:
            raise InvalidArgument(f"Coauthor {coauthor} does not exist.")
    if manifest.replaces:
        replaced = candidate_row(txn, manifest.replaces)
        if replaced["task_id"] != manifest.task_id:
            raise InvalidArgument(f"{manifest.replaces} belongs to another task.")
        if replaced["state"] not in CORRECTABLE:
            raise Conflict(f"{manifest.replaces} is {replaced['state']}; only rejected, "
                           "withdrawn, invalidated or returned candidates are corrected.")
        if replaced["corrected_by"]:
            raise Conflict(f"{manifest.replaces} is already corrected by "
                           f"{replaced['corrected_by']}.")


def _implicit_correction(txn: Txn, actor: Actor, manifest: Manifest) -> str | None:
    """A submission without ``replaces`` corrects the author's single uncorrected failure."""
    rows = txn.all(
        "SELECT id, authors, paths FROM candidates WHERE task_id = ? AND kind = ? "
        "AND state IN ('rejected', 'withdrawn') AND corrected_by IS NULL",
        (manifest.task_id, manifest.kind),
    )
    mine = [row for row in rows if actor.id in loads(row["authors"], [])]
    if len(mine) != 1 or sorted(loads(mine[0]["paths"], [])) != sorted(manifest.paths):
        return None
    return mine[0]["id"]


def register(ctx: HubContext, txn: Txn, actor: Actor, candidate_id: str, manifest: Manifest,
             *, base_commit: str, manifest_hash: str) -> dict[str, Any]:
    """Record a frozen candidate and queue its validation (or its review)."""
    replaces = manifest.replaces or _implicit_correction(txn, actor, manifest)
    authors = [actor.id, *manifest.coauthors]
    if replaces:
        authors += authors_of(candidate_row(txn, replaces))
    authors = list(dict.fromkeys(authors))
    policy = policy_for(ctx, txn, manifest)
    txn.execute(
        "INSERT INTO candidates (id, task_id, task_revision, kind, policy, submitter, authors, "
        "paths, replaces, base_commit, manifest_hash, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'validating', ?)",
        (candidate_id, manifest.task_id, manifest.task_revision, manifest.kind, policy,
         actor.id, dumps(authors), dumps(sorted(manifest.paths)), replaces, base_commit,
         manifest_hash, txn.now),
    )
    if replaces:
        txn.execute("UPDATE candidates SET corrected_by = ? WHERE id = ?",
                    (candidate_id, replaces))
    txn.event("candidate.submitted", candidate_id, actor.id, task=manifest.task_id,
              kind=manifest.kind, policy=policy, paths=manifest.paths, replaces=replaces)
    _queue_validation(ctx, txn, candidate_id, policy, attempt=1)
    txn.after_commit(ctx.wake)
    return {"candidate": candidate_id, "policy": policy, "replaces": replaces,
            "state": candidate_row(txn, candidate_id)["state"]}


def _queue_validation(ctx: HubContext, txn: Txn, candidate_id: str, policy: str, *,
                      attempt: int, not_before: str | None = None) -> str:
    validation_id = txn.next_id(Kind.VALIDATION)
    if policy_validator(ctx, policy) is None:
        # Kind rules passed at capture; there is no further machine check.
        txn.execute(
            "INSERT INTO validations (id, candidate_id, policy, attempt, phase, verdict, "
            "summary, created_at, finished_at) VALUES (?, ?, ?, ?, 'done', 'accepted', "
            "'Kind rules passed; the policy has no further machine check.', ?, ?)",
            (validation_id, candidate_id, policy, attempt, txn.now, txn.now),
        )
        _open_review(ctx, txn, candidate_id)
    else:
        txn.execute(
            "INSERT INTO validations (id, candidate_id, policy, attempt, phase, "
            "next_attempt_at, created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (validation_id, candidate_id, policy, attempt, not_before, txn.now),
        )
    return validation_id


# ---------------------------------------------------------------------- validation
def due_validations(txn: Txn) -> list[Row]:
    """Pending validations whose candidate is still validating and whose time has come."""
    return txn.all(
        "SELECT v.* FROM validations v JOIN candidates c ON c.id = v.candidate_id "
        "WHERE v.phase = 'pending' AND c.state = 'validating' "
        "AND (v.next_attempt_at IS NULL OR v.next_attempt_at <= ?) ORDER BY v.created_at",
        (txn.now,),
    )


def start_validation(txn: Txn, validation_id: str) -> None:
    txn.execute("UPDATE validations SET phase = 'running' WHERE id = ? AND phase = 'pending'",
                (validation_id,))


def queue_validation_wait(txn: Txn, validation_id: str, job: str) -> None:
    """The validator's bounded wait ended first: the phase is ``queued`` (section 8.3)."""
    txn.execute("UPDATE validations SET phase = 'queued', summary = ? WHERE id = ?",
                (f"queued:{job}", validation_id))
    txn.event("validation.queued", validation_id, job=job)


def record_validation(ctx: HubContext, txn: Txn, validation_id: str, report: ValidationReport,
                      report_path: str | None) -> None:
    """Record a machine validation result and move the candidate along."""
    row = txn.one("SELECT * FROM validations WHERE id = ?", (validation_id,))
    if row is None or row["phase"] == "done":
        return
    txn.execute(
        "UPDATE validations SET phase = 'done', verdict = ?, summary = ?, report_path = ?, "
        "finished_at = ? WHERE id = ?",
        (report.verdict.value, report.summary[:2000], report_path, txn.now, validation_id),
    )
    candidate_id = row["candidate_id"]
    candidate = candidate_row(txn, candidate_id)
    txn.event("validation.done", candidate_id, verdict=report.verdict.value,
              validation=validation_id)
    if candidate["state"] != "validating":
        return  # Withdrawn or invalidated meanwhile; the report stays as evidence.
    authors = authors_of(candidate)
    where = f" Report: {report_path}." if report_path else ""
    if report.unqualified_acceptance:
        _open_review(ctx, txn, candidate_id)
        inbox.notify_many(txn, authors, f"{candidate_id} passed machine validation and awaits "
                          "independent review.", ref=candidate_id, task_id=candidate["task_id"])
    elif report.verdict is Verdict.REJECTED:
        _decide(txn, candidate_id, "rejected", f"machine validation: {report.summary[:500]}")
        inbox.notify_many(
            txn, authors,
            f"{candidate_id} was rejected by machine validation: {report.summary}.{where} "
            "Correct it and submit the correction with `replaces`.",
            ref=candidate_id, task_id=candidate["task_id"], wakes=True,
        )
    elif report.verdict is Verdict.UNAVAILABLE:
        delay = min(_UNAVAILABLE_FIRST_DELAY * (2 ** (row["attempt"] - 1)),
                    _UNAVAILABLE_MAX_DELAY)
        from geyser.clock import iso

        _queue_validation(ctx, txn, candidate_id, row["policy"], attempt=row["attempt"] + 1,
                          not_before=iso(txn.moment + delay))
    else:  # needs_operator
        txn.event("operator.attention", candidate_id,
                  message=f"Validation of {candidate_id} needs the operator: {report.summary}")
    txn.after_commit(ctx.wake)


def retry_validation(ctx: HubContext, txn: Txn, candidate_id: str, reason: str) -> dict[str, Any]:
    """``validation.retry``: the operator queues a new attempt now."""
    candidate = candidate_row(txn, candidate_id)
    if candidate["state"] != "validating":
        raise Conflict(f"{candidate_id} is {candidate['state']}, not awaiting validation.")
    txn.execute("UPDATE validations SET phase = 'done', verdict = COALESCE(verdict, "
                "'unavailable') WHERE candidate_id = ? AND phase IN ('pending', 'queued')",
                (candidate_id,))
    attempt = (txn.scalar("SELECT MAX(attempt) FROM validations WHERE candidate_id = ?",
                          (candidate_id,)) or 0) + 1
    validation_id = _queue_validation(ctx, txn, candidate_id, candidate["policy"],
                                      attempt=attempt)
    txn.event("validation.retry", candidate_id, "operator", reason=reason)
    txn.after_commit(ctx.wake)
    return {"candidate": candidate_id, "validation": validation_id}


def newest_validation(txn: Txn, candidate_id: str) -> Row | None:
    return txn.one(
        "SELECT * FROM validations WHERE candidate_id = ? ORDER BY attempt DESC LIMIT 1",
        (candidate_id,),
    )


# -------------------------------------------------------------------------- review
def _open_review(ctx: HubContext, txn: Txn, candidate_id: str) -> None:
    txn.execute("UPDATE candidates SET state = 'reviewing' WHERE id = ?", (candidate_id,))
    if txn.one("SELECT 1 FROM reviews WHERE candidate_id = ?", (candidate_id,)) is None:
        review_id = txn.next_id(Kind.REVIEW)
        txn.execute(
            "INSERT INTO reviews (id, candidate_id, state, created_at) "
            "VALUES (?, ?, 'waiting', ?)",
            (review_id, candidate_id, txn.now),
        )
        txn.event("review.opened", review_id, candidate=candidate_id)
    txn.after_commit(ctx.wake)


def waiting_reviews(txn: Txn) -> list[Row]:
    """Reviews whose candidate passed validation and that have no reviewer yet."""
    return txn.all(
        "SELECT r.* FROM reviews r JOIN candidates c ON c.id = r.candidate_id "
        "WHERE r.state = 'waiting' AND c.state = 'reviewing' ORDER BY r.created_at"
    )


def review_row(txn: Txn, review_id: str) -> Row:
    require_id(review_id, Kind.REVIEW, field="review_id")
    row = txn.one("SELECT * FROM reviews WHERE id = ?", (review_id,))
    if row is None:
        raise NotFound(f"{review_id} does not exist.")
    return row


def assign_reviewer(ctx: HubContext, txn: Txn, review_id: str, reviewer_id: str) -> None:
    """Give a waiting review its new reviewer (section 8.4). No decision selects it."""
    review = review_row(txn, review_id)
    if review["state"] != "waiting":
        raise Conflict(f"{review_id} is {review['state']}.")
    candidate = candidate_row(txn, review["candidate_id"])
    if reviewer_id in authors_of(candidate):
        raise Conflict(f"{reviewer_id} authored {candidate['id']}.")
    txn.execute("UPDATE reviews SET reviewer = ?, state = 'open' WHERE id = ?",
                (reviewer_id, review_id))
    txn.execute("UPDATE agents SET current_review = ?, intent = 'start' WHERE id = ?",
                (review_id, reviewer_id))
    txn.event("review.assigned", review_id, reviewer_id, candidate=candidate["id"])


def record_verdict(ctx: HubContext, txn: Txn, actor: Actor, review_id: str,
                   verdict: ReviewVerdict, report_path: str) -> dict[str, Any]:
    """Record the reviewer's verdict on the exact candidate it reviewed."""
    review = review_row(txn, review_id)
    if review["reviewer"] != actor.id:
        raise Forbidden(f"{review_id} belongs to {review['reviewer']}.")
    if review["state"] != "open":
        raise Conflict(f"{review_id} is {review['state']}; its verdict cannot be recorded.")
    candidate = candidate_row(txn, review["candidate_id"])
    if candidate["state"] != "reviewing":
        raise Conflict(f"{candidate['id']} is {candidate['state']}; the review is moot.")
    if actor.id in authors_of(candidate):
        raise Forbidden("Nobody accepts their own work.")
    accepted = verdict.decision == "accept"
    txn.execute(
        "UPDATE reviews SET state = ?, verdict = ?, report_path = ?, integration_notes = ?, "
        "decided_at = ? WHERE id = ?",
        ("accepted" if accepted else "rejected", dumps(verdict.model_dump(mode="json")),
         report_path,
         dumps(verdict.integration_notes.model_dump()) if verdict.integration_notes else None,
         txn.now, review_id),
    )
    _decide(txn, candidate["id"], "accepted" if accepted else "rejected",
            None if accepted else verdict.summary[:500])
    authors = authors_of(candidate)
    if accepted:
        txn.execute("UPDATE agents SET state = 'parked', current_review = NULL, "
                    "intent = 'none' WHERE id = ?", (actor.id,))
        summary = (f"{candidate['id']} was accepted by {actor.id}. Report: {report_path}. "
                   "It is published once integrated.")
    else:
        agents.retire(txn, actor.id, "review finished with a rejection")
        summary = (f"{candidate['id']} was rejected by {actor.id}: {verdict.summary} "
                   f"Report: {report_path}. Correct it and submit with `replaces`.")
    # A rejection wakes its authors; an acceptance waits for their next turn (or for the
    # publication their predicate names).
    inbox.notify_many(txn, authors, summary, ref=candidate["id"], task_id=candidate["task_id"],
                      wakes=not accepted)
    txn.event("review.decided", review_id, actor.id, decision=verdict.decision,
              candidate=candidate["id"])
    txn.after_commit(ctx.wake)
    return {"review": review_id, "candidate": candidate["id"], "decision": verdict.decision,
            "report": report_path}


def _decide(txn: Txn, candidate_id: str, state: str, reason: str | None) -> None:
    txn.execute("UPDATE candidates SET state = ?, reason = ?, decided_at = ? WHERE id = ?",
                (state, reason, txn.now, candidate_id))


def cancel_review(txn: Txn, candidate_id: str, reason: str) -> None:
    """Cancel an undecided review; its reviewer's execution becomes stale."""
    review = txn.one("SELECT * FROM reviews WHERE candidate_id = ?", (candidate_id,))
    if review is None or review["state"] not in ("waiting", "open"):
        return
    txn.execute("UPDATE reviews SET state = 'cancelled', decided_at = ? WHERE id = ?",
                (txn.now, review["id"]))
    if review["reviewer"]:
        txn.execute("UPDATE agents SET fence = fence + 1 WHERE id = ?", (review["reviewer"],))
        agents.retire(txn, review["reviewer"], f"review cancelled: {reason}")
    txn.event("review.cancelled", review["id"], reason=reason)


# --------------------------------------------------------- withdrawal, invalidation
def withdraw(ctx: HubContext, txn: Txn, actor: Actor, candidate_id: str, reason: str
             ) -> dict[str, Any]:
    """Withdraw a candidate with a reason (sections 8.1 and 5.7)."""
    candidate = candidate_row(txn, candidate_id)
    if actor.id not in authors_of(candidate) and not actor.is_operator:
        raise Forbidden(f"Only the authors of {candidate_id} may withdraw it.")
    if not reason.strip():
        raise InvalidArgument("A withdrawal needs a reason.")
    if candidate["state"] in ("published", "withdrawn"):
        raise Conflict(f"{candidate_id} is already {candidate['state']}.")
    if candidate["state"] == "accepted":
        raise Conflict(f"{candidate_id} is accepted and awaiting integration; it is published "
                       "unless a revision invalidates it.")
    cancel_review(txn, candidate_id, "candidate withdrawn")
    _decide(txn, candidate_id, "withdrawn", reason)
    txn.event("candidate.withdrawn", candidate_id, actor.id, reason=reason)
    txn.after_commit(ctx.wake)
    return {"candidate": candidate_id, "state": "withdrawn"}


def invalidate_for_material_revision(ctx: HubContext, txn: Txn, task_id: str, revision: int
                                     ) -> list[str]:
    """A material revision invalidates every unpublished candidate of older revisions."""
    rows = txn.all(
        "SELECT * FROM candidates WHERE task_id = ? AND task_revision < ? AND state IN "
        "('validating', 'reviewing', 'accepted', 'returned')",
        (task_id, revision),
    )
    for row in rows:
        cancel_review(txn, row["id"], "material revision")
        _decide(txn, row["id"], "invalidated", f"material revision {revision} of {task_id}")
        if row["batch_id"]:
            _leave_batch(txn, row["id"], row["batch_id"],
                         f"invalidated by revision {revision} of {task_id}")
        inbox.notify_many(
            txn, authors_of(row),
            f"{row['id']} was invalidated by revision {revision} of {task_id}. Resubmit it "
            "against the current revision with `replaces`.",
            ref=row["id"], task_id=task_id, wakes=True,
        )
        txn.event("candidate.invalidated", row["id"], revision=revision)
    return [row["id"] for row in rows]


def _leave_batch(txn: Txn, candidate_id: str, batch_id: str, reason: str) -> None:
    """Remove an invalidated member from its open batch; the batch must validate again."""
    batch = txn.one("SELECT members, deferred, state FROM batches WHERE id = ?", (batch_id,))
    if batch is None or batch["state"] != "open":
        return
    members = [m for m in loads(batch["members"], []) if m != candidate_id]
    deferred = loads(batch["deferred"], [])
    deferred.append({"candidate": candidate_id, "reason": reason, "at": txn.now})
    txn.execute("UPDATE batches SET members = ?, deferred = ?, final_tree = NULL WHERE id = ?",
                (dumps(members), dumps(deferred), batch_id))
    txn.execute("UPDATE candidates SET batch_id = NULL WHERE id = ?", (candidate_id,))
    txn.event("batch.member_removed", batch_id, candidate=candidate_id, reason=reason)


# ---------------------------------------------------------------- integration hooks
def accepted_for_integration(ctx: HubContext, txn: Txn) -> list[Row]:
    """Accepted candidates waiting to be published, in acceptance order."""
    rows = txn.all("SELECT * FROM candidates WHERE state = 'accepted' ORDER BY decided_at, id")
    return [row for row in rows if policy_publishes(ctx, row["policy"])]


def mark_returned(ctx: HubContext, txn: Txn, candidate_id: str, reason: str,
                  paths: list[str]) -> None:
    """Direct integration returns a conflicting candidate to its task (section 9.2)."""
    candidate = candidate_row(txn, candidate_id)
    _decide(txn, candidate_id, "returned", reason)
    txn.event("candidate.returned", candidate_id, reason=reason, paths=paths)
    targets = set(authors_of(candidate)) | set(tasks.member_ids(txn, candidate["task_id"]))
    listed = ", ".join(paths[:20]) if paths else "see the report"
    inbox.notify_many(
        txn, targets,
        f"{candidate_id} was returned by integration ({reason}); conflicting paths: {listed}. "
        "Its review stays recorded. Submit a correction against the current Library with "
        "`replaces`, or withdraw it with a reason.",
        ref=candidate_id, task_id=candidate["task_id"], wakes=True,
    )
    txn.after_commit(ctx.wake)


def mark_published(ctx: HubContext, txn: Txn, candidate_ids: list[str], commit: str,
                   publication_id: str) -> None:
    """Record publication; parked reviewers of these candidates retire (section 8.4)."""
    for candidate_id in candidate_ids:
        candidate = candidate_row(txn, candidate_id)
        if candidate["state"] != "accepted":
            # Invalidated while its publication was in flight: the commit exists, but the
            # candidate keeps its state; its correction supersedes the content.
            txn.event("candidate.published_after_change", candidate_id, commit=commit,
                      state=candidate["state"])
            continue
        txn.execute(
            "UPDATE candidates SET state = 'published', published_commit = ?, "
            "publication_id = ? WHERE id = ?",
            (commit, publication_id, candidate_id),
        )
        reviewer = txn.scalar("SELECT reviewer FROM reviews WHERE candidate_id = ?",
                              (candidate_id,))
        if reviewer and txn.scalar("SELECT state FROM agents WHERE id = ?",
                                   (reviewer,)) == "parked":
            agents.retire(txn, reviewer, "reviewed candidate published")
        inbox.notify_many(txn, authors_of(candidate),
                          f"{candidate_id} is published in commit {commit[:12]}.",
                          ref=candidate_id, task_id=candidate["task_id"])
        txn.event("candidate.published", candidate_id, commit=commit,
                  publication=publication_id)
    tasks.refresh_set_aside(ctx, txn)
    txn.after_commit(ctx.wake)


def blocking_contributions(ctx: HubContext, txn: Txn, task_id: str) -> list[dict[str, str]]:
    """Contributions that keep a task from closing (section 5.7, contribution gate)."""
    blocking = []
    for row in txn.all(
        "SELECT id, state, policy FROM candidates WHERE task_id = ? AND corrected_by IS NULL "
        "AND state NOT IN ('published', 'withdrawn') AND required = 1",
        (task_id,),
    ):
        if row["state"] == "accepted" and not policy_publishes(ctx, row["policy"]):
            continue  # Evidence-only acceptances never publish.
        blocking.append({"candidate": row["id"], "state": row["state"]})
    return blocking


def summary(row: Row) -> dict[str, Any]:
    return {
        "candidate": row["id"],
        "task": row["task_id"],
        "kind": row["kind"],
        "policy": row["policy"],
        "state": row["state"],
        "authors": authors_of(row),
        "replaces": row["replaces"],
        "corrected_by": row["corrected_by"],
        "reason": row["reason"],
        "published_commit": row["published_commit"],
    }
