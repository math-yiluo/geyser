"""A small, explicit wrapper over Git plumbing for the Library.

Only plumbing commands are used, with filters disabled, so line endings and
attributes never change bytes. Every call names the repository explicitly and
runs with a fixed identity and no user or system configuration.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

BRANCH = "refs/heads/main"
_ZERO = "0" * 40
FILE_MODE = "100644"
EXECUTABLE_MODE = "100755"


class GitError(RuntimeError):
    def __init__(self, command: list[str], returncode: int, stderr: str):
        super().__init__(f"git {' '.join(command)} failed ({returncode}): {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class Entry:
    mode: str
    oid: str


@dataclass(frozen=True)
class MergeResult:
    clean: bool
    tree: str | None
    conflicts: list[str]


def _environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"HOME", "XDG_CONFIG_HOME"}
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Geyser Hub",
        GIT_AUTHOR_EMAIL="hub@geyser.invalid",
        GIT_COMMITTER_NAME="Geyser Hub",
        GIT_COMMITTER_EMAIL="hub@geyser.invalid",
        HOME=tempfile.gettempdir(),
    )
    if extra:
        environment.update(extra)
    return environment


class GitRepo:
    def __init__(self, path: Path):
        self.path = path

    # ------------------------------------------------------------------ running
    def _run(self, *arguments: str, input: bytes | None = None,
             extra_env: Mapping[str, str] | None = None, ok: Iterable[int] = (0,)
             ) -> subprocess.CompletedProcess[bytes]:
        command = ["git", "-C", str(self.path), *arguments]
        result = subprocess.run(command, input=input, capture_output=True,
                                env=_environment(extra_env), check=False)
        if result.returncode not in set(ok):
            raise GitError(list(arguments), result.returncode,
                           result.stderr.decode("utf-8", "replace"))
        return result

    def _text(self, *arguments: str, **options: object) -> str:
        return self._run(*arguments, **options).stdout.decode("utf-8").strip()  # type: ignore[arg-type]

    # ------------------------------------------------------------------- setup
    @classmethod
    def init(cls, path: Path) -> GitRepo:
        path.mkdir(parents=True, exist_ok=True)
        repo = cls(path)
        if not (path / ".git").exists():
            repo._run("init", "--quiet", "--initial-branch=main")
        for key, value in (("core.autocrlf", "false"), ("core.safecrlf", "false"),
                           ("core.filemode", "true" if os.name != "nt" else "false"),
                           ("gc.auto", "0")):
            repo._run("config", key, value)
        return repo

    def head(self) -> str | None:
        result = self._run("rev-parse", "--verify", "--quiet", BRANCH, ok=(0, 1))
        text = result.stdout.decode().strip()
        return text or None

    # ----------------------------------------------------------------- objects
    def write_blob(self, data: bytes) -> str:
        return self._text("hash-object", "-w", "--no-filters", "--stdin", input=data)

    def write_file(self, path: Path) -> str:
        return self._text("hash-object", "-w", "--no-filters", str(path))

    def read_blob(self, oid: str) -> bytes:
        return self._run("cat-file", "blob", oid).stdout

    def ls_tree(self, treeish: str | None) -> dict[str, Entry]:
        """Every file of a commit or tree, keyed by POSIX path."""
        if treeish is None:
            return {}
        output = self._run("ls-tree", "-r", "-z", "--full-tree", treeish).stdout
        entries: dict[str, Entry] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, kind, oid = meta.decode().split(" ")
            if kind == "blob":
                entries[name.decode("utf-8")] = Entry(mode, oid)
        return entries

    def exists(self, treeish: str | None, path: str) -> bool:
        if treeish is None:
            return False
        result = self._run("cat-file", "-e", f"{treeish}:{path}", ok=(0, 1, 128))
        return result.returncode == 0

    def tree_of(self, commit: str) -> str:
        return self._text("rev-parse", f"{commit}^{{tree}}")

    # ------------------------------------------------------------------- trees
    def build_tree(self, base: str | None, changes: Mapping[str, Entry],
                   deletions: Iterable[str] = ()) -> str:
        """Write a tree: ``base`` with ``changes`` applied and ``deletions`` removed."""
        with tempfile.TemporaryDirectory(prefix="geyser-index-") as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            if base is not None:
                self._run("read-tree", base, extra_env=env)
            else:
                self._run("read-tree", "--empty", extra_env=env)
            lines = [f"0 {_ZERO}\t{path}" for path in deletions]
            lines += [f"{entry.mode} {entry.oid}\t{path}" for path, entry in changes.items()]
            if lines:
                payload = ("\n".join(lines) + "\n").encode("utf-8")
                self._run("update-index", "--index-info", input=payload, extra_env=env)
            return self._text("write-tree", extra_env=env)

    def commit_tree(self, tree: str, parents: Iterable[str], message: str) -> str:
        arguments = ["commit-tree", tree]
        for parent in parents:
            arguments += ["-p", parent]
        return self._text(*arguments, input=message.encode("utf-8"))

    def merge(self, base: str, ours: str, theirs: str) -> MergeResult:
        """A three-way merge without a work tree (``git merge-tree --write-tree``)."""
        result = self._run("merge-tree", "--write-tree", "--name-only", "--no-messages",
                           f"--merge-base={base}", ours, theirs, ok=(0, 1))
        lines = [line for line in result.stdout.decode("utf-8").splitlines() if line]
        tree = lines[0] if lines else None
        if result.returncode == 0:
            return MergeResult(True, tree, [])
        # On conflict the tree holds the conflicted files with conflict markers.
        return MergeResult(False, tree, sorted(set(lines[1:])))

    def diff(self, old: str | None, new: str) -> list[tuple[str, str]]:
        """``(status, path)`` pairs between two commits (A, M, D; renames as D + A)."""
        if old is None:
            return [("A", path) for path in self.ls_tree(new)]
        output = self._run("diff-tree", "-r", "-z", "--no-renames", "--name-status", old,
                           new).stdout
        fields = [field.decode("utf-8") for field in output.split(b"\0") if field]
        return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]

    # --------------------------------------------------------------------- refs
    def update_branch(self, new: str, expected_old: str | None) -> None:
        """Move the branch only if it still points at ``expected_old``."""
        self._run("update-ref", BRANCH, new, expected_old or _ZERO)

    # -------------------------------------------------------------- work trees
    def materialize(self, commit: str | None, target: Path,
                    keep: Iterable[str] = (".git",)) -> None:
        """Make ``target`` hold exactly the files of ``commit`` (except kept top names)."""
        target.mkdir(parents=True, exist_ok=True)
        entries = self.ls_tree(commit)
        kept = set(keep)
        wanted = {PurePosixPath(path) for path in entries}
        for existing in sorted(_files_under(target, kept), reverse=True):
            if existing not in wanted:
                (target / Path(*existing.parts)).unlink()
        for name, entry in entries.items():
            destination = target / Path(*PurePosixPath(name).parts)
            data = self.read_blob(entry.oid)
            if destination.is_file() and destination.read_bytes() == data:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_dir():
                import shutil

                shutil.rmtree(destination)
            destination.write_bytes(data)
            if entry.mode == EXECUTABLE_MODE and os.name != "nt":
                destination.chmod(0o755)
        _remove_empty_dirs(target, kept)


def _files_under(root: Path, kept: set[str]) -> list[PurePosixPath]:
    result = []
    for directory, subdirectories, files in os.walk(root):
        relative = Path(directory).relative_to(root)
        if relative == Path("."):
            subdirectories[:] = [name for name in subdirectories if name not in kept]
            files = [name for name in files if name not in kept]
        for name in files:
            result.append(PurePosixPath(*(relative / name).parts))
    return result


def _remove_empty_dirs(root: Path, kept: set[str]) -> None:
    for directory, _subdirectories, _files in os.walk(root, topdown=False):
        path = Path(directory)
        if path == root or path.relative_to(root).parts[0] in kept:
            continue
        if not any(path.iterdir()):
            try:
                path.rmdir()
            except OSError:
                pass


def file_mode(path: Path) -> str:
    if os.name != "nt" and os.access(path, os.X_OK):
        return EXECUTABLE_MODE
    return FILE_MODE
