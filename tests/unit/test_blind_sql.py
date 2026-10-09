import re

import pytest
from pydantic import SecretStr

from opsfleet_agent.core.errors import QueryRejectedError
from opsfleet_agent.entities.pii_tokens import BlindOperator, BlindTerm
from opsfleet_agent.gateways.bigquery.blind_sql import (
    compile_match,
    compose_blind_query,
    pseudonym_lookup_sql,
)
from opsfleet_agent.gateways.bigquery.scoped_sql import SALT_PARAMETER
from opsfleet_agent.interactors.sql_guard import ensure_safe_sql_text
from tests.fakes import SOURCE

NONCE = "0123456789abcdef"


def term(operator: BlindOperator, *values: str, info_type: str = "PERSON_NAME") -> BlindTerm:
    return BlindTerm(
        operator=operator,
        info_type=info_type,
        label=f"{info_type}_1",
        values=tuple(SecretStr(value) for value in values),
        max_distance=1 if operator is BlindOperator.FUZZY else 0,
    )


TERMS = [
    term(BlindOperator.EXACT, "jane.roe@example.org", info_type="EMAIL_ADDRESS"),
    term(BlindOperator.EXACT, "jane roe"),
    term(BlindOperator.PREFIX, "jan", info_type="EMAIL_ADDRESS"),
    term(BlindOperator.FIRST_NAME, "jane"),
    term(BlindOperator.LAST_NAME, "roe"),
    term(BlindOperator.FUZZY, "jane roe"),
    term(BlindOperator.SOUNDS_LIKE, "jane", "roe"),
]


@pytest.mark.parametrize("blind", TERMS)
def test_values_are_bound_and_only_pseudonyms_are_projected(blind: BlindTerm) -> None:
    match = compile_match("blind_1", blind, SOURCE, NONCE)

    select_line = match.cte.splitlines()[1]
    assert re.fullmatch(r"\s*SELECT SUBSTR\(.*\) AS id, '\w+' AS match_kind", select_line)
    assert f"@{SALT_PARAMETER}" in select_line
    assert "IN (SELECT id FROM users)" in match.cte
    for value in blind.values:
        assert value.get_secret_value() not in match.cte
    assert [p.value for p in match.parameters] == list(blind.values)
    assert all(f"@{parameter.name}" in match.cte for parameter in match.parameters)


def test_a_hostile_value_stays_inside_its_parameter() -> None:
    hostile = term(BlindOperator.EXACT, "x' OR 1=1 --")

    match = compile_match("blind_1", hostile, SOURCE, NONCE)

    assert "OR 1=1" not in match.cte
    assert match.parameters[0].value.get_secret_value() == "x' OR 1=1 --"


def test_the_lookup_returns_masked_identities_after_the_scoped_ctes() -> None:
    match = compile_match("blind_1", TERMS[0], SOURCE, NONCE)

    composed = compose_blind_query(pseudonym_lookup_sql(match, 25), SOURCE, [match])

    assert composed.index("users AS (") < composed.index("blind_1 AS (")
    assert composed.endswith(
        "SELECT users.id, users.first_name, users.last_name, users.email, blind_1.match_kind\n"
        "FROM users JOIN blind_1 USING (id)\nORDER BY users.id LIMIT 25"
    )
    with pytest.raises(ValueError, match="limit"):
        pseudonym_lookup_sql(match, 500)


@pytest.mark.parametrize(
    ("name", "nonce"), [("users", NONCE), ("blind_x", NONCE), ("blind_1", "x")]
)
def test_unsafe_identifiers_are_refused(name: str, nonce: str) -> None:
    with pytest.raises(ValueError, match=r"invalid|nonce"):
        compile_match(name, TERMS[1], SOURCE, nonce)


@pytest.mark.parametrize(
    "attack",
    [
        "SELECT @bm_0123456789abcdef_1_0 AS leaked",
        "SELECT LENGTH(@bm_0123456789abcdef_1_0)",
        "SELECT COUNT(*) FROM users WHERE STARTS_WITH(@bm_0123456789abcdef_1_0, 'j')",
        "SELECT @@project_id",
    ],
)
def test_model_sql_can_never_address_lookup_parameters(attack: str) -> None:
    with pytest.raises(QueryRejectedError):
        ensure_safe_sql_text(attack)
