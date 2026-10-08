import re

from opsfleet_agent.core.errors import QueryRejectedError

_FORBIDDEN_FRAGMENTS = (
    "bigquery-public-data",
    "thelook_ecommerce",
    "information_schema",
    "external_query",
)
_QUALIFIED_QUOTED_PATH = re.compile(r"`[^`]*[.\-][^`]*`")


def ensure_safe_sql_text(sql: str) -> str:
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryRejectedError("empty query")
    lowered = statement.lower()
    for fragment in _FORBIDDEN_FRAGMENTS:
        if fragment in lowered:
            raise QueryRejectedError(f"direct reference to '{fragment}' is not allowed")
    if _QUALIFIED_QUOTED_PATH.search(statement):
        raise QueryRejectedError("qualified table paths are not allowed; use plain table names")
    if ";" in statement:
        raise QueryRejectedError("only a single statement is allowed")
    return statement
