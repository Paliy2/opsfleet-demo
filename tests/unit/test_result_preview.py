from uuid import uuid4

from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.interactors.result_preview import render_preview

RESULT = QueryResult(
    columns=("brand", "revenue"),
    rows=tuple((f"B{i}", float(i)) for i in range(30)),
    total_rows=30,
    bytes_processed=1,
)


def test_preview_is_capped_and_points_to_next_page() -> None:
    result_id = uuid4()

    preview = render_preview(result_id, RESULT, max_rows=5)

    assert preview.count("\n") == 1 + 1 + 5
    assert f"get_result(result_id='{result_id}', offset=5)" in preview
    assert "rows 1-5 of 30" in preview


def test_last_page_has_no_continuation() -> None:
    preview = render_preview(uuid4(), RESULT, max_rows=10, offset=25)

    assert "rows 26-30 of 30" in preview
    assert "get_result" not in preview


def test_long_cells_are_truncated() -> None:
    wide = QueryResult(columns=("note",), rows=(("x" * 500,),), total_rows=1, bytes_processed=1)

    assert "x" * 61 not in render_preview(uuid4(), wide, max_rows=5)
