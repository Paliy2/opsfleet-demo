import concurrent.futures
import datetime
import decimal
from functools import partial
from typing import Any

import anyio
from google.api_core.exceptions import BadRequest
from google.cloud import bigquery

from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.errors import CostLimitError, QueryTimeoutError, SqlSyntaxError
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.query import DryRunReport, JsonScalar, QueryResult, SourceTable
from opsfleet_agent.gateways.bigquery.scoped_sql import (
    compose_scoped_query,
    relative_error_message,
)
from opsfleet_agent.ports.query_engine import QueryEngine

_BYTES_LIMIT_REASON = "bytesBilledLimitExceeded"


def allowed_tables(source_dataset: str) -> frozenset[str]:
    return frozenset(f"{source_dataset}.{table}" for table in SourceTable)


def _to_json_scalar(value: Any) -> JsonScalar:
    match value:
        case None | bool() | int() | float() | str():
            return value
        case decimal.Decimal():
            return float(value)
        case datetime.datetime() | datetime.date() | datetime.time():
            return value.isoformat()
        case _:
            return str(value)


class BigQueryEngine(QueryEngine):
    def __init__(self, client: bigquery.Client, settings: Settings, max_rows: int = 500) -> None:
        self._client = client
        self._source = settings.bq_source_dataset
        self._max_bytes = settings.bq_max_bytes_billed
        self._timeout = settings.bq_timeout_seconds
        self._salt = settings.pii_hash_salt.get_secret_value()
        self._max_rows = max_rows

    async def dry_run(self, sql: str, scope: AccessScope) -> DryRunReport:
        job = await self._submit(sql, scope, dry_run=True)
        tables = frozenset(
            f"{table.project}.{table.dataset_id}.{table.table_id}"
            for table in job.referenced_tables
        )
        return DryRunReport(
            statement_type=job.statement_type or "UNKNOWN",
            referenced_tables=tables,
            bytes_processed=job.total_bytes_processed or 0,
        )

    async def execute(self, sql: str, scope: AccessScope) -> QueryResult:
        job = await self._submit(sql, scope, dry_run=False)
        rows = await self._fetch(job)
        return QueryResult(
            columns=tuple(field.name for field in rows.schema),
            rows=tuple(tuple(_to_json_scalar(value) for value in row.values()) for row in rows),
            total_rows=rows.total_rows or 0,
            bytes_processed=job.total_bytes_processed or 0,
        )

    async def _submit(self, sql: str, scope: AccessScope, *, dry_run: bool) -> bigquery.QueryJob:
        query = partial(
            self._client.query,
            compose_scoped_query(sql, self._source),
            job_config=self._job_config(scope, dry_run=dry_run),
        )
        try:
            return await anyio.to_thread.run_sync(query)
        except BadRequest as error:
            raise self._translate(error) from error

    async def _fetch(self, job: bigquery.QueryJob) -> bigquery.table.RowIterator:
        fetch = partial(job.result, timeout=self._timeout, max_results=self._max_rows)
        try:
            return await anyio.to_thread.run_sync(fetch)
        except BadRequest as error:
            raise self._translate(error) from error
        except concurrent.futures.TimeoutError as error:
            raise QueryTimeoutError(f"query exceeded {self._timeout}s") from error

    def _translate(self, error: BadRequest) -> SqlSyntaxError | CostLimitError:
        reasons = {detail.get("reason") for detail in error.errors or []}
        if _BYTES_LIMIT_REASON in reasons:
            return CostLimitError(error.message)
        return SqlSyntaxError(relative_error_message(error.message, self._source))

    def _job_config(self, scope: AccessScope, *, dry_run: bool) -> bigquery.QueryJobConfig:
        return bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=not dry_run,
            maximum_bytes_billed=self._max_bytes,
            labels={"app": "opsfleet-agent", "role": scope.role.value},
            query_parameters=[
                bigquery.ArrayQueryParameter("brands", "STRING", sorted(scope.brands)),
                bigquery.ScalarQueryParameter("all_brands", "BOOL", scope.sees_all_brands),
                bigquery.ScalarQueryParameter("pii_salt", "STRING", self._salt),
            ],
        )


def create_engine(settings: Settings) -> BigQueryEngine:
    return BigQueryEngine(bigquery.Client(project=settings.gcp_project), settings)
