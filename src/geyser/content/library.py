"""The published Library and agents' workspaces (DESIGN.md section 9.1).

The Library is a Git repository only the Hub moves. ``library/`` is its work tree,
kept equal to the published head. Each worker's workspace holds a checkout of the
Library plus its ``.private/`` directory. At the start of each task activation the
Hub refreshes the workspace with a three-way comparison of the saved base, the
current head and the worker's files; the worker's edits are kept.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from geyser.content.git import Entry, GitRepo
from geyser.layout import PRIVATE_DIR, Layout

KEPT_IN_WORKSPACE = (".git", PRIVATE_DIR)

STARTER_README = """# Wiki

The project's memory of lessons learned. Read Essential before exploring; read
reference notes when their subject comes up. Add an entry for every page you submit.

## Essential

## Reference notes
"""


@dataclass
class RefreshReport:
    base: str | None
    head: str | None
    updated: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    conflict_copies: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"from": _short(self.base), "to": _short(self.head)}
        if self.updated:
            result["updated"] = self.updated[:50]
            if len(self.updated) > 50:
                result["updated_more"] = len(self.updated) - 50
        if self.conflicts:
            result["conflicts"] = self.conflicts
            result["published_versions_in"] = self.conflict_copies
        return result


def _short(commit: str | None) -> str | None:
    return commit[:12] if commit else None


class Library:
    """Read and write access to the Library repository."""

    def __init__(self, layout: Layout):
        self.layout = layout
        self.repo = GitRepo(layout.library)
        self._head_cache: str | None = None
        self._lock = threading.RLock()

    def initialize(self, import_from: Path | None = None) -> str:
        """Create the repository; optionally import an existing tree as the baseline."""
        with self._lock:
            GitRepo.init(self.layout.library)
            head = self.repo.head()
            if head is not None:
                return head
            changes: dict[str, Entry] = {}
            if import_from is not None:
                for path in sorted(import_from.rglob("*")):
                    relative = path.relative_to(import_from)
                    if relative.parts[0] in KEPT_IN_WORKSPACE or not path.is_file():
                        continue
                    if path.is_symlink():
                        continue
                    from geyser.content.git import file_mode

                    changes[PurePosixPath(*relative.parts).as_posix()] = Entry(
                        file_mode(path), self.repo.write_file(path))
            if "wiki/README.md" not in changes:
                changes["wiki/README.md"] = Entry("100644", self.repo.write_blob(
                    STARTER_README.encode("utf-8")))
            tree = self.repo.build_tree(None, changes)
            message = "Initialize the Library" + (" (imported baseline)" if changes else "")
            commit = self.repo.commit_tree(tree, [], message + "\n")
            self.repo.update_branch(commit, None)
            self.repo.materialize(commit, self.layout.library)
            self._head_cache = commit
            return commit

    # ------------------------------------------------------------- LibraryView
    def head(self) -> str | None:
        with self._lock:
            if self._head_cache is None:
                self._head_cache = self.repo.head()
            return self._head_cache

    def invalidate_cache(self) -> None:
        with self._lock:
            self._head_cache = None

    def exists(self, path: str) -> bool:
        head = self.head()
        if head is None:
            return False
        return self.repo.exists(head, path.rstrip("/"))

    # --------------------------------------------------------------- workspaces
    def checkout_workspace(self, workspace: Path) -> str | None:
        """Fill a new workspace with the current head; returns its base commit."""
        head = self.head()
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / PRIVATE_DIR).mkdir(exist_ok=True)
        if head is not None:
            for path, entry in self.repo.ls_tree(head).items():
                destination = workspace / Path(*PurePosixPath(path).parts)
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(self.repo.read_blob(entry.oid))
        return head

    def refresh_workspace(self, workspace: Path, base: str | None) -> RefreshReport:
        """Bring published changes into a workspace without losing the worker's edits.

        For each path the head changed since ``base``: a file the worker left equal to
        the base version takes the head version; a file the worker changed stays, and
        the published version is written to ``.private/refresh/<commit>/<path>``.
        """
        head = self.head()
        report = RefreshReport(base=base, head=head)
        if head is None or head == base:
            return report
        old = self.repo.ls_tree(base)
        new = self.repo.ls_tree(head)
        copies = workspace / PRIVATE_DIR / "refresh" / head[:12]
        for _status, path in self.repo.diff(base, head):
            local = workspace / Path(*PurePosixPath(path).parts)
            local_bytes = local.read_bytes() if local.is_file() else None
            base_bytes = self.repo.read_blob(old[path].oid) if path in old else None
            head_bytes = self.repo.read_blob(new[path].oid) if path in new else None
            if local_bytes == head_bytes:
                continue
            if local_bytes == base_bytes:
                if head_bytes is None:
                    local.unlink(missing_ok=True)
                else:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(head_bytes)
                report.updated.append(path)
                continue
            report.conflicts.append(path)
            if head_bytes is not None:
                copy = copies / Path(*PurePosixPath(path).parts)
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(head_bytes)
        if report.conflicts:
            report.conflict_copies = (workspace / PRIVATE_DIR / "refresh" / head[:12]
                                      ).relative_to(workspace).as_posix()
        return report

    def sync_work_tree(self) -> None:
        """Make ``library/`` equal to the published head again."""
        head = self.head()
        if head is not None:
            self.repo.materialize(head, self.layout.library)
