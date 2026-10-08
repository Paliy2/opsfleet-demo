from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResultRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    purpose: str
    columns: tuple[str, ...]


class AnalysisStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    task: str
    results: tuple[ResultRef, ...] = ()


class AnalysisState(BaseModel):
    model_config = ConfigDict(frozen=True)

    steps: tuple[AnalysisStep, ...] = ()


class UserPreferences(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["table", "bullets", "prose"] | None = None
    depth: Literal["brief", "standard", "deep"] | None = None
    visuals: Literal["charts", "text"] | None = None
    language: str | None = Field(default=None, max_length=32)
    notes: tuple[str, ...] = Field(default=(), max_length=5)

    @property
    def is_empty(self) -> bool:
        return self == UserPreferences()


class ConversationContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str = ""
    state: AnalysisState = AnalysisState()
    preferences: UserPreferences = UserPreferences()
