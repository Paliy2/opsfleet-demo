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
    def __init__(self, base_url: str, headers: dict[str, str]) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0)

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

    async def _json(self, method: str, path: str) -> dict[str, Any]:
        response = await self._client.request(method, path)
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))
        body: dict[str, Any] = response.json()
        return body

    async def me(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/me")

    async def open_session(self) -> UUID:
        return UUID(str((await self._json("POST", "/v1/sessions"))["session_id"]))

    async def reports(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = (await self._json("GET", "/v1/reports"))["reports"]
        return items

    async def report(self, report_id: UUID) -> dict[str, Any]:
        return await self._json("GET", f"/v1/reports/{report_id}")

    async def chart(self, session_id: UUID, chart_id: str) -> bytes:
        response = await self._client.get(f"/v1/sessions/{session_id}/charts/{chart_id}")
        if response.is_error:
            raise ApiError(response.status_code, _error_body(response, response.content))
        return response.content

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
