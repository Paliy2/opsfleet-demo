import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import httpx

_EVENT = "event: "
_DATA = "data: "
_STREAM_TIMEOUT = httpx.Timeout(10.0, read=600.0)


@dataclass(frozen=True)
class StreamEvent:
    name: str
    data: dict[str, Any]


class ApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("message", f"HTTP {status}"))
        self.status = status
        self.code = str(body.get("code", f"HTTP_{status}"))
        self.message = str(body.get("message", "Request failed."))
        self.trace_id = body.get("trace_id")


def _error_body(response: httpx.Response, raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"code": f"HTTP_{response.status_code}", "message": raw.decode()[:200]}
    return parsed if isinstance(parsed, dict) else {"message": str(parsed)}


class AnalystApi:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def authorize(self, access_token: str) -> None:
        self._client.headers["Authorization"] = f"Bearer {access_token}"

    async def auth_config(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/auth")

    async def _json(
        self, method: str, path: str, body: dict[str, Any] | None = None, **params: Any
    ) -> dict[str, Any]:
        query = {key: value for key, value in params.items() if value is not None}
        response = await self._client.request(method, path, json=body, params=query)
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))
        parsed: dict[str, Any] = response.json()
        return parsed

    async def me(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/me")

    async def open_session(self, session_id: UUID | None = None) -> UUID:
        body = {"session_id": str(session_id)} if session_id is not None else None
        return UUID(str((await self._json("POST", "/v1/sessions", body))["session_id"]))

    async def sessions(self, limit: int, before: str | None = None) -> list[dict[str, Any]]:
        page = await self._json("GET", "/v1/sessions", limit=limit, before=before)
        items: list[dict[str, Any]] = page["sessions"]
        return items

    async def transcript(self, session_id: UUID | str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = (
            await self._json("GET", f"/v1/sessions/{session_id}/messages")
        )["messages"]
        return items

    async def delete_session(self, session_id: UUID | str) -> None:
        response = await self._client.delete(f"/v1/sessions/{session_id}")
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))

    async def reports(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = (await self._json("GET", "/v1/reports"))["reports"]
        return items

    async def report(self, report_id: UUID) -> dict[str, Any]:
        return await self._json("GET", f"/v1/reports/{report_id}")

    async def report_pdf(self, report_id: UUID | str) -> bytes:
        response = await self._client.get(f"/v1/reports/{report_id}/pdf")
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))
        return response.content

    async def chart(self, session_id: UUID, chart_id: str) -> bytes:
        response = await self._client.get(f"/v1/sessions/{session_id}/charts/{chart_id}")
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))
        return response.content

    async def feedback(
        self, session_id: UUID, trace_id: str, rating: str, comment: str | None = None
    ) -> None:
        body = {"trace_id": trace_id, "rating": rating, "comment": comment}
        response = await self._client.post(f"/v1/sessions/{session_id}/feedback", json=body)
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))

    def ask(self, session_id: UUID, question: str) -> AsyncIterator[StreamEvent]:
        return self._stream(f"/v1/sessions/{session_id}/turns", {"question": question})

    def decide(self, session_id: UUID, turn_id: str, approved: bool) -> AsyncIterator[StreamEvent]:
        body = {"turn_id": turn_id, "approved": approved}
        return self._stream(f"/v1/sessions/{session_id}/confirmations", body)

    async def _stream(self, path: str, body: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        async with self._client.stream("POST", path, json=body, timeout=_STREAM_TIMEOUT) as reply:
            if reply.is_error:
                raise ApiError(reply.status_code, _error_body(reply, await reply.aread()))
            name = "message"
            async for line in reply.aiter_lines():
                if line.startswith(_EVENT):
                    name = line.removeprefix(_EVENT)
                elif line.startswith(_DATA):
                    yield StreamEvent(name, json.loads(line.removeprefix(_DATA)))
