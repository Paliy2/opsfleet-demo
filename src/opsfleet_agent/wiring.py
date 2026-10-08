from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from google.cloud import bigquery

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.agents.guard import LlmInputGuard
from opsfleet_agent.agents.orchestrator import Orchestrator
from opsfleet_agent.core.config import Settings
from opsfleet_agent.core.models import AgentRole
from opsfleet_agent.gateways.bigquery.catalog import BigQueryCatalog
from opsfleet_agent.gateways.bigquery.engine import BigQueryEngine, allowed_tables
from opsfleet_agent.gateways.llm.chat_models import build_chat_model
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.interactors.run_query import RunQuery
from opsfleet_agent.interactors.schema_text import render_schema
from opsfleet_agent.interactors.seed_demo_users import SeedDemoUsers
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database
from opsfleet_agent.repos.results import SqlResultStore


@dataclass(frozen=True)
class App:
    chat: ChatTurn
    seed_demo_users: SeedDemoUsers


@asynccontextmanager
async def build_app(settings: Settings) -> AsyncIterator[App]:
    engine, sessions = create_database(settings)
    client = bigquery.Client(project=settings.gcp_project)
    catalog = BigQueryCatalog(client, settings.bq_source_dataset)
    access = SqlAccessRepo(sessions)
    schema_text = render_schema(await catalog.table_schemas())
    run_query = RunQuery(
        BigQueryEngine(client, settings),
        allowed_tables=allowed_tables(settings.bq_source_dataset),
        max_bytes=settings.bq_max_bytes_billed,
    )
    analyst = DataAnalyst(
        model=build_chat_model(settings, AgentRole.ANALYST),
        run_query=run_query,
        results=SqlResultStore(sessions),
        schema_text=schema_text,
        max_steps=settings.analyst_max_steps,
        max_sql_failures=settings.analyst_max_sql_failures,
        preview_rows=settings.preview_rows,
    )
    orchestrator = Orchestrator(
        build_chat_model(settings, AgentRole.ORCHESTRATOR),
        analyst,
        schema_text,
        max_steps=settings.orchestrator_max_steps,
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
