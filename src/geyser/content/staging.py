"""Staged candidates as files: materialized trees for checks, and housekeeping.

A candidate's directory moves from ``staging/`` to ``archive/`` once published and
to ``history/`` once rejected, withdrawn or invalidated. The move happens in an
idempotent housekeeping pass, so a crash between a ledger commit and a move only
delays the move. Readers look in all three places.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from sqlite3 import Row
from typing import TYPE_CHECKING

from geyser.content.snapshot import staged_changes
from geyser.core.models import Manifest
from geyser.domains.base import CandidateView
from geyser.jsonio import atomic_write_json, read_json

if TYPE_CHECKING:
    from geyser.hub import Hub

_HISTORY_STATES = ("rejected", "withdrawn", "invalidated")


def candidate_folder(hub: Hub, candidate_id: str) -> Path:
    for folder in (hub.layout.staging(candidate_id), hub.layout.archive(candidate_id),
                   hub.layout.history(candidate_id)):
        if folder.exists():
            return folder
    return hub.layout.staging(candidate_id)


def write_report(hub: Hub, candidate_id: str, name: str, document: dict) -> str:
    """Write a readable report copy beside the candidate (section 8.3)."""
    path = candidate_folder(hub, candidate_id) / "reports" / f"{name}.json"
    atomic_write_json(path, document)
    return hub.layout.relative(path)


@contextmanager
def candidate_view(hub: Hub, row: Row) -> Iterator[CandidateView]:
    """Materialize a candidate's tree (Library base plus its changes) for a check."""
    folder = candidate_folder(hub, row["id"])
    document = read_json(folder / "manifest.json")
    manifest = Manifest.model_validate(document["submission"])
    changed, deleted = staged_changes(folder)
    from geyser.core import tasks

    with hub.read() as txn:
        contract = tasks.load_contract(hub.ctx, txn, row["task_id"], row["task_revision"])
    tree = hub.layout.scratch / f"tree-{row['id']}-{uuid.uuid4().hex[:8]}"
    try:
        base = row["base_commit"] or None
        hub.library.repo.materialize(base, tree, keep=())
        for path, entry in changed.items():
            destination = tree / Path(*PurePosixPath(path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(hub.library.repo.read_blob(entry.oid))
        for path in deleted:
            (tree / Path(*PurePosixPath(path).parts)).unlink(missing_ok=True)
        files = {path: folder / "files" / Path(*PurePosixPath(path).parts) for path in changed}
        yield CandidateView(id=row["id"], kind=row["kind"], policy=row["policy"],
                            manifest=manifest, contract=contract,
                            base_commit=row["base_commit"], changed=files, deleted=deleted,
                            tree=tree)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def housekeep(hub: Hub) -> int:
    """Move finished candidates to ``archive/`` or ``history/``; returns how many moved."""
    moved = 0
    with hub.read() as txn:
        rows = txn.all("SELECT id, state FROM candidates WHERE state IN "
                       "('published', 'rejected', 'withdrawn', 'invalidated')")
    for row in rows:
        source = hub.layout.staging(row["id"])
        if not source.exists():
            continue
        target = (hub.layout.archive(row["id"]) if row["state"] == "published"
                  else hub.layout.history(row["id"]))
        if target.exists():
            shutil.rmtree(source, ignore_errors=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        moved += 1
    return moved
