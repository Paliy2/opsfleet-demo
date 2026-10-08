from datetime import datetime
from enum import StrEnum
from math import ceil
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPORT_PAGE_SIZE = 30
FUZZY_MIN_CHARS = 4
MAX_TERMS = 5
DELETION_BATCH = 500
PREVIEW_ITEMS = 20


class ReportSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    title: str
    created_at: datetime


class ReportHit(ReportSummary):
    snippet: str


class SearchTerm(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=100)
    fuzzy: bool = False


class ReportQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    include: tuple[SearchTerm, ...] = ()
    exclude: tuple[SearchTerm, ...] = ()
    created_after: datetime | None = None
    created_before: datetime | None = None
    session_id: UUID | None = None
    report_ids: tuple[UUID, ...] = ()


class ReportMatches(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ReportHit, ...]
    total: int


class ReportPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ReportHit, ...]
    total: int
    page: int
    corrected: tuple[str, ...] = ()

    @property
    def pages(self) -> int:
        return ceil(self.total / REPORT_PAGE_SIZE)


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


class ReportFilter(BaseModel):
    model_config = ConfigDict(frozen=True)

    include: tuple[str, ...] = Field(default=(), max_length=MAX_TERMS)
    exclude: tuple[str, ...] = Field(default=(), max_length=MAX_TERMS)
    created_after: datetime | None = None
    created_before: datetime | None = None
    this_conversation: bool = False
    report_ids: tuple[UUID, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        return (*self.include, *self.exclude)


class DeletionFilter(ReportFilter):
    @model_validator(mode="after")
    def _has_criterion(self) -> Self:
        criteria = (
            self.include,
            self.exclude,
            self.created_after,
            self.created_before,
            self.this_conversation,
            self.report_ids,
        )
        if not any(criteria):
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


class DeletionPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: UUID
    count: int
    matching: int
    preview: tuple[ReportSummary, ...]
    expires_at: datetime

    @classmethod
    def of(cls, plan: DeletionPlan, matching: int) -> Self:
        return cls(
            plan_id=plan.id,
            count=len(plan.items),
            matching=matching,
            preview=plan.items[:PREVIEW_ITEMS],
            expires_at=plan.expires_at,
        )

    @property
    def unlisted(self) -> int:
        return self.count - len(self.preview)

    @property
    def left_over(self) -> int:
        return self.matching - self.count


class DeletionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    deleted: int
    cancelled: bool
