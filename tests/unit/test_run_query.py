import pytest

from opsfleet_agent.core.errors import (
    CostLimitError,
    EmptyResultError,
    MaskedValueError,
    QueryRejectedError,
)
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.query import DryRunReport, QueryResult
from opsfleet_agent.interactors.run_query import RunQuery
from tests.fakes import ALLOWED, FakeQueryEngine

MANAGER = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=frozenset({"Diesel"}))
MAX_BYTES = 10_000


def run_query(engine: FakeQueryEngine) -> RunQuery:
    return RunQuery(engine, allowed_tables=ALLOWED, max_bytes=MAX_BYTES)


async def test_valid_query_executes_and_returns_rows() -> None:
    engine = FakeQueryEngine()

    result = await run_query(engine)("SELECT brand FROM products", MANAGER)

    assert result.rows == (("Diesel", 100.0),)
    assert engine.executed == ["SELECT brand FROM products"]


async def test_non_select_statement_is_rejected_before_execution() -> None:
    engine = FakeQueryEngine(
        report=DryRunReport(statement_type="DELETE", referenced_tables=ALLOWED, bytes_processed=1)
    )

    with pytest.raises(QueryRejectedError):
        await run_query(engine)("DELETE FROM orders WHERE TRUE", MANAGER)
    assert engine.executed == []


async def test_foreign_table_is_rejected_before_execution() -> None:
    engine = FakeQueryEngine(
        report=DryRunReport(
            statement_type="SELECT",
            referenced_tables=ALLOWED | {"bigquery-public-data.thelook_ecommerce.events"},
            bytes_processed=1,
        )
    )

    with pytest.raises(QueryRejectedError):
        await run_query(engine)("SELECT * FROM events", MANAGER)
    assert engine.executed == []


async def test_expensive_query_is_rejected_before_execution() -> None:
    engine = FakeQueryEngine(
        report=DryRunReport(
            statement_type="SELECT", referenced_tables=ALLOWED, bytes_processed=MAX_BYTES + 1
        )
    )

    with pytest.raises(CostLimitError):
        await run_query(engine)("SELECT * FROM order_items", MANAGER)
    assert engine.executed == []


async def test_empty_result_raises_for_self_correction() -> None:
    engine = FakeQueryEngine(
        result=QueryResult(columns=("brand",), rows=(), total_rows=0, bytes_processed=1)
    )

    with pytest.raises(EmptyResultError):
        await run_query(engine)("SELECT brand FROM products WHERE FALSE", MANAGER)


def test_manager_without_brands_is_invalid() -> None:
    with pytest.raises(ValueError, match="at least one brand"):
        AccessScope(user_id="mgr", role=Role.MANAGER)


async def test_red_team_masked_value_never_reaches_dry_run_or_engine() -> None:
    engine = FakeQueryEngine()

    with pytest.raises(MaskedValueError):
        await run_query(engine)(
            "SELECT id FROM users WHERE LOWER(email) = LOWER('[EMAIL_ADDRESS]')", MANAGER
        )
    assert engine.executed == []
