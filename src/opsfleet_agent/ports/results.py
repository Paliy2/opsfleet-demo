from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.catalog import StoredResult
from opsfleet_agent.entities.query import QueryResult


class ResultStore(Protocol):
    async def save(self, session_id: UUID, sql: str, result: QueryResult) -> UUID: ...

    async def get(self, result_id: UUID, session_id: UUID) -> StoredResult: ...
