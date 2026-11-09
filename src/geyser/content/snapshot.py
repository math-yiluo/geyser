"""Freezing a candidate from a workspace (DESIGN.md section 8.1).

The Hub freezes exactly the differences between the listed paths and the Library
base the workspace was last refreshed to. Capture reads every file twice and
rejects links and files beyond the kind's bounds; the kind's path and companion
rules then apply. The result is a self-contained staging directory::

    manifest.json   the submission, the changed files with hashes, the deletions
    base.json       the Library base commit
    files/<path>    the added and modified files
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from geyser.content.git import Entry, file_mode
from geyser.content.library import Library
from geyser.core.models import Manifest
from geyser.domains.base import ContributionKindSpec
from geyser.errors import InvalidArgument
from geyser.jsonio import atomic_write_json, sha256_bytes, sha256_json
from geyser.layout import PRIVATE_DIR, Layout, safe_relative_path

RESERVED_KNOWLEDGE = ("wiki/", "notes/")
NOTE_SPEC = ContributionKindSpec(
    name="note",
    description="Markdown under wiki/ or notes/: the project's lessons learned.",
    default_policy="core.note@1",
    allowed_prefixes=RESERVED_KNOWLEDGE,
    max_files=200,
    max_file_bytes=2 * 1024 * 1024,
    max_total_bytes=20 * 1024 * 1024,
)


@dataclass
class Capture:
    directory: Path
    base: str | None
    changed: dict[str, Entry] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    manifest_hash: str = ""
    total_bytes: int = 0


def _reject_reserved(path: str) -> None:
    first = path.split("/", 1)[0]
    if first in {PRIVATE_DIR, ".git"}:
        raise InvalidArgument(f"`{path}` is private or internal and is never contributed.")


def _read_stable(path: Path) -> bytes:
    """Read a file twice and insist both reads agree (the file is not being written)."""
    first = path.read_bytes()
    second = path.read_bytes()
    if first != second:
        raise InvalidArgument(f"`{path.name}` changed while it was being frozen; finish "
                              "writing it and submit again.")
    return first


def _workspace_files(workspace: Path, relative: PurePosixPath) -> list[PurePosixPath]:
    local = workspace / Path(*relative.parts)
    if local.is_symlink():
        raise InvalidArgument(f"`{relative}` is a link; links are never contributed.")
    if local.is_file():
        return [relative]
    if not local.is_dir():
        return []
    found = []
    for directory, subdirectories, files in os.walk(local):
        here = Path(directory)
        for name in list(subdirectories):
            if (here / name).is_symlink():
                raise InvalidArgument(f"`{(here / name).relative_to(workspace)}` is a link.")
            if name in {PRIVATE_DIR, ".git"}:
                subdirectories.remove(name)
        for name in files:
            path = here / name
            if path.is_symlink():
                raise InvalidArgument(f"`{path.relative_to(workspace).as_posix()}` is a link.")
            found.append(PurePosixPath(*path.relative_to(workspace).parts))
    return found


def check_kind_rules(spec: ContributionKindSpec, changed: list[str], deleted: list[str],
                     is_note: bool) -> None:
    """Apply a kind's path and companion rules to a candidate's changes."""
    for path in [*changed, *deleted]:
        if spec.allowed_prefixes and not path.startswith(spec.allowed_prefixes):
            raise InvalidArgument(
                f"`{path}` is outside the paths kind `{spec.name}` allows: "
                f"{', '.join(spec.allowed_prefixes)}."
            )
        if spec.excluded_prefixes and path.startswith(spec.excluded_prefixes):
            raise InvalidArgument(f"Kind `{spec.name}` may not touch `{path}`.")
        if not is_note and path.startswith(RESERVED_KNOWLEDGE):
            raise InvalidArgument(f"`{path}` is in the Wiki; submit Wiki changes with "
                                  "`wiki_submit`.")
        if is_note and not path.endswith(".md"):
            raise InvalidArgument(f"Wiki notes are Markdown; `{path}` is not.")
    if spec.companions is not None:
        problems = spec.companions(set(changed))
        if problems:
            raise InvalidArgument(f"Kind `{spec.name}` rules: " + "; ".join(problems))


def capture(layout: Layout, library: Library, workspace: Path, manifest: Manifest,
            base: str | None, spec: ContributionKindSpec) -> Capture:
    """Freeze the listed paths of ``workspace`` against ``base`` into a scratch directory."""
    is_note = manifest.kind == NOTE_SPEC.name
    listed = [safe_relative_path(text, field="paths") for text in manifest.paths]
    for path in listed:
        _reject_reserved(path.as_posix())
    base_entries = library.repo.ls_tree(base)
    directory = layout.scratch / f"capture-{uuid.uuid4().hex}"
    files_root = directory / "files"
    result = Capture(directory=directory, base=base)
    try:
        seen: set[str] = set()
        for relative in listed:
            prefix = relative.as_posix()
            for file_path in _workspace_files(workspace, relative):
                name = file_path.as_posix()
                if name in seen:
                    continue
                seen.add(name)
                _reject_reserved(name)
                local = workspace / Path(*file_path.parts)
                if local.stat().st_size > spec.max_file_bytes:
                    raise InvalidArgument(f"`{name}` exceeds {spec.max_file_bytes:,} bytes.")
                data = _read_stable(local)
                oid = library.repo.write_blob(data)
                mode = file_mode(local)
                previous = base_entries.get(name)
                if previous is not None and previous.oid == oid and previous.mode == mode:
                    continue
                result.changed[name] = Entry(mode, oid)
                result.total_bytes += len(data)
                destination = files_root / Path(*file_path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            for base_path in base_entries:
                inside = base_path == prefix or base_path.startswith(prefix + "/")
                if inside and base_path not in seen and not (
                    workspace / Path(*PurePosixPath(base_path).parts)
                ).exists():
                    result.deleted.append(base_path)
        result.deleted = sorted(set(result.deleted))
        if not result.changed and not result.deleted:
            raise InvalidArgument("The listed paths do not differ from the Library base; "
                                  "there is nothing to contribute.")
        if len(result.changed) > spec.max_files:
            raise InvalidArgument(f"A `{spec.name}` candidate has at most {spec.max_files} "
                                  "files.")
        if result.total_bytes > spec.max_total_bytes:
            raise InvalidArgument(f"A `{spec.name}` candidate is at most "
                                  f"{spec.max_total_bytes:,} bytes.")
        check_kind_rules(spec, sorted(result.changed), result.deleted, is_note)
        if is_note:
            _check_notes(directory, result, base_entries)
        _write_manifest(directory, manifest, result)
        return result
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _check_notes(directory: Path, result: Capture, base_entries: dict[str, Entry]) -> None:
    """Wiki pages are nonempty UTF-8, and ``wiki/README.md`` stays present (section 10.1)."""
    for path in result.changed:
        data = (directory / "files" / Path(*PurePosixPath(path).parts)).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidArgument(f"`{path}` is not valid UTF-8.") from error
        if not text.strip():
            raise InvalidArgument(f"`{path}` is empty.")
    readme = "wiki/README.md"
    if readme in result.deleted or (readme not in base_entries and readme not in result.changed
                                    and any(p.startswith("wiki/") for p in result.changed)):
        raise InvalidArgument("The Wiki needs `wiki/README.md`: add an entry for your page "
                              "there (Essential or Reference notes).")


def _write_manifest(directory: Path, manifest: Manifest, result: Capture) -> None:
    files = {}
    for path, entry in sorted(result.changed.items()):
        data = (directory / "files" / Path(*PurePosixPath(path).parts)).read_bytes()
        files[path] = {"sha256": sha256_bytes(data), "bytes": len(data), "mode": entry.mode,
                       "git": entry.oid}
    document: dict[str, Any] = {
        "submission": manifest.model_dump(mode="json"),
        "changed": files,
        "deleted": result.deleted,
    }
    atomic_write_json(directory / "manifest.json", document)
    atomic_write_json(directory / "base.json", {"commit": result.base})
    result.manifest_hash = sha256_json(document)


def install_staging(capture_dir: Path, staging: Path) -> None:
    """Move a capture into ``staging/<candidate>``, replacing any orphan of a rolled-back try."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    os.replace(capture_dir, staging)


def staged_changes(staging: Path) -> tuple[dict[str, Entry], list[str]]:
    """Read a staged candidate's changes back from its manifest."""
    from geyser.jsonio import read_json

    document = read_json(staging / "manifest.json")
    changed = {path: Entry(info["mode"], info["git"]) for path, info in document["changed"].items()}
    return changed, list(document["deleted"])
