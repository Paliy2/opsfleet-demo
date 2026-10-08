from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
from google.cloud import bigquery
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.agents.guard import LlmInputGuard
from opsfleet_agent.agents.orchestrator import Orchestrator, ReportingTools
from opsfleet_agent.agents.report_writer import ReportWriter
from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.models import AgentRole
from opsfleet_agent.gateways.bigquery.catalog import BigQueryCatalog
from opsfleet_agent.gateways.bigquery.engine import BigQueryEngine, allowed_tables
from opsfleet_agent.gateways.charts.matplotlib_renderer import MatplotlibChartRenderer
from opsfleet_agent.gateways.llm.chat_models import build_chat_model
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.interactors.reports import (
    ConfirmDeletion,
    ListReports,
    PlanDeletion,
    SaveReport,
)
from opsfleet_agent.interactors.run_query import RunQuery
from opsfleet_agent.interactors.schema_text import render_schema
from opsfleet_agent.interactors.seed_demo_users import SeedDemoUsers
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.reports import FileChartRepo, SqlDeletionPlanRepo, SqlReportRepo
from opsfleet_agent.repos.results import SqlResultStore


@dataclass(frozen=True)
class App:
    chat: ChatTurn
    seed_demo_users: SeedDemoUsers


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _checkpointer_url(settings: Settings) -> str:
    return settings.database_url.get_secret_value().replace("+asyncpg", "")


@asynccontextmanager
async def build_app(settings: Settings) -> AsyncIterator[App]:
    engine, sessions = create_database(settings)
    client = bigquery.Client(project=settings.gcp_project)
    catalog = BigQueryCatalog(client, settings.bq_source_dataset)
    access = SqlAccessRepo(sessions)
    results = SqlResultStore(sessions)
    reports = SqlReportRepo(sessions)
    plans = SqlDeletionPlanRepo(sessions)
    schema_text = render_schema(await catalog.table_schemas())
    run_query = RunQuery(
        BigQueryEngine(client, settings),
        allowed_tables=allowed_tables(settings.bq_source_dataset),
        max_bytes=settings.bq_max_bytes_billed,
    )
    analyst = DataAnalyst(
        model=build_chat_model(settings, AgentRole.ANALYST),
        run_query=run_query,
        results=results,
        schema_text=schema_text,
        max_steps=settings.analyst_max_steps,
        max_sql_failures=settings.analyst_max_sql_failures,
        preview_rows=settings.preview_rows,
    )
    reporting = ReportingTools(
        writer=ReportWriter(build_chat_model(settings, AgentRole.WRITER)),
        results=results,
        charts=FileChartRepo(sessions, settings.artifacts_dir / "charts"),
        renderer=MatplotlibChartRenderer(),
        save_report=SaveReport(reports),
        list_reports=ListReports(reports),
        plan_deletion=PlanDeletion(
            reports,
            plans,
            ttl=timedelta(seconds=settings.deletion_confirmation_ttl_seconds),
            now=_utc_now,
        ),
        confirm_deletion=ConfirmDeletion(reports, plans, now=_utc_now),
    )
    async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(settings)) as checkpointer:
        await checkpointer.setup()
        orchestrator = Orchestrator(
            model=build_chat_model(settings, AgentRole.ORCHESTRATOR),
            analyst=analyst,
            reporting=reporting,
            schema_text=schema_text,
            max_steps=settings.orchestrator_max_steps,
            checkpointer=checkpointer,
        )
        chat = ChatTurn(
            access=access,
            conversations=SqlConversationRepo(sessions),
            guard=LlmInputGuard(build_chat_model(settings, AgentRole.GUARD)),
            agent=orchestrator,
            history_max_turns=settings.history_max_turns,
            history_max_chars=settings.history_max_chars,
        )
        try:
            yield App(chat=chat, seed_demo_users=SeedDemoUsers(catalog, access))
        finally:
            await engine.dispose()
            await anyio.to_thread.run_sync(client.close)
