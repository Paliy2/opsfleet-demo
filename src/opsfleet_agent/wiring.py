from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

import anyio
from google.cloud import bigquery
from google.cloud.dlp_v2 import DlpServiceAsyncClient
from google.cloud.storage import Client as StorageClient
from langfuse import Langfuse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from opsfleet_agent.agents.analyst import DataAnalyst
from opsfleet_agent.agents.golden_curator import GoldenCurator
from opsfleet_agent.agents.guard import LlmInputGuard
from opsfleet_agent.agents.orchestrator import Orchestrator, ReportingTools
from opsfleet_agent.agents.report_writer import ReportWriter
from opsfleet_agent.agents.summarizer import LlmSummarizer
from opsfleet_agent.core.config import PiiRedactorKind, Settings
from opsfleet_agent.core.roles import AgentRole
from opsfleet_agent.gateways.bigquery.catalog import BigQueryCatalog
from opsfleet_agent.gateways.bigquery.engine import BigQueryEngine, allowed_tables
from opsfleet_agent.gateways.charts.matplotlib_renderer import MatplotlibChartRenderer
from opsfleet_agent.gateways.dlp.redactor import DlpRedactor
from opsfleet_agent.gateways.llm.chat_models import build_chat_model
from opsfleet_agent.gateways.llm.embeddings import GatewayEmbedder
from opsfleet_agent.gateways.observability.telemetry import TracingTelemetry
from opsfleet_agent.gateways.prompts.langfuse import LangfusePromptSource
from opsfleet_agent.gateways.prompts.local import LocalPromptSource
from opsfleet_agent.gateways.storage.blobs import GcsBlobStore, LocalBlobStore
from opsfleet_agent.interactors.chat_turn import ChatTurn
from opsfleet_agent.interactors.context import UpdatePreferences
from opsfleet_agent.interactors.golden import LoadGoldenTrios, RetrieveExamples
from opsfleet_agent.interactors.pii_redaction import LayeredRedactor, RegexRedactor
from opsfleet_agent.interactors.reports import (
    ConfirmDeletion,
    ExpireDeletionPlans,
    GetReport,
    ListReports,
    PlanDeletion,
    SaveReport,
)
from opsfleet_agent.interactors.run_query import RunQuery
from opsfleet_agent.interactors.schema_text import render_schema
from opsfleet_agent.interactors.seed_demo_users import SeedDemoUsers
from opsfleet_agent.ports.access import AccessRepo
from opsfleet_agent.ports.blobs import BlobStore
from opsfleet_agent.ports.catalog import CatalogRepo
from opsfleet_agent.ports.golden import GoldenRepo
from opsfleet_agent.ports.privacy import PiiRedactor
from opsfleet_agent.ports.prompts import PromptSource
from opsfleet_agent.ports.reports import ChartRepo, ReportRepo
from opsfleet_agent.ports.results import ResultStore
from opsfleet_agent.repos.access import SqlAccessRepo
from opsfleet_agent.repos.context import SqlPreferenceRepo, SqlSessionMemory
from opsfleet_agent.repos.conversations import SqlConversationRepo
from opsfleet_agent.repos.database import create_database, ping
from opsfleet_agent.repos.golden import SqlGoldenRepo
from opsfleet_agent.repos.reports import SqlChartRepo, SqlDeletionPlanRepo, SqlReportRepo
from opsfleet_agent.repos.results import SqlResultStore


@dataclass(frozen=True)
class App:
    chat: ChatTurn
    seed_demo_users: SeedDemoUsers
    curator: GoldenCurator
    load_golden: LoadGoldenTrios
    access: AccessRepo
    run_query: RunQuery
    results: ResultStore
    reports: ReportRepo
    list_reports: ListReports
    get_report: GetReport
    charts: ChartRepo
    ping: Callable[[], Awaitable[None]]
    expire_plans: ExpireDeletionPlans


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _checkpointer_url(settings: Settings) -> str:
    return settings.database_url.get_secret_value().replace("+asyncpg", "")


@contextmanager
def langfuse_client(settings: Settings) -> Iterator[Langfuse | None]:
    if settings.langfuse_secret_key is None:
        yield None
        return
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
        environment=settings.env.value,
    )
    try:
        yield client
    finally:
        client.shutdown()


def build_prompt_source(settings: Settings, client: Langfuse | None) -> PromptSource:
    if client is None:
        return LocalPromptSource()
    return LangfusePromptSource(
        client, label=settings.prompt_label, cache_ttl_seconds=settings.prompt_cache_ttl_seconds
    )


def build_redactor(settings: Settings, catalog: CatalogRepo) -> PiiRedactor:
    if settings.pii_redactor is PiiRedactorKind.REGEX:
        return RegexRedactor()

    async def brand_names() -> tuple[str, ...]:
        return tuple(brand.brand for brand in await catalog.brands_by_revenue())

    dlp = DlpRedactor(
        DlpServiceAsyncClient(),
        project=settings.gcp_project,
        location=settings.dlp_location,
        allowed_terms=brand_names,
        timeout_seconds=settings.dlp_timeout_seconds,
    )
    return LayeredRedactor([RegexRedactor(), dlp])


