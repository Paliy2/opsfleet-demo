from typing import Protocol

from opsfleet_agent.entities.catalog import BrandRevenue, TableSchema


class CatalogRepo(Protocol):
    async def table_schemas(self) -> tuple[TableSchema, ...]: ...

    async def brands_by_revenue(self) -> tuple[BrandRevenue, ...]: ...
