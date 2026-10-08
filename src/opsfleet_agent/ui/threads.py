from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, override
from uuid import UUID, uuid5

import httpx
from chainlit.config import config
from chainlit.context import ChainlitContextException, context
from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

from opsfleet_agent.core.errors import UnauthenticatedError
from opsfleet_agent.gateways.analyst.client import AnalystApi, ApiError

_CALLER: ContextVar[User | None] = ContextVar("thread_store_caller", default=None)
_PAGE_MAX = 100
_RUN_NAME = "on_message"

type _StepKind = Literal["user_message", "assistant_message", "run"]
_NOT_FOUND = 404
_UNAUTHORIZED = 401


def api_headers(user: User) -> dict[str, str]:
    token = user.metadata.get("access_token")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {"X-Demo-User": user.identifier}


def run_id(trace_id: str) -> str:
    return str(UUID(hex=trace_id))


def trace_of(run: str, live_runs: dict[str, str]) -> str:
    return live_runs.get(run) or UUID(run).hex


def _caller() -> User:
    user = _CALLER.get()
    if user is not None:
        return user
    try:
        session_user = context.session.user
    except ChainlitContextException as error:
        raise UnauthenticatedError("no signed-in user for the thread store") from error
    if session_user is None:
        raise UnauthenticatedError("no signed-in user for the thread store")
    return session_user


def _step(
    thread_id: str,
    message: dict[str, Any],
    *,
    step: str,
    parent: str | None,
    kind: _StepKind,
    name: str,
) -> StepDict:
    return {
        "id": step,
        "threadId": thread_id,
        "parentId": parent,
        "name": name,
        "type": kind,
        "input": "",
        "output": "" if kind == "run" else message["content"],
        "createdAt": message["created_at"],
        "start": message["created_at"],
        "end": message["created_at"],
        "metadata": {},
        "streaming": False,
        "isError": False,
    }


def _steps(thread_id: str, author: str, messages: list[dict[str, Any]]) -> list[StepDict]:
    steps: list[StepDict] = []
    asked: str | None = None
    for message in messages:
        own_id = str(uuid5(UUID(thread_id), str(message["id"])))
        if message["role"] == "user":
            asked = own_id
            steps.append(
                _step(
                    thread_id, message, step=own_id, parent=None, kind="user_message", name=author
                )
            )
            continue
        parent = None
        if message.get("trace_id"):
            parent = run_id(message["trace_id"])
            steps.append(
                _step(thread_id, message, step=parent, parent=asked, kind="run", name=_RUN_NAME)
            )
        steps.append(
            _step(
                thread_id,
                message,
                step=own_id,
                parent=parent,
                kind="assistant_message",
                name=config.ui.name,
            )
        )
    return steps


def _thread(
    thread_id: str, author: str, created_at: str, name: str | None, steps: list[StepDict]
) -> ThreadDict:
    return {
        "id": thread_id,
        "createdAt": created_at,
        "name": name,
        "userId": author,
        "userIdentifier": author,
        "tags": [],
        "metadata": {},
        "steps": steps,
        "elements": [],
    }


class ApiThreadStore(BaseDataLayer):
    def __init__(self, api_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_url = api_url
        self._transport = transport

    @asynccontextmanager
    async def _api(self, user: User) -> AsyncIterator[AnalystApi]:
        async with AnalystApi(self._api_url, api_headers(user), self._transport) as api:
            try:
                yield api
            except ApiError as error:
                if error.status == _UNAUTHORIZED:
                    raise UnauthenticatedError(error.message) from error
                raise

    @override
    async def get_user(self, identifier: str) -> PersistedUser | None:
        return None

    @override
    async def create_user(self, user: User) -> PersistedUser:
        _CALLER.set(user)
        return PersistedUser(
            id=user.identifier,
            identifier=user.identifier,
            display_name=user.display_name,
            metadata=user.metadata,
            createdAt=datetime.now(UTC).isoformat(),
        )

    @override
    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        user = _caller()
        limit = min(pagination.first, _PAGE_MAX)
        async with self._api(user) as api:
            items = await api.sessions(limit, pagination.cursor)
        needle = (filters.search or "").lower()
        threads = [
            _thread(item["id"], user.identifier, item["updated_at"], item["title"], [])
            for item in items
            if needle in item["title"].lower()
        ]
        cursor = items[-1]["updated_at"] if items else None
        return PaginatedResponse(
            pageInfo=PageInfo(hasNextPage=len(items) == limit, startCursor=None, endCursor=cursor),
            data=threads,
        )

    @override
    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        user = _caller()
        try:
            async with self._api(user) as api:
                messages = await api.transcript(thread_id)
        except ApiError as error:
            if error.status == _NOT_FOUND:
                return None
            raise
        steps = _steps(thread_id, user.identifier, messages)
        created = messages[0]["created_at"] if messages else datetime.now(UTC).isoformat()
        title = next((m["content"] for m in messages if m["role"] == "user"), None)
        return _thread(thread_id, user.identifier, created, title, steps)

    @override
    async def get_thread_author(self, thread_id: str) -> str:
        thread = await self.get_thread(thread_id)
        if thread is None:
            return ""
        return thread["userIdentifier"] or ""

    @override
    async def delete_thread(self, thread_id: str) -> None:
        async with self._api(_caller()) as api:
            await api.delete_session(thread_id)

    @override
    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        return None

    @override
    async def upsert_feedback(self, feedback: Feedback) -> str:
        return feedback.forId

    @override
    async def delete_feedback(self, feedback_id: str) -> bool:
        return False

    @override
    async def create_element(self, element: Element) -> None:
        return None

    @override
    async def get_element(self, thread_id: str, element_id: str) -> ElementDict | None:
        return None

    @override
    async def delete_element(self, element_id: str, thread_id: str | None = None) -> None:
        return None

    @override
    async def create_step(self, step_dict: StepDict) -> None:
        return None

    @override
    async def update_step(self, step_dict: StepDict) -> None:
        return None

    @override
    async def delete_step(self, step_id: str) -> None:
        return None

    @override
    async def build_debug_url(self) -> str:
        return ""

    @override
    async def close(self) -> None:
        return None

    @override
    async def get_favorite_steps(self, user_id: str) -> list[StepDict]:
        return []
