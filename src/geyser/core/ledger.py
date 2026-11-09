"""The SQLite ledger: one writer, short transactions, append-only events.

``Ledger.transaction()`` opens ``BEGIN IMMEDIATE`` and commits on success or rolls
back on any exception, so a bug inside a command leaves no partial state. Nested
``transaction()`` calls join the outermost one. Callbacks registered with
``Txn.after_commit`` run only once the commit is durable; their errors are logged
and never undo the commit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from geyser.clock import Clock, iso
from geyser.core.schema import ADDED_COLUMNS, SCHEMA, SCHEMA_VERSION
from geyser.ids import Kind, format_id

log = logging.getLogger(__name__)


def dumps(value: Any) -> str | None:
    """Serialize a JSON column; ``None`` stays SQL NULL."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)


class Txn:
    """One ledger transaction. Every timestamp written in it is ``txn.now``."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock):
        self._connection = connection
        self.moment = clock.now()
        self.now = iso(self.moment)
        self._after_commit: list[Callable[[], None]] = []

    def execute(self, sql: str, parameters: tuple | dict = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, parameters)

    def one(self, sql: str, parameters: tuple | dict = ()) -> sqlite3.Row | None:
        return self._connection.execute(sql, parameters).fetchone()

    def all(self, sql: str, parameters: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._connection.execute(sql, parameters).fetchall()

    def scalar(self, sql: str, parameters: tuple | dict = ()) -> Any:
        row = self._connection.execute(sql, parameters).fetchone()
        return None if row is None else row[0]

    def next_number(self, kind: Kind) -> int:
        row = self.one("SELECT next FROM counters WHERE kind = ?", (kind.value,))
        number = 0 if row is None else row["next"]
        self.execute(
            "INSERT INTO counters (kind, next) VALUES (?, ?) "
            "ON CONFLICT (kind) DO UPDATE SET next = excluded.next",
            (kind.value, number + 1),
        )
        return number

    def next_id(self, kind: Kind) -> str:
        """Allocate the next ID of ``kind``. Rolled back with the transaction."""
        return format_id(kind, self.next_number(kind))

    def event(self, kind: str, subject: str | None = None, actor: str | None = None, /,
              **data: Any) -> None:
        """Append an audit event. Events are never updated or deleted."""
        self.execute(
            "INSERT INTO events (at, kind, subject, actor, data) VALUES (?, ?, ?, ?, ?)",
            (self.now, kind, subject, actor, dumps(data) or "{}"),
        )

    def after_commit(self, callback: Callable[[], None]) -> None:
        self._after_commit.append(callback)

    def meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


class Ledger:
    """The project's ledger. Use one instance per Hub process."""

    def __init__(self, path: Path, clock: Clock):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._depth = 0
        self._current: Txn | None = None
        self.before_commit: Callable[[], None] | None = None
        """Test hook: raising here aborts the transaction (fault injection)."""
        self.after_commit_hook: Callable[[], None] | None = None
        """Test hook: raising here simulates a crash right after a durable commit."""
        self._connection = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._connection.executescript(SCHEMA)
            for table, column, declaration in ADDED_COLUMNS:
                present = {row[1] for row in self._connection.execute(
                    f"PRAGMA table_info({table})")}
                if column not in present:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"ledger schema version {row[0]} is not supported "
                    f"(this Geyser uses {SCHEMA_VERSION})"
                )

    @contextmanager
    def transaction(self) -> Iterator[Txn]:
        with self._lock:
            if self._current is not None:
                self._depth += 1
                try:
                    yield self._current
                finally:
                    self._depth -= 1
                return
            txn = Txn(self._connection, self.clock)
            self._connection.execute("BEGIN IMMEDIATE")
            self._current = txn
            try:
                yield txn
                if self.before_commit is not None:
                    self.before_commit()
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            finally:
                self._current = None
        for callback in txn._after_commit:
            try:
                callback()
            except Exception:
                log.exception("after-commit callback failed")
        if self.after_commit_hook is not None:
            self.after_commit_hook()

    @contextmanager
    def read(self) -> Iterator[Txn]:
        """A read view. Inside a transaction it sees that transaction's writes."""
        with self._lock:
            if self._current is not None:
                yield self._current
                return
            yield Txn(self._connection, self.clock)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
