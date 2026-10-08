import importlib

import httpx
import pytest
from fastapi import FastAPI

from opsfleet_agent.core.errors import UnauthenticatedError


def test_ui_module_imports_with_local_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSFLEET_ENV", "local")

    module = importlib.import_module("opsfleet_agent.ui.app")

    assert module.on_open_report is not None
    assert module.chainlit_app.exception_handlers[UnauthenticatedError] is module.signed_out


async def test_an_expired_api_token_signs_the_browser_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSFLEET_ENV", "local")
    module = importlib.import_module("opsfleet_agent.ui.app")
    app = FastAPI()
    app.add_exception_handler(UnauthenticatedError, module.signed_out)

    @app.get("/project/threads")
    async def threads() -> None:
        raise UnauthenticatedError("token expired")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ui") as client:
        client.cookies.set("access_token", "stale")
        response = await client.get("/project/threads")

    assert response.status_code == 401
    assert response.json() == {"detail": "Please sign in again."}
    assert 'access_token=""' in response.headers["set-cookie"]
