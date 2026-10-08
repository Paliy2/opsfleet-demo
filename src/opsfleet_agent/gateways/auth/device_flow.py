from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.core.errors import UnauthenticatedError

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_PENDING = "authorization_pending"
_SLOW_DOWN = "slow_down"
_REFRESH_MARGIN = timedelta(seconds=60)


class DeviceCode(BaseModel):
    model_config = ConfigDict(frozen=True)

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int = Field(gt=0)
    interval: int = Field(default=5, gt=0)


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_url: str
    issuer: str
    client_id: str
    access_token: str
    refresh_token: str | None
    expires_at: datetime

    def fresh(self, now: datetime) -> bool:
        return now + _REFRESH_MARGIN < self.expires_at


class _Endpoints(BaseModel):
    device_authorization_endpoint: str
    token_endpoint: str


class DeviceLogin:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_url: str,
        issuer: str,
        client_id: str,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._http = http
        self._sleep = sleep
        self._api_url = api_url
        self._issuer = issuer.rstrip("/")
        self._client_id = client_id

    async def start(self) -> DeviceCode:
        endpoints = await self._endpoints()
        body = await self._post(endpoints.device_authorization_endpoint, {})
        return DeviceCode.model_validate(body)

    async def wait(self, code: DeviceCode) -> Credentials:
        endpoints = await self._endpoints()
        deadline = datetime.now(UTC) + timedelta(seconds=code.expires_in)
        interval = code.interval
        form = {"grant_type": _DEVICE_GRANT, "device_code": code.device_code}
        while datetime.now(UTC) < deadline:
            await self._sleep(interval)
            response = await self._http.post(
                endpoints.token_endpoint, data={**form, "client_id": self._client_id}
            )
            error = _oauth_error(response)
            if error == _SLOW_DOWN:
                interval += 5
            elif error != _PENDING:
                return self._credentials(_token_body(response))
        raise UnauthenticatedError("device code expired before sign-in completed")

    async def refresh(self, credentials: Credentials) -> Credentials:
        if credentials.refresh_token is None:
            raise UnauthenticatedError("no refresh token; sign in again")
        endpoints = await self._endpoints()
        body = await self._post(
            endpoints.token_endpoint,
            {"grant_type": "refresh_token", "refresh_token": credentials.refresh_token},
        )
        return self._credentials(body)

    async def _endpoints(self) -> _Endpoints:
        response = await self._http.get(f"{self._issuer}/.well-known/openid-configuration")
        response.raise_for_status()
        return _Endpoints.model_validate(response.json())

    async def _post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        response = await self._http.post(url, data={**form, "client_id": self._client_id})
        return _token_body(response)

    def _credentials(self, body: dict[str, Any]) -> Credentials:
        return Credentials(
            api_url=self._api_url,
            issuer=self._issuer,
            client_id=self._client_id,
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body["expires_in"])),
        )


def _oauth_error(response: httpx.Response) -> str | None:
    if response.status_code != httpx.codes.BAD_REQUEST:
        return None
    return str(response.json().get("error"))


def _token_body(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        reason = response.json().get("error_description") if response.content else None
        raise UnauthenticatedError(f"identity provider refused: {reason or response.status_code}")
    body: dict[str, Any] = response.json()
    return body


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Credentials:
        if not self._path.exists():
            raise UnauthenticatedError(f"not signed in ({self._path} missing)")
        return Credentials.model_validate_json(self._path.read_text())

    def save(self, credentials: Credentials) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path.touch(mode=0o600, exist_ok=True)
        self._path.write_text(credentials.model_dump_json())

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
