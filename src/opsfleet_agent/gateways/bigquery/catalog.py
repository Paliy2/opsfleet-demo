from functools import partial

import anyio
from google.cloud import bigquery

from opsfleet_agent.entities.catalog import BrandRevenue, ColumnInfo, TableSchema
from opsfleet_agent.entities.query import SourceTable
from opsfleet_agent.ports.catalog import CatalogRepo

_TABLE_DESCRIPTIONS = {
    SourceTable.USERS: "Customers. Only customers who bought in-scope brands are visible.",
    SourceTable.ORDERS: "Orders. num_of_item counts only in-scope items.",
    SourceTable.ORDER_ITEMS: "Line items; revenue lives in sale_price. Future-dated rows removed.",
    SourceTable.PRODUCTS: "Product catalog, limited to the user's brands.",
}

_COLUMN_NOTES = {
    "users.id": "pseudonymous id (joins to orders.user_id / order_items.user_id)",
    "users.first_name": "pseudonym, not the real name",
    "users.last_name": "pseudonym, not the real name",
    "users.email": "masked local part; domain is real",
    "users.street_address": "surrogate value",
    "users.postal_code": "first 3 characters only",
    "users.latitude": "rounded to 0.1 degree",
    "users.longitude": "rounded to 0.1 degree",
    "users.user_geom": "rounded point",
    "users.country": "normalized English country name",
    "users.state": "region within country; same name can exist in several countries",
    "orders.user_id": "pseudonymous id",
    "order_items.user_id": "pseudonymous id",
    "order_items.status": "Processing | Shipped | Complete | Cancelled | Returned",
    "orders.status": "Processing | Shipped | Complete | Cancelled | Returned",
    "products.department": "Men | Women",
}

_BRANDS_BY_REVENUE = """
SELECT p.brand, ROUND(SUM(oi.sale_price), 2) AS revenue, COUNT(*) AS items
FROM `{source}.order_items` AS oi
JOIN `{source}.products` AS p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled', 'Returned') AND p.brand IS NOT NULL
GROUP BY p.brand
ORDER BY revenue DESC
"""


class BigQueryCatalog(CatalogRepo):
    def __init__(self, client: bigquery.Client, source_dataset: str) -> None:
        self._client = client
        self._source = source_dataset

    async def table_schemas(self) -> tuple[TableSchema, ...]:
        return tuple([await self._describe(table) for table in SourceTable])

    async def brands_by_revenue(self) -> tuple[BrandRevenue, ...]:
        query = partial(self._client.query, _BRANDS_BY_REVENUE.format(source=self._source))
        job = await anyio.to_thread.run_sync(query)
        rows = await anyio.to_thread.run_sync(job.result)
        return tuple(
            BrandRevenue(brand=row["brand"], revenue=float(row["revenue"]), items=row["items"])
            for row in rows
        )

    async def _describe(self, table: SourceTable) -> TableSchema:
        fetch = partial(self._client.get_table, f"{self._source}.{table}")
        bq_table = await anyio.to_thread.run_sync(fetch)
        columns = tuple(
            ColumnInfo(
                name=field.name,
                type=field.field_type,
                note=_COLUMN_NOTES.get(f"{table}.{field.name}", ""),
            )
            for field in bq_table.schema
        )
        return TableSchema(name=table, description=_TABLE_DESCRIPTIONS[table], columns=columns)
