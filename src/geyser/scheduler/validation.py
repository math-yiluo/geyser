"""Running machine validations (DESIGN.md section 8.3).

A validator that raises, crashes or hangs past its bound yields ``unavailable``: an
unresolved dependency, never a defect. Unavailable results are retried after 5
minutes, doubling to at most 3 hours. A validator whose bounded wait ends first
raises ``ValidationContinues``; the phase becomes ``queued`` and the job is attached
again later, whatever the author does.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from typing import TYPE_CHECKING

from geyser.content.staging import candidate_view, write_report
from geyser.core import candidates
from geyser.core.models import ValidationReport, Verdict
from geyser.domains.base import ValidationContext, ValidationContinues

if TYPE_CHECKING:
    from geyser.hub import Hub

log = logging.getLogger(__name__)

VALIDATION_HARD_LIMIT_SECONDS = 6 * 3600
"""A safety bound so one stuck validator cannot hold a slot forever."""


def due(txn):  # noqa: ANN001, ANN201 - re-export for the service's validation loop
    return candidates.due_validations(txn)


async def run_due(hub: Hub, *, limit: int = 4) -> int:
    """Start and finish up to ``limit`` due validations; returns how many ran."""
    with hub.read() as txn:
        due = candidates.due_validations(txn)[:limit]
    if not due:
        return 0
    await asyncio.gather(*(run_one(hub, row["id"]) for row in due))
    return len(due)


async def run_one(hub: Hub, validation_id: str) -> None:
    with hub.transaction() as txn:
        row = txn.one("SELECT * FROM validations WHERE id = ?", (validation_id,))
        if row is None or row["phase"] != "pending":
            return
        candidates.start_validation(txn, validation_id)
        candidate = candidates.candidate_row(txn, row["candidate_id"])
    scratch = hub.layout.scratch / f"validate-{validation_id}-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)
    wait = getattr(hub.pack, "validation_wait_seconds", None)
    context = ValidationContext(hub.layout, hub.config, scratch, attempt=row["attempt"],
                                wait_seconds=wait)
    try:
        report = await _validate(hub, context, candidate)
    except ValidationContinues as pending:
        with hub.transaction() as txn:
            candidates.queue_validation_wait(txn, validation_id, pending.job)
        return
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    document = {"validation": validation_id, "attempt": row["attempt"], "policy": row["policy"],
                **report.model_dump(mode="json")}
    try:
        path = write_report(hub, candidate["id"], validation_id, document)
    except OSError:
        log.exception("could not write the report of %s", validation_id)
        path = None
    with hub.transaction() as txn:
        candidates.record_validation(hub.ctx, txn, validation_id, report, path)


async def _validate(hub: Hub, context: ValidationContext, candidate) -> ValidationReport:
    try:
        with candidate_view(hub, candidate) as view:
            return await asyncio.wait_for(hub.pack.validate_candidate(context, view),
                                          timeout=VALIDATION_HARD_LIMIT_SECONDS)
    except ValidationContinues:
        raise
    except TimeoutError:
        return ValidationReport(verdict=Verdict.UNAVAILABLE,
                                summary="The validator did not finish in time.")
    except Exception as error:  # noqa: BLE001 - a validator bug is not a candidate defect
        log.exception("validator raised for %s", candidate["id"])
        return ValidationReport(verdict=Verdict.UNAVAILABLE,
                                summary=f"The validator raised {type(error).__name__}: {error}")


async def resume_queued(hub: Hub) -> int:
    """Attach again to queued validations of packs that support it."""
    resume = getattr(hub.pack, "resume_validation", None)
    if resume is None:
        return 0
    with hub.read() as txn:
        rows = txn.all("SELECT * FROM validations WHERE phase = 'queued'")
    count = 0
    for row in rows:
        job = (row["summary"] or "").removeprefix("queued:")
        try:
            report = await resume(job)
        except ValidationContinues:
            continue
        except Exception as error:  # noqa: BLE001
            log.exception("resuming validation %s raised", row["id"])
            report = ValidationReport(verdict=Verdict.UNAVAILABLE, summary=repr(error))
        if report is None:
            continue
        path = write_report(hub, row["candidate_id"], row["id"],
                            {"validation": row["id"], **report.model_dump(mode="json")})
        with hub.transaction() as txn:
            txn.execute("UPDATE validations SET phase = 'running' WHERE id = ?", (row["id"],))
            candidates.record_validation(hub.ctx, txn, row["id"], report, path)
        count += 1
    return count
