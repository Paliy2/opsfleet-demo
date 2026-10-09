import pytest

from opsfleet_agent.core.errors import MaskedValueError, QueryRejectedError
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users WHERE LOWER(email) = LOWER('[EMAIL_ADDRESS]')",
        'SELECT * FROM users WHERE first_name = "[PERSON_NAME]"',
        "SELECT * FROM users WHERE CONCAT(first_name, ' ', last_name) LIKE '%[PERSON_NAME]%'",
        "SELECT * FROM users WHERE street_address = '''[STREET_ADDRESS]'''",
        "SELECT * FROM users WHERE email = r'[email_address]'",
        "SELECT * FROM users WHERE first_name = 'Zed' AND phone = '+1 [PHONE_NUMBER]'",
    ],
)
def test_privacy_placeholders_in_literals_are_refused(sql: str) -> None:
    with pytest.raises(MaskedValueError):
        ensure_safe_sql_text(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [1, 2, 3] AS numbers",
        "SELECT tags[OFFSET(0)] FROM (SELECT ['a', 'b'] AS tags)",
        "SELECT CAST(NULL AS ARRAY<STRING>) AS empty",
        "SELECT name FROM products WHERE name LIKE '%[SALE]%'",
        "SELECT '[draft] q3' AS label",
        "SELECT 1 AS n -- it's fine to mention [PERSON_NAME] in a comment",
        "SELECT `[PERSON_NAME]` FROM (SELECT 1 AS `[PERSON_NAME]`)",
        "SELECT 'it\\'s' AS quoted, '[1]' AS bracket",
    ],
)
def test_legitimate_brackets_pass(sql: str) -> None:
    assert ensure_safe_sql_text(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT @pii_salt AS salt",
        "SELECT TO_HEX(SHA256(CONCAT(@pii_salt, '42'))) AS probe",
        "SELECT * FROM users WHERE id IN UNNEST(@brands)",
        "SELECT @@project_id",
        "SELECT 'x' AS a, @ salt AS b",
    ],
)
def test_red_team_query_parameters_cannot_be_read(sql: str) -> None:
    with pytest.raises(QueryRejectedError, match="parameters"):
        ensure_safe_sql_text(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM users WHERE email LIKE '%@example.com'",
        "SELECT 1 AS n -- reach me @ the office",
        "SELECT `odd@name` FROM orders",
    ],
)
def test_at_signs_in_literals_comments_and_identifiers_pass(sql: str) -> None:
    assert ensure_safe_sql_text(sql) == sql
