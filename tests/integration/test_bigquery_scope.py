import re

import pytest

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.core.errors import QueryRejectedError, SqlSyntaxError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.gateways.bigquery.engine import allowed_tables, create_engine
from opsfleet_agent.interactors.run_query import RunQuery

pytestmark = pytest.mark.bq

MANAGER_BRANDS = frozenset({"Calvin Klein", "Carhartt"})
MANAGER = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=MANAGER_BRANDS)
CEO = AccessScope(user_id="ceo", role=Role.CEO)


@pytest.fixture(scope="module")
def run() -> RunQuery:
    settings = get_settings()
    return RunQuery(
        create_engine(settings),
        allowed_tables=allowed_tables(settings.bq_source_dataset),
        max_bytes=settings.bq_max_bytes_billed,
    )


async def test_manager_sees_only_own_brands(run: RunQuery) -> None:
    result = await run("SELECT DISTINCT brand FROM products", MANAGER)

    assert {row[0] for row in result.rows} == MANAGER_BRANDS


async def test_manager_revenue_excludes_other_brands_in_mixed_orders(run: RunQuery) -> None:
    result = await run(
        "SELECT COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.brand NOT IN ('Calvin Klein', 'Carhartt')",
        MANAGER,
    )

    assert result.rows == ((0,),)


async def test_manager_sees_fewer_customers_than_ceo(run: RunQuery) -> None:
    manager = await run("SELECT COUNT(*) FROM users", MANAGER)
    ceo = await run("SELECT COUNT(*) FROM users", CEO)

    assert 0 < int(manager.rows[0][0] or 0) < int(ceo.rows[0][0] or 0)


async def test_customer_pii_is_masked(run: RunQuery) -> None:
    result = await run(
        "SELECT id, email, street_address, postal_code, latitude FROM users LIMIT 200", CEO
    )

    for user_id, email, street, postal, latitude in result.rows:
        assert re.fullmatch(r"[0-9a-f]{16}", str(user_id))
        assert str(email).startswith("user_")
        assert str(street).endswith("Redacted St")
        assert str(postal).endswith("**")
        assert latitude is None or round(float(latitude), 1) == float(latitude)


async def test_pseudonymous_ids_still_join_across_tables(run: RunQuery) -> None:
    result = await run("SELECT COUNT(*) FROM orders o JOIN users u ON u.id = o.user_id", MANAGER)

    assert int(result.rows[0][0] or 0) > 0


async def test_fully_qualified_bypass_is_rejected(run: RunQuery) -> None:
    with pytest.raises(QueryRejectedError):
        await run("SELECT email FROM `bigquery-public-data.thelook_ecommerce.users`", MANAGER)


async def test_syntax_error_is_reported_relative_to_user_sql(run: RunQuery) -> None:
    with pytest.raises(SqlSyntaxError, match=r"\[1:\d+\]"):
        await run("SELEC brand FROM products", MANAGER)
