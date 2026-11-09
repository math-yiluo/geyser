from pathlib import Path

import pytest

from geyser.clock import ManualClock
from geyser.core.ledger import Ledger
from geyser.ids import Kind


def test_counters_start_at_zero_and_roll_back(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3", ManualClock())
    with ledger.transaction() as txn:
        assert txn.next_id(Kind.TASK) == "task-0"
    with pytest.raises(RuntimeError):
        with ledger.transaction() as txn:
            assert txn.next_id(Kind.TASK) == "task-1"
            raise RuntimeError("boom")
    with ledger.transaction() as txn:
        assert txn.next_id(Kind.TASK) == "task-1"
        assert txn.next_id(Kind.CANDIDATE) == "cand-0"
    ledger.close()


def test_nested_transactions_join_and_after_commit_runs_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3", ManualClock())
    calls: list[str] = []
    with ledger.transaction() as outer:
        with ledger.transaction() as inner:
            assert inner is outer
            inner.after_commit(lambda: calls.append("done"))
        assert calls == []
    assert calls == ["done"]
    ledger.close()


def test_before_commit_hook_aborts(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.sqlite3", ManualClock())

    def crash() -> None:
        raise RuntimeError("simulated crash")

    ledger.before_commit = crash
    with pytest.raises(RuntimeError):
        with ledger.transaction() as txn:
            txn.set_meta("x", "1")
    ledger.before_commit = None
    with ledger.read() as txn:
        assert txn.meta("x") is None
    ledger.close()
