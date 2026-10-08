from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.catalog import StoredResult
from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.models.tables import ResultSetRow
from opsfleet_agent.ports.results import ResultStore


class SqlResultStore(ResultStore):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, session_id: UUID, sql: str, result: QueryResult) -> UUID:
        async with self._sessions.begin() as session:
            row = ResultSetRow(
                session_id=session_id, sql=sql, payload=result.model_dump(mode="json")
            )
            session.add(row)
            await session.flush()
            return row.id

    async def get(self, result_id: UUID, session_id: UUID) -> StoredResult:
        async with self._sessions() as session:
            row = await session.get(ResultSetRow, result_id)
            if row is None or row.session_id != session_id:
                raise NotFoundError(f"result {result_id} not found in this session")
            return StoredResult(sql=row.sql, result=QueryResult.model_validate(row.payload))
