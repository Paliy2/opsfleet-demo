from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from opsfleet_agent.entities.access import Role


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
