"""The instance directory layout (DESIGN.md section 18) and safe relative paths."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from geyser.errors import InvalidArgument

PRIVATE_DIR = ".private"
DEFAULT_STATE_DIR = ".geyser"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def safe_relative_path(text: str, *, field: str = "path") -> PurePosixPath:
    """Parse a project-relative path written with forward slashes.

    Refuses absolute paths, ``..``, backslashes, drive letters, empty segments and
    control characters, so a path can never leave the directory it is joined to.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidArgument(f"`{field}` must be a nonempty relative path.")
    if "\\" in text or "\x00" in text or _WINDOWS_DRIVE.match(text):
        raise InvalidArgument(f"`{field}` must use forward slashes: {text!r}.")
    if any(ord(character) < 32 for character in text):
        raise InvalidArgument(f"`{field}` contains a control character.")
    if text.startswith("/"):
        raise InvalidArgument(f"`{field}` must be relative, not absolute: {text!r}.")
    parts = text.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidArgument(f"`{field}` must not contain `.` or `..` segments: {text!r}.")
    return PurePosixPath(*parts)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Layout:
    """Every path of one project instance."""

    root: Path
    state_dir_name: str = DEFAULT_STATE_DIR

    # Configuration and authoritative native files -------------------------------
    @property
    def config(self) -> Path:
        return self.root / "geyser.toml"

    @property
    def tasks(self) -> Path:
        return self.root / "tasks"

    def task_dir(self, task_id: str) -> Path:
        return self.tasks / task_id

    def revision_file(self, task_id: str, revision: int) -> Path:
        return self.task_dir(task_id) / "revisions" / f"{revision}.json"

    def task_markdown(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "task.md"

    @property
    def library(self) -> Path:
        return self.root / "library"

    def staging(self, candidate_id: str) -> Path:
        return self.root / "staging" / candidate_id

    def archive(self, candidate_id: str) -> Path:
        return self.root / "archive" / candidate_id

    def history(self, candidate_id: str) -> Path:
        return self.root / "history" / candidate_id

    def drafts(self, task_id: str) -> Path:
        return self.root / "drafts" / task_id

    def records(self, name: str) -> Path:
        return self.root / "records" / name

    @property
    def sources(self) -> Path:
        return self.root / "sources"

    @property
    def correspondence(self) -> Path:
        return self.root / "correspondence"

    # Agents --------------------------------------------------------------------
    @property
    def agents(self) -> Path:
        return self.root / "agents"

    def workspace(self, agent_id: str) -> Path:
        return self.agents / agent_id / "work"

    def private(self, agent_id: str) -> Path:
        return self.workspace(agent_id) / PRIVATE_DIR

    def handoff_file(self, agent_id: str, task_id: str) -> Path:
        return self.private(agent_id) / "tasks" / task_id / "handoff.md"

    def state_file(self, agent_id: str, task_id: str) -> Path:
        return self.private(agent_id) / "tasks" / task_id / "state.md"

    def journal(self, agent_id: str) -> Path:
        return self.private(agent_id) / "notes" / "journal.md"

    def review_report(self, agent_id: str, review_id: str) -> Path:
        return self.private(agent_id) / "reviews" / review_id / "report.md"

    def instructions_dir(self, agent_id: str) -> Path:
        return self.private(agent_id) / "instructions"

    def material_dir(self, agent_id: str) -> Path:
        return self.private(agent_id) / "material"

    def temporary_dir(self, agent_id: str) -> Path:
        return self.agents / agent_id / "tmp"

    # Hub internals (protected from every sandbox) -------------------------------
    @property
    def state(self) -> Path:
        return self.root / self.state_dir_name

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.sqlite3"

    @property
    def lock(self) -> Path:
        return self.state / "hub.lock"

    @property
    def blobs(self) -> Path:
        return self.state / "blobs"

    @property
    def reports(self) -> Path:
        return self.state / "reports"

    @property
    def talk(self) -> Path:
        return self.state / "talk"

    @property
    def transcripts(self) -> Path:
        return self.state / "transcripts"

    @property
    def holds(self) -> Path:
        return self.state / "holds"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def scratch(self) -> Path:
        """Hub-private working directories (integration merges, final checks)."""
        return self.state / "scratch"

    def ensure(self) -> None:
        """Create the directories every instance has."""
        for directory in (
            self.tasks,
            self.root / "staging",
            self.root / "archive",
            self.root / "history",
            self.root / "drafts",
            self.root / "records",
            self.sources,
            self.correspondence,
            self.agents,
            self.state,
            self.blobs,
            self.reports,
            self.talk,
            self.transcripts,
            self.holds,
            self.logs,
            self.scratch,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        """A path relative to the project root, with forward slashes."""
        return path.resolve().relative_to(self.root.resolve()).as_posix()
