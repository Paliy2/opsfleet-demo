from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.reports import (
    Chart,
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportSummary,
)
from opsfleet_agent.models.tables import ChartRow, DeletionPlanRow, ReportRow
from opsfleet_agent.ports.blobs import BlobStore
from opsfleet_agent.ports.reports import ChartRepo, DeletionPlanRepo, ReportRepo


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlReportRepo(ReportRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report:
        async with self._sessions.begin() as session:
            row = ReportRow(
                owner_id=owner_id,
                session_id=session_id,
                title=title,
                body=body,
                chart_paths=list(chart_paths),
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _to_report(row)

    async def search(
        self,
        owner_id: str,
        text_contains: str | None = None,
        session_id: UUID | None = None,
        report_ids: Sequence[UUID] = (),
    ) -> tuple[ReportSummary, ...]:
        query = select(ReportRow).where(ReportRow.owner_id == owner_id)
        if text_contains:
            pattern = f"%{_escape_like(text_contains)}%"
            query = query.where(
                or_(
                    ReportRow.title.ilike(pattern, escape="\\"),
                    ReportRow.body.ilike(pattern, escape="\\"),
                )
            )
        if session_id is not None:
            query = query.where(ReportRow.session_id == session_id)
        if report_ids:
            query = query.where(ReportRow.id.in_(report_ids))
        async with self._sessions() as session:
            rows = await session.scalars(query.order_by(ReportRow.created_at.desc()))
            return tuple(
                ReportSummary(id=row.id, title=row.title, created_at=row.created_at) for row in rows
            )

    async def get_owned(self, owner_id: str, report_id: UUID) -> Report:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ReportRow).where(ReportRow.id == report_id, ReportRow.owner_id == owner_id)
            )
            if row is None:
                raise NotFoundError(f"report {report_id} not found for this user")
            return _to_report(row)

    async def delete_owned(self, owner_id: str, report_ids: Sequence[UUID]) -> int:
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(ReportRow)
                .where(ReportRow.owner_id == owner_id, ReportRow.id.in_(report_ids))
                .returning(ReportRow.id)
            )
            return len(result.all())


def _to_report(row: ReportRow) -> Report:
    return Report(
        id=row.id,
        owner_id=row.owner_id,
        session_id=row.session_id,
        title=row.title,
        body=row.body,
        chart_paths=tuple(row.chart_paths),
        created_at=row.created_at,
    )


class SqlDeletionPlanRepo(DeletionPlanRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_or_create(
        self, key: str, owner_id: str, items: Sequence[ReportSummary], expires_at: datetime
    ) -> DeletionPlan:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(DeletionPlanRow).where(
                    DeletionPlanRow.key == key, DeletionPlanRow.owner_id == owner_id
                )
            )
            if existing is not None:
                return _to_plan(existing)
            row = DeletionPlanRow(
                key=key,
                owner_id=owner_id,
                items=[item.model_dump(mode="json") for item in items],
                status=DeletionStatus.PENDING.value,
                expires_at=expires_at.astimezone(UTC).replace(tzinfo=None),
            )
            session.add(row)
            await session.flush()
            return _to_plan(row)

    async def get(self, plan_id: UUID, owner_id: str) -> DeletionPlan:
        async with self._sessions() as session:
            row = await session.get(DeletionPlanRow, plan_id)
            if row is None or row.owner_id != owner_id:
                raise NotFoundError(f"deletion plan {plan_id} not found")
            return _to_plan(row)

    async def close(self, plan_id: UUID, status: DeletionStatus, deleted: int) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(DeletionPlanRow, plan_id)
            if row is None:
                raise NotFoundError(f"deletion plan {plan_id} not found")
            row.status = status.value
            row.deleted_count = deleted
            row.closed_at = func.now()
            row.items = [{"id": item["id"]} for item in row.items]

    async def expire_pending(self, now: datetime) -> int:
        cutoff = now.astimezone(UTC).replace(tzinfo=None)
        statement = (
            update(DeletionPlanRow)
            .where(
                DeletionPlanRow.status == DeletionStatus.PENDING.value,
                DeletionPlanRow.expires_at < cutoff,
            )
            .values(status=DeletionStatus.EXPIRED.value, closed_at=cutoff)
            .returning(DeletionPlanRow.id)
        )
        async with self._sessions.begin() as session:
            return len((await session.execute(statement)).all())


def _to_plan(row: DeletionPlanRow) -> DeletionPlan:
    return DeletionPlan(
        id=row.id,
        owner_id=row.owner_id,
        items=tuple(ReportSummary.model_validate(item) for item in row.items if "title" in item),
        status=DeletionStatus(row.status),
        expires_at=row.expires_at.replace(tzinfo=UTC),
    )


class SqlChartRepo(ChartRepo):
    def __init__(self, sessions: async_sessionmaker[AsyncSession], blobs: BlobStore) -> None:
        self._sessions = sessions
        self._blobs = blobs

    async def save(self, session_id: UUID, title: str, png: bytes) -> Chart:
        chart_id = uuid4()
        location = await self._blobs.put(f"charts/{session_id}/{chart_id}.png", png, "image/png")
        async with self._sessions.begin() as session:
            session.add(ChartRow(id=chart_id, session_id=session_id, title=title, path=location))
        return Chart(id=chart_id, title=title, path=location)

    async def get(self, chart_id: UUID, session_id: UUID) -> Chart:
        async with self._sessions() as session:
            row = await session.get(ChartRow, chart_id)
            if row is None or row.session_id != session_id:
                raise NotFoundError(f"chart {chart_id} not found in this conversation")
            return Chart(id=row.id, title=row.title, path=row.path)

    async def read(self, chart_id: UUID, session_id: UUID) -> bytes:
        chart = await self.get(chart_id, session_id)
        return await self._blobs.get(chart.path)
