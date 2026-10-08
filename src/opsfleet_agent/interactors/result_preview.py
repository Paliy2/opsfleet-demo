from uuid import UUID

from opsfleet_agent.entities.query import JsonScalar, QueryResult

_MAX_CELL_CHARS = 60


def _cell(value: JsonScalar) -> str:
    text = "NULL" if value is None else str(value)
    return text if len(text) <= _MAX_CELL_CHARS else f"{text[:_MAX_CELL_CHARS]}…"


def render_preview(result_id: UUID, result: QueryResult, max_rows: int, offset: int = 0) -> str:
    rows = result.rows[offset : offset + max_rows]
    header = " | ".join(result.columns)
    lines = [" | ".join(_cell(value) for value in row) for row in rows]
    shown_to = offset + len(rows)
    footer = f"rows {offset + 1}-{shown_to} of {result.total_rows}"
    if shown_to < len(result.rows):
        footer += f"; more via get_result(result_id='{result_id}', offset={shown_to})"
    elif result.truncated:
        footer += "; full result exceeds the fetch cap, aggregate further in SQL"
    return "\n".join([f"result_id={result_id}", header, *lines, footer])
