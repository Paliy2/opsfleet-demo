from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from opsfleet_agent.core.errors import ConfirmationExpiredError, MaskedSearchError, NotFoundError
from opsfleet_agent.entities.reports import (
    REPORT_PAGE_SIZE,
    DeletionFilter,
    DeletionStatus,
    Report,
    ReportFilter,
)
from opsfleet_agent.interactors.reports import (
    ConfirmDeletion,
    ExpireDeletionPlans,
    GetReport,
    ListReports,
    PlanDeletion,
    SearchReports,
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
        "k", "mgr_a", SESSION, DeletionFilter(include=("client x",))
    )

    assert {item.title for item in plan.items} == {"Q1 review", "Old report"}


async def test_plan_scoped_to_this_conversation(reports: FakeReports) -> None:
    plan = await planner(reports, FakePlans())(
        "k", "mgr_a", SESSION, DeletionFilter(this_conversation=True)
    )

    assert {item.title for item in plan.items} == {"Q1 review", "Brand mix"}


async def test_replanning_with_same_key_returns_same_plan(reports: FakeReports) -> None:
    plans = FakePlans()
    first = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))
    reports.rows.append(StoredReport("mgr_a", SESSION, "New Client X", "added later"))

    second = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))

    assert second == first


async def test_no_match_raises(reports: FakeReports) -> None:
    with pytest.raises(NotFoundError):
        await planner(reports, FakePlans())(
            "k", "mgr_a", SESSION, DeletionFilter(include=("nothing",))
        )


@pytest.mark.parametrize("text", ["[PERSON_NAME]", "reports for [EMAIL_ADDRESS]"])
async def test_masked_text_is_refused_before_any_search(reports: FakeReports, text: str) -> None:
    reports.rows.append(StoredReport("mgr_a", SESSION, f"About {text}", "body"))

    with pytest.raises(MaskedSearchError):
        await planner(reports, FakePlans())("k", "mgr_a", SESSION, DeletionFilter(include=(text,)))
    with pytest.raises(MaskedSearchError):
        await SearchReports(reports)("mgr_a", SESSION, ReportFilter(exclude=(text,)), 1)


async def test_confirm_deletes_exactly_the_previewed_reports(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)(
        "k", "mgr_a", SESSION, DeletionFilter(include=("client x",))
    )
    reports.rows.append(StoredReport("mgr_a", SESSION, "Late Client X", "created after preview"))

    outcome = await ConfirmDeletion(reports, plans, now=lambda: NOW)(plan.id, "mgr_a", True)

    assert outcome.deleted == 2
    assert {r.title for r in reports.rows} == {"Brand mix", "Their report", "Late Client X"}


async def test_declined_confirmation_deletes_nothing(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))

    outcome = await ConfirmDeletion(reports, plans, now=lambda: NOW)(plan.id, "mgr_a", False)

    assert outcome.cancelled
    assert len(reports.rows) == 4


async def test_expired_confirmation_is_rejected(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))
    later = NOW + timedelta(minutes=6)

    with pytest.raises(ConfirmationExpiredError):
        await ConfirmDeletion(reports, plans, now=lambda: later)(plan.id, "mgr_a", True)
    assert len(reports.rows) == 4


async def test_plan_cannot_be_confirmed_twice(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))
    confirm = ConfirmDeletion(reports, plans, now=lambda: NOW)
    await confirm(plan.id, "mgr_a", True)

    with pytest.raises(ConfirmationExpiredError):
        await confirm(plan.id, "mgr_a", True)


async def test_other_user_cannot_confirm_plan(reports: FakeReports) -> None:
    plans = FakePlans()
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))

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
    plan = await planner(reports, plans)("k", "mgr_a", SESSION, DeletionFilter(include=("x",)))

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
    items = await ListReports(reports)("mgr_b")

    assert [item.title for item in items] == ["Their report"]


def search(reports: FakeReports, page: int = 1, **criteria: Any) -> Any:
    return SearchReports(reports)("mgr_a", SESSION, ReportFilter(**criteria), page)


async def test_search_excludes_reports_mentioning_a_term(reports: FakeReports) -> None:
    found = await search(reports, exclude=("client x",))

    assert [hit.title for hit in found.items] == ["Brand mix"]


async def test_misspelled_term_falls_back_to_close_spellings(reports: FakeReports) -> None:
    found = await search(reports, include=("Dieesel",))

    assert [hit.title for hit in found.items] == ["Brand mix"]
    assert found.corrected == ("Dieesel",)


async def test_exact_matches_win_over_close_spellings(reports: FakeReports) -> None:
    reports.rows.append(StoredReport("mgr_a", SESSION, "Kevin Klein notes", "body"))
    reports.rows.append(StoredReport("mgr_a", SESSION, "Calvin Klein review", "body"))

    found = await search(reports, include=("Kevin Klein",))

    assert [hit.title for hit in found.items] == ["Kevin Klein notes"]
    assert found.corrected == ()


async def test_misspelled_exclusion_also_falls_back(reports: FakeReports) -> None:
    reports.rows.append(StoredReport("mgr_a", SESSION, "Calvin Klein review", "body"))

    found = await search(reports, exclude=("Kalvin Klein",))

    assert "Calvin Klein review" not in {hit.title for hit in found.items}
    assert found.corrected == ("Kalvin Klein",)


async def test_short_terms_never_fall_back_to_close_spellings(reports: FakeReports) -> None:
    found = await search(reports, include=("abc",))

    assert found.total == 0
    assert found.corrected == ()


async def test_results_are_paged_with_a_hard_page_size() -> None:
    many = FakeReports(
        rows=[StoredReport("mgr_a", SESSION, f"Report {index}", "body") for index in range(70)]
    )

    first = await search(many)
    last = await search(many, page=3)

    assert (len(first.items), first.total, first.pages) == (REPORT_PAGE_SIZE, 70, 3)
    assert len(last.items) == 70 - 2 * REPORT_PAGE_SIZE


async def test_page_below_one_is_rejected(reports: FakeReports) -> None:
    with pytest.raises(ValueError, match="page"):
        await search(reports, page=0)


async def test_deletion_plans_every_match_not_just_one_page() -> None:
    many = FakeReports(
        rows=[StoredReport("mgr_a", SESSION, f"Client X {index}", "body") for index in range(70)]
    )

    plan = await planner(many, FakePlans())(
        "k", "mgr_a", SESSION, DeletionFilter(include=("client x",))
    )

    assert len(plan.items) == 70


def test_deletion_needs_a_criterion() -> None:
    with pytest.raises(ValueError, match="criterion"):
        DeletionFilter()
