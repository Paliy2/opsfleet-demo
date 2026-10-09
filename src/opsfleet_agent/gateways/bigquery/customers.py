import concurrent.futures
import secrets

import anyio
from google.api_core.exceptions import BadRequest
from google.cloud import bigquery

from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.errors import QueryTimeoutError, SqlSyntaxError
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.pii_tokens import BlindOperator, BlindTerm, CustomerMatch
from opsfleet_agent.gateways.bigquery.blind_sql import (
    CompiledMatch,
    compile_match,
    compose_blind_query,
    pseudonym_lookup_sql,
)
from opsfleet_agent.gateways.bigquery.engine import scope_parameters
from opsfleet_agent.ports.pii_token_vault import CustomerDirectory

_MATCH_SET = "blind_1"


class BigQueryCustomerDirectory(CustomerDirectory):
    def __init__(self, client: bigquery.Client, settings: Settings) -> None:
        self._client = client
        self._source = settings.bq_source_dataset
        self._max_bytes = settings.bq_max_bytes_billed
        self._timeout = settings.bq_timeout_seconds
        self._salt = settings.pii_hash_salt.get_secret_value()

    async def find(
        self, term: BlindTerm, scope: AccessScope, limit: int
    ) -> tuple[CustomerMatch, ...]:
        match = compile_match(_MATCH_SET, term, self._source, secrets.token_hex(8))
        sql = compose_blind_query(pseudonym_lookup_sql(match, limit), self._source, [match])
        rows = await self._run(sql, self._job_config(scope, match))
        return tuple(
            CustomerMatch(
                id=row["id"],
                first_name=row["first_name"],
                last_name=row["last_name"],
                email=row["email"],
                match_kind=BlindOperator(row["match_kind"]),
            )
            for row in rows
        )

    async def _run(self, sql: str, config: bigquery.QueryJobConfig) -> list[bigquery.table.Row]:
        def run() -> list[bigquery.table.Row]:
            job = self._client.query(sql, job_config=config)
            return list(job.result(timeout=self._timeout))

        try:
            return await anyio.to_thread.run_sync(run)
        except BadRequest as error:
            raise SqlSyntaxError(f"customer lookup failed: {error.message}") from error
        except concurrent.futures.TimeoutError as error:
            raise QueryTimeoutError(f"customer lookup exceeded {self._timeout}s") from error

    def _job_config(self, scope: AccessScope, match: CompiledMatch) -> bigquery.QueryJobConfig:
        return bigquery.QueryJobConfig(
            use_query_cache=False,
            maximum_bytes_billed=self._max_bytes,
            labels={"app": "opsfleet-agent", "role": scope.role.value, "purpose": "pii-lookup"},
            query_parameters=[
                *scope_parameters(scope, self._salt),
                *(
                    bigquery.ScalarQueryParameter(
                        parameter.name, "STRING", parameter.value.get_secret_value()
                    )
                    for parameter in match.parameters
                ),
            ],
        )
