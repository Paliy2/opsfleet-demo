from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opsfleet_agent.api.app import Services, _agent_error, _routes
from opsfleet_agent.core.config import Environment
from opsfleet_agent.core.errors import AgentError
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.gateways.ratelimit.redis_limiter import UnlimitedRateLimiter
from opsfleet_agent.interactors.reports import GetReport, ListReports
from opsfleet_agent.wiring import App
from tests.fakes import FakeReports, StoredReport

SESSION = uuid4()


class OpenAccess:
    async def scope(self, user_id: str) -> AccessScope:
        return AccessScope(user_id=user_id, role=Role.MANAGER, brands=frozenset({"Diesel"}))


@dataclass(frozen=True)
class ReportApp:
    chat: OpenAccess
    list_reports: ListReports
    get_report: GetReport


@pytest.fixture
def reports() -> FakeReports:
    return FakeReports(
        rows=[
            StoredReport("mgr_a", SESSION, "Q3 review", "Revenue grew."),
            StoredReport("mgr_b", SESSION, "Their plan", "confidential"),
        ]
    )


@pytest.fixture
def client(reports: FakeReports) -> TestClient:
    api = FastAPI()
    api.add_exception_handler(AgentError, _agent_error)
    _routes(api)
    stub = ReportApp(OpenAccess(), ListReports(reports), GetReport(reports))
    api.state.services = Services(
        app=cast(App, stub),
        verifier=None,
        limiter=UnlimitedRateLimiter(),
        environment=Environment.LOCAL,
        auth=None,
    )
    return TestClient(api)


def as_user(user_id: str) -> dict[str, str]:
    return {"X-Demo-User": user_id}


def test_list_shows_only_own_reports(client: TestClient) -> None:
    response = client.get("/v1/reports", headers=as_user("mgr_a"))

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["reports"]] == ["Q3 review"]


def test_owner_reads_full_report(client: TestClient, reports: FakeReports) -> None:
    mine = reports.rows[0]

    response = client.get(f"/v1/reports/{mine.id}", headers=as_user("mgr_a"))

    assert response.status_code == 200
    assert response.json()["body"] == "Revenue grew."
    assert "owner_id" not in response.json()


def test_other_user_gets_not_found(client: TestClient, reports: FakeReports) -> None:
    theirs = reports.rows[1]

    response = client.get(f"/v1/reports/{theirs.id}", headers=as_user("mgr_a"))

    assert response.status_code == 404
    assert "confidential" not in response.text
    assert "Their plan" not in response.text


def test_reports_require_authentication(client: TestClient) -> None:
    assert client.get("/v1/reports").status_code == 401
