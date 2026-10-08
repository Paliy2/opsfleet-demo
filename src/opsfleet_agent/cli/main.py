import asyncio
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import anyio
import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from langfuse import Langfuse
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.status import Status
from rich.table import Table

from opsfleet_agent.agents.golden_curator import GOLDEN_CATEGORIES
from opsfleet_agent.cli.remote import remote_app
from opsfleet_agent.core.config import Environment, Settings, get_settings
from opsfleet_agent.core.errors import AgentError
from opsfleet_agent.entities.conversation import ConfirmationRequest, ProgressEvent, TurnOutcome
from opsfleet_agent.entities.telemetry import TraceDetail, TraceStep
from opsfleet_agent.gateways.golden_dataset import read_trios, write_trios
from opsfleet_agent.gateways.observability.metrics import serve_metrics
from opsfleet_agent.gateways.observability.traces import LangfuseTraceReader
from opsfleet_agent.gateways.prompts.langfuse import LangfusePromptSource
from opsfleet_agent.wiring import App, build_app, langfuse_client
from opsfleet_agent.workers.main import connect
from opsfleet_agent.workers.workflows import GOLDEN_INGESTION, GoldenIngestionWorkflow

app = typer.Typer(no_args_is_help=True, add_completion=False)
prompts_app = typer.Typer(no_args_is_help=True, help="Manage agent prompts in Langfuse.")
app.add_typer(prompts_app, name="prompts")
golden_app = typer.Typer(no_args_is_help=True, help="Curate and load golden example trios.")
app.add_typer(golden_app, name="golden")
app.add_typer(remote_app, name="remote")
console = Console()
_PREVIEW_CHARS = 240

_STAGE_LABELS = {
    "guard": "Checking request",
    "thinking": "Planning",
    "analysis": "Analyzing",
    "query": "Querying",
    "retry": "Self-correcting",
    "schema": "Reading schema",
    "chart": "Charting",
    "report": "Writing report",
    "confirm": "Awaiting confirmation",
    "examples": "Using examples",
    "memory": "Summarizing earlier turns",
}
_TRAIL_STAGES = frozenset({"analysis", "query", "retry", "chart", "report"})
_EXIT_COMMANDS = frozenset({"/exit", "/quit"})


class StatusProgress:
    def __init__(self, status: Status) -> None:
        self._status = status

    def emit(self, event: ProgressEvent) -> None:
        label = _STAGE_LABELS.get(event.stage, event.stage)
        self._status.update(f"[cyan]{label}[/]… {event.detail}")
        if event.stage in _TRAIL_STAGES and event.detail:
            console.print(f"  [dim]{label}: {event.detail}[/]")


def _require_local(settings: Settings) -> None:
    if settings.env is not Environment.LOCAL:
        raise typer.BadParameter("--user dev login is only available with OPSFLEET_ENV=local")


def _render(outcome: TurnOutcome) -> None:
    console.print(Markdown(outcome.reply.text))
    for path in outcome.reply.chart_paths:
        console.print(f"[magenta]chart:[/] {path}")
    console.print(
        f"[dim]llm calls: {outcome.reply.llm_calls} · "
        f"results: {len(outcome.reply.result_ids)} · session: {outcome.session_id} · "
        f"trace: {outcome.trace_id}[/]"
    )


def _confirmation_table(request: ConfirmationRequest) -> Table:
    table = Table(title="Reports to delete permanently", title_style="bold red")
    table.add_column("Title")
    table.add_column("Created")
    for item in request.items:
        table.add_row(item.title, f"{item.created_at:%Y-%m-%d %H:%M}")
    return table


async def _confirm(
    application: App, user: str, session_id: UUID, request: ConfirmationRequest
) -> TurnOutcome:
    console.print(_confirmation_table(request))
    answer = await anyio.to_thread.run_sync(
        Prompt.ask,
        f"[bold red]Delete these {len(request.items)} report(s)? Type 'delete' to confirm",
    )
    approved = answer.strip().lower() == "delete"
    with console.status("[cyan]Finishing…[/]") as status:
        return await application.chat.confirm(
            user, session_id, request.turn_id, approved, StatusProgress(status)
        )


async def _ask(application: App, user: str, session_id: UUID, question: str) -> None:
    try:
        with console.status("[cyan]Working…[/]") as status:
            outcome = await application.chat(user, session_id, question, StatusProgress(status))
        while outcome.reply.confirmation is not None:
            outcome = await _confirm(
                application, user, outcome.session_id, outcome.reply.confirmation
            )
    except AgentError as error:
        console.print(
            f"[yellow]{error.user_message}[/] [dim]({error.code} · trace: {error.trace_id})[/]"
        )
        return
    _render(outcome)


async def _chat_loop(settings: Settings, user: str, session_id: UUID | None) -> None:
    async with build_app(settings) as application:
        session_id = session_id or await application.chat.open_session(user)
        console.print(f"[bold]OpsFleet analyst[/] — signed in as [green]{user}[/]. /new, /exit")
        while True:
            question = (await anyio.to_thread.run_sync(Prompt.ask, "[bold]you[/]")).strip()
            if question in _EXIT_COMMANDS:
                return
            if question == "/new":
                session_id = await application.chat.open_session(user)
                console.print("[dim]new conversation[/]")
                continue
            if question:
                await _ask(application, user, session_id, question)


async def _seed(settings: Settings) -> None:
    async with build_app(settings) as application:
        for user in await application.seed_demo_users():
            console.print(
                f"{user.user_id:10} {user.role:8} {', '.join(user.brands) or 'all brands'}"
            )


