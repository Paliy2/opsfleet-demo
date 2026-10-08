from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from opsfleet_agent.core.errors import ConfirmationExpiredError, NotFoundError
from opsfleet_agent.entities.reports import DeletionFilter, DeletionStatus, Report
from opsfleet_agent.interactors.reports import (
    ConfirmDeletion,
    ExpireDeletionPlans,
    GetReport,
    ListReports,
    PlanDeletion,
)
from tests.fakes import NOW, FakePlans, FakeReports, StoredReport

SESSION = uuid4()


@pytest.fixture
def reports() -> FakeReports:
    return FakeReports(
        rows=[
            StoredReport("mgr_a", SESSION, "Q1 review", "Client X churn spiked"),
            StoredReport("mgr_a", uuid4(), "Old report", "Client X revenue"),
            StoredReport("mgr_a", SESSION, "Brand mix", "Diesel up 20%"),
            StoredReport("mgr_b", SESSION, "Their report", "Client X too"),
        ]
    )


def planner(reports: FakeReports, plans: FakePlans) -> PlanDeletion:
    return PlanDeletion(reports, plans, ttl=timedelta(minutes=5), now=lambda: NOW)


async def test_plan_matches_only_own_reports_mentioning_text(reports: FakeReports) -> None:
    plan = await planner(reports, FakePlans())(
        "k", "mgr_a", SESSION, DeletionFilter(text_contains="client x")
    )

    assert {item.title for item in plan.items} == {"Q1 review", "Old report"}


async def test_plan_scoped_to_this_conversation(reports: FakeReports) -> None:
    plan = await planner(reports, FakePlans())(
        "k", "mgr_a", SESSION, DeletionFilter(this_conversation=True)
    )

    assert {item.title for item in plan.items} == {"Q1 review", "Brand mix"}


async def test_replanning_with_same_key_returns_same_plan(reports: FakeReports) -> None:
    plans = FakePlans()
    first = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))
    reports.rows.append(StoredReport("mgr_a", SESSION, "New Client X", "added later"))

    second = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))

    assert second == first


async def test_no_match_raises(reports: FakeReports) -> None:
    with pytest.raises(NotFoundError):
        await planner(reports, FakePlans())(
            "k", "mgr_a", SESSION, DeletionFilter(text_contains="nothing")
        )


async def test_confirm_deletes_exactly_the_previewed_reports(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)(
        "k", "mgr_a", SESSION, DeletionFilter(text_contains="client x")
    )
    reports.rows.append(StoredReport("mgr_a", SESSION, "Late Client X", "created after preview"))

    outcome = await ConfirmDeletion(reports, plans, now=lambda: NOW)(plan.id, "mgr_a", True)

    assert outcome.deleted == 2
    assert {r.title for r in reports.rows} == {"Brand mix", "Their report", "Late Client X"}


async def test_declined_confirmation_deletes_nothing(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))

    outcome = await ConfirmDeletion(reports, plans, now=lambda: NOW)(plan.id, "mgr_a", False)

    assert outcome.cancelled
    assert len(reports.rows) == 4


async def test_expired_confirmation_is_rejected(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))
    later = NOW + timedelta(minutes=6)

    with pytest.raises(ConfirmationExpiredError):
        await ConfirmDeletion(reports, plans, now=lambda: later)(plan.id, "mgr_a", True)
    assert len(reports.rows) == 4


async def test_plan_cannot_be_confirmed_twice(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))
    confirm = ConfirmDeletion(reports, plans, now=lambda: NOW)
    await confirm(plan.id, "mgr_a", True)

    with pytest.raises(ConfirmationExpiredError):
        await confirm(plan.id, "mgr_a", True)


async def test_other_user_cannot_confirm_plan(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))

    with pytest.raises(NotFoundError):
        await ConfirmDeletion(reports, plans, now=lambda: NOW)(plan.id, "mgr_b", True)
    assert len(reports.rows) == 4


async def test_plan_key_stays_bounded_for_long_criteria(reports: FakeReports) -> None:
    plans = FakePlans()
    owned = [row.id for row in reports.rows if row.owner_id == "mgr_a"]
    criteria = DeletionFilter(report_ids=(*owned, *(uuid4() for _ in range(20))))

    await planner(reports, plans)("thread", "mgr_a", SESSION, criteria)

    assert all(len(key) <= 80 for key in plans.keys)


async def test_expiry_closes_only_stale_pending_plans(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(text_contains="x"))

    assert await ExpireDeletionPlans(plans, now=lambda: NOW)() == 0
    assert await ExpireDeletionPlans(plans, now=lambda: NOW + timedelta(minutes=6))() == 1
    assert plans.plans[plan.id].status is DeletionStatus.EXPIRED


def owned_by(reports: FakeReports, owner_id: str, title: str) -> StoredReport:
    return next(row for row in reports.rows if row.owner_id == owner_id and row.title == title)


async def test_owner_can_open_own_report(reports: FakeReports) -> None:
    mine = owned_by(reports, "mgr_a", "Q1 review")

    report = await GetReport(reports)("mgr_a", mine.id)

    assert (report.title, report.body) == ("Q1 review", "Client X churn spiked")


async def test_other_user_cannot_open_report(reports: FakeReports) -> None:
    theirs = owned_by(reports, "mgr_a", "Q1 review")

    with pytest.raises(NotFoundError):
        await GetReport(reports)("mgr_b", theirs.id)


async def test_missing_report_is_not_found(reports: FakeReports) -> None:
    with pytest.raises(NotFoundError):
        await GetReport(reports)("mgr_a", uuid4())


class LeakyReports(FakeReports):
    async def get_owned(self, owner_id: str, report_id: UUID) -> Report:
        row = next(row for row in self.rows if row.id == report_id)
        return await super().get_owned(row.owner_id, report_id)


async def test_get_report_rejects_foreign_report_even_if_repo_leaks(
    reports: FakeReports,
) -> None:
    leaky = LeakyReports(rows=reports.rows)
    theirs = owned_by(reports, "mgr_b", "Their report")

    with pytest.raises(NotFoundError):
        await GetReport(leaky)("mgr_a", theirs.id)


async def test_list_returns_only_own_reports(reports: FakeReports) -> None:
    items = await ListReports(reports)("mgr_b", None)

    assert [item.title for item in items] == ["Their report"]
