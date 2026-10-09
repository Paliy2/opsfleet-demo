import re

from opsfleet_agent.gateways.bigquery.scoped_sql import (
    SALT_PARAMETER,
    compose_scoped_query,
    relative_error_message,
    scoped_ctes,
)
from tests.fakes import SOURCE

RAW_PII_SELECTIONS = ("u.first_name", "u.last_name", "u.street_address", "u.latitude AS")


def test_plain_query_is_wrapped_by_scoped_ctes() -> None:
    composed = compose_scoped_query("SELECT COUNT(*) FROM users", SOURCE)

    assert composed.startswith("WITH products AS (")
    assert composed.endswith("SELECT COUNT(*) FROM users")


def test_user_ctes_are_merged_into_one_with_clause() -> None:
    composed = compose_scoped_query("with t AS (SELECT 1 AS n) SELECT n FROM t", SOURCE)

    assert len(re.findall(r"\bWITH\b", composed, re.IGNORECASE)) == 1
    assert composed.endswith(",\nt AS (SELECT 1 AS n) SELECT n FROM t")


def test_recursive_keyword_is_hoisted_to_front() -> None:
    composed = compose_scoped_query("WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t", SOURCE)

    assert composed.startswith("WITH RECURSIVE products AS (")


def test_brand_scope_and_salt_are_parameters_not_literals() -> None:
    ctes = scoped_ctes(SOURCE)

    assert "IN UNNEST(@brands)" in ctes
    assert "@all_brands" in ctes
    assert f"@{SALT_PARAMETER}" in ctes


def test_raw_pii_columns_are_never_projected() -> None:
    ctes = scoped_ctes(SOURCE)

    for selection in RAW_PII_SELECTIONS:
        assert selection not in ctes
    assert ctes.count("u.email") == 1
    assert "SPLIT(u.email, '@')[SAFE_OFFSET(1)]" in ctes


def test_order_item_dates_are_capped_at_now() -> None:
    assert "oi.created_at <= CURRENT_TIMESTAMP()" in scoped_ctes(SOURCE)


def test_error_positions_are_relative_to_user_sql() -> None:
    prefix_lines = compose_scoped_query("", SOURCE).count("\n")

    message = relative_error_message(f"Syntax error at [{prefix_lines + 2}:7]", SOURCE)

    assert message == "Syntax error at [2:7]"
