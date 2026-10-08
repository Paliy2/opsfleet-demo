from uuid import UUID

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from opsfleet_agent.agents.capability import TurnContext
from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.entities.reports import ReportSummary
from opsfleet_agent.interactors.reports import GetReport, ListReports


class ListReportsArgs(BaseModel):
    text_contains: str | None = Field(default=None, description="Optional text to search for.")


class OpenReportArgs(BaseModel):
    report_id: str = Field(description="A report id returned by list_reports.")


def _report_lines(items: tuple[ReportSummary, ...]) -> str:
    return "\n".join(
        f"- {item.id} | {item.title} | {item.created_at:%Y-%m-%d %H:%M}" for item in items
    )


class ReportLibraryCapability:
    name = "report_library"
    prompt_hint = (
        "- `list_reports(text_contains)`: list the user's own saved reports.\n"
        '- `open_report(report_id)`: show a saved report in full ("open my Q3 report"). '
        "Find the id with `list_reports` first; show the returned text as is. Report "
        "`NOT_FOUND` honestly."
    )

    def __init__(self, list_reports: ListReports, get_report: GetReport) -> None:
        self._list_reports = list_reports
        self._get_report = get_report

    def tools(self, turn: TurnContext) -> list[BaseTool]:
        async def list_reports(text_contains: str | None = None) -> str:
            items = await self._list_reports(turn.scope.user_id, text_contains)
            return _report_lines(items) if items else "No saved reports match."

        async def open_report(report_id: str) -> str:
            try:
                report = await self._get_report(turn.scope.user_id, UUID(report_id))
            except (ValueError, NotFoundError):
                return f"NOT_FOUND: no saved report of this user has id {report_id}."
            turn.progress.emit(ProgressEvent(stage="library", detail=report.title))
            return f"# {report.title}\nSaved {report.created_at:%Y-%m-%d %H:%M}\n\n{report.body}"

        return [
            StructuredTool.from_function(
                coroutine=list_reports,
                name="list_reports",
                description="List the user's own saved reports.",
                args_schema=ListReportsArgs,
            ),
            StructuredTool.from_function(
                coroutine=open_report,
                name="open_report",
                description="Open one of the user's own saved reports by id and return its "
                "full text.",
                args_schema=OpenReportArgs,
            ),
        ]
