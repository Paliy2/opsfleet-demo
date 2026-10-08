from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from opsfleet_agent.entities.query import QueryResult
from opsfleet_agent.entities.reports import (
    Chart,
    ChartSpec,
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportSummary,
)


class ReportRepo(Protocol):
    async def save(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report: ...

    async def search(
        self,
        owner_id: str,
        text_contains: str | None = None,
        session_id: UUID | None = None,
        report_ids: Sequence[UUID] = (),
    ) -> tuple[ReportSummary, ...]: ...

    async def delete_owned(self, owner_id: str, report_ids: Sequence[UUID]) -> int: ...


class DeletionPlanRepo(Protocol):
    async def get_or_create(
        self, key: str, owner_id: str, items: Sequence[ReportSummary], expires_at: datetime
    ) -> DeletionPlan: ...

    async def get(self, plan_id: UUID, owner_id: str) -> DeletionPlan: ...

    async def close(self, plan_id: UUID, status: DeletionStatus, deleted: int) -> None: ...

    async def expire_pending(self, now: datetime) -> int:
        """Mark pending plans past their expiry as expired; return how many changed."""


class ChartRenderer(Protocol):
    def render(self, spec: ChartSpec, result: QueryResult) -> bytes: ...


class ChartRepo(Protocol):
    async def save(self, session_id: UUID, title: str, png: bytes) -> Chart: ...

    async def get(self, chart_id: UUID, session_id: UUID) -> Chart: ...

    async def read(self, chart_id: UUID, session_id: UUID) -> bytes: ...
