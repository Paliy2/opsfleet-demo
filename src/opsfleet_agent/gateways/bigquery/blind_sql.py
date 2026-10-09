import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from opsfleet_agent.entities.pii_tokens import BlindOperator, BlindTerm
from opsfleet_agent.gateways.bigquery.scoped_sql import compose_scoped_query, pseudonym_sql

MATCH_SET_NAME = r"blind_[1-9]\d{0,2}"
MAX_LOOKUP_ROWS = 25
_MATCH_SET = re.compile(rf"^{MATCH_SET_NAME}$")
_NONCE = re.compile(r"^[0-9a-f]{16}$")
_LEADING_WITH = re.compile(r"^\s*with\s+(recursive\s+)?", re.IGNORECASE)
_EMAIL_LOCAL = "LOWER(SPLIT(raw.email, '@')[SAFE_OFFSET(0)])"
_FULL_NAME = "LOWER(CONCAT(raw.first_name, ' ', raw.last_name))"


class BlindParameter(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^bm_[0-9a-f]{16}_\d+_\d+$")
    value: SecretStr


class CompiledMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    cte: str
    parameters: tuple[BlindParameter, ...]


def _exact(term: BlindTerm, p: tuple[str, ...]) -> str:
    column = "LOWER(TRIM(raw.email))" if term.info_type == "EMAIL_ADDRESS" else _FULL_NAME
    return f"{column} = {p[0]}"


def _prefix(_term: BlindTerm, p: tuple[str, ...]) -> str:
    return f"STARTS_WITH({_EMAIL_LOCAL}, {p[0]})"


def _first_name(_term: BlindTerm, p: tuple[str, ...]) -> str:
    return f"LOWER(raw.first_name) = {p[0]}"


def _last_name(_term: BlindTerm, p: tuple[str, ...]) -> str:
    return f"LOWER(raw.last_name) = {p[0]}"


def _fuzzy(term: BlindTerm, p: tuple[str, ...]) -> str:
    column = _EMAIL_LOCAL if term.info_type == "EMAIL_ADDRESS" else _FULL_NAME
    return f"EDIT_DISTANCE({column}, {p[0]}, max_distance => 3) <= {int(term.max_distance)}"


def _sounds_like(_term: BlindTerm, p: tuple[str, ...]) -> str:
    return f"SOUNDEX(raw.first_name) = SOUNDEX({p[0]}) AND SOUNDEX(raw.last_name) = SOUNDEX({p[1]})"


_PREDICATES: dict[BlindOperator, Callable[[BlindTerm, tuple[str, ...]], str]] = {
    BlindOperator.EXACT: _exact,
    BlindOperator.PREFIX: _prefix,
    BlindOperator.FIRST_NAME: _first_name,
    BlindOperator.LAST_NAME: _last_name,
    BlindOperator.FUZZY: _fuzzy,
    BlindOperator.SOUNDS_LIKE: _sounds_like,
}


def compile_match(name: str, term: BlindTerm, source_dataset: str, nonce: str) -> CompiledMatch:
    if not _MATCH_SET.match(name):
        raise ValueError(f"invalid match set name {name!r}")
    if not _NONCE.match(nonce):
        raise ValueError("nonce must be 16 lowercase hex characters")
    parameters = tuple(
        BlindParameter(name=f"bm_{nonce}_{name.removeprefix('blind_')}_{index}", value=value)
        for index, value in enumerate(term.values)
    )
    predicate = _PREDICATES[term.operator](term, tuple(f"@{p.name}" for p in parameters))
    raw_id = pseudonym_sql("raw.id")
    cte = f"""{name} AS (
  SELECT {raw_id} AS id, '{term.operator.value}' AS match_kind
  FROM `{source_dataset}.users` AS raw
  WHERE ({predicate})
    AND {raw_id} IN (SELECT id FROM users)
)"""
    return CompiledMatch(name=name, cte=cte, parameters=parameters)


def pseudonym_lookup_sql(match: CompiledMatch, limit: int) -> str:
    if not 1 <= limit <= MAX_LOOKUP_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_LOOKUP_ROWS}")
    return (
        "SELECT users.id, users.first_name, users.last_name, users.email, "
        f"{match.name}.match_kind\n"
        f"FROM users JOIN {match.name} USING (id)\n"
        f"ORDER BY users.id LIMIT {int(limit)}"
    )


def compose_blind_query(
    user_sql: str, source_dataset: str, matches: Sequence[CompiledMatch]
) -> str:
    if not matches:
        return compose_scoped_query(user_sql, source_dataset)
    blind_ctes = ",\n".join(match.cte for match in matches)
    leading_with = _LEADING_WITH.match(user_sql)
    if leading_with is None:
        return compose_scoped_query(f"WITH {blind_ctes}\n{user_sql.strip()}", source_dataset)
    recursive = "RECURSIVE " if leading_with.group(1) else ""
    rest = user_sql[leading_with.end() :].strip()
    return compose_scoped_query(f"WITH {recursive}{blind_ctes},\n{rest}", source_dataset)
