from uuid import UUID

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from opsfleet_agent.agents.capability import TurnContext
from opsfleet_agent.core.errors import (
    MailDeliveryError,
    NotFoundError,
    PiiDetectedError,
    RecipientDeniedError,
)
from opsfleet_agent.entities.conversation import ProgressEvent
from opsfleet_agent.interactors.email_report import EmailReport

_NOT_FOUND = "NOT_FOUND: no saved report of this user has that id. Nothing was sent."
_REFUSALS = {
    NotFoundError: _NOT_FOUND,
    RecipientDeniedError: "RECIPIENT_DENIED: reports can only be emailed to the user's own "
    "registered address, so company data never leaves to other recipients. Nothing was sent. "
    "Offer to send it to the user's own address instead. Do not write any email address "
    "in your answer.",
    PiiDetectedError: "BLOCKED: the report contains personal data and was not sent.",
    MailDeliveryError: "FAILED: the mail service is unavailable. Nothing was sent; suggest "
    "trying again later.",
}


class EmailReportArgs(BaseModel):
    report_id: str = Field(description="A report id returned by list_reports or create_report.")
    to: str | None = Field(
        default=None,
        description="Leave empty: the report goes to the user's own registered address.",
    )


class EmailReportCapability:
    name = "email_report"
    prompt_hint = (
        "- `email_report(report_id, to)`: email one of the user's own saved reports to the "
        'user\'s own registered address. Find the id with `list_reports` first ("the latest" '
        "is the most recently saved). Leave `to` empty; other recipients are refused, so "
        "explain the refusal without writing any email address. Never claim a report was "
        "sent unless the tool returns `SENT`."
    )

    def __init__(self, email_report: EmailReport) -> None:
        self._email_report = email_report

    def tools(self, turn: TurnContext) -> list[BaseTool]:
        async def email_report(report_id: str, to: str | None = None) -> str:
            try:
                report_uuid = UUID(report_id)
            except ValueError:
                return _NOT_FOUND
            try:
                sent = await self._email_report(turn.scope.user_id, report_uuid, to)
            except (NotFoundError, RecipientDeniedError, PiiDetectedError, MailDeliveryError) as e:
                turn.progress.emit(ProgressEvent(stage="email", code=e.code))
                return _REFUSALS[type(e)]
            turn.progress.emit(ProgressEvent(stage="email", detail=sent.title))
            return f"SENT: '{sent.title}' was emailed to the user's registered address."

        return [
            StructuredTool.from_function(
                coroutine=email_report,
                name="email_report",
                description="Email one of the user's own saved reports to the user's own "
                "registered address.",
                args_schema=EmailReportArgs,
            )
        ]
