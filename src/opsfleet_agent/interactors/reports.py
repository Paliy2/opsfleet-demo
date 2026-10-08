import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from opsfleet_agent.core.errors import ConfirmationExpiredError, MaskedSearchError, NotFoundError
from opsfleet_agent.entities.privacy import is_masked
from opsfleet_agent.entities.reports import (
    DeletionFilter,
    DeletionOutcome,
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportSummary,
)
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.ports.blobs import BlobStore
from opsfleet_agent.ports.reports import DeletionPlanRepo, ReportRenderer, ReportRepo


class SaveReport:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report:
        ensure_no_pii(f"{title}\n{body}")
        return await self._reports.save(owner_id, session_id, title, body, chart_paths)


def _ensure_searchable(text_contains: str | None) -> None:
    if is_masked(text_contains):
        raise MaskedSearchError(f"search text holds a privacy placeholder: {text_contains}")


class ListReports:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(self, owner_id: str, text_contains: str | None) -> tuple[ReportSummary, ...]:
        _ensure_searchable(text_contains)
        return await self._reports.search(owner_id, text_contains=text_contains)


class GetReport:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(self, owner_id: str, report_id: UUID) -> Report:
        report = await self._reports.get_owned(owner_id, report_id)
        if report.owner_id != owner_id:
            raise NotFoundError(f"report {report_id} not found for this user")
        return report


class ExportReport:
    def __init__(self, get_report: GetReport, blobs: BlobStore, renderer: ReportRenderer) -> None:
        self._get_report = get_report
        self._blobs = blobs
        self._renderer = renderer

    async def __call__(self, owner_id: str, report_id: UUID) -> tuple[Report, bytes]:
        report = await self._get_report(owner_id, report_id)
        charts: list[bytes] = []
        for path in report.chart_paths:
            try:
                charts.append(await self._blobs.get(path))
            except NotFoundError:
                continue
        missing = len(report.chart_paths) - len(charts)
        return report, await self._renderer.render(report, tuple(charts), missing)


class PlanDeletion:
    def __init__(
        self,
        reports: ReportRepo,
        plans: DeletionPlanRepo,
        ttl: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        self._reports = reports
        self._plans = plans
        self._ttl = ttl
        self._now = now

    async def __call__(
        self, thread_id: str, owner_id: str, session_id: UUID, criteria: DeletionFilter
    ) -> DeletionPlan:
        _ensure_searchable(criteria.text_contains)
        matches = await self._reports.search(
            owner_id,
            text_contains=criteria.text_contains,
            session_id=session_id if criteria.this_conversation else None,
            report_ids=criteria.report_ids,
        )
        if not matches:
            raise NotFoundError("no reports you own match that request")
        digest = hashlib.sha256(criteria.model_dump_json().encode()).hexdigest()
        return await self._plans.get_or_create(
            f"{thread_id}:{digest}", owner_id, matches, self._now() + self._ttl
        )


class ConfirmDeletion:
    def __init__(
        self, reports: ReportRepo, plans: DeletionPlanRepo, now: Callable[[], datetime]
    ) -> None:
        self._reports = reports
        self._plans = plans
        self._now = now

    async def __call__(self, plan_id: UUID, owner_id: str, approved: bool) -> DeletionOutcome:
        plan = await self._plans.get(plan_id, owner_id)
        if plan.status is not DeletionStatus.PENDING or plan.expires_at < self._now():
            raise ConfirmationExpiredError(f"plan {plan_id} is {plan.status} or expired")
        if not approved:
            await self._plans.close(plan.id, DeletionStatus.CANCELLED, deleted=0)
            return DeletionOutcome(deleted=0, cancelled=True)
        deleted = await self._reports.delete_owned(owner_id, [item.id for item in plan.items])
        await self._plans.close(plan.id, DeletionStatus.EXECUTED, deleted=deleted)
        return DeletionOutcome(deleted=deleted, cancelled=False)


class ExpireDeletionPlans:
    def __init__(self, plans: DeletionPlanRepo, now: Callable[[], datetime]) -> None:
        self._plans = plans
        self._now = now

    async def __call__(self) -> int:
        return await self._plans.expire_pending(self._now())
