from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.reports import Report, ReportSummary


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    issuer: str
    cli_client_id: str


class Me(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    role: Role
    brands: tuple[str, ...]


class SessionCreated(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID


class TurnRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)


class ConfirmationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    approved: bool


class ConfirmationItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime


class ConfirmationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: UUID
    plan_id: UUID
    items: tuple[ConfirmationItem, ...]
    expires_at: datetime


class AnswerEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    text: str
    chart_ids: tuple[UUID, ...]
    trace_id: str
    llm_calls: int


class ErrorEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    trace_id: str | None = None


class ReportItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime


class ReportList(BaseModel):
    model_config = ConfigDict(frozen=True)

    reports: tuple[ReportItem, ...]

    @classmethod
    def of(cls, items: tuple[ReportSummary, ...]) -> "ReportList":
        return cls(reports=tuple(ReportItem.model_validate(item.model_dump()) for item in items))


class ReportView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    body: str
    created_at: datetime

    @classmethod
    def of(cls, report: Report) -> "ReportView":
        return cls(id=report.id, title=report.title, body=report.body, created_at=report.created_at)
