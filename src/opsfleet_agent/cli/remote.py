import asyncio
import webbrowser
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import anyio
import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table

from opsfleet_agent.core.config import get_client_settings
from opsfleet_agent.core.errors import UnauthenticatedError
from opsfleet_agent.gateways.analyst.client import AnalystApi, ApiError, StreamEvent
from opsfleet_agent.gateways.auth.device_flow import Credentials, CredentialStore, DeviceLogin

remote_app = typer.Typer(
    no_args_is_help=True, help="Use a deployed analyst API with single sign-on."
)
console = Console()
_CHARTS_DIR = anyio.Path("var/charts")
_EXIT_COMMANDS = frozenset({"/exit", "/quit"})


def _store() -> CredentialStore:
    return CredentialStore(get_client_settings().credentials_path)


async def _login(api_url: str, open_browser: bool) -> Credentials:
    async with AnalystApi(api_url, {}) as api, httpx.AsyncClient(timeout=30.0) as http:
        config = await api.auth_config()
        flow = DeviceLogin(
            http, api_url=api_url, issuer=config["issuer"], client_id=config["cli_client_id"]
        )
        code = await flow.start()
        link = code.verification_uri_complete or code.verification_uri
        console.print(f"Open [bold]{link}[/] and confirm code [bold green]{code.user_code}[/]")
        if open_browser:
            webbrowser.open(link)
        with console.status("[cyan]Waiting for sign-in…[/]"):
            return await flow.wait(code)


async def _fresh(credentials: Credentials) -> Credentials:
    if credentials.fresh(datetime.now(UTC)):
        return credentials
    async with httpx.AsyncClient(timeout=30.0) as http:
        flow = DeviceLogin(
            http,
            api_url=credentials.api_url,
            issuer=credentials.issuer,
            client_id=credentials.client_id,
        )
        renewed = await flow.refresh(credentials)
    _store().save(renewed)
    return renewed


@remote_app.command()
def login(
    api: Annotated[str | None, typer.Option(help="Analyst API base URL.")] = None,
    browser: Annotated[bool, typer.Option(help="Open the sign-in page.")] = True,
) -> None:
    api_url = (api or get_client_settings().api_url).rstrip("/")
    credentials = asyncio.run(_login(api_url, browser))
    _store().save(credentials)
    console.print(f"[green]Signed in[/] to {api_url}")


@remote_app.command()
def logout() -> None:
    _store().clear()
    console.print("Signed out.")


def _run(work: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(work)
    except UnauthenticatedError as error:
        console.print(f"[yellow]{error.detail}[/] — run [bold]opsfleet remote login[/]")
        raise typer.Exit(1) from error
    except ApiError as error:
        console.print(f"[yellow]{error.message}[/] [dim]({error.code})[/]")
        raise typer.Exit(1) from error


@remote_app.command()
def chat() -> None:
    _run(_chat())


@remote_app.command()
def reports() -> None:
    _run(_reports())


@remote_app.command()
def report(report_id: Annotated[UUID, typer.Argument(help="Report id from 'reports'.")]) -> None:
    _run(_report(report_id))


async def _signed_in() -> AnalystApi:
    credentials = await _fresh(_store().load())
    api = AnalystApi(credentials.api_url, {})
    api.authorize(credentials.access_token)
    return api


async def _reports() -> None:
    async with await _signed_in() as api:
        items = await api.reports()
    if not items:
        console.print("No saved reports.")
        return
    table = Table(title="Your saved reports")
    table.add_column("Id", style="dim")
    table.add_column("Title")
    table.add_column("Created")
    for item in items:
        table.add_row(item["id"], item["title"], item["created_at"][:16])
    console.print(table)


async def _report(report_id: UUID) -> None:
    async with await _signed_in() as api:
        body = await api.report(report_id)
    console.print(f"[dim]saved {body['created_at'][:16]}[/]")
    console.print(Markdown(f"# {body['title']}\n\n{body['body']}"))


async def _chat() -> None:
    credentials = await _fresh(_store().load())
    async with AnalystApi(credentials.api_url, {}) as api:
        api.authorize(credentials.access_token)
        me = await api.me()
        session_id = await api.open_session()
        brands = ", ".join(me["brands"]) or "all brands"
        console.print(f"[bold]OpsFleet analyst[/] — {me['user_id']} ({me['role']}), {brands}")
        while True:
            question = (await anyio.to_thread.run_sync(Prompt.ask, "[bold]you[/]")).strip()
            if question in _EXIT_COMMANDS:
                return
            if not question:
                continue
            credentials = await _fresh(credentials)
            api.authorize(credentials.access_token)
            await _turn(api, session_id, api.ask(session_id, question))


async def _turn(api: AnalystApi, session_id: UUID, events: AsyncIterator[StreamEvent]) -> None:
    try:
        final = await _drain(events)
    except ApiError as error:
        console.print(f"[yellow]{error.message}[/] [dim]({error.code})[/]")
        return
    if final.name == "answer":
        await _answer(api, session_id, final.data)
    elif final.name == "confirmation":
        await _confirm(api, session_id, final.data)
    else:
        console.print(f"[yellow]{final.data.get('message')}[/] [dim]({final.data.get('code')})[/]")


async def _drain(events: AsyncIterator[StreamEvent]) -> StreamEvent:
    with console.status("[cyan]Working…[/]") as status:
        async for event in events:
            if event.name != "progress":
                return event
            status.update(f"[cyan]{event.data.get('stage')}[/]… {event.data.get('detail', '')}")
    raise ApiError(502, {"code": "E_STREAM", "message": "The answer stream ended early."})


async def _answer(api: AnalystApi, session_id: UUID, data: dict[str, Any]) -> None:
    console.print(Markdown(data["text"]))
    await _CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    for chart_id in data.get("chart_ids", []):
        path = _CHARTS_DIR / f"{chart_id}.png"
        await path.write_bytes(await api.chart(session_id, chart_id))
        console.print(f"[magenta]chart:[/] {path}")
    console.print(f"[dim]llm calls: {data['llm_calls']} · trace: {data['trace_id']}[/]")


async def _confirm(api: AnalystApi, session_id: UUID, data: dict[str, Any]) -> None:
    table = Table(title="Reports to delete permanently", title_style="bold red")
    table.add_column("Title")
    table.add_column("Created")
    for item in data["items"]:
        table.add_row(item["title"], item["created_at"][:16])
    console.print(table)
    answer = await anyio.to_thread.run_sync(
        Prompt.ask, f"[bold red]Delete these {len(data['items'])} report(s)? Type 'delete'"
    )
    approved = answer.strip().lower() == "delete"
    await _turn(api, session_id, api.decide(session_id, str(data["turn_id"]), approved))
