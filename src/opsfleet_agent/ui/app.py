from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import chainlit as cl
from chainlit.input_widget import InputWidget, Switch

from opsfleet_agent.core.config import Environment, get_client_settings
from opsfleet_agent.gateways.analyst.client import AnalystApi, ApiError, StreamEvent

_SETTINGS = get_client_settings()
_DEBUG = "debug"
_DEMO_USERS = frozenset({"ceo", "mgr_a", "mgr_b", "mgr_empty"})
_STAGES = {
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
    "confirm": "Preparing deletion preview",
}
_STARTERS = (
    ("Brand revenue", "Which of my brands grew revenue the most this quarter vs last quarter?"),
    ("Top customers", "Who are our top 10 customers by lifetime spend, and where are they?"),
    ("Return rates", "Which product categories have the highest return rate this year?"),
    ("What data?", "What data can you analyze for me?"),
)


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
    return cl.User(identifier=username, metadata={"access_token": token, "provider": provider_id})


if _SETTINGS.env is Environment.LOCAL:

    @cl.password_auth_callback
    async def local_login(username: str, _password: str) -> cl.User | None:
        if username not in _DEMO_USERS:
            return None
        return cl.User(identifier=username, metadata={"provider": "local"})


def _api() -> AnalystApi:
    api: AnalystApi = cl.user_session.get("api")
    return api


def _headers(user: cl.User) -> dict[str, str]:
    token = user.metadata.get("access_token")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {"X-Demo-User": user.identifier}


@cl.set_starters
async def starters(_user: cl.User | None = None, _language: str | None = None) -> list[cl.Starter]:
    return [cl.Starter(label=label, message=message) for label, message in _STARTERS]


@cl.on_chat_start
async def start() -> None:
    widgets: list[InputWidget] = [
        Switch(id=_DEBUG, label="Debug: show LLM calls and trace link", initial=False)
    ]
    await cl.ChatSettings(widgets).send()
    user = cl.user_session.get("user")
    api = AnalystApi(_SETTINGS.api_url, _headers(user))
    cl.user_session.set("api", api)
    try:
        me = await api.me()
        cl.user_session.set("session_id", await api.open_session())
    except ApiError as error:
        await cl.Message(content=f"**{error.message}** `{error.code}`").send()
        return
    brands = ", ".join(me["brands"]) or "all brands"
    await cl.Message(
        content=f"Signed in as **{me['user_id']}** ({me['role']}) — scope: {brands}."
    ).send()


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
    await _consume(session_id, _api().ask(session_id, message.content))


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
    elif event.name == "confirmation":
        await _confirm(session_id, event.data)
    else:
        await cl.Message(content=f"⚠️ {event.data.get('message')} `{event.data.get('code')}`").send()


async def _answer(session_id: UUID, data: dict[str, Any]) -> None:
    images = [
        cl.Image(
            name=f"chart-{index + 1}",
            content=await _api().chart(session_id, chart_id),
            display="inline",
        )
        for index, chart_id in enumerate(data.get("chart_ids", []))
    ]
    await cl.Message(content=data["text"], elements=images).send()


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