@app.command()
def chat(
    user: Annotated[str, typer.Option("--user", help="Demo user id (local dev login).")],
    session: Annotated[UUID | None, typer.Option(help="Resume an existing session.")] = None,
) -> None:
    settings = get_settings()
    _require_local(settings)
    if settings.metrics_port is not None:
        serve_metrics(settings.metrics_port)
        console.print(f"[dim]metrics on http://localhost:{settings.metrics_port}/metrics[/]")
    asyncio.run(_chat_loop(settings, user, session))


@app.command()
def seed() -> None:
    asyncio.run(_seed(get_settings()))


@app.command()
def doctor() -> None:
    settings = get_settings()
    console.print(
        f"env={settings.env} project={settings.gcp_project} profile={settings.model_profile}"
    )


def _require_langfuse(client: Langfuse | None) -> Langfuse:
    if client is None:
        raise typer.BadParameter("Langfuse is not configured (OPSFLEET_LANGFUSE_* settings)")
    return client


def _preview(value: object, limit: int = _PREVIEW_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _step_line(step: TraceStep) -> str:
    facts = [
        f"{step.duration_ms} ms" if step.duration_ms is not None else "",
        step.model or "",
        f"{step.tokens} tok" if step.tokens else "",
        f"[red]{step.level}: {step.status}[/]" if step.status else "",
    ]
    details = " · ".join(fact for fact in facts if fact)
    return f"{'  ' * step.depth}[bold]{step.kind.lower()}[/] {step.name} [dim]{details}[/]"


def _render_trace(detail: TraceDetail) -> None:
    console.rule(f"trace {detail.trace_id}")
    console.print(
        f"{detail.name} · {detail.started_at:%Y-%m-%d %H:%M:%S} · user {detail.user_hash} · "
        f"session {detail.session_id} · tags {', '.join(detail.tags)}"
    )
    console.print(f"[bold]question:[/] {_preview(detail.question)}")
    for name, version in sorted(detail.prompts.items()):
        console.print(f"[cyan]prompt[/] {name} = {version}")
    for code in detail.error_codes:
        console.print(f"[yellow]error[/] {code}")
    for fallback in detail.fallbacks:
        console.print(f"[magenta]fallback[/] {fallback}")
    console.rule("steps")
    for step in detail.steps:
        console.print(_step_line(step))
        if step.kind == "TOOL":
            console.print(f"{'  ' * (step.depth + 1)}[dim]in:[/] {_preview(step.input)}")
            console.print(f"{'  ' * (step.depth + 1)}[dim]out:[/] {_preview(step.output)}")
    console.rule("answer")
    console.print(Markdown(str(detail.answer or "")))


@app.command()
def debug(
    trace_id: Annotated[str, typer.Argument(help="Trace id printed after each answer.")],
) -> None:
    with langfuse_client(get_settings()) as client:
        reader = LangfuseTraceReader(_require_langfuse(client))
        try:
            detail = asyncio.run(reader.get(trace_id))
        except AgentError as error:
            console.print(f"[yellow]{error.detail}[/] [dim]({error.code})[/]")
            raise typer.Exit(1) from error
    _render_trace(detail)


@prompts_app.command("push")
def push_prompts(
    label: Annotated[str, typer.Option(help="Label to publish under.")] = "production",
) -> None:
    settings = get_settings()
    with langfuse_client(settings) as client:
        source = LangfusePromptSource(_require_langfuse(client), label=label, cache_ttl_seconds=0)
        for published in asyncio.run(source.publish_local(label)):
            state = "[green]published[/]" if published.changed else "[dim]unchanged[/]"
            console.print(f"{published.name:32} v{published.version:<4} {state}")


class _LineProgress:
    def emit(self, event: ProgressEvent) -> None:
        code = f" [yellow]{event.code}[/]" if event.code else ""
        console.print(f"  [dim]{event.stage}[/] {event.detail}{code}")


async def _curate(settings: Settings, per_category: int) -> None:
    async with build_app(settings) as application:
        result = await application.curator.curate(GOLDEN_CATEGORIES, per_category, _LineProgress())
    write_trios(settings.golden_dataset, result.kept)
    console.print(
        f"[green]kept {len(result.kept)}[/], rejected {len(result.rejected)} "
        f"→ {settings.golden_dataset}"
    )


async def _load_golden(settings: Settings) -> None:
    trios = read_trios(settings.golden_dataset)
    async with build_app(settings) as application:
        loaded = await application.load_golden(trios)
    console.print(f"[green]loaded {loaded} golden trios[/] from {settings.golden_dataset}")


@golden_app.command("curate")
def curate_golden(
    per_category: Annotated[int, typer.Option(min=1, max=12)] = 6,
) -> None:
    asyncio.run(_curate(get_settings(), per_category))


@golden_app.command("load")
def load_golden() -> None:
    asyncio.run(_load_golden(get_settings()))


_ALEMBIC_INI = Path("alembic.ini")


async def _load_golden_durably(settings: Settings) -> None:
    client = await connect(settings)
    loaded = await client.execute_workflow(
        GoldenIngestionWorkflow.run,
        str(settings.golden_dataset),
        id=GOLDEN_INGESTION,
        task_queue=settings.temporal_task_queue,
    )
    console.print(f"[green]loaded {loaded} golden trios[/] via Temporal")


async def _bootstrap(settings: Settings) -> None:
    await _seed(settings)
    if settings.temporal_address is not None:
        await _load_golden_durably(settings)
    else:
        await _load_golden(settings)


@app.command()
def bootstrap() -> None:
    settings = get_settings()
    alembic_command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    asyncio.run(_bootstrap(settings))
    with langfuse_client(settings) as client:
        if client is not None:
            source = LangfusePromptSource(client, label=settings.prompt_label, cache_ttl_seconds=0)
            published = asyncio.run(source.publish_local(settings.prompt_label))
            console.print(f"prompts: {sum(p.changed for p in published)} published")
