from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue


class TurnKind(StrEnum):
    QUESTION = "question"
    CONFIRMATION = "confirmation"


class TraceStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    started_at: datetime
    duration_ms: int | None
    depth: int
    model: str | None = None
    input: JsonValue = None
    output: JsonValue = None
    level: str | None = None
    status: str | None = None
    tokens: int | None = None


class TraceDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    name: str | None
    started_at: datetime
    user_hash: str | None
    session_id: str | None
    tags: tuple[str, ...]
    question: JsonValue
    answer: JsonValue
    prompts: dict[str, str]
    error_codes: tuple[str, ...]
    fallbacks: tuple[str, ...]
    steps: tuple[TraceStep, ...]
