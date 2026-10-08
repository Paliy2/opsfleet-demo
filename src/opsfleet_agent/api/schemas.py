import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.entities.access import Role
from opsfleet_agent.entities.conversation import ChatMessage, MessageRole, SessionSummary
from opsfleet_agent.entities.feedback import (
    FEEDBACK_COMMENT_MAX_CHARS,
    TRACE_ID_MAX_CHARS,
    Rating,
)
from opsfleet_agent.entities.reports import Report, ReportPage


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


class OpenSessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID | None = None


class SessionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class SessionList(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions: tuple[SessionItem, ...]

    @classmethod
    def of(cls, items: tuple[SessionSummary, ...]) -> "SessionList":
        return cls(sessions=tuple(SessionItem.model_validate(item.model_dump()) for item in items))


class MessageItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    role: MessageRole
    content: str
    created_at: datetime
    trace_id: str | None


class Transcript(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    messages: tuple[MessageItem, ...]

    @classmethod
    def of(cls, session_id: UUID, messages: tuple[ChatMessage, ...]) -> "Transcript":
        return cls(
            session_id=session_id,
            messages=tuple(MessageItem.model_validate(item.model_dump()) for item in messages),
        )


class TurnRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)


class ConfirmationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    approved: bool


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str = Field(min_length=1, max_length=TRACE_ID_MAX_CHARS)
    rating: Rating
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)


class ConfirmationItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime


class ConfirmationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: UUID
    plan_id: UUID
    count: int
    matching: int
    preview: tuple[ConfirmationItem, ...]
    expires_at: datetime


class AnswerEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    text: str
    chart_ids: tuple[UUID, ...]
    report_ids: tuple[UUID, ...] = ()
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
    total: int
    page: int
    pages: int

    @classmethod
    def of(cls, page: ReportPage) -> "ReportList":
        return cls(
            reports=tuple(
                ReportItem(id=item.id, title=item.title, created_at=item.created_at)
                for item in page.items
            ),
            total=page.total,
            page=page.page,
            pages=page.pages,
        )


class ReportView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    body: str
    created_at: datetime

    @classmethod
    def of(cls, report: Report) -> "ReportView":
        return cls(id=report.id, title=report.title, body=report.body, created_at=report.created_at)


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_FILENAME_MAX = 80


def report_filename(title: str) -> str:
    stem = _UNSAFE_FILENAME.sub("-", title).strip("-")[:_FILENAME_MAX] or "report"
    return f"{stem}.pdf"
