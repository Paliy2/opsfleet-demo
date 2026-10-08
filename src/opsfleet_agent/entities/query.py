from enum import StrEnum

from pydantic import BaseModel, ConfigDict

type JsonScalar = str | int | float | bool | None


class SourceTable(StrEnum):
    USERS = "users"
    ORDERS = "orders"
    ORDER_ITEMS = "order_items"
    PRODUCTS = "products"


class DryRunReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_type: str
    referenced_tables: frozenset[str]
    bytes_processed: int


class QueryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    columns: tuple[str, ...]
    rows: tuple[tuple[JsonScalar, ...], ...]
    total_rows: int
    bytes_processed: int

    @property
    def is_empty(self) -> bool:
        return self.total_rows == 0

    @property
    def truncated(self) -> bool:
        return self.total_rows > len(self.rows)
