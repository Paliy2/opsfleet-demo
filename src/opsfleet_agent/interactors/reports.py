import hashlib
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from opsfleet_agent.core.errors import ConfirmationExpiredError, NotFoundError
from opsfleet_agent.entities.reports import (
    DeletionFilter,
    DeletionOutcome,
    DeletionPlan,
    DeletionStatus,
    Report,
    ReportSummary,
)
from opsfleet_agent.interactors.pii_scan import ensure_no_pii
from opsfleet_agent.ports.reports import DeletionPlanRepo, ReportRepo


class SaveReport:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(
        self, owner_id: str, session_id: UUID, title: str, body: str, chart_paths: Sequence[str]
    ) -> Report:
        ensure_no_pii(f"{title}\n{body}")
        return await self._reports.save(owner_id, session_id, title, body, chart_paths)


class ListReports:
    def __init__(self, reports: ReportRepo) -> None:
        self._reports = reports

    async def __call__(self, owner_id: str, text_contains: str | None) -> tuple[ReportSummary, ...]:
        return await self._reports.search(owner_id, text_contains=text_contains)


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
