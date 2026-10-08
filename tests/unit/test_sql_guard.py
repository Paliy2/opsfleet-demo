import pytest

from opsfleet_agent.core.errors import QueryRejectedError
from opsfleet_agent.interactors.sql_guard import ensure_safe_sql_text


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT brand, SUM(sale_price) FROM order_items "
        "JOIN products ON products.id = order_items.product_id GROUP BY brand",
        "WITH monthly AS (SELECT 1 AS n) SELECT n FROM monthly",
        "SELECT `status`, COUNT(*) FROM orders GROUP BY 1;",
    ],
)
def test_plain_analytical_queries_pass(sql: str) -> None:
    assert ensure_safe_sql_text(sql) == sql.strip().rstrip(";")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM `bigquery-public-data.thelook_ecommerce.users`",
        "SELECT email FROM `bigquery-public-data`.thelook_ecommerce.users",
        "SELECT * FROM thelook_ecommerce.users",
        "SELECT * FROM `other-project.secrets.salaries`",
        "SELECT * FROM `ds.table`",
        "SELECT table_name FROM INFORMATION_SCHEMA.TABLES",
        "SELECT * FROM EXTERNAL_QUERY('conn', 'SELECT 1')",
        "SELECT 1; DROP TABLE orders",
        "   ",
    ],
)
def test_bypass_attempts_are_rejected(sql: str) -> None:
    with pytest.raises(QueryRejectedError):
        ensure_safe_sql_text(sql)
