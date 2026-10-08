from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import chainlit as cl
from chainlit.auth.cookie import clear_auth_cookie
from chainlit.data.base import BaseDataLayer
from chainlit.element import Element
from chainlit.input_widget import InputWidget, Switch
from chainlit.server import app as chainlit_app
from chainlit.types import CommandDict, Feedback, ThreadDict
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from opsfleet_agent.core.config import Environment, get_client_settings
from opsfleet_agent.core.errors import ErrorCode, UnauthenticatedError
from opsfleet_agent.gateways.analyst.client import AnalystApi, ApiError, StreamEvent
from opsfleet_agent.ui.threads import ApiThreadStore, api_headers, trace_of

_SETTINGS = get_client_settings()
_DEBUG = "debug"
_REPORTS = "Reports"
_OPEN_REPORT = "open_report"
_RUNS = "runs"
_LISTED = "listed"
_SIGNED_OUT = ErrorCode.UNAUTHENTICATED.value
_LIBRARY_LIMIT = 10
_LABEL_CHARS = 40
_REPORTS_COMMAND: CommandDict = {
    "id": _REPORTS,
    "description": "Browse and download your saved reports",
    "icon": "file-text",
    "button": True,
    "persistent": False,
    "selected": False,
}
_DEMO_USERS = frozenset({"ceo", "mgr_a", "mgr_b", "mgr_empty"})
_STAGES = {
    "privacy": "Removed personal data",
    "guard": "Checking the request",
    "memory": "Summarizing earlier turns",
    "thinking": "Planning",
    "analysis": "Analyzing",
    "examples": "Using validated examples",
    "query": "Querying BigQuery",
    "retry": "Self-correcting a query",
    "schema": "Reading the schema",
    "chart": "Charting",
    "report": "Writing the report",
    "library": "Opening a saved report",
    "confirm": "Preparing deletion preview",
}
_STARTERS = (
    ("Brand revenue", "Which of my brands grew revenue the most this quarter vs last quarter?"),
    ("Top customers", "Who are our top 10 customers by lifetime spend, and where are they?"),
    ("Return rates", "Which product categories have the highest return rate this year?"),
    ("What data?", "What data can you analyze for me?"),
)


if _SETTINGS.env is Environment.PRODUCTION:

    @cl.oauth_callback
    async def oauth_callback(
        provider_id: str,
        token: str,
        raw_user_data: dict[str, str],
        _default_user: cl.User,
        _id_token: str | None = None,
    ) -> cl.User | None:
        username = raw_user_data.get("preferred_username")
        if provider_id != "keycloak" or not username:
            return None
        return cl.User(
            identifier=username, metadata={"access_token": token, "provider": provider_id}
        )

else:

    @cl.password_auth_callback
    async def local_login(username: str, _password: str) -> cl.User | None:
        if username not in _DEMO_USERS:
            return None
        return cl.User(identifier=username, metadata={"provider": "local"})


async def signed_out(request: Request, _error: Exception) -> Response:
    response = JSONResponse(status_code=401, content={"detail": UnauthenticatedError.user_message})
    clear_auth_cookie(request, response)
    return response


chainlit_app.add_exception_handler(UnauthenticatedError, signed_out)


@cl.data_layer
def thread_store() -> BaseDataLayer:
    return ApiThreadStore(_SETTINGS.api_url)


def _api() -> AnalystApi:
    api: AnalystApi = cl.user_session.get("api")
    return api


@cl.set_starters
async def starters(_user: cl.User | None = None, _language: str | None = None) -> list[cl.Starter]:
    return [cl.Starter(label=label, message=message) for label, message in _STARTERS]


async def _connect(thread_id: str) -> dict[str, Any] | None:
    widgets: list[InputWidget] = [
        Switch(id=_DEBUG, label="Debug: show LLM calls and trace link", initial=False)
    ]
    await cl.ChatSettings(widgets).send()
    await cl.context.emitter.set_commands([_REPORTS_COMMAND])
    api = AnalystApi(_SETTINGS.api_url, api_headers(cl.user_session.get("user")))
    cl.user_session.set("api", api)
    try:
        me = await api.me()
        cl.user_session.set("session_id", await api.open_session(UUID(thread_id)))
    except ApiError as error:
        await _show_error(error.code, error.message)
        return None
    return me


@cl.on_chat_start
async def start() -> None:
    me = await _connect(cl.context.session.thread_id)
    if me is None:
        return
    brands = ", ".join(me["brands"]) or "all brands"
    await cl.Message(
        content=f"Signed in as **{me['user_id']}** ({me['role']}) — scope: {brands}."
    ).send()


@cl.on_chat_resume
async def resume(thread: ThreadDict) -> None:
    cl.user_session.set(_LISTED, True)
    await _connect(thread["id"])


@cl.on_settings_update
async def settings_update(settings: dict[str, Any]) -> None:
    cl.user_session.set(_DEBUG, bool(settings.get(_DEBUG)))


@cl.on_chat_end
async def end() -> None:
    api: AnalystApi | None = cl.user_session.get("api")
    if api is not None:
        await api.close()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    session_id: UUID | None = cl.user_session.get("session_id")
    if session_id is None:
        await cl.Message(content="The session could not be started. Refresh the page.").send()
        return
    if message.command == _REPORTS or message.content.strip().lower() == "/reports":
        try:
            await _show_library()
        except ApiError as error:
            await _show_error(error.code, error.message)
        return
    await _consume(session_id, _api().ask(session_id, message.content))