@asynccontextmanager
async def build_app(settings: Settings) -> AsyncIterator[App]:
    with langfuse_client(settings) as client:
        async with assemble_app(settings, client) as app:
            yield app


def build_blob_store(settings: Settings) -> BlobStore:
    if settings.chart_bucket is None:
        return LocalBlobStore(settings.artifacts_dir)
    return GcsBlobStore(StorageClient(project=settings.gcp_project), settings.chart_bucket)


def _same_repo(repo: GoldenRepo) -> GoldenRepo:
    return repo


@asynccontextmanager
async def assemble_app(
    settings: Settings,
    langfuse: Langfuse | None,
    *,
    golden_view: Callable[[GoldenRepo], GoldenRepo] = _same_repo,
) -> AsyncIterator[App]:
    telemetry = TracingTelemetry(
        langfuse, salt=settings.pii_hash_salt.get_secret_value(), environment=settings.env.value
    )
    prompts = build_prompt_source(settings, langfuse)
    engine, sessions = create_database(settings)
    client = bigquery.Client(project=settings.gcp_project)
    catalog = BigQueryCatalog(client, settings.bq_source_dataset)
    access = SqlAccessRepo(sessions)
    preferences = SqlPreferenceRepo(sessions)
    results = SqlResultStore(sessions)
    reports = SqlReportRepo(sessions)
    charts = SqlChartRepo(sessions, build_blob_store(settings))
    plans = SqlDeletionPlanRepo(sessions)
    schema_text = render_schema(await catalog.table_schemas())
    embedder = GatewayEmbedder(settings)
    golden = golden_view(SqlGoldenRepo(sessions))
    run_query = RunQuery(
        BigQueryEngine(client, settings),
        allowed_tables=allowed_tables(settings.bq_source_dataset),
        max_bytes=settings.bq_max_bytes_billed,
    )
    analyst = DataAnalyst(
        model=build_chat_model(settings, AgentRole.ANALYST),
        prompts=prompts,
        examples=RetrieveExamples(
            embedder,
            golden,
            limit=settings.golden_examples_limit,
            min_similarity=settings.golden_min_similarity,
        ),
        examples_max_chars=settings.golden_max_chars,
        run_query=run_query,
        results=results,
        schema_text=schema_text,
        max_steps=settings.analyst_max_steps,
        max_sql_failures=settings.analyst_max_sql_failures,
        preview_rows=settings.preview_rows,
    )
    reporting = ReportingTools(
        writer=ReportWriter(build_chat_model(settings, AgentRole.WRITER), prompts),
        results=results,
        charts=charts,
        renderer=MatplotlibChartRenderer(),
        save_report=SaveReport(reports),
        list_reports=ListReports(reports),
        get_report=GetReport(reports),
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
            prompts=prompts,
            preferences=UpdatePreferences(preferences),
            analyst=analyst,
            reporting=reporting,
            schema_text=schema_text,
            max_steps=settings.orchestrator_max_steps,
            checkpointer=checkpointer,
            callbacks=telemetry.callbacks(),
        )
        chat = ChatTurn(
            access=access,
            conversations=SqlConversationRepo(sessions),
            redactor=build_redactor(settings, catalog),
            guard=LlmInputGuard(
                build_chat_model(settings, AgentRole.GUARD), prompts, telemetry.callbacks()
            ),
            agent=orchestrator,
            telemetry=telemetry,
            memory=SqlSessionMemory(sessions),
            preferences=preferences,
            summarizer=LlmSummarizer(
                build_chat_model(settings, AgentRole.SUMMARIZER),
                prompts,
                settings.summary_max_chars,
            ),
            history_max_turns=settings.history_max_turns,
            history_max_chars=settings.history_max_chars,
            state_max_steps=settings.analysis_state_max_steps,
        )
        curator = GoldenCurator(
            model=build_chat_model(settings, AgentRole.CURATOR),
            prompts=prompts,
            run_query=run_query,
            schema_text=schema_text,
            preview_rows=settings.preview_rows,
        )
        try:
            yield App(
                chat=chat,
                seed_demo_users=SeedDemoUsers(catalog, access),
                curator=curator,
                load_golden=LoadGoldenTrios(embedder, golden),
                access=access,
                run_query=run_query,
                results=results,
                reports=reports,
                list_reports=reporting.list_reports,
                get_report=reporting.get_report,
                charts=charts,
                ping=partial(ping, engine),
                expire_plans=ExpireDeletionPlans(plans, now=_utc_now),
            )
        finally:
            await engine.dispose()
            await anyio.to_thread.run_sync(client.close)
