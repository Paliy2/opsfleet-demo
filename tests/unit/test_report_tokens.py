from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from opsfleet_agent.core.errors import (
    AmbiguousCustomerError,
    MaskedSearchError,
    NotFoundError,
    PiiValueExpiredError,
)
from opsfleet_agent.entities.access import AccessScope, Role
from opsfleet_agent.entities.pii_tokens import CustomerMatch, TurnValue
from opsfleet_agent.entities.reports import DeletionFilter, ReportFilter, ReportPage
from opsfleet_agent.gateways.pii_vault.vaults import SealedPiiValueVault
from opsfleet_agent.interactors.reports import PlanDeletion, ResolveCustomerTerms, SearchReports
from tests.fakes import (
    NOW,
    FakeCustomerDirectory,
    FakePlans,
    FakeReports,
    StoredReport,
    customer,
    customer_lookup,
    value_vault,
)

SESSION = uuid4()
MGR_A = AccessScope(user_id="mgr_a", role=Role.MANAGER, brands=frozenset({"Diesel"}))
JANE = customer("1a2b3c4d5e6f7a8b")
NAMESAKE = customer("9f8e7d6c5b4a3f2e")
ABSENT = customer("0123456789abcdef")


def library() -> FakeReports:
    return FakeReports(
        rows=[
            StoredReport("mgr_a", SESSION, "Top customers", "Morgan Costa (user_1a2b3c4d@x.com)"),
            StoredReport("mgr_a", SESSION, "Churn notes", "Morgan Costa (user_9f8e7d6c@x.com)"),
            StoredReport("mgr_a", SESSION, "Ids", "customer id 1a2b3c4d5e6f7a8b bought twice"),
            StoredReport("mgr_a", SESSION, "Brand mix", "Diesel up 20%"),
            StoredReport("mgr_b", SESSION, "Their notes", "Morgan Costa (user_1a2b3c4d@x.com)"),
        ]
    )


async def typed_values(**values: str) -> SealedPiiValueVault:
    vault = value_vault()
    await vault.put(
        "mgr_a",
        SESSION,
        [
            TurnValue(label=label, info_type=label.rsplit("_", 1)[0], value=SecretStr(raw))
            for label, raw in values.items()
        ],
    )
    return vault


def terms(
    reports: FakeReports, vault: SealedPiiValueVault, **found: tuple[CustomerMatch, ...]
) -> ResolveCustomerTerms:
    return ResolveCustomerTerms(reports, customer_lookup(vault, FakeCustomerDirectory(found)))


async def search(
    reports: FakeReports, customers: ResolveCustomerTerms, **criteria: object
) -> ReportPage:
    return await SearchReports(reports, customers)(
        MGR_A, SESSION, ReportFilter.model_validate(criteria), 1
    )


def planner(
    reports: FakeReports, customers: ResolveCustomerTerms, plans: FakePlans
) -> PlanDeletion:
    return PlanDeletion(reports, plans, customers, ttl=timedelta(minutes=5), now=lambda: NOW)


async def test_a_typed_name_finds_reports_by_pseudonymous_id_never_by_surrogate_name() -> None:
    reports = library()
    vault = await typed_values(PERSON_NAME_1="jane roe")

    page = await search(
        reports, terms(reports, vault, **{"jane roe": (JANE,)}), include=("[PERSON_NAME_1]",)
    )

    assert {hit.title for hit in page.items} == {"Top customers", "Ids"}
    assert [(m.token, m.customer.id) for m in page.customers] == [("PERSON_NAME_1", JANE.id)]


async def test_an_unknown_person_and_a_customer_without_reports_look_the_same() -> None:
    reports = library()
    vault = await typed_values(PERSON_NAME_1="jane roe", PERSON_NAME_2="john doe")
    customers = terms(reports, vault, **{"john doe": (ABSENT,)})

    unknown = await search(reports, customers, include=("PERSON_NAME_1",))
    no_reports = await search(reports, customers, include=("PERSON_NAME_2",))

    assert unknown == no_reports
    assert unknown.total == 0
    assert unknown.customers == ()


