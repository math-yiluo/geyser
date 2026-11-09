"""Shared knowledge outside the Library: drafts, shared records, sources (section 10).

None of these is acceptance evidence. Drafts and records are revisioned with an
expected-revision check; sources are registered by fingerprint before they are cited.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from geyser.core import tasks
from geyser.core.actors import Actor
from geyser.core.context import HubContext
from geyser.core.ledger import Txn
from geyser.errors import Conflict, Forbidden, InvalidArgument, NotFound
from geyser.ids import Kind
from geyser.jsonio import atomic_write_text, sha256_bytes, sha256_json, sha256_text
from geyser.layout import safe_relative_path

_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_DRAFT_BYTES = 50 * 1024 * 1024


def _check_name(name: str, what: str) -> None:
    if not _NAME.match(name):
        raise InvalidArgument(f"A {what} name is lowercase letters, digits, `_`, `.`, `-`.")


def _tree_files(root: Path) -> list[tuple[str, Path]]:
    found = []
    for directory, subdirectories, files in os.walk(root):
        here = Path(directory)
        for name in subdirectories:
            if (here / name).is_symlink():
                raise InvalidArgument(f"`{name}` is a link; links are not shared.")
        for name in sorted(files):
            path = here / name
            if path.is_symlink():
                raise InvalidArgument(f"`{name}` is a link; links are not shared.")
            found.append((PurePosixPath(*path.relative_to(root).parts).as_posix(), path))
    return sorted(found)


def folder_fingerprint(root: Path) -> str:
    """A hash binding a folder's relative file list and contents."""
    return sha256_json([(name, sha256_bytes(path.read_bytes())) for name, path in
                        _tree_files(root)])


# -------------------------------------------------------------------------- drafts
def update_draft(ctx: HubContext, txn: Txn, actor: Actor, name: str,
                 expected_revision: int | None) -> dict[str, Any]:
    """Share ``.private/drafts/<name>/`` as an immutable draft revision of one's task."""
    _check_name(name, "draft")
    task_id = actor.task_id
    if task_id is None or not tasks.is_maintainer(txn, task_id, actor.id):
        raise Forbidden("Only a maintainer of the current task shares its drafts.")
    source = ctx.layout.private(actor.id) / "drafts" / name
    if not source.is_dir():
        raise NotFound(f"`.private/drafts/{name}/` does not exist in your workspace.")
    current = txn.scalar("SELECT MAX(revision) FROM drafts WHERE task_id = ?", (task_id,))
    if current != expected_revision:
        raise Conflict(f"The drafts of {task_id} are at revision {current}, not "
                       f"{expected_revision}.", details={"current_revision": current})
    files = _tree_files(source)
    if not files:
        raise InvalidArgument("The draft directory is empty.")
    if sum(path.stat().st_size for _, path in files) > MAX_DRAFT_BYTES:
        raise InvalidArgument(f"A draft is at most {MAX_DRAFT_BYTES:,} bytes.")
    revision = 0 if current is None else current + 1
    target = ctx.layout.drafts(task_id) / str(revision)
    if target.exists():
        shutil.rmtree(target)  # An orphan of a rolled-back transaction.
    for relative, path in files:
        destination = target / Path(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    digest = folder_fingerprint(target)
    txn.execute(
        "INSERT INTO drafts (task_id, revision, name, author, hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, revision, name, actor.id, digest, txn.now),
    )
    txn.event("draft.shared", task_id, actor.id, revision=revision, name=name)
    return {"task": task_id, "draft_revision": revision,
            "path": ctx.layout.relative(target), "files": len(files)}


# ------------------------------------------------------------------ shared records
def update_record(ctx: HubContext, txn: Txn, actor: Actor, name: str,
                  expected_revision: int | None, content: str, fmt: str) -> dict[str, Any]:
    """Write a new revision of a project-wide shared record."""
    _check_name(name, "record")
    if fmt not in {"json", "markdown"}:
        raise InvalidArgument("`format` is `json` or `markdown`.")
    if fmt == "json":
        import json

        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            raise InvalidArgument(f"The record is not valid JSON: {error}") from error
    if len(content) > 2_000_000:
        raise InvalidArgument("A shared record is at most 2,000,000 characters.")
    current = txn.scalar("SELECT MAX(revision) FROM records WHERE name = ?", (name,))
    if current != expected_revision:
        raise Conflict(f"Record `{name}` is at revision {current}, not {expected_revision}. "
                       "Read it again and apply your edit to the current revision.",
                       details={"current_revision": current})
    revision = 0 if current is None else current + 1
    suffix = "json" if fmt == "json" else "md"
    path = ctx.layout.records(name) / str(revision) / f"record.{suffix}"
    atomic_write_text(path, content)
    txn.execute(
        "INSERT INTO records (name, revision, author, hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, revision, actor.id, sha256_text(content), txn.now),
    )
    txn.event("record.updated", name, actor.id, revision=revision)
    return {"record": name, "revision": revision, "path": ctx.layout.relative(path)}


def record_status(ctx: HubContext, txn: Txn, name: str | None) -> dict[str, Any]:
    if name is None:
        rows = txn.all("SELECT name, MAX(revision) AS revision FROM records GROUP BY name "
                       "ORDER BY name")
        return {"records": [dict(row) for row in rows]}
    row = txn.one("SELECT * FROM records WHERE name = ? ORDER BY revision DESC LIMIT 1",
                  (name,))
    if row is None:
        raise NotFound(f"No shared record named `{name}`.")
    folder = ctx.layout.records(name) / str(row["revision"])
    files = sorted(folder.glob("record.*"))
    return {"record": name, "revision": row["revision"], "author": row["author"],
            "path": ctx.layout.relative(files[0]) if files else None}


# ------------------------------------------------------------------------- sources
def register_source(ctx: HubContext, txn: Txn, actor: Actor, path: str, title: str
                    ) -> dict[str, Any]:
    """Record a source's fingerprint and return its ID; a changed file is a new edition."""
    relative = safe_relative_path(path, field="path")
    location = ctx.layout.sources / Path(*relative.parts)
    if location.is_symlink() or not location.exists():
        raise NotFound(f"`sources/{relative}` does not exist.")
    if not title.strip() or len(title) > 300:
        raise InvalidArgument("A source title is 1 to 300 characters.")
    fingerprint = (folder_fingerprint(location) if location.is_dir()
                   else sha256_bytes(location.read_bytes()))
    existing = txn.one("SELECT id FROM sources WHERE path = ? AND fingerprint = ?",
                       (relative.as_posix(), fingerprint))
    if existing:
        return {"source_id": existing["id"], "fingerprint": fingerprint, "new": False}
    source_id = txn.next_id(Kind.SOURCE)
    txn.execute(
        "INSERT INTO sources (id, path, fingerprint, title, registered_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, relative.as_posix(), fingerprint, title, actor.id, txn.now),
    )
    txn.event("source.registered", source_id, actor.id, path=relative.as_posix())
    return {"source_id": source_id, "fingerprint": fingerprint, "new": True,
            "citation": {"source_id": source_id, "sha256": fingerprint, "locator": "…"}}
