import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from opsfleet_agent.core.errors import UnauthenticatedError
from opsfleet_agent.gateways.auth.device_flow import (
    Credentials,
    CredentialStore,
    DeviceCode,
    DeviceLogin,
)

ISSUER = "https://auth.example/realms/opsfleet"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
DEVICE_URL = f"{ISSUER}/protocol/openid-connect/auth/device"
CODE = DeviceCode(
    device_code="dev-1", user_code="ABCD-EFGH", verification_uri="https://v", expires_in=600
)
TOKENS = {"access_token": "at", "refresh_token": "rt", "expires_in": 300}


class Provider:
    def __init__(self, token_replies: list[tuple[int, dict[str, object]]]) -> None:
        self.token_replies = token_replies
        self.token_forms: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"device_authorization_endpoint": DEVICE_URL, "token_endpoint": TOKEN_URL},
            )
        if url == DEVICE_URL:
            return httpx.Response(200, json=CODE.model_dump())
        self.token_forms.append(dict(httpx.QueryParams(request.content.decode())))
        status, body = self.token_replies.pop(0)
        return httpx.Response(status, json=body)


def login(provider: Provider, slept: list[float]) -> DeviceLogin:
    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    return DeviceLogin(http, api_url="https://api", issuer=ISSUER, client_id="cli", sleep=sleep)


async def test_wait_polls_through_pending_and_slow_down() -> None:
    provider = Provider(
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, TOKENS),
        ]
    )
    slept: list[float] = []

    credentials = await login(provider, slept).wait(await login(provider, []).start())

    assert slept == [5, 5, 10]
    assert credentials.access_token == "at"
    assert credentials.issuer == ISSUER
    assert provider.token_forms[0]["device_code"] == "dev-1"
    assert provider.token_forms[0]["client_id"] == "cli"


async def test_wait_fails_fast_when_user_denies() -> None:
    provider = Provider([(400, {"error": "access_denied", "error_description": "denied"})])

    with pytest.raises(UnauthenticatedError, match="denied"):
        await login(provider, []).wait(CODE)


async def test_refresh_exchanges_refresh_token() -> None:
    provider = Provider([(200, {**TOKENS, "access_token": "renewed"})])
    stale = credentials(expires_in=timedelta(seconds=-1))

    renewed = await login(provider, []).refresh(stale)

    assert renewed.access_token == "renewed"
    assert provider.token_forms[0]["grant_type"] == "refresh_token"


def credentials(expires_in: timedelta) -> Credentials:
    return Credentials(
        api_url="https://api",
        issuer=ISSUER,
        client_id="cli",
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(UTC) + expires_in,
    )


def test_credentials_refresh_a_minute_before_expiry() -> None:
    now = datetime.now(UTC)

    assert credentials(timedelta(minutes=5)).fresh(now)
    assert not credentials(timedelta(seconds=30)).fresh(now)


def test_store_round_trips_with_owner_only_permissions(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "opsfleet" / "credentials.json")
    saved = credentials(timedelta(minutes=5))

    store.save(saved)

    path = tmp_path / "opsfleet" / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load() == saved
    assert json.loads(path.read_text())["refresh_token"] == "rt"
    store.clear()
    with pytest.raises(UnauthenticatedError):
        store.load()
