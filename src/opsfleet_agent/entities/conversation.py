from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from opsfleet_agent.entities.access import AccessScope


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


class AgentReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    result_ids: tuple[UUID, ...] = ()
    llm_calls: int = 0


class TurnOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    reply: AgentReply
