from typing import Protocol

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.query import DryRunReport, QueryResult


class QueryEngine(Protocol):
    async def dry_run(self, sql: str, scope: AccessScope) -> DryRunReport: ...

    async def execute(self, sql: str, scope: AccessScope) -> QueryResult: ...
