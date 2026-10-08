from datetime import timedelta
from uuid import uuid4

import pytest

from opsfleet_agent.core.errors import ConfirmationExpiredError, NotFoundError
from opsfleet_agent.entities.reports import (
    DeletionFilter,
)
from opsfleet_agent.interactors.reports import ConfirmDeletion, PlanDeletion
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
