from opsfleet_agent.core.errors import CostLimitError, EmptyResultError, QueryRejectedError
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.query import DryRunReport, QueryResult
from opsfleet_agent.interactors.sql_guard import ensure_safe_sql_text
from opsfleet_agent.ports.query_engine import QueryEngine


class RunQuery:
    def __init__(self, engine: QueryEngine, allowed_tables: frozenset[str], max_bytes: int) -> None:
        self._engine = engine
        self._allowed_tables = allowed_tables
        self._max_bytes = max_bytes

    async def __call__(self, sql: str, scope: AccessScope) -> QueryResult:
        statement = ensure_safe_sql_text(sql)
        self._enforce(await self._engine.dry_run(statement, scope))
        result = await self._engine.execute(statement, scope)
        if result.is_empty:
            raise EmptyResultError("query returned no rows")
        return result

    def _enforce(self, report: DryRunReport) -> None:
        if report.statement_type != "SELECT":
            raise QueryRejectedError(f"statement type {report.statement_type} is not allowed")
        foreign = report.referenced_tables - self._allowed_tables
        if foreign:
            raise QueryRejectedError(f"tables outside the allowed set: {sorted(foreign)}")
        if report.bytes_processed > self._max_bytes:
            raise CostLimitError(
                f"query would scan {report.bytes_processed} bytes (limit {self._max_bytes})"
            )
