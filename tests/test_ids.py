import pytest

from geyser.errors import InvalidArgument
from geyser.ids import Kind, format_id, kind_of, require_id, split_id


def test_format_and_split() -> None:
    assert format_id(Kind.TASK, 0) == "task-0"
    assert split_id("cand-12") == (Kind.CANDIDATE, 12)
    assert split_id("task-01") is None
    assert split_id("bogus-3") is None
    assert kind_of("review-3") is Kind.REVIEW


def test_require_id_names_the_expected_form() -> None:
    assert require_id("task-7", Kind.TASK) == "task-7"
    with pytest.raises(InvalidArgument, match="not a bare number"):
        require_id(17, Kind.TASK, field="task_id")
    with pytest.raises(InvalidArgument, match="is a candidate"):
        require_id("cand-17", Kind.TASK, field="task_id")
    with pytest.raises(InvalidArgument, match="not a well-formed ID"):
        require_id("task 17", Kind.TASK)
