import re
import secrets

from opsfleet_agent.entities.privacy import PSEUDONYM_FIRST_NAMES, PSEUDONYM_LAST_NAMES

_COUNTRY_ALIASES = {"España": "Spain", "Deutschland": "Germany", "Brasil": "Brazil"}

_LEADING_WITH = re.compile(r"^\s*with\s+(recursive\s+)?", re.IGNORECASE)
_ERROR_POSITION = re.compile(r"\[(\d+):(\d+)\]")
SALT_PARAMETER = f"salt_{secrets.token_hex(8)}"


def _pseudonym(column: str) -> str:
    return f"SUBSTR(TO_HEX(SHA256(CONCAT(@{SALT_PARAMETER}, CAST({column} AS STRING)))), 1, 16)"


def _pick(options: tuple[str, ...], seed: str, column: str) -> str:
    literal = ", ".join(f"'{option}'" for option in options)
    fingerprint = f"FARM_FINGERPRINT(CONCAT(@{SALT_PARAMETER}, '{seed}', CAST({column} AS STRING)))"
    return f"[{literal}][OFFSET(MOD(ABS({fingerprint}), {len(options)}))]"


def _normalized_country(column: str) -> str:
    cases = " ".join(f"WHEN '{raw}' THEN '{clean}'" for raw, clean in _COUNTRY_ALIASES.items())
    return f"CASE {column} {cases} ELSE {column} END"


def scoped_ctes(source_dataset: str) -> str:
    src = f"`{source_dataset}"
    return f"""products AS (
  SELECT * FROM {src}.products`
  WHERE @all_brands OR brand IN UNNEST(@brands)
),
order_items AS (
  SELECT oi.id, oi.order_id, {_pseudonym("oi.user_id")} AS user_id, oi.product_id,
    oi.inventory_item_id, oi.status, oi.created_at, oi.shipped_at, oi.delivered_at,
    oi.returned_at, oi.sale_price
  FROM {src}.order_items` AS oi
  JOIN products AS scoped_products ON scoped_products.id = oi.product_id
  WHERE oi.created_at <= CURRENT_TIMESTAMP()
),
orders AS (
  SELECT o.order_id, {_pseudonym("o.user_id")} AS user_id, o.status, o.gender, o.created_at,
    o.returned_at, o.shipped_at, o.delivered_at, scoped_counts.num_of_item
  FROM {src}.orders` AS o
  JOIN (
    SELECT order_id, COUNT(*) AS num_of_item FROM order_items GROUP BY order_id
  ) AS scoped_counts USING (order_id)
),
users AS (
  SELECT {_pseudonym("u.id")} AS id,
    {_pick(PSEUDONYM_FIRST_NAMES, "first", "u.id")} AS first_name,
    {_pick(PSEUDONYM_LAST_NAMES, "last", "u.id")} AS last_name,
    CONCAT('user_', SUBSTR({_pseudonym("u.id")}, 1, 8), '@',
      IFNULL(SPLIT(u.email, '@')[SAFE_OFFSET(1)], 'masked.invalid')) AS email,
    u.age, u.gender, u.state,
    CONCAT(CAST(100 + MOD(ABS(
      FARM_FINGERPRINT(CONCAT(@{SALT_PARAMETER}, 'street', CAST(u.id AS STRING)))
    ), 9900) AS STRING), ' Redacted St') AS street_address,
    CONCAT(SUBSTR(u.postal_code, 1, 3), '**') AS postal_code,
    u.city, {_normalized_country("u.country")} AS country,
    ROUND(u.latitude, 1) AS latitude, ROUND(u.longitude, 1) AS longitude,
    u.traffic_source, u.created_at,
    ST_GEOGPOINT(ROUND(u.longitude, 1), ROUND(u.latitude, 1)) AS user_geom
  FROM {src}.users` AS u
  WHERE @all_brands OR u.id IN (
    SELECT DISTINCT raw_items.user_id
    FROM {src}.order_items` AS raw_items
    JOIN products AS scoped_products ON scoped_products.id = raw_items.product_id
  )
)"""


def compose_scoped_query(user_sql: str, source_dataset: str) -> str:
    ctes = scoped_ctes(source_dataset)
    leading_with = _LEADING_WITH.match(user_sql)
    if leading_with is None:
        return f"WITH {ctes}\n{user_sql.strip()}"
    recursive = "RECURSIVE " if leading_with.group(1) else ""
    return f"WITH {recursive}{ctes},\n{user_sql[leading_with.end() :].strip()}"


def relative_error_message(message: str, source_dataset: str) -> str:
    prefix_lines = compose_scoped_query("", source_dataset).count("\n")

    def shift(match: re.Match[str]) -> str:
        line = max(int(match.group(1)) - prefix_lines, 1)
        return f"[{line}:{match.group(2)}]"

    return _ERROR_POSITION.sub(shift, message)
