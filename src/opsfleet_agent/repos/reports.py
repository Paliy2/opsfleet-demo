from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    SQLColumnExpression,
    delete,
    func,
    literal,
    not_,
    null,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.entities.reports import (
    Chart,
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportHit,
    ReportMatches,
    ReportQuery,
    ReportSummary,
    SearchTerm,
)
from opsfleet_agent.models.tables import ChartRow, DeletionPlanRow, ReportRow
from opsfleet_agent.ports.blobs import BlobStore
from opsfleet_agent.ports.reports import ChartRepo, DeletionPlanRepo, ReportRepo

_FUZZY_THRESHOLD = text("SET LOCAL pg_trgm.word_similarity_threshold = 0.55")
_SNIPPET_CONTEXT = 60


def _counted(owner_id: str, query: ReportQuery) -> Select[int]:
    return select(func.count()).select_from(ReportRow).where(*_filters(owner_id, query))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _term_clause(term: SearchTerm) -> ColumnElement[bool]:
    if term.fuzzy:
        return literal(term.text).bool_op("<%")(ReportRow.search_text)
    return ReportRow.search_text.ilike(f"%{_escape_like(term.text)}%", escape="\\")


def _filters(owner_id: str, query: ReportQuery) -> list[ColumnElement[bool]]:
    clauses = [ReportRow.owner_id == owner_id]
    clauses += [_term_clause(term) for term in query.include]
    clauses += [not_(_term_clause(term)) for term in query.exclude]
    if query.created_after is not None:
        clauses.append(ReportRow.created_at >= query.created_after)
    if query.created_before is not None:
        clauses.append(ReportRow.created_at < query.created_before)
    if query.session_id is not None:
        clauses.append(ReportRow.session_id == query.session_id)
    if query.report_ids:
        clauses.append(ReportRow.id.in_(query.report_ids))
    return clauses


def _ranked(owner_id: str, query: ReportQuery, *columns: SQLColumnExpression[Any]) -> Select[Any]:
    statement = select(*columns).where(*_filters(owner_id, query))
    if query.include:
        relevance = func.greatest(
            *(func.word_similarity(term.text, ReportRow.search_text) for term in query.include)
        )
        statement = statement.order_by(relevance.desc())
    return statement.order_by(ReportRow.created_at.desc())


def _snippet_columns(terms: tuple[SearchTerm, ...]) -> tuple[ColumnElement[Any], ...]:
    lowered = func.lower(ReportRow.body)
    positions = [func.nullif(func.strpos(lowered, term.text.lower()), 0) for term in terms]
    first = func.least(*positions) - 1 if positions else null()
    start = func.coalesce(func.greatest(first - _SNIPPET_CONTEXT, 0), 0)
    end = func.coalesce(first + _SNIPPET_CONTEXT * 2, _SNIPPET_CONTEXT * 2)
    return (
        start.label("start"),
        func.substr(ReportRow.body, start + 1, end - start).label("excerpt"),
        func.length(ReportRow.body).label("length"),
    )


def _hit(row: Row[Any]) -> ReportHit:
    excerpt = " ".join(row.excerpt.split())
    tail = "…" if row.start + len(row.excerpt) < row.length else ""
    return ReportHit(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        snippet=f"{'…' if row.start else ''}{excerpt}{tail}",
    )


def _summary(row: Row[Any]) -> ReportSummary:
    return ReportSummary(id=row.id, title=row.title, created_at=row.created_at)


_SUMMARY_COLUMNS = (ReportRow.id, ReportRow.title, ReportRow.created_at)


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
        self, owner_id: str, query: ReportQuery, *, limit: int, offset: int
    ) -> ReportMatches:
        columns = (*_SUMMARY_COLUMNS, *_snippet_columns(query.include))
        async with self._sessions.begin() as session:
            await session.execute(_FUZZY_THRESHOLD)
            total = await session.scalar(_counted(owner_id, query))
            rows = await session.execute(
                _ranked(owner_id, query, *columns).limit(limit).offset(offset)
            )
            return ReportMatches(items=tuple(_hit(row) for row in rows), total=total or 0)

    async def matching(
        self, owner_id: str, query: ReportQuery, *, limit: int
    ) -> tuple[ReportSummary, ...]:
        async with self._sessions.begin() as session:
            await session.execute(_FUZZY_THRESHOLD)
            rows = await session.execute(_ranked(owner_id, query, *_SUMMARY_COLUMNS).limit(limit))
            return tuple(_summary(row) for row in rows)

    async def count(self, owner_id: str, query: ReportQuery) -> int:
        async with self._sessions.begin() as session:
            await session.execute(_FUZZY_THRESHOLD)
            return await session.scalar(_counted(owner_id, query)) or 0

    async def present_terms(self, owner_id: str, texts: Sequence[str]) -> frozenset[str]:
        hits = [
            func.bool_or(_term_clause(SearchTerm(text=text))).label(f"t{index}")
            for index, text in enumerate(texts)
        ]
        async with self._sessions() as session:
            row = (await session.execute(select(*hits).where(ReportRow.owner_id == owner_id))).one()
        return frozenset(text for text, present in zip(texts, row, strict=True) if present)

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
