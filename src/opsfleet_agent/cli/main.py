import asyncio
from typing import Annotated
from uuid import UUID

import anyio
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.status import Status
from rich.table import Table

from opsfleet_agent.core.config import Environment, Settings, get_settings
from opsfleet_agent.core.errors import AgentError
from opsfleet_agent.entities.conversation import ConfirmationRequest, ProgressEvent, TurnOutcome
from opsfleet_agent.wiring import App, build_app

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

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
        f"results: {len(outcome.reply.result_ids)} · session: {outcome.session_id}[/]"
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
        console.print(f"[yellow]{error.user_message}[/] [dim]({error.code})[/]")
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
