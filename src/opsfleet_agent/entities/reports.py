from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class ReportSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    owner_id: str
    session_id: UUID | None
    title: str
    body: str
    chart_paths: tuple[str, ...]
    created_at: datetime


class Chart(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    path: str


class ChartKind(StrEnum):
    BAR = "bar"
    LINE = "line"
    HORIZONTAL_BAR = "horizontal_bar"


class ChartSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ChartKind
    title: str
    x: str
    y: tuple[str, ...]


class DeletionFilter(BaseModel):
    model_config = ConfigDict(frozen=True)

    text_contains: str | None = None
    this_conversation: bool = False
    report_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def _has_criterion(self) -> Self:
        if not (self.text_contains or self.this_conversation or self.report_ids):
            raise ValueError("deletion needs at least one criterion")
        return self


class DeletionStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DeletionPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    owner_id: str
    items: tuple[ReportSummary, ...]
    status: DeletionStatus
    expires_at: datetime


class DeletionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    deleted: int
    cancelled: bool
