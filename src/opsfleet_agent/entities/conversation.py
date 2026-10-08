from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from opsfleet_agent.core.errors import ErrorCode
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.context import AnalysisStep, ConversationContext
from opsfleet_agent.entities.reports import ReportSummary


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str
    created_at: datetime
    id: int = 0
    trace_id: str | None = None


class ChatSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: str


class SessionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ProgressEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    detail: str = ""
    code: ErrorCode | None = None


class AgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: AccessScope
    session_id: UUID
    question: str
    history: tuple[ChatMessage, ...]
    context: ConversationContext = ConversationContext()


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: UUID
    plan_id: UUID
    items: tuple[ReportSummary, ...]
    expires_at: datetime


class AgentReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    turn_id: UUID
    result_ids: tuple[UUID, ...] = ()
    report_ids: tuple[UUID, ...] = ()
    chart_paths: tuple[str, ...] = ()
    chart_ids: tuple[UUID, ...] = ()
    llm_calls: int = 0
    confirmation: ConfirmationRequest | None = None
    analysis: tuple[AnalysisStep, ...] = ()


class TurnOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    reply: AgentReply
    trace_id: str
