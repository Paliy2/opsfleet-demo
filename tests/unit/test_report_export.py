from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.reports import Report
from opsfleet_agent.interactors.reports import ExportReport, GetReport
from tests.fakes import FakeReports, StoredReport

ROW = StoredReport(
    owner_id="mgr_a",
    session_id=uuid4(),
    title="Q3 review",
    body="## Summary",
    chart_paths=("charts/a.png", "charts/gone.png", "charts/b.png"),
)


class Blobs:
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        return key

    async def get(self, location: str) -> bytes:
        if "gone" in location:
            raise NotFoundError(location)
        return location.encode()


@dataclass
class Renderer:
    calls: list[tuple[tuple[bytes, ...], int]] = field(default_factory=list)

    async def render(self, report: Report, charts: tuple[bytes, ...], missing_charts: int) -> bytes:
        self.calls.append((charts, missing_charts))
        return b"%PDF"


def export(renderer: Renderer) -> ExportReport:
    return ExportReport(GetReport(FakeReports(rows=[ROW])), Blobs(), renderer)


async def test_owner_gets_pdf_with_available_charts_in_order() -> None:
    renderer = Renderer()

    report, pdf = await export(renderer)("mgr_a", ROW.id)

    assert (report.title, pdf) == ("Q3 review", b"%PDF")
    assert renderer.calls == [((b"charts/a.png", b"charts/b.png"), 1)]


async def test_other_users_cannot_export() -> None:
    with pytest.raises(NotFoundError):
        await export(Renderer())("mgr_b", ROW.id)
