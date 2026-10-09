import re

from opsfleet_agent.core.errors import MaskedValueError, QueryRejectedError
from opsfleet_agent.entities.privacy import placeholders_in

_FORBIDDEN_FRAGMENTS = (
    "bigquery-public-data",
    "thelook_ecommerce",
    "information_schema",
    "external_query",
)
_QUALIFIED_QUOTED_PATH = re.compile(r"`[^`]*[.\-][^`]*`")
_COMMENT = r"--[^\n]*|#[^\n]*|/\*.*?\*/"
_QUOTED_IDENTIFIER = r"`(?:[^`\\]|\\.)*`"
_STRING_LITERAL = r"""'{3}.*?'{3}|"{3}.*?"{3}|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*\""""
_SQL_TOKEN = re.compile(
    rf"{_COMMENT}|{_QUOTED_IDENTIFIER}|(?P<literal>{_STRING_LITERAL})", re.DOTALL
)


def _string_literals(statement: str) -> list[str]:
    return [token["literal"] for token in _SQL_TOKEN.finditer(statement) if token["literal"]]


def ensure_no_parameters(statement: str) -> None:
    if "@" in _SQL_TOKEN.sub(" ", statement):
        raise QueryRejectedError("query parameters and system variables (@name) are not allowed")


def ensure_no_masked_values(statement: str) -> None:
    found = sorted({tag for text in _string_literals(statement) for tag in placeholders_in(text)})
    if found:
        raise MaskedValueError(f"string literal holds a privacy placeholder: {found}")


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
    ensure_no_parameters(statement)
    ensure_no_masked_values(statement)
    return statement
