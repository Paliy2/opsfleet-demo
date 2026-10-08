from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.reports import ReportSummary


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: MessageRole
    content: str
    created_at: datetime


class ChatSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: str


class ProgressEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    detail: str = ""


class AgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: AccessScope
    session_id: UUID
    question: str
    history: tuple[ChatMessage, ...]


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
    llm_calls: int = 0
    confirmation: ConfirmationRequest | None = None


class TurnOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    reply: AgentReply
