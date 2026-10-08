from dataclasses import dataclass, field

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.query import DryRunReport, QueryResult

SOURCE = "bigquery-public-data.thelook_ecommerce"
ALLOWED = frozenset(f"{SOURCE}.{t}" for t in ("users", "orders", "order_items", "products"))


@dataclass
class FakeQueryEngine:
    report: DryRunReport = field(
        default_factory=lambda: DryRunReport(
            statement_type="SELECT", referenced_tables=ALLOWED, bytes_processed=1_000
        )
    )
    result: QueryResult = field(
        default_factory=lambda: QueryResult(
            columns=("brand", "revenue"),
            rows=(("Diesel", 100.0),),
            total_rows=1,
            bytes_processed=1_000,
        )
    )
    executed: list[str] = field(default_factory=list)

    async def dry_run(self, sql: str, scope: AccessScope) -> DryRunReport:
        return self.report

    async def execute(self, sql: str, scope: AccessScope) -> QueryResult:
        self.executed.append(sql)
        return self.result