async def _show_library() -> None:
    reports = await _api().reports()
    if not reports:
        await cl.Message(content="You have no saved reports yet. Ask me to write one.").send()
        return
    shown = reports[:_LIBRARY_LIMIT]
    lines = "\n".join(f"- **{item['title']}** · {item['created_at'][:16]}" for item in shown)
    actions = [
        cl.Action(
            name=_OPEN_REPORT,
            payload={"report_id": item["id"]},
            label=_short(item["title"]),
            tooltip=f"Open {item['title']}",
        )
        for item in shown
    ]
    await cl.Message(content=f"**Your reports** ({len(reports)})\n{lines}", actions=actions).send()


def _short(title: str) -> str:
    return title if len(title) <= _LABEL_CHARS else f"{title[: _LABEL_CHARS - 1]}…"


@cl.action_callback(_OPEN_REPORT)
async def on_open_report(action: cl.Action) -> None:
    try:
        report = await _api().report(UUID(action.payload["report_id"]))
        elements = await _report_files(report)
    except ApiError as error:
        await _show_error(error.code, error.message)
        return
    await cl.Message(content=report["body"], elements=elements).send()


async def _report_files(report: dict[str, Any]) -> list[Element]:
    pdf = await _api().report_pdf(report["id"])
    name = f"{report['title']}.pdf"
    return [
        cl.Pdf(name=name, content=pdf, display="side", mime="application/pdf"),
        cl.File(name=name, content=pdf, display="inline", mime="application/pdf"),
    ]


async def _consume(session_id: UUID, events: AsyncIterator[StreamEvent]) -> None:
    async with cl.Step(name="analysis", type="run") as step:
        final: StreamEvent | None = None
        steps = 0
        try:
            async for event in events:
                if event.name == "progress":
                    steps += 1
                    await _progress(step, event.data)
                else:
                    final = event
        except ApiError as error:
            final = StreamEvent("error", {"code": error.code, "message": error.message})
        step.name = f"{steps} analysis steps" if steps != 1 else "1 analysis step"
        if final is not None and cl.user_session.get(_DEBUG):
            await step.stream_token(_debug_line(final.data))
    if final is not None:
        await _finish(session_id, final)


def _debug_line(data: dict[str, Any]) -> str:
    facts = [f"LLM calls: {data['llm_calls']}"] if "llm_calls" in data else []
    trace_id = data.get("trace_id")
    if trace_id and _SETTINGS.trace_url:
        facts.append(f"[trace {trace_id}]({_SETTINGS.trace_url.format(trace_id=trace_id)})")
    elif trace_id:
        facts.append(f"trace `{trace_id}`")
    return f"\n---\n_{' · '.join(facts)}_\n" if facts else ""


async def _progress(step: cl.Step, data: dict[str, Any]) -> None:
    label = _STAGES.get(str(data.get("stage")), str(data.get("stage")))
    detail = f": {data['detail']}" if data.get("detail") else ""
    await step.stream_token(f"- {label}{detail}\n")


async def _finish(session_id: UUID, event: StreamEvent) -> None:
    if event.name == "answer":
        await _answer(session_id, event.data)
        await _list_in_sidebar(session_id)
    elif event.name == "confirmation":
        await _confirm(session_id, event.data)
    else:
        await _show_error(str(event.data.get("code")), str(event.data.get("message")))


async def _show_error(code: str, message: str) -> None:
    if code == _SIGNED_OUT:
        sign_in = cl.CustomElement(name="SignIn", display="inline")
        await cl.Message(content="", elements=[sign_in]).send()
        return
    await cl.Message(content=f"⚠️ {message} `{code}`").send()


async def _answer(session_id: UUID, data: dict[str, Any]) -> None:
    images = [
        cl.Image(
            name=f"chart-{index + 1}",
            content=await _api().chart(session_id, chart_id),
            display="inline",
        )
        for index, chart_id in enumerate(data.get("chart_ids", []))
    ]
    files = [
        element
        for report_id in data.get("report_ids", [])
        for element in await _report_files(await _api().report(UUID(report_id)))
    ]
    elements: list[Element] = [*images, *files]
    await cl.Message(content=data["text"], elements=elements).send()
    run = cl.context.current_run
    if run is not None:
        runs: dict[str, str] = cl.user_session.get(_RUNS) or {}
        runs[run.id] = str(data["trace_id"])
        cl.user_session.set(_RUNS, runs)


async def _list_in_sidebar(session_id: UUID) -> None:
    if cl.user_session.get(_LISTED):
        return
    cl.user_session.set(_LISTED, True)
    await cl.context.emitter.emit(
        "first_interaction", {"interaction": "saved", "thread_id": str(session_id)}
    )


@cl.on_feedback
async def on_feedback(feedback: Feedback) -> None:
    session_id: UUID | None = cl.user_session.get("session_id")
    if session_id is None:
        return
    trace_id = trace_of(feedback.forId, cl.user_session.get(_RUNS) or {})
    rating = "up" if feedback.value == 1 else "down"
    try:
        await _api().feedback(session_id, trace_id, rating, feedback.comment)
    except ApiError as error:
        await _show_error(error.code, error.message)


async def _confirm(session_id: UUID, data: dict[str, Any]) -> None:
    titles = "\n".join(f"- {item['title']} ({item['created_at'][:16]})" for item in data["items"])
    choice = await cl.AskActionMessage(
        content=f"**Permanently delete {len(data['items'])} report(s)?**\n{titles}",
        actions=[
            cl.Action(name="delete", payload={"approved": True}, label="Delete"),
            cl.Action(name="keep", payload={"approved": False}, label="Cancel"),
        ],
        timeout=300,
    ).send()
    approved = bool(choice and choice.get("payload", {}).get("approved"))
    await _consume(session_id, _api().decide(session_id, str(data["turn_id"]), approved))
