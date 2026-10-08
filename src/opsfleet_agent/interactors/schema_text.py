from collections.abc import Iterable

from opsfleet_agent.entities.catalog import TableSchema


def render_schema(tables: Iterable[TableSchema]) -> str:
    sections = []
    for table in tables:
        columns = "\n".join(
            f"  - {column.name} {column.type}" + (f" — {column.note}" if column.note else "")
            for column in table.columns
        )
        sections.append(f"### {table.name}\n{table.description}\n{columns}")
    return "\n\n".join(sections)