async def test_excluding_a_customer_by_token() -> None:
    reports = library()
    vault = await typed_values(EMAIL_ADDRESS_1="jane.roe@example.org")
    customers = terms(reports, vault, **{"jane.roe@example.org": (JANE,)})

    page = await search(reports, customers, include=("Morgan",), exclude=("EMAIL_ADDRESS_1",))

    assert [hit.title for hit in page.items] == ["Churn notes"]


async def test_deletion_lists_the_matched_customer_and_the_exact_reports() -> None:
    reports = library()
    vault = await typed_values(EMAIL_ADDRESS_1="jane.roe@example.org")
    customers = terms(reports, vault, **{"jane.roe@example.org": (JANE,)})

    preview = await planner(reports, customers, FakePlans())(
        "thread", MGR_A, SESSION, DeletionFilter(include=("[EMAIL_ADDRESS_1]",))
    )

    assert {item.title for item in preview.preview} == {"Top customers", "Ids"}
    assert [(m.token, m.customer.email) for m in preview.customers] == [
        ("EMAIL_ADDRESS_1", "user_1a2b3c4d@example.com")
    ]


async def test_deletion_refuses_a_name_shared_by_customers_in_the_users_reports() -> None:
    reports = library()
    vault = await typed_values(PERSON_NAME_1="morgan costa")
    customers = terms(reports, vault, **{"morgan costa": (JANE, NAMESAKE)})

    with pytest.raises(AmbiguousCustomerError) as raised:
        await planner(reports, customers, FakePlans())(
            "thread", MGR_A, SESSION, DeletionFilter(include=("PERSON_NAME_1",))
        )

    assert "user_1a2b3c4d" in raised.value.detail
    assert len(reports.rows) == 5


async def test_namesakes_outside_the_users_reports_do_not_block_deletion() -> None:
    reports = library()
    vault = await typed_values(PERSON_NAME_1="jane roe")
    customers = terms(reports, vault, **{"jane roe": (JANE, ABSENT)})

    preview = await planner(reports, customers, FakePlans())(
        "thread", MGR_A, SESSION, DeletionFilter(include=("PERSON_NAME_1",))
    )

    assert [m.customer.id for m in preview.customers] == [JANE.id]


async def test_resuming_a_planned_deletion_needs_no_raw_value() -> None:
    reports, plans = library(), FakePlans()
    vault = await typed_values(PERSON_NAME_1="jane roe")
    customers = terms(reports, vault, **{"jane roe": (JANE,)})
    criteria = DeletionFilter(include=("PERSON_NAME_1",))
    first = await planner(reports, customers, plans)("thread", MGR_A, SESSION, criteria)

    await vault.forget_session("mgr_a", SESSION)
    resumed = await planner(reports, customers, plans)("thread", MGR_A, SESSION, criteria)

    assert resumed.plan_id == first.plan_id
    with pytest.raises(PiiValueExpiredError):
        await planner(reports, customers, plans)("other-turn", MGR_A, SESSION, criteria)


async def test_an_unknown_customer_has_nothing_to_delete() -> None:
    reports = library()
    vault = await typed_values(PERSON_NAME_1="jane roe")

    with pytest.raises(NotFoundError):
        await planner(reports, terms(reports, vault), FakePlans())(
            "thread", MGR_A, SESSION, DeletionFilter(include=("PERSON_NAME_1",))
        )


@pytest.mark.parametrize("text", ["[PERSON_NAME]", "reports about [PERSON_NAME_1]"])
async def test_placeholders_that_are_not_a_whole_token_term_are_still_refused(text: str) -> None:
    reports = library()
    customers = terms(reports, await typed_values(PERSON_NAME_1="jane roe"))

    with pytest.raises(MaskedSearchError):
        await search(reports, customers, include=(text,))


async def test_other_owners_reports_are_never_matched() -> None:
    reports = FakeReports(rows=[library().rows[-1]])
    vault = await typed_values(PERSON_NAME_1="jane roe")

    page = await search(
        reports, terms(reports, vault, **{"jane roe": (JANE,)}), include=("PERSON_NAME_1",)
    )

    assert page.total == 0
