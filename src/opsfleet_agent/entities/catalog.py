from pydantic import BaseModel, ConfigDict

from opsfleet_agent.entities.query import QueryResult


class ColumnInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    note: str = ""


class TableSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    columns: tuple[ColumnInfo, ...]


class BrandRevenue(BaseModel):
    model_config = ConfigDict(frozen=True)

    brand: str
    revenue: float
    items: int


class StoredResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    sql: str
    result: QueryResult
